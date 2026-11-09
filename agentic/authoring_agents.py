"""Python behind the two code-writing agents defined in agentic/agents/*.md (any model provider with
`complete_json`):

  CodeAuthor      agents/code_author.md       writes a new service (greenfield) or proposes anchored edits (llm_change)
  SpecTester   agents/spec_tester.md  writes the test suite, or the tests for a change, from the SPEC ONLY

The Markdown file owns the role, guardrails and system prompt; this module owns what must stay deterministic:
building the prompt, parsing and bounding the model's reply, and acting only through ctx.write (sandbox, write scopes,
extension allow-list, secret scan). Output is then judged by the Validator (AST, black-box behavioural probe, executed
tests, regression, API diff). Model output is untrusted: it is parsed defensively, path-filtered, size-capped,
syntax-checked and rejected with feedback when wrong.
"""
from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .agent_specs import load_context, load_spec
from .harness.core import FatalError, TransientError, ValidationFailure
from .harness.guardrails import check_test_amendment, scan_for_secrets
from .providers import LLMTransient, LLMUnavailable
from .steps.build import _params, render, template_files

MAX_FILES = 16
MAX_FILE_CHARS = 60_000

INTERFACE_SPEC = load_context("url_shortener_interface") + "\n"

# What each KIND of change to existing code means. The harness checks it too (steps/validate.py): a refactor must leave the
# API identical, a tests/docs change may not touch production code. The prompt tells the model; the validator holds it to it.
KIND_GUIDANCE = {
    "enhancement": "Add the requested behaviour. API and schema changes must be additive only.",
    "bugfix": "Make the smallest change that fixes the defect. Do not alter unrelated behaviour. API changes additive only.",
    "refactor": "Behaviour-preserving restructuring ONLY. The public API (OpenAPI), the database schema and all observable "
                "behaviour must stay identical. No new features.",
    "tests_docs": "No production code changes. Add tests for existing behaviour that is currently untested; every new test "
                  "must pass on the unchanged code.",
}
TEST_GUIDANCE = {
    "enhancement": "Cover the new behaviour, its boundaries and its failure modes.",
    "bugfix": "Write a regression test that demonstrates the defect (it must fail on the old code and pass once fixed), "
              "plus tests for neighbouring behaviour that must not change.",
    "refactor": "Write characterization tests that pin the current behaviour of the code being restructured; they must "
                "pass both before and after.",
    "tests_docs": KIND_GUIDANCE["tests_docs"],
}
MIN_TESTS = {"enhancement": 8, "bugfix": 3, "refactor": 5, "tests_docs": 5}

# A live link-expiry run failed three repair rounds in a row on tests that pinned wording the contract never specifies (an exact
# error message, an exact response description): the code was right, and every rewrite moved the prose again. Tests judge
# behaviour, so they may assert only on what the contract states.
PROSE_RULE = ("* Assert ONLY on what the contract states: status codes, field names, types, bounds, and the machine-readable error `code`. "
              "NEVER assert on human-readable prose: an error `message`, a response or field `description`, a summary. The contract does not "
              "fix that wording, so a correct implementation may phrase it differently (for example with a trailing full stop). Check "
              "`error.code` and the status instead. If a failure you are shown is only a wording difference, relax that assertion; do not copy the wording.\n")


def _kind(req: Dict[str, Any]) -> str:
    return req.get("change_kind") or "enhancement"


def _kind_line(req: Dict[str, Any], tests: bool = False) -> str:
    return f"Kind of change: {_kind(req)}. {(TEST_GUIDANCE if tests else KIND_GUIDANCE)[_kind(req)]}\n"  # shared prompt fragment: agents/context/url_shortener_interface.md


