"""The harness: the runtime that runs the agents and steps safely, end to end.

  discover -> LEAD PLANS -> check plan -> [PLAN GATE] -> LOOP (lead directs, harness executes) -> [RELEASE GATE] -> release

  * The project_lead agent (a model) proposes the task plan and picks what runs next.
  * The harness owns everything with consequences: it validates the plan against the tool catalog, enforces write scopes
    in the sandbox, retries and repairs, runs the validator, and stops at two human gates.
  * Agents are autonomous *between* gates; humans own the two irreversible decisions.

A model is required: the plan is the lead's work, and there is no hidden offline plan.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..providers import NullProvider
from ..authoring_agents import CodeAuthor, SpecTester
from ..steps.analysis import CodebaseAnalyst, RequirementAnalyst
from ..steps.build import CodeGenerator, DocWriter, PatchGenerator, TestGenerator
from ..steps.design import (Architect, ChangeDesigner, ContractDelta, ContractDesigner, DataModeler,
                            MigrationDesigner, OptionsDesigner, RiskAssessor, ValidationStrategist)
from ..steps.validate import Summarizer, Validator
from .approvals import ApprovalPolicy
from .core import (Blackboard, Chaos, Checkpoint, ClarificationNeeded, Context, EventLog, GuardrailViolation, Task,
                   TaskStatus)
from .guardrails import ManifestEntry, Workspace, robust_rmtree, run_unittests
from .lead import ModelLead, check_plan, facts_for
from .loop import LeadError, Loop, PlanError
from .demo import write_demo_result
from .narrator import Narrator
from .report import render_engineering_summary

MAX_PLAN_ATTEMPTS = 3


def default_registry() -> Dict[str, Any]:
    """Everything a task can name in `Task.agent`, by name.

    Model-backed agents (defined in agentic/agents/*.md): project_lead (runs the loop, not a task), requirement_analyst,
    code_author, spec_tester. Everything else is a deterministic step (agentic/steps/*.py): plain Python, no model calls.
    """
    runnable = [RequirementAnalyst(), CodeAuthor(), SpecTester(),
                CodebaseAnalyst(), Architect(), ContractDesigner(), DataModeler(), RiskAssessor(), ValidationStrategist(),
                CodeGenerator(), TestGenerator(), DocWriter(), Validator(), Summarizer(),
                ChangeDesigner(), ContractDelta(), MigrationDesigner(), PatchGenerator(), OptionsDesigner()]
    return {a.name: a for a in runnable}


def agent_registry() -> Dict[str, Any]:
    """default_registry() plus the project_lead agent, which runs the loop instead of being a task. Used to check specs."""
    return {**default_registry(), ModelLead.name: ModelLead(None)}


@dataclass
class RunResult:
    status: str  # completed | completed_degraded | needs_clarification | rejected_at_plan | rejected_at_release | failed
    out_dir: Path
    report: Dict[str, Any]
    summary_path: Optional[Path] = None
    questions: Optional[List[Dict[str, Any]]] = None

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "completed_degraded")


class Harness:
    def __init__(self, out_dir: Path, *, approval: str = "auto", inject: Optional[List[str]] = None,
                 resume: bool = False, echo: Optional[Callable[[str], None]] = print, llm: Any = None,
                 max_workers: int = 4, backoff_base: float = 0.05, input_fn=input, generator: str = "template",
                 lead: Any = None, story_preface: str = "", demo: bool = False):
        self.out_dir = Path(out_dir).resolve()
        self.approval_mode = approval  # a mode string, or an ApprovalPolicy instance (e.g. the API's external gate)
        self.generator = generator
        self.chaos = Chaos(inject)
        self.resume = resume
        self.echo = echo
        self.llm = llm or NullProvider()
        self.max_workers = max_workers
        self.backoff_base = backoff_base
        self.input_fn = input_fn
        self._lead = lead  # tests may inject a lead; normally the model-backed project_lead is built per run
        self.story_preface = story_preface  # scenario setup shown at the top of RUN_STORY.md (e.g. the bug that was planted)
        self.demo = demo  # after a release, really use the released service and write DEMO_RESULT.md (run.py turns this on)

    # ------------------------------------------------------------------ run
    def run(self, text: str, *, target_repo: Optional[str] = None, answers: Optional[Dict[str, str]] = None,
            assume_defaults: bool = False) -> RunResult:
        started = time.time()
        ckpt = Checkpoint(self.out_dir / "state.json")
        saved = ckpt.load() if self.resume else None
        if not saved and self.out_dir.exists():
            for child in ("workspace", "release", "baseline"):
                robust_rmtree(self.out_dir / child)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if not saved:
            (self.out_dir / "clarifications.json").unlink(missing_ok=True)
        events = EventLog(self.out_dir / "events.jsonl", echo=self._echo_filter if self.echo else None,
                          append=bool(saved))
        bb =Blackboard(saved["blackboard"] if saved else None)
        ws = Workspace(self.out_dir / "workspace", baseline_root=self.out_dir / "baseline")
        story = Narrator(self.out_dir / "RUN_STORY.md", bb=bb, ws=ws, preface=self.story_preface, resume=bool(saved))
        events.add_listener(story.on_event)
        if saved:
            for rel, e in saved["manifest"].items():
                ws.manifest[rel] = ManifestEntry(**e)
        if not isinstance(self.approval_mode, str):
            gates = self.approval_mode
            gates.events = events
        else:
            gates = ApprovalPolicy(self.approval_mode, events, self.input_fn)
        agents = default_registry()

        events.emit("run_started", requirement=text[:90], target_repo=target_repo or "-", resume=bool(saved),
                    approval=getattr(gates, "mode", "?"), llm=self.llm.name, generator=self.generator)
        tasks: List[Task] = [Task.from_dict(t) for t in saved["tasks"]] if saved else []

        def save(tasks_now: List[Task]) -> None:
            ckpt.save({"blackboard": bb.snapshot(), "tasks": [t.to_dict() for t in tasks_now],
                       "manifest": {rel: e.__dict__ for rel, e in ws.manifest.items()}})

        # ---------------------------------------------------------- discover
        if not saved:
            bb.set("input", {"text": text, "target_repo": target_repo, "answers": answers or {},
                             "assume_defaults": assume_defaults, "generator": self.generator})
            try:
                self._discover(bb, ws, events, agents, target_repo)
            except ClarificationNeeded as exc:
                (self.out_dir / "clarifications.json").write_text(json.dumps(
                    {"requirement": text, "questions": exc.questions,
                     "how_to_answer": "re-run with --answers answers.json  ({\"Q1\": \"...\"}) or --assume-defaults"}, indent=2),
                    encoding="utf-8")
                events.emit("run_paused", reason="needs_clarification", questions=len(exc.questions))
                return RunResult("needs_clarification", self.out_dir, {"ok": False}, questions=exc.questions)
            except GuardrailViolation as exc:
                events.emit("run_failed", reason=str(exc))
                return RunResult("failed", self.out_dir, {"ok": False, "error": str(exc)})
            req = bb.require("requirement")
            lead = self._lead or ModelLead(self.llm, events)
            if self._lead is None and not getattr(self.llm, "available", False):
                reason = ("no model configured: the harness needs one for the project_lead agent. "
                          + (getattr(self.llm, "reason", "") or "Set AGENTIC_PROVIDER and its settings (see .env.example)."))
                events.emit("run_failed", reason=reason)
                return RunResult("failed", self.out_dir, {"ok": False, "error": reason})
            # ------------------------------------------------- the lead plans, the harness checks
            try:
                tasks, waves, rationale = self._plan(lead, req, bool(target_repo), events)
            except (PlanError, LeadError) as exc:
                status = "plan_rejected" if isinstance(exc, PlanError) else "failed"
                events.emit("run_failed", reason=f"{status}: {exc}")
                return RunResult(status, self.out_dir, {"ok": False, "error": str(exc)})
            bb.set("lead_plan", {"rationale": rationale})
            (self.out_dir / "plan.json").write_text(json.dumps(
                {"plan_type": req["plan_type"], "rationale": rationale, "waves": waves,
                 "tasks": [t.to_dict() for t in tasks]}, indent=2), encoding="utf-8")
            save(tasks)
            # ------------------------------------------------- plan gate
            d = gates.request("plan_approval", self._plan_text(req, tasks, waves, rationale),
                              {"tasks": len(tasks), "waves": len(waves)})
            if not d.approved:
                self._finish(bb, events, gates, tasks, ws, {"ok": False}, "rejected_at_plan", started)
                return RunResult("rejected_at_plan", self.out_dir, {"ok": False},
                                 self.out_dir / "ENGINEERING_SUMMARY.md")

        # ---------------------------------------------------------- execute
        lead = self._lead or ModelLead(self.llm, events)
        # A model writes the code and the tests separately, so they need a round more to converge than deterministic steps do.
        loop = Loop(agents, bb, ws, events, self.chaos, self.llm, lead=lead, max_workers=self.max_workers,
                    max_repairs=3 if self.generator == "llm" else 2, backoff_base=self.backoff_base, on_change=save)
        report = loop.run(tasks, resume=bool(saved))
        save(tasks)

        # ------------------------------------------------- release gate
        vr = bb.get("validation_report") or {}
        blocking = len(report["hard_failed"])
        if not report["ok"]:
            events.emit("release_blocked", failed=",".join(report["hard_failed"]), aborted=report.get("aborted") or "-")
            status = "failed"
            gates.request("release_approval", "Hard failures present; nothing to release.",
                          {"failed": report["hard_failed"], "aborted": report.get("aborted")}, blocking_issues=max(blocking, 1))
        else:
            summary = (f"{vr.get('passed', 0)} checks passed, {vr.get('failed', 0)} failed, {vr.get('warnings', 0)} warning(s); "
                       f"{len(ws.manifest)} artifacts; degraded tasks: {report['degraded'] or 'none'}")
            # An agent amended tests it did not write: a human reads every diff before anything can be released.
            updates = bb.get("test_updates") or []
            tu = gates.request("test_update_approval", self._test_update_text(updates), {"files": [u["path"] for u in updates]}) \
                if updates else None
            if tu is not None and not tu.approved:
                events.emit("release_blocked", failed="test_update_approval", aborted="-")
                d = tu
            else:
                d = gates.request("release_approval", summary, {"validation": vr.get("passed")}, blocking_issues=0)
            if d.approved:
                files = ws.promote(self.out_dir / "release")
                events.emit("released", files=files, path=str(self.out_dir / "release"))
                if self.demo:
                    self._service_demo(events, bb)
                status = "completed_degraded" if report["degraded"] else "completed"
            else:
                status = "rejected_at_release"
        self._finish(bb, events, gates, tasks, ws, report, status, started)
        return RunResult(status, self.out_dir, report, self.out_dir / "ENGINEERING_SUMMARY.md")

    # ---------------------------------------------------------------- phases
    def _discover(self, bb, ws, events, agents, target_repo) -> None:
        def ctx_for(tid: str, agent: str) -> Context:
            return Context(Task(tid, tid, agent), bb, ws, events, self.chaos, self.llm)
        events.emit("phase", name="discover")
        agents["requirement_analyst"].run(ctx_for("discover.requirements", "requirement_analyst"))
        req = bb.require("requirement")
        if req["target_repo"]:
            repo = Path(req["target_repo"])
            if not repo.exists():
                raise GuardrailViolation(f"target repository not found: {repo}")
            n = ws.import_tree(repo)
            events.emit("repo_imported", files=n, note="original is never modified; edits happen on a workspace copy")
            base = run_unittests(ws.root, timeout=180)
            failing = re.findall(r"^(?:FAIL|ERROR): (.+)$", base.output, re.M)[:10]  # names only: shown in the run story
            bb.set("baseline_tests", {"ran": base.tests_run, "ok": base.ok, "seconds": round(base.seconds, 2), "failing": failing})
            events.emit("baseline_tests", ran=base.tests_run, ok=base.ok, failing=len(failing))
            agents["codebase_analyst"].run(ctx_for("discover.codebase", "codebase_analyst"))

    def _plan(self, lead, req, has_repo: bool, events):
        """The lead proposes, the harness checks. A rejected plan goes back with the exact reason (bounded attempts)."""
        facts, feedback, last = facts_for(has_repo), None, "no plan"
        for attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
            raw = lead.plan(req, self.generator, facts, feedback)
            try:
                tasks, waves = check_plan(raw, req["plan_type"], self.generator, facts, req.get("change_kind"))
            except PlanError as exc:
                last, feedback = str(exc), f"{exc}\n(attempt {attempt} of {MAX_PLAN_ATTEMPTS})"
                events.emit("plan_rejected", attempt=attempt, reason=last[:300])
                continue
            rationale = str(raw.get("rationale", ""))[:600]
            events.emit("plan_proposed", attempt=attempt, tasks=len(tasks), waves=len(waves),
                        shape=" | ".join(",".join(w) for w in waves), rationale=rationale[:200])
            return tasks, waves, rationale
        raise PlanError(f"the lead could not produce a valid plan in {MAX_PLAN_ATTEMPTS} attempts; last problem: {last}")

    @staticmethod
    def _plan_text(req, tasks, waves, rationale="") -> str:
        by_id = {t.id: t for t in tasks}
        lines = [f"Plan type: {req['plan_type']}   ({len(tasks)} tasks in {len(waves)} waves)"]
        if rationale:
            lines.append(f"Lead's rationale: {rationale}")
        for i, w in enumerate(waves, 1):
            lines.append(f"  wave {i}: {', '.join(f'{tid} [{by_id[tid].agent}]' for tid in w)}")
        return "\n".join(lines)

    def _service_demo(self, events, bb) -> None:
        """After a release: use the released service for real and write DEMO_RESULT.md. Reports its own failure; never fails the run."""
        req = bb.get("requirement") or {}
        if not (self.out_dir / "release" / "shortener" / "server.py").exists():
            return  # nothing runnable to demonstrate (for example an analysis-only run)
        try:
            summary = write_demo_result(self.out_dir, (bb.get("input") or {}).get("text", ""), req.get("change_kind"))
        except Exception as exc:  # defensive: write_demo_result already contains its own failures
            summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        events.emit("service_demo", **{k: v for k, v in summary.items() if v is not None})

    @staticmethod
    def _test_update_text(updates) -> str:
        lines = [f"{len(updates)} EXISTING test file(s) were amended because the contract change supersedes them. "
                 "Approve only if each amendment keeps the test's intent and does not weaken it."]
        for u in updates:
            lines += ["", f"--- {u['path']}  (reason: {u['reason']})", u["diff"] or "(no textual change)"]
        return "\n".join(lines)

    def _finish(self, bb, events, gates, tasks, ws, report, status, started) -> None:
        if getattr(self.llm, "available", False):
            events.emit("llm_usage", provider=self.llm.name, model=getattr(self.llm, "model", getattr(self.llm, "deployment", "-")),
                        **(getattr(self.llm, "usage", {}) or {}))
        events.emit("run_finished", status=status, seconds=round(time.time() - started, 2))
        md = render_engineering_summary(status=status, bb=bb, tasks=tasks, events=events, gates=gates, report=report,
                                        ws=ws, seconds=time.time() - started, out_dir=self.out_dir)
        (self.out_dir / "ENGINEERING_SUMMARY.md").write_text(md, encoding="utf-8")
        (self.out_dir / "summary.json").write_text(json.dumps({
            "status": status, "report": report, "tasks": [t.to_dict() for t in tasks],
            "gates": [g.__dict__ for g in gates.history]}, indent=2, default=str), encoding="utf-8")

    _QUIET = {"artifact_written", "dag_validated_detail"}

    def _echo_filter(self, line: str) -> None:
        if " artifact_written " in line:
            return
        self.echo(line)
