"""Offline checks of preset editing: the edits, the file they write, ask.py --preset edit, and the dashboard's guarded
writes."""
import copy
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from test_route import free_state
import ask
import dashboard
import preset_edit
import route
import score

HERE = Path(__file__).resolve().parent
DEFAULTS = str(HERE / "test_roles.json")
CONTEXT = {"context": "personal", "source": "test"}


class EditorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="preset-edit-"))
        self.fake = free_state()
        self.fake["machine"] = {}
        self.fake["codex_models"] = [{"id": "gpt-6.1-sol"}]
        patch = mock.patch.dict(os.environ, ASK_PRESET="", ASK_CONTEXT="personal")
        patch.start()
        self.addCleanup(patch.stop)
        self.user = self.tmp / "roles.json"
        self.editor = preset_edit.Editor(user_path=str(self.user), defaults_path=DEFAULTS, fake=self.fake, context=CONTEXT)
        with open(DEFAULTS, encoding="utf-8") as f:
            self.defaults = json.load(f)

    def rows(self, role):
        return copy.deepcopy(route.role_rows(self.defaults["roles"][role]))


class EditTests(EditorCase):
    def test_reorder_off_and_back_on_leaves_the_file_as_shipped(self):
        rows = self.rows("draft")
        self.editor.save([{"role": "draft", "rows": [rows[1], rows[0]] + rows[2:]}])
        saved = json.loads(self.user.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["draft"]["prefer"][:2], [rows[1], rows[0]])
        # The role's other settings come along unchanged (the user's role replaces the shipped one whole).
        self.assertEqual(saved["roles"]["draft"]["score"], self.defaults["roles"]["draft"]["score"])
        self.editor.save([{"role": "draft", "rows": rows[1:], "off": [rows[0]]}])
        saved = json.loads(self.user.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["draft"]["off"], [rows[0]])
        merged = route.merge_roles(self.defaults, saved)
        self.assertEqual(route.role_rows(merged["roles"]["draft"]), rows[1:])
        self.editor.save([{"role": "draft", "rows": rows, "off": []}])
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8")), {})

    def test_switched_off_row_is_not_called(self):
        rows = self.rows("draft")
        before = route.pick("draft", rules=self.defaults, fake=self.fake, context=CONTEXT)
        self.editor.save([{"role": "draft", "rows": rows[1:], "off": rows[:1]}])
        rules = route.merge_roles(self.defaults, json.loads(self.user.read_text(encoding="utf-8")))
        after = route.pick("draft", rules=rules, fake=self.fake, context=CONTEXT)
        self.assertNotEqual(before["model"], after["model"])

    def test_refuses_dropped_foreign_and_empty_rows(self):
        rows = self.rows("review")
        with self.assertRaisesRegex(preset_edit.EditError, "went missing"):
            self.editor.save([{"role": "review", "rows": rows[1:]}])
        with self.assertRaisesRegex(preset_edit.EditError, "only a model of this machine"):
            self.editor.save([{"role": "review", "rows": rows + [{"provider": "agy", "model": "not-here"}]}])
        with self.assertRaisesRegex(preset_edit.EditError, "only a model of this machine"):
            self.editor.save([{"role": "review", "rows": rows + [{"family": "*"}]}])
        with self.assertRaisesRegex(preset_edit.EditError, "at least one row"):
            self.editor.save([{"role": "review", "rows": [], "off": rows}])
        with self.assertRaisesRegex(preset_edit.EditError, "unknown role"):
            self.editor.save([{"role": "nobody", "rows": rows}])
        self.assertFalse(self.user.exists())

    def test_adds_a_model_of_this_machine(self):
        model = {"provider": "ollama", "model": "gpt-oss:20b"}
        self.assertIn(model, [{k: m[k] for k in ("provider", "model")} for m in self.editor.state(with_picks=False)["models"]])
        self.editor.save([{"role": "review", "rows": self.rows("review") + [model]}])
        self.assertEqual(json.loads(self.user.read_text(encoding="utf-8"))["roles"]["review"]["prefer"][-1], model)

    def test_weights_validated_and_quality_counts(self):
        for bad in ({"time": -1}, {"cost": float("nan")}, {"time": True}, {"speed": 1}, {"cost": 9}):
            with self.subTest(bad=bad), self.assertRaises(preset_edit.EditError):
                self.editor.save([{"preset": "thrifty", "weights": bad}])
        with self.assertRaisesRegex(preset_edit.EditError, "no weights"):
            self.editor.save([{"preset": "manual", "weights": {"time": 1}}])
        self.editor.save([{"preset": "thrifty", "weights": {"quality": 2, "time": 0.25}}])
        saved = json.loads(self.user.read_text(encoding="utf-8"))
        self.assertEqual(saved["presets"]["thrifty"], {"time": 0.25, "cost": 1.5, "quality": 2.0})
        self.assertEqual(route.merge_roles(self.defaults, saved)["presets"]["fast"], self.defaults["presets"]["fast"])
        with mock.patch.object(score, "quality", return_value=.5), \
                mock.patch.object(score, "expected_seconds", return_value=10), \
                mock.patch.object(score, "cost_share", return_value=0):
            rules = copy.deepcopy(self.defaults)
            members = [{"ok": True, "provider": "codex", "model": "gpt-6.1-sol"}]
            one = score.rank("review", members, rules, [], {}, {}, weights={"quality": 1})[0][0]["score"]["S"]
            two = score.rank("review", members, rules, [], {}, {}, weights={"quality": 2})[0][0]["score"]["S"]
            self.assertAlmostEqual(two - one, .5)

    def test_preview_writes_nothing_and_shows_the_draft(self):
        rows = self.rows("draft")
        picks = self.editor.preview([{"role": "draft", "rows": rows[1:], "off": rows[:1]}])["picks"]
        self.assertNotEqual(picks["draft"]["picks"]["manual"], self.editor.state()["picks"]["draft"]["picks"]["manual"])
        self.assertFalse(self.user.exists())

    def test_link_kept_backup_made_and_conflict_refused(self):
        real = self.tmp / "profile" / "ask-roles.json"
        real.parent.mkdir()
        real.write_text('{\n  "_comment": "mine"\n}\n', encoding="utf-8")
        try:
            self.user.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        version = self.editor.read()[2]
        self.editor.save([{"preset": "fast", "weights": {"time": 2}}], version=version)
        self.assertTrue(self.user.is_symlink())
        self.assertEqual(json.loads(real.read_text(encoding="utf-8"))["_comment"], "mine")
        self.assertTrue(real.read_text(encoding="utf-8").endswith("}\n"))
        self.assertEqual(Path(str(self.user) + ".bak").read_text(encoding="utf-8"), '{\n  "_comment": "mine"\n}\n')
        with self.assertRaises(preset_edit.Conflict):
            self.editor.save([{"preset": "fast", "weights": {"time": 1}}], version=version)


