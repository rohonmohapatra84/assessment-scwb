"""The lead agent's runtime: ask the model for a plan and for each next decision, and CHECK what comes back.

`ModelLead` is the Python side of agents/project_lead.md (the Markdown holds the role and the prompt). `check_plan` is
where "controlled autonomy" is enforced for planning: the model may choose which tools to use and how they depend on
each other, but anything that breaks a catalog rule is rejected with a message the model can act on.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ..agent_specs import load_spec
from ..providers import LLMTransient, LLMUnavailable
from .core import Task
from .loop import LeadError, PlanError, _ancestors, validate_dag
from .tools import BASE_FACTS, REPO_FACTS, REQUIRED_TOOLS, available, catalog_for_prompt

MAX_TASKS = 14
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ModelLead:
    """Asks the project_lead agent. Raises LeadError only when the provider is unusable; a malformed reply is returned
    as a marker so the caller can show the model what was wrong and ask again."""
    name = "project_lead"
    model_backed = True  # defined in agents/project_lead.md

    def __init__(self, llm: Any, events: Any = None):
        self.llm, self.events = llm, events

    def plan(self, req: Dict[str, Any], generator: str, facts: List[str], feedback: Optional[str] = None) -> Dict[str, Any]:
        pt = req["plan_type"]
        kind = req.get("change_kind") if pt == "llm_change" else None
        payload = {"plan_type": pt, "generator": generator, "change_kind": kind,
                   "requirement": {"text": str(req.get("raw", ""))[:600], "problem": str(req.get("problem_statement", ""))[:600],
                                   "capabilities": json.dumps(req.get("capabilities"), default=str)[:1500],
                                   "acceptance_criteria": json.dumps(req.get("acceptance_criteria"), default=str)[:1500]},
                   "facts_already_available": facts, "catalog": catalog_for_prompt(pt, generator, kind),
                   "limits": {"max_tasks": MAX_TASKS}}
        return self._ask("plan", payload, feedback, max_tokens=4000)

    def decide(self, observation: Dict[str, Any], feedback: Optional[str] = None) -> Dict[str, Any]:
        return self._ask("decide", observation, feedback, max_tokens=800)

    def _ask(self, kind: str, payload: Dict[str, Any], feedback: Optional[str], max_tokens: int) -> Dict[str, Any]:
        prompt = (f"## Your previous answer was rejected, fix this and answer again\n{feedback}\n\n" if feedback else "")
        prompt += "REQUEST_JSON:\n" + json.dumps({"kind": kind, **payload}, indent=1, default=str)
        started = time.time()
        for attempt in (1, 2, 3):
            try:
                reply = self.llm.complete_json(load_spec(self.name).prompt(), prompt, max_tokens=max_tokens)
                break
            except LLMTransient as exc:
                if attempt == 3:
                    raise LeadError(f"model unavailable after retries: {exc}") from exc
                time.sleep(0.5 * attempt)
            except LLMUnavailable as exc:
                if str(exc).startswith("model reply"):  # not JSON: a bad answer, not a dead provider
                    reply = {"__invalid__": str(exc)}
                    break
                raise LeadError(str(exc)) from exc
        if self.events is not None:
            self.events.emit("lead_call", ask=kind, seconds=round(time.time() - started, 1),
                             **{k: v for k, v in (getattr(self.llm, "usage", {}) or {}).items()})
        return reply if isinstance(reply, dict) else {"__invalid__": "reply was not a JSON object"}


def check_plan(raw: Dict[str, Any], plan_type: str, generator: str, facts: List[str],
               kind: Optional[str] = None) -> Tuple[List[Task], List[List[str]]]:
    """Turn the lead's reply into Tasks, or raise PlanError saying exactly what to fix.

    The lead supplies ids, tool names, titles and dependencies. Everything with safety consequences (write scopes,
    optional flags, retry budgets, who the validator may blame) is taken from the catalog instead.
    """
    if not isinstance(raw, dict) or raw.get("__invalid__"):
        raise PlanError(raw.get("__invalid__", "reply was not a JSON object") if isinstance(raw, dict) else "reply was not a JSON object")
    items = raw.get("tasks")
    if not isinstance(items, list) or not items:
        raise PlanError('reply must be {"rationale": "...", "tasks": [{"id","tool","title","deps"}]} with at least one task')
    if len(items) > MAX_TASKS:
        raise PlanError(f"{len(items)} tasks is over the limit of {MAX_TASKS}")
    allowed = {t.name: t for t in available(plan_type, generator, kind)}
    tasks: List[Task] = []
    used_tools: Dict[str, str] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PlanError(f"task {i} is not an object")
        tid, tool, deps = item.get("id"), item.get("tool"), item.get("deps", [])
        if not isinstance(tid, str) or not ID_RE.match(tid):
            raise PlanError(f"task {i}: id must be snake_case (letters, digits, underscore), got {tid!r}")
        if tool not in allowed:
            raise PlanError(f"task '{tid}': tool {tool!r} is not available for a {plan_type}{f' ({kind})' if kind else ''} plan in {generator} mode. "
                            f"Available tools: {sorted(allowed)}")
        if tool in used_tools:
            raise PlanError(f"tool '{tool}' is used twice ('{used_tools[tool]}' and '{tid}'); use each tool at most once")
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise PlanError(f"task '{tid}': deps must be a list of task ids")
        used_tools[tool] = tid
        spec = allowed[tool]
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        tasks.append(Task(id=tid, title=(title or spec.description)[:120], agent=tool, deps=list(dict.fromkeys(deps)),
                          description=spec.description, writes=list(spec.for_plan("writes", plan_type, kind)),
                          optional=plan_type in spec.optional_in, max_attempts=spec.max_attempts))
    for name in REQUIRED_TOOLS:
        if name not in used_tools:
            raise PlanError(f"the plan must include the '{name}' tool")
    if plan_type == "llm_change":  # a change must actually change something that fits its kind
        must = {"spec_tester", "doc_writer"} if kind == "tests_docs" else {"code_author"}
        if not must & set(used_tools):
            raise PlanError(f"a {kind or 'change'} change needs at least one of {sorted(must)} in the plan")
    waves = validate_dag(tasks)  # duplicate ids, unknown deps, cycles, concurrent write conflicts
    by_id = {t.id: t for t in tasks}
    for t in tasks:  # every fact a tool needs must come from discovery or from something it depends on
        have = set(facts)
        for a in _ancestors(t.id, by_id):
            have |= set(allowed[by_id[a].agent].for_plan("provides", plan_type, kind))
        missing = [n for n in allowed[t.agent].for_plan("needs", plan_type, kind) if n not in have]
        if missing:
            makers = sorted(x.name for x in allowed.values() if set(x.for_plan("provides", plan_type, kind)) & set(missing))
            raise PlanError(f"task '{t.id}' ({t.agent}) needs {missing} but none of its dependencies provides it. "
                            f"Tools that provide it: {makers}. Add one and make '{t.id}' depend on it.")
    validator, summarizer = by_id[used_tools["validator"]], by_id[used_tools["summarizer"]]
    before_validator = _ancestors(validator.id, by_id)
    stray = [t.id for t in tasks if t.id not in (validator.id, summarizer.id) and t.id not in before_validator]
    if stray:
        raise PlanError(f"the validator must depend (directly or transitively) on every other task except the summarizer; missing: {stray}")
    if validator.id not in _ancestors(summarizer.id, by_id):
        raise PlanError("the summarizer must depend on the validator")
    validator.repair_targets = [t.id for t in tasks if allowed[t.agent].repairable and t.id in before_validator]
    return tasks, waves


def facts_for(has_repo: bool) -> List[str]:
    """Facts that exist before any task runs: the requirement, and the codebase model when there is a repo."""
    return list(BASE_FACTS) + (list(REPO_FACTS) if has_repo else [])
