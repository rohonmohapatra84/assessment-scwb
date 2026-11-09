#!/usr/bin/env python3
"""Pre-flight check for whichever model provider you configured. Run this BEFORE `python run.py ...`.

  python scripts/model_smoke.py

Configure a provider in .env or the environment (see .env.example): azure, openai (any OpenAI-compatible API), anthropic, or a
command. It checks, in order: configuration, authentication and reachability with a JSON reply, and that the model can follow the
platform's real output contract (a tiny {"files":[...]} reply with valid, working Python). It prints latency and token usage so
you can estimate the cost of a run. Exit code 0 only if every check passes.
"""
import ast
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentic.providers import LLMTransient, LLMUnavailable, get_provider, load_dotenv  # noqa: E402

load_dotenv()  # reads ./.env if present

HINTS = {
    "401": "Authentication failed: wrong or missing API key for this provider.",
    "403": "Authorised but not permitted: check the key's permissions (Azure with Entra ID: the 'Cognitive Services OpenAI User' role "
           "on the resource, which can take minutes to propagate) and any network or firewall settings.",
    "404": "Not found: check the model name (Azure: the DEPLOYMENT name, not the model family) and the base URL / endpoint.",
}


def step(name, fn):
    t0 = time.time()
    try:
        out = fn()
        print(f"  PASS  {name}  ({time.time() - t0:.1f}s)")
        return out
    except (LLMUnavailable, LLMTransient) as exc:
        msg = str(exc)
        print(f"  FAIL  {name}: {msg[:400]}")
        for code, hint in HINTS.items():
            if f"HTTP {code}" in msg:
                print(f"        -> {hint}")
        if isinstance(exc, LLMTransient):
            print("        -> transient (throttling/network): retry, or check your quota")
        sys.exit(1)


def main() -> None:
    p = get_provider()
    print("Model smoke test")
    if not p.available:
        print(f"  FAIL  configuration: {p.reason}")
        sys.exit(1)
    print(f"  provider {p.name}\n  model    {getattr(p, 'model', '-')}")

    r = step("reachable + authenticated + JSON reply",
             lambda: p.complete_json("Reply with JSON only.", 'Return {"ok": true, "n": 3}', max_tokens=200))
    assert isinstance(r, dict), "reply was not a JSON object"

    def contract():
        reply = p.complete_json(
            "You are a code generator. Reply with ONE JSON object only.",
            'Return {"files":[{"path":"shortener/hello.py","content":"<python source>"}]} where the file defines '
            "def add(a, b): returning a+b, with a one-line docstring.", max_tokens=1500)
        files = reply.get("files")
        assert isinstance(files, list) and files, f"no 'files' list in reply: {reply}"
        src = files[0]["content"]
        ast.parse(src)
        ns = {}
        exec(compile(src, "hello.py", "exec"), ns)  # trusted: our own two-line prompt, run only here, in this smoke test
        assert ns["add"](2, 3) == 5, "generated add() is wrong"
        return reply

    step("follows the platform's {files:[{path,content}]} contract with valid, working Python", contract)
    u = p.usage
    print(f"\n  model calls {u['calls']}, tokens in/out {u['prompt_tokens']}/{u['completion_tokens']}")
    print("  A full run uses roughly 6-12 model calls with the template steps (more when the models also write code and tests).")
    print("\nAll checks passed: you can run  python run.py greenfield")


if __name__ == "__main__":
    main()
