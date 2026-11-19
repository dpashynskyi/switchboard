"""ask.py --tidy: archive the Codex threads that switchboard's runs left in the Codex app's list.

A run archives its own thread as it ends (broker.Driver.tidy, ask.ask_codex); this sweeps the ones from before that, and any a
killed run could not archive. A thread is switchboard's when the journal names it (an /ask or writer run) or it was started in
/ask's own room (route.CODEX_ROOM, where nothing else works). A thread written to in the last QUIET seconds may still be
running and is left; so is one open in a terminal handoff. Archiving is reversible: Codex's own unarchive, or carrying the
thread on (--thread, writer.py --continue), brings it back.
"""
import json
import os
from pathlib import Path
import time

import handoff
import providers
import route

QUIET = 30 * 60


def _thread_id(path):
    """rollout-<time>-<uuid>.jsonl -> the uuid (the thread id)."""
    stem = path.name[:-len(".jsonl")]
    return stem[-36:] if len(stem) > 36 and stem[-37] == "-" else None


def _cwd(path):
    """The folder the thread was started in, from the rollout's session_meta line; None when unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = json.loads(handle.readline() or "{}")
    except (OSError, ValueError):
        return None
    payload = first.get("payload") if isinstance(first, dict) else None
    return payload.get("cwd") if isinstance(payload, dict) and isinstance(payload.get("cwd"), str) else None


def _same(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def journal_threads(rows):
    return {str(r["conversation"]) for r in rows
            if isinstance(r, dict) and r.get("provider") == "codex" and r.get("conversation") and "type" not in r}


def find(homes, known, now=None, quiet=QUIET, room=None):
    """{home: [thread id, ...]}: the switchboard threads still in each CODEX_HOME's active sessions."""
    now = time.time() if now is None else now
    room = room or route.CODEX_ROOM
    found = {}
    for home in homes:
        for path in sorted(Path(home, "sessions").glob("**/rollout-*.jsonl")):
            thread = _thread_id(path)
            if not thread:
                continue
            try:
                if now - path.stat().st_mtime < quiet:
                    continue
            except OSError:
                continue
            if handoff.is_open(thread):
                continue
            if thread in known or _same(_cwd(path) or "", room):
                found.setdefault(home, []).append(thread)
    return found


def run(yes=False, quiet=QUIET):
    import ask
    adapter = providers.get("codex")
    homes = list(dict.fromkeys(providers.expand(a["where"]) for a in adapter.accounts() if a.get("where")))
    found = find(homes, journal_threads(route._read_journal_rows()), quiet=quiet)
    total = sum(len(v) for v in found.values())
    if not total:
        print("Nothing to tidy: no switchboard thread is left in the Codex app's list.")
        return 0
    for home, threads in found.items():
        print(f"{home}: {len(threads)} switchboard thread(s) in the Codex app's list")
    if not yes:
        print("Run ask.py --tidy --yes to archive them (Codex can unarchive; carrying a thread on brings it back).")
        return 0
    failed = 0
    cli = route.codex_path()
    for home, threads in found.items():
        result = ask.codex_tidy(cli, threads, "thread/archive", home)
        errors = {t: e for t, e in result.items() if e}
        failed += len(errors)
        print(f"{home}: archived {len(threads) - len(errors)} of {len(threads)}")
        for thread, error in list(errors.items())[:5]:
            print(f"  {thread}: {error}")
    return 2 if failed else 0
