"""Two policies about tests that guard brownfield changes.

1. Approved test updates. Agents may not edit a test they did not write. The one exception is an ENHANCEMENT whose contract
   change makes an existing test obsolete (for example a test pinning the exact fields of a request). The amendment must be
   justified, no weaker than the original, and approved by a human at the `test_update_approval` gate.
2. Bug-fix proof. New regression tests must FAIL on the old (buggy) code, otherwise they prove nothing.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agentic.harness.core import Blackboard, Context, EventLog, GuardrailViolation, Task, ValidationFailure
from agentic.harness.guardrails import Workspace, check_test_amendment
from agentic.harness.harness import Harness
from agentic.authoring_agents import SpecTester
from agentic.steps.validate import Validator
from tests.fake_llm import PINNED_FIND, PINNED_REPLACE, PINNED_TEST_FILE, ScriptedLLM
from tests.test_e2e import BROWNFIELD, ensure_baseline
from tests.test_harness import plan_for

OLD_TEST = ("import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(1, 1)\n"
            "    def test_b(self):\n        self.assertTrue(True)\n")
API = {"paths": {"/x": {"get": {"responses": {"200": {}}}}}, "components": {"schemas": {"R": {"properties": {"a": {"type": "string"}}}}}}
API_PLUS = {"paths": API["paths"], "components": {"schemas": {"R": {"properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}}}}


class Sandbox(unittest.TestCase):
    """A workspace with a pristine baseline (tests/test_old.py + openapi.json) copied in, as a brownfield run does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = root / "src"
        (self.src / "tests").mkdir(parents=True)
        (self.src / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.src / "tests" / "test_old.py").write_text(OLD_TEST, encoding="utf-8")
        self.ws = Workspace(root / "ws", baseline_root=root / "base")
        self.ws.import_tree(self.src)

    def set_contract(self, new_spec, old_spec=API):
        (Path(self.ws.baseline_root) / "openapi.json").write_text(json.dumps(old_spec), encoding="utf-8")
        self.ws.write("openapi.json", json.dumps(new_spec), kind="contract", producer="code_author")

    def ctx(self, kind="enhancement", agent="spec_tester", updates=None):
        bb = Blackboard()
        bb.set("requirement", {"plan_type": "llm_change", "generator": "llm", "change_kind": kind})
        if updates is not None:
            bb.set("test_updates", updates)
        return Context(Task("t1", "tests", agent), bb, self.ws, EventLog(), None, None)


class WorkspaceRules(Sandbox):
    def test_only_an_existing_baseline_test_module_can_be_replaced(self):
        for rel in ("tests/test_new.py", "tests/test_change_x.py", "shortener/app.py", "../escape.py", "tests/sub/test_old.py"):
            with self.subTest(rel=rel), self.assertRaises(GuardrailViolation):
                self.ws.write_test_update(rel, OLD_TEST, producer="t")
        self.ws.write_test_update("tests/test_old.py", OLD_TEST + "# edited\n", producer="t")
        self.assertEqual(self.ws.modified_baseline_tests(), ["tests/test_old.py"])

    def test_modification_is_detected_from_disk_even_when_written_around_the_api(self):
        self.assertEqual(self.ws.modified_baseline_tests(), [])
        (self.ws.root / "tests" / "test_old.py").write_text("# sneaky\n", encoding="utf-8")  # no Workspace.write
        self.assertEqual(self.ws.modified_baseline_tests(), ["tests/test_old.py"])
        (self.ws.root / "tests" / "test_old.py").unlink()  # deleting a protected test is a modification too
        self.assertEqual(self.ws.modified_baseline_tests(), ["tests/test_old.py"])

    def test_revert_puts_the_pristine_tests_back(self):
        self.ws.write_test_update("tests/test_old.py", "# x\n", producer="t")
        self.assertEqual(self.ws.revert_baseline_tests(), ["tests/test_old.py"])
        self.assertEqual(self.ws.modified_baseline_tests(), [])
        self.assertEqual(self.ws.manifest["tests/test_old.py"].kind, "baseline")


