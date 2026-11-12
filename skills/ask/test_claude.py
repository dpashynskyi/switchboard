"""ask.py --claude: a Claude subscription model called by name through a fake `claude -p`."""
import contextlib
import io
import json
import os
import tempfile as _tempfile

os.environ["ASK_ROLES"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_roles.json")
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ask
import route
from test_route import free_state

FAKE_CLAUDE = textwrap.dedent('''
    import json, sys
    args, question = sys.argv[1:], sys.stdin.read()
    model = args[args.index("--model") + 1]
    if "fail" in question:
        print(json.dumps({"type": "result", "is_error": True, "result": "API Error: overloaded", "session_id": "s-1"}))
        sys.exit(1)
    resumed = args[args.index("--resume") + 1] if "--resume" in args else None
    effort = args[args.index("--effort") + 1] if "--effort" in args else None
    tools = args[args.index("--tools") + 1] if "--tools" in args else "default"
    print(json.dumps({"type": "result", "is_error": False, "session_id": "s-1",
                      "usage": {"input_tokens": 3, "output_tokens": 4},
                      "result": f"{model} resumed={resumed} effort={effort} tools={tools!r} q={question}"}))
''')


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        for key, name in {"CACHE_DIR": "", "LOG_DIR": "logs", "JOURNAL_PATH": "usage.jsonl",
                          "RESERVATIONS_PATH": "reservations.json", "RESERVATIONS_LOCK": "reservations.lock"}.items():
            self.stack.enter_context(mock.patch.object(route, key, os.path.join(self.tmp, name)))
        fake = Path(self.tmp) / "fake_claude.py"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "ASK_ROUTE_FAKE": json.dumps(free_state()), "NO_COLOR": "1", "ASK_CONTEXT": "personal",
            "ASK_CLAUDE_CMD": f'"{sys.executable}" -I "{fake}"'}))

    def run_main(self, args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["ask.py"] + args), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = ask.main()
        return code, out.getvalue(), err.getvalue()

    def test_named_model_is_called_and_carries_on(self):
        code, out, err = self.run_main(["--claude", "opus", "hello"])
        self.assertEqual(code, 0, err)
        self.assertIn("[claude-opus-5-5,", out)
        self.assertIn("claude-opus-5-5 resumed=None effort=None tools='' q=hello", out)
        self.assertIn("[conversation s-1: carry on with --thread s-1]", err)
        code, out, err = self.run_main(["--claude", "claude-haiku-5-5", "--thread", "s-1", "--effort", "low", "again"])
        self.assertEqual(code, 0, err)
        self.assertIn("claude-haiku-5-5 resumed=s-1 effort=low", out)
        rows = [json.loads(line) for line in Path(route.JOURNAL_PATH).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r.get("status") for r in rows[-2:]], ["ok", "ok"])

    def test_failure_says_why(self):
        code, out, err = self.run_main(["--claude", "sonnet", "please fail"])
        self.assertNotEqual(code, 0)
        self.assertIn("API Error: overloaded", err)
        self.assertNotIn("[claude-sonnet-5-5,", out)

    def test_bare_name_still_hands_back_to_a_subagent(self):
        code, _, err = self.run_main(["claude-opus-5-5", "hi"])
        self.assertEqual(code, 6)
        self.assertIn("use a Claude subagent (claude-opus-5-5)", err)

    def test_aliases(self):
        ids = ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
        rules = {"subscription_models": ids}
        self.assertEqual(ask.claude_model_id("Fable", rules), "claude-fable-5-2")
        self.assertEqual(ask.claude_model_id("claude-opus-5-5", rules), "claude-opus-5-5")
        self.assertEqual(ask.claude_model_id("claude-opus-4-8", rules), "claude-opus-4-8")
        self.assertEqual(ask.claude_model_id("claude", rules), "claude")


if __name__ == "__main__":
    unittest.main()
