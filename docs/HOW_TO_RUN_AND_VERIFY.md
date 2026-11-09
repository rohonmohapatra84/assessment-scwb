# How to run every scenario and check that it really works

This page answers one question for each scenario: **what do I type, what do I look at, and how do I know it worked?**
Commands are for Windows PowerShell from the project folder (`assessment-schwb`). Python 3.9+, nothing to install.

## 0. One-time setup (2 minutes)

1. Copy `.env.example` to `.env` and fill in one model provider (see the comments in the file). Azure AI Foundry needs
   `AZURE_AI_ENDPOINT`, `AZURE_AI_DEPLOYMENT` and `AZURE_AI_API_KEY`. No Foundry? See [MODEL_SETUP.md](MODEL_SETUP.md).
2. Prove the connection **before** anything else:

```powershell
python scripts/model_smoke.py
```

It must end with a success message. If it does not, nothing else will work: fix `.env` first. (Every scenario below calls the model,
so each run costs a few minutes and some tokens. The framework tests in section 7 call no model and cost nothing.)

## 1. The map: every scenario in one table

| # | Scenario | Command | Time (about) | Output folder | "Working" means |
|---|---|---|---|---|---|
| 1 | **Greenfield**: the mandatory URL shortener | `python run.py greenfield` | 9 min | `output/greenfield/` | status `completed`; a running service; its tests pass |
| 2a | **Brownfield, bug fix: START HERE for brownfield** (a bug is planted for you) | `python run.py brownfield --verify` | 3 to 5 min | `output/brownfield/` | red before, green after; the new tests **fail on the old code** |
| 2b | *Advanced:* brownfield **enhancement** (link expiry, changes the API). **Can fail**, see section 5 | `python run.py enhancement` | 5 to 10 min | `output/enhancement/` | contract grew, an obsolete test may be amended **with your approval** |
| 2c | *Advanced:* brownfield **refactor** | `python run.py refactor --verify` | 3 min | `output/refactor/` | API identical, suite not smaller |
| 2d | *Advanced:* brownfield **tests only** | `python run.py tests --verify` | 3 min | `output/tests_improvement/` | no production file touched |
| 3 | **Ambiguous requirement** | `python run.py ambiguous`, then again with answers | 2 min + 2 min | `output/ambiguous/` | pauses with questions; after answers, a proposal |
| 4 | **Control and failure demos** | section 6 | 2 to 9 min | your `--out` folder | human veto, retries, repair, resume |

Scenarios 2 to 3 use the service from scenario 1 as the "existing code", so run **1 first**. If `output/greenfield/release` is missing,
2b and 3 generate it for you.

**Check everything at once, any time:**

```powershell
python scripts/verify_outputs.py             # re-reads each output folder AND re-runs each release's own tests
python scripts/verify_outputs.py greenfield  # one scenario
python scripts/verify_outputs.py --skip-tests   # records only (fast)
```

For every scenario it prints `[PASS]` or `[FAIL]` lines and ends with `ALL SCENARIOS THAT WERE RUN ARE WORKING` (or lists what failed).
A scenario you have not run says `NOT RUN` and does not count as a failure. It does not trust the harness's own report: it re-executes the tests.

## 2. How to read any output folder

Every run writes to `output/<scenario>/`. The same files appear each time:

| File or folder | What it is | Look here when |
|---|---|---|
| `RUN_STORY.md` | **start here: the run told in plain English, in order.** It is written live while the run executes. For a bug fix it opens with the bug that was planted (file, line, before and after), then shows each stage, which AGENT (model) or step acted, the exact change the code agent made, and what each validation check proves | you want to understand what happened and why |
| `DEMO_RESULT.md` | **the visible payoff**: after the release the service is really started, real long URLs are shortened, redirects followed and analytics read. For a bug fix it shows the bug BEFORE and AFTER side by side | you want to SEE it work |
| `release/` | **the product**: the validated files the agents produced (code, `openapi.json`, `schema.sql`, tests, docs; `changes.diff` for a change). Only exists after the release gate | you want the actual result |
| `ENGINEERING_SUMMARY.md` | the formal engineering report the assignment asks for: understanding, plan, validation, risks, approvals, limits | you want the formal report |
| `input/` | a copy of exactly what this run was given | for reference |
| `summary.json` | machine-readable result: `status`, task states, gate decisions | you want a yes/no (`status`) |
| `plan.json` | the task plan the lead agent proposed and the harness accepted | you want to see the decomposition |
| `events.jsonl` | the audit log, one JSON event per line | something failed and you need to see why |
| `state.json` | the checkpoint (blackboard, tasks, files); `--resume` reads it | you want the raw validation report |
| `release/` | the validated files. **Exists only if the human release gate approved** | you want the actual result |
| `workspace/`, `baseline/` | the sandbox the agents wrote into, and a pristine copy of the existing repo | debugging |
| `clarifications.json` | (ambiguous only) the questions the system needs answered | the run paused |

