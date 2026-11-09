# Run story
This file tells, in plain English and in order, what happened in this run. The complete machine-readable record is `events.jsonl`; the final deliverable is `ENGINEERING_SUMMARY.md`.
- **Request:** Build a scalable URL shortener service with APIs, persistence, and analytics.
- **Existing code given:** none (this builds something new)
- **Model provider:** azure   **Human approval mode:** interactive
**Who acts in this story.** An **AGENT (model)** is a model with a role: `project_lead`, `requirement_analyst`, `code_author`, `spec_tester`. A **step (plain Python)** is deterministic code that makes no model call: design, analysis, validation. The **harness** is the plain-Python referee that checks every plan, sandboxes every file and stops at the human gates.

**What is in this folder.** You only need the first few; the rest are the machinery's own records.

| File or folder | What it is | Do you need it? |
|---|---|---|
| `RUN_STORY.md` | this file: the run in plain English | **read this first** |
| `DEMO_RESULT.md` | the released service actually used: real long URLs shortened, redirects followed, analytics read (written at the end) | **the visible payoff** |
| `release/` | **the product**: the validated files (code, `openapi.json`, `schema.sql`, tests, docs; `changes.diff` for a change). Exists only after the human release gate | **yes: this is the deliverable** |
| `ENGINEERING_SUMMARY.md` | the formal engineering report the assignment asks for: plan, rationale, artifacts, risks, validation, assumptions | the formal deliverable |
| `input/` | a copy of exactly what this run was given, so the output explains itself | for reference |
| `plan.json` | the task plan the lead agent proposed and the harness accepted | optional |
| `events.jsonl` | the complete audit log, one JSON event per line | when debugging or auditing |
| `summary.json` | status and gate decisions, for programs | machine use |
| `state.json` | the checkpoint that lets `--resume` continue a stopped run | machine use |
| `workspace/`, `baseline/` | the sandbox the agents wrote into, and a pristine copy of the existing code | internal; safe to ignore |

## Stage 1: Discover (nothing is generated yet)

The system first works out what is being asked and, when there is existing code, what state it is in.
- `09:19:31` The requirement analyst understood the request as plan type **greenfield**, domain `url_shortener`.

## Stage 2: Plan (the lead agent decomposes, the harness checks)
- `09:19:40` **AGENT (model) `project_lead`** proposed 10 tasks in 7 waves, and the harness accepted the plan (tools exist, dependencies satisfied, no write conflicts, one validator that waits for everything).
    - the lead's reasoning: Establish the architecture first, then design the API contract and persistence schema in parallel. Implementation proceeds alongside risk-driven verification planning; independent tests and documentat
    - wave 1: design_architecture   (tasks in one wave can run in parallel)
    - wave 2: design_contract,design_schema   (tasks in one wave can run in parallel)
    - wave 3: assess_risks,implement_service   (tasks in one wave can run in parallel)
    - wave 4: plan_validation   (tasks in one wave can run in parallel)
    - wave 5: write_documentation,write_spec_tests   (tasks in one wave can run in parallel)
    - wave 6: validate_delivery   (tasks in one wave can run in parallel)
    - wave 7: summarize_evidence   (tasks in one wave can run in parallel)
- `09:19:40` **Human gate 1: the plan.** Nothing is generated until it is approved.
- `09:19:51` Decision at `plan_approval`: **APPROVED** by `human` (interactive).

## Stage 3: Work (agents and steps run; the lead picks the order)

