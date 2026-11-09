# Testing approach

Three different things are tested, and they answer three different questions. Quality is never judged by asking a model: every verdict is produced by executing something and is recorded with evidence.

| Level | Question | How it is answered |
|---|---|---|
| **A. The product** (the generated URL shortener, or the changed service) | does the software do what the requirement says? | the test suite the `spec_tester` agent wrote (73 tests in the committed greenfield run), **executed by the validator**; plus 14 black-box probes |
| **B. The generation** | is this output consistent, safe and complete? | the validator's checks: static, probe, tests, regression, compat, test integrity, traceability (per use case: [usecases/](usecases/)) |
| **C. The system** (the harness and agents) | do planning checks, recovery, guardrails and gates behave as claimed? | `tests/`: 273 tests, offline, a scripted stand-in plays the model |

How to run each, and how to re-check saved results: [HOW_TO_RUN_AND_VERIFY.md](HOW_TO_RUN_AND_VERIFY.md).

## A and B: what runs inside every scenario

The validator runs these families of checks: `static`, `probe`, `tests`, `regression`, `compat`, `tests.existing_tests_protected`, `scope` and `traceability` (and `plan` for the ambiguous case). What each one executes, what it blames, and which kinds of change require which checks is in
[ARCHITECTURE.md](ARCHITECTURE.md), section 5. The use-case pages ([usecases/](usecases/)) list the checks and test cases specific to each scenario. Level A in numbers: the black-box probe drives the public interface through 14 documented flows, then compares the database the code created with the designed schema.
The model-written service suite (committed run, 73 tests): `test_links.py` (19: creation, aliases, redirects, deletion, stats), `test_validation.py` (21: hostile URLs, CRLF, bad schemes, alias rules),
`test_analytics_persistence.py` (16: batching, drop-not-block, unique visitors, persistence), `test_auth_rate_limit.py` (12: API keys, rate limiting), `test_contract.py` (5: router equals the OpenAPI document), plus a provided `wsgi_client.py` helper. File names and counts vary per run. They are deterministic: no sleeps, no network, no ports.

## Test-integrity policies: `tests/test_test_policy.py` (26 tests)

The model writes both the code and the tests, so the harness must stop either from weakening the other. The policies themselves are explained in [ARCHITECTURE.md](ARCHITECTURE.md), section 6; this table maps each rule to the test that proves it.

| Policy | Proven by |
|---|---|
| Only an existing baseline `tests/test_*.py` may be replaced, never a new path, a `test_change_*` file or anything outside `tests/` | `WorkspaceRules` |
| Tampering is detected from the **disk**, even when an agent writes around the API (deleting a protected test counts) | `test_modification_is_detected_from_disk_even_when_written_around_the_api` |
| An amendment may not remove a test, lose an assertion, skip a test or break the syntax | `AmendmentMustNotWeaken` |
| Only `spec_tester`, only in an enhancement, may amend; refactor, bug fix, tests/docs and every other agent may not | `WhoMayAmend` |
| The validator accepts an amendment only for an enhancement with a real contract change, recorded with a reason, no weaker than the original | `ValidatorEnforcesThePolicy` |
| Bad proposals (no reason, missing or ambiguous `find`, unknown file, more than five edits, weakening) are rejected with feedback | `SpecTesterProposals` |
| A bug fix's regression test must **fail on the old code**: passes-on-old and cannot-load-on-old are both refused | `RegressionTestMustFailOnOldCode` |
| **End to end** (real sandbox, validator, gates): an obsolete pinned test is amended after a failure, a human approves, the run completes; rejecting the gate blocks release; with no amendment the run cannot pass; a weakening amendment is refused | `AmendmentEndToEnd` |
| **End to end** on a really seeded bug: tests that fail on the old code pass; tests that pass on the old code cannot complete | `BugfixEndToEnd` |

*Honest limit:* the amendment check counts tests, assertions and skips; it does not understand what an assertion compares. A live amendment kept the count but made a contract comparison ignore `description` and `summary`. Only the human gate catches that.

## C: the framework tests (`tests/`, 273 tests, about 5 minutes)

```powershell
python -m unittest discover -s tests -t .
```

