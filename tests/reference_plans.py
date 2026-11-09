"""Reference plans: what a good project_lead proposes for each plan type (test fixture).

The harness no longer has a built-in planner: the lead agent (a model) writes the plan. Tests use these hand-written
plans as the scripted model's answer (see fake_llm.ScriptedLLM), so end-to-end tests stay deterministic and offline,
and so there is a known-good reference to compare real model plans against.
"""
from __future__ import annotations

from typing import Any, Dict, List

from agentic.harness.core import Task


def build_plan(req: Dict[str, Any]) -> List[Task]:
    pt = req["plan_type"]
    if pt == "greenfield":
        tasks = _greenfield()
        if req.get("generator") == "llm":  # same DAG, same ids and scopes: only the producers change
            swap = {"gen_code": "code_author", "gen_tests": "spec_tester"}
            for t in tasks:
                if t.id in swap:
                    t.agent = swap[t.id]
                    t.description = "[LLM] " + t.description
                    t.max_attempts = 4  # self-correction budget for malformed/incomplete model output
        return tasks
    if pt == "llm_change":
        return _llm_change()
    if pt == "brownfield_change":
        return _brownfield()
    return _analysis()


def _greenfield() -> List[Task]:
    return [
        Task("design_architecture", "Design architecture, ADRs and capacity model", "architect",
             description="Everything downstream depends on the architecture decisions (storage port, async analytics, cache)."),
        Task("design_contract", "Publish the OpenAPI contract", "contract_designer", ["design_architecture"],
             writes=["openapi.json"], description="Contract-first: code, tests and docs all consume this single source of truth."),
        Task("design_data_model", "Define the relational schema", "data_modeler", ["design_architecture"],
             writes=["schema.sql"], description="Runs in parallel with the contract (no dependency between them)."),
        Task("assess_risks", "Identify risks, failure scenarios and mitigations", "risk_assessor",
             ["design_architecture", "design_contract", "design_data_model"],
             description="Risks need the concrete contract/schema; runs in parallel with code generation."),
        Task("define_validation", "Define test strategy and verification steps", "validation_strategist", ["assess_risks"],
             description="Strategy is derived from risks BEFORE tests are written, so tests are risk-driven."),
        Task("gen_code", "Generate service code, scripts and packaging", "code_generator",
             ["design_contract", "design_data_model"],
             writes=["shortener/*", "scripts/*", "Dockerfile", "pyproject.toml", ".gitignore"],
             description="Needs only contract + schema, so it starts as soon as they exist."),
        Task("gen_tests", "Generate unit, integration, contract tests", "test_generator",
             ["design_contract", "define_validation"], writes=["tests/*"],
             description="Written against the contract + strategy, not against the generated code (independent oracle)."),
        Task("gen_docs", "Write README, architecture, ADR, risk and testing docs", "doc_writer",
             ["design_architecture", "design_contract", "assess_risks", "define_validation"], optional=True,
             writes=["README.md", "docs/*"],
             description="Optional: if it fails the release is flagged 'degraded' rather than blocked."),
        Task("validate", "Validate: static, contract, schema, tests, traceability", "validator",
             ["gen_code", "gen_tests", "gen_docs"], repair_targets=["gen_code", "gen_tests"],
             description="Quality gate. Errors are routed back to the responsible producer (repair loop)."),
        Task("summarize", "Aggregate evidence for the engineering summary", "summarizer", ["validate"]),
    ]


