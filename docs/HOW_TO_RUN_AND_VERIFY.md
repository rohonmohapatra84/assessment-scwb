# Run and verify: the reference

The everyday steps live in the **[root README](../README.md)**: connecting a model (section 5, Prerequisites), shortening your own URL through the agents (greenfield), planting a bug and watching the agents fix it (brownfield), the ambiguous requirement, and how to read an output folder (section 6).
This page is the reference for everything else: checking results, the advanced scenarios, the failure and control demos, the framework tests, a demo script and troubleshooting. Commands are for Windows PowerShell from the project folder.

## 1. Checking results from the files on disk

```powershell
python scripts/verify_outputs.py                 # every scenario: re-reads each output folder AND re-runs each release's own tests
python scripts/verify_outputs.py greenfield      # one scenario (also: brownfield, enhancement, ambiguous, refactor, tests)
python scripts/verify_outputs.py --skip-tests    # records only (fast)
```

It prints `[PASS]` or `[FAIL]` lines per scenario and ends with `ALL SCENARIOS THAT WERE RUN ARE WORKING` (or lists what failed). A scenario you have not run says `NOT RUN`, which is not a failure.
It does not trust the harness's own report: it re-executes the tests. For a run you wrote to your own `--out` folder, rely on the `--verify` block that `run.py` prints at the end.

**Status values** (`summary.json`, and the last line the CLI prints):

| Status | Meaning |
|---|---|
| `completed` | done and released |
| `completed_degraded` | released, but an optional task such as documentation failed |
| `needs_clarification` | paused for your answers (ambiguous requirement); not a failure |
| `rejected_at_plan`, `rejected_at_release` | a human said no; nothing released |
| `plan_rejected` | the lead could not produce a valid plan in three tries |
| `failed` | a hard failure; nothing released. Open `events.jsonl` and search `task_failed` and `validation_check ... FAIL` |

## 2. The advanced brownfield scenarios

Do these after the bug fix passes (README, Use case 2). They are harder to read and **the enhancement can fail legitimately**: on live runs it passed once and failed once. A failure means the bounded repair rounds ran out before the code and tests agreed,
and **nothing was released**. Open `events.jsonl`, find the last `validation_check ... FAIL` (for example, new tests asserting wording the contract never specified), and re-run: it often passes. Treat a pass as "it can work", not "it always works".
The kind of change is detected from your wording and decides which tools the lead may use and what the validator demands. All of these use `output/greenfield/release` as the existing code (built for you if missing).

| Scenario | Command (input) | "Working" means |
|---|---|---|
| **Enhancement** that changes the API (link expiry) | `python run.py enhancement --approve interactive` (`input/enhancement/requirement.txt`) | the released `openapi.json` documents expiry; the API change is additive; any amended existing test was approved at `test_update_approval` |
| **Refactor** (behaviour must not change) | `python run.py refactor --verify` (`input/refactor/requirement.txt`) | `compat.openapi_unchanged` PASS (the API is **identical**); the suite is not smaller; no existing test touched |
| **Tests only** | `python run.py tests --verify` (`input/tests_improvement/requirement.txt`) | `scope.tests_docs_only` PASS: no production file written; `plan.json` has no `code_author` task |

For the enhancement, with `--approve interactive` you review the diff of every amended test at the `test_update_approval` gate. To check:

1. `python scripts/verify_outputs.py enhancement`.
2. `ENGINEERING_SUMMARY.md`, section **Existing tests amended** (file, reason, who proposed it), and `release/changes.diff`.
3. The guard: `python run.py enhancement --approve reject:test_update_approval` must end `rejected_at_release` and leave **no** `release/` folder.
4. Expiry works: start the released service (README, greenfield), `POST /api/v1/links` with `{"url":"https://example.com","expires_in_seconds":60}`, and confirm the response carries an expiry time.

## 3. Control and failure demos (the "controlled autonomy" evidence)