class AmendmentMustNotWeaken(unittest.TestCase):
    def check(self, new):
        return check_test_amendment(OLD_TEST, new, "tests/test_old.py")

    def test_correcting_an_expected_value_is_fine(self):
        self.assertEqual(self.check(OLD_TEST.replace("assertEqual(1, 1)", "assertEqual(1, 1)  # ok\n        self.assertEqual(2, 2)")), [])

    def test_removing_a_test_or_an_assertion_or_disabling_it_is_refused(self):
        self.assertTrue(any("removes test" in p for p in self.check(OLD_TEST.replace("def test_b", "def other"))))
        self.assertTrue(any("assertions" in p for p in self.check(OLD_TEST.replace("self.assertTrue(True)", "pass"))))
        skipped = OLD_TEST.replace("    def test_b", "    @unittest.skip('later')\n    def test_b")
        self.assertTrue(any("disables" in p for p in self.check(skipped)))
        self.assertTrue(any("SyntaxError" in p for p in self.check("def broken(:\n")))


class WhoMayAmend(Sandbox):
    def test_an_enhancement_spec_tester_may_amend_and_it_is_recorded_for_the_human(self):
        ctx = self.ctx()
        ctx.update_existing_test("tests/test_old.py", OLD_TEST + "        self.assertTrue(True)\n", "contract grew")
        rec = ctx.bb.get("test_updates")
        self.assertEqual([u["path"] for u in rec], ["tests/test_old.py"])
        self.assertIn("+        self.assertTrue(True)", rec[0]["diff"])
        self.assertEqual(rec[0]["reason"], "contract grew")

    def test_other_kinds_and_other_agents_may_not(self):
        for kind in ("bugfix", "refactor", "tests_docs"):
            with self.subTest(kind=kind), self.assertRaises(GuardrailViolation):
                self.ctx(kind).update_existing_test("tests/test_old.py", OLD_TEST + "#\n", "r")
        for agent in ("code_author", "doc_writer", "validator"):
            with self.subTest(agent=agent), self.assertRaises(GuardrailViolation):
                self.ctx("enhancement", agent).update_existing_test("tests/test_old.py", OLD_TEST + "#\n", "r")
        self.assertEqual(self.ws.modified_baseline_tests(), [])


class ValidatorEnforcesThePolicy(Sandbox):
    def check(self, kind="enhancement", updates=None):
        records = []
        Validator()._test_amendments(self.ctx(kind, updates=updates), lambda n, ok, d, t=None, **kw: records.append((n, ok, d)), kind)
        return records[0]

    def amend(self, content=OLD_TEST + "        self.assertTrue(True)\n"):
        (self.ws.root / "tests" / "test_old.py").write_text(content, encoding="utf-8")

    def test_untouched_tests_pass(self):
        self.assertTrue(self.check()[1])

    def test_a_justified_recorded_amendment_in_an_enhancement_with_a_contract_change_passes(self):
        self.set_contract(API_PLUS)
        self.amend()
        name, ok, detail = self.check(updates=[{"path": "tests/test_old.py", "reason": "new field"}])
        self.assertTrue(ok, detail)

    def test_an_amendment_nobody_recorded_is_a_violation(self):
        self.set_contract(API_PLUS)
        self.amend()
        self.assertIn("without a recorded", self.check(updates=[])[2])

    def test_no_contract_change_means_no_reason_to_edit_a_test(self):
        self.set_contract(API)
        self.amend()
        self.assertIn("did not", self.check(updates=[{"path": "tests/test_old.py", "reason": "r"}])[2])

    def test_a_refactor_or_bugfix_may_not_modify_existing_tests_at_all(self):
        self.set_contract(API_PLUS)
        self.amend()
        for kind in ("refactor", "bugfix", "tests_docs"):
            with self.subTest(kind=kind):
                ok, detail = self.check(kind, [{"path": "tests/test_old.py", "reason": "r"}])[1:]
                self.assertFalse(ok)
                self.assertIn("may not modify existing tests", detail)

    def test_a_weakening_amendment_is_a_violation_even_if_recorded(self):
        self.set_contract(API_PLUS)
        self.amend("import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n    def test_b(self):\n        pass\n")
        ok, detail = self.check(updates=[{"path": "tests/test_old.py", "reason": "r"}])[1:]
        self.assertFalse(ok)
        self.assertIn("weaken", detail)

    def test_an_obsolete_test_is_blamed_on_the_tests_too_but_only_when_the_contract_changed(self):
        self.assertFalse(Validator._contract_changed(self.ctx()))
        self.set_contract(API_PLUS)
        self.assertTrue(Validator._contract_changed(self.ctx()))


