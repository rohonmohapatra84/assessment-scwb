"""ScriptedLLM: an offline stand-in for the model (any provider).

It answers each generator prompt in the JSON shape the real model is instructed to use, reusing the template
sources as the "model output", and can be told to misbehave so the *system's* handling of bad model output
(self-correction, repair loop, retry) is tested without credentials or network. It does NOT prove the real model
writes good code: that is what scripts/model_smoke.py and a live run are for.
"""
import json
import threading

from agentic.knowledge import EXPIRY_EDITS
from agentic.providers import LLMTransient
from agentic.steps.build import _params, render, template_files
from tests.reference_plans import lead_reply

CREATE_APP_SHIM = '''

def create_app(db_path=":memory:", *, base_url="http://localhost:8080", rate_limit_per_minute=120,
               api_keys=(), redirect_status=302):
    """Platform interface: build the WSGI application."""
    config = Config(db_path=db_path, base_url=base_url, rate_limit_per_minute=rate_limit_per_minute,
                    api_keys=tuple(api_keys), redirect_status=redirect_status)
    app = Application(config)
    app.flush_analytics = app.service.recorder.flush
    return app
'''

TESTS = '''
import os
import tempfile
import unittest

from shortener.app import create_app

from .wsgi_client import Client


def new(db=":memory:", **kw):
    app = create_app(db, **kw)
    return app, Client(app)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app, self.c = new()

    def tearDown(self):
        self.app.close()

    def mk(self, url="https://example.com/x", **extra):
        return self.c.request("POST", "/api/v1/links", {"url": url, **extra})

    def test_ac1_create_returns_201_with_code(self):
        r = self.mk()
        self.assertEqual(r.status, 201)
        self.assertEqual(len(r.json()["code"]), 7)
        self.assertIn("short_url", r.json())

    def test_ac2_redirects_with_302(self):
        code = self.mk("https://example.com/dest").json()["code"]
        r = self.c.request("GET", "/" + code)
        self.assertEqual((r.status, r.headers["location"]), (302, "https://example.com/dest"))

    def test_ac3_unknown_code_is_404_json(self):
        r = self.c.request("GET", "/nosuchcode")
        self.assertEqual(r.status, 404)
        self.assertIn("error", r.json())

    def test_ac4_alias_conflict_409_and_invalid_400(self):
        self.assertEqual(self.mk(custom_alias="promo-1").status, 201)
        self.assertEqual(self.mk(custom_alias="promo-1").status, 409)
        self.assertEqual(self.mk(custom_alias="a").status, 400)

    def test_ac5_rejects_bad_urls(self):
        for url in ("ftp://example.com/x", "http://127.0.0.1/x", "http://10.0.0.1/", "https://u:p@example.com/", "javascript:alert(1)"):
            with self.subTest(url=url):
                self.assertEqual(self.mk(url).status, 400)

    def test_ac6_stats_after_clicks(self):
        code = self.mk().json()["code"]
        self.c.request("GET", "/" + code)
        self.c.request("GET", "/" + code, ip="198.51.100.4")
        self.app.flush_analytics()
        s = self.c.request("GET", f"/api/v1/links/{code}/stats").json()
        self.assertEqual(s["total_clicks"], 2)
        self.assertEqual(s["unique_visitors"], 2)

    def test_ac7_links_persist_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.db")
            a1, c1 = new(path)
            code = c1.request("POST", "/api/v1/links", {"url": "https://example.com/keep"}).json()["code"]
            a1.close()
            a2, c2 = new(path)
            self.assertEqual(c2.request("GET", "/" + code).status, 302)
            a2.close()

    def test_ac8_rate_limit_returns_429(self):
        a, c = new(rate_limit_per_minute=2)
        codes = [c.request("POST", "/api/v1/links", {"url": "https://example.com/r"}).status for _ in range(3)]
        a.close()
        self.assertEqual(codes, [201, 201, 429])

    def test_ac9_documented_operations_are_reachable(self):
        self.assertEqual(self.c.request("GET", "/healthz").status, 200)
        self.assertEqual(self.c.request("GET", "/openapi.json").status, 200)

    def test_delete_then_404(self):
        code = self.mk().json()["code"]
        self.assertEqual(self.c.request("DELETE", "/api/v1/links/" + code).status, 204)
        self.assertEqual(self.c.request("GET", "/" + code).status, 404)

    def test_metadata(self):
        code = self.mk().json()["code"]
        self.assertEqual(self.c.request("GET", "/api/v1/links/" + code).json()["code"], code)

    def test_malformed_json_is_400(self):
        self.assertEqual(self.c.request("POST", "/api/v1/links", raw_body=b"{nope").status, 400)

    def test_oversized_body_is_400(self):
        self.assertEqual(self.c.request("POST", "/api/v1/links", raw_body=b"x" * 20000).status, 400)

    def test_stats_days_validation(self):
        code = self.mk().json()["code"]
        self.assertEqual(self.c.request("GET", f"/api/v1/links/{code}/stats?days=0").status, 400)
        self.assertEqual(self.c.request("GET", f"/api/v1/links/{code}/stats?days=91").status, 400)

    def test_request_id_header(self):
        self.assertIn("x-request-id", self.c.request("GET", "/healthz").headers)

    def test_wrong_method_is_405(self):
        self.assertEqual(self.c.request("PUT", "/api/v1/links").status, 405)

    def test_crlf_in_url_is_rejected(self):
        self.assertEqual(self.mk("https://example.com/a\\r\\nX: 1").status, 400)

    def test_auth_required_when_keys_configured(self):
        a, c = new(api_keys=("k-12345678",))
        self.assertEqual(c.request("POST", "/api/v1/links", {"url": "https://example.com/z"}).status, 401)
        r = c.request("POST", "/api/v1/links", {"url": "https://example.com/z"}, headers={"X-API-Key": "k-12345678"})
        a.close()
        self.assertEqual(r.status, 201)

    def test_custom_alias_is_used(self):
        self.assertEqual(self.mk(custom_alias="my-alias").json()["code"], "my-alias")

    def test_private_hosts_are_blocked(self):
        for url in ("http://localhost/x", "http://192.168.0.1/x", "http://169.254.169.254/"):
            self.assertEqual(self.mk(url).status, 400)

    def test_deleted_code_cannot_be_reused(self):
        code = self.mk(custom_alias="reuse-me").json()["code"]
        self.c.request("DELETE", "/api/v1/links/" + code)
        self.assertEqual(self.mk(custom_alias="reuse-me").status, 409)
'''


