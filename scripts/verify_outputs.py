#!/usr/bin/env python3
"""Check, without trusting any report, whether each scenario's output really works.

  python scripts/verify_outputs.py                 # every scenario found under output/
  python scripts/verify_outputs.py greenfield      # just one
  python scripts/verify_outputs.py --skip-tests    # read the records only (fast); the default also RE-RUNS each release's own tests
  python scripts/verify_outputs.py --output-dir output_claude brownfield   # check results kept in another folder

For each scenario it reads what the harness recorded (summary.json, state.json) and then checks the files on disk. The release's
test suite is executed again here, in a fresh process, so a green result does not depend on the harness having said so.
Exit code 0 only if every scenario that was run passes. Scenarios never run are reported as NOT RUN and do not fail the check.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

# scenario -> (folder, what it proves)
SCENARIOS = {
    "greenfield": ("greenfield", "the mandatory URL shortener, written by the model and validated"),
    "brownfield": ("brownfield", "bug fix on a planted defect: regression test must fail on the old code, pass on the fix"),
    "enhancement": ("enhancement", "link expiry (changes the API): an obsolete test may be amended, with human approval"),
    "ambiguous": ("ambiguous", "vague requirement: clarification gate, then a gap-aware proposal"),
    "refactor": ("refactor", "behaviour-preserving change: API identical"),
    "tests": ("tests_improvement", "tests-only change: no production file touched"),
    "chaos": ("greenfield_chaos", "greenfield with injected faults: retry, repair, recovery"),
}


class Result:
    def __init__(self, name: str):
        self.name, self.lines, self.failed = name, [], False

    def check(self, ok: bool, text: str, detail: str = "") -> bool:
        self.lines.append(f"    [{'PASS' if ok else 'FAIL'}] {text}" + (f"  ({detail})" if detail and not ok else ""))
        self.failed |= not ok
        return ok

    def info(self, text: str) -> None:
        self.lines.append(f"    [info] {text}")


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_release_tests(release: Path, timeout: int = 600):
    """Run the released project's own test suite in a fresh, isolated interpreter."""
    proc = subprocess.run([sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=release,
                          capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    ran = next((ln for ln in out.splitlines() if ln.startswith("Ran ")), "Ran ? tests")
    return proc.returncode == 0, ran, out[-600:]


def print_result(key: str, res: "Result") -> None:
    print(f"\n{key:<12} {'FAIL' if res.failed else 'OK'}")
    print("\n".join(res.lines))


def verify(key: str, skip_tests: bool, d: Path = None) -> Result:
    """Check one scenario. `d` is the folder a run wrote to; by default the standard folder under output/."""
    folder, purpose = SCENARIOS[key]
    d = Path(d) if d else OUTPUT / folder
    res = Result(key)
    res.info(purpose)
    summary, state = load(d / "summary.json"), load(d / "state.json")
    if key == "ambiguous" and (d / "clarifications.json").exists() and not summary:
        res.check(False, "run only paused for answers; re-run with --answers input/ambiguous/answers.json")
        return res
    if not summary or not state:
        res.check(False, f"no summary.json/state.json in {d} (run it first)")
        return res
    bb = state.get("blackboard", {})
    status = summary.get("status")
    res.check(status in ("completed", "completed_degraded"), f"run status is completed (got {status!r})")
    checks = (bb.get("validation_report") or {}).get("checks", [])
    failed = [c["check"] for c in checks if c["status"] == "FAIL"]
    res.check(bool(checks) and not failed, f"validator: {len(checks)} checks, none failed", ", ".join(failed))
    gates = {g["gate"]: g for g in summary.get("gates", [])}
    res.check(all(g["approved"] for g in gates.values()) and "release_approval" in gates,
              "human gates all approved: " + ", ".join(f"{k} by {v['decided_by']}" for k, v in gates.items()))
    release = d / "release"
    if not res.check(release.is_dir(), "release/ exists (only written after the release gate)"):
        return res

    names = {c["check"]: c["status"] for c in checks}
    if key == "greenfield" or key == "chaos":
        res.check((release / "openapi.json").exists() and (release / "schema.sql").exists(), "openapi.json and schema.sql released")
        res.check(len(list((release / "shortener").glob("*.py"))) >= 5, "service package has code modules")
        if (bb.get("requirement") or {}).get("generator") == "llm":  # the probe judges model-written code only
            res.check(names.get("probe.behaviour") == "PASS", "black-box behavioural probe passed")
        else:
            res.info("template-mode run (offline reference): the behavioural probe does not apply")
        if key == "chaos":
            res.check(sum((summary["report"].get("repairs") or {}).values()) + summary["report"].get("retries", 0) > 0,
                      "faults were injected and recovered from (retries or repairs recorded)")
    elif key == "brownfield":
        proof = names.get("regression.fails_on_old_code")
        res.check(proof == "PASS", "new tests FAIL on the buggy baseline (they prove the defect)",
                  "not recorded: this output predates the check; re-run `python run.py brownfield`" if proof is None else f"check is {proof}")
        res.check(names.get("regression.suite_green_after_fix") == "PASS", "suite is green after the fix")
        res.check(not (bb.get("baseline_tests") or {}).get("ok", True), "baseline really was red (the planted bug was seen)")
    elif key == "enhancement":
        res.check("expir" in (release / "openapi.json").read_text(encoding="utf-8").lower(), "released openapi.json documents expiry")
        res.check(names.get("compat.openapi_additive_only") == "PASS", "API change is additive only")
        updates = bb.get("test_updates") or []
        res.info(f"existing tests amended: {[u['path'] for u in updates] or 'none needed'}")
        if updates:
            res.check("test_update_approval" in gates and gates["test_update_approval"]["approved"],
                      "every amended test was approved at the test_update_approval gate")
        res.check(names.get("tests.existing_tests_protected") == "PASS", "no existing test changed outside the policy")
    elif key == "ambiguous":
        res.check((release / "docs" / "PROPOSAL.md").exists(), "docs/PROPOSAL.md released")
        res.check(bool(bb.get("options")), "objectives and options with trade-offs were produced")
    elif key == "refactor":
        res.check(names.get("compat.openapi_unchanged") == "PASS", "OpenAPI identical to the baseline")
    elif key == "tests":
        res.check(names.get("scope.tests_docs_only") == "PASS", "only tests/docs were written")

    if skip_tests or key == "ambiguous":
        return res
    if (release / "tests").is_dir():
        ok, ran, tail = run_release_tests(release)
        res.check(ok, f"RE-RAN the release's own tests just now: {ran}", tail)
    else:
        res.check(False, "release has no tests/ directory")
    return res


def main(argv) -> int:
    global OUTPUT
    argv = list(argv)
    if "--output-dir" in argv:  # check a different set of results, e.g. runs made with another model
        i = argv.index("--output-dir")
        if i + 1 >= len(argv):
            print("--output-dir needs a folder")
            return 2
        OUTPUT = Path(argv[i + 1]).resolve()
        del argv[i:i + 2]
        print(f"checking results in {OUTPUT}")
    skip = "--skip-tests" in argv
    wanted = [a for a in argv if not a.startswith("--")] or list(SCENARIOS)
    unknown = [w for w in wanted if w not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s) {unknown}; choose from {sorted(SCENARIOS)}")
        return 2
    bad = False
    for key in wanted:
        folder = SCENARIOS[key][0]
        if not (OUTPUT / folder).is_dir():
            print(f"\n{key:<12} NOT RUN  (no output/{folder})")
            continue
        res = verify(key, skip)
        print_result(key, res)
        bad |= res.failed
    print("\n" + ("SOME CHECKS FAILED" if bad else "ALL SCENARIOS THAT WERE RUN ARE WORKING"))
    return 1 if bad else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
