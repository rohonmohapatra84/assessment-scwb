#!/usr/bin/env python3
"""CLI for the agentic engineering system.

Every scenario reads its INPUT from input/<scenario>/requirement.txt and writes its OUTPUT to output/<scenario>/ (see input/README.md).

  python run.py demo                       # greenfield + brownfield bug fix + ambiguous, end to end
  python run.py greenfield                 # mandatory URL-shortener use case
  python run.py brownfield                 # BUG FIX: plant a bug in a copy of the shortener, then fix it (your first brownfield test)
  python run.py enhancement                # add link expiry (changes the API; may amend an obsolete test with human approval)
  python run.py refactor                   # behaviour-preserving restructuring
  python run.py tests                      # tests-only improvement
  python run.py ambiguous                  # vague request -> clarification gate -> proposal
  python run.py custom "<requirement>" [--repo PATH] [--answers FILE] [--assume-defaults]
  python run.py agents                     # model-backed agents (agentic/agents/*.md) and deterministic steps

Common flags: --approve auto|interactive|reject|reject:<gate>   --inject SPEC (repeatable)   --out DIR   --resume   --verify
              --requirement-file FILE   (use your own requirement instead of input/<scenario>/requirement.txt)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agentic.providers import get_provider, load_dotenv  # noqa: E402
from agentic.harness.harness import Harness, RunResult  # noqa: E402

load_dotenv()  # picks up AZURE_AI_* from ./.env if present (never overrides real environment variables)

INPUT = ROOT / "input"
OUTPUT = ROOT / "output"


def requirement_for(args, name: str):
    """The requirement text for a scenario: input/<name>/requirement.txt, or --requirement-file. Returns (text, path)."""
    path = Path(args.requirement_file) if getattr(args, "requirement_file", None) else INPUT / name / "requirement.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit(f"requirement file not found: {path}")
    if not text:
        raise SystemExit(f"requirement file is empty: {path}")
    return text, path


def archive_input(result: RunResult, req_path: Path, answers_path: Path = None, extras: dict = None) -> None:
    """Copy the input a run used into its output folder, so every output is self-describing (output/<scenario>/input/)."""
    dest = result.out_dir / "input"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(req_path, dest / "requirement.txt")
    if answers_path and Path(answers_path).exists():
        shutil.copyfile(answers_path, dest / "answers.json")
    for name, text in (extras or {}).items():
        (dest / name).write_text(text, encoding="utf-8")


def out_dir(args, name: str) -> Path:
    """The model is the default. The offline reference mode (used by the test suite) gets its own folder so it can
    never be mistaken for a real run."""
    return Path(args.out) if args.out else OUTPUT / (name + ("_template" if args.generator == "template" else ""))


def banner(text: str) -> None:
    print("\n" + "=" * 78 + f"\n{text}\n" + "=" * 78)


def _harness(args, name: str, **kw) -> Harness:
    return Harness(out_dir(args, name), approval=args.approve, inject=args.inject, resume=args.resume,
                   llm=get_provider(), generator=args.generator, demo=True, **kw)


def run_greenfield(args) -> RunResult:
    text, path = requirement_for(args, "greenfield")
    banner("SCENARIO 1 - GREENFIELD: " + text)
    result = _harness(args, "greenfield").run(text)
    archive_input(result, path)
    return result


def _ensure_baseline(args) -> Path:
    baseline = OUTPUT / "greenfield" / "release"
    if not (baseline / "shortener").exists():
        print("(no greenfield release found. A brownfield change needs an existing service to change, so the URL shortener is being "
              "built first: about 9 minutes with a real model. This happens once.)")
        text = (INPUT / "greenfield" / "requirement.txt").read_text(encoding="utf-8").strip()
        r = Harness(OUTPUT / "greenfield", approval="auto", echo=lambda _l: None, llm=get_provider(),
                    generator=args.generator).run(text)
        if not r.ok:
            raise SystemExit("could not build baseline service: " + r.status)
    return baseline


def _seed_buggy_copy(args) -> Path:
    """Existing code with a realistic planted defect: a copy of the greenfield release, so the original stays untouched.
    It lives INSIDE the run folder (<out>/input/existing_code_with_bug) so one folder holds everything about the run."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import seed_bug
    run_dir = out_dir(args, "brownfield")
    target = run_dir / "input" / "existing_code_with_bug"
    try:
        seed_bug.seed(_ensure_baseline(args), target)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    print(f"(planted a bug in a copy: {target})")
    return target