class SpecTesterProposals(Sandbox):
    def plan(self, raw):
        return SpecTester()._plan_test_updates(self.ctx(), raw)

    def test_a_good_proposal_becomes_new_content_and_reason(self):
        out = self.plan([{"path": "tests/test_old.py", "find": "assertEqual(1, 1)", "replace": "assertEqual(1, 1)\n        self.assertTrue(1)",
                          "reason": "why"}])
        self.assertEqual(out[0][0], "tests/test_old.py")
        self.assertIn("self.assertTrue(1)", out[0][1])
        self.assertEqual(out[0][2], "why")

    def test_bad_proposals_are_rejected_with_feedback(self):
        edit = {"path": "tests/test_old.py", "find": "assertEqual(1, 1)", "replace": "assertEqual(1, 1)", "reason": "r"}
        bad = [[{**edit, "reason": ""}], [{**edit, "find": "nonexistent"}], [{**edit, "path": "tests/test_missing.py"}],
               [{**edit, "find": "self"}],  # matches more than once
               [dict(edit) for _ in range(6)], "not a list", [{**edit, "replace": "pass"}]]  # last one weakens
        for raw in bad:
            with self.subTest(raw=str(raw)[:60]), self.assertRaises(ValidationFailure):
                self.plan(raw)

    def test_no_proposal_is_not_an_error(self):
        self.assertEqual(self.plan(None), [])
        self.assertEqual(self.plan([]), [])


