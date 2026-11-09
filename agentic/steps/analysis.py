"""Discovery-stage agents: requirement understanding and codebase reasoning."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..agent_specs import load_spec
from ..knowledge import EXPIRY_ACCEPTANCE, SHORTENER_ACCEPTANCE
from ..providers import LLMTransient, LLMUnavailable, sanitize_questions
from ..harness.core import ClarificationNeeded, GuardrailViolation

# ---------------------------------------------------------------- lexicons ---
INJECTION_PATTERNS = [r"ignore (all |any )?(previous|prior) instructions", r"system prompt", r"rm\s+-rf",
                      r"disregard .* rules", r"exfiltrate", r"api[_ -]?key", r"password"]

VAGUE_TERMS: Dict[str, Dict[str, str]] = {
    "scalable": {"facet": "scalability",
                 "question": "What load must the system handle (new links/month, redirects/sec at peak)?",
                 "default": "10M new links/month, 100:1 read:write, peak = 10x average (~4k redirects/s); stateless horizontally-scalable service tier"},
    "fast": {"facet": "performance",
             "question": "Which path must be faster (redirect, create, or both) and what p95 latency target applies?",
             "default": "redirect p95 < 50 ms server-side at peak load; create p95 < 200 ms"},
    "faster": {"facet": "performance", "alias": "fast"},
    "performance": {"facet": "performance", "alias": "fast"},
    "efficient": {"facet": "performance", "alias": "fast"},
    "secure": {"facet": "security",
               "question": "Which threats are in scope: link abuse/phishing, unauthenticated API use, code enumeration, rate-limit evasion?",
               "default": "abuse + enumeration + API misuse: API-key auth on write endpoints, rate limiting, URL reputation screening, non-sequential codes"},
    "safer": {"facet": "security", "alias": "secure"},
    "security": {"facet": "security", "alias": "secure"},
    "reliable": {"facet": "availability",
                 "question": "What availability target (e.g. 99.9%) and RPO/RTO are required?",
                 "default": "99.9% monthly availability, RPO <= 5 min, RTO <= 30 min"},
    "robust": {"facet": "availability", "alias": "reliable"},
    "better": {"facet": "generic", "question": "What measurable outcome defines 'better' here?", "default": "unspecified"},
    "improve": {"facet": "generic", "alias": "better"},
    "user-friendly": {"facet": "generic", "alias": "better"},
}

NUMERIC_TARGET = re.compile(r"\b\d+(\.\d+)?\s?(ms|s|sec|seconds|rps|req/s|qps|%|k|m|million|links|users)\b", re.I)
DOMAIN_SHORTENER = re.compile(r"url[\s-]?shorten|link[\s-]?shorten|short[\s-]?link|shortener", re.I)
RECIPES = {"expiry": re.compile(r"expir|\bttl\b|time[- ]to[- ]live|self[- ]destruct", re.I)}


def _canonical(term: str) -> str:
    entry = VAGUE_TERMS[term]
    return entry.get("alias", term)


class RequirementAnalyst:
    """Interprets intent, detects ambiguity, normalises to an engineering problem.

    Deterministic by default; an LLM (if configured) may *add* clarification
    questions, never change the classification or the plan.
    """
    name = "requirement_analyst"
    model_backed = True  # defined in agents/requirement_analyst.md (the model only ever adds questions)

    def run(self, ctx) -> Dict[str, Any]:
        cfg = ctx.bb.require("input")
        raw = re.sub(r"[\x00-\x08\x0b-\x1f]", " ", cfg["text"])[:2000].strip()
        text = raw.lower()
        repo: Optional[str] = cfg.get("target_repo")
        answers: Dict[str, str] = cfg.get("answers") or {}
        assume_defaults: bool = bool(cfg.get("assume_defaults"))

        # 1. security screening of the requirement text itself (prompt injection)
        flags = [p for p in INJECTION_PATTERNS if re.search(p, text)]
        if flags:
            ctx.events.emit("guardrail_hit", rule="suspicious_requirement_text", patterns=len(flags))

        # 2. domain / change detection
        domain = "url_shortener" if DOMAIN_SHORTENER.search(text) else None
        recipe = next((name for name, pat in RECIPES.items() if pat.search(text)), None)
        if repo and not domain and (Path(repo) / "shortener").exists():
            domain = "url_shortener"  # recognised from the codebase itself
        change_kind = self._change_kind(text) if repo else None

        # 3. ambiguity detection
        has_numeric = bool(NUMERIC_TARGET.search(text))
        found = []
        for term in VAGUE_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", text) and _canonical(term) not in [f["canonical"] for f in found]:
                found.append({"term": term, "canonical": _canonical(term)})
        ambiguities = []
        for i, f in enumerate(found, 1):
            spec = VAGUE_TERMS[f["canonical"]]
            ambiguities.append({"id": f"Q{i}", "term": f["term"], "facet": spec["facet"],
                                "question": spec["question"], "default": spec["default"]})
        blocking = (len(ambiguities) >= 2 and not has_numeric) or (not domain and not recipe and not repo and len(raw.split()) < 8)
        questions = []
        if blocking:
            if not ambiguities:
                ambiguities = [{"id": "Q1", "term": "scope", "facet": "generic",
                                "question": "What system or component is this about, and what outcome is expected?",
                                "default": "unspecified"}]
            questions = [{"id": a["id"], "question": a["question"], "default": a["default"]} for a in ambiguities]

        assumptions: List[Dict[str, Any]] = []
        resolved_by: Dict[str, str] = {}
        if blocking:
            for q in questions:
                if answers.get(q["id"]):
                    resolved_by[q["id"]] = "human"
                    assumptions.append({"id": f"AS-{q['id']}", "text": f"{q['question']} -> {answers[q['id']]}",
                                        "source": "human answer", "confidence": "high", "needs_confirmation": False})
                elif assume_defaults:
                    resolved_by[q["id"]] = "default"
                    assumptions.append({"id": f"AS-{q['id']}", "text": f"{q['question']} -> assumed: {q['default']}",
                                        "source": "analyst default", "confidence": "low", "needs_confirmation": True})
            unresolved = [q for q in questions if q["id"] not in resolved_by]
            if unresolved:
                ctx.events.emit("clarification_needed", questions=len(unresolved))
                raise ClarificationNeeded(unresolved)
        else:
            for a in ambiguities:  # non-blocking vague term: proceed on a documented assumption
                assumptions.append({"id": f"AS-{a['id']}", "text": f"'{a['term']}' interpreted as: {a['default']}",
                                    "source": "analyst default", "confidence": "medium", "needs_confirmation": True})

        # 4. optional LLM enrichment (untrusted, additive only)
        llm_questions: List[str] = []
        try:
            payload = ctx.llm.complete_json(load_spec(self.name).prompt(), f"Requirement: {raw}")
            llm_questions = sanitize_questions(payload.get("questions"))
            ctx.events.emit("llm_used", provider=ctx.llm.name, extra_questions=len(llm_questions))
        except (LLMUnavailable, LLMTransient) as exc:
            ctx.events.emit("llm_fallback", reason=str(exc)[:80])

        # 5. normalise
        use_llm = cfg.get("generator") == "llm"
        if use_llm and not getattr(ctx.llm, "available", False):
            raise GuardrailViolation("--generator llm requires a configured provider (set AGENTIC_PROVIDER and its settings, "
                                     "see .env.example)")
        plan_type = self._plan_type(domain, recipe, repo, use_llm, bool(blocking))
        req = {
            "raw": raw, "security_flags": flags, "domain": domain or "unknown", "plan_type": plan_type,
            "generator": "llm" if use_llm else "template",
            "scenario": "brownfield" if repo else "greenfield", "target_repo": repo,
            "change_kind": change_kind, "change_recipe": recipe if repo else None,
            "was_ambiguous": bool(blocking), "ambiguities": ambiguities, "resolved_by": resolved_by,
            "assumptions": assumptions, "llm_suggested_questions": llm_questions,
            "concerns": sorted({a["facet"] for a in ambiguities}),
        }
        self._normalise(req, text, answers)
        ctx.bb.set("requirement", req)
        ctx.events.emit("requirement_normalised", plan_type=plan_type, domain=req["domain"],
                        capabilities=len(req["capabilities"]), assumptions=len(assumptions),
                        ambiguous=bool(blocking))
        return {"plan_type": plan_type}

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _change_kind(text: str) -> str:
        """What kind of change to existing code this is. The kind decides which tools the plan may use and which
        checks the validator applies (see harness/tools.py and steps/validate.py). Order matters: "refactor ... and
        update the docs" is a refactor; "fix the bug in the docs build" is a bugfix; "fix typos in the docs" is docs."""
        if re.search(r"\brefactor|clean ?up|restructure|reorgani[sz]e", text):
            return "refactor"
        if re.search(r"\b(bug|defect|broken|crash|incorrect|wrong)\b", text):
            return "bugfix"
        if re.search(r"\b(tests?|coverage|docs?|documentation|readme|docstrings?)\b", text):
            return "tests_docs"
        if re.search(r"\bfix(es|ed)?\b", text):
            return "bugfix"
        return "enhancement"

    @staticmethod
    def _plan_type(domain, recipe, repo, use_llm=False, ambiguous=False) -> str:
        if not repo:
            return "greenfield" if domain == "url_shortener" else "analysis"
        if ambiguous:  # goals still vague after the questions: a measurable proposal comes before any change
            return "analysis"
        # a hand-written recipe covers one known change (template mode); anything else is a model-proposed change
        return "brownfield_change" if (recipe and not use_llm) else "llm_change"

    def _normalise(self, req: Dict[str, Any], text: str, answers: Dict[str, str]) -> None:
        caps: List[Dict[str, str]] = []
        nfrs: List[Dict[str, Any]] = []
        if req["plan_type"] == "greenfield":
            caps = [
                {"id": "CAP-1", "name": "Create short link", "detail": "POST url (+ optional custom alias) -> unique short code"},
                {"id": "CAP-2", "name": "Redirect", "detail": "GET /{code} -> HTTP redirect to the original URL, low latency"},
                {"id": "CAP-3", "name": "Link management", "detail": "fetch metadata and delete a link"},
                {"id": "CAP-4", "name": "Persistence", "detail": "links survive restarts; versioned schema migrations"},
                {"id": "CAP-5", "name": "Click analytics", "detail": "total / unique clicks, daily series, referrers, devices"},
                {"id": "CAP-6", "name": "Abuse protection", "detail": "URL validation, rate limiting, optional API-key auth"},
            ]
            nfrs = [{"id": "NFR-1", "name": "Scalability", "target": "10M links/month, ~4k redirects/s peak",
                     "assumed": True},
                    {"id": "NFR-2", "name": "Redirect latency", "target": "p95 < 50 ms server-side", "assumed": True},
                    {"id": "NFR-3", "name": "Availability", "target": "99.9%", "assumed": True}]
            req["title"] = "Scalable URL shortener service with APIs, persistence and analytics"
            req["problem_statement"] = ("Build a horizontally-scalable, read-heavy (~100:1) HTTP service that creates short "
                                        "codes for long URLs, redirects with minimal latency, persists links durably, and records "
                                        "click analytics off the hot path, with contract-first APIs, validation against abuse, "
                                        "and a test-verified implementation.")
            req["acceptance_criteria"] = SHORTENER_ACCEPTANCE
        elif req["plan_type"] == "brownfield_change":
            caps = [
                {"id": "CAP-1", "name": "Optional link expiry on create", "detail": "expires_in_seconds -> stored absolute UTC expires_at"},
                {"id": "CAP-2", "name": "Expired redirect behaviour", "detail": "HTTP 410 Gone at/after expiry"},
                {"id": "CAP-3", "name": "Backward compatibility", "detail": "existing links never expire; existing clients unaffected"},
                {"id": "CAP-4", "name": "Schema migration", "detail": "additive nullable column via versioned migration v2"},
            ]
            nfrs = [{"id": "NFR-1", "name": "No regression", "target": "all existing tests pass", "assumed": False},
                    {"id": "NFR-2", "name": "API compatibility", "target": "additive-only OpenAPI changes", "assumed": False}]
            req["title"] = "Add link expiry to the existing URL shortener"
            req["problem_statement"] = ("Extend the existing service so a short link can optionally expire. Expired links must "
                                        "return 410 Gone, legacy links must keep working, and the database must migrate in place "
                                        "without breaking existing clients or tests.")
            req["acceptance_criteria"] = EXPIRY_ACCEPTANCE
        elif req["plan_type"] == "llm_change":
            answered = "; ".join(f"{k}: {v}" for k, v in answers.items())
            caps = [{"id": "CAP-1", "name": "Implement the requested change", "detail": req["raw"] + (f"  [clarifications: {answered}]" if answered else "")},
                    {"id": "CAP-2", "name": "Backward compatibility", "detail": "existing behaviour, data and API clients keep working"}]
            nfrs = [{"id": "NFR-1", "name": "No regression", "target": "all existing tests pass", "assumed": False},
                    {"id": "NFR-2", "name": "API compatibility", "target": "additive-only OpenAPI changes", "assumed": False}]
            req["title"] = "Change: " + req["raw"][:80]
            req["problem_statement"] = ("Implement the requested change in the existing service with minimal anchored edits, keep it backward "
                                        "compatible, and prove it with new tests while the full existing suite stays green.")
            req["acceptance_criteria"] = [
                "AC-1 The requested behaviour is implemented and covered by new automated tests: " + req["raw"][:160]]
            req["acceptance_criteria"] += [f"AC-{i + 2} Clarified objective: {k} = {v}" for i, (k, v) in enumerate(answers.items())]
            req["acceptance_criteria"] += ["AC-%d All pre-existing tests still pass; OpenAPI diff is additive only." % (len(answers) + 2)]
        else:  # analysis / proposal
            perf = answers.get("Q1") if "performance" in req["concerns"] else None
            facets = req["concerns"] or ["generic"]
            for i, facet in enumerate(facets, 1):
                a = next((x for x in req["ambiguities"] if x["facet"] == facet), None)
                chosen = None
                if a:
                    chosen = answers.get(a["id"]) or a["default"]
                caps.append({"id": f"CAP-{i}", "name": f"Improve {facet}", "detail": chosen or "to be defined"})
            req["title"] = "Proposal: " + req["raw"][:70]
            req["problem_statement"] = ("Convert a vague goal into measurable engineering objectives, evaluate design options "
                                        "with trade-offs, and define how each objective will be verified before implementation starts.")
            req["acceptance_criteria"] = [
                f"AC-{i} {c['name']}: {c['detail']} - measured by an automated check in CI" for i, c in enumerate(caps, 1)]
        req["capabilities"], req["nfrs"] = caps, nfrs
        req["scenario"] = "brownfield" if req["target_repo"] else "greenfield"