def _brownfield() -> List[Task]:
    return [
        Task("design_change", "Design the change from the impact analysis", "change_designer",
             description="Decisions are driven by the codebase model (impacted routes, modules, tables)."),
        Task("design_contract_delta", "Derive the OpenAPI delta (additive)", "contract_delta", ["design_change"],
             writes=["openapi.json"], description="Patches the existing spec rather than regenerating it."),
        Task("design_data_migration", "Design the schema migration", "migration_designer", ["design_change"],
             writes=["schema.sql"], description="Additive, nullable column: zero-downtime."),
        Task("assess_risks", "Assess change risks and rollback", "risk_assessor", ["design_change"]),
        Task("define_validation", "Define regression + change test strategy", "validation_strategist", ["assess_risks"]),
        Task("gen_patch", "Apply anchored edits to existing modules", "patch_generator",
             ["design_contract_delta", "design_data_migration"], writes=["shortener/*", "changes.diff"],
             description="Edits an existing codebase: every anchor must match exactly once or the task fails safe."),
        Task("gen_tests", "Generate change tests + migration test", "test_generator",
             ["design_contract_delta", "define_validation"], writes=["tests/*"]),
        Task("gen_docs", "Update README, changelog and change record", "doc_writer",
             ["design_change", "assess_risks", "gen_patch"], optional=True, writes=["README.md", "CHANGELOG.md", "docs/*"]),
        Task("validate", "Validate: regression, compat, migration, contract", "validator",
             ["gen_patch", "gen_tests", "gen_docs"], repair_targets=["gen_patch", "gen_tests", "design_contract_delta"]),
        Task("summarize", "Aggregate evidence for the engineering summary", "summarizer", ["validate"]),
    ]


def _llm_change() -> List[Task]:
    return [
        Task("gen_patch", "LLM proposes anchored edits from the impact analysis", "code_author", max_attempts=4,
             writes=["shortener/*", "openapi.json", "schema.sql", "changes.diff"],
             description="[LLM] Edits must match the pristine baseline exactly once; mismatches are rejected with feedback, never guessed."),
        Task("gen_tests", "LLM writes change tests from the spec only", "spec_tester", ["gen_patch"], max_attempts=4,
             writes=["tests/test_change_*.py"],
             description="[LLM] Waits for the published contract, never reads the code: tests come from the spec."),
        Task("assess_risks", "Assess change risks", "risk_assessor", ["gen_patch"],
             description="Combines the standing risk list with the risks the model itself reported."),
        Task("define_validation", "Define regression + change test strategy", "validation_strategist", ["assess_risks"]),
        Task("gen_docs", "Write the change record", "doc_writer", ["gen_patch", "assess_risks"], optional=True,
             writes=["docs/*", "CHANGELOG.md"]),
        Task("validate", "Validate: regression, API diff, behavioural probe, new tests", "validator",
             ["gen_patch", "gen_tests", "gen_docs", "define_validation"], repair_targets=["gen_patch", "gen_tests"]),
        Task("summarize", "Aggregate evidence for the engineering summary", "summarizer", ["validate"]),
    ]


def _analysis() -> List[Task]:
    return [
        Task("design_options", "Turn vague goals into measurable objectives and options", "options_designer",
             description="No code is generated until the objectives are confirmed; options carry explicit trade-offs."),
        Task("assess_risks", "Assess risks of pursuing each objective", "risk_assessor", ["design_options"]),
        Task("define_validation", "Define how each objective will be verified", "validation_strategist", ["design_options"],
             description="Independent of risk assessment, so the two run in parallel; both feed the proposal."),
        Task("gen_docs", "Write the proposal document", "doc_writer", ["design_options", "assess_risks", "define_validation"],
             writes=["docs/*"]),
        Task("validate", "Validate plan completeness and measurability", "validator",
             ["gen_docs", "define_validation", "design_options"], repair_targets=["design_options"]),
        Task("summarize", "Aggregate evidence for the engineering summary", "summarizer", ["validate"]),
    ]

def lead_reply(req: Dict[str, Any]) -> Dict[str, Any]:
    """The reference plan in the JSON shape the project_lead agent must return."""
    return {"rationale": "reference plan (scripted)",
            "tasks": [{"id": t.id, "tool": t.agent, "title": t.title, "deps": list(t.deps)} for t in build_plan(req)]}
