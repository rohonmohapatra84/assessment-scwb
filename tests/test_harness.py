"""The model-driven harness: the lead proposes, the harness checks.

Covers the tool catalog, plan checking, the decision loop (including a lead that misbehaves), and end-to-end runs where the
(scripted) lead chooses its own task ids, which proves failures are blamed by role and not by hard-coded id.
"""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentic.harness.core import Blackboard, Chaos, EventLog, Task, TaskStatus
from agentic.harness.guardrails import Workspace
from agentic.harness.harness import Harness, default_registry
from agentic.harness.lead import MAX_TASKS, ModelLead, check_plan, facts_for
from agentic.harness.loop import GreedyLead, LeadError, Loop, PlanError
from agentic.harness.tools import ALL_PLANS, REQUIRED_TOOLS, TOOLS, available, catalog_for_prompt, roles_of
from agentic.providers import LLMUnavailable
from tests.fake_llm import ScriptedLLM
from tests.reference_plans import build_plan, lead_reply

GREENFIELD = "Build a scalable URL shortener service with APIs, persistence, and analytics."
PLAN_CASES = [("greenfield", "template", False), ("greenfield", "llm", False), ("brownfield_change", "template", True),
              ("llm_change", "llm", True), ("analysis", "template", True)]


def reply_for(pt, generator="template"):
    return lead_reply({"plan_type": pt, "generator": generator})


def check(reply, pt="greenfield", generator="template", repo=False, kind=None):
    return check_plan(reply, pt, generator, facts_for(repo), kind)


class Catalog(unittest.TestCase):
    def test_every_runnable_step_has_a_tool_spec_and_vice_versa(self):
        discovery = {"requirement_analyst", "codebase_analyst"}  # run by the harness itself before planning
        self.assertEqual(set(TOOLS), set(default_registry()) - discovery)

    def test_catalog_write_scopes_match_the_reference_plans(self):
        """The catalog (data) must grant each tool exactly what the old hand-written planner granted."""
        for pt, generator, _ in PLAN_CASES:
            for t in build_plan({"plan_type": pt, "generator": generator}):
                if t.writes:
                    self.assertEqual(set(t.writes), set(TOOLS[t.agent].for_plan("writes", pt)), f"{pt}/{generator}/{t.agent}")

    def test_the_reference_plans_pass_the_plan_check(self):
        for pt, generator, repo in PLAN_CASES:
            tasks, waves = check(reply_for(pt, generator), pt, generator, repo)
            self.assertGreaterEqual(len(waves), 4, f"{pt}: should have a real dependency structure")
            self.assertEqual(sum(t.agent in REQUIRED_TOOLS for t in tasks), 2)

    def test_template_tools_are_hidden_in_model_mode_and_the_reverse(self):
        template = {t.name for t in available("greenfield", "template")}
        model = {t.name for t in available("greenfield", "llm")}
        self.assertIn("code_generator", template - model)
        self.assertIn("code_author", model - template)
        self.assertTrue({"validator", "summarizer"} <= template & model)

    def test_every_tool_is_offered_in_at_least_one_plan_and_has_a_description(self):
        for name, spec in TOOLS.items():
            self.assertTrue(spec.description, name)
            self.assertTrue(any(name in {t.name for t in available(pt, g)} for pt in ALL_PLANS for g in ("template", "llm")), name)

    def test_prompt_catalog_shows_permissions_and_needs(self):
        entry = next(e for e in catalog_for_prompt("greenfield", "template") if e["tool"] == "code_generator")
        self.assertEqual(entry["needs"], ["contract", "schema"])
        self.assertIn("shortener/*", entry["writes"])

    def test_roles_resolve_for_blame(self):
        self.assertIn("code", roles_of("code_generator", "greenfield"))
        self.assertIn("tests", roles_of("spec_tester", "greenfield"))
        self.assertIn("contract", roles_of("contract_delta", "brownfield_change"))
        self.assertEqual(roles_of("not_a_tool", "greenfield"), ())


