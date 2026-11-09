"""The live demo (DEMO_RESULT.md): the released service is really started, a long URL is shortened, the redirect is followed and the
analytics are read. For a bug fix the same steps run on the original buggy code, so before and after are visible side by side."""
import json
import tempfile
import unittest
from pathlib import Path

from agentic.harness.demo import CLICKS, render_demo_md, run_demo, write_demo_result
from agentic.harness.harness import Harness
from scripts.seed_bug import seed
from tests.fake_llm import ScriptedLLM
from tests.test_e2e import ensure_baseline
from tests.test_harness import plan_for

REQUEST = "Fix the bug where link stats report unique_visitors equal to the total number of clicks."


class DemoOnARealService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.good = ensure_baseline()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.buggy = Path(cls.tmp.name) / "buggy"
        seed(cls.good, cls.buggy)
        cls.good_run = run_demo(cls.good, "good")
        cls.bad_run = run_demo(cls.buggy, "buggy")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_a_long_url_becomes_a_short_url_that_redirects_back(self):
        r = self.good_run
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["links"]), 2)
        for link in r["links"]:
            self.assertEqual(link["create_status"], 201)
            self.assertTrue(link["code"])
            self.assertIn(link["code"], link["short_url"])
            self.assertEqual(link["redirect_status"], 302)
            self.assertEqual(link["location"], link["long_url"])
            self.assertTrue(link["redirect_ok"])

    def test_analytics_count_clicks_and_one_visitor_once(self):
        self.assertEqual(self.good_run["stats"], {"total_clicks": CLICKS, "unique_visitors": 1})

    def test_the_planted_bug_is_visible_before_and_gone_after(self):
        self.assertEqual(self.bad_run["stats"]["total_clicks"], CLICKS)
        self.assertEqual(self.bad_run["stats"]["unique_visitors"], CLICKS)  # the bug: every click counted as a unique visitor
        self.assertEqual(self.good_run["stats"]["unique_visitors"], 1)

    def test_the_report_tells_the_before_and_after_story(self):
        md = render_demo_md(self.good_run, self.bad_run, REQUEST, "bugfix")
        for needle in ("# Demo result", "https://en.wikipedia.org/wiki/URL_shortening", "**YES**", "BEFORE (the planted bug)",
                       "AFTER (the agents' fix)", "one visitor should count once", "Try it yourself"):
            self.assertIn(needle, md)

    def test_the_release_folder_is_never_written_to_or_left_running(self):
        before = sorted(p.name for p in self.good.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        run_demo(self.good, "again")
        after = sorted(p.name for p in self.good.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        self.assertEqual(before, after)  # it ran from a temporary copy: no .db, no log, nothing left in the release


class DemoFailuresAreReportedNotRaised(unittest.TestCase):
    def test_a_folder_with_no_service(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_demo(Path(d), "empty")
        self.assertFalse(r["ok"])
        self.assertIn("nothing to run", r["error"])

    def test_a_service_that_crashes_at_start_up_says_so_and_shows_why(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "shortener").mkdir()
            (Path(d) / "shortener" / "__init__.py").write_text("", encoding="utf-8")
            (Path(d) / "shortener" / "server.py").write_text("def main(argv=None):\n    raise RuntimeError('boom at start-up')\n", encoding="utf-8")
            r = run_demo(Path(d), "crashing", startup_s=10)
        self.assertFalse(r["ok"])
        self.assertIn("exited at start-up", r["error"])
        self.assertIn("boom at start-up", r["log_tail"])

    def test_write_demo_result_on_a_run_without_a_release_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            summary = write_demo_result(Path(d), "x", None)
            self.assertFalse(summary["ok"])
            self.assertTrue((Path(d) / "DEMO_RESULT.md").exists())
            self.assertIn("did not fully work", (Path(d) / "DEMO_RESULT.md").read_text(encoding="utf-8"))


class DemoInAHarnessRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = ensure_baseline()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "run"
        self.repo = Path(self.tmp.name) / "buggy"
        seed(self.template, self.repo)

    def run_it(self, **kw):
        llm = ScriptedLLM(plan=lambda req: plan_for("bugfix"), real_bugfix=True)
        return Harness(self.out, echo=None, llm=llm, **kw).run(REQUEST, target_repo=str(self.repo))

    def test_a_bug_fix_run_ends_with_a_demo_that_shows_before_and_after(self):
        r = self.run_it(demo=True)
        self.assertEqual(r.status, "completed")
        md = (self.out / "DEMO_RESULT.md").read_text(encoding="utf-8")
        self.assertIn("BEFORE (the planted bug)", md)
        self.assertIn("**YES**", md)
        events = [json.loads(ln) for ln in (self.out / "events.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
        demo = next(e for e in events if e["kind"] == "service_demo")
        self.assertTrue(demo["ok"])
        self.assertEqual(demo["unique_visitors"], 1)
        self.assertEqual(demo["before_unique"], CLICKS)
        story = (self.out / "RUN_STORY.md").read_text(encoding="utf-8")
        self.assertIn("**Live demo**", story)
        self.assertIn("that difference is the bug fix, seen working", story)
        self.assertIn("DEMO_RESULT.md", story)

    def test_the_demo_is_off_by_default_so_nothing_else_changes(self):
        self.run_it()
        self.assertFalse((self.out / "DEMO_RESULT.md").exists())

    def test_a_run_that_is_not_released_has_no_demo(self):
        llm = ScriptedLLM(plan=lambda req: plan_for("bugfix"), real_bugfix=True)
        r = Harness(self.out, echo=None, llm=llm, demo=True, approval="reject:release_approval").run(REQUEST, target_repo=str(self.repo))
        self.assertEqual(r.status, "rejected_at_release")
        self.assertFalse((self.out / "DEMO_RESULT.md").exists())


if __name__ == "__main__":
    unittest.main()
