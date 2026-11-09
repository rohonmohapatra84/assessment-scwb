#!/usr/bin/env python3
"""Copy a generated release and plant one realistic bug in the copy, for demonstrating the bug-fix flow.

  python scripts/seed_bug.py output/greenfield/release output/brownfield/input/existing_code_with_bug
  python run.py custom "Fix the bug where link stats report unique_visitors equal to the total number of clicks: repeat visits from the same visitor are counted as separate unique visitors." --repo output/brownfield/input/existing_code_with_bug --out output/bugfix

(`python run.py brownfield` does both steps for you.)

The bug: the stats query counts every click as a unique visitor (COUNT(DISTINCT visitor_hash) becomes COUNT(visitor_hash)).
The service's own tests notice, so the baseline suite starts RED; a correct fix turns it green.
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentic.harness.guardrails import robust_rmtree  # noqa: E402  (retries the delete: OneDrive and editors briefly lock folders)

GOOD, BAD = "COUNT(DISTINCT visitor_hash)", "COUNT(visitor_hash)"


def seed(src: Path, dst: Path) -> Path:
    """Copy `src` to `dst` (replacing it) and plant the bug. Returns the changed file. Raises RuntimeError if it cannot."""
    src, dst = Path(src), Path(dst)
    target_rel = Path("shortener") / "storage.py"
    if not (src / target_rel).exists():
        raise RuntimeError(f"{src} does not look like a generated release (no shortener/storage.py)")
    if GOOD not in (src / target_rel).read_text(encoding="utf-8"):
        raise RuntimeError("expected query not found in shortener/storage.py; was this release generated differently?")
    if dst.exists():
        try:
            robust_rmtree(dst)
        except OSError as exc:
            raise RuntimeError(f"could not replace {dst}: {exc}") from exc
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db*", "*.sqlite*"))
    target = dst / target_rel
    target.write_text(target.read_text(encoding="utf-8").replace(GOOD, BAD), encoding="utf-8")
    return target


def describe(target: Path) -> dict:
    """Where the bug was planted, for the run story: file, line number, the buggy line and what it was before."""
    target = Path(target)
    for n, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        if BAD in line:
            return {"file": "shortener/" + target.name, "line": n, "buggy": line.strip(), "original": line.replace(BAD, GOOD).strip()}
    return {"file": "shortener/" + target.name, "line": 0, "buggy": BAD, "original": GOOD}


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    try:
        target = seed(Path(argv[1]), Path(argv[2]))
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f"seeded bug in {target}\n  {GOOD}  ->  {BAD}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
