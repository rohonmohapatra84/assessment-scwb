# Documentation index

Start with the [root README](../README.md): the ask, the use cases, how to connect a model and run each scenario (shorten your own URL through the agents, plant a bug and watch the fix), and how to read a run's output. Then use this page to find the one document that answers your next question.

**Each document has one job; the others link to it instead of repeating it.**

| Question | Read | It is the only place for |
|---|---|---|
| How do I connect a model? I have no Azure Foundry | [MODEL_SETUP.md](MODEL_SETUP.md) | complete `.env` examples for every provider, a tool's CLI, what works with no model, troubleshooting |
| What was built, and how does it work? | [ARCHITECTURE.md](ARCHITECTURE.md) | the design in plain language: a run start to finish, who does what, guardrails, the validator, failure handling, decisions, limits and roadmap |
| How does each use case work, and how is it judged? | [usecases/01_GREENFIELD.md](usecases/01_GREENFIELD.md), [02_BROWNFIELD](usecases/02_BROWNFIELD.md), [03_AMBIGUOUS](usecases/03_AMBIGUOUS.md) | each scenario's roles, flow, test cases and decisions |
| What goes in? | [../input/README.md](../input/README.md) | one input folder per scenario, mirrored by `output/`; the planted bugs are in `input/brownfield/bugs/` |
| How do I run the advanced scenarios, failure demos, or re-check results? | [HOW_TO_RUN_AND_VERIFY.md](HOW_TO_RUN_AND_VERIFY.md) | `verify_outputs`, status values, enhancement/refactor/tests-only, control and failure demos, a demo script, troubleshooting |
| How is it tested? | [TESTING.md](TESTING.md) | the three levels of testing, the test inventory (273 tests), the rule-to-test mapping, the bugs found |
| Can I host it? | [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) | optional: the hosted service with remote approval gates (not yet run in Azure) |

If two documents ever disagree, ARCHITECTURE.md and the use-case pages are authoritative.