| What you want to see | Command | What to look for |
|---|---|---|
| **Human veto** at the release gate | `python run.py greenfield --approve reject:release_approval --out output/veto_demo` | work done, status `rejected_at_release`, **no `release/`** |
| **Human veto** at the plan gate | `python run.py greenfield --approve reject:plan_approval --out output/veto_demo2` | nothing generated, status `rejected_at_plan` |
| **You** decide each gate | `python run.py greenfield --approve interactive` | the prompts, then the audit table in the summary |
| **Retry with backoff** | `python run.py greenfield --inject transient:spec_tester:2 --out output/retry_demo` | `task_retry` events in `events.jsonl` |
| **Hard failure** | `python run.py greenfield --inject fatal:code_author --out output/fatal_demo` | dependents `skipped`, status `failed`, no `release/` |
| **Validator catches a defect, repair loop heals it** | `python run.py greenfield --generator template --inject defect:code_generator --out output/chaos_demo` | `repair_loop` event; validation failed, then zero failures |
| **Resume** a stopped run | `python run.py greenfield --resume --out output/fatal_demo` | only the failed tasks run again |

`--inject` names a task id **or a tool name** (the lead chooses task ids, so use tool names such as `code_author`, `spec_tester`). The `defect:` form plants a flaw in the offline reference generator, so it needs `--generator template`; `transient:` and `fatal:` work with the model.
All of these still need the model for the lead agent.

## 4. Testing the framework itself (no model, no cost)

```powershell
python -m unittest discover -s tests -t .       # about 5 minutes; expect: Ran 273 tests ... OK
python -m unittest tests.test_test_policy -v    # just the test-update and bug-fix-proof policies (about 90 seconds)
python run.py agents                            # lists the agents and steps; ends "specs and registered agents are consistent"
```

These use a scripted stand-in for the model, so they prove how the **system** treats good and bad model output (bad plans, garbage decisions, flawed code, a weakening test edit, a regression test that passes on the buggy code). They do not prove a real model writes good code: that is what the live runs are for.
What each test file covers is in [TESTING.md](TESTING.md).

## 5. A five-minute demo script

1. `python scripts/model_smoke.py`, then show the plan and gates of a greenfield run: `python run.py greenfield --shorten https://www.python.org/downloads/ --approve interactive` (about 9 minutes: start it first, or use the committed `output/greenfield`).
2. Open `output/greenfield/ENGINEERING_SUMMARY.md`: the plan table and graph (section 3) and the validation evidence (section 6). Open the short URL in a browser.
3. Brownfield: show a planted bug (`input/brownfield/bugs/`), the red tests, then `output/brownfield/ENGINEERING_SUMMARY.md`: the red baseline, the "Bug-fix proof" line and `release/changes.diff`.
4. `python run.py greenfield --generator template --inject defect:code_generator --inject transient:test_generator:2 --out output/chaos_demo`: the retry with backoff, the validator failure, the repair round, then green.
5. `python run.py enhancement --approve interactive`: when the amendment gate appears, read the diff and say yes or no.
6. Show `output/ambiguous/clarifications.json`, then `release/docs/PROPOSAL.md`.
7. `python run.py greenfield --approve reject:release_approval`: the work exists, nothing is released.
8. `python scripts/verify_outputs.py`: every scenario re-checked from the files on disk.
9. Close with the limits section of [ARCHITECTURE.md](ARCHITECTURE.md): what is real, what is not, and the roadmap.

## 6. If something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| `model_smoke.py` fails | `.env` is wrong or the deployment name is wrong; the message says which. See [MODEL_SETUP.md](MODEL_SETUP.md), section 5 |
| `could not remove ... output/greenfield/release` | the generated service is still running, or OneDrive is syncing; stop the server (Ctrl+C) and retry |
| `plan_rejected` | the model could not produce a valid plan in 3 tries; re-run, and read `events.jsonl` for the `plan_rejected` reasons |
| status `failed` | open `events.jsonl`; search `task_failed` and `validation_check`; the last `FAIL` line names the check and the evidence. Models are not deterministic, so a re-run often passes |
| a short URL says "failed to load" | the service that was serving it has stopped (you pressed Ctrl+C, used `--once`, or closed the terminal) |
| `verify_outputs` says `NOT RUN` | you have not run that scenario yet; run its command |
| `verify_outputs` says "predates the check" | the folder was made before that check existed; re-run the scenario |
| A run seems stuck at a gate | you used `--approve interactive`: type `y` or `n` |
| Runs differ from the committed output | expected: models write the plan, code and tests, so runs are not reproducible end to end |