# Tests for behaviour the baseline service ALREADY has (so they pass on unchanged code): what a tests-only or refactor
# change should add. Self-contained WSGI calls: the baseline repo has no wsgi_client helper.
EXISTING_BEHAVIOUR_TESTS = '''
import unittest

from .helpers import make_app


class ExistingBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.app, self.service, self.clock, self.client = make_app()

    def test_healthz_is_ok(self):
        self.assertEqual(self.client.request("GET", "/healthz").status, 200)

    def test_unknown_code_is_404(self):
        self.assertEqual(self.client.request("GET", "/nosuchcode").status, 404)

    def test_another_unknown_code_is_404(self):
        self.assertEqual(self.client.request("GET", "/zzzzzzz").status, 404)

    def test_openapi_document_is_served(self):
        self.assertEqual(self.client.request("GET", "/openapi.json").status, 200)

    def test_creating_a_link_returns_201(self):
        self.assertEqual(self.client.request("POST", "/api/v1/links", {"url": "https://example.com/x"}).status, 201)
'''


# A realistic bug fix for the seeded bug (scripts/seed_bug.py) and the regression tests that demonstrate it.
BUGFIX_EDIT = {"path": "shortener/storage.py", "find": "SELECT COUNT(visitor_hash) FROM click_events",
               "replace": "SELECT COUNT(DISTINCT visitor_hash) FROM click_events"}
