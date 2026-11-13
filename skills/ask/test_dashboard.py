"""Offline dashboard checks; run with python -I skills/ask/test_dashboard.py."""

from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import http.client
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
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import dashboard
import route


NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp()


def fixture():
    return {
        "cloud": {"pools": [
            {"id": "gemini", "provider": "agy", "label": "Gemini Models", "covers": ["gemini-*"],
             "windows": [
                 {"window": "weekly", "remaining_fraction": 0.08, "pace": 2.6, "resets_in_s": 90061},
                 {"window": "5h", "remaining_fraction": 0.6, "pace": 3.0, "resets_in_s": 3661},
             ]},
            {"id": "third-party", "provider": "agy", "label": "Claude and GPT models", "covers": ["claude-*", "gpt-oss-*"],
             "windows": [
                 {"window": "weekly", "remaining_fraction": 0.6, "pace": 2.6},
                 {"window": "5h", "remaining_fraction": 0.15, "pace": 2.6},
             ]},
            {"id": "future-pool", "provider": "future", "label": "Future pool", "covers": ["new-*"],
             "windows": [{"window": "monthly", "remaining_fraction": 0.04, "pace": 4.0}]},
        ]},
        "claude_usage": {
            "read_at": datetime.fromtimestamp(NOW - 7 * 3600, timezone.utc).isoformat(),
            "plan": "Max", "windows": [
                {"window": "weekly", "label": "weekly · all models", "remaining_fraction": 0.5, "pace": 1.0},
                {"window": "weekly", "label": "weekly · Fable", "remaining_fraction": 0.02, "pace": 0.7},
            ],
        },
        "codex_usage": {"rateLimits": {"planType": "pro", "secondary": {
            "windowDurationMins": 10080, "usedPercent": 30, "resetsAt": NOW + 86400,
        }}},
        "local": {
            "processes": [], "quns": 5, "gpu_used_mb": 2048, "gpu_total_mb": 16384,
            "gpu_util": 7, "ollama_up": True,
            "ollama_ps": [{"name": "qwen3:14b", "size_vram": 2_000_000_000}],
        },
        "reservations": [
            {"pid": 123, "alive": True, "script": "writer.py", "role": "writer", "provider": "agy",
             "pool_id": "gemini", "model": "gemini-4-argon-high", "tokens": 300000,
             "share": {"weekly": 0.05, "5h": 0.15}},
            {"pid": 124, "alive": False, "script": "dead.py", "provider": "agy", "pool_id": "gemini",
             "model": "gemini-4-argon-high", "tokens": 100000, "share": {"weekly": 0.5}},
        ],
    }


