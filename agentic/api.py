"""HTTP API for running the harness in the cloud (Azure Container Apps) with remote human approval gates.

  POST /runs                      {requirement, generator?, target_run?, answers?, assume_defaults?}  -> 202 {run_id}
  GET  /runs                      list
  GET  /runs/{id}                 status, phase, pending gate (with summary), tasks, validation, LLM usage
  POST /runs/{id}/gates/{gate}    {approved: bool, by: str, note?: str}   <- the human decision (plan_approval | release_approval)
  POST /runs/{id}/answers         {answers: {"Q1": "..."}}   answer clarification questions -> starts a new run
  GET  /runs/{id}/summary         ENGINEERING_SUMMARY.md
  GET  /runs/{id}/events          audit log (NDJSON)
  GET  /runs/{id}/files[/{path}]  released artifacts   |   GET /runs/{id}/release.zip
  GET  /healthz                   liveness (no auth)

Security: bearer token on everything except /healthz; remote callers can NOT name filesystem paths (a brownfield
target is another *run id*); `auto` approval is disabled unless AGENTIC_ALLOW_AUTO=1; unattended gates time out
as REJECTED; one run per thread with a concurrency cap; file serving is path-jailed to the run's release directory.
"""
from __future__ import annotations

import hmac
import io
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from .harness.approvals import ExternalApproval
from .providers import get_provider
from .harness.harness import Harness

log = logging.getLogger("agentic.api")
MAX_BODY = 64 * 1024
RUN_ID = re.compile(r"^[0-9a-f]{12}$")


class RunRegistry:
    def __init__(self, data_dir: Path, llm_factory: Callable[[], Any], allow_auto: bool, gate_timeout: float, max_active: int):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.llm_factory, self.allow_auto, self.gate_timeout, self.max_active = llm_factory, allow_auto, gate_timeout, max_active
        self.runs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        for meta in self.dir.glob("*/meta.json"):  # runs from before a restart are listed, never silently re-run
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                if m.get("status") in ("running", "waiting_approval"):
                    m["status"] = "interrupted"
                self.runs[m["run_id"]] = {"meta": m, "approval": None, "thread": None}
            except (OSError, ValueError, KeyError):
                continue

    def _save(self, run_id: str) -> None:
        (self.dir / run_id / "meta.json").write_text(json.dumps(self.runs[run_id]["meta"], indent=2), encoding="utf-8")

    def active(self) -> int:
        return sum(1 for r in self.runs.values() if r["thread"] and r["thread"].is_alive())

    def start(self, body: Dict[str, Any]) -> Dict[str, Any]:
        requirement = body.get("requirement")
        if not isinstance(requirement, str) or not (5 <= len(requirement.strip()) <= 2000):
            raise ValueError("requirement must be a string of 5..2000 characters")
        llm = self.llm_factory()
        if not getattr(llm, "available", False):  # the harness plans with a model; there is no offline plan
            raise ValueError("no LLM provider is configured on this server: the harness needs a model for its project_lead agent")
        generator = body.get("generator") or "llm"
        if generator not in ("llm", "template"):
            raise ValueError("generator must be 'llm' or 'template'")
        repo = None
        target = body.get("target_run")
        if target is not None:
            if not isinstance(target, str) or not RUN_ID.match(target) or target not in self.runs:
                raise ValueError("target_run must be the id of an existing run")
            repo = self.dir / target / "release"
            if not (repo / "shortener").exists():
                raise ValueError("target_run has no released codebase (it must have completed and been approved)")
        answers = body.get("answers") or {}
        if not isinstance(answers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in answers.items()):
            raise ValueError("answers must be an object of string to string")
        approval_mode = body.get("approval", "external")
        if approval_mode == "auto" and not self.allow_auto:
            raise ValueError("auto approval is disabled on this server (human gates are mandatory)")
        with self._lock:
            if self.active() >= self.max_active:
                raise OverflowError(f"too many active runs (max {self.max_active})")
            run_id = uuid.uuid4().hex[:12]
            out = self.dir / run_id
            out.mkdir(parents=True)
            gate = ExternalApproval(self.gate_timeout) if approval_mode == "external" else None
            meta = {"run_id": run_id, "requirement": requirement.strip(), "generator": generator, "target_run": target,
                    "status": "running", "created": time.time(), "answers": answers}
            rec = {"meta": meta, "approval": gate, "thread": None, "llm": llm}
            self.runs[run_id] = rec
            self._save(run_id)

            def work() -> None:
                try:
                    pipe = Harness(out, approval=gate if gate else "auto", echo=None, llm=llm, generator=generator)
                    res = pipe.run(requirement.strip(), target_repo=str(repo) if repo else None, answers=answers,
                                   assume_defaults=bool(body.get("assume_defaults")))
                    meta["status"] = res.status
                    if res.questions:
                        meta["questions"] = res.questions
                except Exception as exc:  # never let a run thread die silently
                    log.exception("run %s crashed", run_id)
                    meta["status"], meta["error"] = "crashed", f"{type(exc).__name__}: {exc}"
                meta["finished"] = time.time()
                self._save(run_id)
            t = threading.Thread(target=work, name=f"run-{run_id}", daemon=True)
            rec["thread"] = t
            t.start()
        return meta

    def describe(self, run_id: str) -> Dict[str, Any]:
        rec = self.runs[run_id]
        meta = dict(rec["meta"])
        out = self.dir / run_id
        gate = rec["approval"]
        if gate is not None and gate.pending:
            meta["status"] = "waiting_approval"
            meta["pending_gate"] = {k: v for k, v in gate.pending.items() if k != "requested_at"}
        state = out / "state.json"
        if state.exists():
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
                meta["tasks"] = [{"id": t["id"], "agent": t["agent"], "status": t["status"], "attempts": t["attempts"],
                                  "generation": t["generation"]} for t in data["tasks"]]
                bb = data["blackboard"]
                vr = bb.get("validation_report")
                if vr:
                    meta["validation"] = {k: vr[k] for k in ("passed", "failed", "warnings", "generation")}
                if bb.get("requirement"):
                    meta["plan_type"] = bb["requirement"]["plan_type"]
            except (OSError, ValueError, KeyError):
                pass
        llm = rec.get("llm")
        if llm is not None and getattr(llm, "available", False):
            meta["llm"] = {"provider": llm.name, "model": getattr(llm, "model", None), **(llm.usage or {})}
        meta["decisions"] = [d.__dict__ for d in gate.history] if gate is not None else []
        return meta