def _call(ctx, agent: str, prompt: str, max_tokens: int) -> Dict[str, Any]:
    """Call the model with `agent`'s system prompt, taken from its Markdown spec (agents/<agent>.md)."""
    started = time.time()
    try:
        out = ctx.llm.complete_json(load_spec(agent).prompt(), prompt, max_tokens=max_tokens)
    except LLMTransient as exc:
        raise TransientError(f"LLM transient failure: {exc}") from exc
    except LLMUnavailable as exc:
        raise FatalError(f"LLM unavailable: {exc}") from exc
    ctx.events.emit("llm_call", task=ctx.task.id, provider=ctx.llm.name, seconds=round(time.time() - started, 1),
                    **{k: v for k, v in (getattr(ctx.llm, "usage", {}) or {}).items()})
    return out


def _feedback_block(ctx, current_files: Dict[str, str] = None) -> str:
    if not ctx.feedback:
        return ""
    msgs = "\n".join(f"- [{f.get('check', 'finding')}] {f.get('message', '')}" for f in ctx.feedback)[:7000]
    block = f"\n\n## PREVIOUS ATTEMPT FAILED VALIDATION - fix exactly these problems\n{msgs}\n"
    if current_files:
        budget, parts = 45_000, []
        for p, c in current_files.items():
            if budget <= 0:
                break
            parts.append(f"### {p}\n{c[:budget]}")
            budget -= len(c)
        block += "\n## YOUR PREVIOUS FILES (return corrected COMPLETE files)\n" + "\n\n".join(parts)
    return block


def _secret_problem(path: str, content: str) -> str:
    """The same scan the sandbox applies on write, run BEFORE writing so the model gets feedback and retries
    instead of the task dying on a GuardrailViolation. Returns '' when clean."""
    leaks = scan_for_secrets(content)
    if not leaks:
        return ""
    return (f"{path}: contains secret-like text ({', '.join(leaks)}). Never write credentials. The scan flags a quoted value of 8 or more "
            f"characters assigned to a name containing password, secret, api_key or token (e.g. token = \"abcdefgh12\"). "
            f"Rename that variable (for example 'code' or 'value') or use a short placeholder under 8 characters")


def _validate_files(raw: Any, path_re: str, label: str) -> List[Tuple[str, str]]:
    problems: List[str] = []
    if not isinstance(raw, list) or not raw:
        raise ValidationFailure(f"{label}: reply must contain a non-empty 'files' list",
                                [{"check": "llm.shape", "message": "reply must be {\"files\":[{\"path\":...,\"content\":...}]}"}])
    out: List[Tuple[str, str]] = []
    for item in raw[:MAX_FILES + 1]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            problems.append("each file needs string 'path' and 'content'")
            continue
        path, content = item["path"].replace("\\", "/"), item["content"]
        if not re.fullmatch(path_re, path):
            problems.append(f"path {path!r} not allowed (must match {path_re})")
        elif len(content) > MAX_FILE_CHARS:
            problems.append(f"{path} exceeds {MAX_FILE_CHARS} characters")
        elif "\x00" in content:
            problems.append(f"{path}: contains a null byte (\\x00). Return plain text only, with no NUL characters")
        else:
            if path.endswith(".py"):
                try:
                    ast.parse(content)
                except (SyntaxError, ValueError) as exc:  # Python < 3.12 raises ValueError, not SyntaxError, for source it cannot read
                    problems.append(f"{path}: SyntaxError line {getattr(exc, 'lineno', '?')}: {getattr(exc, 'msg', exc)}")
            secret = _secret_problem(path, content)
            if secret:
                problems.append(secret)
            out.append((path, content))
    if len(raw) > MAX_FILES:
        problems.append(f"too many files (max {MAX_FILES})")
    if problems:
        raise ValidationFailure(f"{label}: {len(problems)} problem(s)", [{"check": "llm.output", "message": m} for m in problems])
    return out


