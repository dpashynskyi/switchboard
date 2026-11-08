"""Offline cloud.py tests: claude is a fake script, the routine API a mocked urlopen; no network, no account."""
import contextlib
import io
import json
import os
import tempfile as _tempfile

os.environ["ASK_ROLES"] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "ask",
                                       "test_roles.json")
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
from pathlib import Path  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import textwrap  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cloud  # noqa: E402

FAKE = textwrap.dedent('''
    import json, os, sys
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as h:
        h.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    if mode == "fail":
        print("Not logged in", file=sys.stderr); sys.exit(1)
    if "-p" in sys.argv:
        sid = sys.argv[sys.argv.index("--cloud") + 1]
        print(json.dumps({"ok": True, "session_id": sid, "url": "https://claude.ai/code/" + sid}))
    else:
        print("Created cloud session\\nView: https://claude.ai/code/session_01AbCdEfGh123")
''')

POOLS = {"pools": [{"provider": "claude-subscription", "label": "Claude (max) · weekly", "stale": False,
                    "windows": [{"window": "weekly", "remaining_fraction": 0.05, "available_fraction": 0.05}]}]}


class CloudTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="cloud-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        fake = self.dir / "fake_claude.py"
        fake.write_text(FAKE, encoding="utf-8")
        self.log = self.dir / "calls.jsonl"
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.brief = self.dir / "BRIEF.md"
        self.brief.write_text("Fix the export button.\n", encoding="utf-8")
        env = {"CLOUD_CLAUDE_CMD": f'"{sys.executable}" -I "{fake}"', "FAKE_CLAUDE_LOG": str(self.log),
               "ASK_CLOUD_DIR": str(self.dir / "state"), "ASK_CONTEXT": "personal"}
        for patcher in (mock.patch.dict(os.environ, env),
                        mock.patch.object(cloud.route, "cloud_state", return_value={"pools": []}),
                        mock.patch.object(cloud.route, "journal")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cloud(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cloud.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_start_sends_the_brief_whole_and_records_the_session(self):
        code, out, err = self.run_cloud("start", "--cwd", str(self.repo), "--brief", str(self.brief), "and test it")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://claude.ai/code/session_01AbCdEfGh123")
        (call,) = self.calls()
        self.assertEqual(call["argv"][0], "--cloud")
        self.assertIn("Fix the export button.", call["argv"][1])
        self.assertIn("never push to or merge into the default branch", call["argv"][1])
        self.assertTrue(call["argv"][1].endswith("and test it"))
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("not a git repository", err)
        (row,) = cloud.load_sessions()
        self.assertEqual((row["id"], row["via"], row["context"]), ("session_01AbCdEfGh123", "cli", "personal"))
        status = cloud.route.journal.call_args
        self.assertEqual((status.args[3], status.kwargs["status"], status.kwargs["conversation"]),
                         ("claude-subscription", "ok", "session_01AbCdEfGh123"))

    def test_send_follows_up_through_print_mode(self):
        self.run_cloud("start", "--cwd", str(self.repo), "--brief", str(self.brief))
        code, out, err = self.run_cloud("send", "session_01AbCdEfGh123", "also update the docs")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[-1]["argv"],
                         ["-p", "also update the docs", "--cloud", "session_01AbCdEfGh123", "--output-format", "json"])
        self.assertEqual(cloud.load_sessions()[0]["messages"], 2)
        self.assertIn("session_01AbCdEfGh123", out)

    def test_a_failed_cli_is_reported_and_journalled(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_MODE": "fail"}):
            code, out, err = self.run_cloud("start", "--cwd", str(self.repo), "--brief", str(self.brief))
        self.assertEqual(code, cloud.EXIT_FAIL)
        self.assertIn("Not logged in", err)
        self.assertEqual(cloud.load_sessions(), [])
        self.assertEqual(cloud.route.journal.call_args.kwargs["status"], "error")

    def test_a_window_under_its_floor_refuses_unless_forced(self):
        with mock.patch.object(cloud.route, "cloud_state", return_value=POOLS):
            code, _, err = self.run_cloud("start", "--cwd", str(self.repo), "--brief", str(self.brief))
            self.assertEqual(code, cloud.EXIT_QUOTA)
            self.assertIn("under the floor", err)
            self.assertEqual(self.calls(), [])
            code, _, err = self.run_cloud("start", "--cwd", str(self.repo), "--brief", str(self.brief), "--force")
        self.assertEqual(code, 0, err)

    def test_the_context_policy_refuses(self):
        refused = {"allowed": False, "reason": "work account, personal context", "account": "claude-subscription"}
        with mock.patch.object(cloud.route, "policy_for", return_value=refused):
            code, _, err = self.run_cloud("start", "--cwd", str(self.repo), "--brief", str(self.brief))
        self.assertEqual(code, cloud.route.EXIT_POLICY)
        self.assertIn("work account", err)
        self.assertEqual(self.calls(), [])

    def test_git_notes_for_what_the_clone_will_not_see(self):
        git = ["git", "-C", str(self.repo)]
        subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        notes, branch = cloud.left_behind(str(self.repo))
        self.assertEqual(branch, "main")
        self.assertTrue(any("uncommitted" in n for n in notes))
        self.assertTrue(any("no upstream" in n for n in notes))

    def test_routine_fire_posts_the_prompt_with_the_token_from_the_environment(self):
        seen = {}

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(req, timeout):
            seen.update(url=req.full_url, auth=req.get_header("Authorization"), body=json.loads(req.data))
            return Reply(json.dumps({"type": "routine_fire", "claude_code_session_id": "session_01RoutineXyz",
                                     "claude_code_session_url": "https://claude.ai/code/session_01RoutineXyz"}).encode())

        with mock.patch.dict(os.environ, {"CLAUDE_ROUTINE_TOKEN": "sk-ant-oat01-test"}), \
                mock.patch.object(cloud.urllib.request, "urlopen", urlopen):
            code, out, err = self.run_cloud("start", "--routine", "trig_01abc", "--cwd", str(self.repo),
                                            "--brief", str(self.brief))
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["url"], "https://api.anthropic.com/v1/claude_code/routines/trig_01abc/fire")
        self.assertEqual(seen["auth"], "Bearer sk-ant-oat01-test")
        self.assertIn("Fix the export button.", seen["body"]["text"])
        self.assertEqual(out.strip(), "https://claude.ai/code/session_01RoutineXyz")
        self.assertEqual(self.calls(), [])
        self.assertNotIn("sk-ant", json.dumps(cloud.load_sessions()))

    def test_routine_needs_its_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_ROUTINE_TOKEN", None)
            code, _, err = self.run_cloud("start", "--routine", "trig_01abc", "--brief", str(self.brief),
                                          "--cwd", str(self.repo))
        self.assertEqual(code, cloud.EXIT_FAIL)
        self.assertIn("CLAUDE_ROUTINE_TOKEN is not set", err)

    def test_parse_session(self):
        self.assertEqual(cloud.parse_session('{"ok": true, "session_id": "session_01X", "url": "u"}'),
                         ("session_01X", "u"))
        self.assertEqual(cloud.parse_session("see https://claude.ai/code/session_01Abcdefgh"),
                         ("session_01Abcdefgh", "https://claude.ai/code/session_01Abcdefgh"))
        self.assertEqual(cloud.parse_session("nothing here"), (None, None))


if __name__ == "__main__":
    unittest.main()
