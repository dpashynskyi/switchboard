"""Offline handoffs; every path is temporary and every provider is blocked or mocked."""
import contextlib
import io
import json
import os

# The tests read a frozen roles file, not the live roles.json.
os.environ["ASK_ROLES"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_roles.json")
os.environ["ASK_LANG"] = "uk"  # the tests read the Ukrainian words; test_limits checks the English ones too
import tempfile as _tempfile
# A machine's own machine.json, roles.json and journal never decide a test.
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest import mock
import ask
import broker
import dashboard
import handoff
import providers
import route
from test_route import free_state

ORIGINAL_HISTORY = handoff.acp_history
ROOT = Path(__file__).resolve().parents[2]


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT)))
        for key, name in {"CACHE_DIR": "cache", "LOG_DIR": "logs", "ASK_ROOM": "agy room",
                          "CODEX_ROOM": "codex room", "JOURNAL_PATH": "usage.jsonl",
                          "ACCOUNTS_HOME": "home", "HOME": "home", "CONFIG_PATH": "models.json",
                          "MACHINE_PATH": "machine.json", "AGY_CONVERSATIONS_PATH": "conversations.json",
                          "RESERVATIONS_PATH": "reservations.json", "RESERVATIONS_LOCK": "reservations.lock"}.items():
            self.stack.enter_context(mock.patch.object(route, key, str(self.root / name)))
        Path(route.CACHE_DIR).mkdir()
        self.fake = free_state()
        self.fake.pop("journal", None)
        self.fake["codex_accounts"] = [
            {"id": "codex:~/.codex", "where": "~/.codex", "active": True},
            {"id": "codex:~/.codex work", "where": "~/.codex work", "active": False}]
        self.stack.enter_context(mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.fake),
                                                             "ASK_CONTEXT": "personal", "CODEX_HOME": "~/.codex"}))
        self.stack.enter_context(mock.patch("subprocess.Popen", side_effect=AssertionError("no real provider")))
        self.stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=AssertionError("no network")))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.out))
        self.stack.enter_context(contextlib.redirect_stderr(self.err))
        self.history = self.stack.enter_context(mock.patch.object(handoff, "acp_history", return_value=[]))

    def main(self, *args):
        self.out.seek(0); self.out.truncate()
        with mock.patch.object(sys, "argv", ["ask.py", *args]):
            return ask.main()

    def row(self, cid="thread", provider="codex", **kw):
        return route.journal("ask.py", None, "model", provider, "model", conversation=cid,
                             context="personal", account=route.account_of(provider), **kw)

    def events(self):
        return [r for r in route._read_journal_rows(journal_path=route.JOURNAL_PATH) if r.get("type") == "handoff"]

    def append(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def usage(self, n):
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": n, "cached_input_tokens": n // 2, "output_tokens": n // 5,
            "reasoning_output_tokens": n // 10, "total_tokens": n * 2}}}}

    def turn(self, user, answer, reasoning="", model="model"):
        def msg(role, text):
            return {"type": "response_item", "payload": {"type": "message", "role": role, "content": [
                {"type": "input_text" if role == "user" else "output_text", "text": text}]}}
        return [{"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "turn_context", "payload": {"model": model}},
                msg("developer", "never print this"), msg("user", "<environment_context>context</environment_context>"),
                msg("user", user), {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": reasoning}]}},
                msg("assistant", answer), {"type": "event_msg", "payload": {"type": "task_complete"}}]

    def test_commands(self):
        agy = providers.get("agy").handoff("agy", "id 'quote", "model")
        self.assertEqual(shlex.split(agy), ["cd", route.ASK_ROOM, "&&", "agy", "--conversation", "id 'quote", "--model", "model"])
        for acc in self.fake["codex_accounts"]:
            cmd = providers.get("codex").handoff(acc["id"], "thread")
            self.assertEqual(shlex.split(cmd), ["cd", route.CODEX_ROOM, "&&", "CODEX_HOME=" + providers.expand(acc["where"]),
                                               "codex", "resume", "-C", route.CODEX_ROOM, "-s", "read-only", "thread"])
            self.assertEqual(cmd, providers.get("codex").handoff(acc, "thread"))
        self.assertIsNone(providers.get("codex").handoff("codex:unknown", "thread"))
        for provider in ("ollama", "claude-subscription"):
            self.assertIsNone(providers.get(provider).handoff(provider, "thread"))
        for room in ("/non-ascii/é", "/newline\npath", "/control\x7fpath"):
            with mock.patch.object(route, "CODEX_ROOM", room), mock.patch.object(route, "ASK_ROOM", room):
                self.assertIsNone(providers.get("codex").handoff("codex:~/.codex", "thread"))
                self.assertIsNone(providers.get("agy").handoff("agy", "thread"))

    def test_lookup(self):
        self.row("alpha", "agy"); self.row("alpine", "agy"); self.row("beta", "agy")
        broker.atomic_json(str(Path(route.CACHE_DIR) / "ask_last_conversation.json"), {"agy": "alpha"})
        self.assertEqual(handoff.find_conversation("")["conversation"], "alpha")
        self.assertEqual(handoff.find_conversation("bet")["conversation"], "beta")
        for arg in ("al", "unknown"):
            with self.assertRaises(ValueError):
                handoff.find_conversation(arg)
        (Path(route.CACHE_DIR) / "ask_last_conversation.json").unlink()
        directory = Path(ask.reasoning_dir()); directory.mkdir()
        broker.atomic_json(str(directory / "last.json"), {"conversation": "beta"})
        self.assertEqual(handoff.find_conversation("")["conversation"], "beta")

    def test_policy_and_crossing(self):
        self.row("thread", "agy")
        self.assertEqual(self.main("--handoff", "thread", "--context", "work"), 7)
        self.assertEqual(self.events()[-1]["event"], "refused")
        self.history.assert_not_called()
        self.assertEqual(self.out.getvalue(), "")
        self.fake["machine"] = {"accounts": {"agy": {"label": "personal", "work_ok": True}}}
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.fake)}):
            self.assertEqual(self.main("--handoff", "thread", "--context", "work"), 0)
        self.assertIn("crossing:", self.err.getvalue())
        self.assertTrue(self.events()[-1]["crossed"])
        self.assertEqual(len(self.out.getvalue().splitlines()), 2)

    def test_open_blocks_continuations(self):
        self.row("thread", "agy")
        self.assertEqual(self.main("--handoff", "thread"), 0)
        self.assertEqual(self.main("--handoff", "thread"), 2)
        broker.atomic_json(str(Path(route.CACHE_DIR) / "ask_last_conversation.json"), {"agy": "thread"})
        for cid in ("thread", True):
            self.assertEqual(ask.ask_broker("agy", "model", "question", cid)[0], 2)
        self.assertIn("ask.py --handoff-back thread ... first", self.err.getvalue())

    def test_back_modes_rollout_and_totals(self):
        for mode in ("full", "summary", "none"):
            cid = "thread-" + mode
            self.row(cid)
            path = Path(providers.expand("~/.codex")) / "sessions/2026/11/05" / ("rollout-stamp-" + cid + ".jsonl")
            self.append(path, self.turn("old 1", "old answer 1") + self.turn("old 2", "old answer 2") + [self.usage(100)])
            self.assertEqual(self.main("--handoff", cid), 0)
            clean = route.read_journal_24h()
            self.append(path, self.turn("someone@example.com " + "x" * 350,
                                       "sk-" + "x" * 20 + "\n\nSecond paragraph",
                                       "Private summary", "new-model") + [self.usage(200)])
            self.assertEqual(self.main("--handoff-back", cid, mode), 0)
            out = self.out.getvalue()
            for secret in ("someone@example.com", "sk-", "Private summary", "old answer", "never print this"):
                self.assertNotIn(secret, out)
            if mode == "full":
                self.assertIn("<email>", out); self.assertIn("Second paragraph", out)
            elif mode == "summary":
                self.assertIn("1 turns", out); self.assertIn("<email>", out)
                self.assertNotIn("Second paragraph", out)
            else:
                self.assertEqual(out.strip(), "The user finished in the terminal and passed nothing on (1 turns).")
            state = handoff._json(handoff._path(cid))
            self.assertFalse(state["open"]); self.assertEqual(state["turns"], 1)
            self.assertEqual(state["mode"], mode)
            text = (Path(ask.reasoning_dir()) / (cid + ".md")).read_text()
            self.assertIn("## Turn 1", text); self.assertIn("Private summary", text)
            self.assertIn("new-model (terminal)", text)
            event = self.events()[-1]
            self.assertEqual(event["terminal_tokens"], {"in": 100, "cached_in": 50, "out": 20, "reasoning": 10, "total": 200})
            self.assertNotIn("tokens", event); self.assertNotIn("pool", event)
            self.assertEqual(route.read_journal_24h(), clean)
            view, recent = dashboard.journal_views(route._read_journal_rows(journal_path=route.JOURNAL_PATH), time.time() + 1)
            self.assertTrue(all(v == 0 for day in view["days"] for v in day["tokens"].values()))
            self.assertEqual(recent[0]["status"], "handoff back")
            self.assertIsNone(recent[0]["total_tokens"])
            self.assertEqual(self.main("--handoff-back", cid, mode), 2)

    def test_agy_back_and_fallback(self):
        self.row("agy-id", "agy", crossed=True)
        existing = broker.ReasoningLog(ask.reasoning_dir(), "agy-id", "agy", "model")
        existing.close(None)
        self.history.side_effect = OSError("unavailable")
        handoff.start("agy-id")
        self.assertEqual(handoff._json(handoff._path("agy-id"))["mark_from"], "reasoning")
        self.assertIn("previously crossed", self.err.getvalue())
        self.history.side_effect = None
        self.history.return_value = [{"user": "old", "answer": "old", "reasoning": "old"},
                                     {"user": "someone@example.com", "answer": "answer", "reasoning": "terminal thought"}]
        self.assertEqual(self.main("--handoff-back", "agy-id", "full"), 0)
        self.assertIn("<email>", self.out.getvalue())
        self.assertNotIn("terminal thought", self.out.getvalue())
        text = (Path(ask.reasoning_dir()) / "agy-id.md").read_text()
        self.assertIn("## Turn 2", text); self.assertIn("terminal thought", text)
        self.assertNotIn("terminal_tokens", self.events()[-1])

    def test_invalid_mode_empty_reasoning_and_unsupported(self):
        self.row("agy-id", "agy")
        handoff.start("agy-id")
        self.assertEqual(self.main("--handoff-back", "agy-id", "invalid"), 2)
        self.assertTrue(handoff.is_open("agy-id"))
        self.history.return_value = [{"user": "hi", "answer": "hello", "reasoning": ""}]
        handoff.back("agy-id", "none")
        self.assertIn("no reasoning from the provider", (Path(ask.reasoning_dir()) / "agy-id.md").read_text())
        self.row("local", "ollama")
        self.assertEqual(self.main("--handoff", "local"), 2)
        self.assertIn("this provider cannot carry", self.err.getvalue())

    def test_acp_history_replay_protocol(self):
        calls = []
        class Transport:
            def __init__(self, command, cwd, handler, notification, *args):
                self.notification = notification
                calls.append(command)
            def request(self, method, params, timeout):
                calls.append((method, params))
                if method == "session/load":
                    for kind, text in [("user_message_chunk", "Q"), ("user_message_chunk", "1"),
                                       ("agent_thought_chunk", "thought"), ("agent_message_chunk", "A"),
                                       ("user_message_chunk", "Q2"), ("agent_message_chunk", "A2")]:
                        self.notification("session/update", {"update": {"sessionUpdate": kind, "content": {"text": text}}})
            def close(self):
                calls.append("closed")
        with mock.patch.object(broker, "Transport", Transport):
            result = ORIGINAL_HISTORY("fake-agy", "session", route.ASK_ROOM)
        self.assertEqual(result, [{"user": "Q1", "reasoning": "thought", "answer": "A"},
                                  {"user": "Q2", "reasoning": "", "answer": "A2"}])
        self.assertEqual(calls[1][0], "initialize"); self.assertEqual(calls[2][0], "session/load")
        self.assertEqual(calls[-1], "closed")


if __name__ == "__main__":
    unittest.main()