# ------------------------------------------------------------ code analysis ---
class CodebaseAnalyst:
    """Builds a system-level model of an existing Python codebase using the AST:
    modules + internal import graph, classes/functions, HTTP routes (decorator
    based), SQL tables and which functions read/write them, and a route ->
    handler -> function -> table data-flow. Then performs change-impact
    analysis for the normalised requirement."""
    name = "codebase_analyst"

    CONCEPTS = {  # requirement vocabulary -> flow keywords (heuristic; LLM can replace this)
        "create": {"create", "shorten", "alias", "expir", "ttl", "validate", "secur", "abuse", "auth", "rate limit"},
        "redirect": {"redirect", "resolve", "expir", "ttl", "gone", "410", "latency", "fast", "cache"},
        "stats": {"analytics", "stats", "click", "report"},
        "delete": {"delete", "remove"},
    }

    def run(self, ctx) -> Dict[str, Any]:
        req = ctx.requirement
        repo = Path(req["target_repo"]).resolve()
        model = self.index(repo)
        impact = self.impact(model, req["raw"].lower())
        ctx.bb.set("codebase_model", model)
        ctx.bb.set("impact", impact)
        ctx.events.emit("codebase_indexed", modules=len(model["modules"]), routes=len(model["routes"]),
                        tables=len(model["tables"]), tests=len(model["tests"]))
        ctx.events.emit("impact_analysed", routes=len(impact["routes"]), modules=len(impact["modules"]),
                        tables=",".join(impact["tables"]), risk=impact["risk_level"])
        return {"modules": len(model["modules"])}

    # ---------------------------------------------------------------- index
    def index(self, repo: Path) -> Dict[str, Any]:
        modules: Dict[str, Any] = {}
        tests: Dict[str, Any] = {}
        routes: List[Dict[str, Any]] = []
        func_defs: List[Dict[str, Any]] = []
        for path in sorted(repo.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo).as_posix()
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            info = {"path": rel, "loc": src.count("\n") + 1, "imports": [], "classes": [], "functions": [],
                    "sql": sorted(set(self._sql_tables(src)))}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level >= 1:
                    info["imports"].append((node.module or "").split(".")[0] or "." )
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in ("shortener", "tests"):
                    info["imports"].append(node.module.split(".")[-1])
                elif isinstance(node, ast.ClassDef):
                    info["classes"].append(node.name)
            for node, cls in self._functions_with_class(tree):
                strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                reads, writes = self._sql_access(strings)
                calls = []  # (receiver hint, name): receiver lets us resolve self.repo.create_link vs app.create_link
                for stmt in node.body:  # body only: decorator calls (e.g. router.route) are registration, not data flow
                    for n in ast.walk(stmt):
                        if isinstance(n, ast.Call):
                            if isinstance(n.func, ast.Attribute):
                                v = n.func.value
                                recv = v.id if isinstance(v, ast.Name) else (v.attr if isinstance(v, ast.Attribute) else "?")
                                calls.append((recv, n.func.attr))
                            elif isinstance(n.func, ast.Name):
                                calls.append((None, n.func.id))
                fi = {"name": node.name, "cls": cls, "module": rel, "calls": calls,
                      "tables": sorted(reads | writes), "writes_tables": sorted(writes)}
                info["functions"].append(node.name)
                func_defs.append(fi)
                for dec in node.decorator_list:
                    r = self._route_from_decorator(dec)
                    if r:
                        routes.append({"method": r[0], "path": r[1], "handler": node.name, "module": rel})
            (tests if rel.startswith("tests/") else modules)[rel] = info
        tables = self._tables_from_schema(repo)
        flows = [self._flow(r, func_defs) for r in routes]
        return {"repo": str(repo), "modules": modules, "tests": tests, "routes": routes, "tables": tables,
                "flows": flows,
                "import_graph": {m: sorted(set(i["imports"])) for m, i in modules.items()}}

    @staticmethod
    def _functions_with_class(tree: ast.AST):
        """Yield (function node, enclosing class name or None)."""
        def visit(node, cls):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    yield from visit(child, child.name)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child, cls
                    yield from visit(child, cls)
                else:
                    yield from visit(child, cls)
        yield from visit(tree, None)

    _SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER)\b", re.I)

    @classmethod
    def _sql_access(cls, strings: List[str]):
        """Tables read / written by SQL embedded in string literals (not prose or comments)."""
        reads: Set[str] = set()
        writes: Set[str] = set()
        for s in strings:
            if not cls._SQL_START.match(s):
                continue
            reads |= set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", s, flags=re.I))
            writes |= set(re.findall(r"\bINSERT\s+INTO\s+(\w+)", s, flags=re.I))
            writes |= set(re.findall(r"\bUPDATE\s+(\w+)\s+SET\b", s, flags=re.I))
            writes |= set(re.findall(r"\bDELETE\s+FROM\s+(\w+)", s, flags=re.I))
        return reads, writes

    @classmethod
    def _sql_tables(cls, src: str) -> List[str]:
        """Module-level summary of tables mentioned in SQL string literals."""
        try:
            strings = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        except SyntaxError:
            return []
        reads, writes = cls._sql_access(strings)
        return sorted(reads | writes)

    @staticmethod
    def _route_from_decorator(dec: ast.AST):
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "route" \
                and len(dec.args) >= 2 and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in dec.args[:2]):
            return dec.args[0].value, dec.args[1].value
        return None

    @staticmethod
    def _tables_from_schema(repo: Path) -> Dict[str, List[str]]:
        tables: Dict[str, List[str]] = {}
        candidates = list(repo.glob("schema.sql")) + [p for p in repo.rglob("storage.py")]
        for p in candidates:
            for m in re.finditer(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\);", p.read_text(encoding="utf-8"), re.S | re.I):
                cols = [ln.strip().split()[0] for ln in m.group(2).split("\n")
                        if ln.strip() and not ln.strip().upper().startswith(("PRIMARY", "UNIQUE", "FOREIGN", "--"))]
                tables.setdefault(m.group(1), [c.strip(",") for c in cols])
        return tables

    @staticmethod
    def _resolve(recv, name, cur, by_name) -> List[Dict[str, Any]]:
        """Receiver-aware call resolution. self.x -> same class; <hint>.x -> a class whose name contains the hint
        (repo -> Repository, cache -> TTLCache, service -> ShortenerService); bare calls -> module functions."""
        cands = by_name.get(name, [])
        if recv == "self":
            return [c for c in cands if c["cls"] == cur["cls"]]
        if recv is None:
            plain = [c for c in cands if c["cls"] is None]
            return [c for c in plain if c["module"] == cur["module"]] or plain
        hint = recv.lower().lstrip("_")
        return [c for c in cands if c["cls"] and hint in c["cls"].lower()]

    def _flow(self, route: Dict[str, Any], funcs: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for f in funcs:
            by_name.setdefault(f["name"], []).append(f)
        start = next((f for f in by_name.get(route["handler"], []) if f["module"] == route["module"] and f["cls"] is None), None)
        from collections import deque
        seen: Set[int] = set()
        chain: List[str] = []
        mods: Set[str] = set()
        direct: Set[str] = {route["module"]}  # the API surface itself
        reads: Set[str] = set()
        writes: Set[str] = set()
        queue = deque([(start, None)] if start else [])
        while queue:  # breadth-first from the handler: chain reads in call order
            cur, parent = queue.popleft()
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            chain.append(f"{cur['module'].split('/')[-1]}:{(cur['cls'] + '.') if cur['cls'] else ''}{cur['name']}")
            mods.add(cur["module"])
            if cur["tables"]:  # data layer, plus its direct caller: a data change propagates one level up
                direct.add(cur["module"])
                if parent:
                    direct.add(parent["module"])
            reads |= set(cur["tables"]) - set(cur["writes_tables"])
            writes |= set(cur["writes_tables"])
            for recv, name in cur["calls"]:
                for target in self._resolve(recv, name, cur, by_name):
                    queue.append((target, cur))
        return {"route": f"{route['method']} {route['path']}", "handler": route["handler"], "chain": chain,
                "modules": sorted(mods), "direct_modules": sorted(direct),
                "tables_read": sorted(reads), "tables_written": sorted(writes)}

    # --------------------------------------------------------------- impact
    def impact(self, model: Dict[str, Any], text: str) -> Dict[str, Any]:
        active = {c for c in self.CONCEPTS if any(re.search(k, text) for k in self.CONCEPTS[c])}
        if "create" not in active and "redirect" not in active:
            active |= {"create", "redirect"}  # vague change: assume core flows are in play
        wanted_routes = []
        for f in model["flows"]:
            r = f["route"]
            hit = (("create" in active and r.startswith("POST /api/v1/links")) or
                   ("redirect" in active and r.startswith("GET /{")) or
                   ("stats" in active and r.endswith("/stats")) or
                   ("delete" in active and r.startswith("DELETE")))
            if hit:
                wanted_routes.append(f)
        flow_modules: Set[str] = set()   # everything the affected flows touch (transitive)
        direct: Set[str] = set()         # modules that must change: API surface, data layer, data layer's callers
        tables: Set[str] = set()
        for f in wanted_routes:
            flow_modules |= set(f["modules"])
            direct |= set(f["direct_modules"])
            tables |= set(f["tables_read"]) | set(f["tables_written"])
        direct |= {m for m in model["modules"] if m.endswith("models.py")}  # shared value objects follow the data
        imported_by = {m: [o for o, imps in model["import_graph"].items() if Path(m).stem in imps] for m in direct}
        # tests: those importing a changed module, plus those importing a test helper that does (transitive)
        impacted = {t for t, info in model["tests"].items() if any(Path(m).stem in info["imports"] for m in direct)}
        helper_stems = {Path(t).stem for t in impacted}
        impacted |= {t for t, info in model["tests"].items() if set(info["imports"]) & helper_stems}
        public_api = any(f["route"].startswith(("POST", "GET /{")) for f in wanted_routes)
        risk = "high" if "redirect" in active and public_api else "medium"
        return {"concepts": sorted(active), "routes": [f["route"] for f in wanted_routes], "flows": wanted_routes,
                "modules": sorted(direct), "flow_modules": sorted(flow_modules), "tables": sorted(tables),
                "imported_by": imported_by, "tests": sorted(impacted), "risk_level": risk,
                "public_api_surface_changed": public_api,
                "blast_radius": f"{len(direct)} of {len(model['modules'])} modules change; {len(flow_modules)} are on the affected flows"}