class CommandLineTests(EditorCase):
    def run_cli(self, *argv):
        lines = []
        code = preset_edit.main(list(argv), out=lines.append, editor=self.editor)
        return code, "\n".join(lines)

    def test_show_move_off_on_add(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("thrifty: quality 1, time 0.2, cost 1.5", text)
        self.assertFalse(self.user.exists())
        rows = self.rows("draft")
        self.assertEqual(self.run_cli("manual", "--role", "draft", "--move", "2", "1")[0], 0)
        self.assertEqual(route.role_rows(json.loads(self.user.read_text())["roles"]["draft"])[:2], [rows[1], rows[0]])
        code, text = self.run_cli("manual", "--role", "draft", "--off", "1")
        self.assertEqual(code, 0)
        self.assertIn("(off)", text)
        self.assertIn("Would pick now:", text)
        n = len(rows)
        self.assertEqual(self.run_cli("manual", "--role", "draft", "--on", str(n))[0], 0)
        self.assertEqual(self.run_cli("manual", "--role", "draft", "--add", "ollama:gpt-oss:20b")[0], 0)
        self.assertEqual(json.loads(self.user.read_text())["roles"]["draft"]["prefer"][-1],
                         {"provider": "ollama", "model": "gpt-oss:20b"})

    def test_refusals_write_nothing(self):
        for argv in (("manual", "--role", "draft", "--move", "9", "1"), ("manual", "--role", "draft", "--on", "1"),
                     ("manual", "--time", "1"), ("thrifty", "--role", "draft", "--off", "1"),
                     ("thrifty", "--cost", "-2"), ("absent",), ("manual", "--role", "draft", "--add", "agy")):
            with self.subTest(argv=argv):
                code, text = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("Nothing written", text)
        self.assertFalse(self.user.exists())

    def test_weights(self):
        code, text = self.run_cli("fast", "--quality", "1.5", "--cost", "0.4")
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(self.user.read_text())["presets"]["fast"], {"time": 1.5, "cost": 0.4, "quality": 1.5})

    def test_ask_hands_preset_edit_over(self):
        for argv in (["ask.py", "--preset", "edit", "fast"], ["ask.py", "--preset=edit", "fast"]):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(preset_edit, "main", return_value=0) as main:
                self.assertEqual(ask.main(), 0)
                main.assert_called_once_with(["fast"])