# ------------------------------------------------------------------ greenfield ---
class _ServiceWriter:
    """CodeAuthor, greenfield: write the whole service from the contract and schema."""

    def _write_service(self, ctx) -> Dict[str, Any]:
        req = ctx.requirement
        ctx.bb.require("contract")
        spec = ctx.ws.read("openapi.json")
        schema = ctx.ws.read("schema.sql")
        design = ctx.bb.get("design") or {}
        adrs = "\n".join(f"- {a['id']} {a['decision']}: {a['why']}" for a in design.get("adrs", []))
        current = {}
        if ctx.feedback:
            current = {p: ctx.ws.read(p) for p in ctx.ws.list_files(".py") if p.startswith("shortener/")}
        prompt = (f"# Task\nImplement this service.\nRequirement: {req['raw']}\nProblem: {req['problem_statement']}\n\n"
                  f"## Architecture decisions to follow\n{adrs}\n\n## Interface and engineering requirements\n{INTERFACE_SPEC}\n"
                  f"## OpenAPI contract (source of truth)\n{spec}\n\n## SQL schema (source of truth)\n{schema}\n\n"
                  "## Output\nJSON: {\"files\":[{\"path\":\"shortener/<module>.py\",\"content\":\"...\"}, ...]}. "
                  "Allowed paths: shortener/*.py only (at most 14 modules, e.g. errors, models, config, validation, cache, "
                  "ratelimit, analytics, storage, service, app, server, __init__)."
                  + _feedback_block(ctx, current))
        raw = _call(ctx, self.name, prompt, max_tokens=24000)
        files = _validate_files(raw.get("files"), r"shortener/[A-Za-z0-9_]+\.py", "code")
        by_path = dict(files)
        problems = []
        if "shortener/app.py" not in by_path or not re.search(r"def\s+create_app\s*\(", by_path.get("shortener/app.py", "")):
            problems.append("shortener/app.py must define create_app(db_path=..., *, base_url, rate_limit_per_minute, api_keys, redirect_status)")
        if "shortener/server.py" not in by_path or not re.search(r"def\s+main\s*\(", by_path.get("shortener/server.py", "")):
            problems.append("shortener/server.py must define main(argv=None)")
        if "flush_analytics" not in by_path.get("shortener/app.py", "") and not any("flush_analytics" in c for c in by_path.values()):
            problems.append("the app object returned by create_app must expose flush_analytics() and close()")
        if problems:
            raise ValidationFailure("code: interface not satisfied", [{"check": "llm.interface", "message": m} for m in problems])
        by_path.setdefault("shortener/__init__.py", '"""URL shortener."""\n')
        for p, c in sorted(by_path.items()):
            ctx.write(p, c, "code")
        extras = []
        for rel, text in template_files("urlshortener"):  # deterministic packaging: no tokens spent, nothing to hallucinate
            if rel in ("Dockerfile", "pyproject.toml", ".gitignore"):
                ctx.write(rel, render(text, _params(ctx)), "config")
                extras.append(rel)
        return {"files": sorted(by_path), "extras": extras, "generated_by": f"{ctx.llm.name}"}


