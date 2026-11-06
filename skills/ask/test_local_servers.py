"""Offline coverage of configured OpenAI-compatible local servers."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["ASK_ROLES"] = os.path.join(HERE, "test_roles.json")
os.environ["ASK_LANG"] = "uk"
import tempfile as _tempfile
# A machine's own machine.json, roles.json and journal never decide a test.
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
import ask
import providers
import route
from test_route import free_state


def setUpModule():
    # Keep even legacy accounting tests inside the test room.
    tmp = tempfile.TemporaryDirectory(dir=HERE)
    unittest.addModuleCleanup(tmp.cleanup)
    # Keep temporary fixtures within this worktree.
    temp_patcher = mock.patch.object(tempfile, "tempdir", tmp.name)
    temp_patcher.start()
    unittest.addModuleCleanup(temp_patcher.stop)
    patches = {
        "CACHE_DIR": tmp.name, "LOG_DIR": os.path.join(tmp.name, "logs"),
        "RESERVATIONS_PATH": os.path.join(tmp.name, "reservations.json"),
        "RESERVATIONS_LOCK": os.path.join(tmp.name, "reservations.lock"),
        "AGY_CONVERSATIONS_PATH": os.path.join(tmp.name, "agy_conversations.json"),
        "JOURNAL_PATH": os.path.join(tmp.name, "usage.jsonl"),
        "CONFIG_PATH": os.path.join(tmp.name, "models.json"),
        "MACHINE_PATH": os.path.join(tmp.name, "machine.json"),
        "ACCOUNTS_HOME": os.path.join(tmp.name, "home"),
        "CODEX_ROOM": os.path.join(tmp.name, "codex-room"),
        "CODEX_USAGE_CACHE": os.path.join(tmp.name, "codex_usage.json"),
        "CODEX_MODELS_CACHE": os.path.join(tmp.name, "codex_models.json"),
    }
    for key, value in patches.items():
        patcher = mock.patch.object(route, key, value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
    # The routing tests predate the context policy: they run in the personal context, where the role's
    # order stands; PolicyTests cover the policy itself.
    env_patcher = mock.patch.dict(os.environ, {"ASK_CONTEXT": "personal"})
    env_patcher.start()
    unittest.addModuleCleanup(env_patcher.stop)
    for target in ("subprocess.run", "subprocess.Popen", "urllib.request.urlopen"):
        patcher = mock.patch(target, side_effect=AssertionError("Tests must use fake providers"))
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def tearDownModule():
    # Python 3.9 only drains addModuleCleanup when this hook exists.
    pass



class LocalServersTests(unittest.TestCase):
    def setUp(self):
        providers._SERVER_MODELS.clear()
        self.state = free_state()
        self.config = {"url": "http://127.0.0.1:1234/v1"}
        self.state["machine"] = {"local_servers": {"studio": self.config}, "paths": {"ollama": None}}
        self.state["openai_servers"] = {"studio": {"up": True, "models": ["qwen3-14b"]}}

    def adapter(self):
        return providers.get("studio", fake=self.state)

    def response(self, data, raw=None):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = raw if raw is not None else json.dumps(data).encode()
        return reply

    def test_inventory_and_listing(self):
        self.assertIn("studio", route.local_providers(fake=self.state))
        self.assertIn("studio", [a.name for a in providers.all_adapters(fake=self.state)])
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state)}):
            self.assertIn("studio", providers.NAMES)
        self.assertIsNone(providers.account_email("studio", fake=self.state))
        rows, absent = route.live_inventory(fake=self.state)
        self.assertIn({"provider": "studio", "id": "qwen3-14b", "family": "qwen3-14b", "efforts": []}, rows)
        self.assertNotIn("studio", absent)
        rows = route.models(fake=self.state)
        self.assertEqual(next(r["pool"] for r in rows if r["provider"] == "studio"), "studio, local")
        inv = providers.inventory(fake=self.state, context="work")
        acc = next(r for r in inv if r["provider"] == "studio")["accounts"][0]
        self.assertEqual(acc["label"], "local")
        self.assertIn("studio", route.offered_providers("qwen3-14b", fake=self.state))
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state)}), \
             mock.patch.object(sys, "argv", ["ask.py", "--list"]), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ask.main(), 0)
            self.assertIn("studio", out.getvalue())

    def test_path_and_empty_ids(self):
        for raw in ("/models/model.gguf", "C:\\models\\model.bin", "model.GGUF", ""):
            self.state["openai_servers"]["studio"]["models"] = [raw]
            self.config["models"] = ["alias", "other"]
            self.assertEqual([r["id"] for r in self.adapter().models()[1]], ["alias"])
            self.config.pop("models")
            self.assertEqual([r["id"] for r in self.adapter().models()[1]], ["model"] if raw else [])

    def test_static_and_config_filter(self):
        self.assertFalse(route.is_model_offered("studio", "other", fake=self.state))
        with mock.patch("urllib.request.urlopen") as urlopen:
            code, text, _, _ = self.adapter().run(None, "other", None, "hello")
            self.assertEqual(code, 2)
            self.assertIn("not offered", text)
            urlopen.assert_not_called()
        self.state["openai_servers"]["studio"]["models"] = ["a", "b", "c"]
        self.config["models"] = ["b", "c"]
        self.assertEqual([r["id"] for r in self.adapter().models()[1]], ["b", "c"])
        self.config["static"] = True
        self.assertEqual(self.adapter().models()[1], [])
        self.config["models"] = ["a", "b"]
        self.assertEqual([r["id"] for r in self.adapter().models()[1]], ["a"])

    def test_invalid_names_reported(self):
        self.state["machine"]["local_servers"].update({"ollama": self.config, "bad:name": self.config})
        self.assertIsInstance(providers.get("ollama", fake=self.state), providers.OllamaAdapter)
        self.assertNotIn("bad:name", [a.name for a in providers.all_adapters(fake=self.state)])
        notes = [r["detect"].get("note") or "" for r in providers.inventory(fake=self.state)]
        self.assertEqual(sum("entry skipped" in n for n in notes), 2)

    def test_detection_http_and_absence(self):
        ad = providers.OpenAIAdapter("studio", self.config)
        with mock.patch("urllib.request.urlopen", return_value=self.response({"data": [{"id": "q"}]})) as call:
            self.assertTrue(ad.detect()["present"])
            self.assertEqual(call.call_args.kwargs["timeout"], 2)
            self.assertEqual(call.call_args.args[0].full_url, self.config["url"] + "/models")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as call:
            self.assertTrue(ad.detect()["present"])  # the list read a moment ago still holds: no second request
            call.assert_not_called()
            providers._SERVER_MODELS.clear()
            self.assertEqual(ad.detect()["note"], "no OpenAI-compatible API at " + self.config["url"])
            self.assertFalse(ad.detect()["present"])
            self.assertEqual(call.call_count, 1)  # a stopped server is asked once a TTL, not once a check
        self.state["openai_servers"]["studio"]["up"] = False
        self.assertFalse(self.adapter().detect()["present"])

    def test_run_usage_estimate_auth_and_log(self):
        self.config.update(api_key_env="ASK_TEST_KEY", load_timeout_s=180)
        for usage in ({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, None, "missing"):
            data = {"choices": [{"message": {"content": "answer"}}]}
            if usage != "missing":
                data["usage"] = usage
            with mock.patch.dict(os.environ, {"ASK_TEST_KEY": "secret-test-key"}), \
                 mock.patch("urllib.request.urlopen", return_value=self.response(data)) as call:
                code, answer, tokens, log = self.adapter().run(None, "qwen3-14b", None, "question", timeout_s=20)
            self.assertEqual((code, answer), (0, "answer"))
            request = call.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
            self.assertEqual(call.call_args.kwargs["timeout"], 200)
            self.assertEqual(json.loads(request.data), {"model": "qwen3-14b", "messages": [{"role": "user", "content": "question"}], "stream": False})
            if isinstance(usage, dict):
                self.assertEqual(tokens, {"in": 7, "out": 3, "total": 10})
            else:
                self.assertTrue(tokens["estimated"])
                self.assertEqual(tokens["total"], 3)
            with open(log, encoding="utf-8") as f:
                self.assertNotIn("secret-test-key", f.read())
            self.assertNotIn("secret-test-key", answer)

    def test_review_wire_ids_escaped_keys_cache_keys_and_refresh(self):
        # A shown alias is sent as the id the server listed.
        self.config.update(static=True, models=["qwen"])
        self.state["openai_servers"]["studio"]["models"] = ["/models/Qwen.gguf"]
        reply = {"choices": [{"message": {"content": "ok"}}]}
        with mock.patch("urllib.request.urlopen", return_value=self.response(reply)) as call:
            code, _, _, _ = self.adapter().run(None, "qwen", None, "q")
        self.assertEqual((code, json.loads(call.call_args.args[0].data)["model"]), (0, "/models/Qwen.gguf"))
        # A key echoed with JSON escapes is redacted in the answer and the log.
        self.config.update(static=False, models=[], api_key_env="ASK_TEST_KEY")
        self.state["openai_servers"]["studio"]["models"] = ["qwen3-14b"]
        escaped = '{"choices": [{"message": {"content": "\\u0073ecret-test-key"}}]}'.encode("utf-8")
        with mock.patch.dict(os.environ, {"ASK_TEST_KEY": "secret-test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=self.response(None, raw=escaped)):
            code, answer, _, log = self.adapter().run(None, "qwen3-14b", None, "q")
        self.assertEqual((code, answer), (0, "[redacted]"))
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("secret-test-key", text)
        self.assertNotIn("\\u0073ecret", text)
        # Two instances on one gateway with different keys do not share a list; force_refresh asks again.
        a = providers.OpenAIAdapter("a", {"url": "http://127.0.0.1:9/v1", "api_key_env": "KEY_A"})
        b = providers.OpenAIAdapter("b", {"url": "http://127.0.0.1:9/v1", "api_key_env": "KEY_B"})
        with mock.patch("urllib.request.urlopen", side_effect=[self.response({"data": [{"id": "x"}]}),
                                                               self.response({"data": [{"id": "y"}]}),
                                                               self.response({"data": [{"id": "z"}]})]) as call:
            self.assertEqual([r["id"] for r in a.models()[1]], ["x"])
            self.assertEqual([r["id"] for r in b.models()[1]], ["y"])
            self.assertEqual([r["id"] for r in a.models(force_refresh=True)[1]], ["z"])
        self.assertEqual(call.call_count, 3)

    def test_run_errors(self):
        for error in (urllib.error.HTTPError("url", 503, "bad", {}, None), urllib.error.URLError("offline")):
            with mock.patch("urllib.request.urlopen", side_effect=error):
                code, text, _, _ = self.adapter().run(None, "qwen3-14b", None, "question")
            self.assertEqual(code, 2)
            self.assertLess(len(text), 100)

    def test_local_role_and_memory(self):
        rules = route.load_roles()
        rules["roles"] = {"local-test": {"local_only": True, "models": [{"provider": "studio", "model": "qwen3-14b"}]}}
        result = route.pick("local-test", fake=self.state, rules=rules)
        self.assertEqual(result["provider"], "studio")
        self.assertEqual(result["model"], "qwen3-14b")
        for size in (100, {"qwen3-14b": 100}):
            self.config["size_gb"] = size
            result = route.check_name({"provider": "studio", "model": "qwen3-14b"}, fake=self.state)
            self.assertEqual(result["status"], "busy")
            self.assertIn("low VRAM", result["reason"])
        self.config.pop("size_gb")
        self.state["local"]["gpu_total_mb"] = 0
        with mock.patch.object(route, "needed_vram_gb", side_effect=AssertionError("no size check")):
            self.assertTrue(route.local_state("qwen3-14b", fake=self.state, provider="studio")["free"])
        self.state["local"]["lock"] = {"pid": 123456, "alive": True}
        self.assertFalse(route.local_state("qwen3-14b", fake=self.state, provider="studio")["free"])

    def test_explicit_call_lock_and_journal(self):
        events = []
        def run(*args, **kwargs):
            events.append("run")
            return 0, "answer", {"total": 3}, None
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state)}), \
             mock.patch.object(sys, "argv", ["ask.py", "--server", "studio", "qwen3-14b", "question"]), \
             mock.patch.object(route, "claim_gpu", side_effect=lambda *a: (events.append("claim") or True, "ok")), \
             mock.patch.object(route, "release_local_lock", side_effect=lambda: events.append("release")), \
             mock.patch.object(providers.OpenAIAdapter, "run", side_effect=run) as called, \
             mock.patch.object(route, "journal") as journal, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ask.main(), 0)
        self.assertEqual(events, ["claim", "run", "release"])
        self.assertEqual(called.call_args.args[3], "question")
        self.assertEqual(journal.call_args.args[3], "studio")


if __name__ == "__main__":
    unittest.main()