REGRESSION_TESTS = '''
import unittest

from .helpers import make_app


class RepeatVisitorRegression(unittest.TestCase):
    def setUp(self):
        self.app, self.service, self.clock, self.client = make_app()
        self.code = self.client.request("POST", "/api/v1/links", {"url": "https://example.com/x"}).json()["code"]

    def stats(self):
        self.service.recorder.flush()
        return self.client.request("GET", "/api/v1/links/%s/stats" % self.code).json()

    def test_repeat_visits_from_one_visitor_count_as_one_unique_visitor(self):
        for _ in range(3):
            self.client.request("GET", "/" + self.code)
        self.assertEqual(self.stats()["unique_visitors"], 1)

    def test_total_clicks_still_counts_every_visit(self):
        for _ in range(3):
            self.client.request("GET", "/" + self.code)
        self.assertEqual(self.stats()["total_clicks"], 3)

    def test_two_different_visitors_are_two_unique_visitors(self):
        self.client.request("GET", "/" + self.code, ip="198.51.100.1")
        self.client.request("GET", "/" + self.code, ip="198.51.100.2")
        self.assertEqual(self.stats()["unique_visitors"], 2)
'''

# An enhancement that makes an EXISTING test obsolete: a new request field is added to the contract, and a baseline test
# pins the exact field list of that request.
OPENAPI_EDIT = {"path": "openapi.json",
                "find": '"custom_alias": {\n            "type": "string",\n            "pattern": "^[A-Za-z0-9_-]{3,32}$"\n          }',
                "replace": '"custom_alias": {\n            "type": "string",\n            "pattern": "^[A-Za-z0-9_-]{3,32}$"\n          },\n'
                           '          "expires_in_seconds": {"type": "integer", "minimum": 60, "maximum": 31536000}'}
PINNED_TEST_FILE = '''import json
import unittest
from pathlib import Path


class PinnedContractTests(unittest.TestCase):
    def test_create_request_has_exactly_the_original_fields(self):
        spec = json.loads((Path(__file__).resolve().parent.parent / "openapi.json").read_text(encoding="utf-8"))
        props = sorted(spec["components"]["schemas"]["CreateLinkRequest"]["properties"])
        self.assertEqual(props, ["custom_alias", "url"])
'''
PINNED_FIND = 'self.assertEqual(props, ["custom_alias", "url"])'
PINNED_REPLACE = 'self.assertEqual(props, ["custom_alias", "expires_in_seconds", "url"])'