class _ServiceTester:
    """SpecTester, greenfield: write the full test suite from the spec only."""

    def _write_service_tests(self, ctx) -> Dict[str, Any]:
        req = ctx.requirement
        spec = ctx.ws.read("openapi.json")
        support = {rel: render(text, _params(ctx)) for rel, text in template_files("llm_support")}
        for rel, content in support.items():
            ctx.write(rel, content, "test")
        acs = req["acceptance_criteria"]
        strategy = ctx.bb.get("validation_strategy") or {}
        current = {}
        if ctx.feedback:
            current = {p: ctx.ws.read(p) for p in ctx.ws.list_files(".py") if re.fullmatch(r"tests/test_[a-z0-9_]+\.py", p)}
        prompt = (f"# Task\nWrite the automated test suite (unittest) for this service from the SPEC ONLY. You have not seen the "
                  f"implementation and must not assume internals: test through the public interface below.\n\n"
                  f"## Interface\n{INTERFACE_SPEC}\n## Acceptance criteria\n" + "\n".join(acs) +
                  f"\n\n## OpenAPI contract\n{spec}\n\n## Test helper you MUST use (already provided as tests/wsgi_client.py)\n"
                  f"{support['tests/wsgi_client.py']}\n"
                  f"## Verification strategy layers\n{json.dumps([l['layer'] + ': ' + l['scope'] for l in strategy.get('layers', [])])}\n\n"
                  "## Rules\n* Build the app with `from shortener.app import create_app; app = create_app(\":memory:\")` in setUp; call "
                  "`app.close()` in tearDown. Use `Client(app)` from tests.wsgi_client (`from .wsgi_client import Client`).\n"
                  "* Click analytics are asynchronous: call `app.flush_analytics()` before asserting stats.\n"
                  "* For persistence use a temp file db_path (tempfile.TemporaryDirectory), close the app, create a new app on the same path.\n"
                  "* Name at least one test per acceptance criterion `test_ac<N>_<what>` (N = 1..%d, e.g. test_ac2_redirects_with_302)."
                  " Add more tests for edge cases (invalid input, 404/405, auth, rate limit with rate_limit_per_minute=2, "
                  "private-host URLs, CRLF in URL, days out of range, malformed JSON, oversized body, delete then redirect).\n"
                  "* Deterministic: no sleeps, no network, no ports.\n"
                  "* 3 to 5 files named tests/test_<topic>.py, 35+ tests total.\n"
                  "## Output\nJSON: {\"files\":[{\"path\":\"tests/test_<topic>.py\",\"content\":\"...\"}]}" % len(acs)
                  + _feedback_block(ctx, current))
        raw = _call(ctx, self.name, prompt, max_tokens=24000)
        files = _validate_files(raw.get("files"), r"tests/test_[a-z0-9_]+\.py", "tests")
        # every acceptance criterion must have a named test (verified here AND again by the validator)
        names: Dict[str, str] = {}
        total = 0
        for path, content in files:
            stem = Path(path).stem
            for m in re.finditer(r"def\s+(test_ac(\d+)_\w*)\s*\(", content):
                names.setdefault(f"AC-{m.group(2)}", f"{stem}::{m.group(1)}")
            total += len(re.findall(r"def\s+test_\w+\s*\(", content))
        missing = [a.split()[0] for a in acs if a.split()[0] not in names]
        if missing or total < 20:
            raise ValidationFailure("tests: coverage rules not met", [{"check": "llm.tests", "message":
                                    f"missing test_ac<N>_ tests for {missing}; total tests {total} (need >= 20)"}])
        for path, content in files:
            ctx.write(path, content, "test")
        ctx.bb.set("ac_tests", names)
        return {"files": [p for p, _ in files], "tests": total}


