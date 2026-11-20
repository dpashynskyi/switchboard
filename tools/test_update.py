"""Offline update.py tests: a bare origin and its clones in a temporary folder, real git, no network."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["ASK_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = tempfile.mkdtemp(prefix="ask-test-cache-")
os.environ.pop("SWITCHBOARD_AUTOUPDATE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import update  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


def commit(repo, files, msg):
    for rel, text in files.items():
        p = Path(repo, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg)


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sb-update-")
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(self.tmp, "init", "-q", "--bare", "-b", "main", self.origin)
        self.up = os.path.join(self.tmp, "upstream")
        sh(self.tmp, "clone", "-q", self.origin, self.up)
        sh(self.up, "checkout", "-q", "-b", "main")
        commit(self.up, {"a.txt": "one\n", "hooks/register.tsx": "v1\n", "profile/ask-roles.json": '{"v": 1}\n'}, "first")
        sh(self.up, "push", "-q", "origin", "main")
        self.me = os.path.join(self.tmp, "me")
        sh(self.tmp, "clone", "-q", self.origin, self.me)
        self.cfg = tempfile.mkdtemp(prefix="cfg-", dir=self.tmp)
        self.cache = tempfile.mkdtemp(prefix="cache-", dir=self.tmp)
        self.patches = [mock.patch.object(update, "ROOT", os.path.realpath(self.me)),
                        mock.patch.object(update, "CONFIG_DIR", self.cfg),
                        mock.patch.object(update, "CACHE_DIR", self.cache),
                        mock.patch.object(update, "STATE_PATH", os.path.join(self.cache, "update.json")),
                        mock.patch.object(update, "LOCK_PATH", os.path.join(self.cache, "update.lock")),
                        mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def push(self, files, msg):
        commit(self.up, files, msg)
        sh(self.up, "push", "-q", "origin", "main")

    def head(self, repo):
        return sh(repo, "rev-parse", "HEAD").strip()

    def mine(self, result):
        return next(r for r in result["repos"] if os.path.realpath(r["path"]) == os.path.realpath(self.me))

    def test_fast_forwards_main_and_says_what_came(self):
        self.push({"a.txt": "two\n"}, "a: two")
        res = update.run(force=True)
        r = self.mine(res)
        self.assertEqual(r["state"], "updated", r)
        self.assertEqual(r["subjects"], ["a: two"])
        self.assertFalse(r["needs_new_session"])
        self.assertEqual(self.head(self.me), self.head(self.up))
        self.assertEqual(Path(self.me, "a.txt").read_text(), "two\n")

    def test_current_when_nothing_new(self):
        self.assertEqual(self.mine(update.run(force=True))["state"], "current")

    def test_local_edits_elsewhere_stay_and_do_not_block(self):
        Path(self.me, "hooks/register.tsx").write_text("my edit\n")
        Path(self.me, "notes.txt").write_text("untracked\n")
        self.push({"a.txt": "two\n"}, "a: two")
        r = self.mine(update.run(force=True))
        self.assertEqual(r["state"], "updated", r)
        self.assertEqual(Path(self.me, "hooks/register.tsx").read_text(), "my edit\n")
        self.assertTrue(Path(self.me, "notes.txt").exists())

    def test_local_edit_in_a_changed_file_skips_and_keeps_it(self):
        Path(self.me, "a.txt").write_text("mine\n")
        before = self.head(self.me)
        self.push({"a.txt": "two\n"}, "a: two")
        r = self.mine(update.run(force=True))
        self.assertEqual(r["state"], "skipped")
        self.assertIn("a.txt", r["note"])
        self.assertEqual(self.head(self.me), before)
        self.assertEqual(Path(self.me, "a.txt").read_text(), "mine\n")

    def test_untracked_file_the_update_adds_skips(self):
        Path(self.me, "b.txt").write_text("mine\n")
        self.push({"b.txt": "theirs\n"}, "add b")
        r = self.mine(update.run(force=True))
        self.assertEqual(r["state"], "skipped")
        self.assertIn("b.txt", r["note"])
        self.assertEqual(Path(self.me, "b.txt").read_text(), "mine\n")

    def test_another_branch_is_left_alone(self):
        sh(self.me, "checkout", "-q", "-b", "feature")
        self.push({"a.txt": "two\n"}, "a: two")
        r = self.mine(update.run(force=True))
        self.assertEqual(r["state"], "skipped")
        self.assertIn("feature", r["note"])

    def test_local_commits_are_not_merged(self):
        commit(self.me, {"c.txt": "c\n"}, "mine")
        before = self.head(self.me)
        self.push({"a.txt": "two\n"}, "a: two")
        r = self.mine(update.run(force=True))
        self.assertEqual(r["state"], "skipped")
        self.assertIn("local commit", r["note"])
        self.assertEqual(self.head(self.me), before)

    def test_merge_in_progress_is_left_alone(self):
        git_dir = Path(self.me, ".git")
        (git_dir / "MERGE_HEAD").write_text(self.head(self.me) + "\n")
        self.push({"a.txt": "two\n"}, "a: two")
        r = self.mine(update.run(force=True))
        self.assertEqual(r["state"], "skipped")
        self.assertIn("merge", r["note"])

    def test_module_change_needs_a_new_session_unless_watched(self):
        self.push({"hooks/register.tsx": "v2\n"}, "band: v2")
        self.assertTrue(self.mine(update.run(force=True))["needs_new_session"])

    def test_throttle_and_off_switch(self):
        self.assertTrue(update.run(every=3600)["checked"])
        res = update.run(every=3600)
        self.assertEqual((res["checked"], res["reason"]), (False, "checked recently"))
        self.assertTrue(update.run(every=3600, now=time.time() + 3601)["checked"])
        with mock.patch.dict(os.environ, {"SWITCHBOARD_AUTOUPDATE": "0"}):
            self.assertEqual(update.run(every=0)["reason"], "off")
            self.assertTrue(update.run(every=0, force=True)["checked"])
        Path(self.cfg, "machine.json").write_text(json.dumps({"update": {"auto": False}}))
        self.assertEqual(update.run(every=0)["reason"], "off")

    def test_a_running_check_is_not_doubled(self):
        Path(update.LOCK_PATH).write_text("")
        self.assertEqual(update.run(force=True)["reason"], "another check is running")
        os.utime(update.LOCK_PATH, (time.time() - 3600, time.time() - 3600))
        self.assertTrue(update.run(force=True)["checked"])

    def test_dry_run_moves_nothing(self):
        before = self.head(self.me)
        self.push({"a.txt": "two\n"}, "a: two")
        r = self.mine(update.run(force=True, dry_run=True))
        self.assertEqual((r["state"], r["note"], r["subjects"]), ("skipped", "dry run", ["a: two"]))
        self.assertEqual(self.head(self.me), before)

    def test_repos_from_machine_json_and_a_roles_copy(self):
        # A second clone named in machine.json, and roles.json as a copy (Windows without links) of its profile file.
        other = os.path.join(self.tmp, "other")
        sh(self.tmp, "clone", "-q", self.origin, other)
        Path(self.cfg, "machine.json").write_text(json.dumps({"update": {"repos": [other]}}))
        Path(self.cfg, "roles.json").write_bytes(b'{"v": 1}\r\n')  # the old version, CRLF as a Windows copy has it
        self.push({"profile/ask-roles.json": '{"v": 2}\n'}, "roles: v2")
        res = update.run(force=True)
        self.assertEqual([r["state"] for r in res["repos"]], ["updated", "updated"])
        self.assertEqual(Path(self.cfg, "roles.json").read_text(), '{"v": 2}\n')
        self.assertEqual([c["state"] for c in res["copies"]], ["updated"])

    def test_an_edited_copy_is_left(self):
        Path(self.cfg, "roles.json").write_text('{"v": 1, "mine": true}\n')
        self.push({"profile/ask-roles.json": '{"v": 2}\n'}, "roles: v2")
        res = update.run(force=True)
        self.assertEqual(res["copies"], [])
        self.assertEqual(Path(self.cfg, "roles.json").read_text(), '{"v": 1, "mine": true}\n')

    def test_cli_prints_ascii_json(self):
        out = subprocess.run([sys.executable, "-B", str(Path(update.__file__)), "--dry-run", "--every", "0"],
                             capture_output=True, text=True, env={**os.environ, "SWITCHBOARD_AUTOUPDATE": "0"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["reason"], "off")
        self.assertTrue(out.stdout.isascii())


if __name__ == "__main__":
    unittest.main()
