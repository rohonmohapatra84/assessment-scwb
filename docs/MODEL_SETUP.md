# Model setup: running the system without Azure Foundry

The system needs **one thing** from a model: take a prompt, return one JSON object. Everything else (planning checks, the sandbox, validation,
the gates) is plain Python. So any model that can do that works, and you choose it in the `.env` file at the project root.

> **Important.** Claude Code, Codex, Cursor and GitHub Copilot are **applications**, not model APIs. This project cannot call "Copilot" or "Cursor"
> directly. You reach the model behind them in one of two ways: (a) use an **API** the same vendor or a partner offers (section 2), or (b) drive
> the tool's **command line** through the `command` provider (section 3). Route (a) is more reliable.

## 1. How `.env` works

1. Copy `.env.example` to `.env` (same folder as `run.py`).
2. Fill in **one** provider block. Lines starting with `#` are ignored.
3. Optionally set `AGENTIC_PROVIDER=azure|openai|anthropic|command`. If unset, the first provider whose settings exist is used, in this order:
   `azure`, `command`, `openai`, `anthropic`. **Leave only one block filled in**, or set `AGENTIC_PROVIDER` explicitly, so you know which one runs.
4. Check it: `python scripts/model_smoke.py`. It tests configuration, authentication, a JSON reply, and that the model can produce valid Python in the
   system's real output format. It prints latency and token usage. **If it fails, nothing else will work.**
5. Run any scenario: `python run.py greenfield`, `python run.py brownfield`, and so on. `run.py` and the scripts read `.env` themselves. The test suite
   never reads it (it uses a scripted stand-in), so tests stay offline and free.

