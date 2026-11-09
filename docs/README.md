# Documentation index

Start with the [root README](../README.md): the ask, the use cases, how the agents are structured, and a map of every folder. Then read in this order.

| # | Read | You get |
|---|---|---|
| 0 | [../input/README.md](../input/README.md) | what goes in: one input folder per scenario, mirrored by `output/` |
| 1 | [ARCHITECTURE.md](ARCHITECTURE.md) | the design in one place: five layers, how an agent is designed, control flow, guardrails, the validator, the test-integrity policies, decisions, limits and roadmap |
| 2 | [usecases/01_GREENFIELD.md](usecases/01_GREENFIELD.md) | the URL shortener: who takes part, design flow, test cases, decisions |
| 3 | [usecases/02_BROWNFIELD.md](usecases/02_BROWNFIELD.md) | bug fix, enhancement, refactor, tests/docs: design flow, the two policies that keep tests honest, test cases per kind, decisions |
| 4 | [usecases/03_AMBIGUOUS.md](usecases/03_AMBIGUOUS.md) | the vague requirement: why it pauses, the proposal it produces, test cases, decisions |
| 5 | [HOW_TO_RUN_AND_VERIFY.md](HOW_TO_RUN_AND_VERIFY.md) | the command for each scenario, what to open, how to know it worked (`scripts/verify_outputs.py`), a demo script |
| 6 | [MODEL_SETUP.md](MODEL_SETUP.md) | the `.env` file and running without Azure Foundry (OpenAI, Claude, GitHub Models, local models, a tool's CLI) |
| 7 | [TESTING.md](TESTING.md) | what "correct" means at three levels and how each is tested |
| 8 | [INTERVIEW_QA.md](INTERVIEW_QA.md) | likely questions with defensible answers |
| 9 | [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) | optional: the hosted service with remote approval gates (not yet run in Azure) |

Each document has one job and the others link to it rather than repeating it. If two documents ever disagree, ARCHITECTURE.md and the use-case pages are authoritative.
