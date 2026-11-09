"""Renders the structured engineering summary (assessment deliverable #8):
plan + rationale, artifacts, risks / trade-offs / validation, assumptions / limitations,
plus the evidence that autonomy was controlled (retries, repairs, guardrails, approvals)."""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .core import Task, TaskStatus


def _t(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "_none_"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows]
    return "\n".join(out)


def _waves(tasks: List[Task]) -> Dict[str, int]:
    level: Dict[str, int] = {}
    by_id = {t.id: t for t in tasks}

    def lvl(tid: str) -> int:
        if tid not in level:
            level[tid] = 1 + max([lvl(d) for d in by_id[tid].deps], default=0)
        return level[tid]
    for t in tasks:
        lvl(t.id)
    return level


def _mermaid(tasks: List[Task]) -> str:
    cls = {TaskStatus.SUCCEEDED: "ok", TaskStatus.FAILED: "bad", TaskStatus.SKIPPED: "skip",
           TaskStatus.PENDING: "skip", TaskStatus.RUNNING: "skip"}
    lines = ["```mermaid", "flowchart TD"]
    for t in tasks:
        lines.append(f'  {t.id}["{t.id}<br/>{t.agent}"]:::{cls[t.status]}')
    for t in tasks:
        for d in t.deps:
            lines.append(f"  {d} --> {t.id}")
    lines += ["  classDef ok fill:#d4edda,stroke:#2e7d32", "  classDef bad fill:#f8d7da,stroke:#c62828",
              "  classDef skip fill:#eee,stroke:#999", "```"]
    return "\n".join(lines)


