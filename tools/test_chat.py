"""Offline room chat tests using fake members and in-process input."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_room import FAKE  # noqa: E402
import chat  # noqa: E402
import room  # noqa: E402


class ChatTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chat-")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        fake = folder / "fake.py"
        fake.write_text(FAKE, encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"ASK_ROOMS_DIR": str(folder / "rooms"),
                                               "ROOM_ASK_CMD": f'"{sys.executable}" -I "{fake}"'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, slug="test", **kwargs):
        return room.open_room(slug, ["alpha=alpha", "beta=beta"], **kwargs)

    def run_chat(self, current, lines, colors=None):
        out = io.StringIO()
        self.assertEqual(chat.run(current, lines, out, colors, 5), 0)
        return out.getvalue()

    def main_chat(self, args, text=""):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = chat.main(args, stdin=io.StringIO(text), out=out)
        return code, out.getvalue(), err.getvalue()

    def test_plain_line(self):
        current = self.create()
        out = self.run_chat(current, ["hello", "/quit"])
        entries = current.entries()
        self.assertEqual([e["kind"] for e in entries], ["open", "owner", "say", "say"])
        self.assertEqual(entries[1]["text"], "hello")
        for name in ("alpha", "beta"):
            self.assertIn(name + " here", out)
        self.assertIn("… waiting for alpha, beta", out)
        self.assertNotIn("\033", out)

    def test_owner_text_preserves_spaces(self):
        current = self.create()
        self.run_chat(current, ["  hello  \n"])
        self.assertEqual(current.entries()[1]["text"], "  hello  ")

    def test_claude_and_add_members_mid_chat(self):
        current = self.create()
        with mock.patch.object(room, "claude_models", return_value=["claude-fable-5-2", "claude-opus-5-5"]):
            out = self.run_chat(current, ["/claude", "/claude opus", "/claude fable deep", "/claude opus",
                                          "/add gamma=gamma", "/add bad", "@gamma hi", "/members", "/quit"])
        self.assertIn("Claude models: claude-fable-5-2, claude-opus-5-5", out)
        self.assertIn("opus joined the room (--claude claude-opus-5-5)", out)
        self.assertIn("deep joined the room (--claude claude-fable-5-2)", out)
        self.assertIn("the room already has a member opus", out)
        self.assertIn("Usage: /add NAME=ARGS", out)
        self.assertIn("gamma here", out)
        names = [m["name"] for m in room.Room(current.path).config["members"]]
        self.assertEqual(names, ["alpha", "beta", "opus", "deep", "gamma"])
        self.assertEqual(room.claude_name("claude-fable-5-2"), "fable")

    def test_target_and_unknown_member(self):
        current = self.create()
        self.run_chat(current, ["@beta hello"])
        self.assertEqual(current.entries()[1]["to"], "beta")
        self.assertEqual(current.entries()[2]["by"], "beta")
        before = current.entries()
        out = self.run_chat(current, ["@nobody", " ", "", "@beta", "@beta   "])
        self.assertIn("No member 'nobody'; members: alpha, beta", out)
        self.assertEqual(out.count("Write the message after @beta."), 2)
        self.assertEqual(current.entries(), before)

    def test_round_order_limit_and_owner_reset(self):
        current = self.create(max_rounds=1)
        out = self.run_chat(current, ["/round discuss", "/round blocked"])
        self.assertIn("1 rounds since the owner last spoke: a deadlock", out)
        entries = current.entries()
        self.assertEqual(entries[1]["kind"], "round")
        self.assertEqual(entries[1]["text"], "discuss")
        self.assertEqual([e["by"] for e in entries[2:]], ["alpha", "beta"])
        self.assertIn("saw alpha: True", entries[-1]["text"])
        self.run_chat(current, ["continue", "/round"])
        self.assertEqual(current.rounds_since_owner(), 1)

    def test_commands(self):
        current = self.create()
        other = self.create("another")
        current.member("alpha")["veto_used"] = True
        current.append({"kind": "note", "text": "older"})
        current.append({"kind": "note", "text": "recent"})
        current.append({"kind": "note", "text": "latest"})
        out = self.run_chat(current, ["/tail 2", "/members", "/rooms", "/help", "/x", "/exit", "ignored"])
        self.assertIn("recent", out)
        self.assertIn("latest", out)
        self.assertNotIn("older", out)
        self.assertIn("alpha  alpha (veto used)", out)
        self.assertIn("* " + current.path.name, out)
        self.assertIn("  " + other.path.name, out)
        for command in chat.HELP:
            self.assertIn(command, out)
        self.assertIn("Unknown command /x; /help lists them.", out)
        self.assertEqual(len(current.entries()), 4)

    def test_colors_and_rendering(self):
        current = self.create()
        colors = {"alpha": 36, "beta": 33}
        out = self.run_chat(current, ["hello", "@alpha again"], colors)
        self.assertEqual(out.count("\033[1;36malpha\033[0m"), 2)
        self.assertIn("\033[1;33mbeta\033[0m", out)
        self.assertIn("fake-alpha", out)
        self.assertIn(" s", out)
        old = {"kind": "say", "by": "alpha", "text": "Привіт світе " * 20 + "\n\nкінець"}
        with mock.patch.object(chat.shutil, "get_terminal_size", return_value=os.terminal_size((30, 24))):
            rendered = chat.show(old, None)
        self.assertIn("Привіт", rendered)
        self.assertIn("\n  \n", rendered)
        self.assertTrue(all(len(line) <= 30 for line in rendered.splitlines()))
        self.assertIn("you → beta", chat.show({"kind": "owner", "to": "beta", "text": "hello"}, colors))
        self.assertIn("alpha asks you: why", chat.show({"kind": "ask_owner", "by": "alpha", "text": "why"}, colors))
        self.assertIn("alpha did not answer", chat.show({"kind": "error", "by": "alpha"}, None))

    def test_main_usage_and_no_rooms(self):
        code, out, _ = self.main_chat([])
        self.assertEqual(code, 2)
        self.assertIn('chat.py --new <slug> --member "codex=--role review"', out)
        self.assertEqual(self.main_chat(["test", "--new", "another"])[0], 2)
        code, _, err = self.main_chat(["missing"])
        self.assertEqual(code, 2)
        self.assertIn("No room", err)
        self.assertEqual(self.main_chat(["--new", "bad slug", "--member", "alpha=alpha"])[0], 2)
        self.assertEqual(self.main_chat(["--new", "test", "--member", "bad"])[0], 2)

    def test_main_new_and_newest(self):
        code, out, _ = self.main_chat(["--new", "test", "--member", "alpha=alpha", "--topic", "discussion"], "hello\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Topic: discussion", out)
        self.assertIn("Rule: veto", out)
        self.assertIn("alpha here", out)
        current = self.create("zz-newest")
        code, out, _ = self.main_chat([])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(current.path.name))
        self.assertEqual(self.main_chat(["--new", "test", "--member", "alpha=alpha"])[0], 2)

    def test_main_color_decision(self):
        self.create()
        with mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertIn("\033", self.main_chat([])[1])
            self.assertNotIn("\033", self.main_chat(["--no-color"])[1])
        with mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertNotIn("\033", self.main_chat([])[1])
        with mock.patch.object(sys.stdout, "isatty", return_value=False):
            self.assertNotIn("\033", self.main_chat([])[1])

    def test_interrupt_returns_to_input(self):
        current = self.create()
        def interrupt(*args, **kwargs):
            kwargs["on_interrupt"]()
            raise KeyboardInterrupt
        with mock.patch.object(room, "parallel_turns", side_effect=interrupt):
            out = self.run_chat(current, ["hello", "/help", "/quit"])
        self.assertIn(chat.INTERRUPTED, out)
        self.assertIn("/help  list commands", out)

    def test_prompt_interrupt_and_eof(self):
        with mock.patch("builtins.input", side_effect=[KeyboardInterrupt, "hello", EOFError]) as prompt, mock.patch("builtins.print"):
            self.assertEqual(list(chat.prompt_lines()), ["hello"])
        prompt.assert_called_with("you> ")


    def test_slow_member_sees_the_faster_answer_next_turn(self):
        current = self.create()
        current.append({"kind": "owner", "text": "hi"})
        # Both turns read the transcript up to the owner's line; alpha ends first, beta after.
        room.record_turn(current, "alpha", '{"say": "alpha here"}', "t-alpha", None, "", 1.0, 2)
        room.record_turn(current, "beta", '{"say": "beta here"}', "t-beta", None, "", 2.0, 2)
        beta = room.brief(current, current.member("beta"), "next")
        alpha = room.brief(current, current.member("alpha"), "next")
        self.assertIn("alpha here", beta)
        self.assertNotIn("beta here", beta)
        self.assertIn("beta here", alpha)
        self.assertNotIn("alpha here", alpha)

    def test_wrap_keeps_indent_and_long_words(self):
        url = "https://example.com/" + "a" * 150
        item = {"kind": "say", "by": "alpha", "text": "    indented code\n" + url + " tail"}
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((40, 20))):
            shown = chat.show(item, None).split("\n")
        self.assertIn("      indented code", shown)
        self.assertIn("  " + url, shown)

    def test_default_room_is_the_one_last_talked_in(self):
        older = self.create("zeta")
        newer = self.create("alpha")
        os.utime(newer.transcript_path, (1, 1))
        code, out, _ = self.main_chat([], "/quit\n")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], older.path.name)


if __name__ == '__main__':
    unittest.main()
