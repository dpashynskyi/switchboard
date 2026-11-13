"""Regenerate the command line reference from each script's help."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [('ask', 'skills/ask/ask.py'), ('writer', 'tools/writer.py'), ('limits', 'skills/ask/limits.py'),
           ('preset_edit', 'skills/ask/preset_edit.py'), ('dashboard', 'skills/ask/dashboard.py'),
           ('stats_push', 'skills/ask/stats_push.py'), ('experience', 'skills/ask/experience.py'),
           ('comment_check', 'tools/comment_check.py'), ('room', 'tools/room.py'), ('chat', 'tools/chat.py'), ('cloud', 'tools/cloud.py')]
# argparse lays out help differently from one Python to the next ("optional arguments:" became "options:" in 3.10,
# usage lines wrap otherwise): the page names the Python it was made under, and only that Python compares it.
PYTHON = f"{sys.version_info[0]}.{sys.version_info[1]}"
INTRO = f"# Command line\n\nEvery script's `--help` under Python {PYTHON}; `python3 tools/cli_docs.py` rewrites this page.\n"
MADE_UNDER = re.compile(r"`--help` under Python (\d+\.\d+);")


def render(root=ROOT):
    env = {k: v for k, v in os.environ.items() if not k.startswith('ASK_') and k != 'CODEX_HOME'}
    with tempfile.TemporaryDirectory(prefix='cli-docs-') as tmp:
        env.update(COLUMNS='110', NO_COLOR='1', ASK_LANG='en', ASK_CONFIG_DIR=tmp + '/config', ASK_CACHE_DIR=tmp + '/cache')
        sections = {}
        for name, script in SCRIPTS:
            result = subprocess.run([sys.executable, '-B', str(root / script), '--help'], cwd=root, env=env,
                                    capture_output=True, text=True, encoding='utf-8', check=True)
            sections[name] = f'\n## {name}\n\n```text\n{result.stdout.rstrip()}\n```\n'
    return INTRO, sections


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    intro, sections = render()
    expected = intro + ''.join(sections.values())
    path = ROOT / 'docs/cli.md'
    if args.check:
        actual = path.read_text(encoding='utf-8') if path.exists() else ''
        made = MADE_UNDER.search(actual)
        if made and made.group(1) != PYTHON:
            print(f'docs/cli.md: made under Python {made.group(1)}, this is {PYTHON}: not compared')
            return 0
        if actual == expected:
            return 0
        old_intro = actual.split('\n## ', 1)[0]
        if old_intro != intro:
            print('docs/cli.md: intro differs')
        for name, section in sections.items():
            start = actual.find(f'\n## {name}\n')
            end = actual.find('\n## ', start + 1) if start >= 0 else -1
            old = actual[start:end if end >= 0 else None] if start >= 0 else ''
            if old != section:
                print(f'docs/cli.md: {name} section differs')
        return 1
    path.write_text(expected, encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
