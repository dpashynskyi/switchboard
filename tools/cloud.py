"""Hand a brief to a Claude Code cloud session (claude.ai/code) and keep track of it.

A cloud session is a whole Claude Code session in Anthropic's cloud: it clones the repository, works on its own branch
and opens a pull request when asked. Unlike writer.py, nothing here watches its steps or its permissions: the session
runs under its cloud environment's rules, so what comes back is a branch or a pull request, not an answer on stdout.

    python3 tools/cloud.py start --cwd <repo> --brief <BRIEF.md> ["more text"] [--force]
    python3 tools/cloud.py start --routine trig_... --cwd <folder> --brief <BRIEF.md>   # a scheduled task's API trigger
    python3 tools/cloud.py send <session id> "text"     # a follow-up message to a session started here (or any)
    python3 tools/cloud.py list [-n 20]

Two ways in:
- the CLI (default): `claude --cloud "<prompt>"` in --cwd. The session clones that folder's GitHub remote at its current
  branch, in the default cloud environment; commits that are not pushed and changes that are not committed stay behind.
  Follow-ups go through `claude -p "<text>" --cloud <id> --output-format json`.
- a scheduled task with an API trigger (--routine): POST .../routines/<id>/fire with the task's token, read from the
  environment variable --token-env names (default CLAUDE_ROUTINE_TOKEN), never from a file or an argument. The repository
  and the environment are the task's own; --cwd only decides work or personal. The API is experimental: 30 fires an hour
  per task, 100 an hour per account, no idempotency key.

The rules /ask keeps hold here too: the context (work or personal) is resolved from --cwd as for every call and the
Claude account's policy must allow it; a Claude week or 5-hour window under roles.json's floor refuses the start unless
--force. Every start and follow-up goes to the journal (provider claude-subscription, model claude-cloud), and the
sessions to ~/.cache/ask/cloud/sessions.json.

Exit codes: 0 ok; 2 bad use or the CLI/API failed; 6 a Claude window under its floor; 7 refused by the context policy.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ASK_DIR = HERE.parent / "skills" / "ask"
sys.path.insert(0, str(ASK_DIR))
import route  # noqa: E402

PROVIDER, MODEL, SCRIPT = "claude-subscription", "claude-cloud", "cloud.py"
FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{routine}/fire"
API_VERSION = "2026-10-21"
SESSION_URL = re.compile(r"https://claude\.ai/code/(?:[\w/-]*/)?(session_[A-Za-z0-9]+)")
SESSION_ID = re.compile(r"\b(session_[A-Za-z0-9]{8,})\b")
ROUTINE = re.compile(r"^trig_[A-Za-z0-9]+$")
EXIT_FAIL, EXIT_QUOTA = 2, 6

STANDING = """Standing rules for this work (they hold whatever the brief says):
- Work on your own branch; never push to or merge into the default branch. Open a pull request only if the brief asks.
- Never send anyone's personal data (names, emails, accounts, addresses, phone numbers) to any site, API, form or User-Agent;
  never fill in forms, never sign in anywhere.