Tasks start when everything they need exists. The harness refuses anything not ready, retries flaky failures, and sends validator failures back to whoever is responsible.
- `09:19:51` Step `architect` (plain Python, no model) starts task `design_architecture`. Decide the architecture: components, data flows, capacity model, architecture decision records.
- `09:19:54` `project_lead` chose to start: `design_contract,design_schema`. Why: Both ready tasks can run independently in parallel and together unblock risk assessment and service implementation.
- `09:19:54` Step `contract_designer` (plain Python, no model) starts task `design_contract`. Publish the OpenAPI contract (openapi.json). Code, tests and docs all build on it.
- `09:19:54` Step `data_modeler` (plain Python, no model) starts task `design_schema`. Define the relational schema (schema.sql).
- `09:19:54` `design_contract` finished and wrote: `openapi.json`.
- `09:19:54` `design_schema` finished and wrote: `schema.sql`.
- `09:19:57` `project_lead` chose to start: `assess_risks,implement_service`. Why: Both ready tasks are independent and can run concurrently, unlocking validation planning and advancing implementation.
- `09:19:57` Step `risk_assessor` (plain Python, no model) starts task `assess_risks`. List risks, failure scenarios and mitigations for the work in hand.
- `09:19:57` **AGENT (model) `code_author`** starts task `implement_service`. It writes the code change. For existing code that means minimal anchored edits (each `find` text must match the original exactly once), so the result is a small reviewable diff, not a rewrite.
- `09:20:00` `project_lead` chose to start: `plan_validation`. Why: Start validation planning alongside service implementation to unblock specification tests and documentation.
- `09:20:00` Step `validation_strategist` (plain Python, no model) starts task `plan_validation`. Define the test strategy and verification steps, derived from the risks, before any test is written.
- `09:20:03` `project_lead` chose to start: `write_spec_tests,write_documentation`. Why: Both ready tasks can run independently alongside implementation and are required to unblock delivery validation.
- `09:20:03` **AGENT (model) `spec_tester`** starts task `write_spec_tests`. It writes the tests from the written contract only. It never sees the code, so its tests are an independent check, not a copy of the code's behaviour.
- `09:20:03` Step `doc_writer` (plain Python, no model) starts task `write_documentation`. Write the documentation: README, architecture, ADRs, risks (new system); change record and changelog (change); proposal (analysis).
- `09:20:03` `write_documentation` finished and wrote: `README.md`, `docs/ARCHITECTURE.md`, `docs/ADR.md`, `docs/RISKS.md`, `docs/TESTING.md`.
- `09:22:35` `write_spec_tests` called the model (`azure`, 152.3s).
- `09:22:35` `spec_tester` finished: wrote 51 test(s) in 5 file(s), from the specification only (it never saw the code).
    - `test_ac1_create_https_link`
    - `test_ac2_redirects_with_302`
    - `test_ac3_unknown_code_returns_json_404`
    - `test_ac4_taken_alias_returns_409`
    - `test_valid_alias_boundaries_and_characters`
- `09:23:16` `implement_service` called the model (`azure`, 198.7s).
- `09:23:16` `code_author` finished and wrote 15 file(s).

## Stage 4: Validate (the referee executes checks; it never asks a model)

**Validation round 1**
- `09:23:16` PASS `static.syntax`: every Python file parses.
- `09:23:16` PASS `static.secrets`: no password, key or token was written into the code.
- `09:23:16` PASS `static.required_files`: the files the service needs are present.
- `09:23:16` **FAIL** `probe.behaviour`: 14 black-box probes drove the service like a real client (create, redirect, stats, delete, restart). Evidence: failed probes: custom alias created (201) then conflict (409) ((201, 500))
- `09:23:16` PASS `probe.schema_conformance`: the database the code creates has every table and column that was designed.
- `09:23:17` **FAIL** `tests.suite`: the whole test suite was executed in an isolated process. Evidence: 51 ran; failing: ['test_ac4_taken_alias_returns_409 (tests.test_api.LinkAPITests)', 'test_concurrent_alias_creation_has_exactly_one_winner (
- `09:23:17` PASS `traceability.acceptance_criteria`: every acceptance criterion has a test that names it.
- `09:23:17` PASS `traceability.risks`: risks that name a covering test resolve to a real test.
- `09:23:17` PASS `docs.present`: documentation exists.
- `09:23:17` **Validation failed, so the referee sent it back** (repair round 1). Blamed role(s) re-run: `implement_service` with 2 finding(s) as feedback. This is the recovery loop: the failure is routed to whoever is responsible, not just retried blindly.
- `09:23:17` **AGENT (model) `code_author`** starts task `implement_service`. It writes the code change. For existing code that means minimal anchored edits (each `find` text must match the original exactly once), so the result is a small reviewable diff, not a rewrite. This is a **re-run (generation 1)**: it now receives the validator's exact findings as feedback.
- `09:23:46` `implement_service` called the model (`azure`, 29.1s).
- `09:23:46` `implement_service` produced output that failed its own pre-check (shortener/app.py must define create_app(db_path=..., *, base_url, rate_limit_per_minute, api_keys, redirect_status)). It tries again with that problem quoted back to it.
