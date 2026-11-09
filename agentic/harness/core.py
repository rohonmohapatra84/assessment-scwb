"""Core vocabulary of the engine, in one place.

  1. Task model and the error taxonomy that tells the loop how to recover
  2. EventLog (audit trail), Blackboard (shared memory between steps), Checkpoint (resume)
  3. Context (what every step receives) and Chaos (fault injection used to prove recovery paths)

Everything is plain dataclasses / enums / JSON-serialisable values so the full run state can be checkpointed,
audited and summarised.
"""
from __future__ import annotations

import enum
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol

if TYPE_CHECKING:  # guardrails imports this module, so only import it for type checking
    from .guardrails import Workspace


# =========================================================== 1. tasks and errors
class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # a hard dependency failed, so the task never ran


class Scenario(str, enum.Enum):
    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"
    AMBIGUOUS = "ambiguous"  # needs clarification before it can be planned


# ----------------------------------------------------------------- errors ---
class AgentError(Exception):
    """Base class. Subclasses tell the loop HOW to recover."""


class TransientError(AgentError):
    """Environmental / flaky failure. Orchestrator retries with backoff."""


class ValidationFailure(AgentError):
    """Output failed a quality check. Carries findings used as repair feedback."""

    def __init__(self, message: str, findings: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.findings = findings or []


class GuardrailViolation(AgentError):
    """A safety rule was hit. Never retried: fails the task immediately."""


class FatalError(AgentError):
    """Unrecoverable failure (e.g. patch anchor drift). No retry, no repair."""


class ClarificationNeeded(AgentError):
    """Requirement is too ambiguous to plan; pipeline pauses for a human."""

    def __init__(self, questions: List[Dict[str, Any]]):
        super().__init__(f"{len(questions)} clarification question(s) outstanding")
        self.questions = questions


# ------------------------------------------------------------------ tasks ---
@dataclass
class Task:
    id: str
    title: str
    agent: str
    deps: List[str] = field(default_factory=list)
    description: str = ""
    writes: List[str] = field(default_factory=list)  # glob patterns the task may write
    optional: bool = False  # failure degrades the run but does not block dependents
    repair_targets: List[str] = field(default_factory=list)  # tasks to re-run when this one fails validation
    max_attempts: int = 3
    # runtime state
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    generation: int = 0  # incremented every time a repair loop re-runs the task

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Task":
        d = dict(d)
        d["status"] = TaskStatus(d["status"])
        return Task(**d)


@dataclass
class Finding:
    check: str
    severity: str  # error | warning | info
    message: str
    target: Optional[str] = None  # task id responsible for fixing it

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =========================================================== 2. run state
class EventLog:
    """Append-only, thread-safe audit trail. Every decision, retry, approval and
    guardrail hit is recorded here and mirrored to events.jsonl."""

    def __init__(self, path: Optional[Path] = None, echo: Optional[Callable[[str], None]] = None,
                 append: bool = False):
        self._lock = threading.Lock()
        self.events: List[Dict[str, Any]] = []
        self._path = path
        self._echo = echo
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []  # e.g. the run-story narrator; never allowed to break a run
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            if append and path.exists():  # resume: keep the audit trail of the earlier session
                self.events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            else:
                path.write_text("", encoding="utf-8")

    def emit(self, kind: str, **fields: Any) -> None:
        evt = {"ts": round(time.time(), 3), "kind": kind, **fields}
        with self._lock:
            self.events.append(evt)
            if self._path:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(evt, default=str) + "\n")
        if self._echo:
            self._echo(self.format(evt))
        for listener in self._listeners:
            try:
                listener(evt)
            except Exception:  # a reporting helper must never be able to fail the run it describes
                pass

    def add_listener(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    @staticmethod
    def format(evt: Dict[str, Any]) -> str:
        rest = " ".join(f"{k}={v}" for k, v in evt.items() if k not in ("ts", "kind"))
        return f"[{time.strftime('%H:%M:%S', time.localtime(evt['ts']))}] {evt['kind']:<18} {rest}"

    def of_kind(self, *kinds: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e["kind"] in kinds]


class Blackboard:
    """Thread-safe key/value store through which agents hand work to each other.
    Values must be JSON-serialisable so the board can be checkpointed."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = data or {}

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def require(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                raise KeyError(f"blackboard key '{key}' missing - an upstream task did not publish it")
            return self._data[key]

    def append(self, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(key, []).append(value)

    def pop(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.pop(key, default)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data, default=str))


class Checkpoint:
    """Atomic JSON checkpoint written after every task state change."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def save(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            for attempt in range(40):  # Windows refuses to replace a file another thread is reading (e.g. the status API)
                try:
                    tmp.replace(self.path)  # atomic rename: never leaves a half-written checkpoint
                    return
                except PermissionError:
                    time.sleep(0.05)
            tmp.replace(self.path)  # still locked after ~2s: surface the error

    def load(self) -> Optional[Dict[str, Any]]:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))


