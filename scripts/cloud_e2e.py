#!/usr/bin/env python3
"""End-to-end test of the DEPLOYED service: submit a requirement, review the gates as a human, download the result.

  python scripts/cloud_e2e.py --url https://<app>.azurecontainerapps.io --token <api token>
  python scripts/cloud_e2e.py --url ... --token ... --requirement "Build a scalable URL shortener service with APIs, persistence, and analytics."
  python scripts/cloud_e2e.py --url ... --token ... --yes --by ci-bot      # unattended approvals (CI); default is to ask you

Flow: POST /runs -> wait for the plan gate -> show the plan -> you approve -> agents run -> wait for the release gate ->
show validation evidence -> you approve -> download release.zip and the engineering summary into ./cloud_out/<run_id>/.
Stdlib only.
"""
import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def call(base, token, method, path, body=None, raw=False):
    req = urllib.request.Request(base.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            return data if raw else json.loads(data)
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} from {path}: {exc.read().decode()[:300]}")


def wait(base, token, rid, pred, what, timeout=1800):
    end, last = time.time() + timeout, None
    while time.time() < end:
        d = call(base, token, "GET", f"/runs/{rid}")
        line = f"status={d['status']} tasks=" + ",".join(f"{t['id']}:{t['status'][:4]}" for t in d.get("tasks", [])[:10])
        if line != last:
            print("  ", line[:170])
            last = line
        if pred(d):
            return d
        time.sleep(3)
    sys.exit(f"timed out waiting for {what}")


def decide(args, d, gate_name):
    g = d["pending_gate"]
    print(f"\n=== GATE: {g['gate']} ===\n{g['summary']}\n(blocking issues: {g['blocking_issues']})")
    if args.yes:
        ok = True
    else:
        ok = input("Approve? [y/N] ").strip().lower() in ("y", "yes")
    r = call(args.url, args.token, "POST", f"/runs/{d['run_id']}/gates/{g['gate']}", {"approved": ok, "by": args.by})
    print("  decision sent:", r)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--requirement", default="Build a scalable URL shortener service with APIs, persistence, and analytics.")
    ap.add_argument("--generator", choices=["llm", "template"], default=None, help="default: llm if the server has one configured")
    ap.add_argument("--by", default="reviewer", help="who is deciding (recorded in the audit trail)")
    ap.add_argument("--yes", action="store_true", help="approve both gates without asking (CI)")
    args = ap.parse_args()

    h = json.loads(urllib.request.urlopen(args.url.rstrip("/") + "/healthz", timeout=15).read())
    print("server:", h)
    body = {"requirement": args.requirement}
    if args.generator:
        body["generator"] = args.generator
    run = call(args.url, args.token, "POST", "/runs", body)
    rid = run["run_id"]
    print(f"run {rid} started (generator={run['generator']})")

    d = wait(args.url, args.token, rid, lambda d: d.get("pending_gate") or d["status"] not in ("running", "waiting_approval"), "plan gate")
    if not d.get("pending_gate"):
        sys.exit(f"run ended before the plan gate: {d['status']} {d.get('questions') or d.get('error') or ''}")
    if not decide(args, d, "plan"):
        sys.exit("plan rejected by reviewer")
    d = wait(args.url, args.token, rid, lambda d: d.get("pending_gate") or d["status"] not in ("running", "waiting_approval"), "next gate")
    if (d.get("pending_gate") or {}).get("gate") == "test_update_approval":  # only when an agent amended an existing test
        if not decide(args, d, "test update"):
            sys.exit("test update rejected by reviewer: nothing will be released")
        d = wait(args.url, args.token, rid, lambda d: (d.get("pending_gate") or {}).get("gate") == "release_approval" or d["status"] not in ("running", "waiting_approval"), "release gate")
    if not d.get("pending_gate"):
        sys.exit(f"run ended before release: {d['status']} {d.get('error', '')}")
    print("validation:", d.get("validation"), " llm:", d.get("llm"))
    if not decide(args, d, "release"):
        sys.exit("release rejected by reviewer")
    d = wait(args.url, args.token, rid, lambda d: d["status"] not in ("running", "waiting_approval"), "completion")
    print("\nfinal status:", d["status"])
    out = Path("cloud_out") / rid
    out.mkdir(parents=True, exist_ok=True)
    (out / "ENGINEERING_SUMMARY.md").write_bytes(call(args.url, args.token, "GET", f"/runs/{rid}/summary", raw=True))
    if d["status"].startswith("completed"):
        zipfile.ZipFile(io.BytesIO(call(args.url, args.token, "GET", f"/runs/{rid}/release.zip", raw=True))).extractall(out / "release")
        print(f"downloaded to {out}  (run its tests:  cd {out / 'release'} && python -m unittest discover -s tests -t .)")
    print("decisions:", [(x["gate"], x["approved"], x["decided_by"]) for x in d["decisions"]])


if __name__ == "__main__":
    main()