class PlanCheck(unittest.TestCase):
    def reply(self):
        return reply_for("greenfield")

    def rejects(self, reply, pattern, **kw):
        with self.assertRaisesRegex(PlanError, pattern):
            check(reply, **kw)

    def test_valid_plan_gets_scopes_optional_flags_and_repair_targets_from_the_catalog(self):
        tasks, _ = check(self.reply())
        by_id = {t.id: t for t in tasks}
        self.assertEqual(by_id["gen_code"].writes, list(TOOLS["code_generator"].writes["*"]))
        self.assertTrue(by_id["gen_docs"].optional)
        self.assertFalse(by_id["gen_code"].optional)
        self.assertEqual(sorted(by_id["validate"].repair_targets), ["gen_code", "gen_tests"])

    def test_model_cannot_widen_a_write_scope_or_mark_a_task_optional(self):
        r = self.reply()
        for t in r["tasks"]:
            t["writes"], t["optional"] = ["**/*"], True
        tasks, _ = check(r)
        by_id = {t.id: t for t in tasks}
        self.assertNotIn("**/*", by_id["gen_code"].writes)
        self.assertFalse(by_id["gen_code"].optional)

    def test_rejects_malformed_replies(self):
        for bad in ({}, {"tasks": []}, {"tasks": "x"}, {"__invalid__": "model reply was not valid JSON"}, []):
            with self.subTest(bad=bad), self.assertRaises(PlanError):
                check(bad)

    def test_rejects_unknown_tool_and_wrong_mode_tool(self):
        r = self.reply()
        r["tasks"][0]["tool"] = "magic_wand"
        self.rejects(r, "not available.*Available tools")
        r = reply_for("greenfield", "llm")
        with self.assertRaisesRegex(PlanError, "code_author.*not available"):
            check_plan(r, "greenfield", "template", facts_for(False))  # model-backed tool in template mode

    def test_rejects_a_tool_used_twice(self):
        r = self.reply()
        r["tasks"].append({"id": "second_risk", "tool": "risk_assessor", "title": "again", "deps": ["design_architecture"]})
        self.rejects(r, "used twice")

    def test_rejects_a_missing_validator_or_summarizer(self):
        for name in REQUIRED_TOOLS:
            r = self.reply()
            r["tasks"] = [t for t in r["tasks"] if t["tool"] != name]
            self.rejects(r, f"must include the '{name}'")

    def test_rejects_a_task_whose_needs_are_not_provided_by_its_dependencies(self):
        r = self.reply()
        for t in r["tasks"]:
            if t["id"] == "gen_code":
                t["deps"] = []  # code needs the contract and schema; nothing upstream provides them now
        self.rejects(r, "gen_code.*needs.*contract")

    def test_error_message_names_the_tools_that_would_fix_it(self):
        r = self.reply()
        for t in r["tasks"]:
            if t["id"] == "gen_code":
                t["deps"] = []
        with self.assertRaisesRegex(PlanError, "contract_designer"):
            check(r)

    def test_rejects_a_validator_that_does_not_wait_for_everything(self):
        r = self.reply()
        for t in r["tasks"]:
            if t["id"] == "validate":
                t["deps"] = ["gen_code", "gen_tests"]  # gen_docs is not waited for
        self.rejects(r, "validator must depend.*gen_docs")

    def test_rejects_cycles_unknown_deps_bad_ids_and_oversized_plans(self):
        r = self.reply()
        r["tasks"][0]["deps"] = ["summarize"]
        self.rejects(r, "cycle")
        r = self.reply()
        r["tasks"][1]["deps"] = ["ghost"]
        self.rejects(r, "unknown task")
        r = self.reply()
        r["tasks"][0]["id"] = "Bad Id!"
        self.rejects(r, "snake_case")
        r = self.reply()
        r["tasks"] = r["tasks"] * (MAX_TASKS // 5)
        self.rejects(r, "over the limit")

    def test_summarizer_must_follow_the_validator(self):
        r = self.reply()
        for t in r["tasks"]:
            if t["id"] == "summarize":
                t["deps"] = ["gen_code"]
        self.rejects(r, "summarizer must depend on the validator|needs.*validation_report")


class LoopWithALead(unittest.TestCase):
    """The loop with tiny fake agents (no code generation): who the lead picks, and what the harness refuses."""

    class Fn:
        def __init__(self, name, fn=None):
            self.name, self._fn = name, fn

        def run(self, ctx):
            return self._fn(ctx) if self._fn else {}

    def make(self, lead, names="abc", **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events = EventLog()
        loop = Loop({n: self.Fn(n) for n in names}, Blackboard(), Workspace(Path(tmp.name)), events, Chaos(), None,
                    lead=lead, backoff_base=0.001, **kw)
        return loop, events

    def tasks(self):  # a, b, c all independent
        return [Task("a", "a", "a"), Task("b", "b", "b"), Task("c", "c", "c")]

    class Scripted:
        def __init__(self, *replies):
            self.replies, self.seen = list(replies), []

        def decide(self, observation, feedback=None):
            self.seen.append((observation, feedback))
            return self.replies.pop(0) if self.replies else {"action": "start", "tasks": list(observation["ready"])}

    def test_the_lead_decides_order_and_the_harness_runs_exactly_that(self):
        order = []
        lead = self.Scripted({"action": "start", "tasks": ["c"]}, {"action": "start", "tasks": ["a"]},
                             {"action": "start", "tasks": ["b"]})
        loop, _ = self.make(lead, max_workers=1)
        for n in "abc":
            loop.agents[n] = self.Fn(n, lambda ctx, n=n: order.append(n))
        report = loop.run(self.tasks())
        self.assertTrue(report["ok"])
        self.assertEqual(order, ["c", "a", "b"])
        self.assertEqual(report["lead_turns"], 2)  # the last task had no alternative, so the lead was not asked

    def test_no_model_call_when_there_is_nothing_to_decide(self):
        lead = self.Scripted()
        loop, _ = self.make(lead)
        report = loop.run([Task("a", "a", "a")])
        self.assertTrue(report["ok"])
        self.assertEqual((lead.seen, report["lead_turns"]), ([], 0))

    def test_observation_lists_ready_tasks_and_free_slots(self):
        lead = self.Scripted()
        loop, _ = self.make(lead, max_workers=2)
        loop.run(self.tasks())
        obs, _ = lead.seen[0]
        self.assertEqual((sorted(obs["ready"]), obs["free_slots"], obs["running"]), (["a", "b", "c"], 2, []))
        self.assertEqual({t["id"] for t in obs["tasks"]}, {"a", "b", "c"})

    def test_decision_for_a_task_that_is_not_ready_is_refused_then_fixed(self):
        tasks = [Task("a", "a", "a"), Task("b", "b", "b", ["a"]), Task("c", "c", "c")]
        lead = self.Scripted({"action": "start", "tasks": ["b"]}, {"action": "start", "tasks": ["a", "c"]})
        loop, events = self.make(lead)
        report = loop.run(tasks)
        self.assertTrue(report["ok"])
        self.assertIn("Invalid decision", lead.seen[1][1])  # the lead was told what was wrong
        self.assertEqual(report["lead_fallbacks"], 0)

    def test_two_bad_decisions_trigger_a_logged_fallback_and_the_run_still_finishes(self):
        lead = self.Scripted({"action": "dance"}, {"nope": 1})
        loop, events = self.make(lead)
        report = loop.run(self.tasks())
        self.assertTrue(report["ok"])
        self.assertEqual(report["lead_fallbacks"], 1)
        self.assertEqual(len(events.of_kind("lead_fallback")), 1)

    def test_more_tasks_than_free_slots_is_refused(self):
        lead = self.Scripted({"action": "start", "tasks": ["a", "b", "c"]})
        loop, _ = self.make(lead, max_workers=2)
        report = loop.run(self.tasks())
        self.assertTrue(report["ok"])
        self.assertIn("at most 2", lead.seen[1][1])

    def test_a_lead_that_cannot_answer_falls_back(self):
        class Dead:
            def decide(self, observation, feedback=None):
                raise LeadError("provider down")
        loop, events = self.make(Dead())
        report = loop.run(self.tasks())
        self.assertTrue(report["ok"])
        self.assertEqual(report["lead_fallbacks"], 1)

    def test_abort_stops_the_run_and_nothing_pending_is_started(self):
        started = []
        lead = self.Scripted({"action": "abort", "why": "scope is wrong"})
        loop, events = self.make(lead)
        for n in "abc":
            loop.agents[n] = self.Fn(n, lambda ctx, n=n: started.append(n))
        report = loop.run(self.tasks())
        self.assertFalse(report["ok"])
        self.assertEqual((started, report["aborted"]), ([], "scope is wrong"))
        self.assertEqual(len(events.of_kind("lead_aborted")), 1)

    def test_wait_is_only_valid_while_something_is_running(self):
        gate, started = threading.Event(), []

        def slow(ctx):
            started.append("a")
            gate.wait(2)
        lead = self.Scripted({"action": "start", "tasks": ["a"]}, {"action": "wait"})
        loop, _ = self.make(lead, max_workers=2)
        loop.agents["a"] = self.Fn("a", slow)
        threading.Timer(0.2, gate.set).start()
        report = loop.run([Task("a", "a", "a"), Task("b", "b", "b")])
        self.assertTrue(report["ok"])

    def test_greedy_lead_is_the_baseline(self):
        loop, _ = self.make(GreedyLead())
        self.assertTrue(loop.run(self.tasks())["ok"])

    def test_turn_budget_bounds_the_number_of_lead_calls(self):
        class Stubborn:
            calls = 0
            def decide(self, observation, feedback=None):
                Stubborn.calls += 1
                return {"action": "wait"}  # never starts anything
        loop, _ = self.make(Stubborn())
        report = loop.run(self.tasks())
        self.assertTrue(report["ok"])
        self.assertLessEqual(Stubborn.calls, 6 * 3 + 10 + 2)

    def test_validator_findings_blame_a_role_not_an_id(self):
        """The validator says 'code'; the plan's task is called whatever the lead chose."""
        from agentic.harness.core import ValidationFailure
        runs = {"n": 0}

        def producer(ctx):
            runs["n"] += 1

        def validator(ctx):
            if runs["n"] < 2:
                raise ValidationFailure("bad", [{"check": "x", "severity": "error", "message": "m", "target": "code"}])
        loop, _ = self.make(GreedyLead(), names=["code_generator", "validator"])
        loop.agents["code_generator"] = self.Fn("code_generator", producer)
        loop.agents["validator"] = self.Fn("validator", validator)
        loop.bb.set("requirement", {"plan_type": "greenfield"})
        report = loop.run([Task("build_the_thing", "b", "code_generator"),
                           Task("check_the_thing", "c", "validator", ["build_the_thing"], repair_targets=["build_the_thing"])])
        self.assertTrue(report["ok"])
        self.assertEqual((runs["n"], report["repairs"]), (2, {"check_the_thing": 1}))


class ModelLeadUsage(unittest.TestCase):
    def test_plan_prompt_contains_a_parsable_request_with_catalog_and_limits(self):
        llm = ScriptedLLM()
        lead = ModelLead(llm)
        lead.plan({"plan_type": "greenfield", "raw": GREENFIELD, "problem_statement": "p"}, "template", facts_for(False))
        req = llm.lead_requests[0]
        self.assertEqual((req["kind"], req["plan_type"], req["limits"]["max_tasks"]), ("plan", "greenfield", MAX_TASKS))
        self.assertIn("architect", {c["tool"] for c in req["catalog"]})
        self.assertEqual(req["facts_already_available"], ["input", "requirement"])

    def test_feedback_is_shown_to_the_model_on_a_retry(self):
        llm = ScriptedLLM()
        ModelLead(llm).plan({"plan_type": "greenfield"}, "template", [], feedback="tool 'x' is not available")
        self.assertIn("previous answer was rejected", llm.prompts[-1])
        self.assertIn("tool 'x' is not available", llm.prompts[-1])

    def test_unusable_provider_is_a_lead_error_but_bad_json_is_just_a_bad_answer(self):
        class Down:
            def complete_json(self, *a, **k):
                raise LLMUnavailable("HTTP 401 from the provider")

        class NotJson:
            def complete_json(self, *a, **k):
                raise LLMUnavailable("model reply contained no JSON object")
        with self.assertRaises(LeadError):
            ModelLead(Down()).decide({"ready": []})
        self.assertIn("__invalid__", ModelLead(NotJson()).decide({"ready": []}))


def read_events(out):
    return [json.loads(l) for l in (Path(out) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def renamed_plan(req):
    """The reference plan with every task id changed: the lead, not the harness, picks ids."""
    reply = lead_reply({"plan_type": req["plan_type"], "generator": req["generator"]})
    ids = {t["id"]: "t_" + t["id"] for t in reply["tasks"]}
    for t in reply["tasks"]:
        t["id"], t["deps"] = ids[t["id"]], [ids[d] for d in t["deps"]]
    return reply


class HarnessEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def run_harness(self, llm, **kw):
        return Harness(self.out, echo=None, llm=llm, **kw).run(GREENFIELD)

    def test_a_rejected_plan_is_corrected_using_the_harness_feedback(self):
        llm = ScriptedLLM(bad_plans=1)
        r = self.run_harness(llm)
        self.assertEqual(r.status, "completed")
        kinds = [e["kind"] for e in read_events(self.out)]
        self.assertEqual((kinds.count("plan_rejected"), kinds.count("plan_proposed")), (1, 1))
        plan_prompts = [p for p in llm.prompts if "REQUEST_JSON" in p and '"kind": "plan"' in p]
        self.assertEqual(len(plan_prompts), 2)
        self.assertIn("no_such_tool", plan_prompts[1])  # the second ask quotes what was wrong

    def test_three_bad_plans_stop_the_run_before_any_work(self):
        r = self.run_harness(ScriptedLLM(bad_plans=3))
        self.assertEqual(r.status, "plan_rejected")
        self.assertFalse((self.out / "release").exists())
        self.assertNotIn("task_started", [e["kind"] for e in read_events(self.out)])

    def test_the_lead_chooses_its_own_ids_and_the_defect_is_still_repaired_by_role(self):
        r = self.run_harness(ScriptedLLM(plan=renamed_plan), inject=["defect:code_generator"])
        self.assertEqual(r.status, "completed")
        self.assertEqual(r.report["repairs"], {"t_validate": 1})
        self.assertIn("repair_loop", [e["kind"] for e in read_events(self.out)])
        tasks = json.loads((self.out / "summary.json").read_text())["tasks"]
        self.assertTrue(all(t["id"].startswith("t_") for t in tasks))

    def test_a_plan_without_the_optional_docs_task_still_completes_with_a_warning(self):
        def no_docs(req):
            reply = lead_reply({"plan_type": req["plan_type"], "generator": req["generator"]})
            reply["tasks"] = [t for t in reply["tasks"] if t["tool"] != "doc_writer"]
            for t in reply["tasks"]:
                t["deps"] = [d for d in t["deps"] if d != "gen_docs"]
            return reply
        r = self.run_harness(ScriptedLLM(plan=no_docs))
        self.assertEqual(r.status, "completed")
        self.assertFalse((self.out / "release" / "docs").exists())

    def test_lead_abort_releases_nothing(self):
        r = self.run_harness(ScriptedLLM(decider=lambda req: {"action": "abort", "why": "testing abort"}))
        self.assertEqual(r.status, "failed")
        self.assertFalse((self.out / "release").exists())
        self.assertIn("lead_aborted", [e["kind"] for e in read_events(self.out)])

    def test_a_lead_that_returns_garbage_decisions_cannot_stall_the_run(self):
        r = self.run_harness(ScriptedLLM(decider=lambda req: {"action": "start", "tasks": ["not_a_task"]}))
        self.assertEqual(r.status, "completed")
        self.assertGreaterEqual(r.report["lead_fallbacks"], 1)

    def test_a_lead_whose_provider_dies_mid_run_falls_back_and_completes(self):
        def dead(req):
            raise LLMUnavailable("HTTP 401 from the provider")
        r = self.run_harness(ScriptedLLM(decider=dead))
        self.assertEqual(r.status, "completed")
        self.assertGreaterEqual(r.report["lead_fallbacks"], 1)

    def test_plan_and_decisions_are_in_the_audit_log_and_plan_file(self):
        r = self.run_harness(ScriptedLLM())
        kinds = [e["kind"] for e in read_events(self.out)]
        for k in ("lead_call", "plan_proposed", "lead_decision", "gate_decided"):
            self.assertIn(k, kinds)
        plan = json.loads((self.out / "plan.json").read_text())
        self.assertIn("rationale", plan)
        self.assertEqual(r.report["lead_turns"], kinds.count("lead_decision"))  # every consultation is on the record

    def test_a_human_rejecting_the_plan_stops_before_any_task_runs(self):
        r = Harness(self.out, approval="reject:plan_approval", echo=None, llm=ScriptedLLM()).run(GREENFIELD)
        self.assertEqual(r.status, "rejected_at_plan")
        self.assertNotIn("task_started", [e["kind"] for e in read_events(self.out)])
        self.assertFalse((self.out / "release").exists())

    def test_the_plan_gate_text_shows_the_lead_rationale_and_which_tool_each_task_uses(self):
        tasks, waves = check(reply_for("greenfield"))
        text = Harness._plan_text({"plan_type": "greenfield"}, tasks, waves, "because reasons")
        self.assertIn("[architect]", text)
        self.assertIn("because reasons", text)
        self.assertIn("wave 1: design_architecture", text)


# ====================================================================== change kinds
# Brownfield is not one thing: enhancement, bug fix, refactor, and tests/docs improvements need different tools and
# different proof. One concept (the KIND) drives both: the catalog (what may be planned) and the validator (what is checked).

def change_reply(*specs):
    """specs: (id, tool, deps). Builds a lead reply for a change plan."""
    return {"rationale": "scripted", "tasks": [{"id": i, "tool": t, "title": i, "deps": list(d)} for i, t, d in specs]}


def plan_for(kind):
    summarize = ("summarize", "summarizer", ["validate"])
    if kind in ("refactor",):
        base = [("author", "code_author", []), ("risks", "risk_assessor", ["author"]), ("strategy", "validation_strategist", ["risks"]),
                ("docs", "doc_writer", ["author", "risks"])]
        return change_reply(*base, ("validate", "validator", ["author", "risks", "strategy", "docs"]), summarize)
    if kind in ("bugfix", "enhancement"):
        base = [("author", "code_author", []), ("tests", "spec_tester", ["author"]), ("risks", "risk_assessor", ["author"]),
                ("strategy", "validation_strategist", ["risks"]), ("docs", "doc_writer", ["author", "risks"])]
        return change_reply(*base, ("validate", "validator", ["author", "tests", "risks", "strategy", "docs"]), summarize)
    base = [("tests", "spec_tester", []), ("risks", "risk_assessor", []), ("strategy", "validation_strategist", ["risks"]),
            ("docs", "doc_writer", ["risks"])]
    return change_reply(*base, ("validate", "validator", ["tests", "risks", "strategy", "docs"]), summarize)


class ChangeKindRouting(unittest.TestCase):
    def test_kind_is_detected_from_the_request(self):
        from agentic.steps.analysis import RequirementAnalyst as RA
        cases = {
            "refactor the storage module": "refactor",
            "clean up the create link flow": "refactor",
            "refactor the service and update the docs": "refactor",   # refactor wins over docs
            "fix the bug where stats counts every click as unique": "bugfix",
            "the redirect is broken for deleted links": "bugfix",
            "fix the bug in the docs build": "bugfix",                 # a bug is a bug even if it is in docs tooling
            "improve test coverage for the rate limiter": "tests_docs",
            "update the README and add docstrings": "tests_docs",
            "fix typos in the documentation": "tests_docs",
            "add an optional custom domain to links": "enhancement",
        }
        for text, kind in cases.items():
            self.assertEqual(RA._change_kind(text), kind, text)

    def test_every_shipped_input_exists_and_routes_to_the_kind_its_scenario_expects(self):
        """input/<scenario>/requirement.txt is what `run.py <scenario>` sends. Editing it must not silently change the kind of change."""
        from agentic.steps.analysis import RequirementAnalyst as RA
        root = Path(__file__).resolve().parent.parent / "input"
        expected = {"brownfield": "bugfix", "enhancement": "enhancement", "refactor": "refactor", "tests_improvement": "tests_docs"}
        for name, kind in expected.items():
            text = (root / name / "requirement.txt").read_text(encoding="utf-8").strip()
            self.assertTrue(text, name)
            # the analyser lowercases the text before classifying, so the test does too
            self.assertEqual(RA._change_kind(text.lower()), kind, f"input/{name}/requirement.txt is detected as the wrong kind")
        for name in ("greenfield", "ambiguous"):
            self.assertTrue((root / name / "requirement.txt").read_text(encoding="utf-8").strip(), name)
        self.assertIsInstance(json.loads((root / "ambiguous" / "answers.json").read_text(encoding="utf-8")), dict)

    def test_plan_type_routing(self):
        from agentic.steps.analysis import RequirementAnalyst as RA
        pt = RA._plan_type
        self.assertEqual(pt("url_shortener", None, None), "greenfield")
        self.assertEqual(pt(None, None, None), "analysis")                                   # no repo, unknown domain
        self.assertEqual(pt("url_shortener", "expiry", "repo"), "brownfield_change")        # the one hand-written recipe
        self.assertEqual(pt("url_shortener", "expiry", "repo", use_llm=True), "llm_change")  # model mode prefers the model
        for kind_text in ("a bug fix", "a refactor", "a tests/docs improvement", "an enhancement"):
            self.assertEqual(pt("url_shortener", None, "repo"), "llm_change", kind_text)    # no recipe: the model proposes it
        self.assertEqual(pt("url_shortener", None, "repo", ambiguous=True), "analysis")      # vague: proposal first


class ChangeKindCatalog(unittest.TestCase):
    def tools(self, kind, generator="template"):
        return {t.name for t in available("llm_change", generator, kind)}

    def test_code_author_is_not_offered_for_a_tests_and_docs_change(self):
        self.assertNotIn("code_author", self.tools("tests_docs"))
        self.assertTrue({"spec_tester", "doc_writer", "validator"} <= self.tools("tests_docs"))
        for kind in ("enhancement", "bugfix", "refactor"):
            self.assertIn("code_author", self.tools(kind))

    def test_a_change_always_uses_the_model_backed_tools_even_in_template_mode(self):
        self.assertTrue({"code_author", "spec_tester"} <= self.tools("bugfix", "template"))
        self.assertNotIn("patch_generator", self.tools("bugfix", "template"))

    def test_validator_needs_depend_on_the_kind(self):
        v = TOOLS["validator"]
        self.assertEqual(v.for_plan("needs", "llm_change", "bugfix"), ("code", "tests"))
        self.assertEqual(v.for_plan("needs", "llm_change", "refactor"), ("code",))
        self.assertEqual(v.for_plan("needs", "llm_change", "tests_docs"), ())

    def test_a_refactor_plan_needs_no_new_tests_but_a_bugfix_plan_does(self):
        check(plan_for("refactor"), "llm_change", "template", True, "refactor")
        check(plan_for("bugfix"), "llm_change", "template", True, "bugfix")
        bugfix_without_tests = plan_for("refactor")
        with self.assertRaisesRegex(PlanError, "validator.*needs.*tests|needs \\[.tests.\\]"):
            check(bugfix_without_tests, "llm_change", "template", True, "bugfix")

    def test_a_change_test_author_must_wait_for_the_code_authors_published_contract(self):
        reply = plan_for("bugfix")
        for t in reply["tasks"]:
            if t["id"] == "tests":
                t["deps"] = []  # would run in parallel with the code author, before any contract exists
        with self.assertRaisesRegex(PlanError, "tests.*needs.*change_design"):
            check(reply, "llm_change", "template", True, "bugfix")
        check(plan_for("tests_docs"), "llm_change", "template", True, "tests_docs")  # no code author, so nothing to wait for

    def test_a_tests_and_docs_plan_is_valid_without_any_code_task(self):
        tasks, _ = check(plan_for("tests_docs"), "llm_change", "template", True, "tests_docs")
        self.assertNotIn("code_author", {t.agent for t in tasks})

    def test_tests_docs_plan_that_smuggles_in_a_code_author_is_rejected(self):
        reply = plan_for("tests_docs")
        reply["tasks"].insert(0, {"id": "author", "tool": "code_author", "title": "x", "deps": []})
        reply["tasks"][-2]["deps"].append("author")
        with self.assertRaisesRegex(PlanError, "code_author.*not available"):
            check(reply, "llm_change", "template", True, "tests_docs")

    def test_a_change_plan_must_actually_change_something_that_fits_its_kind(self):
        only_gate = change_reply(("validate", "validator", []), ("summarize", "summarizer", ["validate"]))
        for kind, expect in (("enhancement", "code_author"), ("tests_docs", "spec_tester")):
            with self.assertRaisesRegex(PlanError, f"needs at least one of.*{expect}"):
                check(only_gate, "llm_change", "template", True, kind)

    def test_the_lead_is_told_the_kind_and_only_sees_tools_that_fit_it(self):
        llm = ScriptedLLM()
        ModelLead(llm).plan({"plan_type": "llm_change", "change_kind": "tests_docs", "raw": "x"}, "template", facts_for(True))
        req = llm.lead_requests[0]
        self.assertEqual(req["change_kind"], "tests_docs")
        self.assertNotIn("code_author", {c["tool"] for c in req["catalog"]})


class ChangeKindValidatorRules(unittest.TestCase):
    """The validator holds each kind to its own standard."""

    def make_ctx(self, baseline_api, current_api):
        from types import SimpleNamespace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root, base = Path(tmp.name) / "ws", Path(tmp.name) / "base"
        root.mkdir()
        base.mkdir()
        (base / "openapi.json").write_text(json.dumps(baseline_api), encoding="utf-8")
        (root / "openapi.json").write_text(json.dumps(current_api), encoding="utf-8")
        ws = Workspace(root, baseline_root=base)
        return SimpleNamespace(ws=ws, bb=Blackboard(), events=EventLog(), requirement={"plan_type": "llm_change"})

    API = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"},
           "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}}}
    API_PLUS = {**API, "paths": {**API["paths"], "/b": {"get": {"responses": {"200": {"description": "ok"}}}}}}

    def run_check(self, fn, ctx, *args):
        records = []
        fn(ctx, lambda name, ok, detail, target=None, **kw: records.append((name, ok, target)), *args)
        return {r[0]: r for r in records}

    def test_refactor_must_leave_the_api_identical_even_for_additions(self):
        from agentic.steps.validate import Validator
        same = self.run_check(Validator()._compat_baseline, self.make_ctx(self.API, self.API), "refactor")
        self.assertTrue(same["compat.openapi_unchanged"][1])
        added = self.run_check(Validator()._compat_baseline, self.make_ctx(self.API, self.API_PLUS), "refactor")
        self.assertFalse(added["compat.openapi_unchanged"][1])
        self.assertEqual(added["compat.openapi_unchanged"][2], "code")  # the finding blames the code role

    def test_enhancement_may_add_to_the_api_but_not_break_it(self):
        from agentic.steps.validate import Validator
        ok = self.run_check(Validator()._compat_baseline, self.make_ctx(self.API, self.API_PLUS), "enhancement")
        self.assertTrue(ok["compat.openapi_additive_only"][1])
        broken = self.run_check(Validator()._compat_baseline, self.make_ctx(self.API_PLUS, self.API), "enhancement")
        self.assertFalse(broken["compat.openapi_additive_only"][1])

    def blame_for(self, old_fails, new_fails):
        """Run the validator's test check on a real workspace where the chosen old/new tests fail."""
        from types import SimpleNamespace
        from agentic.steps.validate import Validator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tests = Path(tmp.name) / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        body = lambda fails: "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(%s)\n" % (not fails)
        (tests / "test_old.py").write_text(body(old_fails), encoding="utf-8")
        (tests / "test_change_new.py").write_text(body(new_fails), encoding="utf-8")
        bb = Blackboard()
        bb.set("baseline_tests", {"ran": 1, "ok": True})
        ctx = SimpleNamespace(ws=Workspace(Path(tmp.name)), bb=bb, events=EventLog(), task=SimpleNamespace(generation=0),
                              requirement={"plan_type": "llm_change", "generator": "template"})
        records = []
        Validator()._tests(ctx, lambda name, ok, detail, target=None, **kw: records.append((name, ok, target)), "code", "llm_change", "bugfix")
        return {r[0]: r[2] for r in records}

    def test_a_failing_new_test_blames_both_sides_a_failing_old_test_blames_the_code(self):
        """A new test written from the contract can fail because the test is wrong OR because the code does not meet its
        contract. The harness cannot tell which, so both are sent the failure; an old test failing is the code's fault."""
        self.assertEqual(self.blame_for(old_fails=False, new_fails=True)["tests.suite"], ["code", "tests"])
        self.assertEqual(self.blame_for(old_fails=True, new_fails=False)["tests.suite"], "code")
        self.assertEqual(self.blame_for(old_fails=True, new_fails=True)["tests.suite"], ["code", "tests"])

    def test_the_regression_checks_use_the_same_blame_so_signals_do_not_conflict(self):
        blame = self.blame_for(old_fails=False, new_fails=True)
        self.assertEqual(blame["regression.suite_green_after_fix"], ["code", "tests"])

    def test_tests_docs_change_may_not_touch_production_files(self):
        from agentic.harness.guardrails import ManifestEntry
        from agentic.steps.validate import Validator
        ctx = self.make_ctx(self.API, self.API)
        entry = lambda rel, producer: ManifestEntry(rel, "code", 1, "h", producer)
        ctx.ws.manifest.update({"shortener/app.py": entry("shortener/app.py", "baseline"),   # imported, not changed
                                "tests/test_change_x.py": entry("tests/test_change_x.py", "spec_tests"),
                                "docs/CHANGE.md": entry("docs/CHANGE.md", "docs")})
        ok = self.run_check(Validator()._scope_tests_docs_only, ctx)
        self.assertTrue(ok["scope.tests_docs_only"][1])
        ctx.ws.manifest["shortener/service.py"] = entry("shortener/service.py", "sneaky_task")
        bad = self.run_check(Validator()._scope_tests_docs_only, ctx)
        self.assertFalse(bad["scope.tests_docs_only"][1])


class ChangeTestPromptTellsTheTruth(unittest.TestCase):
    """The test author is only told about helpers the codebase really has. (A live run failed because it was told to use a
    FakeClock that a model-written service does not contain, so the model built elaborate workarounds.)"""

    def prompt_for(self, files, after=None):
        from types import SimpleNamespace
        from agentic.authoring_agents import SpecTester
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name) / "base"
        (base / "tests").mkdir(parents=True)
        (base / "openapi.json").write_text("{}", encoding="utf-8")
        for rel, text in files.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(text, encoding="utf-8")
        seen = []

        class Llm:
            name, usage = "stub", {}

            def complete_json(self, system, prompt, max_tokens=0):
                seen.append(prompt)
                body = "import unittest\n\nclass T(unittest.TestCase):\n" + "".join(f"    def test_{i}(self):\n        pass\n" for i in range(6))
                return {"files": [{"path": "tests/test_change_x.py", "content": body}]}
        bb = Blackboard()
        if after is not None:  # the code author has finished and published its contract
            bb.set("change_design", {"summary": "x"})
        ctx = SimpleNamespace(ws=SimpleNamespace(baseline_root=base, read_baseline=lambda rel: (base / rel).read_text(encoding="utf-8"),
                                                 read=lambda rel: after, exists=lambda rel: after is not None,
                                                 revert_baseline_tests=lambda: []),
                              bb=bb, events=EventLog(), llm=Llm(), feedback=[], task=SimpleNamespace(id="t"),
                              requirement={"raw": "x", "acceptance_criteria": ["AC-1 something"], "change_kind": "tests_docs"},
                              write=lambda *a, **k: None)
        SpecTester()._write_change_tests(ctx)
        return seen[0]

    def test_the_test_author_is_shown_the_contract_the_code_author_published(self):
        files = {"tests/wsgi_client.py": "class Client:\n    pass\n", "tests/test_core.py": "# existing"}
        after = prompt = self.prompt_for(files, after='{"paths": {}, "marker": "AFTER-CONTRACT-MARKER"}')
        self.assertIn("OpenAPI AFTER the change", after)
        self.assertIn("AFTER-CONTRACT-MARKER", after)
        self.assertIn("exactly as the AFTER contract declares them", after)
        before_only = self.prompt_for(files)  # no published contract yet (for example a tests-only change)
        self.assertNotIn("OpenAPI AFTER the change", before_only)

    def test_a_service_that_exposes_create_app_has_its_interface_shown_to_the_test_author(self):
        files = {"tests/wsgi_client.py": "class Client:\n    pass\n", "tests/test_core.py": "# existing",
                 "shortener/app.py": "def create_app(db_path=\":memory:\", *, api_keys=()):\n    pass\n"}
        p = self.prompt_for(files)
        self.assertIn("Service interface", p)
        self.assertIn("authentication is OFF unless api_keys is non-empty", p)
        self.assertIn("create_app(", p)
        other = self.prompt_for({"tests/helpers.py": "class FakeClock:\n    pass\n", "tests/test_api.py": "# existing"})
        self.assertNotIn("Service interface", other)  # a codebase without that interface is not told about one

    def test_the_test_author_is_told_not_to_pin_wording_the_contract_does_not_fix(self):
        """A live run failed three repair rounds on tests asserting an exact error message and response description."""
        p = self.prompt_for({"tests/helpers.py": "class FakeClock:\n    pass\n", "tests/test_api.py": "# existing"})
        self.assertIn("NEVER assert on human-readable prose", p)
        self.assertIn("machine-readable error `code`", p)

    def test_a_codebase_with_a_fake_clock_helper_is_told_to_use_it(self):
        p = self.prompt_for({"tests/helpers.py": "class FakeClock:\n    pass\n", "tests/test_api.py": "# existing"})
        self.assertIn("tests/helpers.py", p)
        self.assertIn("use the FakeClock helper", p)
        self.assertNotIn("NO injectable clock", p)

    def test_a_codebase_without_one_is_not_told_to_use_a_clock_that_does_not_exist(self):
        p = self.prompt_for({"tests/wsgi_client.py": "class Client:\n    pass\n", "tests/test_core.py": "# existing"})
        self.assertIn("tests/wsgi_client.py", p)
        self.assertIn("from .wsgi_client import", p)
        self.assertNotIn("use the FakeClock helper", p)
        self.assertIn("NO injectable clock", p)
        self.assertIn("change the stored timestamps with SQL", p)
        self.assertIn("# existing", p)  # an example test from the codebase is included even without test_api.py


class CodeAuthorSeesWhatItMustChange(unittest.TestCase):
    """A live brownfield run failed because the impact analysis found nothing in a model-written service (it recognises
    decorator routes and SQL literals only), so the code author was shown one file of fifteen and could only invent new files."""

    FILES = {"shortener/app.py": "APP-SOURCE", "shortener/service.py": "SERVICE-SOURCE", "shortener/storage.py": "STORAGE-SOURCE",
             "shortener/models.py": "MODELS-SOURCE", "shortener/cache.py": "CACHE-SOURCE", "openapi.json": "{}", "schema.sql": "-- sql"}

    def prompt_for(self, impact):
        from types import SimpleNamespace
        from agentic.harness.core import ValidationFailure
        from agentic.authoring_agents import CodeAuthor
        seen = []

        class Llm:
            name, usage = "stub", {}

            def complete_json(self, system, prompt, max_tokens=0):
                seen.append(prompt)
                return {"edits": [], "new_files": []}  # rejected after the prompt is built: only the prompt matters here
        bb = Blackboard()
        bb.set("impact", impact)
        ctx = SimpleNamespace(ws=SimpleNamespace(list_files=lambda: sorted(self.FILES), read_baseline=lambda rel: self.FILES[rel]),
                              bb=bb, events=EventLog(), llm=Llm(), feedback=[], task=SimpleNamespace(id="t"),
                              requirement={"raw": "add a ttl", "change_kind": "enhancement"})
        with self.assertRaises(ValidationFailure):
            CodeAuthor()._propose_change(ctx)
        return seen[0]

    def test_when_the_analysis_finds_nothing_the_author_is_shown_every_module(self):
        p = self.prompt_for({"routes": [], "tables": [], "modules": ["shortener/models.py"]})
        for marker in ("APP-SOURCE", "SERVICE-SOURCE", "STORAGE-SOURCE", "MODELS-SOURCE", "CACHE-SOURCE"):
            self.assertIn(marker, p)
        self.assertIn("recognised no routes or tables", p)

    def test_when_the_analysis_finds_the_impact_only_those_modules_are_shown(self):
        p = self.prompt_for({"routes": ["POST /api/v1/links"], "tables": ["links"], "modules": ["shortener/app.py"]})
        self.assertIn("APP-SOURCE", p)
        self.assertNotIn("CACHE-SOURCE", p)
        self.assertNotIn("recognised no routes or tables", p)

    def test_the_most_important_modules_come_first_when_the_budget_is_tight(self):
        p = self.prompt_for({"routes": [], "tables": [], "modules": []})
        self.assertLess(p.index("APP-SOURCE"), p.index("CACHE-SOURCE"))
        self.assertLess(p.index("STORAGE-SOURCE"), p.index("CACHE-SOURCE"))


class ChangeKindEndToEnd(unittest.TestCase):
    """One real run per kind on a real generated codebase (scripted lead and model, real sandbox and validator)."""
    REQUESTS = {"refactor": "Refactor the create link flow to tidy up the service module without changing behaviour.",
                "bugfix": "Fix the bug in create link alias handling so duplicate aliases are reported clearly.",
                "tests_docs": "Improve test coverage for the create link flow and update the documentation."}

    @classmethod
    def setUpClass(cls):
        from tests.test_e2e import ensure_baseline
        cls.repo = ensure_baseline()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def run_kind(self, kind):
        llm = ScriptedLLM(plan=lambda req: plan_for(kind))
        r = Harness(self.out, echo=None, llm=llm).run(self.REQUESTS[kind], target_repo=str(self.repo))
        checks = {c["check"]: c["status"] for c in json.loads((self.out / "state.json").read_text())["blackboard"]
                  .get("validation_report", {}).get("checks", [])}
        return r, checks, llm

    def test_refactor_completes_with_identical_api_and_no_new_tests_required(self):
        r, checks, _ = self.run_kind("refactor")
        self.assertEqual(r.status, "completed", checks)
        self.assertEqual(checks["compat.openapi_unchanged"], "PASS")
        self.assertEqual(checks["regression.suite_not_smaller"], "PASS")
        self.assertNotIn("regression.new_tests_added", checks)
        # the template baseline has no create_app interface, so the black-box probe does not apply; the baseline suite is the proof
        self.assertEqual(checks["probe.behaviour"], "SKIP")
        self.assertEqual(checks["regression.baseline_green"], "PASS")

    def test_bugfix_must_come_with_a_new_regression_test_that_fails_on_the_old_code(self):
        from scripts.seed_bug import seed
        buggy = Path(self.tmp.name) / "buggy"
        seed(self.repo, buggy)  # a real defect: the baseline suite starts RED
        llm = ScriptedLLM(plan=lambda req: plan_for("bugfix"), real_bugfix=True)
        r = Harness(self.out, echo=None, llm=llm).run(
            "Fix the bug where link stats report unique_visitors equal to the total number of clicks.", target_repo=str(buggy))
        checks = {c["check"]: c["status"] for c in json.loads((self.out / "state.json").read_text())["blackboard"]
                  .get("validation_report", {}).get("checks", [])}
        self.assertEqual(r.status, "completed", checks)
        self.assertEqual(checks["regression.new_tests_added"], "PASS")
        self.assertEqual(checks["regression.suite_green_after_fix"], "PASS")
        self.assertEqual(checks["regression.fails_on_old_code"], "PASS")
        self.assertNotIn("regression.baseline_green", checks)  # a bug fix may start from a red suite
        test_prompt = next(p for p in llm.prompts if "Write NEW unittest tests" in p)
        self.assertIn("regression test that demonstrates the defect", test_prompt)

    def test_tests_and_docs_change_writes_no_production_code(self):
        r, checks, llm = self.run_kind("tests_docs")
        self.assertEqual(r.status, "completed", checks)
        self.assertEqual(checks["scope.tests_docs_only"], "PASS")
        self.assertEqual(checks["compat.openapi_unchanged"], "PASS")
        self.assertFalse(any("Modify an EXISTING codebase" in p for p in llm.prompts))  # the code author never ran
        self.assertTrue((self.out / "release" / "tests" / "test_change_existing_behaviour.py").exists())

    def test_the_model_is_told_which_kind_of_change_it_is_making(self):
        _, _, llm = self.run_kind("refactor")
        prompt = next(p for p in llm.prompts if "Modify an EXISTING codebase" in p)
        self.assertIn("Kind of change: refactor", prompt)
        self.assertIn("Behaviour-preserving restructuring ONLY", prompt)


if __name__ == "__main__":
    unittest.main()