**Status values** (`summary.json`, and the last line the CLI prints): `completed` (done and released), `completed_degraded` (released, but an
optional task such as documentation failed), `needs_clarification` (paused for your answers; not a failure), `rejected_at_plan` /
`rejected_at_release` (a human said no; nothing released), `plan_rejected` (the lead could not produce a valid plan in three tries),
`failed` (a hard failure; nothing released: open `events.jsonl` and look for `task_failed` and `validation_check ... FAIL`).

## 3. Scenario 1: Greenfield, the URL shortener

**What it shows:** requirement understanding, decomposition into 10 tasks, a model writing a whole service and a separate model call writing
its tests, a validator judging it, two human gates.

```powershell
python run.py greenfield
```

You will see the plan, then task start and finish lines, then the validation checks. With the default `--approve auto` both human gates are
approved automatically and labelled `policy:auto`. To decide yourself: `python run.py greenfield --approve interactive` (type `y` or `n` at each gate).

**See a URL get shortened:** open `output/greenfield/DEMO_RESULT.md`. At the end of the run the harness started the service the agents built, shortened two real URLs, followed the redirects and read the analytics, then stopped it. If your run finished before this feature existed, create it with no model call:

```powershell
python scripts/demo_service.py output/greenfield
```

**Check it worked, in four steps:**

1. **The verdict.** `python scripts/verify_outputs.py greenfield` should print `greenfield OK` with these lines passing: status completed,
   validator checks none failed, gates approved, `release/` exists, `openapi.json` and `schema.sql` released, the behavioural probe passed, and
   `RE-RAN the release's own tests just now: Ran 73 tests ... OK` (the count varies by run: the model writes the tests).
2. **The story.** Open `output/greenfield/ENGINEERING_SUMMARY.md`. Section 3 is the plan table and graph (note tasks that run in parallel), section 4
   shows any retry or repair (for example `task_self_correct`, `repair_loop`), section 6 is every validation check with its evidence, section 7 the risks.
3. **The tests, yourself:**

```powershell
cd output/greenfield/release
python -m unittest discover -s tests -t .       # expect: Ran 73 tests ... OK (varies by run)
```

4. **The service, yourself** (this is the part that proves it is a real service, not just files):

```powershell
python -m shortener.server --port 8080          # leave this window open; open a second PowerShell for the next lines
$r = Invoke-RestMethod -Method Post -Uri http://localhost:8080/api/v1/links -ContentType 'application/json' -Body '{"url":"https://example.com/some/long/path"}'
$r                                               # shows code and short_url
curl.exe -si "http://localhost:8080/$($r.code)"  # expect: 302 and a Location: https://example.com/some/long/path
Invoke-RestMethod "http://localhost:8080/api/v1/links/$($r.code)/stats?days=7"   # clicks and unique visitors
Invoke-RestMethod http://localhost:8080/healthz
```

Stop the server with Ctrl+C **before** re-running scenarios: a running server locks `output/greenfield/release` and the next run cannot replace it.
Optional: `python scripts/load_smoke.py --requests 2000` (inside `release/`) prints requests per second and latency for one process.

**It is NOT working if:** the status is `failed`; `release/` is missing; a `[FAIL]` line appears; or the service does not start. Open `events.jsonl`
and search for `task_failed` and `FAIL`.

## 4. Scenario 2a: your first brownfield test, the bug fix (one command, one verify, one payoff)

**What "brownfield" means for the URL shortener.** Greenfield built the shortener. Brownfield is the next day: the service exists and someone asks for a
change. **A person reports the problem; the agents do not discover it.** For this demo the system plants one bug in a copy of your shortener (so you have a real
defect), then a plain-English report is the request: *"Fix the bug where link stats report unique_visitors equal to the total number of clicks."* The agents' job
is to find **where** in the existing code that happens, change it minimally, and **prove** the fix. See [usecases/02_BROWNFIELD.md](usecases/02_BROWNFIELD.md).

**One command:**

```powershell
python run.py brownfield --verify
```

(Add `--approve interactive` if you want to be the human at the two gates and type `y`; the default approves automatically and labels it `policy:auto`.)
If `output/greenfield/release` does not exist yet the command says so and builds the shortener first (about 9 minutes, once).

**What you will see:** a short "what this does" note; the planted bug; the existing tests run **red** (that is the bug); the plan; the agents working; the validation
checks; and at the end the `--verify` block. **One payoff line decides it:** `>> VERIFIED: this run really works.` (or `>> NOT VERIFIED` with the FAIL lines).
The verify re-reads the files on disk and re-runs the released service's own tests, so it does not trust the run's own report.