class Handler(BaseHTTPRequestHandler):
    server_version = "agentic-sdlc"
    protocol_version = "HTTP/1.1"

    # --------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    @property
    def reg(self) -> RunRegistry:
        return self.server.registry  # type: ignore[attr-defined]

    def _send(self, status: int, payload: Any = None, ctype: str = "application/json", raw: Optional[bytes] = None) -> None:
        body = raw if raw is not None else json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, msg: str) -> None:
        self._send(status, {"error": msg})

    def _authed(self) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        return supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:], token)

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request body too large")
        data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # ---------------------------------------------------------------- routing
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/healthz" and method == "GET":
                llm = self.server.llm_factory()  # type: ignore[attr-defined]
                return self._send(200, {"status": "ok", "llm": {"provider": llm.name, "model": getattr(llm, "model", None)},
                                        "active_runs": self.reg.active()})
            if not self._authed():
                return self._err(401, "missing or invalid bearer token")
            parts = path.strip("/").split("/")
            if parts[0] != "runs":
                return self._err(404, "no such route")
            if len(parts) == 1:
                if method == "POST":
                    return self._send(202, self._start())
                return self._send(200, [{k: self.reg.runs[r]["meta"].get(k) for k in ("run_id", "status", "requirement", "generator", "created")}
                                        for r in sorted(self.reg.runs, key=lambda x: -self.reg.runs[x]["meta"].get("created", 0))])
            run_id = parts[1]
            if not RUN_ID.match(run_id) or run_id not in self.reg.runs:
                return self._err(404, "no such run")
            rest = parts[2:]
            if not rest and method == "GET":
                return self._send(200, self.reg.describe(run_id))
            if rest[:1] == ["gates"] and len(rest) == 2 and method == "POST":
                return self._gate(run_id, rest[1])
            if rest == ["answers"] and method == "POST":
                return self._answers(run_id)
            if method == "GET":
                return self._read(run_id, rest)
            return self._err(405, "method not allowed")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._err(400, str(exc))
        except OverflowError as exc:
            return self._err(429, str(exc))
        except Exception:
            log.exception("unhandled")
            return self._err(500, "internal error")

    # --------------------------------------------------------------- handlers
    def _start(self) -> Dict[str, Any]:
        meta = self.reg.start(self._body())
        return {"run_id": meta["run_id"], "status": meta["status"], "generator": meta["generator"]}

    def _gate(self, run_id: str, gate: str) -> None:
        body = self._body()
        approval = self.reg.runs[run_id]["approval"]
        if approval is None:
            return self._err(409, "this run has no external gates")
        if not isinstance(body.get("approved"), bool) or not isinstance(body.get("by"), str) or not body["by"].strip():
            return self._err(400, "body needs approved (bool) and by (non-empty string: who is deciding)")
        if not approval.decide(gate, body["approved"], body["by"].strip()[:80], str(body.get("note", ""))[:300]):
            return self._err(409, f"gate '{gate}' is not pending")
        self._send(200, {"gate": gate, "approved": body["approved"], "by": body["by"]})

    def _answers(self, run_id: str) -> None:
        meta = self.reg.runs[run_id]["meta"]
        if meta.get("status") != "needs_clarification":
            return self._err(409, "run is not waiting for clarification")
        body = self._body()
        new = self.reg.start({"requirement": meta["requirement"], "generator": meta["generator"], "target_run": meta.get("target_run"),
                              "answers": body.get("answers") or {}, "assume_defaults": bool(body.get("assume_defaults"))})
        self._send(202, {"run_id": new["run_id"], "status": new["status"], "continues": run_id})

    def _read(self, run_id: str, rest) -> None:
        out = self.reg.dir / run_id
        if rest == ["summary"]:
            p = out / "ENGINEERING_SUMMARY.md"
            return self._send(200, raw=p.read_bytes(), ctype="text/markdown; charset=utf-8") if p.exists() else self._err(404, "summary not ready")
        if rest == ["events"]:
            p = out / "events.jsonl"
            return self._send(200, raw=p.read_bytes() if p.exists() else b"", ctype="application/x-ndjson")
        release = (out / "release").resolve()
        if rest == ["release.zip"]:
            if not release.exists():
                return self._err(404, "nothing released (run not completed or release not approved)")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(release.rglob("*")):
                    if f.is_file() and "__pycache__" not in f.parts:
                        z.write(f, f.relative_to(release).as_posix())
            return self._send(200, raw=buf.getvalue(), ctype="application/zip")
        if rest[:1] == ["files"]:
            if not release.exists():
                return self._err(404, "nothing released")
            if len(rest) == 1:
                return self._send(200, [f.relative_to(release).as_posix() for f in sorted(release.rglob("*"))
                                        if f.is_file() and "__pycache__" not in f.parts])
            target = (release / "/".join(rest[1:])).resolve()
            if release not in target.parents or not target.is_file():
                return self._err(404, "no such file")
            return self._send(200, raw=target.read_bytes(), ctype="text/plain; charset=utf-8")
        self._err(404, "no such route")


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, data_dir: Path, token: Optional[str], llm_factory: Callable[[], Any] = get_provider,
                 allow_auto: bool = False, gate_timeout: float = 3600.0, max_active: int = 2):
        super().__init__(addr, Handler)
        self.token, self.llm_factory = token, llm_factory
        self.registry = RunRegistry(data_dir, llm_factory, allow_auto, gate_timeout, max_active)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    env = os.environ
    token = env.get("AGENTIC_API_TOKEN")
    if not token and env.get("AGENTIC_ALLOW_ANON") != "1":
        raise SystemExit("AGENTIC_API_TOKEN is required (set AGENTIC_ALLOW_ANON=1 only for local experiments)")
    host, port = env.get("HOST", "0.0.0.0"), int(env.get("PORT", "8080"))
    srv = ApiServer((host, port), Path(env.get("AGENTIC_DATA_DIR", "/data")), token,
                    allow_auto=env.get("AGENTIC_ALLOW_AUTO") == "1",
                    gate_timeout=float(env.get("AGENTIC_GATE_TIMEOUT", "3600")),
                    max_active=int(env.get("AGENTIC_MAX_ACTIVE_RUNS", "2")))
    llm = get_provider()
    log.info("listening on %s:%s  llm=%s model=%s", host, port, llm.name, getattr(llm, "model", "-"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