class RegressionTestMustFailOnOldCode(unittest.TestCase):
    """validate.py: regression.fails_on_old_code runs the new tests against a copy of the pristine baseline."""
    BASE = "def total(items):\n    return len(items)  # BUG: should be the sum\n"
    FIXED = "def total(items):\n    return sum(items)\n"
    CATCHES = "import unittest\nfrom calc import total\n\nclass T(unittest.TestCase):\n    def test_sum(self):\n        self.assertEqual(total([1, 2]), 3)\n"
    NOT_BUG = "import unittest\nfrom calc import total\n\nclass T(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(total([]), 0)\n"
    BROKEN = "import unittest\nfrom calc import no_such_function\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n"

    def run_check(self, new_test):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src"
        (src / "tests").mkdir(parents=True)
        (src / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (src / "calc.py").write_text(self.BASE, encoding="utf-8")
        ws = Workspace(Path(tmp.name) / "ws", baseline_root=Path(tmp.name) / "base")
        ws.import_tree(src)
        ws.write("calc.py", self.FIXED, kind="code", producer="code_author")
        ws.write("tests/test_change_calc.py", new_test, kind="test", producer="spec_tester")
        ctx = SimpleNamespace(ws=ws, bb=Blackboard(), events=EventLog())
        records = []
        Validator()._regression_proves_defect(ctx, lambda n, ok, d, t=None, **kw: records.append((n, ok, d, t)))
        return records[0], ctx

    def test_a_test_that_fails_on_the_buggy_code_proves_the_defect(self):
        (name, ok, detail, _), ctx = self.run_check(self.CATCHES)
        self.assertEqual(name, "regression.fails_on_old_code")
        self.assertTrue(ok, detail)
        self.assertEqual(ctx.bb.get("old_code_run")["failing_new_tests"], 1)

    def test_a_test_that_also_passes_on_the_buggy_code_proves_nothing(self):
        (_, ok, detail, target), _ = self.run_check(self.NOT_BUG)
        self.assertFalse(ok)
        self.assertEqual(target, "tests")
        self.assertIn("PASSES on the old", detail)

    def test_a_test_that_cannot_even_load_on_the_old_code_is_not_proof_either(self):
        (_, ok, detail, _), _ = self.run_check(self.BROKEN)
        self.assertFalse(ok)
        self.assertIn("could not even load", detail)


class AmendmentEndToEnd(unittest.TestCase):
    """A real harness run (scripted model, real sandbox, validator and gates) on a baseline with a test that pins the
    exact request fields. The enhancement adds a field, so that test is superseded."""

    @classmethod
    def setUpClass(cls):
        cls.template = ensure_baseline()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        shutil.copytree(self.template, self.repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.repo / "tests" / "test_pinned_contract.py").write_text(PINNED_TEST_FILE, encoding="utf-8")
        self.out = Path(self.tmp.name) / "run"

    def run_it(self, approval="auto", **model):
        llm = ScriptedLLM(plan=lambda req: plan_for("enhancement"), amend_openapi=True, **model)
        r = Harness(self.out, echo=None, llm=llm, generator="llm", approval=approval).run(BROWNFIELD, target_repo=str(self.repo))
        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        checks = {c["check"]: c["status"] for c in (state["blackboard"].get("validation_report") or {}).get("checks", [])}
        gates = [g["gate"] for g in json.loads((self.out / "summary.json").read_text(encoding="utf-8"))["gates"]]
        return r, state["blackboard"], checks, gates

    def test_the_obsolete_test_is_amended_after_a_failure_a_human_approves_and_the_run_completes(self):
        r, bb, checks, gates = self.run_it()
        self.assertEqual(r.status, "completed", checks)
        self.assertEqual(checks["tests.existing_tests_protected"], "PASS")
        self.assertEqual(checks["compat.openapi_additive_only"], "PASS")
        self.assertGreaterEqual(sum(r.report["repairs"].values()), 1)  # first the suite went red, then the policy applied
        self.assertEqual([u["path"] for u in bb["test_updates"]], ["tests/test_pinned_contract.py"])
        self.assertEqual(gates, ["plan_approval", "test_update_approval", "release_approval"])
        released = (self.out / "release" / "tests" / "test_pinned_contract.py").read_text(encoding="utf-8")
        self.assertIn(PINNED_REPLACE, released)
        self.assertNotIn(PINNED_FIND, released)
        # the original repository is never touched
        self.assertIn(PINNED_FIND, (self.repo / "tests" / "test_pinned_contract.py").read_text(encoding="utf-8"))
        summary = (self.out / "ENGINEERING_SUMMARY.md").read_text(encoding="utf-8")
        self.assertIn("Existing tests amended", summary)
        self.assertIn("test_pinned_contract.py", summary)

    def test_a_human_who_rejects_the_amendment_blocks_the_release(self):
        r, _, _, gates = self.run_it(approval="reject:test_update_approval")
        self.assertEqual(r.status, "rejected_at_release")
        self.assertEqual(gates[-1], "test_update_approval")
        self.assertFalse((self.out / "release").exists())

    def test_without_the_policy_path_the_run_cannot_pass_the_obsolete_test(self):
        r, _, checks, _ = self.run_it(test_updates="none")
        self.assertEqual(r.status, "failed")
        self.assertFalse((self.out / "release").exists())

    def test_an_amendment_that_weakens_the_test_is_rejected_and_nothing_is_released(self):
        r, bb, _, _ = self.run_it(test_updates="weaken")
        self.assertEqual(r.status, "failed")
        self.assertFalse((self.out / "release").exists())
        self.assertFalse(bb.get("test_updates"))


class BugfixEndToEnd(unittest.TestCase):
    """A real bug (scripts/seed_bug.py) fixed by the scripted model. The regression test must fail on the buggy baseline."""
    REQUEST = "Fix the bug where link stats report unique_visitors equal to the total number of clicks."

    @classmethod
    def setUpClass(cls):
        cls.template = ensure_baseline()

    def setUp(self):
        from scripts.seed_bug import seed
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "buggy"
        seed(self.template, self.repo)
        self.out = Path(self.tmp.name) / "run"

    def run_it(self, **model):
        llm = ScriptedLLM(plan=lambda req: plan_for("bugfix"), real_bugfix=True, **model)
        r = Harness(self.out, echo=None, llm=llm).run(self.REQUEST, target_repo=str(self.repo))
        state = json.loads((self.out / "state.json").read_text(encoding="utf-8"))
        checks = {c["check"]: c["status"] for c in (state["blackboard"].get("validation_report") or {}).get("checks", [])}
        return r, state["blackboard"], checks

    def test_a_regression_test_that_fails_on_the_old_code_and_passes_on_the_fix_completes(self):
        r, bb, checks = self.run_it()
        self.assertEqual(r.status, "completed", checks)
        self.assertEqual(checks["regression.fails_on_old_code"], "PASS")
        self.assertEqual(checks["regression.suite_green_after_fix"], "PASS")
        self.assertFalse(bb["baseline_tests"]["ok"])  # the seeded bug turned the baseline red
        self.assertGreaterEqual(bb["old_code_run"]["failing_new_tests"], 1)
        self.assertIn("Bug-fix proof", (self.out / "ENGINEERING_SUMMARY.md").read_text(encoding="utf-8"))

    def test_tests_that_pass_on_the_buggy_code_are_sent_back_and_the_run_cannot_complete(self):
        r, bb, checks = self.run_it(bugfix_tests="weak")
        self.assertEqual(r.status, "failed")
        self.assertEqual(checks["regression.fails_on_old_code"], "FAIL")
        self.assertFalse((self.out / "release").exists())


if __name__ == "__main__":
    unittest.main()
