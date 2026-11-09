"""The cloud API: remote human gates, auth, path safety. Uses the offline ScriptedLLM (no network, no credentials)."""
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from agentic.api import ApiServer
from agentic.providers import NullProvider
from tests.fake_llm import ScriptedLLM
from tests.test_e2e import GREENFIELD

TOKEN = "test-token-123456"


class Api:
    def __init__(self, tmp, llm_factory, **kw):
        self.srv = ApiServer(("127.0.0.1", 0), Path(tmp), TOKEN, llm_factory=llm_factory, **kw)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, token=TOKEN, raw=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (data if raw else (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data))

    def wait(self, run_id, pred, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            _, d = self.call("GET", f"/runs/{run_id}")
            if pred(d):
                return d
            time.sleep(0.1)
        raise AssertionError(f"timeout waiting; last={d.get('status')} pending={d.get('pending_gate', {}).get('gate')}")

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class ApiServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        getattr(self, "api", None) and self.api.stop()
        self.tmp.cleanup()

    def start(self, factory=ScriptedLLM, **kw):
        self.api = Api(self.tmp.name, factory, **kw)
        return self.api

    def test_full_llm_run_with_remote_human_approvals(self):
        api = self.start()
        status, r = api.call("POST", "/runs", {"requirement": GREENFIELD})
        self.assertEqual((status, r["generator"]), (202, "llm"))  # defaults to the LLM when one is configured
        rid = r["run_id"]
        d = api.wait(rid, lambda d: d.get("pending_gate"))
        self.assertEqual(d["pending_gate"]["gate"], "plan_approval")
        self.assertIn("wave 1", d["pending_gate"]["summary"])
        self.assertNotIn("release", [t["id"] for t in d.get("tasks", [])])
        # nothing executes until a human decides: no workspace yet
        self.assertFalse((Path(self.tmp.name) / rid / "workspace" / "shortener").exists())
        self.assertEqual(api.call("POST", f"/runs/{rid}/gates/plan_approval", {"approved": True, "by": "alice"})[0], 200)
        d = api.wait(rid, lambda d: d.get("pending_gate", {}).get("gate") == "release_approval")
        self.assertEqual(d["validation"]["failed"], 0)
        self.assertEqual(api.call("GET", f"/runs/{rid}/files")[0], 404)  # validated but NOT released yet
        self.assertEqual(api.call("POST", f"/runs/{rid}/gates/release_approval", {"approved": True, "by": "bob", "note": "lgtm"})[0], 200)
        d = api.wait(rid, lambda d: d["status"] == "completed")
        self.assertEqual([x["decided_by"] for x in d["decisions"]], ["alice", "bob"])  # real identities in the audit trail
        self.assertEqual(d["llm"]["provider"], "scripted-fake")
        self.assertGreater(d["llm"]["calls"], 2)
        files = api.call("GET", f"/runs/{rid}/files")[1]
        self.assertIn("shortener/app.py", files)
        self.assertIn("Engineering Summary", api.call("GET", f"/runs/{rid}/summary", raw=True)[1].decode())
        z = zipfile.ZipFile(io.BytesIO(api.call("GET", f"/runs/{rid}/release.zip", raw=True)[1]))
        self.assertIn("openapi.json", z.namelist())

    def test_human_rejection_blocks_release(self):
        api = self.start()
        rid = api.call("POST", "/runs", {"requirement": GREENFIELD})[1]["run_id"]
        api.wait(rid, lambda d: d.get("pending_gate"))
        api.call("POST", f"/runs/{rid}/gates/plan_approval", {"approved": True, "by": "alice"})
        api.wait(rid, lambda d: d.get("pending_gate", {}).get("gate") == "release_approval")
        api.call("POST", f"/runs/{rid}/gates/release_approval", {"approved": False, "by": "bob", "note": "not yet"})
        d = api.wait(rid, lambda d: d["status"] == "rejected_at_release")
        self.assertEqual(api.call("GET", f"/runs/{rid}/release.zip")[0], 404)
        self.assertFalse(d["decisions"][-1]["approved"])

    def test_unattended_gate_times_out_as_rejection(self):
        api = self.start(gate_timeout=0.5)
        rid = api.call("POST", "/runs", {"requirement": GREENFIELD})[1]["run_id"]
        d = api.wait(rid, lambda d: d["status"] == "rejected_at_plan", timeout=30)
        self.assertEqual(d["decisions"][0]["decided_by"], "timeout")

    def test_auth_and_input_validation(self):
        api = self.start()
        self.assertEqual(api.call("GET", "/runs", token=None)[0], 401)
        self.assertEqual(api.call("GET", "/runs", token="wrong")[0], 401)
        self.assertEqual(api.call("GET", "/healthz", token=None)[0], 200)  # liveness probe needs no secret
        self.assertEqual(api.call("POST", "/runs", {"requirement": "x"})[0], 400)
        self.assertEqual(api.call("POST", "/runs", {"requirement": GREENFIELD, "generator": "gpt"})[0], 400)
        self.assertEqual(api.call("POST", "/runs", {"requirement": GREENFIELD, "approval": "auto"})[0], 400)  # auto disabled
        self.assertEqual(api.call("GET", "/runs/doesnotexist1")[0], 404)

    def test_remote_callers_cannot_name_filesystem_paths(self):
        api = self.start()
        for evil in ("C:/Windows", "../../etc", "/etc/passwd", "0123456789ab"):
            status, _ = api.call("POST", "/runs", {"requirement": GREENFIELD, "target_run": evil})
            self.assertEqual(status, 400, evil)

    def test_gate_decision_needs_identity_and_a_pending_gate(self):
        api = self.start()
        rid = api.call("POST", "/runs", {"requirement": GREENFIELD})[1]["run_id"]
        api.wait(rid, lambda d: d.get("pending_gate"))
        self.assertEqual(api.call("POST", f"/runs/{rid}/gates/plan_approval", {"approved": True})[0], 400)  # no 'by'
        self.assertEqual(api.call("POST", f"/runs/{rid}/gates/release_approval", {"approved": True, "by": "x"})[0], 409)  # wrong gate
        api.call("POST", f"/runs/{rid}/gates/plan_approval", {"approved": False, "by": "alice"})
        api.wait(rid, lambda d: d["status"] == "rejected_at_plan")

    def test_clarification_flow_over_the_api(self):
        api = self.start()
        done = api.call("POST", "/runs", {"requirement": "Make the link service faster and more secure.", "generator": "template"})[1]["run_id"]
        d = api.wait(done, lambda d: d["status"] == "needs_clarification")
        self.assertEqual(len(d["questions"]), 2)
        status, r = api.call("POST", f"/runs/{done}/answers", {"answers": {"Q1": "redirect p95 under 30 ms", "Q2": "API keys required"}})
        self.assertEqual((status, r["continues"]), (202, done))
        d2 = api.wait(r["run_id"], lambda d: d.get("pending_gate"))
        self.assertEqual(d2["pending_gate"]["gate"], "plan_approval")
        api.call("POST", f"/runs/{r['run_id']}/gates/plan_approval", {"approved": False, "by": "alice"})

    def test_run_without_a_model_is_a_clear_400(self):
        api = self.start(factory=NullProvider)
        for body in ({"requirement": GREENFIELD, "generator": "llm"}, {"requirement": GREENFIELD}):
            status, r = api.call("POST", "/runs", body)
            self.assertEqual(status, 400)
            self.assertIn("no LLM provider", r["error"])

    def test_template_generator_can_be_requested_explicitly_when_a_model_exists(self):
        api = self.start()
        status, r = api.call("POST", "/runs", {"requirement": GREENFIELD, "generator": "template"})
        self.assertEqual((status, r["generator"]), (202, "template"))
        api.call("POST", f"/runs/{r['run_id']}/gates/plan_approval", {"approved": False, "by": "a"})

    def test_concurrency_cap(self):
        api = self.start(max_active=1)
        first = api.call("POST", "/runs", {"requirement": GREENFIELD})[1]["run_id"]
        api.wait(first, lambda d: d.get("pending_gate"))
        self.assertEqual(api.call("POST", "/runs", {"requirement": GREENFIELD})[0], 429)
        api.call("POST", f"/runs/{first}/gates/plan_approval", {"approved": False, "by": "a"})

    def test_release_files_are_path_jailed(self):
        api = self.start()
        rid = api.call("POST", "/runs", {"requirement": GREENFIELD})[1]["run_id"]
        api.wait(rid, lambda d: d.get("pending_gate"))
        api.call("POST", f"/runs/{rid}/gates/plan_approval", {"approved": True, "by": "a"})
        api.wait(rid, lambda d: d.get("pending_gate", {}).get("gate") == "release_approval")
        api.call("POST", f"/runs/{rid}/gates/release_approval", {"approved": True, "by": "b"})
        api.wait(rid, lambda d: d["status"] == "completed")
        for p in ("..%2f..%2fmeta.json", "..%5c..%5cmeta.json", "%2e%2e/%2e%2e/state.json"):
            self.assertIn(api.call("GET", f"/runs/{rid}/files/{p}")[0], (404, 400))
        self.assertEqual(api.call("GET", f"/runs/{rid}/files/openapi.json")[0], 200)


if __name__ == "__main__":
    unittest.main()
