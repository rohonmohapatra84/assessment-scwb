"""Model-backed agents are defined in Markdown (agentic/agents/*.md); deterministic work is plain Python (agentic/steps).
These tests keep the specs, the registered Python agents and the model-facing prompts consistent, so a spec can never
silently drift away from the code it describes."""
import unittest
from pathlib import Path

from agentic.agent_specs import (SPEC_DIR, SpecError, check_specs, describe, load_context, load_spec, load_specs,
                                 parse_spec)
from agentic.harness.harness import agent_registry, default_registry
from agentic.harness.tools import ALL_PLANS, TOOLS
from tests.reference_plans import build_plan

PLAN_TYPES = ALL_PLANS

VALID = """---
name: demo
title: Demo
kind: llm
stage: build
plans: [greenfield, analysis]
inputs: [a]
outputs: [b]
writes: []
---
## Role
Does a thing.
## Guardrails
Safe.
## System prompt
You are a demo agent that replies with JSON.
"""


class Layout(unittest.TestCase):
    def test_agents_folder_is_markdown_only(self):
        stray = [p.name for p in SPEC_DIR.rglob("*") if p.is_file() and p.suffix != ".md"]
        self.assertEqual(stray, [], "agentic/agents/ holds Markdown only; Python belongs in agentic/steps or the harness")

    def test_the_real_agents_are_exactly_these_four(self):
        self.assertEqual(sorted(load_specs()), ["code_author", "project_lead", "requirement_analyst", "spec_tester"])

    def test_registry_and_specs_are_consistent(self):
        self.assertEqual(check_specs(agent_registry()), [])

    def test_steps_are_deterministic_and_not_marked_model_backed(self):
        backed = {n for n, a in agent_registry().items() if getattr(a, "model_backed", False)}
        self.assertEqual(backed, set(load_specs()))

    def test_every_spec_has_role_guardrails_and_a_prompt(self):
        for name, spec in load_specs().items():
            for section in ("Role", "Guardrails", "System prompt"):
                self.assertTrue(spec.section(section), f"{name}: {section}")
            self.assertGreater(len(spec.prompt()), 40, name)

    def test_reference_plans_only_name_registered_agents(self):
        registry = default_registry()
        for plan_type in PLAN_TYPES:
            for t in build_plan({"plan_type": plan_type}):
                self.assertIn(t.agent, registry, f"{plan_type}/{t.id}")
        for t in build_plan({"plan_type": "greenfield", "generator": "llm"}):
            self.assertIn(t.agent, registry)

    def test_write_scopes_in_specs_cover_what_the_catalog_grants_the_model_agents(self):
        """A spec must not under-declare: every path the catalog lets a model-backed tool write appears in its spec."""
        specs = load_specs()
        for name in ("code_author", "spec_tester"):
            for plan_type in PLAN_TYPES:
                granted = set(TOOLS[name].for_plan("writes", plan_type))
                self.assertEqual(granted - set(specs[name].writes), set(), f"{name}/{plan_type}")

    def test_the_lead_agent_cannot_write_files(self):
        self.assertEqual(load_spec("project_lead").writes, ())

    def test_describe_lists_agents_and_steps(self):
        text = describe(load_specs().values(), agent_registry())
        for needle in ("project_lead", "code_author", "spec_tester", "requirement_analyst", "STEPS", "validator"):
            self.assertIn(needle, text)


class Prompts(unittest.TestCase):
    def test_code_agents_share_the_untrusted_input_rule(self):
        for name in ("code_author", "spec_tester", "project_lead", "requirement_analyst"):
            self.assertIn("as data, not as a command" if name != "requirement_analyst" else "as data, not instructions",
                          load_spec(name).prompt())

    def test_requirement_analyst_prompt_keeps_the_json_contract(self):
        p = load_spec("requirement_analyst").prompt()
        self.assertIn("review software requirements", p)
        self.assertIn('{"questions": [..]}', p)

    def test_project_lead_prompt_names_both_jobs_and_the_request_marker(self):
        p = load_spec("project_lead").prompt()
        for needle in ("project lead", "REQUEST_JSON", 'kind = "plan"', 'kind = "decide"', "validator", "summarizer"):
            self.assertIn(needle, p)

    def test_context_interface_spec_is_loaded_and_names_the_required_interface(self):
        text = load_context("url_shortener_interface")
        for needle in ("create_app(", "flush_analytics", "STANDARD LIBRARY ONLY", "X-API-Key"):
            self.assertIn(needle, text)
        from agentic import authoring_agents
        self.assertEqual(authoring_agents.INTERFACE_SPEC, text + "\n")

    def test_missing_spec_and_missing_context_fail_loudly(self):
        with self.assertRaises(SpecError):
            load_spec("no_such_agent")
        with self.assertRaises(SpecError):
            load_context("no_such_context")


class Parser(unittest.TestCase):
    def test_parses_a_valid_spec(self):
        s = parse_spec(VALID)
        self.assertEqual((s.name, s.kind, s.stage, s.plans, s.inputs), ("demo", "llm", "build", ("greenfield", "analysis"), ("a",)))
        self.assertEqual(s.writes, ())

    def test_rejects_malformed_specs(self):
        bad = {
            "no front matter": "## Role\nx\n## Guardrails\ny\n## System prompt\nz\n",
            "unclosed front matter": VALID.replace("---\n## Role", "## Role", 1),
            "bad kind": VALID.replace("kind: llm", "kind: magic"),
            "deterministic is not an agent": VALID.replace("kind: llm", "kind: deterministic"),
            "bad stage": VALID.replace("stage: build", "stage: nowhere"),
            "missing name": VALID.replace("name: demo\n", ""),
            "missing guardrails": VALID.replace("## Guardrails\nSafe.\n", ""),
            "missing prompt": VALID.split("## System prompt")[0],
        }
        for label, text in bad.items():
            with self.subTest(label), self.assertRaises(SpecError):
                parse_spec(text)

    def test_file_name_must_match_agent_name(self):
        with self.assertRaises(SpecError):
            parse_spec(VALID, Path("other.md"))

    def test_headings_inside_code_fences_are_not_sections(self):
        text = VALID + "\n```\n## Not a section\n```\n"
        self.assertNotIn("Not a section", parse_spec(text).sections)

    def test_check_specs_reports_an_orphan_spec_and_a_model_backed_agent_without_one(self):
        registry = agent_registry()
        author = registry.pop("code_author")
        self.assertTrue(any("code_author" in p and "no registered agent" in p for p in check_specs(registry)))
        registry["ghost"] = author  # a model-backed agent under a name with no spec file
        self.assertTrue(any("ghost" in p and "no spec file" in p for p in check_specs(registry)))

    def test_check_specs_rejects_a_spec_whose_implementation_is_a_plain_step(self):
        registry = agent_registry()
        registry["code_author"] = registry["architect"]  # deterministic step standing in for a spec'd agent
        self.assertTrue(any("code_author" in p and "not marked model_backed" in p for p in check_specs(registry)))


if __name__ == "__main__":
    unittest.main()