# ------------------------------------------------------------------ brownfield ---
class _ChangeProposer:
    """CodeAuthor, brownfield: propose anchored edits (find/replace, each `find` must match exactly once)."""

    def _propose_change(self, ctx) -> Dict[str, Any]:
        req, impact = ctx.requirement, ctx.bb.require("impact")
        baseline_files = [p for p in ctx.ws.list_files() if not p.startswith("tests/") and p.endswith((".py", ".json", ".sql"))]
        # Show the model what must change PLUS the modules those import (one hop): e.g. the shared error types a new
        # behaviour needs. Bounded so the prompt stays small; everything else is listed by name only.
        graph = (ctx.bb.get("codebase_model") or {}).get("import_graph", {})
        stems = {s for m in impact["modules"] for s in graph.get(m, [])}
        hop = [p for p in baseline_files if p.endswith(".py") and Path(p).stem in stems]
        focus = [m for m in impact["modules"] if m in baseline_files] + hop + [p for p in ("openapi.json", "schema.sql") if p in baseline_files]
        # Static analysis is a hint, not a gate. It recognises one idiom (decorator routes, SQL in string literals); a service
        # built another way (a model-written one dispatches with `if path.startswith(...)`) yields no routes or tables, and
        # the author would be shown almost nothing and could only invent new files. Then show every module that fits.
        analysis_blind = not impact.get("routes") and not impact.get("tables")
        if analysis_blind:
            priority = ("app", "service", "storage", "validation", "models", "errors", "config")
            stem = lambda p: Path(p).stem
            focus += sorted((p for p in baseline_files if p.endswith(".py")),
                            key=lambda p: (stem(p) not in priority, priority.index(stem(p)) if stem(p) in priority else 0, p))
        blind_note = ("(The analysis recognised no routes or tables in this code, so every module that fits is shown.)\n"
                      if analysis_blind else "")
        sources, budget = {}, 80_000
        for p in dict.fromkeys(focus):
            text = ctx.ws.read_baseline(p)
            if len(text) <= budget:
                sources[p], budget = text, budget - len(text)
        others = [p for p in baseline_files if p not in sources]
        answers = (ctx.bb.get("input", {}).get("answers") or {})
        prompt = (f"# Task\nModify an EXISTING codebase to satisfy this change request.\nRequest: {req['raw']}\n" + _kind_line(req)
                  + (f"Clarifications from the human: {json.dumps(answers)}\n" if answers else "")
                  + f"Impact analysis (from AST): routes={impact['routes']} tables={impact['tables']} modules to change={impact['modules']}\n"
                  f"{blind_note}Other files (not shown, do not edit): {others}\n\n"
                  "## Current source of the files you may edit\n" + "\n\n".join(f"### {p}\n{c}" for p, c in sources.items()) +
                  "\n\n## Rules\n* Return MINIMAL anchored edits: {\"path\",\"find\",\"replace\"}. `find` must be an exact, "
                  "multi-line-safe substring that occurs EXACTLY ONCE in the ORIGINAL file shown above (include enough surrounding "
                  "context to be unique). Edits to one file are applied in order.\n* Be backward compatible: existing behaviour, "
                  "data and API clients must keep working; schema changes must be additive via a new versioned migration; API "
                  "changes additive only; update openapi.json and schema.sql consistently, and describe every new or changed request/response "
                  "field in openapi.json precisely (exact name, type, bounds, default, meaning): the tests are written from the contract alone.\n"
                  "* You may add new modules under shortener/ "
                  "via `new_files`. Do NOT edit tests (they are generated separately).\n"
                  "## Output JSON\n{\"summary\":\"...\",\"decisions\":[{\"title\":\"\",\"why\":\"\",\"tradeoffs\":\"\"}],"
                  "\"risks\":[{\"risk\":\"\",\"mitigation\":\"\"}],\"edits\":[{\"path\":\"\",\"find\":\"\",\"replace\":\"\"}],"
                  "\"new_files\":[{\"path\":\"shortener/x.py\",\"content\":\"\"}]}"
                  + _feedback_block(ctx))
        raw = _call(ctx, self.name, prompt, max_tokens=16000)
        edits = raw.get("edits") if isinstance(raw.get("edits"), list) else []
        if not edits and not raw.get("new_files"):
            raise ValidationFailure("change: no edits proposed", [{"check": "llm.change", "message": "reply contained no edits or new_files"}])
        contents = {p: c for p, c in sources.items()}
        problems: List[str] = []
        for i, e in enumerate(edits[:60]):
            if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("path", "find", "replace")):
                problems.append(f"edit {i}: needs string path/find/replace")
                continue
            path = e["path"].replace("\\", "/")
            if path not in contents:
                problems.append(f"edit {i}: {path!r} is not an editable file (editable: {sorted(contents)})")
                continue
            n = contents[path].count(e["find"])
            if not e["find"] or n != 1:
                problems.append(f"edit {i} in {path}: `find` matched {n} times (must be exactly 1): {e['find'][:80]!r}")
                continue
            contents[path] = contents[path].replace(e["find"], e["replace"], 1)
        new_files = (_validate_files(raw["new_files"], r"shortener/[A-Za-z0-9_]+\.py", "new_files")
                     if raw.get("new_files") else [])
        new_files = [(p, c) for p, c in new_files if p not in contents]  # never silently overwrite an existing module
        for p, c in contents.items():
            secret = _secret_problem(p, c)
            if secret:
                problems.append(secret)
            if p.endswith(".py"):
                try:
                    ast.parse(c)
                except (SyntaxError, ValueError) as exc:
                    problems.append(f"{p}: result has SyntaxError line {getattr(exc, 'lineno', '?')}: {getattr(exc, 'msg', exc)}")
            if p.endswith(".json"):
                try:
                    json.loads(c)
                except ValueError as exc:
                    problems.append(f"{p}: result is not valid JSON: {exc}")
        if problems:
            raise ValidationFailure(f"change: {len(problems)} problem(s)", [{"check": "llm.change", "message": m} for m in problems])
        import difflib
        diff_parts, changed = [], []
        for p, new in contents.items():
            old = sources[p]
            if new != old:
                ctx.write(p, new, "code" if p.endswith(".py") else "contract" if p.endswith(".json") else "schema")
                changed.append(p)
                diff_parts.append("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{p}", f"b/{p}")))
        for p, c in new_files:
            ctx.write(p, c, "code")
            changed.append(p)
            diff_parts.append("".join(difflib.unified_diff([], c.splitlines(True), "/dev/null", f"b/{p}")))
        diff = "\n".join(diff_parts)
        ctx.write("changes.diff", diff, "diff")
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        clean = lambda items, keys: [{k: str(x.get(k, ""))[:400] for k in keys} for x in items if isinstance(x, dict)][:8]
        ctx.bb.set("change_design", {
            "summary": str(raw.get("summary", ""))[:600], "decisions": [dict(id=f"CHG-ADR-{i + 1}", **d) for i, d in enumerate(clean(raw.get("decisions") or [], ("title", "why", "tradeoffs")))],
            "touch_points": {"modules": changed, "routes": impact["routes"], "tables": impact["tables"]},
            "rollout": ["deploy additive migration (if any)", "deploy new code", "enable clients"], "out_of_scope": [],
            "llm_risks": clean(raw.get("risks") or [], ("risk", "mitigation"))})
        ctx.bb.set("patch_stats", {"files": sorted(changed), "added": added, "removed": removed, "edits": len(edits)})
        return {"files": sorted(changed), "added": added, "removed": removed}


