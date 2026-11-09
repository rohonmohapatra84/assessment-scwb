"""The run story (RUN_STORY.md): a plain-English account of a run, written live from the event log.

It must (1) explain the scenario setup, (2) say which stage we are in and who acts (an AGENT that is a model, or a plain-Python step),
(3) quote the real change the code agent made, (4) explain each validation check, and (5) never be able to break a run.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agentic.harness.core import Blackboard, EventLog
from agentic.harness.guardrails import Workspace
from agentic.harness.harness import Harness
from agentic.harness.narrator import CHECK_MEANING, STATUS_MEANING, Narrator
from tests.fake_llm import ScriptedLLM, PINNED_TEST_FILE
from tests.test_e2e import BROWNFIELD, ensure_baseline
from tests.test_harness import plan_for

PREFACE = ("## Stage 0: Scenario setup (done by the demo script BEFORE any agent runs)\n\n"
           "2. **Planted ONE bug** in `shortener/storage.py` at line 136:\n   - before (correct): `COUNT(DISTINCT visitor_hash)`\n"
           "   - after (buggy):    `COUNT(visitor_hash)`\n")


class NarratorUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "RUN_STORY.md"
        self.bb = Blackboard()
        self.ws = Workspace(Path(self.tmp.name) / "ws")
        self.story = Narrator(self.path, bb=self.bb, ws=self.ws, preface=PREFACE)

    def feed(self, kind, **fields):
        self.story.on_event({"ts": time.time(), "kind": kind, **fields})

    def text(self):
        return self.path.read_text(encoding="utf-8")

    def test_the_opening_names_the_request_the_mode_and_the_setup(self):
        self.feed("run_started", requirement="Fix the bug", target_repo="/some/repo", llm="azure", approval="auto")
        t = self.text()
        for needle in ("# Run story", "Fix the bug", "/some/repo", "azure", "AGENT (model)", "step (plain Python)", "Stage 0", "line 136",
                       "COUNT(visitor_hash)", "Stage 1: Discover"):
            self.assertIn(needle, t)

    def test_a_red_baseline_is_explained_as_the_symptom_of_the_bug(self):
        self.bb.set("baseline_tests", {"ran": 69, "ok": False, "failing": ["test_unique (tests.test_links.X)"]})
        self.feed("baseline_tests", ran=69, ok=False, failing=1)
        t = self.text()
        self.assertIn("RED", t)
        self.assertIn("symptom of the bug", t)
        self.assertIn("test_unique", t)

    def test_models_and_steps_are_told_apart_and_a_rerun_is_explained(self):
        self.feed("task_started", task="fix", agent="code_author", generation=0, concurrent=1)
        self.feed("task_started", task="risks", agent="risk_assessor", generation=0, concurrent=1)
        self.feed("task_started", task="fix", agent="code_author", generation=1, concurrent=1)
        t = self.text()
        self.assertIn("**AGENT (model) `code_author`**", t)
        self.assertIn("Step `risk_assessor` (plain Python, no model)", t)
        self.assertIn("re-run (generation 1)", t)
        self.assertIn("validator's exact findings", t)

    def test_when_the_code_agent_finishes_the_real_change_is_quoted(self):
        self.feed("task_started", task="fix", agent="code_author", generation=0, concurrent=1)
        self.ws.write("changes.diff", "--- a/shortener/storage.py\n+++ b/shortener/storage.py\n@@ -1 +1 @@\n-COUNT(visitor_hash)\n+COUNT(DISTINCT visitor_hash)\n",
                      kind="diff", producer="fix")
        self.bb.set("patch_stats", {"files": ["shortener/storage.py"], "added": 1, "removed": 1, "edits": 1})
        self.bb.set("change_design", {"summary": "count distinct visitors"})
        self.feed("task_succeeded", task="fix", attempts=1, seconds=3.0)
        t = self.text()
        self.assertIn("```diff", t)
        self.assertIn("+COUNT(DISTINCT visitor_hash)", t)
        self.assertIn("count distinct visitors", t)
        self.assertIn("anchored edit", t)

    def test_validation_checks_are_explained_and_the_proof_is_called_out(self):
        self.feed("task_started", task="validate", agent="validator", generation=0, concurrent=1)
        self.feed("validation_check", check="regression.fails_on_old_code", status="PASS", detail="10 new tests fail on the unfixed baseline")
        self.feed("validation_check", check="tests.suite", status="FAIL", detail="87 ran; failing: ['x']\nmore")
        t = self.text()
        self.assertIn("Stage 4: Validate", t)
        self.assertIn("THE PROOF", t)
        self.assertIn("**FAIL** `tests.suite`", t)
        self.assertIn("Evidence: 87 ran", t)

    def test_a_repair_is_described_as_routed_to_whoever_is_responsible(self):
        self.feed("repair_loop", validator="validate", round=1, targets="fix,tests", findings=4)
        t = self.text()
        self.assertIn("repair round 1", t)
        self.assertIn("not just retried blindly", t)

    def test_the_result_section_explains_the_status(self):
        self.bb.set("old_code_run", {"ran": 78, "failing_new_tests": 10})
        self.bb.set("test_run", {"ran": 78, "ok": True})
        self.bb.set("baseline_tests", {"ran": 69, "ok": False})
        self.feed("run_finished", status="completed", seconds=12.0)
        t = self.text()
        self.assertIn("## Result", t)
        self.assertIn("DONE", t)
        self.assertIn("Bug-fix proof", t)
        self.assertIn("10 of the new tests FAILED on the original buggy code", t)

    def test_every_check_and_status_the_system_can_produce_has_an_explanation(self):
        from agentic.harness.loop import Loop  # noqa: F401  (import check only)
        for status in ("completed", "completed_degraded", "needs_clarification", "rejected_at_plan", "rejected_at_release", "plan_rejected", "failed"):
            self.assertIn(status, STATUS_MEANING)
        for check in ("static.syntax", "tests.suite", "regression.fails_on_old_code", "compat.openapi_additive_only", "tests.existing_tests_protected",
                      "scope.tests_docs_only", "probe.behaviour"):
            self.assertIn(check, CHECK_MEANING)

    def test_a_listener_that_raises_can_never_break_the_run(self):
        log = EventLog()
        log.add_listener(lambda e: 1 / 0)
        seen = []
        log.add_listener(seen.append)
        log.emit("anything", x=1)  # must not raise
        self.assertEqual(len(seen), 1)

    def test_an_unknown_event_is_ignored(self):
        self.feed("some_future_event", x=1)
        self.assertEqual(self.text(), "")


class StoryInARealRun(unittest.TestCase):
    """Real runs (scripted model, real sandbox, validator and gates): the story file is produced and tells the truth."""

    @classmethod
    def setUpClass(cls):
        cls.template = ensure_baseline()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "run"

    def story(self):
        return (self.out / "RUN_STORY.md").read_text(encoding="utf-8")

    def test_a_bug_fix_run_tells_the_whole_story_in_order(self):
        from scripts.seed_bug import seed
        repo = Path(self.tmp.name) / "buggy"
        seed(self.template, repo)
        llm = ScriptedLLM(plan=lambda req: plan_for("bugfix"), real_bugfix=True)
        r = Harness(self.out, echo=None, llm=llm, story_preface=PREFACE).run(
            "Fix the bug where link stats report unique_visitors equal to the total number of clicks.", target_repo=str(repo))
        self.assertEqual(r.status, "completed")
        t = self.story()
        order = ["# Run story", "Stage 0: Scenario setup", "line 136", "Stage 1: Discover", "RED", "Stage 2: Plan", "`project_lead`",
                 "Human gate 1", "Stage 3: Work", "**AGENT (model) `code_author`**", "```diff", "COUNT(DISTINCT visitor_hash)",
                 "**AGENT (model) `spec_tester`**", "Stage 4: Validate", "THE PROOF", "Stage 5: Approval and release", "## Result", "Bug-fix proof"]
        pos = -1
        for needle in order:
            nxt = t.find(needle, pos + 1)
            self.assertGreater(nxt, pos, f"{needle!r} missing or out of order in the story")
            pos = nxt
        self.assertNotIn("Traceback", t)

    def test_an_enhancement_with_a_repair_and_an_amended_test_is_explained(self):
        repo = Path(self.tmp.name) / "repo"
        shutil.copytree(self.template, repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (repo / "tests" / "test_pinned_contract.py").write_text(PINNED_TEST_FILE, encoding="utf-8")
        llm = ScriptedLLM(plan=lambda req: plan_for("enhancement"), amend_openapi=True)
        r = Harness(self.out, echo=None, llm=llm, generator="llm").run(BROWNFIELD, target_repo=str(repo))
        self.assertEqual(r.status, "completed")
        t = self.story()
        for needle in ("repair round 1", "proposes amending an EXISTING test", "test_pinned_contract.py", "Human gate `test_update_approval`",
                       "re-run (generation 1)", "Existing tests amended"):
            self.assertIn(needle, t)

    def test_a_human_veto_is_described_and_the_run_still_has_a_story(self):
        llm = ScriptedLLM(plan=lambda req: plan_for("refactor"))
        r = Harness(self.out, echo=None, llm=llm, approval="reject:plan_approval").run(
            "Refactor the create link flow to tidy up the service module without changing behaviour.", target_repo=str(self.template))
        self.assertEqual(r.status, "rejected_at_plan")
        t = self.story()
        self.assertIn("**REJECTED**", t)
        self.assertIn("STOPPED by a human at the plan gate", t)

    def test_the_story_is_written_beside_the_audit_log(self):
        llm = ScriptedLLM(plan=lambda req: plan_for("refactor"))
        Harness(self.out, echo=None, llm=llm, approval="reject:plan_approval").run(
            "Refactor the create link flow to tidy up the service module without changing behaviour.", target_repo=str(self.template))
        self.assertTrue((self.out / "RUN_STORY.md").exists())
        self.assertTrue((self.out / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
