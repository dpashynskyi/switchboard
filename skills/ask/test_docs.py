"""docs/config.md names every setting the code reads: the environment variables, the keys of machine.json and of a
local server's entry, and the keys of the shipped roles.json. A new setting without a line in the docs fails here."""
import os as _os
import tempfile as _tempfile
# Isolated before route is imported, whichever test module the runner loads first: a machine's own machine.json,
# caches and claude_usage.json are never read or written by a test.
_os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
_os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
_os.environ["ASK_CLAUDE_USAGE_PATH"] = _os.path.join(_os.environ["ASK_CACHE_DIR"], "claude_usage.json")
import glob
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DOC = os.path.join(ROOT, "docs", "config.md")
ROLES = os.path.join(HERE, "roles.json")
# Read by the code but not settings: the shell's own, an error the loader keeps, and the journal's fields (its rows
# are read through a variable called entry too).
NOT_SETTINGS = {"HOME", "USERPROFILE", "PATH", "TERM", "_error", "event", "status", "type", "verdict"}


def sources():
    files = [p for p in glob.glob(os.path.join(HERE, "*.py")) if not os.path.basename(p).startswith("test_")]
    files += [os.path.join(ROOT, "tools", n) for n in ("writer.py", "broker.py", "room.py") if os.path.exists(os.path.join(ROOT, "tools", n))]
    files += glob.glob(os.path.join(ROOT, "mods", "limits-band", "band.py"))
    files += glob.glob(os.path.join(ROOT, "hooks", "*.tsx")) + glob.glob(os.path.join(ROOT, "mods", "limits-band", "hooks", "*.tsx"))
    return {p: Path(p).read_text(encoding="utf-8") for p in files}


@unittest.skipIf(DOC is None, "no docs/config.md here")
class DocsTests(unittest.TestCase):
    def setUp(self):
        with open(DOC, encoding="utf-8") as f:
            self.doc = f.read()
        self.code = sources()

    def test_cli_reference_is_current(self):
        result = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "tools", "cli_docs.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if "not compared" in result.stdout:
            self.skipTest(result.stdout.strip())

    def missing(self, names):
        # A key with a leading underscore is the code's own mark, not a setting.
        return sorted(n for n in set(names) - NOT_SETTINGS if not n.startswith("_") and f"`{n}`" not in self.doc)

    def test_every_environment_variable(self):
        names = set()
        for text in self.code.values():
            names.update(re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z_]+)"', text))
            names.update(re.findall(r'\benv\.get\("([A-Z_]+)"', text))
            names.update(re.findall(r"\$\.env\.get\('([A-Z_]+)'\)", text))
        self.assertIn("ASK_CONTEXT", names)  # the scan itself works
        self.assertEqual(self.missing(names), [])

    def test_every_machine_json_key_and_local_server_key(self):
        names = set()
        for text in self.code.values():
            names.update(re.findall(r'\(?machine(?: or \{\})?\)?\.get\("([a-z_]+)"', text))
            names.update(re.findall(r'self\.config\.get\("([a-z_]+)"', text))
            # Nested keys: hardware.gpu.* (hw, gpu) and an account's fields in "accounts" (entry).
            names.update(re.findall(r'\b(?:hw|gpu|entry)\.get\("([a-z_]+)"', text))
        self.assertIn("local_servers", names)
        self.assertEqual(self.missing(names), [])

    def test_every_key_of_the_shipped_roles(self):
        with open(ROLES, encoding="utf-8") as f:
            roles = json.load(f)
        names = {k for k in roles if not k.startswith("_")}
        names |= {k for spec in roles.get("roles", {}).values() if isinstance(spec, dict) for k in spec}
        names |= {k for k in roles.get("experience", {}) if not k.startswith("_")}
        self.assertEqual(self.missing(names), [])


if __name__ == "__main__":
    unittest.main()
