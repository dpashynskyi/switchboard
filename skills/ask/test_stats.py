"""Offline stats transport and queue regression tests; no real provider calls."""
import base64
from contextlib import ExitStack
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import dashboard
import route
import stats_push
import stats_server
from stats_store import MAX_BODY, StatsStore, atomic_json

READ = "r" * 24
WRITE = "w" * 24


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StatsStore(self.tmp.name)
        self.server = stats_server.make_server(self.tmp.name, READ, WRITE, port=0, store=self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/api/state", token=READ, body=None, headers=None, method=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        h = {"Authorization": "Bearer " + token} if token is not None else {}
        if body is not None:
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        connection.request(method or ("POST" if body is not None else "GET"), path, body=body, headers=h)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_auth(self):
        for token in (None, "wrong"):
            status, headers, _ = self.request(token=token)
            self.assertEqual(status, 401)
            self.assertEqual(headers["WWW-Authenticate"], 'Basic realm="switchboard"')
        self.assertEqual(self.request(token=WRITE)[0], 403)
        self.assertEqual(self.request("/ingest", body={"machine": "pc"})[0], 403)

    def test_basic(self):
        auth = base64.b64encode(("any:" + READ).encode()).decode()
        self.assertEqual(self.request(headers={"Authorization": "Basic " + auth})[0], 200)
        for value in ("!bad!", base64.b64encode(b"\xff:x").decode(), base64.b64encode(b"no-colon").decode()):
            self.assertEqual(self.request(headers={"Authorization": "Basic " + value})[0], 401)

    def test_every_method_authenticates(self):
        for method in ("HEAD", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CUSTOM"):
            self.assertEqual(self.request(token=None, method=method)[0], 401)

    def test_token_validation(self):
        for read, write in ((None, WRITE), ("short", WRITE), (READ, "short"), (READ, READ)):
            with self.assertRaises(ValueError):
                stats_server.make_server(self.tmp.name, read, write, port=0)

    def test_ingest_disabled(self):
        server = stats_server.make_server(self.tmp.name, READ, port=0)
        with server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                conn.request("POST", "/ingest", b"{}", {"Authorization": "Bearer " + READ})
                self.assertEqual(conn.getresponse().status, 403)
                conn.close()
            finally:
                server.shutdown()
                thread.join()

    def test_dedupe_and_restart(self):
        body = {"machine": "pc", "rows": [{"ts": 1, "log": "private"}, {"ts": 2}]}
        first = self.request("/ingest", WRITE, body)
        self.assertEqual(json.loads(first[2])["added"], 2)
        self.assertEqual(json.loads(self.request("/ingest", WRITE, body)[2])["duplicates"], 2)
        restarted = StatsStore(self.tmp.name)
        self.assertEqual(restarted.ingest(body)["duplicates"], 2)
        self.assertNotIn("log", (Path(self.tmp.name) / "machines/pc/usage.jsonl").read_text())

    def test_validation(self):
        for body in ({"machine": "../x"}, {"machine": "PC"}, {"machine": "pc", "snapshots": {"unknown": {}}},
                     {"machine": "pc", "rows": [1]}, [], {"machine": ""}):
            self.assertEqual(self.request("/ingest", WRITE, body)[0], 400)
        self.assertEqual(self.request("/ingest", WRITE, {}, {"Content-Length": str(MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request("/ingest", WRITE, {}, {"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request("/ingest", WRITE, {}, {"Content-Length": "x"})[0], 400)
        self.assertEqual(self.request("/ingest", WRITE, {}, {"Transfer-Encoding": "chunked"})[0], 411)
        self.assertEqual(self.request("/ingest", WRITE, {}, {"Content-Type": "text/plain"})[0], 415)

    def test_missing_length(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.putrequest("POST", "/ingest")
        conn.putheader("Authorization", "Bearer " + WRITE)
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 411)
        response.read()
        conn.close()

    def test_read_only_routes_and_page(self):
        for path in ("/presets", "/api/presets", "/api/presets/save"):
            self.assertEqual(self.request(path)[0], 404)
            self.assertEqual(self.request(path, WRITE, {})[0], 404)
        page = self.request("/")[2].decode()
        self.assertIn("Switchboard stats", page)
        self.assertIn("<th>Machine</th>", page)
        self.assertIn("<section hidden", page)
        self.assertNotIn("$('nav').hidden = false", page)

    def test_file_only_state(self):
        snapshots = {"agy_usage": {"groups": [{"name": "Gemini Models", "buckets": [{"window": "weekly", "remaining_fraction": .5}]}]},
                     "codex_usage": {"fetched_at": 1, "result": {"rateLimits": {"secondary": {"windowDurationMins": 10080, "usedPercent": 30}}}},
                     "claude_usage": {"windows": [{"window": "weekly", "remaining_fraction": .2}]},
                     "pool_history": {"Gemini Models": [[1, .6, None]]}, "reservations": []}
        with ExitStack() as stack:
            for name in ("subprocess.run", "subprocess.Popen", "urllib.request.urlopen", "route.cloud_state", "route.local_state", "route.read_pool_history"):
                stack.enter_context(mock.patch(name, side_effect=AssertionError("live access")))
            for name in ("pc", "laptop"):
                self.store.ingest({"machine": name, "rows": [{"ts": 1}], "snapshots": snapshots})
            state = self.store.gather(now=1000)
            self.assertEqual(len(state["machines"]), 2)
            self.assertEqual(len(state["cloud"]["pools"]), 6)
            self.assertEqual({row["machine"] for row in state["journal"]}, {"pc", "laptop"})
            self.assertEqual(self.request()[0], 200)
            one = json.loads(self.request("/api/state?machine=pc")[2])
            self.assertEqual(len(one["machines"]), 1)
            self.assertAlmostEqual(one["cloud"]["pools"][1]["windows"][0]["remaining_fraction"], .7)

    def test_broken_snapshot_stale_and_partial_line(self):
        self.store.ingest({"machine": "pc"})
        self.store.ingest({"machine": "laptop"})
        folder = Path(self.tmp.name) / "machines/pc"
        (folder / "snapshots").mkdir(exist_ok=True)
        (folder / "snapshots/codex_usage.json").write_text("broken")
        (folder / "usage.jsonl").write_bytes(b'{"ts":1}\n{"ts":2}')
        atomic_json(folder / "meta.json", {"last_ingest": 1})
        state = self.store.gather(now=1000)
        pc = next(view for view in state["machines"] if view["machine"] == "pc")
        self.assertTrue(pc["stale"])
        self.assertTrue(pc["errors"])
        self.assertEqual(len(state["journal"]), 1)
        self.assertEqual(self.store.ingest({"machine": "pc", "rows": [{"ts": 3}]})["added"], 1)

    def test_cache_invalidated(self):
        before = self.store.get()
        self.assertIs(before, self.store.get())
        self.store.ingest({"machine": "pc"})
        self.assertEqual(len(self.store.get()["machines"]), 1)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        paths = {"CACHE_DIR": self.tmp.name, "JOURNAL_PATH": str(self.root / "usage.jsonl"),
                 "USAGE_CACHE": str(self.root / "agy.json"), "CODEX_USAGE_CACHE": str(self.root / "codex.json"),
                 "CLAUDE_USAGE_PATH": str(self.root / "claude.json"), "POOL_HISTORY": str(self.root / "history.json"),
                 "RESERVATIONS_PATH": str(self.root / "reservations.json")}
        self.stack.enter_context(mock.patch.multiple(route, **paths))
        self.token = self.root / "token"
        self.token.write_text(WRITE)
        self.config = {"url": "http://127.0.0.1:1", "token_file": str(self.token), "machine": "pc"}
        self.stack.enter_context(mock.patch.object(route, "load_machine", return_value={"stats": self.config}))
        self.journal = Path(route.JOURNAL_PATH)
        self.journal.write_bytes(b'{"ts":1,"log":"private"}\n')
        self.state = self.root / "stats_push.json"

    def test_failed_push_then_backlog(self):
        with mock.patch.object(stats_push, "post", side_effect=OSError()):
            self.assertEqual(stats_push.main(["--quiet"]), 1)
        self.assertFalse(self.state.exists())
        with mock.patch.object(stats_push, "post") as post:
            self.assertEqual(stats_push.main(["--quiet"]), 0)
            self.assertNotIn("log", post.call_args.args[2]["rows"][0])
        self.assertEqual(json.loads(self.state.read_text())["offset"], self.journal.stat().st_size)

    def test_dry_run(self):
        with mock.patch.object(stats_push, "post") as post, mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(stats_push.main(["--dry-run"]), 0)
            post.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_rotation_and_growth_fingerprint(self):
        with mock.patch.object(stats_push, "post") as post:
            stats_push.main(["--quiet"])
            with self.journal.open("ab") as handle:
                handle.write(b'{"ts":2}\n')
            stats_push.main(["--quiet"])
            self.assertEqual(post.call_args.args[2]["rows"], [{"ts": 2}])
            self.journal.write_bytes(b'{"ts":3}\n')
            stats_push.main(["--quiet"])
            self.assertEqual(post.call_args.args[2]["rows"], [{"ts": 3}])

    def test_partial_unicode_and_invalid_lines(self):
        data = '{"name":"模型"}\r\nbad\n{"ts":2}'.encode()
        self.journal.write_bytes(data)
        with mock.patch.object(stats_push, "post"):
            stats_push.main(["--quiet"])
        self.assertEqual(json.loads(self.state.read_text())["offset"], data.rfind(b"\n") + 1)

    def test_batch_acknowledgements(self):
        self.journal.write_bytes(b'{"ts":1}\n' * 2001)
        with mock.patch.object(stats_push, "post", side_effect=[None, OSError()]):
            self.assertEqual(stats_push.main(["--quiet"]), 1)
        self.assertEqual(json.loads(self.state.read_text())["offset"], len(b'{"ts":1}\n') * 2000)

    def test_large_rows_and_byte_limit(self):
        self.journal.write_text(json.dumps({"data": "x" * (stats_push.BATCH_BYTES + 1)}) + "\n" + json.dumps({"data": "x" * MAX_BODY}) + '\n{"ts":2}\n', encoding="utf-8")
        with mock.patch.object(stats_push, "post") as post:
            self.assertEqual(stats_push.main(["--quiet"]), 0)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(len(post.call_args_list[0].args[2]["rows"]), 1)
            self.assertEqual(post.call_args.args[2]["rows"], [{"ts": 2}])
        self.assertEqual(json.loads(self.state.read_text())["offset"], self.journal.stat().st_size)

    def test_growth_across_four_kilobytes(self):
        with mock.patch.object(stats_push, "post") as post:
            stats_push.main(["--quiet"])
            with self.journal.open("ab") as handle:
                handle.write((json.dumps({"data": "x" * 5000}) + "\n").encode())
            stats_push.main(["--quiet"])
            self.assertEqual(len(post.call_args.args[2]["rows"]), 1)
            self.assertEqual(json.loads(self.state.read_text())["head_len"], 4096)

    def test_busy_lock_marker(self):
        with mock.patch.object(stats_push, "acquire_lock", return_value=False), mock.patch.object(stats_push, "post") as post:
            self.assertEqual(stats_push.main(["--quiet"]), 0)
            post.assert_not_called()
        self.assertTrue((self.root / "stats_push.again").exists())

    def test_extra_pass(self):
        def send(*args):
            (self.root / "stats_push.again").touch()
        with mock.patch.object(stats_push, "push_once", side_effect=send) as push:
            stats_push.main(["--quiet"])
        self.assertEqual(push.call_count, 4)

    def test_token_errors_and_no_config(self):
        self.token.unlink()
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(stats_push.main([]), 2)
            self.token.write_text("  ")
            self.assertEqual(stats_push.main([]), 2)
        with mock.patch.object(route, "load_machine", return_value={}):
            self.assertEqual(stats_push.main([]), 0)

    def test_snapshots_throttle_and_change(self):
        atomic_json(route.CODEX_USAGE_CACHE, {"result": {}})
        with mock.patch.object(stats_push, "post") as post:
            stats_push.main(["--quiet", "--snapshots-only"])
            self.assertIn("codex_usage", post.call_args.args[2]["snapshots"])
            post.reset_mock()
            atomic_json(route.CODEX_USAGE_CACHE, {"result": {"new": 1}})
            stats_push.main(["--quiet", "--snapshots-only"])
            post.assert_not_called()
            stats_push.main(["--quiet"])
            self.assertIn("snapshots", post.call_args.args[2])

    def test_loopback_real_push(self):
        server = stats_server.make_server(str(self.root / "server"), READ, WRITE, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.config["url"] = f"http://127.0.0.1:{server.server_port}"
        try:
            self.assertEqual(stats_push.main(["--quiet"]), 0)
            self.assertEqual(len(server.store.get()["journal"]), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_no_redirects(self):
        self.assertIsNone(stats_push.NoRedirect().redirect_request(None, None, 302, "", {}, "http://127.0.0.1:1"))

    def test_notify_no_stats_fake_and_test_folder(self):
        with mock.patch.object(route, "_stats_machine", None), mock.patch.object(route, "resolve_fake", return_value=None), mock.patch.dict(os.environ, {"ASK_CACHE_DIR": ""}), mock.patch.object(route, "load_machine", return_value={}) as load, mock.patch.object(route.subprocess, "Popen") as popen:
            route.notify_stats()
            route.notify_stats()
            self.assertEqual(load.call_count, 1)
            popen.assert_not_called()
        with mock.patch.object(route, "resolve_fake", return_value={}), mock.patch.object(route.subprocess, "Popen") as popen:
            route.notify_stats()
            popen.assert_not_called()

    def test_notify_detached(self):
        with mock.patch.object(route, "_stats_machine", {"stats": {}}), mock.patch.object(route, "resolve_fake", return_value=None), mock.patch.dict(os.environ, {"ASK_CACHE_DIR": ""}), mock.patch.object(route.subprocess, "Popen") as popen:
            route.notify_stats()
            self.assertEqual(popen.call_args.args[0][-1], "--quiet")
            self.assertTrue(popen.call_args.kwargs["close_fds"])
            self.assertEqual(popen.call_args.kwargs["stdin"], route.subprocess.DEVNULL)
            if os.name == "nt":
                self.assertEqual(popen.call_args.kwargs["creationflags"], route.subprocess.CREATE_NO_WINDOW | route.subprocess.DETACHED_PROCESS)
            else:
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
        with mock.patch.dict(os.environ, {"ASK_CACHE_DIR": self.tmp.name}), mock.patch.object(route.subprocess, "Popen") as popen:
            route.notify_stats()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