# =========================================================== 3. execution context
class Chaos:
    """Deterministic fault injection.

    specs (repeatable CLI flag --inject):
      transient:<task>:<n>   first n attempts raise TransientError  -> exercises retry/backoff
      defect:<task>          first generation emits a *flawed* artifact -> exercises validate/repair loop
      fatal:<task>           task always raises FatalError -> exercises skip/degrade/resume
    """

    def __init__(self, specs: Optional[List[str]] = None):
        self.transient: Dict[str, int] = {}
        self.defects: set = set()
        self.fatals: set = set()
        for spec in specs or []:
            parts = spec.split(":")
            if parts[0] == "transient" and len(parts) == 3:
                self.transient[parts[1]] = int(parts[2])
            elif parts[0] == "defect" and len(parts) == 2:
                self.defects.add(parts[1])
            elif parts[0] == "fatal" and len(parts) == 2:
                self.fatals.add(parts[1])
            else:
                raise ValueError(f"bad --inject spec {spec!r}")

    # A spec names a task id OR a tool name ("gen_code" or "code_generator"): with a model-written plan the ids are
    # the lead's choice, but the tool names are fixed, so injection by tool name always works.
    def before_attempt(self, task: Task, attempt: int) -> None:
        if task.id in self.fatals or task.agent in self.fatals:
            raise FatalError(f"[chaos] injected fatal failure in {task.id}")
        if attempt <= self.transient.get(task.id, self.transient.get(task.agent, 0)):
            raise TransientError(f"[chaos] injected transient failure in {task.id} (attempt {attempt})")

    def should_emit_defect(self, task: Task) -> bool:
        return (task.id in self.defects or task.agent in self.defects) and task.generation == 0


@dataclass
class Context:
    task: Task
    bb: Blackboard
    ws: Workspace
    events: EventLog
    chaos: Chaos
    llm: Any
    attempt: int = 1
    feedback: List[Dict[str, Any]] = field(default_factory=list)  # repair findings from validators

    @property
    def requirement(self) -> Dict[str, Any]:
        return self.bb.require("requirement")

    def write(self, rel: str, content: str, kind: str) -> None:
        """Scoped, scanned write. kind in {code,test,doc,contract,schema,diff,report}."""
        self.ws.write(rel, content, kind=kind, producer=self.task.id, scopes=self.task.writes or None)
        self.events.emit("artifact_written", task=self.task.id, path=rel, artifact_kind=kind)

    def update_existing_test(self, rel: str, content: str, reason: str) -> None:
        """Amend a test that already exists in the repository. Allowed only where the tool catalog says so (`amends_tests`
        lists the change kinds), and recorded for the human `test_update_approval` gate and the validator."""
        from .tools import TOOLS  # local import: tools.py is data and must stay free of runtime imports
        spec = TOOLS.get(self.task.agent)
        kind = self.requirement.get("change_kind") or "enhancement"
        if not spec or kind not in spec.amends_tests:
            raise GuardrailViolation(f"task '{self.task.id}' may not amend existing tests in a '{kind}' change")
        old = self.ws.read_baseline(rel)
        self.ws.write_test_update(rel, content, producer=self.task.id)
        import difflib
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), f"a/{rel}", f"b/{rel}"))
        self.bb.append("test_updates", {"path": rel, "reason": reason[:400], "task": self.task.id, "diff": diff[:6000]})
        self.events.emit("test_update_proposed", task=self.task.id, path=rel, reason=reason[:120])

    def note(self, message: str, **fields: Any) -> None:
        self.events.emit("agent_note", task=self.task.id, message=message, **fields)


class Agent(Protocol):
    name: str

    def run(self, ctx: Context) -> Dict[str, Any]: ...