**To watch the agents work, open `output/brownfield/RUN_STORY.md`** (add `--out` to use another folder). It is written live, so you can open it while the run is going and
refresh it. It tells the story in order: *Stage 0* the bug that was planted and exactly where (file, line, before and after); *Stage 1* the existing tests running red; *Stage 2*
the lead agent's plan; *Stage 3* the `code_author` agent starting, the **exact diff it produced**, then the `spec_tester` agent and the tests it wrote; *Stage 4* every validation check
in plain English, ending with the proof that the new tests fail on the buggy code; *Stage 5* the human gates and the release.

**Optional, to see it with your own eyes** (about 2 minutes):

1. `cd output\brownfield\input\existing_code_with_bug; python -m unittest discover -s tests -t .` shows **failures**: that is the planted bug. Then
   `cd ..\..\release; python -m unittest discover -s tests -t .` is **green**: that is the fix.
2. Open `output/brownfield/release/changes.diff`: a small change (the `COUNT(DISTINCT visitor_hash)` query), not a rewrite.
3. Open `output/brownfield/ENGINEERING_SUMMARY.md`: section 2 (codebase reasoning), and the line starting **Bug-fix proof**.

Your original `output/greenfield/release` is never modified. To keep results separate from earlier runs, add `--out output_retest\brownfield`; the planted-bug copy then lands
inside it as `output_retest\brownfield\input\existing_code_with_bug`, and you re-check with `python scripts/verify_outputs.py --output-dir output_retest brownfield`.

> If `verify_outputs` says "not recorded: this output predates the check", the folder was made before the old-code check existed. Re-run `python run.py brownfield --verify`.

## 5. Advanced: the other kinds of change (enhancement, refactor, tests-only)

**Do these only after the bug fix passes.** They are harder to read and one of them can fail legitimately.

> **The enhancement can fail, and that is a legitimate result.** On live runs it passed once and failed once. A failed run is not a broken system: it means the bounded repair
> rounds ran out before the model's code and tests agreed, and **nothing was released**. Open `events.jsonl`, find the last `validation_check ... FAIL`, and you will see why
> (for example, new tests asserting wording the contract never specified). Re-running often passes. Treat a pass as "it can work", not "it always works".

The **kind** of change is detected from your wording and decides which tools the lead may use and what the validator demands.

### 2b. Enhancement that changes the API: link expiry

```powershell
python run.py enhancement                       # auto-approves the gates
python run.py enhancement --approve interactive # you review the test-amendment diff (recommended for a demo)
```

The request: "Add support for expiring short links: callers can set an optional TTL when creating a link, and once expired the link must stop
redirecting (return 410 Gone). Existing links must keep working." It adds a request field and a response code, so it is the case where an existing test
can become obsolete (the baseline contains a test that pins the whole OpenAPI document).

**What you will see, in order:** the plan gate; `code_author` proposes anchored edits and publishes the changed `openapi.json`; `spec_tester` writes
new tests from that contract; the validator may fail a *baseline* test; then, if the contract really changed, `spec_tester` is shown that test and may
propose an amendment; the validator checks it; then the **`test_update_approval` gate** shows you the diff of every amended test and its reason; then the release gate.

**Check it worked:**

1. `python scripts/verify_outputs.py enhancement`: the released `openapi.json` documents expiry, the API change is additive only, no existing test changed
   outside the policy and every amended test was approved at `test_update_approval`.
2. In `ENGINEERING_SUMMARY.md` look for the section **Existing tests amended**: file, reason, who proposed it. Also `release/changes.diff`.
3. Try the guard yourself: `python run.py enhancement --approve reject:test_update_approval` must end `rejected_at_release` and leave **no** `release/` folder.
4. Expiry works: start the released service (as in section 3, from `output/enhancement/release`), create a link with `{"url":"https://example.com","expires_in_seconds":60}`,
   and confirm the response carries an expiry time.

### 2c. Refactor (behaviour must not change)

```powershell
python run.py refactor --verify          # input: input/refactor/requirement.txt
```

Working = `compat.openapi_unchanged` PASS (the API is **identical**, not merely compatible) and the suite is not smaller. A refactor may not touch any existing test.

### 2d. Tests-only improvement

```powershell
python run.py tests --verify             # input: input/tests_improvement/requirement.txt
```

Working = `scope.tests_docs_only` PASS: the validator fails the run if **any production file** was written. The code-writing agent is not even offered to the lead in this kind of change; open `plan.json` and confirm there is no `code_author` task.

## 6. Scenario 3: the ambiguous requirement (two steps)

**What it shows:** the system refuses to guess. "Make the link service faster and more secure" has two vague terms and no number, so it must ask.

```powershell
python run.py ambiguous
```

Expected: status `needs_clarification` and **no code generated**. Open `output/ambiguous/clarifications.json`: each question has a default. This is a
pause, not a failure (exit code 0).

