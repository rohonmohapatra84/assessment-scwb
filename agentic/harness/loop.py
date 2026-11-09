"""The harness loop: a lead agent decides, the harness checks and executes.

    while work remains:
        observe   which tasks are ready, running, failed
        decide    the LEAD (a model, see lead.py) chooses which ready tasks to start
        check     the harness accepts only tasks that are ready and fit the free worker slots; otherwise it
                  asks again, and after two bad answers falls back to starting every ready task (logged)
        execute   run them in parallel with retry, repair, degrade and checkpointing, then observe again

The lead chooses WHAT RUNS NEXT. It cannot skip a dependency, widen a write scope, or release anything: those rules
live in the harness (the plan check in lead.py, `Workspace` scopes, the human gates). With no lead given, `GreedyLead`
starts everything that is ready, which is the baseline policy and what the unit tests use.

Behaviours of the execution machinery
  * Dependency-aware scheduling - a task is offered only when all hard deps succeeded;
    independent tasks run concurrently (ThreadPoolExecutor).
  * Blackboard coordination - agents publish results/contracts; downstream
    agents consume them (contract-first, cross-step coordination).
  * Retry with exponential backoff for TransientError.
  * Repair loop - a validator that fails with findings makes the loop
    invalidate the implicated producer tasks (and their dependents), inject the
    findings as feedback, and re-run them (bounded by max_repairs).
  * Graceful degradation - optional tasks may fail without blocking; tasks
    whose hard deps failed are SKIPPED, never run on bad inputs.
  * Checkpointing - state is persisted after every transition; resume re-runs
    only what did not succeed.
"""
from __future__ import annotations

import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from fnmatch import fnmatch
from typing import Any, Callable, Dict, List, Optional, Set

from .core import (Agent, Blackboard, Chaos, Context, EventLog, FatalError, GuardrailViolation, Task, TaskStatus,
                   TransientError, ValidationFailure)
from .guardrails import Workspace
from .tools import roles_of


class PlanError(Exception):
    pass


class LeadError(Exception):
    """The lead could not give a usable answer (provider down, or no reply). The loop falls back to GreedyLead."""


class GreedyLead:
    """Baseline policy: start every ready task. Used by unit tests and as the fallback when the lead is unusable."""
    name = "greedy"

    def decide(self, observation: Dict[str, Any], feedback: Optional[str] = None) -> Dict[str, Any]:
        return {"action": "start", "tasks": list(observation["ready"]), "why": "start everything that is ready"}


def _patterns_overlap(a: str, b: str) -> bool:
    return a == b or fnmatch(a, b) or fnmatch(b, a)


