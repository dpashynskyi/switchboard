"""ask.py --tidy: which Codex threads are switchboard's and still in the app's list. Offline: no Codex runs."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import handoff
import route
import tidy

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
D = "44444444-4444-4444-4444-444444444444"


class TidyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "codex")
        self.room = os.path.join(self.tmp.name, "room")
        os.makedirs(self.room)
        patcher = mock.patch.object(handoff, "is_open", side_effect=lambda cid: cid == D)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rollout(self, thread, cwd, age=3600):
        path = Path(self.home, "sessions", "2026", "11", "16", f"rollout-2026-11-16T10-00-00-{thread}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread, "cwd": cwd}}) + "\n", encoding="utf-8")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def test_journal_and_room_threads_only_quiet_and_not_handed_off(self):
        self.rollout(A, "/some/repo")            # a writer run: in the journal
        self.rollout(B, self.room)               # /ask's room, even unjournalled
        self.rollout(C, "/user/own/work")        # the user's own thread: left alone
        self.rollout(D, self.room)               # open in a terminal handoff: left alone
        self.rollout("55555555-5555-5555-5555-555555555555", self.room, age=60)  # may still be running
        rows = [{"provider": "codex", "conversation": A}, {"provider": "agy", "conversation": C},
                {"provider": "codex", "conversation": C, "type": "handoff"}]
        found = tidy.find([self.home], tidy.journal_threads(rows), room=self.room)
        self.assertEqual(found, {self.home: [A, B]})

    def test_run_lists_without_yes_and_archives_with_it(self):
        self.rollout(B, self.room)
        account = {"id": "codex:~/.codex", "where": self.home, "active": True}
        adapter = mock.Mock(accounts=mock.Mock(return_value=[account]))
        import ask
        with mock.patch.object(tidy.providers, "get", return_value=adapter), \
             mock.patch.object(tidy.providers, "expand", side_effect=lambda p: p), \
             mock.patch.object(route, "_read_journal_rows", return_value=[]), \
             mock.patch.object(route, "CODEX_ROOM", self.room), \
             mock.patch.object(route, "codex_path", return_value="codex"), \
             mock.patch.object(ask, "codex_tidy", return_value={B: None}) as archive, \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            self.assertEqual(tidy.run(), 0)
            archive.assert_not_called()
            self.assertIn("--tidy --yes", out.getvalue())
            self.assertEqual(tidy.run(yes=True), 0)
            archive.assert_called_once_with("codex", [B], "thread/archive", self.home)
            self.assertIn("archived 1 of 1", out.getvalue())


if __name__ == "__main__":
    unittest.main()
