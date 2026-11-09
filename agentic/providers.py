"""Model providers: the harness is not tied to any vendor.

The harness needs exactly one thing from a model: `complete_json(system, prompt, max_tokens) -> dict`. Any backend that can
do that works. Choose with AGENTIC_PROVIDER, or leave it unset and the first provider whose settings exist is used
(order: azure, command, openai-compatible, anthropic).

  azure      Azure AI Foundry / Azure OpenAI
               AZURE_AI_ENDPOINT, AZURE_AI_DEPLOYMENT (or AGENTIC_MODEL), AZURE_AI_API_KEY
               no key: Microsoft Entra ID (managed identity in Azure, `az login` locally)
  openai     any OpenAI-compatible chat-completions API (OpenAI, GitHub Models, OpenRouter, Ollama, vLLM, LM Studio, ...)
               AGENTIC_MODEL, AGENTIC_BASE_URL (default https://api.openai.com/v1), AGENTIC_API_KEY or OPENAI_API_KEY
  anthropic  Claude through the Anthropic Messages API
               ANTHROPIC_API_KEY, optional AGENTIC_MODEL
  command    any program that reads the prompt on stdin and prints a reply (a CLI you already have logged in)
               AGENTIC_LLM_COMMAND   e.g. a coding-assistant CLI's non-interactive mode; see .env.example

Model output is ALWAYS treated as untrusted: parsed defensively, size-capped, written only through the sandboxed Workspace
(write scopes, extension allow-list, secret scan) and judged by executing checks. That holds for every provider.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

PROVIDERS = ("azure", "openai", "anthropic", "command")


class LLMUnavailable(Exception):
    """Not configured, or a permanent failure (bad credentials, bad model name, unusable reply)."""


class LLMTransient(Exception):
    """Throttling / 5xx / network / truncated output: safe to retry with backoff."""


def extract_json(text: str) -> Dict[str, Any]:
    """Tolerant JSON extraction: strips code fences and surrounding prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMUnavailable("model reply contained no JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"model reply was not valid JSON: {exc}") from exc


class NullProvider:
    """No model configured. `reason` says what to set."""
    name = "none"
    available = False
    usage: Dict[str, int] = {}

    def __init__(self, reason: str = "no model configured: set AGENTIC_PROVIDER and its settings (see .env.example)"):
        self.reason = reason

    def complete_json(self, system: str, prompt: str, max_tokens: int = 1024) -> Dict[str, Any]:
        raise LLMUnavailable(self.reason)


class _Usage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def add(self, prompt: int, completion: int) -> None:
        with self._lock:
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += prompt
            self.usage["completion_tokens"] += completion


TRANSIENT_HTTP = (408, 409, 429, 500, 502, 503, 504, 529)


class OpenAICompatibleProvider(_Usage):
    """Chat completions against any OpenAI-compatible endpoint (stdlib only).

    Different servers disagree on two details, so the provider adapts once and remembers: some models reject
    `response_format` (JSON mode), and newer OpenAI models want `max_completion_tokens` where many servers want `max_tokens`.
    """

    name = "openai-compatible"
    available = True
    hint = "check the base URL, model name and API key"

    def __init__(self, base_url: str, model: str, api_key: Optional[str] = None, timeout: int = 300):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._json_mode = True
        self._token_param = "max_completion_tokens"

    # subclasses change only these two
    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4096) -> Dict[str, Any]:
        for attempt in (1, 2, 3):
            body: Dict[str, Any] = {"model": self.model, self._token_param: max_tokens,
                                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
            if self._json_mode:
                body["response_format"] = {"type": "json_object"}
            req = urllib.request.Request(self._url(), data=json.dumps(body).encode(), method="POST",
                                         headers={"content-type": "application/json", **self._auth_headers()})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code == 400 and attempt < 3:
                    if self._json_mode and "response_format" in detail:
                        self._json_mode = False  # no JSON mode here: rely on the prompt and the tolerant parser
                        continue
                    if self._token_param == "max_completion_tokens" and "max_completion_tokens" in detail:
                        self._token_param = "max_tokens"
                        continue
                    if self._token_param == "max_tokens" and "max_tokens" in detail:
                        self._token_param = "max_completion_tokens"
                        continue
                if exc.code in TRANSIENT_HTTP:
                    raise LLMTransient(f"HTTP {exc.code}: {detail}") from exc
                raise LLMUnavailable(f"HTTP {exc.code} from {self.name} ({self.hint}): {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LLMTransient(f"network error calling {self.name}: {exc}") from exc
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailable(f"unexpected {self.name} response shape: {str(payload)[:200]}") from exc
        u = payload.get("usage") or {}
        self.add(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))
        if choice.get("finish_reason") == "length":
            raise LLMTransient("model output was truncated (finish_reason=length); retrying with feedback")
        return extract_json(text)


class AzureOpenAIProvider(OpenAICompatibleProvider):
    """Azure AI Foundry / Azure OpenAI: the same API on Azure's route, with Azure authentication.

    The model is the DEPLOYMENT name. Key auth uses the `api-key` header; without a key it uses Microsoft Entra ID
    (the app's managed identity in Azure, or `az login` on a developer machine)."""

    name = "azure"
    hint = "check endpoint, deployment name, RBAC"
    SCOPE = "https://cognitiveservices.azure.com"

    def __init__(self, endpoint: str, deployment: str, api_key: Optional[str] = None, timeout: int = 300):
        super().__init__(endpoint.rstrip("/") + "/openai/v1", deployment, api_key, timeout)
        self.endpoint = endpoint.rstrip("/")
        self.deployment = deployment
        self._token: Optional[tuple] = None  # (token, expiry_epoch)

    def _auth_headers(self) -> Dict[str, str]:
        if self.api_key:
            return {"api-key": self.api_key}
        return {"Authorization": f"Bearer {self._entra_token()}"}

    def _entra_token(self) -> str:
        if self._token and self._token[1] - 120 > time.time():
            return self._token[0]
        ident, header = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
        try:
            if ident and header:  # Azure Container Apps / App Service managed identity (keyless)
                client_id = os.environ.get("AZURE_CLIENT_ID")
                url = f"{ident}?resource={self.SCOPE}&api-version=2019-08-01" + (f"&client_id={client_id}" if client_id else "")
                req = urllib.request.Request(url, headers={"X-IDENTITY-HEADER": header})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                self._token = (data["access_token"], float(data.get("expires_on", time.time() + 3000)))
            else:  # local developer: az login
                out = subprocess.run(["az", "account", "get-access-token", "--resource", self.SCOPE, "-o", "json"],
                                     capture_output=True, text=True, timeout=30, shell=(os.name == "nt"))
                if out.returncode != 0:
                    raise LLMUnavailable("no AZURE_AI_API_KEY, no managed identity, and `az login` token failed: " + out.stderr[:200])
                data = json.loads(out.stdout)
                self._token = (data["accessToken"], time.time() + 1800)
        except LLMUnavailable:
            raise
        except Exception as exc:
            raise LLMUnavailable(f"could not obtain an Entra ID token: {exc}") from exc
        return self._token[0]


AzureFoundryProvider = AzureOpenAIProvider  # earlier name, kept so existing imports keep working


class AnthropicProvider(_Usage):
    """Claude through the Anthropic Messages API (stdlib only)."""

    name = "anthropic"
    available = True
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: Optional[str] = None, timeout: int = 300):
        super().__init__()
        self.api_key = api_key
        self.model = model or os.environ.get("AGENTIC_MODEL", "claude-sonnet-5-5")
        self.timeout = timeout

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4096) -> Dict[str, Any]:
        body = json.dumps({"model": self.model, "max_tokens": max_tokens, "system": system,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in TRANSIENT_HTTP:
                raise LLMTransient(f"HTTP {exc.code}: {detail}") from exc
            raise LLMUnavailable(f"HTTP {exc.code} from anthropic (check ANTHROPIC_API_KEY and the model name): {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMTransient(f"network error calling anthropic: {exc}") from exc
        u = payload.get("usage") or {}
        self.add(int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        if payload.get("stop_reason") == "max_tokens":
            raise LLMTransient("model output was truncated (stop_reason=max_tokens); retrying with feedback")
        return extract_json(text)


class CommandProvider(_Usage):
    """Any program that reads the prompt on stdin and prints a reply on stdout.

    This is the route for tools that have no public model API but do have a command line (for example a coding assistant's
    non-interactive mode). The command comes from YOUR configuration (AGENTIC_LLM_COMMAND), exactly as if you typed it. The
    system prompt and the request are sent together on stdin; the reply must contain one JSON object (fences and prose are
    tolerated). Token usage is estimated (about 4 characters per token) because a command does not report it."""

    name = "command"
    available = True

    def __init__(self, command: str, timeout: int = 900):
        super().__init__()
        self.command = command
        self.model = shlex.split(command, posix=True)[0] if command.strip() else "-"  # shown in the run summary
        self.timeout = timeout

    def complete_json(self, system: str, prompt: str, max_tokens: int = 4096) -> Dict[str, Any]:
        text = f"{system}\n\n{prompt}\n\nReply with ONE JSON object and nothing else."
        argv = self.command if os.name == "nt" else shlex.split(self.command)  # a .cmd shim needs the shell on Windows
        try:
            proc = subprocess.run(argv, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout, shell=(os.name == "nt"))
        except subprocess.TimeoutExpired as exc:
            raise LLMTransient(f"command timed out after {self.timeout}s") from exc
        except (OSError, ValueError) as exc:
            raise LLMUnavailable(f"could not run AGENTIC_LLM_COMMAND ({self.command!r}): {exc}") from exc
        if proc.returncode != 0:
            raise LLMUnavailable(f"command exited with {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
        self.add(len(text) // 4, len(proc.stdout) // 4)
        return extract_json(proc.stdout)


def load_dotenv(path=None) -> int:
    """Load KEY=VALUE lines from a .env file into os.environ (stdlib only).

    Called by the entry points (run.py, scripts), deliberately NOT by get_provider(), so tests stay hermetic.
    Never overrides a variable that is already set; skips blanks, comments and unfilled '<placeholder>' values.
    Returns the number of variables set.
    """
    from pathlib import Path
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.is_file():
        return 0
    n = 0
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if not key or not val or val.startswith("<") or key in os.environ:
            continue
        os.environ[key] = val
        n += 1
    return n


def get_provider():
    """The model provider named by AGENTIC_PROVIDER, or the first whose settings exist. Never raises: a missing or
    incomplete setting gives a NullProvider whose `reason` says what to set."""
    env = os.environ
    name = (env.get("AGENTIC_PROVIDER") or "").strip().lower()
    model = env.get("AGENTIC_MODEL")
    if not name:
        if env.get("AZURE_AI_ENDPOINT") and (env.get("AZURE_AI_DEPLOYMENT") or model):
            name = "azure"
        elif env.get("AGENTIC_LLM_COMMAND"):
            name = "command"
        elif env.get("AGENTIC_BASE_URL") and model:
            name = "openai"
        elif env.get("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif env.get("OPENAI_API_KEY") and model:
            name = "openai"
        else:
            return NullProvider()
    if name not in PROVIDERS:
        return NullProvider(f"no model configured: AGENTIC_PROVIDER={name!r} is not one of {list(PROVIDERS)}")
    if name == "azure":
        deployment = env.get("AZURE_AI_DEPLOYMENT") or model
        if not (env.get("AZURE_AI_ENDPOINT") and deployment):
            return NullProvider("no model configured: provider 'azure' needs AZURE_AI_ENDPOINT and AZURE_AI_DEPLOYMENT")
        return AzureOpenAIProvider(env["AZURE_AI_ENDPOINT"], deployment, env.get("AZURE_AI_API_KEY"))
    if name == "openai":
        if not model:
            return NullProvider("no model configured: provider 'openai' needs AGENTIC_MODEL (and optionally AGENTIC_BASE_URL, AGENTIC_API_KEY)")
        return OpenAICompatibleProvider(env.get("AGENTIC_BASE_URL") or "https://api.openai.com/v1", model,
                                        env.get("AGENTIC_API_KEY") or env.get("OPENAI_API_KEY"))
    if name == "anthropic":
        if not env.get("ANTHROPIC_API_KEY"):
            return NullProvider("no model configured: provider 'anthropic' needs ANTHROPIC_API_KEY")
        return AnthropicProvider(env["ANTHROPIC_API_KEY"], model)
    if not env.get("AGENTIC_LLM_COMMAND"):
        return NullProvider("no model configured: provider 'command' needs AGENTIC_LLM_COMMAND")
    return CommandProvider(env["AGENTIC_LLM_COMMAND"])


def sanitize_questions(raw: Any, limit: int = 3) -> List[str]:
    """Validate untrusted LLM output: list of short strings, capped."""
    if not isinstance(raw, list):
        return []
    return [i.strip() for i in raw if isinstance(i, str) and 5 <= len(i) <= 240][:limit]
