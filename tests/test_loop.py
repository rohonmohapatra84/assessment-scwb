"""Unit tests for the harness loop, using tiny fake agents (no code generation involved)."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentic.harness.guardrails import Workspace
from agentic.harness.core import (FatalError, Task, TaskStatus, TransientError, ValidationFailure)
from agentic.harness.loop import GreedyLead, Loop, PlanError, validate_dag
from agentic.harness.core import Chaos
from agentic.harness.core import Blackboard, EventLog


class Fn:
    """Wrap a function as an agent."""
    def __init__(self, name, fn):
        self.name, self._fn = name, fn

    def run(self, ctx):
        return self._fn(ctx)


def make(agents, chaos=None, **kw):
    tmp = tempfile.TemporaryDirectory()
    bb, events = Blackboard(), EventLog()
    orch = Loop({a.name: a for a in agents}, bb, Workspace(Path(tmp.name)), events, chaos or Chaos(),
                        None, backoff_base=0.001, **kw)
    return orch, bb, events, tmp


class DagValidationTests(unittest.TestCase):
    def test_waves_and_parallelism(self):
        tasks = [Task("a", "a", "x"), Task("b", "b", "x", ["a"]), Task("c", "c", "x", ["a"]), Task("d", "d", "x", ["b", "c"])]
        self.assertEqual(validate_dag(tasks), [["a"], ["b", "c"], ["d"]])

    def test_cycle_rejected(self):
        with self.assertRaisesRegex(PlanError, "cycle"):
            validate_dag([Task("a", "a", "x", ["b"]), Task("b", "b", "x", ["a"])])

    def test_unknown_dependency_rejected(self):
        with self.assertRaisesRegex(PlanError, "unknown task"):
            validate_dag([Task("a", "a", "x", ["ghost"])])

    def test_duplicate_ids_rejected(self):
        with self.assertRaisesRegex(PlanError, "duplicate"):
            validate_dag([Task("a", "a", "x"), Task("a", "a2", "x")])

    def test_concurrent_write_conflict_rejected(self):
        tasks = [Task("a", "a", "x", writes=["src/*"]), Task("b", "b", "x", writes=["src/app.py"])]
        with self.assertRaisesRegex(PlanError, "write conflict"):
            validate_dag(tasks)

    def test_ordered_tasks_may_share_write_scope(self):
        validate_dag([Task("a", "a", "x", writes=["src/*"]), Task("b", "b", "x", ["a"], writes=["src/*"])])

    def test_repair_target_must_be_ancestor(self):
        with self.assertRaisesRegex(PlanError, "ancestor"):
            validate_dag([Task("a", "a", "x"), Task("v", "v", "x", repair_targets=["a"])])


class ExecutionTests(unittest.TestCase):
    def test_runs_in_dependency_order_and_in_parallel(self):
        order, lock, active, peak = [], threading.Lock(), [0], [0]

        def work(name):
            def fn(ctx):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    order.append(name)
                    active[0] -= 1
            return fn
        agents = [Fn(n, work(n)) for n in "abcd"]
        orch, _, _, tmp = make(agents)
        tasks = [Task("a", "a", "a"), Task("b", "b", "b", ["a"]), Task("c", "c", "c", ["a"]), Task("d", "d", "d", ["b", "c"])]
        report = orch.run(tasks)
        self.assertTrue(report["ok"])
        self.assertEqual(order[0], "a")
        self.assertEqual(order[-1], "d")
        self.assertEqual(peak[0], 2)  # b and c overlapped
        tmp.cleanup()

    def test_blackboard_hand_off_between_tasks(self):
        agents = [Fn("p", lambda ctx: ctx.bb.set("contract", {"v": 7}) or {}),
                  Fn("c", lambda ctx: {"seen": ctx.bb.require("contract")["v"]})]
        orch, bb, _, tmp = make(agents)
        orch.run([Task("p", "p", "p"), Task("c", "c", "c", ["p"])])
        self.assertEqual(bb.get("result:c"), {"seen": 7})
        tmp.cleanup()

    def test_transient_error_is_retried_with_backoff(self):
        calls = []

        def flaky(ctx):
            calls.append(ctx.attempt)
            if ctx.attempt < 3:
                raise TransientError("blip")
            return {"ok": True}
        orch, _, events, tmp = make([Fn("f", flaky)])
        t = Task("t", "t", "f")
        report = orch.run([t])
        self.assertTrue(report["ok"])
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(len(events.of_kind("task_retry")), 2)
        tmp.cleanup()

    def test_transient_error_gives_up_after_max_attempts(self):
        def always(ctx):
            raise TransientError("down")
        orch, _, _, tmp = make([Fn("f", always)])
        t = Task("t", "t", "f", max_attempts=2)
        report = orch.run([t])
        self.assertFalse(report["ok"])
        self.assertEqual((t.status, t.attempts), (TaskStatus.FAILED, 2))
        tmp.cleanup()

    def test_failure_skips_dependents_but_not_independent_tasks(self):
        ran = []

        def boom(ctx):
            raise FatalError("nope")
        agents = [Fn("boom", boom), Fn("ok", lambda ctx: ran.append(ctx.task.id) or {})]
        orch, _, _, tmp = make(agents)
        tasks = [Task("bad", "bad", "boom"), Task("child", "child", "ok", ["bad"]), Task("solo", "solo", "ok")]
        report = orch.run(tasks)
        st = {t.id: t.status for t in tasks}
        self.assertEqual(st, {"bad": TaskStatus.FAILED, "child": TaskStatus.SKIPPED, "solo": TaskStatus.SUCCEEDED})
        self.assertEqual(ran, ["solo"])  # the child never ran on bad inputs
        self.assertEqual(report["hard_failed"], ["bad", "child"])
        tmp.cleanup()

    def test_optional_failure_degrades_without_blocking(self):
        def boom(ctx):
            raise FatalError("docs broke")
        agents = [Fn("boom", boom), Fn("ok", lambda ctx: {})]
        orch, _, _, tmp = make(agents)
        tasks = [Task("docs", "docs", "boom", optional=True), Task("after", "after", "ok", ["docs"])]
        report = orch.run(tasks)
        self.assertTrue(report["ok"])
        self.assertEqual(report["degraded"], ["docs"])
        self.assertEqual(tasks[1].status, TaskStatus.SUCCEEDED)
        tmp.cleanup()

    def test_agent_crash_is_contained(self):
        def bug(ctx):
            return 1 / 0
        orch, _, events, tmp = make([Fn("bug", bug)])
        t = Task("t", "t", "bug")
        report = orch.run([t])
        self.assertFalse(report["ok"])
        self.assertIn("ZeroDivisionError", t.error)
        tmp.cleanup()

    def test_repair_loop_routes_feedback_to_the_responsible_producer(self):
        def producer(ctx):
            return {"fixed": bool(ctx.feedback), "gen": ctx.task.generation, "feedback": ctx.feedback}

        def validator(ctx):
            res = ctx.bb.require("result:prod")
            if not res["fixed"]:
                raise ValidationFailure("bad output", [{"severity": "error", "message": "missing X", "target": "prod"}])
            return {"validated_gen": res["gen"]}
        orch, bb, events, tmp = make([Fn("prod", producer), Fn("val", validator)])
        tasks = [Task("prod", "prod", "prod"), Task("val", "val", "val", ["prod"], repair_targets=["prod"])]
        report = orch.run(tasks)
        self.assertTrue(report["ok"])
        self.assertEqual(report["repairs"], {"val": 1})
        self.assertEqual(bb.get("result:val"), {"validated_gen": 1})
        self.assertEqual(bb.get("result:prod")["feedback"][0]["message"], "missing X")
        tmp.cleanup()

    def test_repair_loop_is_bounded(self):
        def producer(ctx):
            return {}

        def validator(ctx):
            raise ValidationFailure("never good", [{"severity": "error", "message": "x", "target": "prod"}])
        orch, _, _, tmp = make([Fn("prod", producer), Fn("val", validator)], max_repairs=2)
        tasks = [Task("prod", "prod", "prod"), Task("val", "val", "val", ["prod"], repair_targets=["prod"])]
        report = orch.run(tasks)
        self.assertFalse(report["ok"])
        self.assertEqual(report["repairs"]["val"], 2)
        self.assertEqual(tasks[1].status, TaskStatus.FAILED)
        tmp.cleanup()

    def test_producer_self_corrects_with_its_own_findings(self):
        seen = []

        def agent(ctx):
            seen.append(list(ctx.feedback))
            if not ctx.feedback:
                raise ValidationFailure("self check failed", [{"message": "fix me"}])
            return {}
        orch, _, events, tmp = make([Fn("a", agent)])
        report = orch.run([Task("t", "t", "a")])
        self.assertTrue(report["ok"])
        self.assertEqual(seen[1], [{"message": "fix me"}])
        tmp.cleanup()

    def test_chaos_transient_and_fatal_injection(self):
        orch, _, events, tmp = make([Fn("a", lambda ctx: {})], chaos=Chaos(["transient:t:2"]))
        self.assertTrue(orch.run([Task("t", "t", "a")])["ok"])
        self.assertEqual(len(events.of_kind("task_retry")), 2)
        tmp.cleanup()
        orch, _, _, tmp = make([Fn("a", lambda ctx: {})], chaos=Chaos(["fatal:t"]))
        self.assertFalse(orch.run([Task("t", "t", "a")])["ok"])
        tmp.cleanup()

    def test_resume_only_reruns_unsuccessful_tasks(self):
        runs = []
        fail = [True]

        def a(ctx):
            runs.append("a")
            return {}

        def b(ctx):
            runs.append("b")
            if fail[0]:
                raise FatalError("first time")
            return {}
        orch, _, _, tmp = make([Fn("a", a), Fn("b", b)])
        tasks = [Task("a", "a", "a"), Task("b", "b", "b", ["a"])]
        self.assertFalse(orch.run(tasks)["ok"])
        fail[0] = False
        self.assertTrue(orch.run(tasks, resume=True)["ok"])
        self.assertEqual(runs, ["a", "b", "b"])  # 'a' was not re-run
        tmp.cleanup()

    def test_bad_chaos_spec_rejected(self):
        with self.assertRaises(ValueError):
            Chaos(["explode:everything"])


if __name__ == "__main__":
    unittest.main()