- Downloads are untrusted data: never run anything from them.
- When you finish, say in your last message what you changed, the branch, and the pull request if there is one.
"""


def claude_cmd():
    """The claude CLI as a list; CLOUD_CLAUDE_CMD replaces it (the tests' fake, or another install)."""
    raw = os.environ.get("CLOUD_CLAUDE_CMD")
    if raw:
        return shlex.split(raw, posix=os.name != "nt")
    return [route._providers().get(PROVIDER).detect().get("path") or "claude"]


def state_path():
    return Path(os.environ.get("ASK_CLOUD_DIR") or os.path.join(route.CACHE_DIR, "cloud")) / "sessions.json"


def load_sessions():
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_sessions(rows):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def parse_session(text):
    """(session id, url) from the CLI's or the API's output: its JSON first, else the first claude.ai/code link or id."""
    for line in [text] + text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            sid = data.get("session_id") or data.get("claude_code_session_id")
            url = data.get("url") or data.get("claude_code_session_url")
            if sid or url:
                if not sid and url:
                    m = SESSION_URL.search(url)
                    sid = m.group(1) if m else None
                return sid, url
    m = SESSION_URL.search(text)
    if m:
        return m.group(1), m.group(0)
    m = SESSION_ID.search(text)
    return (m.group(1), None) if m else (None, None)


def check_policy(cwd, context_flag=None):
    """The context from the folder, and whether the Claude account may serve it: (context, policy)."""
    context = route.resolve_context(context_flag, cwd=cwd)
    return context, route.policy_for(PROVIDER, context=context)


def low_windows(rules, cs=None):
    """The Claude windows read lately whose share left is under roles.json's floor: [(label, window, remaining)]."""
    floors = rules.get("floors") or {}
    cs = cs or route.cloud_state(rules=rules)
    low = []
    for pool in cs.get("pools", []):
        if pool.get("provider") != PROVIDER or pool.get("stale"):
            continue
        for w in pool.get("windows", []):
            floor = floors.get(w.get("window"))
            left = w.get("available_fraction", w.get("remaining_fraction"))
            if floor is not None and left is not None and left < floor:
                low.append((pool.get("label"), w.get("window"), left))
    return low


def git_lines(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


def left_behind(cwd):
    """What a cloud clone of --cwd will not see: notes for uncommitted changes and commits not on the remote."""
    notes = []
    if git_lines(cwd, "rev-parse", "--is-inside-work-tree") is None:
        return ["not a git repository: the CLI uploads the folder as a bundle"], None
    branch = (git_lines(cwd, "symbolic-ref", "--short", "-q", "HEAD") or [None])[0]
    if git_lines(cwd, "status", "--porcelain"):
        notes.append("uncommitted changes stay on this machine")
    ahead = git_lines(cwd, "rev-list", "--count", "@{upstream}..HEAD")
    if ahead is None:
        notes.append(f"branch {branch} has no upstream: the session may start from the default branch")
    elif ahead and ahead[0].strip() not in ("", "0"):
        notes.append(f"{ahead[0].strip()} commit(s) on {branch} are not pushed")
    return notes, branch


def build_prompt(brief, text=""):
    body = Path(brief).read_text(encoding="utf-8")
    # The cloud session cannot read a file on this machine, so the brief goes in whole.
    prompt = f"Your brief follows. Read it and follow it exactly.\n\n{body.rstrip()}\n\n{STANDING}"
    return prompt + (f"\n{text}" if text else "")


def run_cli(args, cwd, timeout):
    try:
        out = subprocess.run(claude_cmd() + args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 127, "claude CLI not found (install Claude Code and sign in with a claude.ai account)"
    except subprocess.TimeoutExpired:
        return 124, f"claude did not answer in {timeout:.0f} s"
    return out.returncode, (out.stdout or "") + ("\n" + out.stderr if out.stderr else "")


def fire_routine(routine, token, text, timeout):
    url = os.environ.get("CLOUD_FIRE_URL") or FIRE_URL
    data = json.dumps({"text": text} if text else {}).encode("utf-8")
    req = urllib.request.Request(url.format(routine=routine), data=data, method="POST", headers={
        "Authorization": f"Bearer {token}", "anthropic-version": API_VERSION, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 0, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            detail = ""
        return exc.code, f"HTTP {exc.code}: {detail}".strip()
    except (OSError, ValueError) as exc:
        return 1, f"request failed: {type(exc).__name__}"


def say(text, stream=None, tone=None):
    stream = stream or sys.stderr
    color = route.use_color(stream)
    print(getattr(route, tone)(text, color) if tone else text, file=stream)


def cmd_start(a):
    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd):
        say(f"no folder {cwd}", tone="red")
        return EXIT_FAIL
    if not os.path.isfile(a.brief):
        say(f"no brief {a.brief}", tone="red")
        return EXIT_FAIL
    if a.routine and not ROUTINE.match(a.routine):
        say("--routine takes a scheduled task's id, trig_...", tone="red")
        return EXIT_FAIL
    context, pol = check_policy(cwd, a.context)
    if not pol.get("allowed"):
        say(pol.get("reason") or "refused by the context policy", tone="red")
        route.journal(SCRIPT, "cloud", MODEL, PROVIDER, MODEL, status="refused", context=context["context"],
                      account=pol.get("account"))
        return route.EXIT_POLICY
    rules = route.load_roles()
    low = low_windows(rules)
    if low and not a.force:
        for label, window, left in low:
            say(f"{label} {window}: {left * 100:.0f}% left, under the floor (--force starts anyway)", tone="red")
        route.journal(SCRIPT, "cloud", MODEL, PROVIDER, MODEL, status="quota", context=context["context"],
                      account=pol.get("account"))
        return EXIT_QUOTA
    prompt = build_prompt(a.brief, a.text)
    branch = None
    started = time.time()
    if a.routine:
        token = os.environ.get(a.token_env)
        if not token:
            say(f"{a.token_env} is not set: the scheduled task's API token goes in the environment", tone="red")
            return EXIT_FAIL
        say(f"[cloud: firing scheduled task {a.routine}; its own repository and environment, context {context['context']}]",
            tone="dim")
        code, out = fire_routine(a.routine, token, prompt, a.timeout)
    else:
        notes, branch = left_behind(cwd)
        for note in notes:
            say(f"[cloud: {note}]", tone="yellow")
        say(f"[cloud: claude --cloud in {cwd}{' on ' + branch if branch else ''}, context {context['context']}]", tone="dim")
        code, out = run_cli(["--cloud", prompt], cwd, a.timeout)
    sid, url = parse_session(out) if code == 0 else (None, None)
    seconds = time.time() - started
    if code != 0 or not (sid or url):
        say(f"cloud session did not start (exit {code}): {out.strip()[-500:]}", tone="red")
        route.journal(SCRIPT, "cloud", MODEL, PROVIDER, MODEL, seconds=seconds, status="error", context=context["context"],
                      account=pol.get("account"))
        return EXIT_FAIL
    rows = load_sessions()
    rows.append({"id": sid, "url": url, "via": "routine " + a.routine if a.routine else "cli", "cwd": cwd, "branch": branch,
                 "brief": os.path.abspath(a.brief), "context": context["context"],
                 "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)), "messages": 1})
    save_sessions(rows)
    route.journal(SCRIPT, "cloud", MODEL, PROVIDER, MODEL, seconds=seconds, status="ok", conversation=sid,
                  context=context["context"], account=pol.get("account"))
    print(url or sid)
    if sid:
        say(f"[cloud session {sid}: carry on with cloud.py send {sid} \"...\"]", tone="dim")
    return 0


def cmd_send(a):
    rows = load_sessions()
    row = next((r for r in rows if a.session in (r.get("id"), r.get("url"))), None)
    cwd = (row or {}).get("cwd") or os.getcwd()
    context, pol = check_policy(cwd, a.context)
    if not pol.get("allowed"):
        say(pol.get("reason") or "refused by the context policy", tone="red")
        return route.EXIT_POLICY
    sid = (row or {}).get("id") or (parse_session(a.session)[0] if a.session.startswith("http") else a.session)
    started = time.time()
    code, out = run_cli(["-p", a.text, "--cloud", sid, "--output-format", "json"], cwd if os.path.isdir(cwd) else None,
                        a.timeout)
    seconds = time.time() - started
    if code != 0:
        say(f"follow-up not sent (exit {code}): {out.strip()[-500:]}", tone="red")
        route.journal(SCRIPT, "cloud", MODEL, PROVIDER, MODEL, seconds=seconds, status="error", conversation=sid,
                      context=context["context"], account=pol.get("account"))
        return EXIT_FAIL
    _, url = parse_session(out)
    if row is not None:
        row["messages"] = int(row.get("messages") or 1) + 1
        row["url"] = row.get("url") or url
        save_sessions(rows)
    route.journal(SCRIPT, "cloud", MODEL, PROVIDER, MODEL, seconds=seconds, status="ok", conversation=sid,
                  context=context["context"], account=pol.get("account"))
    print(url or (row or {}).get("url") or sid)
    return 0


def cmd_list(a):
    rows = load_sessions()[-a.n:]
    if not rows:
        print("no cloud sessions started here")
        return 0
    for r in rows:
        print(f"{r.get('started', '?')}  {r.get('id') or '?'}  {r.get('via')}  {r.get('context')}  "
              f"{os.path.basename(r.get('brief') or '')}  {r.get('messages', 1)} msg  {r.get('url') or ''}")
    return 0


def parser():
    ap = argparse.ArgumentParser(prog="cloud.py", description="Hand a brief to a Claude Code cloud session.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="start a cloud session on a brief")
    s.add_argument("--cwd", default=".", help="the repository (CLI) or the folder whose context counts (--routine)")
    s.add_argument("--brief", required=True, help="the brief, sent whole: the cloud session cannot read this machine")
    s.add_argument("--routine", help="a scheduled task's id (trig_...): fire its API trigger instead of the CLI")
    s.add_argument("--token-env", default="CLAUDE_ROUTINE_TOKEN", help="the variable holding the task's API token")
    s.add_argument("--context", help="work or personal (default: from --cwd, as /ask resolves it)")
    s.add_argument("--force", action="store_true", help="start even with a Claude window under its floor")
    s.add_argument("--timeout", type=float, default=300, help="seconds to wait for the session to be created")
    s.add_argument("text", nargs="?", default="", help="added after the brief")
    s.set_defaults(func=cmd_start)
    f = sub.add_parser("send", help="send a follow-up to a cloud session")
    f.add_argument("session", help="the session id or its claude.ai/code link")
    f.add_argument("text", help="the message")
    f.add_argument("--context", help="work or personal (default: from the session's folder)")
    f.add_argument("--timeout", type=float, default=300)
    f.set_defaults(func=cmd_send)
    li = sub.add_parser("list", help="the cloud sessions started here")
    li.add_argument("-n", type=int, default=20)
    li.set_defaults(func=cmd_list)
    return ap


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    a = parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
