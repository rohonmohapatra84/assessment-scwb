"""The run story: a plain-English account of what happened in a run, written LIVE to <out>/RUN_STORY.md.

events.jsonl is the audit trail (complete, machine-readable). The story is the same facts told in order for a person who has
never seen the system: which stage we are in, who acts (a model-backed AGENT or a plain-Python STEP), what they decided, what the
validator checked and why it matters. For a bug-fix run it opens with the scenario setup (what bug was planted and where) and, when
the code agent finishes, quotes the actual change it made.

The Narrator only LISTENS to the event log and reads the blackboard and workspace; it never changes the run, and a failure inside
it is swallowed by the event log, so it can never break a run.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# model-backed agents; everything else that runs a task is a deterministic step
MODEL_AGENTS = ("code_author", "spec_tester", "requirement_analyst", "project_lead")

ROLE_LINE = {
    "code_author": "It writes the code change. For existing code that means minimal anchored edits (each `find` text must match the original exactly once), "
                   "so the result is a small reviewable diff, not a rewrite.",
    "spec_tester": "It writes the tests from the written contract only. It never sees the code, so its tests are an independent check, not a copy of the code's behaviour.",
}

CHECK_MEANING = {
    "static.syntax": "every Python file parses",
    "static.secrets": "no password, key or token was written into the code",
    "static.required_files": "the files the service needs are present",
    "probe.behaviour": "14 black-box probes drove the service like a real client (create, redirect, stats, delete, restart)",
    "probe.schema_conformance": "the database the code creates has every table and column that was designed",
    "tests.suite": "the whole test suite was executed in an isolated process",
    "regression.baseline_green": "the tests that passed before still pass",
    "regression.suite_green_after_fix": "the suite is green after the fix (for a bug it may have started red)",
    "regression.new_tests_added": "new tests were added",
    "regression.suite_not_smaller": "no test was lost",
    "regression.fails_on_old_code": "THE PROOF: the new tests FAIL on the original buggy code and PASS on the fix, so they really catch this bug",
    "compat.openapi_additive_only": "the public API only grew; nothing existing was removed or changed",
    "compat.openapi_unchanged": "the public API is identical to before",
    "tests.existing_tests_protected": "no existing test was edited, except an amendment a human must approve",
    "scope.tests_docs_only": "only tests and docs were written; production code is untouched",
    "traceability.acceptance_criteria": "every acceptance criterion has a test that names it",
    "traceability.risks": "risks that name a covering test resolve to a real test",
    "docs.present": "documentation exists",
    "plan.ambiguities_resolved": "every vague term has a human answer or a recorded default",
    "plan.objectives_measurable": "every objective has a metric and a target",
    "plan.options_with_tradeoffs": "every objective has evaluated options",
    "plan.verification_defined": "a verification plan exists",
    "docs.proposal_present": "the proposal document exists",
}

STATUS_MEANING = {
    "completed": "DONE. Validated, approved and released.",
    "completed_degraded": "DONE, but an optional task (such as documentation) failed. Released with a warning.",
    "needs_clarification": "PAUSED. The request is too vague; the system asked questions instead of guessing. Nothing was generated.",
    "rejected_at_plan": "STOPPED by a human at the plan gate. Nothing was generated.",
    "rejected_at_release": "STOPPED by a human at an approval gate. The work exists but nothing was released.",
    "plan_rejected": "STOPPED. The lead agent could not produce a valid plan in three tries.",
    "failed": "FAILED. A hard failure or the repair rounds ran out. Nothing was released. See the last FAIL below.",
}


def _first_line(text: Any, limit: int = 170) -> str:
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line if len(line) <= limit else line[:limit - 3] + "..."


class Narrator:
    def __init__(self, path: Path, *, bb=None, ws=None, preface: str = "", resume: bool = False):
        self.path = Path(path)
        self.bb, self.ws = bb, ws
        self._lock = threading.Lock()
        self._stage_no = 0
        self._written: Dict[str, List[str]] = {}
        self._agents: Dict[str, str] = {}  # task id -> tool name, learned from task_started events
        self._validation_round = 0
        self._preface = preface
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if resume and self.path.exists():
            self._w("\n---\n*(the run was resumed from its checkpoint)*\n")
        else:
            self.path.write_text("", encoding="utf-8")

    # ----------------------------------------------------------------- plumbing
    def _w(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")

    @staticmethod
    def _t(e: Dict[str, Any]) -> str:
        return time.strftime("%H:%M:%S", time.localtime(e["ts"]))

    def _stage(self, number: int, title: str, blurb: str = "") -> None:
        if self._stage_no == number:
            return
        self._stage_no = number
        self._w(f"\n## Stage {number}: {title}\n" + (f"\n{blurb}\n" if blurb else ""))

    def _say(self, e: Dict[str, Any], text: str) -> None:
        self._w(f"- `{self._t(e)}` {text}")

    def on_event(self, e: Dict[str, Any]) -> None:
        with self._lock:
            handler = getattr(self, "_on_" + e["kind"], None)
            if handler:
                handler(e)

    def _req(self) -> Dict[str, Any]:
        return (self.bb.get("requirement") if self.bb else None) or {}

    # ----------------------------------------------------------------- stages
    def _on_run_started(self, e):
        self._w("# Run story\n")
        self._w("This file tells, in plain English and in order, what happened in this run. The complete machine-readable record is "
                "`events.jsonl`; the final deliverable is `ENGINEERING_SUMMARY.md`.\n")
        self._w(f"- **Request:** {e.get('requirement')}")
        self._w(f"- **Existing code given:** {e.get('target_repo')}" if e.get("target_repo") not in (None, "-") else "- **Existing code given:** none (this builds something new)")
        self._w(f"- **Model provider:** {e.get('llm')}   **Human approval mode:** {e.get('approval')}\n")
        self._w("**Who acts in this story.** An **AGENT (model)** is a model with a role: `project_lead`, `requirement_analyst`, `code_author`, `spec_tester`. "
                "A **step (plain Python)** is deterministic code that makes no model call: design, analysis, validation. The **harness** is the "
                "plain-Python referee that checks every plan, sandboxes every file and stops at the human gates.")
        self._w("\n**What is in this folder.** You only need the first few; the rest are the machinery's own records.\n\n"
                "| File or folder | What it is | Do you need it? |\n|---|---|---|\n"
                "| `RUN_STORY.md` | this file: the run in plain English | **read this first** |\n"
                "| `DEMO_RESULT.md` | the released service actually used: real long URLs shortened, redirects followed, analytics read (written at the end) | **the visible payoff** |\n"
                "| `release/` | **the product**: the validated files (code, `openapi.json`, `schema.sql`, tests, docs; `changes.diff` for a change). Exists only after the human release gate | **yes: this is the deliverable** |\n"
                "| `ENGINEERING_SUMMARY.md` | the formal engineering report the assignment asks for: plan, rationale, artifacts, risks, validation, assumptions | the formal deliverable |\n"
                "| `input/` | a copy of exactly what this run was given, so the output explains itself | for reference |\n"
                "| `plan.json` | the task plan the lead agent proposed and the harness accepted | optional |\n"
                "| `events.jsonl` | the complete audit log, one JSON event per line | when debugging or auditing |\n"
                "| `summary.json` | status and gate decisions, for programs | machine use |\n"
                "| `state.json` | the checkpoint that lets `--resume` continue a stopped run | machine use |\n"
                "| `workspace/`, `baseline/` | the sandbox the agents wrote into, and a pristine copy of the existing code | internal; safe to ignore |\n")
        if self._preface:
            self._w("\n" + self._preface.strip() + "\n")
        self._stage(1, "Discover (nothing is generated yet)",
                    "The system first works out what is being asked and, when there is existing code, what state it is in.")

    def _on_repo_imported(self, e):
        self._say(e, f"Copied the existing code ({e.get('files')} files) into a sandbox. The original is never modified; all edits happen on the copy.")

    def _on_baseline_tests(self, e):
        n, ok = e.get("ran"), e.get("ok")
        names = ((self.bb.get("baseline_tests") or {}).get("failing") or []) if self.bb else []
        if ok:
            self._say(e, f"Ran the existing test suite BEFORE touching anything: **{n} tests, all green**. This is the regression reference: "
                         "after the change, these must still pass.")
        else:
            self._say(e, f"Ran the existing test suite BEFORE touching anything: **it is RED** ({e.get('failing', '?')} failing of {n}). "
                         "For a bug fix this is the symptom of the bug, recorded before any agent has done anything.")
            for name in names[:6]:
                self._w(f"    - failing: `{name}`")

    def _on_requirement_normalised(self, e):
        req = self._req()
        kind = req.get("change_kind")
        self._say(e, f"The requirement analyst understood the request as plan type **{e.get('plan_type')}**"
                     + (f", kind of change **{kind}**" if kind else "") + f", domain `{e.get('domain')}`.")

    def _on_codebase_indexed(self, e):
        self._say(e, f"The codebase analyst (a step) mapped the existing code: {e.get('modules')} modules, {e.get('routes')} routes, "
                     f"{e.get('tables')} tables, {e.get('tests')} test files. This is how the system knows what a change touches.")

    def _on_impact_analysed(self, e):
        impact = (self.bb.get("impact") if self.bb else None) or {}
        mods = ", ".join(f"`{m}`" for m in impact.get("modules", [])[:6])
        self._say(e, f"Impact analysis: the change touches {e.get('routes')} route(s), table(s) `{e.get('tables')}`, risk **{e.get('risk')}**"
                     + (f"; modules most likely to change: {mods}." if mods else "."))

    def _on_clarification_needed(self, e):
        self._say(e, f"The request is too vague ({e.get('questions')} unanswered question(s)). The system asks instead of guessing.")

    def _on_plan_rejected(self, e):
        self._say(e, f"The harness rejected the lead's plan (attempt {e.get('attempt')}): {_first_line(e.get('reason'), 200)}. The lead is asked again with that reason.")

    def _on_plan_proposed(self, e):
        self._stage(2, "Plan (the lead agent decomposes, the harness checks)")
        waves = [w.strip() for w in str(e.get("shape", "")).split("|")]
        self._say(e, f"**AGENT (model) `project_lead`** proposed {e.get('tasks')} tasks in {e.get('waves')} waves, and the harness accepted the plan "
                     f"(tools exist, dependencies satisfied, no write conflicts, one validator that waits for everything).")
        if e.get("rationale"):
            self._w(f"    - the lead's reasoning: {e.get('rationale')}")
        for i, w in enumerate(waves, 1):
            self._w(f"    - wave {i}: {w}   (tasks in one wave can run in parallel)")

    def _on_gate_requested(self, e):
        gate = e.get("gate")
        if gate == "plan_approval":
            self._say(e, "**Human gate 1: the plan.** Nothing is generated until it is approved.")
        else:
            self._stage(5, "Approval and release")
            what = {"test_update_approval": "an agent amended an existing test, so a person must read each diff and its reason",
                    "release_approval": "the validated result needs a final yes before any file leaves the sandbox"}.get(gate, "approval needed")
            self._say(e, f"**Human gate `{gate}`:** {what}.")

    def _on_gate_decided(self, e):
        self._say(e, f"Decision at `{e.get('gate')}`: **{'APPROVED' if e.get('approved') else 'REJECTED'}** by `{e.get('by')}` ({e.get('note')}).")

    def _on_dag_validated(self, e):
        self._stage(3, "Work (agents and steps run; the lead picks the order)",
                    "Tasks start when everything they need exists. The harness refuses anything not ready, retries flaky failures, and sends validator "
                    "failures back to whoever is responsible.")

    def _on_lead_decision(self, e):
        self._say(e, f"`project_lead` chose to start: `{e.get('tasks')}`. Why: {e.get('why')}")

    def _on_lead_fallback(self, e):
        self._say(e, f"The lead gave unusable answers, so the harness started every ready task itself ({e.get('started')}). Logged, not hidden.")

    def _desc(self, tool: str) -> str:
        try:
            from .tools import TOOLS
            return TOOLS[tool].description if tool in TOOLS else ""
        except Exception:
            return ""

    def _on_task_started(self, e):
        tool, task, gen = e.get("agent"), e.get("task"), int(e.get("generation") or 0)
        self._agents[str(task)] = tool
        if tool == "validator":
            self._validation_round += 1
            self._stage(4, "Validate (the referee executes checks; it never asks a model)")
            self._w(f"\n**Validation round {self._validation_round}**" + (" (after a repair)" if gen else ""))
            return
        if tool == "summarizer":
            return
        again = f" This is a **re-run (generation {gen})**: it now receives the validator's exact findings as feedback." if gen else ""
        if tool in MODEL_AGENTS:
            self._say(e, f"**AGENT (model) `{tool}`** starts task `{task}`. {ROLE_LINE.get(tool, self._desc(tool))}{again}")
        else:
            self._say(e, f"Step `{tool}` (plain Python, no model) starts task `{task}`. {self._desc(tool)}")

    def _on_artifact_written(self, e):
        self._written.setdefault(str(e.get("task")), []).append(str(e.get("path")))

    def _on_llm_call(self, e):
        self._say(e, f"`{e.get('task')}` called the model (`{e.get('provider')}`, {e.get('seconds')}s).")

    def _on_task_retry(self, e):
        self._say(e, f"`{e.get('task')}` hit a temporary problem (attempt {e.get('attempt')}): {_first_line(e.get('error'), 120)}. Retrying after {e.get('backoff_s')}s.")

    def _on_task_self_correct(self, e):
        self._say(e, f"`{e.get('task')}` produced output that failed its own pre-check ({_first_line(e.get('first_problem') or e.get('error'), 140)}). "
                     "It tries again with that problem quoted back to it.")

    def _on_task_skipped(self, e):
        self._say(e, f"`{e.get('task')}` was SKIPPED because something it depends on failed: it never runs on bad inputs.")

    def _on_task_failed(self, e):
        self._say(e, f"`{e.get('task')}` FAILED: {_first_line(e.get('error'), 200)}")

    def _on_test_update_proposed(self, e):
        self._say(e, f"**`spec_tester` proposes amending an EXISTING test** `{e.get('path')}`. Reason it gave: {e.get('reason')}. "
                     "Allowed only in an enhancement whose contract changed; a person will see the diff at the approval gate.")

    def _on_task_succeeded(self, e):
        task = str(e.get("task"))
        files = self._written.pop(task, [])
        agent = self._agent_of(task)
        if agent == "code_author":
            self._code_author_done(e, task, files)
        elif agent == "spec_tester":
            self._spec_tester_done(e, task, files)
        elif agent in ("validator", "summarizer"):
            return
        elif files:
            self._say(e, f"`{task}` finished and wrote: {', '.join(f'`{f}`' for f in files[:6])}.")

    def _agent_of(self, task: str) -> Optional[str]:
        return self._agents.get(task)

    def _code_author_done(self, e, task: str, files: List[str]) -> None:
        stats = (self.bb.get("patch_stats") if self.bb else None) or {}
        design = (self.bb.get("change_design") if self.bb else None) or {}
        if stats:
            self._say(e, f"`code_author` finished: it changed {len(stats.get('files', []))} file(s) "
                         f"({', '.join('`'+f+'`' for f in stats.get('files', [])[:5])}), +{stats.get('added')} / -{stats.get('removed')} lines, "
                         f"{stats.get('edits')} anchored edit(s). Each edit's `find` text had to match the original exactly once.")
            if design.get("summary"):
                self._w(f"    - its own summary: {design['summary']}")
            self._quote_diff()
        else:
            self._say(e, f"`code_author` finished and wrote {len(files)} file(s).")

    def _quote_diff(self) -> None:
        try:
            diff = self.ws.read("changes.diff")
        except Exception:
            return
        lines = [ln for ln in diff.splitlines() if ln.startswith(("---", "+++", "@@", "+", "-"))][:40]
        if lines:
            self._w("\n    **The change the agent made** (from `changes.diff`, trimmed):\n\n```diff\n" + "\n".join(lines) + "\n```\n")

    def _spec_tester_done(self, e, task: str, files: List[str]) -> None:
        names, n = [], 0
        for f in files:
            if f.startswith("tests/test_") and self.ws:
                try:
                    found = re.findall(r"def\s+(test_\w+)\s*\(", self.ws.read(f))
                    n += len(found)
                    names += found
                except Exception:
                    pass
        self._say(e, f"`spec_tester` finished: wrote {n} test(s) in {len([f for f in files if f.startswith('tests/test_')])} file(s), "
                     "from the specification only (it never saw the code).")
        for nm in names[:5]:
            self._w(f"    - `{nm}`")

    def _on_validation_check(self, e):
        ok = e.get("status")
        mark = {"PASS": "PASS", "FAIL": "**FAIL**", "WARN": "warn", "SKIP": "skip"}.get(ok, ok)
        meaning = CHECK_MEANING.get(str(e.get("check")), "")
        if ok == "SKIP":
            self._say(e, f"skip `{e.get('check')}`: does not apply to this codebase. {_first_line(e.get('detail'), 150)}")
            return
        self._say(e, f"{mark} `{e.get('check')}`" + (f": {meaning}." if meaning else ".") + (f" Evidence: {_first_line(e.get('detail'), 150)}" if ok != "PASS" or not meaning else ""))

    def _on_repair_loop(self, e):
        self._say(e, f"**Validation failed, so the referee sent it back** (repair round {e.get('round')}). Blamed role(s) re-run: `{e.get('targets')}` "
                     f"with {e.get('findings')} finding(s) as feedback. This is the recovery loop: the failure is routed to whoever is responsible, "
                     "not just retried blindly.")
        self._stage_no = 3  # the next task starts are 'work' again

    def _on_released(self, e):
        self._say(e, f"Released {e.get('files')} files to `{e.get('path')}`. Files leave the sandbox only after the human release gate.")

    def _on_service_demo(self, e):
        if e.get("first_long") and e.get("first_short"):
            self._say(e, f"**Live demo** (the released service, really used): `{e.get('first_long')}` became `{e.get('first_short')}`; following that short URL returned "
                         f"HTTP {e.get('redirect')} and {'**landed on the long URL**' if e.get('redirect_ok') else 'did NOT land on the long URL'}. "
                         f"After 3 clicks from one visitor the analytics say total_clicks={e.get('total_clicks')}, unique_visitors={e.get('unique_visitors')}."
                         + (f" On the ORIGINAL buggy code the same steps gave unique_visitors={e.get('before_unique')}: that difference is the bug fix, seen working." if e.get("before_unique") is not None else "")
                         + " Full detail in `DEMO_RESULT.md`.")
        else:
            self._say(e, f"The live demo could not complete: {_first_line(e.get('error'), 200)}. See `DEMO_RESULT.md`.")

    def _on_release_blocked(self, e):
        self._say(e, f"Release blocked: `{e.get('failed')}`. Nothing was released.")

    def _on_run_failed(self, e):
        self._say(e, f"The run failed: {_first_line(e.get('reason'), 220)}")

    def _on_run_paused(self, e):
        self._say(e, "The run PAUSED for human answers (see `clarifications.json`). Nothing was generated.")

    def _on_run_finished(self, e):
        status = str(e.get("status"))
        self._w(f"\n## Result\n\n**{status}**: {STATUS_MEANING.get(status, '')}  (took {e.get('seconds')}s)\n")
        if self.bb:
            old = self.bb.get("old_code_run")
            if old:
                self._w(f"- **Bug-fix proof:** {old.get('failing_new_tests')} of the new tests FAILED on the original buggy code and pass on the fix.")
            test_updates = self.bb.get("test_updates") or []
            if test_updates:
                self._w(f"- **Existing tests amended (approved by a person):** {', '.join('`'+u['path']+'`' for u in test_updates)}")
            tr = self.bb.get("test_run")
            if tr:
                base = (self.bb.get("baseline_tests") or {}).get("ran")
                self._w(f"- **Tests:** {tr.get('ran')} ran, ok={tr.get('ok')}" + (f" (the existing suite had {base})" if base else "") + ".")
        self._w("- **Open next:** `DEMO_RESULT.md` (the service really used, if the run reached release), `release/` (the product), `release/changes.diff` "
                "(the exact change, for a change), `ENGINEERING_SUMMARY.md` (the formal report), `events.jsonl` (every decision).")
