#!/usr/bin/env python3
"""Shorten YOUR OWN URL with the service the agents built. No model is called.

  python scripts/shorten_url.py https://www.python.org/downloads/
  python scripts/shorten_url.py https://example.com/a/very/long/path?x=1 https://www.wikipedia.org/ --alias my-link
  python scripts/shorten_url.py <url> --once                 # shorten, check the redirect, show analytics, then stop
  python scripts/shorten_url.py <url> --service-dir output/enhancement/release --port 9000

By default the service stays running after shortening, so you can open the short URL in a browser (each visit counts as a click).
Press Ctrl+C to stop it. The service runs from a temporary COPY of the release on 127.0.0.1 only; the release folder is never locked or written to.

Notes: the service accepts only public http(s) hosts (it resolves the host name, so you need internet access), rejects credentials in a URL,
and limits a URL to 2048 characters. A rejected URL prints the service's own error message.
"""
import argparse
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agentic.harness.guardrails import robust_rmtree  # noqa: E402


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
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


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # show each line at once: the script then waits while the service runs
    p = argparse.ArgumentParser(description="Shorten your own URL with the service the agents built.")
    p.add_argument("urls", nargs="+", help="one or more long URLs (http or https)")
    p.add_argument("--service-dir", default=str(ROOT / "output" / "greenfield" / "release"), help="a released service folder (default: output/greenfield/release)")
    p.add_argument("--alias", help="custom alias (3-32 letters, digits, _ or -); applies to the first URL only")
    p.add_argument("--port", type=int, default=8080, help="port to serve on (default 8080; a free port is used if it is busy)")
    p.add_argument("--once", action="store_true", help="do not keep the service running afterwards")
    args = p.parse_args(argv)

    service_dir = Path(args.service_dir).resolve()
    if not (service_dir / "shortener" / "server.py").exists():
        print(f"no service found in {service_dir}.\nBuild it first with: python run.py greenfield   (or pass --service-dir <a release folder>)")
        return 1

    port = args.port
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
    except OSError:
        port = free_port()
        print(f"(port {args.port} is busy, using {port})")

    tmp = Path(tempfile.mkdtemp(prefix="shorten_"))
    proc, log, failed = None, None, 0
    try:
        svc = tmp / "svc"
        shutil.copytree(service_dir, svc, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "*.db-wal", "*.db-shm"))
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC")}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "SHORTENER_DB": str(tmp / "links.db"), "SHORTENER_BASE_URL": f"http://127.0.0.1:{port}"})
        code = f"from shortener.server import main; main(['--host','127.0.0.1','--port','{port}'])"
        log = (tmp / "server.log").open("w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-B", "-c", code], cwd=str(svc), env=env, stdout=log, stderr=subprocess.STDOUT)

        deadline, healthy = time.time() + 25, False
        while time.time() < deadline and proc.poll() is None:
            try:
                if call(port, "GET", "/healthz")[0] == 200:
                    healthy = True
                    break
            except OSError:
                time.sleep(0.2)
        if not healthy:
            print("the service did not start. Last lines of its log:")
            log.flush()
            print("\n".join((tmp / "server.log").read_text(encoding="utf-8", errors="replace").splitlines()[-15:]))
            return 1
        print(f"service running at http://127.0.0.1:{port} (from {service_dir})\n")

        first_code = None
        results = []  # (long_url, short_url) of every link that works, repeated at the end where the user will look
        for i, long_url in enumerate(args.urls):
            body = {"url": long_url}
            if args.alias and i == 0:
                body["custom_alias"] = args.alias
            status, _, resp = call(port, "POST", "/api/v1/links", body)
            if status != 201 or not isinstance(resp, dict) or not resp.get("code"):
                failed += 1
                msg = (resp or {}).get("error", {}).get("message") if isinstance(resp, dict) else None
                print(f"[REJECTED] {long_url}\n           HTTP {status}: {msg or resp}\n")
                continue
            first_code = first_code or resp["code"]
            short = resp.get("short_url") or f"http://127.0.0.1:{port}/{resp['code']}"
            rs, headers, _ = call(port, "GET", "/" + resp["code"])
            ok = rs in (301, 302, 303, 307, 308) and headers.get("location") == long_url
            failed += 0 if ok else 1
            if ok:
                results.append((long_url, short))
            print(f"long URL : {long_url}\nshort URL: {short}\nredirect : HTTP {rs} -> {headers.get('location')}   "
                  f"[{'OK, lands on your URL' if ok else 'DOES NOT match your URL'}]\n")

        if first_code:
            stats = None
            end = time.time() + 8  # analytics are written in batches: wait for the flush
            while time.time() < end:
                s, _, body = call(port, "GET", f"/api/v1/links/{first_code}/stats?days=7")
                if s == 200 and isinstance(body, dict):
                    stats = body
                    if body.get("total_clicks", 0) >= 1:
                        break
                time.sleep(0.4)
            if stats:
                print(f"analytics for {first_code}: total_clicks={stats.get('total_clicks')} unique_visitors={stats.get('unique_visitors')}\n")

        if args.once or not first_code:
            if first_code:
                print("The service has stopped, so the short URL above no longer opens in a browser. Re-run without --once to keep it running.")
            return 1 if failed else 0
        print("=" * 78)
        print("YOUR SHORT URL IS LIVE. Open it in your browser now (this terminal must stay open):\n")
        for long_url, short in results:
            print(f"  {short}\n     -> {long_url}")
        print(f"\nCheck its analytics after a few clicks:\n  Invoke-RestMethod \"http://127.0.0.1:{port}/api/v1/links/{first_code}/stats?days=7\"\n"
              f"\nPress Ctrl+C here to stop the service (the short URL stops working then).")
        print("=" * 78)
        try:
            while proc.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nstopped.")
        return 1 if failed else 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log is not None:
            log.close()
        try:
            robust_rmtree(tmp)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
