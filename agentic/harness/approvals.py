"""Human-in-the-loop approval gates (controlled autonomy).

Agents run autonomously *between* gates; a human (or a scripted policy in
CI/demo) must approve the plan before execution and the validated result
before release. Every decision is written to the audit log.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .core import EventLog


@dataclass
class Decision:
    gate: str
    approved: bool
    decided_by: str
    note: str = ""


class ExternalApproval:
    """Gates decided by a remote human through the HTTP API (cloud deployment).

    request() BLOCKS the pipeline thread until someone calls decide() for that gate, or the timeout
    elapses (timeout = rejection: an unattended run never ships). Subclass-free duck type of ApprovalPolicy."""

    mode = "external"

    def __init__(self, timeout_s: float = 3600.0):
        self.events: Optional[EventLog] = None
        self.timeout_s = timeout_s
        self.history: List[Decision] = []
        self.pending: Optional[Dict[str, Any]] = None
        self._decision: Optional[Decision] = None
        self._evt = threading.Event()
        self._lock = threading.Lock()

    def request(self, gate: str, summary: str, evidence: Dict[str, Any], blocking_issues: int = 0) -> Decision:
        if self.events:
            self.events.emit("gate_requested", gate=gate, blocking_issues=blocking_issues)
        with self._lock:
            self._decision = None
            self._evt.clear()
            self.pending = {"gate": gate, "summary": summary, "evidence": evidence, "blocking_issues": blocking_issues,
                            "requested_at": time.time()}
        got = self._evt.wait(self.timeout_s)
        with self._lock:
            decision = self._decision if got and self._decision else Decision(gate, False, "timeout", "no human decision before timeout")
            if blocking_issues and decision.approved:
                decision = Decision(gate, False, "policy", f"{blocking_issues} blocking issue(s); approval refused")
            self.pending = None
        self.history.append(decision)
        if self.events:
            self.events.emit("gate_decided", gate=gate, approved=decision.approved, by=decision.decided_by, note=decision.note)
        return decision

    def decide(self, gate: str, approved: bool, by: str, note: str = "") -> bool:
        with self._lock:
            if not self.pending or self.pending["gate"] != gate:
                return False
            self._decision = Decision(gate, bool(approved), by, note)
            self._evt.set()
            return True


class ApprovalPolicy:
    """policy: 'interactive' -> prompt on stdin
               'auto'        -> approve when evidence has no blocking issues (demo/CI)
               'reject'      -> reject every gate (tests the human-veto path)
               'reject:<gate>' -> reject only one gate
    """

    def __init__(self, mode: str = "auto", events: Optional[EventLog] = None, input_fn=input):
        self.mode = mode
        self.events = events
        self._input = input_fn
        self.history: List[Decision] = []

    def request(self, gate: str, summary: str, evidence: Dict[str, Any], blocking_issues: int = 0) -> Decision:
        if self.events:
            self.events.emit("gate_requested", gate=gate, blocking_issues=blocking_issues)
        if self.mode == "interactive":
            print(f"\n=== APPROVAL GATE: {gate} ===\n{summary}\n")
            ans = self._input("Approve? [y/N] ").strip().lower()
            decision = Decision(gate, ans in ("y", "yes"), "human", "interactive")
        elif self.mode == "reject" or self.mode == f"reject:{gate}":
            decision = Decision(gate, False, "policy:reject", "rejected by policy (veto test)")
        elif self.mode == "auto" or self.mode.startswith("reject:"):  # reject:<other gate> behaves like auto here
            if blocking_issues:
                decision = Decision(gate, False, "policy:auto", f"{blocking_issues} blocking issue(s); auto-approve refused")
            else:
                decision = Decision(gate, True, "policy:auto", "auto-approved: no blocking issues (demo/CI mode)")
        else:
            raise ValueError(f"unknown approval mode {self.mode!r}")
        self.history.append(decision)
        if self.events:
            self.events.emit("gate_decided", gate=gate, approved=decision.approved,
                             by=decision.decided_by, note=decision.note)
        return decision