`.env` holds secrets. Do not commit it or paste it into chats (the repository's `.gitignore` should list it; check with `git check-ignore .env` if you use git).

**File format.** One `KEY=value` per line, no quotes needed (quotes are stripped if you add them). Blank lines and lines starting with `#` are ignored. A value that still starts with `<` (an unfilled placeholder) counts as **not set**. A variable already set in your shell is **not** overridden by `.env`, so if a run uses the wrong model, check `echo $env:AGENTIC_PROVIDER` and the other variables in your PowerShell session.

### Complete `.env` examples (copy one, replace the values)

**Azure AI Foundry / Azure OpenAI** (the setup the system was verified with). The deployment name is the name you gave the deployment, not the model family. Omit the key to use `az login`:

```
AZURE_AI_ENDPOINT=https://my-resource.services.ai.azure.com
AZURE_AI_DEPLOYMENT=my-deployment-name
AZURE_AI_API_KEY=xxxxxxxxxxxxxxxx
```

**Anthropic (Claude) API key:**

```
AGENTIC_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
AGENTIC_MODEL=claude-sonnet-5-5
```

**OpenAI API key:**

```
AGENTIC_PROVIDER=openai
AGENTIC_BASE_URL=https://api.openai.com/v1
AGENTIC_MODEL=<a current chat model id from your account>
AGENTIC_API_KEY=sk-xxxxxxxxxxxxxxxx
```

**OpenRouter or GitHub Models** (the same shape; only the URL, model id and key change. Take the exact URL, model id and token scope from the provider's current documentation):

```
AGENTIC_PROVIDER=openai
AGENTIC_BASE_URL=<the provider's OpenAI-compatible base URL>
AGENTIC_MODEL=<a model id from the provider's catalog>
AGENTIC_API_KEY=<your key or token>
```

**A local model, no cloud and no key** (Ollama shown; LM Studio and vLLM only change the URL and port):

```
AGENTIC_PROVIDER=openai
AGENTIC_BASE_URL=http://localhost:11434/v1
AGENTIC_MODEL=<the model name you pulled>
```

**A tool's command line** (see section 3 for the risks):

```
AGENTIC_PROVIDER=command
AGENTIC_LLM_COMMAND=powershell -NoProfile -File C:\tools\ask.ps1
```

Then run `python scripts/model_smoke.py`.

## 2. Providers you can use (pick the one you already have)

| You have | Provider | What to put in `.env` | Verified here? |
|---|---|---|---|
| **Azure AI Foundry / Azure OpenAI** | `azure` | `AZURE_AI_ENDPOINT=https://<resource>.services.ai.azure.com`<br>`AZURE_AI_DEPLOYMENT=<deployment name>`<br>`AZURE_AI_API_KEY=<key>` (omit the key to use `az login`) | **Yes, live** (all scenarios) |
| **OpenAI API key** | `openai` | `AGENTIC_PROVIDER=openai`<br>`AGENTIC_BASE_URL=https://api.openai.com/v1`<br>`AGENTIC_MODEL=<model id>`<br>`AGENTIC_API_KEY=<key>` | Wire format tested with mocked HTTP only |
| **Anthropic (Claude) API key** | `anthropic` | `AGENTIC_PROVIDER=anthropic`<br>`ANTHROPIC_API_KEY=<key>`<br>`AGENTIC_MODEL=claude-sonnet-5-5` (optional) | Wire format tested with mocked HTTP only |
| **GitHub account (Copilot users)** | `openai` via **GitHub Models** | `AGENTIC_PROVIDER=openai`<br>`AGENTIC_BASE_URL=<GitHub Models inference URL>`<br>`AGENTIC_MODEL=<model id from the GitHub Models catalog>`<br>`AGENTIC_API_KEY=<a GitHub token with models access>` | Not run. Get the exact URL, model ids and token scope from GitHub's current docs |
| **OpenRouter** (one key, many models) | `openai` | `AGENTIC_BASE_URL=https://openrouter.ai/api/v1`, `AGENTIC_MODEL=<id>`, `AGENTIC_API_KEY=<key>` | Not run |
| **Local model** (Ollama, LM Studio, vLLM) | `openai` | `AGENTIC_BASE_URL=http://localhost:11434/v1` (Ollama), `AGENTIC_MODEL=<name>`; no key needed | Not run |

Real-world notes:
- A greenfield run asks the model for large outputs (a whole service, a test suite of 20+ tests) and a valid JSON plan. **Use a strong, current model.**
  Small local models usually fail at the plan step (`plan_rejected`) or return truncated or malformed JSON (`failed`). The system protects itself
  (it rejects bad output and retries), but it cannot make a weak model strong.
- Cost: a greenfield run used about 10 calls and roughly 100 to 150k tokens including repairs; the link-expiry enhancement about 54k prompt and 17k completion tokens.
  The summary records calls and tokens for every run.
- Only `azure` has been run live. For the others, **`python scripts/model_smoke.py` is your test**: if it passes, the real runs use the same code path.

## 3. Using a tool's command line (Claude Code, Codex, Cursor, Copilot CLI)

The `command` provider runs a command you supply, sends the system prompt and the request together on **stdin**, and reads one JSON object from **stdout**
(code fences and surrounding prose are tolerated). Set:

```
AGENTIC_PROVIDER=command
AGENTIC_LLM_COMMAND=<the command, exactly as you would type it, in non-interactive mode>
```

Starting points to **confirm with the tool's own `--help`** (I have not run any of these, and flags change between versions):

| Tool | Non-interactive mode is typically | Notes |
|---|---|---|
| Claude Code | `claude -p` (print mode) | reads the prompt from stdin and prints the reply |
| OpenAI Codex CLI | `codex exec` | check how it takes a prompt on stdin |
| Cursor | `cursor-agent -p` | check the flag for printing without interaction |
| GitHub Copilot CLI | `copilot -p` | check the current command name and flags |

**Risks specific to this route, and what to do about them:**

1. **These are coding agents with tools.** A CLI may try to read or edit files in its working directory. The provider runs the command from your current
   folder, i.e. the project. Run it from a harmless place with a small wrapper, and disable the tool's file tools if it has a flag for that. Example wrapper
   (Windows, untested, adapt it; save as `C:\tools\ask.ps1`):

   ```powershell
   $prompt = [Console]::In.ReadToEnd()
   Push-Location $env:TEMP          # a harmless working directory
   $prompt | claude -p              # replace with your tool's non-interactive command
   Pop-Location
   ```

   then `AGENTIC_LLM_COMMAND=powershell -NoProfile -File C:\tools\ask.ps1`.
2. **Its own system prompt and tools can add prose or ask questions.** The reply must still contain one JSON object. If `model_smoke.py` fails on the
   "JSON reply" step, the tool is not following the instruction; try another mode or provider.
3. **Slow and sequential.** Every model call starts a process (timeout 15 minutes each). A greenfield run may take much longer than with an API.
4. **Token usage is estimated** (about 4 characters per token); the command does not report it.
5. **Subscription terms.** Check that scripted use of your tool's CLI is allowed under your plan.

If a tool's CLI does not behave, use its **API** instead (section 2): Claude through `anthropic`, OpenAI/Codex models through `openai`, Copilot-licensed
models through GitHub Models.

## 4. What you can do with no model at all

| Goal | How | Needs a model? |
|---|---|---|
| Prove the framework works end to end, including fault injection and the policies | `python -m unittest discover -s tests -t .` (273 tests, about 5 minutes) | No (scripted stand-in) |
| Read real results of every scenario | open the committed `output/<scenario>/ENGINEERING_SUMMARY.md` (if you kept it) | No |
| Re-check committed outputs | `python scripts/verify_outputs.py` (re-runs each release's own tests) | No |
| Run the generated URL shortener | `cd output/greenfield/release; python -m shortener.server --port 8080` | No |
| List the agents and steps | `python run.py agents` | No |
| Run a **new** scenario | `python run.py ...` | **Yes.** The lead agent that plans is a model, and there is deliberately no hidden offline plan |

So a reviewer with no model access can still verify everything that was recorded and every safety claim, but cannot produce a new run.

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no model configured: ...` | no provider block is filled in, or `AGENTIC_PROVIDER` names one with missing settings. The message says which setting |
| `HTTP 401` | wrong or missing key for that provider |
| `HTTP 403` | key valid but not permitted. Azure with Entra ID: the *Cognitive Services OpenAI User* role on the resource (can take minutes to apply) |
| `HTTP 404` | wrong model name or base URL. Azure: use the **deployment name**, not the model family |
| `HTTP 429` or 5xx | rate limit or provider trouble. These are retried with backoff; if it persists, wait or lower concurrency |
| `plan_rejected` | the model could not produce a valid plan in 3 attempts; try a stronger model |
| Smoke test passes but a run fails at the code or tests step | the model's output was truncated or too weak; open `events.jsonl` and look at `task_failed` and `validation_check` lines |
| Two providers configured and the wrong one runs | set `AGENTIC_PROVIDER` explicitly |