class ScriptedLLM:
    name = "scripted-fake"
    available = True
    deployment = "fake-deployment"

    def __init__(self, *, code_mutations=None, transient_first=0, bad_anchor_first=False, drop_interface_first=False,
                 bad_plans=0, plan=None, decider=None, real_bugfix=False, bugfix_tests="regression",
                 amend_openapi=False, test_updates="valid"):
        self.real_bugfix = real_bugfix        # fix the seeded bug for real (storage.py) instead of a comment-only change
        self.bugfix_tests = bugfix_tests      # "regression" (fails on the old code) | "weak" (passes on the old code too)
        self.amend_openapi = amend_openapi    # the enhancement also adds a field to openapi.json (a contract change)
        self.test_updates = test_updates      # once shown the failing test: "valid" | "weaken" | "none"
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.prompts = []
        # project_lead behaviour: by default the reference plan, and "start as many ready tasks as there are slots"
        self.bad_plans_left = bad_plans   # first N plan requests get an invalid plan (exercises plan rejection)
        self.plan = plan                  # fn(request: dict) -> reply dict, replaces the reference plan
        self.decider = decider            # fn(request: dict) -> reply dict, replaces the default decision
        self.lead_requests = []           # every REQUEST_JSON the lead was asked, for assertions
        self._lock = threading.Lock()
        self.code_calls = 0
        self.change_calls = 0
        self.transient_left = transient_first
        self.code_mutations = code_mutations or []  # list of fn(files: dict) applied to successive code generations
        self.bad_anchor_first = bad_anchor_first
        self.drop_interface_first = drop_interface_first

    def _comment_only_change(self):
        """A behaviour-preserving edit: a comment. Stands in for a refactor or a minimal fix."""
        return {"summary": "document the alias error", "decisions": [], "risks": [], "new_files": [],
                "edits": [{"path": "shortener/errors.py", "find": "class AliasTaken(ShortenerError):",
                           "replace": "# Raised when a custom alias is already in use.\nclass AliasTaken(ShortenerError):"}]}

    def _lead(self, prompt):
        """Play the project_lead agent: parse the REQUEST_JSON block exactly as a real model would see it."""
        req = json.loads(prompt.split("REQUEST_JSON:\n", 1)[1])
        with self._lock:
            self.lead_requests.append(req)
            bad = req["kind"] == "plan" and self.bad_plans_left > 0
            if bad:
                self.bad_plans_left -= 1
        if req["kind"] == "plan":
            if bad:
                return {"rationale": "scripted bad plan", "tasks": [{"id": "x", "tool": "no_such_tool", "title": "x", "deps": []}]}
            if self.plan:
                return self.plan(req)
            return lead_reply({"plan_type": req["plan_type"], "generator": req["generator"]})
        if self.decider:
            return self.decider(req)
        return {"action": "start", "tasks": req["ready"][:req["free_slots"]], "why": "scripted"}

    def complete_json(self, system, prompt, max_tokens=1024):
        with self._lock:
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += len(prompt) // 4
            self.usage["completion_tokens"] += 500
            self.prompts.append(prompt)
            if self.transient_left > 0 and "Implement this service" in prompt:
                self.transient_left -= 1
                raise LLMTransient("HTTP 429: rate limited (scripted)")
        if "project lead" in system:
            return self._lead(prompt)
        if "review software requirements" in system:
            return {"questions": ["Which regions must the service be deployed to?", "x"]}
        if "Implement this service" in prompt:
            return self._code(prompt)
        if "Write the automated test suite" in prompt:
            return {"files": [{"path": "tests/test_spec_api.py", "content": TESTS}]}
        if "Modify an EXISTING codebase" in prompt:
            if "Kind of change: bugfix" in prompt and self.real_bugfix:
                return {"summary": "count distinct visitors", "decisions": [], "risks": [], "new_files": [], "edits": [dict(BUGFIX_EDIT)]}
            if "Kind of change: refactor" in prompt or "Kind of change: bugfix" in prompt:
                return self._comment_only_change()
            return self._change()
        if "Write NEW unittest tests" in prompt and "Kind of change: bugfix" in prompt and self.real_bugfix \
                and self.bugfix_tests == "regression":
            return {"files": [{"path": "tests/test_change_repeat_visitor.py", "content": REGRESSION_TESTS}]}
        if "Write NEW unittest tests" in prompt and "Kind of change: enhancement" not in prompt:
            return {"files": [{"path": "tests/test_change_existing_behaviour.py", "content": EXISTING_BEHAVIOUR_TESTS}]}
        if "Write NEW unittest tests" in prompt:
            text = next(t for rel, t in template_files("expiry") if rel.endswith("test_expiry.py"))
            reply = {"files": [{"path": "tests/test_change_expiry.py", "content": text}]}
            if "Existing tests that failed after the change" in prompt and self.test_updates != "none":
                replace = PINNED_REPLACE if self.test_updates == "valid" else "pass"  # "weaken": drop the assertion
                reply["test_updates"] = [{"path": "tests/test_pinned_contract.py", "find": PINNED_FIND, "replace": replace,
                                          "reason": "the request body gained the optional expires_in_seconds field"}]
            return reply
        raise AssertionError("unexpected prompt: " + prompt[:80])

    def _code(self, prompt):
        n = self.code_calls
        self.code_calls += 1
        files = {}
        for rel, text in template_files("urlshortener"):
            if rel.startswith("shortener/"):
                files[rel] = render(text, _params(None))
        files["shortener/app.py"] += CREATE_APP_SHIM
        if n == 0 and self.drop_interface_first:
            files["shortener/app.py"] = files["shortener/app.py"].replace("def create_app", "def make_app")
        if n < len(self.code_mutations) and self.code_mutations[n]:
            self.code_mutations[n](files)
        return {"files": [{"path": p, "content": c} for p, c in files.items()]}

    def _change(self):
        n = self.change_calls
        self.change_calls += 1
        edits = [{"path": e.path, "find": e.find, "replace": e.replace} for e in EXPIRY_EDITS]
        if self.amend_openapi:
            edits.append(dict(OPENAPI_EDIT))
        if n == 0 and self.bad_anchor_first:
            edits[0]["find"] = "class DoesNotExist(Exception):"
        return {"summary": "Add optional expires_in_seconds; enforce at read time; 410 for expired.",
                "decisions": [{"title": "Absolute expires_at", "why": "survives caches and restarts", "tradeoffs": "no TTL edits"}],
                "risks": [{"risk": "Cached link served after expiry", "mitigation": "check expiry on the cached object"}],
                "edits": edits, "new_files": []}
