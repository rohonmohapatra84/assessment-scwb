#!/usr/bin/env python3
"""Use the service a finished run released, and write DEMO_RESULT.md next to it. No model is called.

  python scripts/demo_service.py output/greenfield      # shorten real URLs with the service the agents built
  python scripts/demo_service.py output/brownfield      # also shows the bug BEFORE and AFTER the fix

It starts <run>/release on a private port from a temporary copy, shortens two real URLs, follows the redirects, clicks a link three times
and reads the analytics, then stops the service. (python run.py does the same automatically at the end of every run.)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentic.harness.demo import write_demo_result  # noqa: E402


def main(argv) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    run_dir = Path(argv[0]).resolve()
    if not (run_dir / "release" / "shortener" / "server.py").exists():
        print(f"{run_dir} has no release/shortener/server.py. Run the scenario first (and approve the release gate).")
        return 1
    request, kind = "", None
    try:
        bb = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["blackboard"]
        request = (bb.get("input") or {}).get("text", "")
        kind = (bb.get("requirement") or {}).get("change_kind")
    except (OSError, ValueError, KeyError):
        pass
    print(f"using the service in {run_dir / 'release'} ...")
    summary = write_demo_result(run_dir, request, kind)
    print(f"long URL : {summary.get('first_long')}")
    print(f"short URL: {summary.get('first_short')}")
    print(f"redirect : HTTP {summary.get('redirect')} -> {'lands on the long URL' if summary.get('redirect_ok') else 'DID NOT land on the long URL'}")
    print(f"analytics: total_clicks={summary.get('total_clicks')} unique_visitors={summary.get('unique_visitors')}"
          + (f"   (before the fix: unique_visitors={summary.get('before_unique')})" if summary.get("before_unique") is not None else ""))
    if summary.get("error"):
        print("problem  :", summary["error"])
    print(f"written  : {run_dir / 'DEMO_RESULT.md'}")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