| File | Tests | Covers |
|---|---|---|
| `test_harness.py` | 72 | the tool catalog (every runnable step has a spec and vice versa; template tools hidden in model mode); **plan check** (unknown tool, tool used twice, missing `needs`, cycle, write conflict, validator must wait for everything, model cannot widen a write scope); the **loop with a lead** (the lead picks the order, no model call without a real choice, bad decisions refused then a logged fallback, abort, turn budget); the lead's prompt; end to end with a scripted lead (bad plan corrected, three bad plans stop the run, defect repaired by role, a human rejecting the plan stops everything); change-kind routing, catalog and validator rules; prompts tell the truth (only helpers that exist, the published contract is shown, the test author is told not to pin wording); one run per kind |
| `test_loop.py` | 20 | the loop with fake agents: DAG validation; dependency order and **real parallelism**; blackboard hand-off; transient retry with backoff; **dependents skipped, independent task still runs**; optional failure degrades; agent crash contained; **repair loop routes feedback to the responsible producer** and is bounded; self-correction; fault injection; **resume re-runs only unsuccessful tasks** |
| `test_guardrails.py` | 15 | path traversal; extension allow-list; **write-scope enforcement**; secret scan; size cap; baseline import leaves the source untouched; promote; **test-runner timeout kills a hanging suite**; **a child process cannot see a parent secret**; approval policies (auto refuses blocking issues, reject, `reject:<gate>`, interactive is audited) |
| `test_agents.py` | 21 | requirement analyst (assumption vs blocking ambiguity; numeric target removes ambiguity; answers recorded as human; **prompt injection flagged and not obeyed**; model output sanitised); plan shapes; OpenAPI diff classification; codebase analyst (routes, tables, **receiver-aware flows**, impact tiers); **model output with a NUL byte or secret-like text is rejected as feedback, not a crash** |
| `test_agent_specs.py` | 20 | the Markdown agent files: parsing, the fields each must have, they match the registered workers, no file names a vendor |
| `test_llm.py` | 21 | the Azure provider wire format and error taxonomy; the **system's handling of model output** with a scripted model: probe passes, missing interface rejected and self-corrected, a **behavioural bug is caught and repaired with the failing probe quoted back**, throttling retried, `../escape.py` blocked, an embedded secret **sent back as feedback and fixed on retry** (and never written if it keeps leaking), the test author never sees the code, bad anchors rejected with feedback |
| `test_providers.py` | 26 | provider selection, the OpenAI-compatible, Anthropic and command providers' request and response shapes (mocked HTTP and local scripts) |
| `test_api_service.py` | 11 | the HTTP service: **a full run with remote human approvals** (nothing runs before the plan gate, nothing is released before the release gate, decisions carry the reviewer's identity); rejection blocks release; an **unattended gate times out as a rejection**; bearer auth; remote callers cannot name filesystem paths |
| `test_e2e.py` | 16 | whole runs with real generation and validation (below) |
| `test_test_policy.py` | 26 | the test-integrity policies (above) |
| `test_demo.py` | 11 | the live demo (`DEMO_RESULT.md`): on a real service a long URL becomes a short URL that redirects back (302 to the exact long URL), one visitor clicking three times is `total_clicks=3, unique_visitors=1`, the planted bug shows `unique_visitors=3` before and `1` after, the release folder is never written to, a missing or crashing service is reported (with the crash reason) and never raised, and the demo is off by default and absent when nothing was released |
| `test_narrator.py` | 14 | the plain-English run story (`RUN_STORY.md`): the scenario setup, stage order, models told apart from steps, the real diff quoted, each validation check explained, repairs and amended tests described, and a listener that raises can never break a run; two real scripted runs (bug fix, enhancement with an amended test) and a human veto |

`tests/fake_llm.py` is the scripted stand-in for the model; `tests/reference_plans.py` holds hand-written plans the stand-in lead returns. They prove how the **system** treats good and bad model output; they do not prove a real model writes good code. The live runs do.

### End-to-end behaviours proven (`test_e2e.py`)

| Behaviour | Test |
|---|---|
| Happy path: validated release, 70+ tests executed, 2+ concurrent tasks | `test_happy_path_produces_validated_release` |
| **Defect, validator catches it, repair through feedback, green; gates still apply** | `test_injected_defect_is_caught_by_validator_and_repaired_via_feedback` |
| Transient failures retried with backoff | `test_transient_failures_are_retried_with_backoff` |
| Optional task dies: `completed_degraded` | `test_optional_task_failure_degrades_but_does_not_block` |
| Fatal: dependents skipped, **nothing released**, an independent branch still completed | `test_fatal_failure_skips_dependents_and_releases_nothing` |
| Resume from the checkpoint; completed work is not re-run | `test_resume_from_checkpoint_skips_completed_work` |
| Human veto at each gate | `test_human_can_veto_at_either_gate` |
| Injection text flagged, classification unaffected, nothing written outside the sandbox | `test_prompt_injection_text_is_flagged_not_obeyed` |
| **The released service really starts as a subprocess and serves requests** | `test_generated_service_really_runs` |
| Brownfield: validated, additive, **the original repository's hash is unchanged**; incomplete patch repaired from the pristine baseline; **anchor drift fails safe** and is not retried | `BrownfieldTests` |
| Ambiguous: pause, answers, resume; assume-defaults flagged; **no code generated** | `AmbiguousTests` |

## Bugs the tests and probes found in this work (worth quoting)

| Found by | Bug | Fix |
|---|---|---|
| the black-box probe (intermittent) | `ClickRecorder.flush()` was not read-your-writes: while the background thread was mid-write, a manual flush returned early and `/stats` read zero | a flush lock and a regression test that holds a slow write in flight |
| the same probe, via a flaky repair test | **stale bytecode**: a regenerated file with the same size in the same second reused the old `.pyc`, so a *correct* repair still ran the *old* code (`-I` ignores `PYTHONDONTWRITEBYTECODE`) | `-B` on every validator subprocess |
| live enhancement runs | seven failures, each a real harness defect: a prompt naming a helper that did not exist, tests written before the contract, wrong blame, an analysis that saw 1 of 15 files, the interface withheld from the test author, a test pinning the whole contract | each fixed; the last led to the approved-test-update policy |
| a live enhancement run | the new tests asserted an exact error message and description the contract never fixed, and every repair moved the wording | the test author may assert only status, fields, types, bounds and the machine-readable error `code` |
| review of the bug-fix flow | a regression test was never proven to fail on the bug | `regression.fails_on_old_code` |
| cloud API test on Windows | the status endpoint reading `state.json` blocked the checkpoint's atomic rename | a retrying replace |

## Known limits of this testing

- With the scripted model, tests prove how the system treats good and bad model output, **not** how good a real model's code is. Measuring that needs live runs and an eval harness (golden requirements, first-pass validation rate, repair rounds); the validator already records those numbers per run.
- Live results are a handful of runs on one provider. The enhancement failed once and passed once.
- The validator proves consistency and tested behaviour, not business correctness.
- No mutation testing and no property-based tests (a natural next step for URL validation and the code generator).
- The load smoke test (`scripts/load_smoke.py` inside the release) is single-process and indicative only.
