"""The launcher row above the limits band: which providers and models this machine has, and opening a new session
with one of them in one of its clients.

    python ~/switchboard/mods/launcher/launch.py list
    python ~/switchboard/mods/launcher/launch.py open <provider> <model> <client> [--cwd DIR] [--dry-run]

`list` prints one JSON line: the providers the router finds present and allowed here (providers.inventory), each
with its models in the router's order and the clients it opens in. Accounts and emails stay out of it.
`open` starts the client: a new window of the terminal (iTerm, else Terminal) running the provider's own CLI with
that model, or the provider's desktop app on the folder. Claude's desktop app takes no model from a link: it opens a
new Code session on the folder with the app's own model.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.parse

ASK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "ask")
if ASK not in sys.path:
    sys.path.insert(0, ASK)

import providers  # noqa: E402

# Label and colour of each provider's buttons; a provider not named here (a local server) gets the last one.
LOOK = {
    "claude-subscription": ("Claude", "#D97757"),
    "agy": ("agy · Gemini", "#4285F4"),
    "codex": ("Codex · GPT", "#10A37F"),
    "ollama": ("Ollama", "#9B7BD4"),
}
OTHER = "#8A8F98"

# The clients each provider opens in: (id, label). "terminal" is the provider's own CLI in a new terminal window.
CLIENTS = {
    "claude-subscription": [("desktop", "Claude desktop"), ("terminal", "термінал")],
    "agy": [("terminal", "термінал"), ("app", "Antigravity")],
    "codex": [("terminal", "термінал"), ("app", "Codex app")],
    "ollama": [("terminal", "термінал")],
}
CLIENTS_EN = {"terminal": "terminal"}
EFFORT_PICK = ("high", "medium", "xhigh", "low", "max")


def _model_ids(row):
    """The ids a launch takes for one inventory row: agy names a model with its effort (gemini-4-argon-high), one id
    per family at the effort its roles use, else high; the others take the row's id."""
    ids = row.get("ids") or {}
    if not ids:
        return [row["id"]]
    used = [r.split("@", 1)[1] for r in row.get("roles") or [] if "@" in r]
    for effort in [e for e in EFFORT_PICK if e in used] + list(EFFORT_PICK):
        if effort in ids:
            return [ids[effort]]
    return [next(iter(ids.values()))]


def listing(lang="en"):
    out = []
    for entry in providers.inventory():
        name = entry["provider"]
        if not (entry.get("detect") or {}).get("present"):
            continue
        models, in_roles = [], []
        for acc in entry.get("accounts") or []:
            if not acc.get("allowed"):
                continue
            for row in acc.get("models") or []:
                for mid in _model_ids(row):
                    if mid in models:
                        continue
                    models.append(mid)
                    if row.get("roles"):
                        in_roles.append(mid)
        if not models:
            continue
        label, color = LOOK.get(name, (entry.get("title") or name, OTHER))
        clients = CLIENTS.get(name, [])
        if not clients:
            continue
        out.append({
            "id": name,
            "label": label,
            "color": color,
            # The router's own models first, in its order: the one a role picks most is the likeliest wish.
            "models": in_roles + [m for m in models if m not in in_roles],
            "clients": [{"id": c, "label": text if lang == "uk" else CLIENTS_EN.get(c, text)} for c, text in clients],
        })
    return out


def _quote(s):
    return shlex.quote(s)


def command_line(provider, model, cwd):
    """The shell line the terminal window runs for a provider's own CLI."""
    if provider == "claude-subscription":
        cli = shutil.which("claude") or "claude"
        run = [cli, "--model", model]
    elif provider == "agy":
        cli = providers.get("agy").detect().get("path") or "agy"
        run = [cli, "--model", model]
    elif provider == "codex":
        cli = providers.get("codex").detect().get("path") or "codex"
        run = [cli, "-m", model]
    elif provider == "ollama":
        cli = providers.get("ollama").detect().get("path") or "ollama"
        run = [cli, "run", model]
    else:
        raise SystemExit(f"no terminal client for {provider}")
    return f"cd {_quote(cwd)} && " + " ".join(_quote(p) for p in run)


def _applescript_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_argv(line):
    if os.path.isdir("/Applications/iTerm.app"):
        # The window runs the line in a login shell and stays as that shell after it: a `write text` into a fresh
        # window sometimes landed before the shell was up and was lost.
        shell = os.environ.get("SHELL") or "/bin/zsh"
        held = f"{_quote(shell)} -lic {_quote(line + '; exec ' + _quote(shell) + ' -l')}"
        script = (
            'tell application "iTerm"\n'
            "  activate\n"
            f"  create window with default profile command {_applescript_string(held)}\n"
            "end tell"
        )
    else:
        script = (
            'tell application "Terminal"\n'
            "  activate\n"
            f"  do script {_applescript_string(line)}\n"
            "end tell"
        )
    return ["osascript", "-e", script]


def open_argv(provider, model, client, cwd):
    if client == "terminal":
        return terminal_argv(command_line(provider, model, cwd))
    if provider == "claude-subscription" and client == "desktop":
        query = urllib.parse.urlencode({"folder": cwd, "source": "switchboard"})
        return ["open", f"claude://code/new?{query}"]
    if provider == "agy" and client == "app":
        return ["open", "-a", "Antigravity", cwd]
    if provider == "codex" and client == "app":
        cli = providers.get("codex").detect().get("path") or "codex"
        return [cli, "app", cwd]
    raise SystemExit(f"no client {client} for {provider}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="verb", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--lang", default=os.environ.get("ASK_LANG", "en"))
    op = sub.add_parser("open")
    op.add_argument("provider")
    op.add_argument("model")
    op.add_argument("client")
    op.add_argument("--cwd", default=os.getcwd())
    op.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.verb == "list":
        print(json.dumps({"providers": listing(a.lang)}, ensure_ascii=False))
        return 0
    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    argv_ = open_argv(a.provider, a.model, a.client, cwd)
    if a.dry_run:
        print(json.dumps(argv_, ensure_ascii=False))
        return 0
    done = subprocess.run(argv_, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        sys.stderr.write((done.stderr or done.stdout or "").strip()[-300:] + "\n")
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
