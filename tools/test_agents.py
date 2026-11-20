"""Offline checks of the plugin's thin subagents (agents/*.md): Haiku, Bash only, and every script and flag they tell the
model to use still exists, so a renamed flag fails here rather than in a session."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = sorted((ROOT / "agents").glob("*.md"))
SCRIPTS = {"skills/ask/ask.py": None, "tools/writer.py": None}


def front(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, f"{path.name}: no frontmatter"
    meta = dict(line.split(": ", 1) for line in m.group(1).splitlines())
    return meta, text[m.end():]


def help_text(script):
    if SCRIPTS[script] is None:
        SCRIPTS[script] = subprocess.run([sys.executable, "-B", str(ROOT / script), "--help"], capture_output=True,
                                         text=True, timeout=60).stdout
    return SCRIPTS[script]


class AgentsTest(unittest.TestCase):
    def test_there_are_agents(self):
        self.assertGreaterEqual(len(AGENTS), 3)

    def test_frontmatter(self):
        for path in AGENTS:
            meta, _ = front(path)
            self.assertEqual(meta.get("name"), path.stem, path.name)
            self.assertTrue(meta.get("description"), path.name)
            self.assertEqual(meta.get("model"), "haiku", path.name)
            self.assertEqual(meta.get("tools"), "Bash", path.name)

    def test_scripts_and_flags_exist(self):
        for path in AGENTS:
            _, body = front(path)
            for script in SCRIPTS:
                for call in re.findall(re.escape(script) + r'"? ([^`]*)', body):
                    self.assertTrue((ROOT / script).exists(), script)
                    for flag in set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", call)):
                        self.assertIn(flag, help_text(script), f"{path.name}: {script} {flag}")

    def test_relays_do_not_do_the_work(self):
        for path in AGENTS:
            _, body = front(path)
            self.assertIn("thin relay", body, path.name)
            self.assertIn("`6`", body, path.name)  # the Claude-subagent fallback goes back to the caller


if __name__ == "__main__":
    unittest.main()
