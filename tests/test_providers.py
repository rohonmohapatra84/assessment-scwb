"""Model providers: the harness is vendor-neutral, so every provider is tested for its wire format, its error mapping and
how it is selected. Offline: HTTP is mocked and the command provider runs tiny local scripts. No network, no credentials.

What this proves is the WIRE FORMAT as the providers document it. It does not prove that a live service accepts it: only
`python scripts/model_smoke.py` against that service does, and so far only the azure provider has been run live.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agentic.providers import (AnthropicProvider, AzureOpenAIProvider, CommandProvider, LLMTransient, LLMUnavailable, NullProvider,
                         OpenAICompatibleProvider, get_provider)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def chat_ok(content='{"x": 1}', finish="stop", usage=(11, 7)):
    return Resp(json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}],
                            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}).encode())


def http_error(code, body="boom"):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))


class OpenAICompatible(unittest.TestCase):
    def provider(self, **kw):
        return OpenAICompatibleProvider("https://api.example.com/v1/", "some-model", **kw)

    def test_request_shape_with_a_key(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data))
            return chat_ok()
        with mock.patch("urllib.request.urlopen", fake):
            p = self.provider(api_key="sk-test")
            self.assertEqual(p.complete_json("sys", "hello", max_tokens=50), {"x": 1})
        self.assertEqual(seen["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual((seen["body"]["model"], seen["body"]["max_completion_tokens"]), ("some-model", 50))
        self.assertEqual(seen["body"]["messages"][1], {"role": "user", "content": "hello"})
        self.assertEqual(p.usage, {"calls": 1, "prompt_tokens": 11, "completion_tokens": 7})

    def test_no_key_means_no_auth_header_for_local_servers(self):
        seen = {}
        with mock.patch("urllib.request.urlopen", lambda req, timeout=None: (seen.update(h=dict(req.header_items())), chat_ok())[1]):
            self.provider().complete_json("s", "p")
        self.assertNotIn("Authorization", seen["h"])

    def test_servers_that_want_max_tokens_are_handled_once_and_remembered(self):
        used = []

        def fake(req, timeout=None):
            body = json.loads(req.data)
            used.append("max_completion_tokens" in body)
            if "max_completion_tokens" in body:
                raise http_error(400, "Unrecognized request argument: max_completion_tokens")
            return chat_ok()
        with mock.patch("urllib.request.urlopen", fake):
            p = self.provider()
            p.complete_json("s", "p")
            p.complete_json("s", "p")
        self.assertEqual(used, [True, False, False])

    def test_models_that_reject_max_tokens_are_handled_too(self):
        p = self.provider()
        p._token_param = "max_tokens"
        used = []

        def fake(req, timeout=None):
            body = json.loads(req.data)
            used.append("max_tokens" in body)
            if "max_tokens" in body:
                raise http_error(400, "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.")
            return chat_ok()
        with mock.patch("urllib.request.urlopen", fake):
            p.complete_json("s", "p")
        self.assertEqual(used, [True, False])

    def test_both_adaptations_can_happen_in_one_call(self):
        calls = []

        def fake(req, timeout=None):
            body = json.loads(req.data)
            calls.append(sorted(k for k in body if k in ("response_format", "max_completion_tokens", "max_tokens")))
            if "response_format" in body:
                raise http_error(400, "response_format is not supported")
            if "max_completion_tokens" in body:
                raise http_error(400, "max_completion_tokens is not supported")
            return chat_ok()
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(self.provider().complete_json("s", "p"), {"x": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], ["max_tokens"])

    def test_error_mapping(self):
        for code in (408, 429, 500, 503):
            with mock.patch("urllib.request.urlopen", side_effect=http_error(code)), self.assertRaises(LLMTransient):
                self.provider().complete_json("s", "p")
        for code in (401, 403, 404):
            with mock.patch("urllib.request.urlopen", side_effect=http_error(code)), \
                    self.assertRaisesRegex(LLMUnavailable, "from openai-compatible.*check the base URL"):
                self.provider().complete_json("s", "p")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")), self.assertRaises(LLMTransient):
            self.provider().complete_json("s", "p")

    def test_truncation_and_bad_shapes(self):
        with mock.patch("urllib.request.urlopen", return_value=chat_ok(finish="length")), self.assertRaisesRegex(LLMTransient, "truncated"):
            self.provider().complete_json("s", "p")
        with mock.patch("urllib.request.urlopen", return_value=Resp(b'{"oops": 1}')), self.assertRaisesRegex(LLMUnavailable, "response shape"):
            self.provider().complete_json("s", "p")

    def test_fenced_json_in_the_reply_is_accepted(self):
        with mock.patch("urllib.request.urlopen", return_value=chat_ok('Here you go:\n```json\n{"a": 2}\n```')):
            self.assertEqual(self.provider().complete_json("s", "p"), {"a": 2})

    def test_azure_is_the_same_provider_with_azure_routing_and_auth(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update(url=req.full_url, headers=dict(req.header_items()))
            return chat_ok()
        with mock.patch("urllib.request.urlopen", fake):
            p = AzureOpenAIProvider("https://res.services.ai.azure.com/", "my-deployment", api_key="k")
            p.complete_json("s", "p")
        self.assertIsInstance(p, OpenAICompatibleProvider)
        self.assertEqual(seen["url"], "https://res.services.ai.azure.com/openai/v1/chat/completions")
        self.assertEqual(seen["headers"]["Api-key"], "k")
        self.assertNotIn("Authorization", seen["headers"])
        self.assertEqual((p.model, p.deployment, p.name), ("my-deployment", "my-deployment", "azure"))


def anthropic_ok(text='{"x": 1}', stop="end_turn", usage=(9, 4)):
    return Resp(json.dumps({"content": [{"type": "text", "text": text}], "stop_reason": stop,
                            "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}}).encode())


class Anthropic(unittest.TestCase):
    def test_request_shape_and_usage(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data), timeout=timeout)
            return anthropic_ok()
        with mock.patch("urllib.request.urlopen", fake):
            p = AnthropicProvider("sk-ant-test", model="claude-test")
            self.assertEqual(p.complete_json("the system", "the prompt", max_tokens=77), {"x": 1})
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(seen["headers"]["X-api-key"], "sk-ant-test")
        self.assertEqual(seen["headers"]["Anthropic-version"], "2023-06-01")
        self.assertEqual((seen["body"]["model"], seen["body"]["max_tokens"], seen["body"]["system"]), ("claude-test", 77, "the system"))
        self.assertEqual(seen["body"]["messages"], [{"role": "user", "content": "the prompt"}])
        self.assertGreaterEqual(seen["timeout"], 120)  # code generation takes longer than a short request
        self.assertEqual(p.usage, {"calls": 1, "prompt_tokens": 9, "completion_tokens": 4})

    def test_truncation_is_transient_and_errors_are_mapped(self):
        p = AnthropicProvider("k", model="m")
        with mock.patch("urllib.request.urlopen", return_value=anthropic_ok(stop="max_tokens")), self.assertRaisesRegex(LLMTransient, "truncated"):
            p.complete_json("s", "p")
        for code in (429, 500, 529):
            with mock.patch("urllib.request.urlopen", side_effect=http_error(code)), self.assertRaises(LLMTransient):
                p.complete_json("s", "p")
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401)), self.assertRaisesRegex(LLMUnavailable, "ANTHROPIC_API_KEY"):
            p.complete_json("s", "p")

    def test_json_is_extracted_from_text_blocks(self):
        with mock.patch("urllib.request.urlopen", return_value=anthropic_ok('Sure.\n```json\n{"a": 3}\n```')):
            self.assertEqual(AnthropicProvider("k", model="m").complete_json("s", "p"), {"a": 3})


class Command(unittest.TestCase):
    """A command provider runs whatever the user configured. Tested with tiny local scripts, not with any real tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cmd(self, source):
        script = Path(self.tmp.name) / f"s{abs(hash(source))}.py"
        script.write_text(source, encoding="utf-8")
        return f'"{sys.executable}" "{script}"'

    def test_prompt_goes_to_stdin_and_json_comes_back_from_stdout(self):
        cmd = self.cmd("import sys, json\ntext = sys.stdin.read()\n"
                       "print('Sure!\\n```json')\nprint(json.dumps({'got_system': 'SYS-MARK' in text, 'got_prompt': 'PROMPT-MARK' in text}))\nprint('```')\n")
        p = CommandProvider(cmd)
        self.assertEqual(p.complete_json("SYS-MARK", "PROMPT-MARK"), {"got_system": True, "got_prompt": True})
        self.assertEqual(p.usage["calls"], 1)
        self.assertGreater(p.usage["prompt_tokens"], 0)  # estimated: a command does not report usage

    def test_a_reply_without_json_is_a_bad_answer_not_a_dead_provider(self):
        with self.assertRaisesRegex(LLMUnavailable, "^model reply"):  # the lead treats this prefix as 'ask again'
            CommandProvider(self.cmd("print('I cannot help with that')")).complete_json("s", "p")

    def test_a_failing_command_reports_its_stderr(self):
        with self.assertRaisesRegex(LLMUnavailable, "exited with 3.*not logged in"):
            CommandProvider(self.cmd("import sys\nsys.stderr.write('not logged in')\nsys.exit(3)")).complete_json("s", "p")

    def test_a_slow_command_times_out_as_transient(self):
        with self.assertRaisesRegex(LLMTransient, "timed out"):
            CommandProvider(self.cmd("import time\ntime.sleep(10)"), timeout=1).complete_json("s", "p")

    def test_a_missing_program_is_unavailable(self):
        with self.assertRaises(LLMUnavailable):
            CommandProvider("definitely-not-a-real-program-xyz").complete_json("s", "p")


