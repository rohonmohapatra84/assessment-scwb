"""The tool catalog: what the harness lets a plan use, and the rules each tool carries.

A tool is a deterministic step (agentic/steps) or a model-backed agent (agentic/agents/*.md). The catalog says, as data:

  needs     facts that must already exist (produced by an upstream task or by discovery) before the tool may start
  provides  facts the tool publishes; also the ROLES a validator finding can blame ("code", "tests", "contract", ...)
  writes    the only workspace paths the tool may write (a model cannot widen this)
  plans     which plan types may use it;  generator: template / llm / any (template tools vs model-backed tools)
  kinds     for a change to existing code, the KIND of change (enhancement / bugfix / refactor / tests_docs) can narrow
            what a tool may do: a tests_docs change cannot use code_author at all, a refactor needs no new tests

The lead agent proposes a plan over these tools, and `check_plan` (lead.py) rejects anything that breaks a rule here.
Keeping the rules in data means a model can choose the structure of the work but can never choose its own permissions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

GREENFIELD, BROWNFIELD, LLM_CHANGE, ANALYSIS = "greenfield", "brownfield_change", "llm_change", "analysis"
ALL_PLANS = (GREENFIELD, BROWNFIELD, LLM_CHANGE, ANALYSIS)

# facts the discover phase publishes before any planning (see harness.py)
BASE_FACTS = ("input", "requirement")
REPO_FACTS = ("codebase_model", "impact", "baseline_tests")


def _by_plan(default, **per_plan):
    """A value that can differ per plan type: _by_plan(("a",), llm_change=("b",))."""
    return {"*": tuple(default), **{k: tuple(v) for k, v in per_plan.items()}}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    plans: Tuple[str, ...] = ALL_PLANS
    generator: str = "any"  # template | llm | any
    needs: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: _by_plan(()))
    provides: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: _by_plan(()))
    writes: Dict[str, Tuple[str, ...]] = field(default_factory=lambda: _by_plan(()))
    max_attempts: int = 3
    optional_in: Tuple[str, ...] = ()  # plan types where failure degrades the run instead of blocking it
    repairable: bool = False  # the validator may send it findings and re-run it
    not_for_kinds: Tuple[str, ...] = ()  # change kinds where this tool is not offered at all
    amends_tests: Tuple[str, ...] = ()  # change kinds where the tool may propose edits to EXISTING tests (human-approved)

    def for_plan(self, field_name: str, plan_type: str, kind: str = None) -> Tuple[str, ...]:
        """Value for a plan type, optionally narrowed by change kind: key "llm_change:refactor" beats "llm_change"."""
        table = getattr(self, field_name)
        if kind and f"{plan_type}:{kind}" in table:
            return table[f"{plan_type}:{kind}"]
        return table.get(plan_type, table["*"])


def _t(name, description, **kw) -> ToolSpec:
    return ToolSpec(name=name, description=description, **kw)


TOOLS: Dict[str, ToolSpec] = {t.name: t for t in [
    # ---------------------------------------------------------------- design
    _t("architect", "Decide the architecture: components, data flows, capacity model, architecture decision records.",
       plans=(GREENFIELD,), provides=_by_plan(("design",))),
    _t("contract_designer", "Publish the OpenAPI contract (openapi.json). Code, tests and docs all build on it.",
       plans=(GREENFIELD,), needs=_by_plan(("design",)), provides=_by_plan(("contract",)), writes=_by_plan(("openapi.json",))),
    _t("data_modeler", "Define the relational schema (schema.sql).",
       plans=(GREENFIELD,), needs=_by_plan(("design",)), provides=_by_plan(("schema",)), writes=_by_plan(("schema.sql",))),
    _t("change_designer", "Design a change to existing code from the impact analysis: decisions, touch points, rollout, rollback.",
       plans=(BROWNFIELD,), needs=_by_plan(("impact",)), provides=_by_plan(("change_design",))),
    _t("contract_delta", "Derive the new OpenAPI contract by patching the existing one (additive changes only).",
       plans=(BROWNFIELD,), needs=_by_plan(("change_design",)), provides=_by_plan(("contract", "contract_baseline")),
       writes=_by_plan(("openapi.json",)), repairable=True),
    _t("migration_designer", "Design the database migration for the change (additive, nullable) and update schema.sql.",
       plans=(BROWNFIELD,), needs=_by_plan(("change_design",)), provides=_by_plan(("schema",)), writes=_by_plan(("schema.sql",))),
    _t("options_designer", "For a vague requirement: measurable objectives, design options and trade-offs, gap analysis against the codebase.",
       plans=(ANALYSIS,), provides=_by_plan(("options",))),
    # ------------------------------------------------------------ risk and test strategy
    _t("risk_assessor", "List risks, failure scenarios and mitigations for the work in hand.",
       needs=_by_plan(("design", "contract", "schema"), brownfield_change=("change_design",), llm_change=("change_design",),
                      analysis=("options",), **{"llm_change:tests_docs": ()}),
       provides=_by_plan(("risks",))),
    _t("validation_strategist", "Define the test strategy and verification steps, derived from the risks, before any test is written.",
       needs=_by_plan(("risks",), analysis=("options",)),  # analysis: runs beside the risk assessor, both feed the proposal
       provides=_by_plan(("validation_strategy",))),
    # ----------------------------------------------------------------- build (template)
    _t("code_generator", "Template-based: render the URL shortener service code and packaging (deterministic, no model).",
       plans=(GREENFIELD,), generator="template", needs=_by_plan(("contract", "schema")), provides=_by_plan(("code",)),
       writes=_by_plan(("shortener/*", "scripts/*", "Dockerfile", "pyproject.toml", ".gitignore")), repairable=True),
    _t("test_generator", "Template-based: render the test suite and traceability table from the contract and strategy (no model).",
       plans=(GREENFIELD, BROWNFIELD), generator="template", needs=_by_plan(("contract", "validation_strategy")),
       provides=_by_plan(("tests", "ac_tests")), writes=_by_plan(("tests/*",)), repairable=True),
    _t("patch_generator", "Template-based: apply the anchored edit recipe (link expiry) to the existing code and write changes.diff.",
       plans=(BROWNFIELD,), generator="template", needs=_by_plan(("contract", "schema")), provides=_by_plan(("code", "patch_stats")),
       writes=_by_plan(("shortener/*", "changes.diff")), repairable=True),
    # ----------------------------------------------------------------- build (model-backed agents)
    _t("code_author", "MODEL-BACKED agent: writes the service (greenfield) or proposes anchored edits to existing code (llm_change).",
       plans=(GREENFIELD, LLM_CHANGE), generator="llm", max_attempts=4, repairable=True, not_for_kinds=("tests_docs",),
       needs=_by_plan(("contract", "schema"), llm_change=("impact",)),
       provides=_by_plan(("code",), llm_change=("code", "change_design", "patch_stats")),
       writes=_by_plan(("shortener/*", "scripts/*", "Dockerfile", "pyproject.toml", ".gitignore"),
                       llm_change=("shortener/*", "openapi.json", "schema.sql", "changes.diff"))),
    _t("spec_tester", "MODEL-BACKED agent: writes tests from the specification only, never seeing the code (independent oracle).",
       plans=(GREENFIELD, LLM_CHANGE), generator="llm", max_attempts=4, repairable=True, amends_tests=("enhancement",),
       # a change is tested against the contract the code author PUBLISHES (never the code), so the tests wait for it
       needs=_by_plan(("contract", "validation_strategy"), llm_change=("change_design",), **{"llm_change:tests_docs": ()}),
       provides=_by_plan(("tests", "ac_tests"), llm_change=("tests",)),
       writes=_by_plan(("tests/*",), llm_change=("tests/test_change_*.py",))),
    _t("doc_writer", "Write the documentation: README, architecture, ADRs, risks (new system); change record and changelog (change); proposal (analysis).",
       needs=_by_plan(("design", "contract", "risks", "validation_strategy"), brownfield_change=("change_design", "risks", "patch_stats"),
                      llm_change=("change_design", "risks", "patch_stats"), analysis=("options", "risks", "validation_strategy"),
                      **{"llm_change:tests_docs": ("risks",)}),
       provides=_by_plan(("docs",)),
       writes=_by_plan(("README.md", "docs/*"), brownfield_change=("README.md", "CHANGELOG.md", "docs/*"),
                       llm_change=("docs/*", "CHANGELOG.md"), analysis=("docs/*",)),
       optional_in=(GREENFIELD, BROWNFIELD, LLM_CHANGE)),
    # ----------------------------------------------------------------- check
    _t("validator", "The quality gate: runs static, contract, schema, test, regression and traceability checks. Blames producers by role.",
       needs=_by_plan(("code", "tests", "contract", "schema"), brownfield_change=("code", "tests", "contract", "schema", "contract_baseline"),
                      llm_change=("code", "tests"), analysis=("options", "validation_strategy", "docs"),
                      **{"llm_change:refactor": ("code",), "llm_change:tests_docs": ()}),  # a refactor needs no new tests; tests_docs has no code
       provides=_by_plan(("validation_report",))),
    _t("summarizer", "Aggregate the evidence for the engineering summary.",
       needs=_by_plan(("validation_report",)), provides=_by_plan(("summary_data",))),
]}

REQUIRED_TOOLS = ("validator", "summarizer")


def available(plan_type: str, generator: str, kind: str = None) -> List[ToolSpec]:
    """Tools a plan of this type may use. Template tools are hidden in model mode and the reverse; a change to existing
    code always uses the model-backed tools (no template recipe covers an arbitrary change); the change kind can hide more."""
    wanted = "llm" if generator == "llm" or plan_type == LLM_CHANGE else "template"
    return [t for t in TOOLS.values() if plan_type in t.plans and t.generator in ("any", wanted)
            and not (plan_type == LLM_CHANGE and kind in t.not_for_kinds)]


def roles_of(tool_name: str, plan_type: str, kind: str = None) -> Tuple[str, ...]:
    """The roles a validator finding can use to blame a task running this tool."""
    spec = TOOLS.get(tool_name)
    return spec.for_plan("provides", plan_type, kind) if spec else ()


def catalog_for_prompt(plan_type: str, generator: str, kind: str = None) -> List[dict]:
    """What the lead agent is shown for each tool. Permissions are included so the plan can respect them."""
    out = []
    for t in available(plan_type, generator, kind):
        out.append({"tool": t.name, "description": t.description, "needs": list(t.for_plan("needs", plan_type, kind)),
                    "provides": list(t.for_plan("provides", plan_type, kind)), "writes": list(t.for_plan("writes", plan_type, kind)),
                    "may_fail_without_blocking": plan_type in t.optional_in})
    return out