class ServerTests(EditorCase):
    def setUp(self):
        super().setUp()
        self.server = dashboard.make_server(0, store=mock.Mock(get=lambda: {}), editor=self.editor, token="secret-token")
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=data, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def write_headers(self, **over):
        headers = {"Origin": self.origin, "X-Switchboard-Token": "secret-token", "Content-Type": "application/json"}
        headers.update(over)
        return {k: v for k, v in headers.items() if v is not None}

    def test_page_and_state(self):
        status, body = self.request("GET", "/presets")
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret-token", body)
        status, body = self.request("GET", "/api/presets")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual([p["name"] for p in state["presets"]], ["manual", "thrifty", "strong", "fast"])
        self.assertIn("draft", state["picks"])

    def test_writes_are_guarded(self):
        edit = {"edits": [{"preset": "fast", "weights": {"time": 2}}]}
        for name, headers, code in (
                ("no token", self.write_headers(**{"X-Switchboard-Token": None}), 403),
                ("wrong token", self.write_headers(**{"X-Switchboard-Token": "guess"}), 403),
                ("no origin", self.write_headers(Origin=None), 403),
                ("other site", self.write_headers(Origin="http://evil.example"), 403),
                ("other port", self.write_headers(Origin="http://127.0.0.1:1"), 403),
                ("rebound name", self.write_headers(Host="evil.example"), 403),
                ("form post", self.write_headers(**{"Content-Type": "application/x-www-form-urlencoded"}), 415)):
            with self.subTest(name):
                self.assertEqual(self.request("POST", "/api/presets/save", edit, headers)[0], code)
        self.assertFalse(self.user.exists())
        status, body = self.request("POST", "/api/presets/save", edit, self.write_headers())
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertTrue(result["written"])
        self.assertEqual(result["state"]["presets"][3]["weights"]["time"], 2)
        self.assertEqual(json.loads(self.user.read_text())["presets"]["fast"]["time"], 2)

    def test_preview_bad_edit_and_conflict(self):
        rows = self.rows("draft")
        status, body = self.request("POST", "/api/presets/preview",
                                    {"edits": [{"role": "draft", "rows": rows[1:], "off": rows[:1]}]}, self.write_headers())
        self.assertEqual(status, 200, body)
        self.assertIn("draft", json.loads(body)["picks"])
        status, _ = self.request("POST", "/api/presets/save", {"edits": [{"role": "draft", "rows": []}]}, self.write_headers())
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/presets/save",
                                 {"edits": [{"preset": "fast", "weights": {"time": 2}}], "version": "stale"},
                                 self.write_headers())
        self.assertEqual(status, 409)
        self.assertFalse(self.user.exists())


if __name__ == "__main__":
    unittest.main()