def render_engineering_summary(*, status, bb, tasks, events, gates, report, ws, seconds, out_dir) -> str:
    req = bb.get("requirement") or {}
    pt = req.get("plan_type", "?")
    L: List[str] = []
    A = L.append
    A(f"# Engineering Summary: {req.get('title', 'n/a')}")
    A("")
    A(f"- **Status:** `{status}`   - **Plan type:** `{pt}`   - **Duration:** {seconds:.1f}s")
    A(f"- **Requirement:** \"{req.get('raw', '')}\"")
    A("")

    # ---------------------------------------------------------------- 1 understanding
    A("## 1. Requirement understanding")
    A("")
    A(f"**Normalised problem:** {req.get('problem_statement', '')}")
    A("")
    A(_t(["ID", "Capability", "Detail"], [[c["id"], c["name"], c["detail"]] for c in req.get("capabilities", [])]))
    if req.get("nfrs"):
        A("")
        A(_t(["ID", "Non-functional requirement", "Target", "Assumed?"],
             [[n["id"], n["name"], n["target"], "yes" if n["assumed"] else "no"] for n in req["nfrs"]]))
    if req.get("ambiguities"):
        A("")
        A("**Ambiguities detected**")
        A("")
        A(_t(["ID", "Vague term", "Question", "Resolved by"],
             [[a["id"], a["term"], a["question"], req["resolved_by"].get(a["id"], "documented assumption")] for a in req["ambiguities"]]))
    if req.get("assumptions"):
        A("")
        A("**Assumptions recorded**")
        A("")
        A(_t(["ID", "Assumption", "Confidence", "Needs confirmation"],
             [[a["id"], a["text"], a["confidence"], "yes" if a["needs_confirmation"] else "no"] for a in req["assumptions"]]))
    if req.get("security_flags"):
        A("")
        A(f"> Guardrail: requirement text matched {len(req['security_flags'])} suspicious-instruction pattern(s); it was treated as data only.")
    A("")
    A("**Acceptance criteria**")
    A("")
    A("\n".join(f"- {a}" for a in req.get("acceptance_criteria", [])))
    A("")

    # ---------------------------------------------------------------- 2 codebase
    model, impact = bb.get("codebase_model"), bb.get("impact")
    if model:
        A("## 2. Codebase reasoning (brownfield)")
        A("")
        A(f"Indexed **{len(model['modules'])} modules, {len(model['tests'])} test files, {len(model['routes'])} routes, "
          f"{len(model['tables'])} tables** by AST analysis. Baseline suite: {(bb.get('baseline_tests') or {}).get('ran')} tests, "
          f"green={(bb.get('baseline_tests') or {}).get('ok')}.")
        A("")
        A("**Data flows (route -> call chain -> tables)**")
        A("")
        A(_t(["Route", "Call chain", "Reads", "Writes"],
             [[f["route"], " > ".join(f["chain"][:7]) + (" > ..." if len(f["chain"]) > 7 else ""),
               ",".join(f["tables_read"]), ",".join(f["tables_written"])] for f in model["flows"]]))
        A("")
        A(f"**Impact of this change** (risk: **{impact['risk_level']}**)")
        A("")
        A(f"- Routes: {', '.join(f'`{r}`' for r in impact['routes'])}")
        A(f"- Blast radius: {impact['blast_radius']}")
        A(f"- Modules that change: {', '.join(f'`{m}`' for m in impact['modules'])}")
        A(f"- Other modules on the affected flows (not edited, re-tested): "
          f"{', '.join(f'`{m}`' for m in impact['flow_modules'] if m not in impact['modules'])}")
        A(f"- Tables: {', '.join(f'`{t}`' for t in impact['tables'])}")
        A(f"- Tests that exercise the impacted code: {', '.join(f'`{t}`' for t in impact['tests'])}")
        A("")

    # ---------------------------------------------------------------- 3 plan
    A("## 3. Implementation plan and rationale")
    A("")
    lv = _waves(tasks)
    A("`Tool` is what runs the task. **(model)** marks the model-backed agents; every other tool is a deterministic step (plain Python, no model call).")
    A("")
    model_backed = {"code_author", "spec_tester", "requirement_analyst"}
    A(_t(["Wave", "Task", "Tool", "Depends on", "Status", "Attempts", "Gen", "Rationale"],
         [[lv[t.id], f"`{t.id}`", t.agent + (" **(model)**" if t.agent in model_backed else ""), ", ".join(t.deps) or "-", t.status.value + (" (optional)" if t.optional else ""),
           t.attempts, t.generation, t.description] for t in sorted(tasks, key=lambda x: (lv[x.id], x.id))]))
    A("")
    A(_mermaid(tasks))
    A("")
    A("**Design decisions**")
    A("")
    design, change, options = bb.get("design"), bb.get("change_design"), bb.get("options")
    if design:
        A(_t(["ID", "Decision", "Why", "Trade-off"], [[a["id"], a["decision"], a["why"], a["tradeoffs"]] for a in design["adrs"]]))
    elif change:
        A(_t(["ID", "Decision", "Why", "Trade-off"], [[a["id"], a["title"], a["why"], a["tradeoffs"]] for a in change["decisions"]]))
    elif options:
        A(_t(["Objective", "Target", "Recommendation"], [[o["objective"], o["target"], o["recommended"]] for o in options["objectives"]]))
    A("")

    # ---------------------------------------------------------------- 4 execution
    A("## 4. Orchestration evidence")
    A("")
    A(f"- Max concurrently running tasks: **{report.get('max_concurrency', '?')}**   - Transient retries: **{report.get('retries', 0)}**   "
      f"- Repair loops: **{sum((report.get('repairs') or {}).values())}**   - Degraded (optional) tasks: **{', '.join(report.get('degraded') or []) or 'none'}**")
    usage = events.of_kind("llm_usage")
    if usage:
        u = usage[-1]
        A("")
        A(f"- **LLM:** provider `{u.get('provider')}`, model `{u.get('model') or u.get('deployment')}`: {u.get('calls', 0)} call(s), "
          f"{u.get('prompt_tokens', 0):,} prompt + {u.get('completion_tokens', 0):,} completion tokens. Every output was written through the "
          "sandbox and judged by executed validation, not by the model.")
    A("")
    notable = events.of_kind("task_retry", "task_self_correct", "repair_loop", "task_failed", "task_skipped",
                             "guardrail_hit", "clarification_needed", "llm_fallback", "llm_used", "release_blocked")
    A(_t(["Event", "Detail"], [[e["kind"], " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "kind"))] for e in notable]))
    A("")
    hist = bb.get("validation_history") or []
    if hist:
        A("**Validation across repair generations**")
        A("")
        A(_t(["Generation", "Failed checks", "Which"], [[h["generation"], h["failed"], ", ".join(h["failed_checks"]) or "-"] for h in hist]))
        A("")

    # ---------------------------------------------------------------- 5 artifacts
    A("## 5. Generated artifacts")
    A("")
    manifest = [e for e in ws.manifest.values() if e.kind != "baseline"]
    kinds: Dict[str, int] = {}
    for e in manifest:
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    A(f"{len(manifest)} files written by agents ({', '.join(f'{k}: {v}' for k, v in sorted(kinds.items()))}); "
      f"{len([e for e in ws.manifest.values() if e.kind == 'baseline'])} baseline files carried over unchanged-or-patched.")
    A("")
    A(_t(["Path", "Kind", "Bytes", "Produced by", "sha256"], [[f"`{e.path}`", e.kind, e.bytes, e.producer, e.sha256[:12]] for e in sorted(manifest, key=lambda x: x.path)]))
    A("")
    if bb.get("patch_stats"):
        p = bb.get("patch_stats")
        A(f"**Patch:** {p['edits']} anchored edits across {len(p['files'])} files (+{p['added']} / -{p['removed']} lines); see `changes.diff`.")
        A("")

    # ---------------------------------------------------------------- 6 validation
    A("## 6. Validation")
    A("")
    vr = bb.get("validation_report")
    if vr:
        A(_t(["Check", "Result", "Evidence"], [[f"`{c['check']}`", c["status"], c["detail"].splitlines()[0][:200]] for c in vr["checks"]]))
        A("")
        skipped = sum(c["status"] == "SKIP" for c in vr["checks"])
        A(f"**{vr['passed']} passed, {vr['failed']} failed, {vr['warnings']} warning(s)"
          + (f", {skipped} skipped" if skipped else "") + f"** (final generation {vr['generation']}).")
        tr = bb.get("test_run")
        if tr:
            A(f" Executed test suite: **{tr['ran']} tests**, ok={tr['ok']}, {tr['seconds']}s.")
        if bb.get("api_diff"):
            d = bb.get("api_diff")
            A("")
            A(f"OpenAPI diff: breaking={len(d['breaking'])}; additive: {'; '.join(d['additive'])}")
    else:
        A("_Validation did not run (pipeline stopped earlier)._")
    A("")
    if bb.get("test_updates"):
        A("**Existing tests amended (human-approved test-update policy)**")
        A("")
        A("Agents may not edit a test they did not write, except in an enhancement whose contract change supersedes it. "
          "Each amendment below was checked by counting (no test removed, no assertion lost, nothing skipped) and went to the "
          "`test_update_approval` gate.")
        A("")
        A(_t(["File", "Reason", "Proposed by"], [[f"`{u['path']}`", u["reason"], u["task"]] for u in bb.get("test_updates")]))
        A("")
    if bb.get("old_code_run"):
        o = bb.get("old_code_run")
        A(f"**Bug-fix proof:** the new tests were run against the unfixed baseline: {o['failing_new_tests']} of them failed there "
          "and pass on the fix, so they demonstrate the defect.")
        A("")

    # ---------------------------------------------------------------- 7 risks
    A("## 7. Risks, trade-offs and failure scenarios")
    A("")
    risks = bb.get("risks")
    if risks:
        A("**Domain risks**")
        A("")
        A(_t(["ID", "Risk", "Likelihood", "Impact", "Mitigation", "Covered by"],
             [[r["id"], r["risk"], r.get("likelihood", "-"), r.get("impact", "-"), r["mitigation"], r.get("covered_by", "-")] for r in risks["domain"]]))
        A("")
        A("**Risks of autonomous generation itself** (and the guardrail for each)")
        A("")
        A(_t(["ID", "Risk", "Guardrail"], [[r["id"], r["risk"], r["mitigation"]] for r in risks["agentic"]]))
    A("")

    # ---------------------------------------------------------------- 8 autonomy
    A("## 8. Controlled autonomy: approval audit")
    A("")
    A(_t(["Gate", "Approved", "Decided by", "Note"], [[g.gate, g.approved, g.decided_by, g.note] for g in gates.history]))
    A("")
    A("Agents ran without human input between gates; the original repository (brownfield) is never modified, and nothing is copied to `release/` unless the release gate approves.")
    A("")

    # ---------------------------------------------------------------- 9 limits
    A("## 9. Assumptions and limitations")
    A("")
    A("\n".join(f"- {s}" for s in [
        "Agents are deterministic and template/recipe-driven so runs are reproducible offline; an LLM is an optional, additive seam (requirement questions only) and is untested against a live API in this prototype.",
        "Codebase analysis is Python-AST and regex based (decorator routes, SQL strings); it will not understand dynamic routing, ORMs or other languages.",
        "Brownfield changes are limited to implemented recipes (link expiry). An unknown change produces a proposal, not code - by design, rather than guessing.",
        "Capacity numbers are assumptions, not measurements; the load smoke test is single-process.",
        "SQLite (single node) stands in for a scalable datastore behind a Repository port; the scale-out path is documented, not implemented.",
        "Per-node rate limiting and in-process cache are approximate across multiple nodes (see ADR-005/007).",
        "Human approval in `auto` mode is a scripted policy for demo/CI, not a substitute for review.",
    ] + [f"Assumption: {a['text']}" for a in req.get("assumptions", [])]))
    A("")
    return "\n".join(L) + "\n"