class DashboardTests(unittest.TestCase):
    def setUp(self):
        # Keep all fixtures and potential accidental cache writes in this worktree.
        self.room = tempfile.TemporaryDirectory(dir=HERE)
        self.addCleanup(self.room.cleanup)
        self.root = Path(self.room.name)
        self.journal = self.root / "usage.jsonl"
        self.journal.write_text("", encoding="utf-8")
        self.fake = fixture()
        self.fake["journal_path"] = str(self.journal)
        self.fake_path = self.root / "fake.json"
        self.fake_path.write_text(json.dumps(self.fake), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": str(self.fake_path)})
        env.start()
        self.addCleanup(env.stop)
        for target in ("subprocess.run", "subprocess.Popen", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("Dashboard tests must not contact providers"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("CACHE_DIR", "ASK_ROOM", "CODEX_ROOM", "LOG_DIR"):
            patcher = mock.patch.object(route, name, str(self.root))
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("USAGE_CACHE", "CODEX_USAGE_CACHE", "CODEX_MODELS_CACHE", "AGY_MODELS_CACHE",
                     "CLAUDE_USAGE_PATH", "VRAM_CACHE", "LOCAL_LOCK", "RESERVATIONS_PATH",
                     "RESERVATIONS_LOCK", "CONFIG_PATH", "MACHINE_PATH", "JOURNAL_PATH"):
            patcher = mock.patch.object(route, name, str(self.root / (name.lower() + ".json")))
            patcher.start()
            self.addCleanup(patcher.stop)
        Path(route.MACHINE_PATH).write_text("{}", encoding="utf-8")

    def write_rows(self, rows):
        with self.journal.open("w", encoding="utf-8") as stream:
            stream.write("not a JSON line\n")
            for row in rows:
                stream.write(json.dumps(row) + "\n")
            stream.write('{"unfinished":\n')

    def start_server(self, store=None):
        server = dashboard.make_server(0, store=store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop)
        return server

    def get(self, server, path):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_api_preserves_all_pools_windows_numbers_and_statuses(self):
        state = dashboard.gather_state(now=NOW)
        expected = route.cloud_state(fake=self.fake, now=NOW)
        pools = state["cloud"]["pools"]
        self.assertEqual([p["id"] for p in pools], [p["id"] for p in expected["pools"]])
        self.assertEqual(sum(len(p["windows"]) for p in pools), 8)
        for pool, original in zip(pools, expected["pools"]):
            for window, original_window in zip(pool["windows"], original["windows"]):
                for key, value in original_window.items():
                    self.assertEqual(window[key], value, (pool["id"], key))
                self.assertEqual(window["remaining_percent"], f"{original_window['remaining_fraction'] * 100:.1f}%")
        by_id = {p["id"]: p for p in pools}
        self.assertEqual([w["status"] for w in by_id["gemini"]["windows"]], ["critical", "good"])
        self.assertEqual([w["status"] for w in by_id["third-party"]["windows"]], ["warning", "good"])
        self.assertEqual(by_id["future-pool"]["windows"][0]["status"], "critical")
        self.assertEqual(by_id["codex"]["windows"][0]["status"], "good")
        self.assertEqual([w["status"] for p in pools if p["provider"] == "claude-subscription" for w in p["windows"]], ["good", "critical"])
        self.assertEqual(state["cloud"]["claude_subscription"]["status"], "stale")
        self.assertEqual(len(by_id["gemini"]["reservations"]), 1)
        self.assertAlmostEqual(by_id["gemini"]["windows"][0]["available_fraction"], 0.03)
        self.assertEqual(by_id["gemini"]["windows"][0]["remaining_percent"], "8.0%")
        server = self.start_server(dashboard.StateStore(fake=self.fake))
        with mock.patch.object(dashboard.time, "time", return_value=NOW):
            status, headers, body = self.get(server, "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        actual = json.loads(body)
        self.assertEqual([p["id"] for p in actual["cloud"]["pools"]], [p["id"] for p in pools])
        self.assertEqual([w["status"] for p in actual["cloud"]["pools"] for w in p["windows"]],
                         [w["status"] for p in pools for w in p["windows"]])

    def test_status_boundaries_follow_configured_rules(self):
        rules = {"floors": {"weekly": 0.2, "5h": 0.3}, "pace_skip": 3.0}
        for window, remaining, pace, expected in (
            ("weekly", 0.199, 4.0, "critical"), ("weekly", 0.2, 3.0, "good"),
            ("weekly", 0.2, 3.001, "warning"), ("5h", 0.299, 4.0, "critical"),
            ("5h", 0.3, 4.0, "good"), ("monthly", 0.199, 4.0, "critical"),
        ):
            with self.subTest(window=window, remaining=remaining, pace=pace):
                self.assertEqual(dashboard.window_status({"window": window, "remaining_fraction": remaining, "pace": pace}, rules), expected)

    def test_chart_sums_four_named_series_and_overflow_and_fills_days(self):
        today = datetime.fromtimestamp(NOW, timezone.utc)
        rows = []
        for offset in (0, 2, 13):
            stamp = (today - timedelta(days=offset)).timestamp()
            for provider, pool, model, total in (
                ("agy", "Gemini Models", "gemini-4-argon-high", 100),
                ("agy", None, "gemini-3.8-flash-high", 11),
                ("agy", "Claude and GPT models", "claude-sonnet-5-5-high", 200),
                ("agy", None, "gpt-oss-120b-medium", 22),
                ("codex", "ChatGPT (pro)", "gpt-6.1-sol", 300),
                ("ollama", "local", "qwen3:14b", 400),
                ("future", "unknown", "new-model", 500),
                ("agy", "New pool", "gemini-new", 33),
            ):
                rows.append({"ts": stamp, "provider": provider, "pool": pool, "model": model, "tokens": {"total": total}})
        rows.extend([
            {"time": today.isoformat(), "provider": "ollama", "tokens": {"in": 8, "out": 9}},
            {"ts": NOW, "provider": "agy", "model": "gemini-4-argon-high", "tokens": {"total": None, "in": 4, "out": 6}},
            {"ts": NOW - 14 * 86400, "provider": "ollama", "tokens": {"total": 9999}},
            {"ts": NOW + 86400, "provider": "ollama", "tokens": {"total": 9999}},
            {"ts": NOW, "type": "verdict", "verdict": "BUSY", "tokens": {"total": 9999}},
            {"time": "broken timestamp", "tokens": {"total": 9999}},
            None,
        ])
        self.write_rows(rows)
        chart = dashboard.gather_state(now=NOW)["chart"]
        self.assertEqual(chart["timezone"], "UTC")
        self.assertEqual(len(chart["days"]), 14)
        self.assertEqual(chart["days"][0]["day"], "2026-10-21")
        self.assertEqual(chart["days"][-1]["day"], "2026-11-03")
        self.assertEqual([s["id"] for s in chart["series"]], [s[0] for s in dashboard.SERIES])
        for index, day in enumerate(chart["days"]):
            expected = {key: 0 for key, _, _ in dashboard.SERIES}
            if index in (0, 11, 13):
                expected.update({"agy-gemini": 111, "agy-claude-gpt": 222, "codex-chatgpt": 300, "ollama-local": 400, "other": 533})
            if index == 13:
                expected["ollama-local"] += 17
                expected["agy-gemini"] += 10
            self.assertEqual(day["tokens"], expected, day["day"])

    def test_table_is_newest_first_caps_at_twenty_and_shows_verdict(self):
        rows = [{"ts": NOW - index * 60, "time": datetime.fromtimestamp(NOW - index * 60, timezone.utc).isoformat(),
                 "script": f"script-{index}.py", "tokens": {"total": index}, "status": "ok"} for index in range(27)]
        rows[0].update({"type": "verdict", "verdict": "BUSY: engine about to start", "status": "ok"})
        self.write_rows(list(reversed(rows)))
        recent = dashboard.gather_state(now=NOW)["journal"]
        self.assertEqual(len(recent), 20)
        self.assertEqual([r["script"] for r in recent], ["verdict"] + [f"script-{i}.py" for i in range(1, 20)])
        self.assertIsNone(recent[0]["total_tokens"])
        self.assertEqual(recent[0]["status"], "BUSY: engine about to start")
        self.assertEqual(recent[19]["total_tokens"], 19)

    def test_gpu_preserves_every_busy_reason_and_ollama_details(self):
        fake = copy.deepcopy(self.fake)
        fd, module = tempfile.mkstemp(suffix="_hooks.py", dir=self.root)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("def local_blockers(state, settings):\n    return ['engine running', 'engine about to start']\n")
        fake["machine"] = {"hooks": {"module": module}}
        fake["local"].update({"lock": {"pid": 54321, "alive": True}, "ollama_up": False})
        state = dashboard.gather_state(fake=fake, now=NOW)
        expected = route.local_state(fake=fake)
        self.assertEqual(state["local"], expected)
        self.assertFalse(state["local"]["free"])
        for reason in ("engine running", "about to start", "local.lock"):
            self.assertTrue(any(reason in value for value in state["local"]["reasons"]), reason)
        self.assertEqual(state["gpu_display"]["vram"], "2.0 / 16.0 GB VRAM")
        self.assertEqual(state["gpu_display"]["loaded"], ["qwen3:14b (2.0 GB)"])

    def test_once_inlines_safe_json_without_external_dependencies(self):
        hostile = '</script><script src="evil.js">&'
        self.write_rows([{"ts": NOW, "script": hostile, "tokens": {"total": 25}}])
        output = self.root / "snapshot.html"
        with mock.patch.object(dashboard, "make_server", side_effect=AssertionError("Snapshot must not serve")), \
                mock.patch.object(dashboard.webbrowser, "open", side_effect=AssertionError("Snapshot must not open a browser")):
            self.assertEqual(dashboard.main(["--once", str(output)]), 0)
        page = output.read_text(encoding="utf-8")
        self.assertNotIn("__SNAPSHOT__", page)
        self.assertNotIn(hostile, page)
        self.assertNotRegex(page, r"(?i)(?:https?:)?//")

        class DependencyParser(HTMLParser):
            def handle_starttag(parser, tag, attrs):
                self.assertFalse(any(key in ("src", "href") for key, _ in attrs), tag)

        DependencyParser().feed(page)
        data = re.search(r'<script id="snapshot" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(data)["journal"][0]["script"], hostile)
        self.assertIn("snapshot (automatic refresh disabled)", page)

    def test_loopback_server_is_lazy_and_two_tabs_share_one_gather(self):
        store = dashboard.StateStore()
        with mock.patch.object(dashboard, "gather_state", wraps=dashboard.gather_state) as gather:
            server = self.start_server(store)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            gather.assert_not_called()
            status, headers, body = self.get(server, "/")
            self.assertEqual(status, 200)
            self.assertIn(b"Local limits", body)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            gather.assert_not_called()
            self.assertEqual(self.get(server, "/favicon.ico")[0], 404)
            barrier = threading.Barrier(2)

            def read_tab(_):
                barrier.wait(timeout=5)
                return self.get(server, "/api/state")

            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(read_tab, range(2)))
            self.assertEqual([reply[0] for reply in replies], [200, 200])
            self.assertEqual(json.loads(replies[0][2]), json.loads(replies[1][2]))
            self.assertEqual(gather.call_count, 1)
            self.get(server, "/api/state")
            self.assertEqual(gather.call_count, 1)
            store._gathered_at -= 61
            self.get(server, "/api/state")
            self.assertEqual(gather.call_count, 2)

    def test_foreign_host_header_is_refused(self):
        server = self.start_server(dashboard.StateStore())
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            connection.request("GET", "/api/state", headers={"Host": "evil.example"})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()
        self.assertEqual(self.get(server, "/")[0], 200)

    def test_recent_rows_are_trimmed_and_nan_is_served(self):
        self.write_rows([{"ts": NOW, "time": datetime.fromtimestamp(NOW, timezone.utc).isoformat(), "script": "ask.py",
                          "status": "ok", "tokens": {"total": 5}, "seconds": float("nan"),
                          "log": "/home/someone/.cache/ask/logs/x.log", "account": "codex:~/.codex", "note": "private"}])
        state = dashboard.gather_state(now=NOW)
        row = state["journal"][0]
        self.assertNotIn("log", row)
        self.assertNotIn("account", row)
        self.assertNotIn("note", row)
        self.assertEqual(row["total_tokens"], 5)
        body = json.dumps(dashboard.finite(state), allow_nan=False)
        self.assertNotIn("NaN", body)

    def test_api_gather_failure_is_json_and_retryable(self):
        store = mock.Mock()
        store.get.side_effect = [RuntimeError("private provider details"), {"ok": True}]
        server = self.start_server(store)
        status, _, body = self.get(server, "/api/state")
        self.assertEqual(status, 503)
        self.assertIn("error", json.loads(body))
        self.assertNotIn(b"private provider details", body)
        self.assertEqual(json.loads(self.get(server, "/api/state")[2]), {"ok": True})


if __name__ == "__main__":
    unittest.main()