def bugfix_story_preface(repo: Path, request: str) -> str:
    """The setup of the bug-fix demo, told at the top of RUN_STORY.md: what was planted, where, and who knows what."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import seed_bug
    d = seed_bug.describe(repo / "shortener" / "storage.py")
    return (
        "## Stage 0: Scenario setup (done by the demo script BEFORE any agent runs)\n\n"
        "This is a **brownfield** run: the URL shortener already exists and someone wants a change. For the bug-fix demo the script first "
        "creates a real defect to fix.\n\n"
        f"1. **Copied the existing service** to `{repo}` (your original `output/greenfield/release` is untouched).\n"
        f"2. **Planted ONE bug** in that copy, in `{d['file']}` at line {d['line']}:\n\n"
        f"   - before (correct): `{d['original']}`\n"
        f"   - after (buggy):    `{d['buggy']}`\n\n"
        "   Effect: the stats query counts every click as a *unique visitor*, so `unique_visitors` always equals `total_clicks`. "
        "The service's own tests notice, so the existing test suite starts **red**.\n"
        f"3. **A person reports the problem in plain English**. That sentence is the requirement the agents receive:\n\n"
        f"   > {request}\n\n"
        "**What the agents are and are not told:** they get only that sentence and the code. They are **not** told the file, the line "
        "or the fix. Finding *where* the behaviour lives (codebase analysis), fixing it minimally, writing tests that fail on the "
        "bug, and proving it is their job. The agents do not discover that a bug exists; the report does.\n")


def run_brownfield(args) -> RunResult:
    text, path = requirement_for(args, "brownfield")
    banner("SCENARIO 2 - BROWNFIELD BUG FIX: " + text)
    if not args.repo:
        print("What this does: (1) copies the URL shortener and plants ONE bug in the copy, so you have a real defect to fix; (2) a person\n"
              "(you) reports it in plain English, as in the request above; (3) the agents locate the faulty code, fix it and write tests that\n"
              "fail on the buggy code; (4) the validator proves it. The agents do not discover the bug: the request says what is wrong.\n")
    repo = args.repo or str(_seed_buggy_copy(args))
    preface = bugfix_story_preface(Path(repo), text) if not args.repo else ""
    result = _harness(args, "brownfield", story_preface=preface).run(text, target_repo=repo)
    archive_input(result, path, extras={"existing_code_and_planted_bug.md": preface} if preface else None)
    return result


def _change_to_existing_code(args, name: str, title: str) -> RunResult:
    text, path = requirement_for(args, name)
    banner(f"{title}: {text}")
    repo = args.repo or str(_ensure_baseline(args))
    result = _harness(args, name).run(text, target_repo=repo)
    archive_input(result, path)
    return result


def run_enhancement(args) -> RunResult:
    return _change_to_existing_code(args, "enhancement", "SCENARIO 2b - BROWNFIELD ENHANCEMENT (changes the API)")


def run_refactor(args) -> RunResult:
    return _change_to_existing_code(args, "refactor", "SCENARIO 2c - BROWNFIELD REFACTOR (behaviour must not change)")


def run_tests(args) -> RunResult:
    return _change_to_existing_code(args, "tests_improvement", "SCENARIO 2d - BROWNFIELD TESTS ONLY (no production code may change)")


def run_ambiguous(args) -> RunResult:
    text, path = requirement_for(args, "ambiguous")
    banner("SCENARIO 3 - AMBIGUOUS: " + text)
    repo = args.repo or str(_ensure_baseline(args))
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8")) if args.answers else None
    result = _harness(args, "ambiguous").run(text, target_repo=repo, answers=answers, assume_defaults=args.assume_defaults)
    archive_input(result, path, answers_path=args.answers)
    return result


def run_custom(args) -> RunResult:
    banner("CUSTOM REQUIREMENT: " + args.text)
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8")) if args.answers else None
    return _harness(args, "custom").run(args.text, target_repo=args.repo, answers=answers, assume_defaults=args.assume_defaults)


def report(result: RunResult) -> int:
    print(f"\n>> status: {result.status}")
    if result.questions:
        print(">> the analyst needs answers before it can plan:")
        for q in result.questions:
            print(f"   {q['id']}: {q['question']}\n       (default if you say 'assume': {q['default']})")
        print(f">> see {result.out_dir / 'clarifications.json'}; re-run with --answers input/ambiguous/answers.json or --assume-defaults")
    story = result.out_dir / "RUN_STORY.md"
    if story.exists():
        print(f">> run story (plain English, what happened and why): {story}")
    if (result.out_dir / "DEMO_RESULT.md").exists():
        print(f">> demo result (the service really used: long URL -> short URL -> redirect): {result.out_dir / 'DEMO_RESULT.md'}")
    if (result.out_dir / "input").is_dir():
        print(f">> input used: {result.out_dir / 'input'}")
    if result.summary_path:
        print(f">> engineering summary: {result.summary_path}")
    if result.status == "completed" or result.status == "completed_degraded":
        print(f">> released artifacts:  {result.out_dir / 'release'}")
    return 0 if result.ok or result.status == "needs_clarification" else 1


def verify_run(key: str, result: RunResult) -> int:
    """--verify: re-check this run from the files on disk (and re-run the release's own tests). One verdict."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import verify_outputs
    res = verify_outputs.verify(key, False, result.out_dir)
    verify_outputs.print_result(key, res)
    print("\n>> VERIFIED: this run really works." if not res.failed else "\n>> NOT VERIFIED: see the FAIL lines above.")
    return 1 if res.failed else 0


def demo(args) -> int:
    answers_file = INPUT / "ambiguous" / "answers.json"
    codes = []
    codes.append(report(run_greenfield(args)))
    codes.append(report(run_brownfield(args)))
    banner("SCENARIO 3a - AMBIGUOUS, first pass (no answers): harness must PAUSE")
    text, _ = requirement_for(args, "ambiguous")
    first = Harness(OUTPUT / "ambiguous", approval=args.approve, llm=get_provider(), generator=args.generator).run(
        text, target_repo=str(OUTPUT / "greenfield" / "release"))
    codes.append(report(first))
    banner("SCENARIO 3b - AMBIGUOUS, human answers supplied: harness resumes and completes")
    args.answers = str(answers_file)
    codes.append(report(run_ambiguous(args)))
    return max(codes)


def list_agents(args) -> int:
    """Show the model-backed agents (Markdown specs) and the deterministic steps, and check they are consistent."""
    from agentic.agent_specs import check_specs, describe, load_specs
    from agentic.harness.harness import agent_registry
    registry = agent_registry()
    print(describe(load_specs().values(), registry))
    problems = check_specs(registry)
    print("\nspecs and registered agents are consistent" if not problems else "\nPROBLEMS:\n  " + "\n  ".join(problems))
    return 1 if problems else 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", choices=["demo", "greenfield", "brownfield", "enhancement", "refactor", "tests", "ambiguous", "custom", "agents"])
    p.add_argument("text", nargs="?", help="requirement text (custom only)")
    p.add_argument("--repo", help="existing repository to modify (brownfield)")
    p.add_argument("--answers", help="JSON file {\"Q1\": \"answer\"} for clarification questions (see input/ambiguous/answers.json)")
    p.add_argument("--assume-defaults", action="store_true", help="resolve ambiguities with documented defaults")
    p.add_argument("--approve", default="auto", help="auto | interactive | reject | reject:<gate>")
    p.add_argument("--inject", action="append", default=[], help="fault injection: transient:<task>:<n> | defect:<task> | fatal:<task>")
    p.add_argument("--generator", choices=["llm", "template"], default="llm",
                   help="llm (default) = the model writes the code and tests. template = offline reference mode used by the test suite, not a use case")
    p.add_argument("--out", help="output directory")
    p.add_argument("--resume", action="store_true", help="resume from the checkpoint in --out")
    p.add_argument("--verify", action="store_true",
                   help="after a scenario, re-check the run from disk and re-run the release's own tests, then print one verdict")
    p.add_argument("--requirement-file", help="use this requirement text file instead of input/<scenario>/requirement.txt")
    args = p.parse_args()
    if args.scenario == "custom" and not args.text:
        p.error("custom requires the requirement text")

    def with_verify(key, runner):
        def go(a):
            result = runner(a)
            code = report(result)
            if a.verify:
                code = max(code, verify_run(key, result) if result.ok else 1)
            return code
        return go
    return {"demo": demo, "greenfield": with_verify("greenfield", run_greenfield), "brownfield": with_verify("brownfield", run_brownfield),
            "enhancement": with_verify("enhancement", run_enhancement), "refactor": with_verify("refactor", run_refactor),
            "tests": with_verify("tests", run_tests), "ambiguous": lambda a: report(run_ambiguous(a)), "custom": lambda a: report(run_custom(a)),
            "agents": list_agents}[args.scenario](args)


if __name__ == "__main__":
    sys.exit(main())