Then answer the questions, either with a file of answers or by accepting documented defaults:

```powershell
python run.py ambiguous --answers input/ambiguous/answers.json         # a human answers
python run.py ambiguous --assume-defaults                              # or accept defaults (recorded as "needs confirmation")
python scripts/verify_outputs.py ambiguous
```

Working = status `completed`, and `output/ambiguous/release/docs/PROPOSAL.md` exists with measurable objectives, options with trade-offs, a gap analysis
against the existing code ("is this already in the codebase?") and a validation plan. The answers (or defaults) are recorded in the summary.

## 7. Control and failure demos (the "controlled autonomy" evidence)

| What you want to see | Command | What to look for |
|---|---|---|
| **Human veto** at the release gate | `python run.py greenfield --approve reject:release_approval --out output/veto_demo` | work done, status `rejected_at_release`, **no `release/`** |
| **Human veto** at the plan gate | `python run.py greenfield --approve reject:plan_approval --out output/veto_demo2` | nothing generated, status `rejected_at_plan` |
| **You** decide each gate | `python run.py greenfield --approve interactive` | the prompts, then the audit table in the summary |
| **Retry with backoff** | `python run.py greenfield --inject transient:spec_tester:2 --out output/retry_demo` | `task_retry` events in `events.jsonl` |
| **Hard failure** | `python run.py greenfield --inject fatal:code_author --out output/fatal_demo` | dependents `skipped`, status `failed`, no `release/` |
| **Validator catches a defect, repair loop heals it** | `python run.py greenfield --generator template --inject defect:code_generator --out output/greenfield_chaos` | `repair_loop` event, validation history failed then zero |
| **Resume** a stopped run | `python run.py greenfield --resume --out output/fatal_demo` | only the failed tasks run again |

`--inject` names a task id **or a tool name** (the lead chooses task ids, so use tool names such as `code_author`, `spec_tester`). The `defect:` form plants a flaw in
the offline reference generator, so it needs `--generator template`; `transient:` and `fatal:` work with the model. All of these still need the model for the lead agent.

## 8. Testing the framework itself (no model, no cost)

```powershell
python -m unittest discover -s tests -t .       # about 5 minutes; expect: Ran 266 tests ... OK
python -m unittest tests.test_test_policy -v    # just the test-update and bug-fix-proof policies (about 90 seconds)
python run.py agents                            # lists the agents and steps; ends "specs and registered agents are consistent"
```

These use a scripted stand-in for the model, so they prove how the **system** treats good and bad model output (bad plans, garbage decisions, flawed code,
a weakening test edit, a regression test that passes on the buggy code). They do not prove a real model writes good code: that is what the live runs above are for.

## 9. A five-minute demo script

1. `python scripts/model_smoke.py`, then `python run.py demo` while you explain the three use cases.
2. Open `output/greenfield/ENGINEERING_SUMMARY.md`: the plan table and graph (section 3) and the validation evidence (section 6).
3. `python run.py greenfield --generator template --inject defect:code_generator --inject transient:test_generator:2 --out output/chaos_demo`: the retry with backoff, the validator failure, the repair round, then green.
   (`defect:` plants a flaw in the offline reference generator, so it needs `--generator template`; `transient:` and `fatal:` also work with the model.)
4. Open `output/brownfield/ENGINEERING_SUMMARY.md`: the red baseline, the "Bug-fix proof" line, and `release/changes.diff`.
5. `python run.py enhancement --approve interactive`: when the amendment gate appears, read the diff and say yes or no.
6. Show `output/ambiguous/clarifications.json`, then `release/docs/PROPOSAL.md`.
7. `python run.py greenfield --approve reject:release_approval`: the work exists, nothing is released.
8. `python scripts/verify_outputs.py`: every scenario re-checked from the files on disk.
9. Close with the limits section of [ARCHITECTURE.md](ARCHITECTURE.md): what is real, what is not, and the roadmap.

## 10. If something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| `model_smoke.py` fails | `.env` is wrong or the deployment name is wrong; the message says which |
| `could not remove ... output/greenfield/release` | the generated service is still running, or OneDrive is syncing; stop the server (Ctrl+C) and retry |
| `plan_rejected` | the model could not produce a valid plan in 3 tries; just re-run, and read `events.jsonl` for `plan_rejected` reasons |
| status `failed` | open `events.jsonl`; search `task_failed` and `validation_check`; the last `FAIL` line names the check and the evidence |
| `verify_outputs` says `NOT RUN` | you have not run that scenario yet; run its command |
| `verify_outputs` says "predates the check" | re-run that scenario |
| A run seems stuck at a gate | you used `--approve interactive`: type `y` or `n` |
| Runs differ from the committed output | expected: models write the plan, code and tests, so runs are not reproducible end to end |
