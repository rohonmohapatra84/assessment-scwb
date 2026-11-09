"""Validation agent: the quality gate between generation and human review.

Check families (each produces PASS/FAIL/WARN evidence, never a bare boolean):
  static        syntax (AST) of every python file, secret scan, required files
  contract      implemented routes == documented OpenAPI operations
  schema        schema.sql == tables produced by the real migrations (executed in a sandboxed subprocess)
  tests         full suite executed in an isolated subprocess (timeout, scrubbed env)
  regression    (brownfield) baseline suite still green and strictly grown by new tests
  compat        (brownfield) OpenAPI diff is additive only
  traceability  every acceptance criterion / risk maps to a test that really exists
  plan          (analysis) ambiguities resolved, every objective measurable and verifiable

Failures become findings with a `target` task id; the loop feeds them
back to that producer (repair loop) before escalating to a human.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Tuple

from ..harness.guardrails import check_test_amendment, robust_rmtree, run_unittests, scan_for_secrets
from ..harness.core import ValidationFailure
from .analysis import CodebaseAnalyst

SCHEMA_PROBE = r'''
import json, sqlite3, sys
sys.path.insert(0, ".")
from shortener.storage import Repository
def tables(conn):
    out = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"):
        out[name] = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % name)]
    return out
repo = Repository(":memory:")
ref = sqlite3.connect(":memory:")
ref.executescript(open("schema.sql", encoding="utf-8").read())
print(json.dumps({"migrated": tables(repo._conn), "schema_sql": tables(ref), "version": repo.schema_version()}))
'''


BEHAVIOUR_PROBE = r'''
import io, json, os, shutil, sqlite3, sys, tempfile
sys.path.insert(0, ".")
results = []
def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:160]})
def call(app, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else b""
    p, _, q = path.partition("?")
    env = {"REQUEST_METHOD": method, "PATH_INFO": p, "QUERY_STRING": q, "CONTENT_LENGTH": str(len(data)),
           "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(data), "REMOTE_ADDR": "203.0.113.9",
           "SERVER_NAME": "probe", "SERVER_PORT": "80", "wsgi.url_scheme": "http"}
    for k, v in (headers or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    cap = {}
    def sr(status, hdrs, exc_info=None):
        cap["s"] = int(status.split()[0]); cap["h"] = {k.lower(): v for k, v in hdrs}
    res = app(env, sr)
    out = b"".join(res)
    if hasattr(res, "close"): res.close()
    try: j = json.loads(out.decode()) if out else None
    except Exception: j = None
    return cap["s"], cap["h"], j
tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "probe.db"); tables = {}
try:
    from shortener.app import create_app
    app = create_app(db)
    s, h, j = call(app, "POST", "/api/v1/links", {"url": "https://example.com/probe"})
    check("create returns 201 with code and short_url", s == 201 and isinstance(j, dict) and j.get("code") and j.get("short_url"), (s, j))
    code = (j or {}).get("code", "missing")
    s, h, _ = call(app, "GET", "/" + code)
    check("redirect is 302 with the original Location", s == 302 and h.get("location") == "https://example.com/probe", (s, h.get("location")))
    s, h, j = call(app, "GET", "/nosuchcode1")
    check("unknown code is 404 with error body", s == 404 and isinstance(j, dict) and "error" in j, (s, j))
    s, _, j = call(app, "POST", "/api/v1/links", {"url": "ftp://example.com/x"})
    check("non-http scheme rejected with 400", s == 400, s)
    s, _, _ = call(app, "POST", "/api/v1/links", {"url": "http://127.0.0.1/admin"})
    check("loopback destination rejected with 400", s == 400, s)
    s, _, _ = call(app, "POST", "/api/v1/links", {"url": "https://example.com/a", "custom_alias": "probe-alias"})
    s2, _, _ = call(app, "POST", "/api/v1/links", {"url": "https://example.com/b", "custom_alias": "probe-alias"})
    check("custom alias created (201) then conflict (409)", s == 201 and s2 == 409, (s, s2))
    s, _, _ = call(app, "POST", "/api/v1/links", {"url": "https://example.com/a", "custom_alias": "x"})
    check("invalid alias rejected with 400", s == 400, s)
    s, _, j = call(app, "GET", "/api/v1/links/" + code)
    check("metadata returns 200 with code", s == 200 and isinstance(j, dict) and j.get("code") == code, (s, j))
    app.flush_analytics()
    s, _, j = call(app, "GET", "/api/v1/links/%s/stats?days=7" % code)
    check("stats returns 200 with numeric total_clicks >= 1", s == 200 and isinstance(j, dict) and isinstance(j.get("total_clicks"), int) and j["total_clicks"] >= 1, (s, j))
    s, _, _ = call(app, "GET", "/api/v1/links/%s/stats?days=500" % code)
    check("stats days out of range is 400", s == 400, s)
    s, _, _ = call(app, "GET", "/healthz")
    check("healthz is 200", s == 200, s)
    s, _, _ = call(app, "DELETE", "/api/v1/links/" + code)
    s2, _, _ = call(app, "GET", "/" + code)
    check("delete is 204 and the code then 404s", s == 204 and s2 == 404, (s, s2))
    s, h, _ = call(app, "GET", "/healthz")
    check("responses carry X-Request-Id", "x-request-id" in h, list(h))
    app.close()
    app2 = create_app(db)
    s, h, _ = call(app2, "GET", "/probe-alias")
    check("links survive restart (same db file)", s == 302 and h.get("location") == "https://example.com/a", (s, h.get("location")))
    app2.close()
except Exception as e:
    check("probe ran without crashing", False, repr(e))
try:
    conn = sqlite3.connect(db)
    for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        tables[n] = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % n)]
    conn.close()
except Exception as e:
    check("database readable", False, repr(e))
shutil.rmtree(tmp, ignore_errors=True)
print("PROBE_JSON=" + json.dumps({"results": results, "tables": tables}))
'''


def openapi_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    breaking: List[str] = []
    additive: List[str] = []
    for path, item in old.get("paths", {}).items():
        for method, op in item.items():
            nop = new.get("paths", {}).get(path, {}).get(method)
            if nop is None:
                breaking.append(f"removed operation {method.upper()} {path}")
                continue
            for status in op["responses"]:
                if status not in nop["responses"]:
                    breaking.append(f"removed response {status} from {method.upper()} {path}")
            for status in nop["responses"]:
                if status not in op["responses"]:
                    additive.append(f"new response {status} on {method.upper()} {path}")
    for path, item in new.get("paths", {}).items():
        for method in item:
            if method not in old.get("paths", {}).get(path, {}):
                additive.append(f"new operation {method.upper()} {path}")
    old_s, new_s = old.get("components", {}).get("schemas", {}), new.get("components", {}).get("schemas", {})
    for name, schema in old_s.items():
        if name not in new_s:
            breaking.append(f"removed schema {name}")
            continue
        o_props, n_props = schema.get("properties", {}), new_s[name].get("properties", {})
        for prop, spec in o_props.items():
            if prop not in n_props:
                breaking.append(f"removed property {name}.{prop}")
            elif n_props[prop].get("type") != spec.get("type"):
                breaking.append(f"type change {name}.{prop}: {spec.get('type')} -> {n_props[prop].get('type')}")
        for prop in n_props:
            if prop not in o_props:
                additive.append(f"new property {name}.{prop}")
        new_required = set(new_s[name].get("required", [])) - set(schema.get("required", []))
        if new_required and name.endswith("Request"):
            breaking.append(f"new required request field(s) in {name}: {sorted(new_required)}")
    return breaking, additive


def test_inventory(ws) -> Dict[str, Any]:
    """AST-scan tests/ -> {'stems': {...}, 'qualified': {'test_api::Class.method', 'test_api::method', ...}}"""
    stems, qualified = set(), set()
    for rel in ws.list_files(".py"):
        if not rel.startswith("tests/") or rel.endswith("__init__.py"):
            continue
        stem = rel.split("/")[-1][:-3]
        stems.add(stem)
        try:
            tree = ast.parse(ws.read(rel))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, ast.FunctionDef) and m.name.startswith("test"):
                        qualified |= {f"{stem}::{node.name}.{m.name}", f"{stem}::{m.name}"}
            elif isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                qualified.add(f"{stem}::{node.name}")
    return {"stems": stems, "qualified": qualified}


class Validator:
    name = "validator"

    def run(self, ctx) -> Dict[str, Any]:
        pt = ctx.requirement["plan_type"]
        code_task = {"greenfield": "code", "brownfield_change": "code", "llm_change": "code"}.get(pt)  # a ROLE: the harness maps it to the plan's task(s)
        llm_gen = ctx.requirement.get("generator") == "llm"
        checks: List[Dict[str, Any]] = []
        findings: List[Dict[str, Any]] = []

        def record(name: str, ok: bool, detail: str, target=None, severity: str = "error", warn_only: bool = False,
                   skip: bool = False):
            status = "SKIP" if skip else ("PASS" if ok else ("WARN" if warn_only else "FAIL"))
            ok = ok or skip
            checks.append({"check": name, "status": status, "detail": detail})
            if not ok:  # `target` may be a list: one finding per responsible producer
                for t in (target if isinstance(target, list) else [target]):
                    findings.append({"check": name, "severity": "warning" if warn_only else severity, "message": detail, "target": t})
            ctx.events.emit("validation_check", check=name, status=status, detail=detail[:140])

        if pt == "analysis":
            self._plan_checks(ctx, record)
        elif pt == "llm_change":
            kind = ctx.requirement.get("change_kind") or "enhancement"
            self._static(ctx, record, code_task)
            # enhancement/bugfix legitimately change probed behaviour: flag it. refactor/tests_docs must not: block it.
            self._probe(ctx, record, code_task, warn_only=kind in ("enhancement", "bugfix"))
            self._tests(ctx, record, code_task, pt, kind)
            self._compat_baseline(ctx, record, kind)
            self._test_amendments(ctx, record, kind)
            if kind == "bugfix":
                self._regression_proves_defect(ctx, record)
            if kind == "tests_docs":
                self._scope_tests_docs_only(ctx, record)
            self._docs(ctx, record)
        elif llm_gen:  # generative greenfield: judged black-box, because the structure of the code is not ours
            self._static(ctx, record, code_task)
            self._probe(ctx, record, code_task, warn_only=False)
            self._tests(ctx, record, code_task, pt)
            self._traceability(ctx, record)
            self._docs(ctx, record)
        else:
            self._static(ctx, record, code_task)
            self._contract(ctx, record, code_task)
            self._schema(ctx, record, code_task)
            self._tests(ctx, record, code_task, pt)
            self._traceability(ctx, record)
            if pt == "brownfield_change":
                self._compat(ctx, record)
            self._docs(ctx, record)
        report = {"plan_type": pt, "checks": checks, "findings": findings,
                  "passed": sum(c["status"] == "PASS" for c in checks), "failed": sum(c["status"] == "FAIL" for c in checks),
                  "warnings": sum(c["status"] == "WARN" for c in checks), "generation": ctx.task.generation}
        ctx.bb.set("validation_report", report)
        ctx.bb.append("validation_history", {"generation": ctx.task.generation, "failed": report["failed"],
                                             "failed_checks": [c["check"] for c in checks if c["status"] == "FAIL"]})
        errors = [f for f in findings if f["severity"] == "error"]
        if errors:
            raise ValidationFailure(f"{len(errors)} validation error(s): " + "; ".join(f["check"] for f in errors), findings)
        return report

    # ------------------------------------------------------------- checks
    def _static(self, ctx, record, code_task):
        bad_syntax, secrets = [], []
        for rel in ctx.ws.list_files():
            text = ctx.ws.read(rel)
            if rel.endswith(".py"):
                try:
                    ast.parse(text)
                except SyntaxError as exc:
                    bad_syntax.append(f"{rel}:{exc.lineno}")
            if scan_for_secrets(text):
                secrets.append(rel)
        n = len(ctx.ws.list_files(".py"))
        record("static.syntax", not bad_syntax, f"{n} python files parse" if not bad_syntax else f"syntax errors: {bad_syntax}", code_task)
        record("static.secrets", not secrets, "no secret-like strings" if not secrets else f"secret-like content in {secrets}", code_task)
        pt, llm = ctx.requirement["plan_type"], ctx.requirement.get("generator") == "llm"
        if pt == "llm_change":
            needed = []  # the baseline already satisfies this; edits are judged by tests and probes
        elif llm:
            needed = ["openapi.json", "schema.sql", "shortener/app.py", "shortener/server.py"]
        else:
            needed = ["openapi.json", "schema.sql", "shortener/app.py", "shortener/storage.py"]
        missing = [f for f in needed if not ctx.ws.exists(f)]
        record("static.required_files", not missing, "all required files present" if not missing else f"missing: {missing}", code_task)

    def _contract(self, ctx, record, code_task):
        model = CodebaseAnalyst().index(ctx.ws.root)
        implemented = {(r["method"], r["path"]) for r in model["routes"]}
        documented = {(o["method"], o["path"]) for o in ctx.bb.require("contract")["operations"]}
        missing, extra = sorted(documented - implemented), sorted(implemented - documented)
        ok = not missing and not extra
        detail = f"{len(implemented)} routes == {len(documented)} documented operations"
        if not ok:
            detail = "; ".join(filter(None, [
                f"documented but not implemented: {', '.join(f'{m} {p}' for m, p in missing)}" if missing else "",
                f"implemented but undocumented: {', '.join(f'{m} {p}' for m, p in extra)}" if extra else ""]))
        record("contract.routes_match_openapi", ok, detail, code_task)

    def _schema(self, ctx, record, code_task):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # a regenerated file with the same size+second would otherwise reuse a stale .pyc
        try:
            proc = subprocess.run([sys.executable, "-I", "-B", "-c", SCHEMA_PROBE], cwd=str(ctx.ws.root), env=env,
                                  capture_output=True, text=True, timeout=30)
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as exc:
            record("schema.parity", False, f"could not probe schema: {exc}; {getattr(locals().get('proc'), 'stderr', '')[-200:]}", code_task)
            return
        migrated = {t: sorted(c) for t, c in data["migrated"].items()}
        sqlfile = {t: sorted(c) for t, c in data["schema_sql"].items()}
        expected = {t: sorted(c) for t, c in ctx.bb.require("schema")["tables"].items()}
        ok = migrated == sqlfile == expected
        detail = f"schema.sql == migrations == design (v{data['version']}, {len(migrated)} tables)"
        if not ok:
            detail = f"schema drift: migrations={migrated} schema.sql={sqlfile} design={expected}"
        record("schema.parity", ok, detail, code_task)

    def _tests(self, ctx, record, code_task, pt, kind=None):
        if pt == "llm_change" and kind == "tests_docs":
            code_task = "tests"  # no production code is written in this plan, so the tests are what to repair
        run = run_unittests(ctx.ws.root, timeout=180)
        ctx.bb.set("test_run", {"ran": run.tests_run, "ok": run.ok, "seconds": round(run.seconds, 2)})
        # With a generative backend either side may be wrong: after the first repair round, blame both producers.
        llm = ctx.requirement.get("generator") == "llm"
        blame = [code_task, "tests"] if llm and ctx.task.generation >= 1 and pt != "llm_change" else code_task
        fails = re.findall(r"^(?:FAIL|ERROR): (.+)$", run.output, re.M) if not run.ok else []
        if pt == "llm_change" and kind != "tests_docs" and fails:
            # Blame follows the evidence. A failing EXISTING test is the code's. A failing NEW test (test_change_*) could be
            # either side: the test was written from the published contract, so it may be wrong, or the code may not meet
            # its own contract. The harness cannot tell which, so it blames BOTH and the contract arbitrates. Blaming only the
            # tests meant a real code bug could never be sent back; blaming only the code meant a wrong test never was.
            new_failing = any("test_change_" in f for f in fails)
            # An EXISTING test may also fail because the intended contract change made it obsolete (a test that pins the whole
            # OpenAPI document). Only an enhancement whose contract really changed may amend such a test, so only then is
            # the tests producer blamed as well; otherwise a failing existing test is the code's.
            may_amend = kind == "enhancement" and self._contract_changed(ctx)
            blame = [code_task, "tests"] if (new_failing or may_amend) else code_task
        if run.ok:
            record("tests.suite", True, f"{run.tests_run} tests passed in {run.seconds:.1f}s", code_task)
        else:
            tail = "\n".join(run.output.strip().splitlines()[-40:]) if llm else "\n".join(run.output.strip().splitlines()[-12:])
            record("tests.suite", False, f"{run.tests_run} ran; failing: {fails[:10] or 'collection/import error'}\n{tail}", blame)
        if pt in ("brownfield_change", "llm_change"):
            base = ctx.bb.get("baseline_tests") or {}
            grew = run.tests_run > base.get("ran", 0)
            if kind == "bugfix":  # the defect may itself have turned the baseline red: the proof is a green suite afterwards
                record("regression.suite_green_after_fix", run.ok,
                       f"suite was {'green' if base.get('ok') else 'RED (the defect)'} before the fix ({base.get('ran')} tests); "
                       f"now {'green' if run.ok else 'RED'}", blame)
            else:
                record("regression.baseline_green", run.ok and base.get("ok", False),
                       f"baseline suite ({base.get('ran')} tests) was green before; suite now {'green' if run.ok else 'RED'}", blame)
            if kind in (None, "enhancement", "bugfix"):  # new behaviour or a fixed defect must come with new tests
                record("regression.new_tests_added", grew, f"suite grew {base.get('ran')} -> {run.tests_run} tests", "tests")
            else:  # refactor / tests_docs: nothing may be lost
                record("regression.suite_not_smaller", run.tests_run >= base.get("ran", 0),
                       f"suite {base.get('ran')} -> {run.tests_run} tests", "tests")

    def _traceability(self, ctx, record):
        inv = test_inventory(ctx.ws)
        missing = []
        for ac, ref in (ctx.bb.get("ac_tests") or {}).items():
            stem, _, name = ref.partition("::")
            key = f"{stem}::{name.split('.')[-1]}"
            if key not in inv["qualified"]:
                missing.append(f"{ac}->{ref}")
        record("traceability.acceptance_criteria", not missing,
               f"{len(ctx.bb.get('ac_tests') or {})} acceptance criteria map to existing tests" if not missing else f"no such test: {missing}",
               "tests")
        bad = []
        risks = (ctx.bb.get("risks") or {}).get("domain", [])
        if ctx.requirement.get("generator") == "llm":
            risks = []  # risk catalogue names template tests; generative suites are traced through test_ac<N>_ names instead
        checked = 0
        for r in risks:
            ref = r.get("covered_by", "")
            if re.fullmatch(r"test_\w+(::test_\w+)?", ref):
                checked += 1
                stem, _, name = ref.partition("::")
                if (name and ref not in inv["qualified"]) or (not name and stem not in inv["stems"]):
                    bad.append(f"{r['id']}->{ref}")
        record("traceability.risks", not bad, f"{checked} test-covered risks resolve to real tests" if not bad else f"risk coverage points at missing tests: {bad}", "tests")

    def _compat(self, ctx, record):
        base = ctx.bb.require("contract_baseline")["spec"]
        new = json.loads(ctx.ws.read("openapi.json"))
        breaking, additive = openapi_diff(base, new)
        ctx.bb.set("api_diff", {"breaking": breaking, "additive": additive})
        record("compat.openapi_additive_only", not breaking,
               f"{len(additive)} additive change(s), 0 breaking" if not breaking else f"BREAKING: {breaking}", "contract")

    def _probe(self, ctx, record, code_task, warn_only):
        """Black-box behavioural conformance: drive the public WSGI interface through the documented flows in a
        sandboxed subprocess, then compare the tables the code actually created with the designed schema."""
        if not ctx.ws.exists("shortener/app.py"):
            record("probe.behaviour", False, "shortener/app.py missing", code_task, warn_only=warn_only)
            return
        # A change to EXISTING code is judged against what it already is: if the codebase never exposed the interface the
        # probe drives, the probe does not apply and the baseline regression suite carries the proof instead.
        changing_existing = ctx.requirement.get("plan_type") == "llm_change"
        if (warn_only or changing_existing) and "def create_app" not in ctx.ws.read("shortener/app.py"):
            record("probe.behaviour", True, "skipped: this codebase does not expose the create_app interface the probe drives",
                   skip=True)
            return
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # a regenerated file with the same size+second would otherwise reuse a stale .pyc
        try:
            proc = subprocess.run([sys.executable, "-I", "-B", "-c", BEHAVIOUR_PROBE], cwd=str(ctx.ws.root), env=env,
                                  capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            record("probe.behaviour", False, "behaviour probe timed out (90s): deadlock or blocking call on the request path", code_task, warn_only=warn_only)
            return
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_JSON=")), None)
        if not line:
            record("probe.behaviour", False, "probe could not run (import/start-up error): " + (proc.stderr or proc.stdout)[-600:], code_task, warn_only=warn_only)
            return
        data = json.loads(line[len("PROBE_JSON="):])
        failed = [r for r in data["results"] if not r["ok"]]
        record("probe.behaviour", not failed,
               f"{len(data['results'])} black-box probes passed" if not failed else
               "failed probes: " + "; ".join(f"{r['name']} ({r['detail']})" for r in failed), code_task, warn_only=warn_only)
        expected = (ctx.bb.get("schema") or {}).get("tables", {})
        actual = data["tables"]
        gaps = [f"{t}.{c}" for t, cols in expected.items() for c in cols if c not in actual.get(t, [])]
        if expected:
            record("probe.schema_conformance", not gaps,
                   f"the database the code creates contains every designed table and column ({len(expected)} tables)" if not gaps
                   else f"designed columns missing from the created database: {gaps[:8]}", code_task, warn_only=warn_only)

    def _compat_baseline(self, ctx, record, kind=None):
        try:
            base = json.loads(ctx.ws.read_baseline("openapi.json"))
            new = json.loads(ctx.ws.read("openapi.json"))
        except (FileNotFoundError, ValueError) as exc:
            record("compat.openapi_additive_only", False, f"openapi.json unreadable: {exc}", "code")
            return
        breaking, additive = openapi_diff(base, new)
        ctx.bb.set("api_diff", {"breaking": breaking, "additive": additive})
        if kind in ("refactor", "tests_docs"):  # behaviour-preserving: the public API must be IDENTICAL, additions too
            changed = breaking + additive
            record("compat.openapi_unchanged", not changed,
                   "OpenAPI identical to the baseline" if not changed else f"API changed during a {kind}: {changed}", "code")
            return
        record("compat.openapi_additive_only", not breaking,
               f"{len(additive)} additive change(s), 0 breaking" if not breaking else f"BREAKING: {breaking}", "code")

    def _regression_proves_defect(self, ctx, record):
        """A bug-fix regression test is only evidence if it FAILS on the old (buggy) code. Run the new tests against a copy
        of the pristine baseline: at least one must fail by an assertion, not by an import error."""
        import shutil
        import tempfile
        from pathlib import Path
        new_tests = [r for r in ctx.ws.list_files(".py") if r.startswith("tests/test_change_")]
        if ctx.ws.baseline_root is None or not new_tests:
            record("regression.fails_on_old_code", False, "no new regression test to run against the old code", "tests")
            return
        tmp = Path(tempfile.mkdtemp(prefix="oldcode_"))
        try:
            shutil.copytree(ctx.ws.baseline_root, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for rel in new_tests:
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ctx.ws.root / rel, dest)
            run = run_unittests(tmp, timeout=180)
            fails = re.findall(r"^(?:FAIL|ERROR): (.+)$", run.output, re.M)
            proving = [f for f in fails if "test_change_" in f and "_FailedTest" not in f]
            broken = [f for f in fails if "test_change_" in f and "_FailedTest" in f]
        finally:
            robust_rmtree(tmp)
        ctx.bb.set("old_code_run", {"ran": run.tests_run, "failing_new_tests": len(proving)})
        if proving:
            record("regression.fails_on_old_code", True,
                   f"{len(proving)} new test(s) fail on the unfixed baseline and pass on the fix: the tests demonstrate the defect")
        elif broken:
            record("regression.fails_on_old_code", False,
                   f"the new tests could not even load against the old code ({broken[:2]}); they must import only what the old code provides", "tests")
        else:
            record("regression.fails_on_old_code", False,
                   "every new test also PASSES on the old, buggy code, so none of them demonstrates the defect; "
                   "write a test that asserts the correct behaviour the defect violates", "tests")

    @staticmethod
    def _contract_changed(ctx) -> bool:
        try:
            breaking, additive = openapi_diff(json.loads(ctx.ws.read_baseline("openapi.json")), json.loads(ctx.ws.read("openapi.json")))
        except (FileNotFoundError, ValueError):
            return False
        return bool(breaking or additive)

    def _test_amendments(self, ctx, record, kind):
        """The approved-test-update policy, enforced from the disk. Any baseline test that differs from the pristine
        baseline must be (1) in an enhancement, (2) justified by a real contract change, (3) a recorded amendment with a
        reason, and (4) no weaker than the original. Anything else is a violation, whoever wrote it."""
        changed = ctx.ws.modified_baseline_tests()
        recorded = {u["path"]: u for u in (ctx.bb.get("test_updates") or [])}
        if not changed:
            record("tests.existing_tests_protected", True, "no existing test was modified")
            return
        problems = []
        if kind != "enhancement":
            problems.append(f"a {kind} may not modify existing tests: {changed}")
        elif not self._contract_changed(ctx):
            problems.append(f"existing tests changed but the OpenAPI contract did not: {changed}")
        for rel in changed:
            if rel not in recorded:
                problems.append(f"{rel} was modified without a recorded, reasoned amendment")
                continue
            if not recorded[rel].get("reason"):
                problems.append(f"{rel}: amendment has no reason")
            try:
                problems += check_test_amendment(ctx.ws.read_baseline(rel), ctx.ws.read(rel), rel)
            except FileNotFoundError:
                problems.append(f"{rel}: baseline test was deleted")
        record("tests.existing_tests_protected", not problems,
               f"{len(changed)} existing test file(s) amended, each justified by the contract change and awaiting human approval: {changed}"
               if not problems else "; ".join(problems), ["tests"])

    def _scope_tests_docs_only(self, ctx, record):
        """A tests/docs improvement must leave production code alone. The manifest says which files this run wrote."""
        written = [rel for rel, e in ctx.ws.manifest.items() if e.producer != "baseline"]
        bad = [p for p in written if not (p.startswith(("tests/", "docs/")) or p in ("README.md", "CHANGELOG.md", "changes.diff"))]
        record("scope.tests_docs_only", not bad, f"{len(written)} file(s) written, all tests or docs" if not bad
               else f"production files changed during a tests/docs improvement: {bad}", "code")

    def _docs(self, ctx, record):
        pt = ctx.requirement["plan_type"]
        needed = ["docs/CHANGE.md"] if pt == "llm_change" else ["README.md"] + (["docs/ARCHITECTURE.md", "docs/ADR.md"] if pt == "greenfield" else ["CHANGELOG.md"])
        missing = [f for f in needed if not ctx.ws.exists(f)]
        record("docs.present", not missing, "documentation present" if not missing else f"missing docs (degraded): {missing}", None, warn_only=True)

    def _plan_checks(self, ctx, record):
        req = ctx.requirement
        opts = ctx.bb.get("options") or {}
        unresolved = [a["id"] for a in req["ambiguities"] if a["id"] not in req["resolved_by"] and req["was_ambiguous"]]
        record("plan.ambiguities_resolved", not unresolved, "every blocking ambiguity has a human answer or a recorded default"
               if not unresolved else f"unresolved: {unresolved}", "options_designer")
        objectives = opts.get("objectives", [])
        unmeasurable = [o["facet"] for o in objectives if not o.get("metric") or o.get("target") in (None, "", "unspecified", "to be defined")]
        record("plan.objectives_measurable", not unmeasurable, f"{len(objectives)} objectives each have a metric and a target"
               if not unmeasurable else f"objectives without measurable target: {unmeasurable}", "options_designer")
        has_opts = [o["facet"] for o in objectives if len(o.get("options", [])) >= 1]
        record("plan.options_with_tradeoffs", len(has_opts) == len(objectives) and bool(objectives), "each objective has evaluated options with trade-offs",
               "options_designer")
        verified = ctx.bb.get("validation_strategy") or {}
        record("plan.verification_defined", bool(verified.get("layers")), f"{len(verified.get('layers', []))} verification layers defined", "options_designer")
        doc_ok = ctx.ws.exists("docs/PROPOSAL.md")
        record("docs.proposal_present", doc_ok, "docs/PROPOSAL.md present" if doc_ok else "proposal document missing (degraded)", None, warn_only=True)


class Summarizer:
    """Aggregates evidence from the run into one structure; the pipeline renders it after the human decision."""
    name = "summarizer"

    def run(self, ctx) -> Dict[str, Any]:
        manifest = {rel: {"kind": e.kind, "bytes": e.bytes, "producer": e.producer, "sha256": e.sha256[:12]}
                    for rel, e in sorted(ctx.ws.manifest.items())}
        data = {"artifact_count": len(manifest), "artifacts": manifest,
                "by_kind": self._count(manifest), "validation": ctx.bb.get("validation_report"),
                "tests": ctx.bb.get("test_run"), "risks": ctx.bb.get("risks"), "api_diff": ctx.bb.get("api_diff"),
                "patch": ctx.bb.get("patch_stats")}
        ctx.bb.set("summary_data", data)
        return {"artifacts": len(manifest)}

    @staticmethod
    def _count(manifest) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in manifest.values():
            out[e["kind"]] = out.get(e["kind"], 0) + 1
        return out