def validate_dag(tasks: List[Task]) -> List[List[str]]:
    """Static plan validation. Returns execution 'waves' (topological levels).

    Rejects: duplicate ids, unknown deps, cycles, unknown repair targets, and
    *write conflicts* between tasks that could run concurrently.
    """
    by_id = {t.id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise PlanError("duplicate task ids")
    for t in tasks:
        for d in t.deps:
            if d not in by_id:
                raise PlanError(f"task '{t.id}' depends on unknown task '{d}'")
        for r in t.repair_targets:
            if r not in by_id:
                raise PlanError(f"task '{t.id}' repair target '{r}' unknown")
            if r not in _ancestors(t.id, by_id):
                raise PlanError(f"task '{t.id}' can only repair upstream tasks; '{r}' is not an ancestor")
    # Kahn's algorithm -> waves (also detects cycles)
    indeg = {t.id: len(set(t.deps)) for t in tasks}
    waves: List[List[str]] = []
    ready = sorted(i for i, n in indeg.items() if n == 0)
    seen = 0
    while ready:
        waves.append(ready)
        seen += len(ready)
        nxt: List[str] = []
        for tid in ready:
            for t in tasks:
                if tid in t.deps:
                    indeg[t.id] -= 1
                    if indeg[t.id] == 0:
                        nxt.append(t.id)
        ready = sorted(nxt)
    if seen != len(tasks):
        raise PlanError("dependency cycle detected")
    # write-conflict check for tasks with no ordering between them
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a.id in _ancestors(b.id, by_id) or b.id in _ancestors(a.id, by_id):
                continue
            for pa in a.writes:
                for pb in b.writes:
                    if _patterns_overlap(pa, pb):
                        raise PlanError(f"write conflict: concurrent tasks '{a.id}' and '{b.id}' both write '{pa}' / '{pb}'")
    return waves


def _ancestors(task_id: str, by_id: Dict[str, Task]) -> Set[str]:
    out: Set[str] = set()
    stack = list(by_id[task_id].deps)
    while stack:
        cur = stack.pop()
        if cur not in out:
            out.add(cur)
            stack.extend(by_id[cur].deps)
    return out


def _dependents(task_ids: Set[str], tasks: List[Task]) -> Set[str]:
    out: Set[str] = set()
    frontier = set(task_ids)
    while frontier:
        nxt = {t.id for t in tasks if set(t.deps) & frontier and t.id not in out}
        out |= nxt
        frontier = nxt
    return out


class Loop:
    def __init__(self, agents: Dict[str, Agent], bb: Blackboard, ws: Workspace, events: EventLog,
                 chaos: Chaos, llm: Any, *, lead: Any = None, max_workers: int = 4, max_repairs: int = 2,
                 backoff_base: float = 0.05, on_change: Optional[Callable[[List[Task]], None]] = None):
        self.agents = agents
        self.bb, self.ws, self.events, self.chaos, self.llm = bb, ws, events, chaos, llm
        self.lead = lead or GreedyLead()
        self.max_workers = max_workers
        self.max_repairs = max_repairs
        self.backoff_base = backoff_base
        self.on_change = on_change or (lambda tasks: None)
        self.repairs_used: Dict[str, int] = {}
        self.max_concurrency_seen = 0
        self.turns = 0  # decisions actually asked of the lead
        self.lead_fallbacks = 0
        self.aborted: Optional[str] = None  # set when the lead aborts the run

    # ------------------------------------------------------------------ run
    def run(self, tasks: List[Task], resume: bool = False) -> Dict[str, Any]:
        waves = validate_dag(tasks)
        self.events.emit("dag_validated", tasks=len(tasks), waves=len(waves),
                         shape=" | ".join(",".join(w) for w in waves))
        by_id = {t.id: t for t in tasks}
        if resume:
            for t in tasks:
                if t.status != TaskStatus.SUCCEEDED:
                    t.status, t.error = TaskStatus.PENDING, None
        running: Dict[Future, Task] = {}
        max_turns = 6 * len(tasks) + 10  # hard bound on lead consultations: the loop always terminates
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="agent") as pool:
            while True:
                self._skip_blocked(tasks, by_id)
                busy = {t.id for t in running.values()}
                ready = [t for t in tasks if t.status == TaskStatus.PENDING and t.id not in busy
                         and self._deps_satisfied(t, by_id)]
                if ready and len(running) < self.max_workers and not self.aborted:
                    for t in self._choose(ready, tasks, running, max_turns):
                        t.status, t.started_at = TaskStatus.RUNNING, time.time()
                        self.events.emit("task_started", task=t.id, agent=t.agent, generation=t.generation,
                                         concurrent=len(running) + 1)
                        running[pool.submit(self._execute, t)] = t
                        self.max_concurrency_seen = max(self.max_concurrency_seen, len(running))
                        self.on_change(tasks)
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    task = running.pop(fut)
                    self._settle(task, fut, tasks)
                    self.on_change(tasks)
        return self.report(tasks)

    # ------------------------------------------------------------------ lead
    def _choose(self, ready: List[Task], tasks: List[Task], running: Dict[Future, Task], max_turns: int) -> List[Task]:
        """Which ready tasks start now. The lead proposes; the harness disposes."""
        slots = self.max_workers - len(running)
        if not running and len(ready) == 1:
            return ready  # no real choice: do not spend a model call on it
        if isinstance(self.lead, GreedyLead):
            return ready[:slots]
        if self.turns >= max_turns:
            return self._fallback(ready, slots, "turn budget spent")
        observation = self._observation(ready, tasks, running, slots)
        by_id = {t.id: t for t in ready}
        feedback: Optional[str] = None
        for _ in range(2):
            self.turns += 1
            try:
                decision = self.lead.decide(observation, feedback)
            except LeadError as exc:
                return self._fallback(ready, slots, f"lead unavailable: {exc}")
            action = decision.get("action") if isinstance(decision, dict) else None
            why = str(decision.get("why", ""))[:300] if isinstance(decision, dict) else ""
            if action == "abort":
                self.aborted = why or "the lead aborted the run"
                self.events.emit("lead_aborted", turn=self.turns, why=self.aborted)
                return []
            if action == "wait" and running:
                self.events.emit("lead_decision", turn=self.turns, action="wait", why=why)
                return []
            ids = decision.get("tasks") if action == "start" else None
            if (isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids)
                    and all(i in by_id for i in ids) and len(ids) <= slots):
                self.events.emit("lead_decision", turn=self.turns, action="start", tasks=",".join(ids), why=why)
                return [by_id[i] for i in ids]
            feedback = (f"Invalid decision {str(decision)[:160]!r}. Reply with {{\"action\":\"start\",\"tasks\":[...]}} using only ids "
                        f"from `ready` ({sorted(by_id)}), at most {slots} of them, or {{\"action\":\"wait\"}} while tasks run.")
        return self._fallback(ready, slots, "two invalid decisions")

    def _fallback(self, ready: List[Task], slots: int, reason: str) -> List[Task]:
        self.lead_fallbacks += 1
        self.events.emit("lead_fallback", reason=reason, started=",".join(t.id for t in ready[:slots]))
        return ready[:slots]

    def _observation(self, ready: List[Task], tasks: List[Task], running: Dict[Future, Task], slots: int) -> Dict[str, Any]:
        return {"turn": self.turns + 1, "free_slots": slots, "ready": [t.id for t in ready],
                "running": [t.id for t in running.values()],
                "tasks": [{"id": t.id, "tool": t.agent, "status": t.status.value, "deps": t.deps} for t in tasks],
                "failed": [{"id": t.id, "error": (t.error or "")[:160]} for t in tasks if t.status == TaskStatus.FAILED],
                "repairs": dict(self.repairs_used)}

    # ------------------------------------------------------------ scheduling
    @staticmethod
    def _deps_satisfied(t: Task, by_id: Dict[str, Task]) -> bool:
        for d in t.deps:
            dep = by_id[d]
            if dep.status == TaskStatus.SUCCEEDED:
                continue
            if dep.optional and dep.status in (TaskStatus.FAILED, TaskStatus.SKIPPED):
                continue  # degraded dependency: proceed without it
            return False
        return True

    def _skip_blocked(self, tasks: List[Task], by_id: Dict[str, Task]) -> None:
        changed = True
        while changed:
            changed = False
            for t in tasks:
                if t.status != TaskStatus.PENDING:
                    continue
                for d in t.deps:
                    dep = by_id[d]
                    if dep.status in (TaskStatus.FAILED, TaskStatus.SKIPPED) and not dep.optional:
                        t.status, t.error = TaskStatus.SKIPPED, f"blocked by failed dependency '{d}'"
                        self.events.emit("task_skipped", task=t.id, reason=t.error)
                        changed = True
                        break

    # ------------------------------------------------------------- execution
    def _execute(self, task: Task) -> Dict[str, Any]:
        """Runs in a worker thread. Handles retry/backoff for transient errors."""
        agent = self.agents[task.agent]
        feedback = self.bb.get(f"feedback:{task.id}", [])
        last_exc: Optional[BaseException] = None
        for attempt in range(1, task.max_attempts + 1):
            task.attempts += 1
            ctx = Context(task, self.bb, self.ws, self.events, self.chaos, self.llm, attempt=attempt, feedback=feedback)
            try:
                self.chaos.before_attempt(task, attempt)
                result = agent.run(ctx)
                return result or {}
            except TransientError as exc:
                last_exc = exc
                delay = self.backoff_base * (2 ** (attempt - 1))
                self.events.emit("task_retry", task=task.id, attempt=attempt, error=str(exc), backoff_s=round(delay, 3))
                if attempt < task.max_attempts:
                    time.sleep(delay)
                    continue
                raise
            except ValidationFailure as exc:
                # Validators with repair targets bubble up to the repair loop;
                # producers self-correct using their own findings as feedback.
                if task.repair_targets:
                    raise
                last_exc = exc
                feedback = exc.findings
                first = (exc.findings[0].get("message", "") if exc.findings else "")[:220]
                self.events.emit("task_self_correct", task=task.id, attempt=attempt, error=str(exc), first_problem=first)
                if attempt >= task.max_attempts:
                    raise
        raise last_exc or RuntimeError("unreachable")

    def _settle(self, task: Task, fut: Future, tasks: List[Task]) -> None:
        task.ended_at = time.time()
        try:
            result = fut.result()
        except ValidationFailure as exc:
            self._handle_validation_failure(task, exc, tasks)
            return
        except (GuardrailViolation, FatalError, TransientError) as exc:
            task.status, task.error = TaskStatus.FAILED, f"{type(exc).__name__}: {exc}"
            self.events.emit("task_failed", task=task.id, error=task.error, optional=task.optional)
            return
        except Exception as exc:  # agent bug: record, never crash the loop
            task.status, task.error = TaskStatus.FAILED, f"{type(exc).__name__}: {exc}"
            self.events.emit("task_failed", task=task.id, error=task.error, optional=task.optional,
                             trace=traceback.format_exc(limit=3))
            return
        task.status, task.error = TaskStatus.SUCCEEDED, None
        self.bb.set(f"result:{task.id}", result)
        self.events.emit("task_succeeded", task=task.id, attempts=task.attempts,
                         seconds=round(task.ended_at - (task.started_at or task.ended_at), 3))

    def _handle_validation_failure(self, task: Task, exc: ValidationFailure, tasks: List[Task]) -> None:
        used = self.repairs_used.get(task.id, 0)
        by_id = {t.id: t for t in tasks}
        requirement = self.bb.get("requirement") or {}
        plan_type, kind = requirement.get("plan_type", "*"), requirement.get("change_kind")

        def blamed(finding: Dict[str, Any], tid: str) -> bool:
            """A finding names a task id, or a ROLE ('code', 'tests', 'contract') that the plan's tasks provide.
            Roles matter because with a model-written plan the harness does not know the task ids in advance."""
            target = finding.get("target")
            return target == tid or (target is not None and target in roles_of(by_id[tid].agent, plan_type, kind))

        targets: Set[str] = {tid for tid in task.repair_targets if any(blamed(f, tid) for f in exc.findings)}
        if not targets or used >= self.max_repairs:
            task.status, task.error = TaskStatus.FAILED, f"ValidationFailure: {exc} (repairs used={used})"
            self.events.emit("task_failed", task=task.id, error=task.error, optional=task.optional)
            return
        self.repairs_used[task.id] = used + 1
        errors = [f for f in exc.findings if f.get("severity") == "error"]
        self.events.emit("repair_loop", validator=task.id, round=used + 1, targets=",".join(sorted(targets)),
                         findings=len(errors))
        by_id = {t.id: t for t in tasks}
        task.status = TaskStatus.PENDING  # the validator itself re-runs after the repaired producers
        task.generation += 1
        reset = set(targets) | _dependents(set(targets), tasks)
        for tid in reset:
            t = by_id[tid]
            if t.status == TaskStatus.RUNNING:
                continue
            t.status, t.error, t.attempts = TaskStatus.PENDING, None, 0
            if tid in targets:
                t.generation += 1
                self.bb.set(f"feedback:{tid}", [f for f in errors if blamed(f, tid)])
            self.bb.pop(f"result:{tid}")

    # ---------------------------------------------------------------- report
    def report(self, tasks: List[Task]) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for t in tasks:
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        hard_failed = [t.id for t in tasks if t.status != TaskStatus.SUCCEEDED and not t.optional]
        degraded = [t.id for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.SKIPPED) and t.optional]
        return {"ok": not hard_failed and not self.aborted, "counts": counts, "hard_failed": hard_failed, "degraded": degraded,
                "repairs": dict(self.repairs_used), "max_concurrency": self.max_concurrency_seen,
                "retries": len(self.events.of_kind("task_retry")), "lead_turns": self.turns,
                "lead_fallbacks": self.lead_fallbacks, "aborted": self.aborted}
