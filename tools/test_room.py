"""Offline room tests: members are a fake ask.py, no real agents or network."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import room  # noqa: E402

FAKE = textwrap.dedent('''
    import json, sys
    persona, prompt = sys.argv[1], sys.argv[-1]
    thread = "--thread" in sys.argv
    print("[conversation t-%s: carry on with --thread t-%s]" % (persona, persona), file=sys.stderr)
    if persona == "broken":
        print("quota spent", file=sys.stderr); sys.exit(6)
    print("[fake-%s, a pool]" % persona)
    if prompt.count("Vote on proposal"):
        vote = {"yes": "yes", "no": "no", "veto": "veto", "plain": None}.get(persona, "yes")
        if vote is None:
            print("Yes. It is fine.")
        else:
            print(json.dumps({"vote": vote, "why": persona + " says so"}))
        sys.exit(0)
    said = {"say": "%s here; thread %s; saw alpha: %s" % (persona, thread, "alpha here" in prompt)}
    if persona == "planner" and "plan" in prompt.lower():
        said["propose"] = {"kind": "plan", "body": "1. read 2. write"}
    if persona == "asker":
        said["ask_owner"] = "which folder?"
    if persona == "prose":
        print("Just prose, no JSON.")
    else:
        print(json.dumps(said))
''')


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="room-")
        self.addCleanup(self.temporary.cleanup)
        folder = Path(self.temporary.name)
        fake = folder / "fake_ask.py"
        fake.write_text(FAKE, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"ASK_ROOMS_DIR": str(folder / "rooms"),
                                               "ROOM_ASK_CMD": f'"{sys.executable}" -I "{fake}"'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_room(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = room.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def open_room(self, *members, rule="veto", extra=()):
        argv = ["new", "t", "--rule", rule, "--topic", "a test", *extra]
        for name in members:
            argv += ["--member", f"{name}={name}"]
        code, out, _ = self.run_room(*argv)
        self.assertEqual(code, 0)
        return out.strip()

    def kinds(self, path):
        return [e["kind"] for e in room.Room(path).entries()]

    def test_say_fans_out_and_carries_threads(self):
        path = self.open_room("alpha", "beta")
        code, out, _ = self.run_room("say", path, "hello")
        self.assertEqual(code, 0)
        self.assertIn("alpha here", out)
        self.assertIn("beta here", out)
        self.assertNotIn("fake-alpha", out)  # the header line is not the answer
        config = room.Room(path).config
        self.assertEqual([m["thread"] for m in config["members"]], ["t-alpha", "t-beta"])
        self.assertEqual(config["members"][0]["model"], "fake-alpha")
        _, out, _ = self.run_room("say", path, "again")
        self.assertIn("thread True", out)  # the second turn carries the conversation on

    def test_say_to_one_member(self):
        path = self.open_room("alpha", "beta")
        _, out, _ = self.run_room("say", path, "only you", "--to", "beta")
        self.assertIn("beta here", out)
        self.assertNotIn("alpha here", out)

    def test_round_in_order_later_member_sees_earlier(self):
        path = self.open_room("alpha", "beta")
        code, out, _ = self.run_room("round", path, "--note", "discuss")
        self.assertEqual(code, 0)
        lines = [l for l in out.splitlines() if " here;" in l]
        self.assertTrue(lines[0].startswith("[3] alpha"))
        self.assertIn("saw alpha: True", lines[1])

    def test_member_proposal_and_owner_question(self):
        path = self.open_room("planner", "asker")
        self.run_room("round", path, "--note", "make a plan")
        entries = room.Room(path).entries()
        proposal = [e for e in entries if e["kind"] == "proposal"][0]
        self.assertEqual((proposal["id"], proposal["by"], proposal["text"]), ("p1", "planner", "1. read 2. write"))
        self.assertIn("ask_owner", self.kinds(path))

    def test_prose_reply_is_kept_as_say(self):
        path = self.open_room("prose")
        _, out, _ = self.run_room("say", path, "hi")
        self.assertIn("prose: Just prose, no JSON.", out)
        self.assertFalse(room.Room(path).entries()[-1]["structured"])

    def test_failed_member_is_an_error_entry(self):
        path = self.open_room("broken", "alpha")
        code, out, _ = self.run_room("say", path, "hi")
        self.assertEqual(code, 0)
        self.assertIn("broken did not answer: exit 6: quota spent", out)

    def test_no_member_answered(self):
        path = self.open_room("broken")
        code, _, _ = self.run_room("say", path, "hi")
        self.assertEqual(code, 4)

    def propose(self, path):
        body = Path(self.temporary.name) / "plan.md"
        body.write_text("do it", encoding="utf-8")
        self.run_room("propose", path, "plan", "--file", str(body))
        return "p1"

    def result(self, path):
        return [e for e in room.Room(path).entries() if e["kind"] == "result"][-1]

    def test_veto_once_then_counted_as_no(self):
        path = self.open_room("yes", "veto", "plain")
        self.run_room("vote", path, self.propose(path))
        self.assertEqual(self.result(path)["outcome"], "vetoed")
        self.assertTrue(room.Room(path).member("veto")["veto_used"])
        self.run_room("vote", path, "p1")
        result = self.result(path)
        self.assertEqual(result["outcome"], "agreed")  # yes, plain yes, the spent veto is a no
        self.assertEqual(result["tally"], "2 yes, 1 no")

    def test_majority_ignores_veto(self):
        path = self.open_room("yes", "veto", "no", rule="majority")
        self.run_room("vote", path, self.propose(path))
        self.assertEqual(self.result(path)["outcome"], "rejected")

    def test_unanimous(self):
        path = self.open_room("yes", "no", rule="unanimous")
        self.run_room("vote", path, self.propose(path))
        self.assertEqual(self.result(path)["outcome"], "rejected")

    def test_arbiter_decides(self):
        path = self.open_room("yes", "no", rule="arbiter", extra=("--arbiter", "no"))
        self.run_room("vote", path, self.propose(path))
        self.assertEqual(self.result(path)["outcome"], "rejected")

    def test_round_limit_is_the_owners_call(self):
        path = self.open_room("alpha", extra=("--max-rounds", "1"))
        self.assertEqual(self.run_room("round", path)[0], 0)
        code, _, err = self.run_room("round", path)
        self.assertEqual(code, 3)
        self.assertIn("deadlock", err)
        self.run_room("say", path, "carry on")
        self.assertEqual(self.run_room("round", path)[0], 0)

    def test_secrets_are_redacted_in_the_transcript(self):
        path = self.open_room("alpha")
        self.run_room("note", path, "key sk-abcdefghijklmnopqrstuv and a@b.example")
        text = (Path(path) / "transcript.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("sk-abcdefghijklmnopqrstuv", text)
        self.assertNotIn("a@b.example", text)

    def test_new_refuses_bad_rooms(self):
        for argv in (["new", "Bad Slug", "--member", "a=a"], ["new", "x"], ["new", "x", "--member", "a=a",
                     "--member", "a=b"], ["new", "x", "--member", "a=a", "--rule", "arbiter"]):
            with self.assertRaises(SystemExit):
                self.run_room(*argv)

    def test_find_by_slug_and_list(self):
        path = self.open_room("alpha")
        self.assertEqual(str(room.Room.find("t").path), path)
        _, out, _ = self.run_room("list")
        self.assertIn("alpha", out)

    def test_parse_reply_fenced(self):
        reply, structured = room.parse_reply('Sure:\n```json\n{"say": "x"}\n```')
        self.assertEqual((reply, structured), ({"say": "x"}, True))


if __name__ == "__main__":
    unittest.main()
