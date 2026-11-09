import tempfile
import unittest
from pathlib import Path

from agentic.harness.approvals import ApprovalPolicy
from agentic.harness.guardrails import Workspace, run_unittests, scan_for_secrets
from agentic.harness.core import GuardrailViolation


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self.tmp.name) / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, rel, content="x = 1\n", **kw):
        self.ws.write(rel, content, kind="code", producer="t", **kw)

    def test_normal_write_is_recorded_in_manifest(self):
        self.w("pkg/a.py")
        e = self.ws.manifest["pkg/a.py"]
        self.assertEqual((e.kind, e.producer, e.bytes), ("code", "t", 6))
        self.assertEqual(len(e.sha256), 64)

    def test_path_traversal_blocked(self):
        for bad in ("../evil.py", "a/../../evil.py", "/etc/passwd", "C:\\Windows\\x.py", ""):
            with self.subTest(path=bad):
                with self.assertRaises(GuardrailViolation):
                    self.w(bad)

    def test_extension_allow_list(self):
        for bad in ("run.exe", "x.dll", "payload.bat", "lib.so"):
            with self.assertRaises(GuardrailViolation):
                self.w(bad)
        self.w("Dockerfile", "FROM scratch\n")  # allowed bare name

    def test_write_scope_is_enforced(self):
        self.w("tests/test_a.py", scopes=["tests/*"])
        with self.assertRaisesRegex(GuardrailViolation, "outside its declared scope"):
            self.w("shortener/app.py", scopes=["tests/*"])

    def test_secret_scan_blocks_credentials(self):
        for leak in ('API_KEY = "abcd1234efgh5678"', "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----",
                     "sk-ant-api03-" + "a" * 30):
            with self.subTest(leak=leak[:20]):
                with self.assertRaisesRegex(GuardrailViolation, "secret"):
                    self.w("cfg.py", leak)
        self.assertEqual(scan_for_secrets("password = os.environ['X']"), [])  # reading env is fine

    def test_size_cap(self):
        with self.assertRaisesRegex(GuardrailViolation, "too large"):
            self.w("big.txt", "x" * 300_000)

    def test_baseline_import_does_not_touch_source_and_supports_pristine_reads(self):
        src = Path(self.tmp.name) / "src"
        (src / "pkg").mkdir(parents=True)
        (src / "pkg" / "m.py").write_text("v = 1\n", encoding="utf-8")
        ws = Workspace(Path(self.tmp.name) / "ws2", baseline_root=Path(self.tmp.name) / "base")
        ws.import_tree(src)
        ws.write("pkg/m.py", "v = 2\n", kind="code", producer="t")
        self.assertEqual((src / "pkg" / "m.py").read_text(), "v = 1\n")  # original untouched
        self.assertEqual(ws.read("pkg/m.py"), "v = 2\n")
        self.assertEqual(ws.read_baseline("pkg/m.py"), "v = 1\n")  # pristine copy for idempotent repair

    def test_import_skips_database_files_a_running_service_left_behind(self):
        """Regression: a release that had been run contained shortener.sqlite3 (+ -wal/-shm); the binary files were imported
        and the validator died with UnicodeDecodeError."""
        src = Path(self.tmp.name) / "src_db"
        src.mkdir()
        (src / "m.py").write_text("v = 1\n", encoding="utf-8")
        for name in ("shortener.sqlite3", "shortener.sqlite3-wal", "shortener.sqlite3-shm", "x.db"):
            (src / name).write_bytes(b"SQLite format 3\x00" + b"\x90" * 200)
        ws = Workspace(Path(self.tmp.name) / "ws_db", baseline_root=Path(self.tmp.name) / "base_db")
        self.assertEqual(ws.import_tree(src), 1)
        self.assertEqual(ws.list_files(), ["m.py"])
        self.assertFalse((Path(self.tmp.name) / "base_db" / "shortener.sqlite3").exists())

    def test_promote_copies_only_workspace_files(self):
        self.w("a.py")
        dest = Path(self.tmp.name) / "release"
        self.assertEqual(self.ws.promote(dest), 1)
        self.assertTrue((dest / "a.py").exists())


class TestRunnerTests(unittest.TestCase):
    def test_runs_in_subprocess_with_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("")
            (root / "tests" / "test_ok.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.assertTrue(True)\n")
            r = run_unittests(root, timeout=60)
            self.assertTrue(r.ok)
            self.assertEqual(r.tests_run, 1)
            (root / "tests" / "test_slow.py").write_text("import time, unittest\nclass S(unittest.TestCase):\n    def test_s(self): time.sleep(30)\n")
            slow = run_unittests(root, timeout=2)
            self.assertFalse(slow.ok)
            self.assertIn("TIMEOUT", slow.output)

    def test_failures_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text("")
            (root / "tests" / "test_bad.py").write_text("import unittest\nclass B(unittest.TestCase):\n    def test_b(self): self.assertEqual(1, 2)\n")
            r = run_unittests(root, timeout=60)
            self.assertFalse(r.ok)
            self.assertEqual(r.failures, 1)

    def test_child_does_not_inherit_secrets(self):
        import os
        os.environ["AGENTIC_TEST_SECRET"] = "hunter2"
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "tests").mkdir()
                (root / "tests" / "__init__.py").write_text("")
                (root / "tests" / "test_env.py").write_text(
                    "import os, unittest\nclass E(unittest.TestCase):\n    def test_e(self): self.assertNotIn('AGENTIC_TEST_SECRET', os.environ)\n")
                self.assertTrue(run_unittests(root, timeout=60).ok)
        finally:
            del os.environ["AGENTIC_TEST_SECRET"]


class ApprovalTests(unittest.TestCase):
    def test_auto_approves_clean_and_refuses_blocking(self):
        p = ApprovalPolicy("auto")
        self.assertTrue(p.request("g", "s", {}, blocking_issues=0).approved)
        self.assertFalse(p.request("g", "s", {}, blocking_issues=2).approved)

    def test_reject_modes(self):
        self.assertFalse(ApprovalPolicy("reject").request("any", "s", {}).approved)
        p = ApprovalPolicy("reject:release_approval")
        self.assertTrue(p.request("plan_approval", "s", {}).approved)
        self.assertFalse(p.request("release_approval", "s", {}).approved)

    def test_interactive_uses_human_answer_and_is_audited(self):
        p = ApprovalPolicy("interactive", input_fn=lambda prompt: "y")
        d = p.request("plan_approval", "summary", {})
        self.assertTrue(d.approved)
        self.assertEqual(p.history[0].decided_by, "human")
        self.assertFalse(ApprovalPolicy("interactive", input_fn=lambda prompt: "").request("g", "s", {}).approved)


if __name__ == "__main__":
    unittest.main()