class _ChangeTester:
    """SpecTester, brownfield: new tests for a change request, from the spec only."""

    def _write_change_tests(self, ctx) -> Dict[str, Any]:
        req = ctx.requirement
        base = ctx.ws.baseline_root
        ctx.ws.revert_baseline_tests()  # a repair re-run proposes its amendments from scratch
        ctx.bb.set("test_updates", [])
        amendable = _kind(req) == "enhancement" and bool(ctx.feedback)
        failing_block = self._failing_existing_tests(ctx) if amendable else ""
        # Describe what THIS codebase really provides: a hand-built service has tests/helpers.py, a model-written one has
        # tests/wsgi_client.py. Telling the model to use a helper that does not exist makes it build elaborate workarounds.
        helper_path = next((p for p in ("tests/helpers.py", "tests/wsgi_client.py") if base and (base / p).exists()), None)
        helpers = ctx.ws.read_baseline(helper_path) if helper_path else ""
        helper_module = Path(helper_path).stem if helper_path else "helpers"
        sample = ""
        existing = sorted(p for p in (base / "tests").glob("test_*.py")) if base and (base / "tests").is_dir() else []
        if existing:
            sample = ctx.ws.read_baseline("tests/" + next((p.name for p in existing if p.name == "test_api.py"), existing[0].name))[:6000]
        if "FakeClock" in helpers:
            clock_rule = "Deterministic: use the FakeClock helper, no sleeps."
        else:
            clock_rule = ("Deterministic: no sleeps. This codebase has NO injectable clock and NO fixture that exposes its database, so "
                          "for time-dependent behaviour do this yourself in setUp: create a temp-file path (tempfile), build the app on it "
                          "with create_app(db_path=<that path>), then use sqlite3.connect(<that path>) to change the stored timestamps with "
                          "SQL through the documented schema (for example UPDATE a link's expires_at into the past) and call the public API. "
                          "Do not search for fixtures, do not patch internals.")
        spec = ctx.ws.read_baseline("openapi.json") if (ctx.ws.baseline_root and (ctx.ws.baseline_root / "openapi.json").exists()) else ""
        answers = (ctx.bb.get("input", {}).get("answers") or {})
        # The tests are written from the CONTRACT the code author published (never from the code): when the request does
        # not name a new field, the updated OpenAPI is the only place its name, type and bounds are defined.
        spec_now = ctx.ws.read("openapi.json") if ctx.bb.get("change_design") and ctx.ws.exists("openapi.json") else ""
        after_block = (f"## OpenAPI AFTER the change (the published contract the new behaviour must satisfy)\n{spec_now}\n\n"
                       if spec_now and spec_now != spec else "")
        # How the app is built and configured is part of the specification too. Without it the author cannot know that, for
        # example, authentication is OFF unless api_keys is non-empty, and writes tests that assume it is on.
        app_py = (base / "shortener" / "app.py") if base else None
        has_interface = bool(app_py and app_py.exists() and "def create_app" in app_py.read_text(encoding="utf-8", errors="replace"))
        iface = (f"## Service interface (how the app is built and configured; authentication is OFF unless api_keys is non-empty)\n"
                 f"{INTERFACE_SPEC}\n" if has_interface else "")
        acs = req["acceptance_criteria"]
        prompt = (f"# Task\nWrite NEW unittest tests for a change request to an existing service, from the SPEC ONLY "
                  f"(you have not seen the new implementation).\nRequest: {req['raw']}\n" + _kind_line(req, tests=True)
                  + (f"Clarifications: {json.dumps(answers)}\n" if answers else "")
                  + "\n## Acceptance criteria\n" + "\n".join(acs) + f"\n\n## Existing OpenAPI (before the change)\n{spec}\n\n{after_block}{iface}"
                  f"## Existing test helper ({helper_path or 'none'}) you may import with `from .{helper_module} import ...`\n{helpers}\n\n"
                  f"## Example of an existing test file\n{sample}\n\n## Rules\n* Files named tests/test_change_<topic>.py (1-3 files, {MIN_TESTS[_kind(req)]}+ tests).\n"
                  "* Cover happy path, boundaries, invalid input, backward compatibility of existing behaviour, and any "
                  f"schema migration from the old schema if the request implies one. {clock_rule}\n"
                  "* Use field names, bounds and status codes exactly as the AFTER contract declares them. Never read or assume the implementation.\n"
                  + PROSE_RULE
                  + "* Do not modify existing tests" + (" except as described under 'Existing tests that failed' below.\n" if failing_block else ".\n")
                  + "## Output JSON\n{\"files\":[{\"path\":\"tests/test_change_x.py\",\"content\":\"...\"}]"
                  + (",\"test_updates\":[{\"path\":\"tests/test_x.py\",\"find\":\"exact text occurring once\",\"replace\":\"...\",\"reason\":\"...\"}]}"
                     if failing_block else "}")
                  + failing_block + _feedback_block(ctx))
        raw = _call(ctx, self.name, prompt, max_tokens=14000)
        files = _validate_files(raw.get("files"), r"tests/test_change_[a-z0-9_]+\.py", "tests")
        total = sum(len(re.findall(r"def\s+test_\w+\s*\(", c)) for _, c in files)
        need = MIN_TESTS[_kind(req)]
        if total < need:
            raise ValidationFailure("tests: too few", [{"check": "llm.tests", "message": f"only {total} tests; need >= {need}"}])
        updates = self._plan_test_updates(ctx, raw.get("test_updates")) if amendable else []
        for p, c in files:
            ctx.write(p, c, "test")
        for path, content, reason in updates:
            ctx.update_existing_test(path, content, reason)
        return {"files": [p for p, _ in files], "tests": total, "test_updates": [u[0] for u in updates]}

    # -- the approved-test-update policy ------------------------------------------------------------------
    MAX_TEST_UPDATES = 5

    @staticmethod
    def _failing_existing_tests(ctx) -> str:
        """Source of the EXISTING test modules that failed in the last validation, so the agent can see what it may amend."""
        text = "\n".join(str(f.get("message", "")) for f in ctx.feedback)
        modules = sorted({m.split(".")[1] for m in re.findall(r"\((tests\.test_[A-Za-z0-9_]+)\.", text) if not m.startswith("tests.test_change_")})
        parts, budget = [], 30_000
        for mod in modules:
            rel = f"tests/{mod}.py"
            try:
                src = ctx.ws.read_baseline(rel)
            except FileNotFoundError:
                continue
            parts.append(f"### {rel}\n{src[:budget]}")
            budget -= len(src)
            if budget <= 0:
                break
        if not parts:
            return ""
        return ("\n\n## Existing tests that failed after the change (source)\nA failing existing test is normally a defect in the "
                "CODE, not a reason to edit the test. You may return `test_updates` ONLY for a test whose assertion is made obsolete by "
                "the intended contract change (for example it pins the whole OpenAPI document). Each update needs an exact, "
                "unique `find`, the corrected `replace` and a one-sentence `reason`. Never delete a test, never weaken or remove "
                "an assertion, never touch a test that fails for another reason. A human reviews every diff.\n" + "\n\n".join(parts))

    def _plan_test_updates(self, ctx, raw) -> List[Tuple[str, str, str]]:
        """Turn the model's proposed amendments into (path, new content, reason), or reject the reply with feedback."""
        if raw in (None, []):
            return []
        problems: List[str] = []
        if not isinstance(raw, list) or len(raw) > self.MAX_TEST_UPDATES:
            raise ValidationFailure("test_updates: bad shape", [{"check": "llm.test_updates",
                                    "message": f"test_updates must be a list of at most {self.MAX_TEST_UPDATES} edits"}])
        contents: Dict[str, str] = {}
        reasons: Dict[str, str] = {}
        for i, e in enumerate(raw):
            if not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e.get(k) for k in ("path", "find", "reason")) \
                    or not isinstance(e.get("replace"), str):
                problems.append(f"test_update {i}: needs path, find, replace and a non-empty reason")
                continue
            path = e["path"].replace("\\", "/")
            try:
                contents.setdefault(path, ctx.ws.read_baseline(path))
            except (FileNotFoundError, Exception) as exc:
                problems.append(f"test_update {i}: {path!r} is not an existing test module ({exc})")
                continue
            if contents[path].count(e["find"]) != 1:
                problems.append(f"test_update {i} in {path}: `find` matched {contents[path].count(e['find'])} times (must be exactly 1)")
                continue
            contents[path] = contents[path].replace(e["find"], e["replace"], 1)
            reasons[path] = (reasons.get(path, "") + " " + e["reason"]).strip()
        for path, new in contents.items():
            problems += check_test_amendment(ctx.ws.read_baseline(path), new, path)
        if problems:
            raise ValidationFailure(f"test_updates: {len(problems)} problem(s)", [{"check": "llm.test_updates", "message": m} for m in problems])
        return [(p, contents[p], reasons.get(p, "")) for p in contents]



# ------------------------------------------------------------------ the two agents ---
class CodeAuthor(_ServiceWriter, _ChangeProposer):
    """agents/code_author.md. New service for a greenfield plan, anchored edits for an llm_change plan."""
    name = "code_author"
    model_backed = True

    def run(self, ctx) -> Dict[str, Any]:
        if ctx.requirement["plan_type"] == "llm_change":
            return self._propose_change(ctx)
        return self._write_service(ctx)


class SpecTester(_ServiceTester, _ChangeTester):
    """agents/spec_tester.md. Full suite for a greenfield plan, change tests for an llm_change plan."""
    name = "spec_tester"
    model_backed = True
    __test__ = False  # not a unittest case, despite the name

    def run(self, ctx) -> Dict[str, Any]:
        if ctx.requirement["plan_type"] == "llm_change":
            return self._write_change_tests(ctx)
        return self._write_service_tests(ctx)
