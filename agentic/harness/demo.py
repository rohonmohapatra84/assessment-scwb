"""The live demo: actually USE the service the agents built, and write down what happened (DEMO_RESULT.md).

Validation already drives the service with probes, but nothing recorded a real long URL turning into a short one. After a release, this
starts the released service on a private port, shortens real URLs, follows the redirects, clicks a link a few times and reads the
analytics. For a bug fix it also runs the same steps on the ORIGINAL buggy code, so the before and after sit side by side.

Safety: the service runs from a temporary COPY of the release (the release folder is never locked or written to), on 127.0.0.1 only,
with a scrubbed environment, a hard timeout, and it is always stopped. It relies only on the published interface contract
(`shortener.server.main`, `--host/--port`, env `SHORTENER_DB`, `SHORTENER_BASE_URL`, and the documented HTTP API). Any failure is reported
inside the demo result; it can never fail the run it describes.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .guardrails import robust_rmtree

SAMPLE_URLS = [
    "https://en.wikipedia.org/wiki/URL_shortening",
    "https://example.com/products/summer-sale?utm_source=newsletter&utm_campaign=2026&ref=homepage",
]
CLICKS = 3  # redirects of the first link, all from one client: total_clicks should be 3 and unique_visitors 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(port: int, method: str, path: str, body: Optional[dict] = None, timeout: float = 5.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = None
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed
    finally:
        conn.close()


def run_demo(service_dir: Path, label: str, *, startup_s: float = 25.0, stats_wait_s: float = 8.0) -> Dict[str, Any]:
    """Run the service in `service_dir` (copied first), shorten URLs, follow redirects, read stats. Never raises."""
    result: Dict[str, Any] = {"label": label, "ok": False, "links": [], "stats": None, "error": None, "log_tail": ""}
    tmp = Path(tempfile.mkdtemp(prefix="demo_"))
    proc, log = None, None
    try:
        service_dir = Path(service_dir)
        if not (service_dir / "shortener" / "server.py").exists():
            result["error"] = f"no shortener/server.py in {service_dir}: nothing to run"
            return result
        svc = tmp / "svc"
        shutil.copytree(service_dir, svc, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "*.db-wal", "*.db-shm"))
        port = _free_port()
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC")}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "SHORTENER_DB": str(tmp / "demo.db"), "SHORTENER_BASE_URL": f"http://127.0.0.1:{port}"})
        code = f"from shortener.server import main; main(['--host','127.0.0.1','--port','{port}'])"
        log = (tmp / "server.log").open("w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=str(svc), env=env, stdout=log, stderr=subprocess.STDOUT)
        result["base_url"] = f"http://127.0.0.1:{port}"

        deadline, healthy = time.time() + startup_s, False
        while time.time() < deadline and proc.poll() is None:
            try:
                if _call(port, "GET", "/healthz", timeout=1.0)[0] == 200:
                    healthy = True
                    break
            except OSError:
                time.sleep(0.2)
        if not healthy:
            result["error"] = ("the service exited at start-up" if proc.poll() is not None else "the service did not answer /healthz in time")
            return result

        for i, long_url in enumerate(SAMPLE_URLS):
            entry: Dict[str, Any] = {"long_url": long_url}
            status, _, body = _call(port, "POST", "/api/v1/links", {"url": long_url})
            entry["create_status"] = status
            body = body if isinstance(body, dict) else {}
            entry["code"] = body.get("code")
            entry["short_url"] = body.get("short_url") or (f"http://127.0.0.1:{port}/{body['code']}" if body.get("code") else None)
            if entry["code"]:
                n = CLICKS if i == 0 else 1
                statuses = []
                for _ in range(n):  # redirects are NOT followed: we read the Location header the service sends
                    s, h, _b = _call(port, "GET", "/" + entry["code"])
                    statuses.append((s, h.get("location")))
                entry["redirect_status"], entry["location"] = statuses[0]
                entry["redirect_ok"] = entry["redirect_status"] in (301, 302, 303, 307, 308) and entry["location"] == long_url
                entry["clicks"] = n
            result["links"].append(entry)

        first = result["links"][0] if result["links"] else {}
        if first.get("code"):
            stats = None
            end = time.time() + stats_wait_s  # analytics are eventually consistent: wait for the batch flush
            while time.time() < end:
                s, _h, body = _call(port, "GET", f"/api/v1/links/{first['code']}/stats?days=7")
                if s == 200 and isinstance(body, dict):
                    stats = body
                    if body.get("total_clicks", 0) >= first.get("clicks", CLICKS):
                        break
                time.sleep(0.4)
            result["stats"] = {k: stats.get(k) for k in ("total_clicks", "unique_visitors")} if stats else None
        result["ok"] = bool(result["links"]) and all(l.get("redirect_ok") for l in result["links"]) and result["stats"] is not None
        if not result["ok"] and not result["error"]:
            result["error"] = "the service ran but a step did not behave as documented (see the table)"
        return result
    except Exception as exc:  # the demo describes the run; it must never break it
        result["error"] = f"demo crashed: {type(exc).__name__}: {exc}"
        return result
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log is not None:
            log.close()  # an open handle would stop the temporary folder from being deleted on Windows
        try:
            log_path = tmp / "server.log"
            if log_path.exists() and not result["ok"]:
                result["log_tail"] = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:])
            robust_rmtree(tmp)
        except OSError:
            pass


def render_demo_md(after: Dict[str, Any], before: Optional[Dict[str, Any]], request: str, kind: Optional[str]) -> str:
    L: List[str] = ["# Demo result: the service the agents built, actually used", ""]
    L.append("After the release, the harness started the **released service** on a private port, shortened real URLs, followed the redirects and read the analytics. "
             "Nothing here is mocked: these are real HTTP calls to the code the agents wrote. The service ran from a temporary copy and was stopped afterwards, "
             "so the short URLs below no longer resolve.\n")
    L.append(f"**Request this run answered:** {request}\n")
    L.append(f"## {after['label']}\n")
    if after.get("error"):
        L.append(f"> **The demo did not fully work:** {after['error']}\n")
        if after.get("log_tail"):
            L.append("```\n" + after["log_tail"] + "\n```\n")
    if after["links"]:
        L.append("| # | Long URL (input) | Short code | Short URL (output) | Create | Redirect | Lands on the long URL? |")
        L.append("|---|---|---|---|---|---|---|")
        for i, l in enumerate(after["links"], 1):
            L.append(f"| {i} | `{l['long_url']}` | `{l.get('code')}` | `{l.get('short_url')}` | {l.get('create_status')} | "
                     f"{l.get('redirect_status')} | {'**YES**' if l.get('redirect_ok') else '**NO**'} (`Location: {l.get('location')}`) |")
        L.append("")
    if after.get("stats"):
        s = after["stats"]
        L.append(f"**Analytics for short code #1** after {after['links'][0].get('clicks', CLICKS)} clicks from one visitor: "
                 f"`total_clicks = {s.get('total_clicks')}`, `unique_visitors = {s.get('unique_visitors')}`.\n")
    if kind == "bugfix" and before is not None:
        L.append("## The bug fix, before and after\n")
        L.append(f"Same steps, run on the **original buggy code** and on the **fixed code**: {CLICKS} clicks on one link from one visitor.\n")
        L.append("| | total_clicks | unique_visitors | Correct? |")
        L.append("|---|---|---|---|")
        for r, name in ((before, "BEFORE (the planted bug)"), (after, "AFTER (the agents' fix)")):
            s = r.get("stats") or {}
            ok = s.get("total_clicks") == CLICKS and s.get("unique_visitors") == 1
            L.append(f"| {name} | {s.get('total_clicks')} | {s.get('unique_visitors')} | {'**YES**' if ok else '**NO**: one visitor should count once'} |")
        L.append("")
        if before.get("error"):
            L.append(f"*(the BEFORE run reported: {before['error']})*\n")
    L.append("## Try it yourself\n")
    L.append("```powershell\ncd release\npython -m shortener.server --port 8080\n```\n")
    L.append("Then, in a second terminal:\n")
    L.append("```powershell\n$r = Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/v1/links -ContentType 'application/json' "
             "-Body '{\"url\":\"https://example.com/some/long/path\"}'\n$r\ncurl.exe -si \"http://localhost:8080/$($r.code)\"\n```\n")
    return "\n".join(L)


def write_demo_result(run_dir: Path, request: str, kind: Optional[str], *, release: Optional[Path] = None,
                      baseline: Optional[Path] = None) -> Dict[str, Any]:
    """Run the demo for a finished run and write <run_dir>/DEMO_RESULT.md. Returns a small summary for the event log."""
    run_dir = Path(run_dir)
    after = run_demo(release or run_dir / "release", "The released service in use")
    before = run_demo(baseline or run_dir / "baseline", "The original (buggy) service in use") if kind == "bugfix" else None
    (run_dir / "DEMO_RESULT.md").write_text(render_demo_md(after, before, request, kind), encoding="utf-8")
    first = after["links"][0] if after["links"] else {}
    return {"ok": after["ok"], "first_long": first.get("long_url"), "first_short": first.get("short_url"), "redirect": first.get("redirect_status"),
            "redirect_ok": first.get("redirect_ok"), "total_clicks": (after.get("stats") or {}).get("total_clicks"),
            "unique_visitors": (after.get("stats") or {}).get("unique_visitors"),
            "before_unique": ((before or {}).get("stats") or {}).get("unique_visitors") if before else None,
            "error": after.get("error")}