class Selection(unittest.TestCase):
    def pick(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return get_provider()

    def test_nothing_configured_says_what_to_set(self):
        p = self.pick()
        self.assertFalse(p.available)
        self.assertIn("AGENTIC_PROVIDER", p.reason)
        with self.assertRaisesRegex(LLMUnavailable, "no model configured"):
            p.complete_json("s", "p")

    def test_each_provider_is_selected_from_its_own_settings(self):
        a = self.pick(AZURE_AI_ENDPOINT="https://e", AZURE_AI_DEPLOYMENT="d", AZURE_AI_API_KEY="k")
        self.assertEqual((a.name, a.model, a.api_key), ("azure", "d", "k"))
        o = self.pick(AGENTIC_BASE_URL="http://localhost:11434/v1", AGENTIC_MODEL="llama3")
        self.assertEqual((o.name, o.base_url, o.model, o.api_key), ("openai-compatible", "http://localhost:11434/v1", "llama3", None))
        c = self.pick(ANTHROPIC_API_KEY="sk-ant", AGENTIC_MODEL="claude-x")
        self.assertEqual((c.name, c.model), ("anthropic", "claude-x"))
        m = self.pick(AGENTIC_LLM_COMMAND="my-cli --print")
        self.assertEqual((m.name, m.model), ("command", "my-cli"))

    def test_openai_defaults_to_the_public_base_url_and_reads_the_standard_key(self):
        o = self.pick(AGENTIC_PROVIDER="openai", AGENTIC_MODEL="gpt-x", OPENAI_API_KEY="sk-1")
        self.assertEqual((o.base_url, o.api_key), ("https://api.openai.com/v1", "sk-1"))

    def test_an_explicit_choice_beats_auto_detection(self):
        p = self.pick(AGENTIC_PROVIDER="anthropic", ANTHROPIC_API_KEY="k",
                      AZURE_AI_ENDPOINT="https://e", AZURE_AI_DEPLOYMENT="d")
        self.assertEqual(p.name, "anthropic")

    def test_incomplete_or_unknown_choices_explain_themselves(self):
        self.assertIn("AGENTIC_MODEL", self.pick(AGENTIC_PROVIDER="openai").reason)
        self.assertIn("ANTHROPIC_API_KEY", self.pick(AGENTIC_PROVIDER="anthropic").reason)
        self.assertIn("AGENTIC_LLM_COMMAND", self.pick(AGENTIC_PROVIDER="command").reason)
        self.assertIn("AZURE_AI_ENDPOINT", self.pick(AGENTIC_PROVIDER="azure").reason)
        bad = self.pick(AGENTIC_PROVIDER="skynet")
        self.assertFalse(bad.available)
        self.assertIn("azure", bad.reason)

    def test_a_bare_openai_key_without_a_model_does_not_guess(self):
        self.assertFalse(self.pick(OPENAI_API_KEY="sk-1").available)

    def test_null_provider_carries_a_custom_reason(self):
        self.assertEqual(NullProvider("because").reason, "because")


class HarnessIsVendorNeutral(unittest.TestCase):
    """No agent file, and nothing the harness shows a model, may name a vendor."""

    def test_agent_files_name_no_vendor(self):
        agents = Path(__file__).resolve().parent.parent / "agentic" / "agents"
        for md in agents.rglob("*.md"):
            text = md.read_text(encoding="utf-8").lower()
            for vendor in ("foundry", "azure", "openai", "anthropic", "claude", "gpt", "copilot", "cursor", "codex"):
                self.assertNotIn(vendor, text, f"{md.name} mentions {vendor}")

    def test_the_failure_message_for_a_missing_model_names_no_vendor_variable(self):
        from agentic.harness.harness import Harness
        with tempfile.TemporaryDirectory() as tmp:
            r = Harness(Path(tmp) / "run", echo=None, llm=NullProvider()).run("Build a scalable URL shortener service with APIs, persistence, and analytics.")
        self.assertEqual(r.status, "failed")
        self.assertIn("AGENTIC_PROVIDER", r.report["error"])
        self.assertNotIn("AZURE", r.report["error"])


if __name__ == "__main__":
    unittest.main()
