"""Keep switchboard (and the repositories it reads from) on the latest main, without a manual `git pull`.

The limits band runs this at a session's start and every quarter of an hour; the script itself checks at most once an
hour (--every), so a run in between only reads a file.

    python3 tools/update.py [--every SECONDS] [--force] [--repo PATH ...] [--dry-run]

What it updates: this clone, every path in machine.json "update": {"repos": [...]}, and the repositories the router
already reads from (the folder of machine.json's "hooks" module, the target of ~/.config/ask/roles.json when it is a
link), each only when it is a git clone.

A repository is moved only when that is safe, and is left as it is otherwise, with the reason in the output:
- it is on main, tracking origin/main (a clone on another branch is someone's work in progress);
- no merge, rebase, cherry-pick or bisect is in progress;
- it has no commits of its own that origin/main lacks (fast-forward only: nothing is merged, rebased or stashed);
- no file with local edits, staged or not, and no untracked file is one the update changes. Edits elsewhere stay as
  they are and do not hold the update back; git's own fast-forward refuses anything this check missed.
The fetch runs with prompts off (GIT_TERMINAL_PROMPT=0, Git Credential Manager non-interactive, ssh in batch mode), so
a clone that needs a sign-in fails quietly instead of opening a window.

Copies: on Windows a link often stands as a copy (~/.config/ask/roles.json of a profile file). A watched copy (that
file, and machine.json "update": {"copies": [...]}) that still holds exactly the old version of a file the update
changed is replaced by the new version; a copy with edits of its own is left and named in the output.

Off: SWITCHBOARD_AUTOUPDATE=0 in the environment, or "update": {"auto": false} in machine.json. --force checks now
whatever the interval and the switch say (the /switchboard-update command).

Output: one JSON object on stdout (ASCII only), {"checked": bool, "reason": str|null, "repos": [...], "copies": [...]},
each repo {"name", "path", "state": "updated"|"current"|"skipped"|"error", "note", "why", "detail", "from", "to",
"subjects", "changed", "needs_new_session"}; why is busy|branch|ahead|clash|dry|error for a repo that did not move
(detail: the branch, the count of local commits, the files with local edits), for the band to say in its language. needs_new_session is true when the update changed the mod's own module, manifest or
agents, which a session that does not watch the folder reads only at its start. Exit 0 whatever happened to the repos;
2 on bad use.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("ASK_CONFIG_DIR") or os.path.join(HOME, ".config", "ask")
CACHE_DIR = os.environ.get("ASK_CACHE_DIR") or os.path.join(HOME, ".cache", "ask")
STATE_PATH = os.path.join(CACHE_DIR, "update.json")
LOCK_PATH = os.path.join(CACHE_DIR, "update.lock")
BRANCH = "main"
REMOTE = "origin"
DEFAULT_EVERY = 3600
LOCK_STALE_S = 600
GIT_TIMEOUT = 90
# What a session that does not watch the plugin folder reads only at its start.
SESSION_PATHS = ("hooks/", "types/", "agents/", ".claude-plugin/")


def git(repo, *args, timeout=GIT_TIMEOUT):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env["LC_ALL"] = "C"
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashing up on Windows
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, env=env, timeout=timeout,
                       stdin=subprocess.DEVNULL, creationflags=flags)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def git_ok(repo, *args, timeout=GIT_TIMEOUT):
    code, out, err = git(repo, *args, timeout=timeout)
    if code != 0:
        raise RuntimeError((err.strip() or out.strip() or f"git {args[0]}: exit {code}").splitlines()[-1][:200])
    return out


def git_bytes(repo, *args):
    flags = 0x08000000 if os.name == "nt" else 0
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=GIT_TIMEOUT,
                       stdin=subprocess.DEVNULL, creationflags=flags)
    return r.stdout if r.returncode == 0 else None


def load_machine():
    try:
        with open(os.path.join(CONFIG_DIR, "machine.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def toplevel(path):
    """The work tree holding path (a file or a folder), or None."""
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(folder):
        return None
    try:
        code, out, _ = git(folder, "rev-parse", "--show-toplevel", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.normpath(out.strip()) if code == 0 and out.strip() else None


def watched_copies(machine):
    conf = machine.get("update") if isinstance(machine.get("update"), dict) else {}
    paths = [os.path.join(CONFIG_DIR, "roles.json")] + [p for p in conf.get("copies") or [] if isinstance(p, str)]
    seen, out = set(), []
    for p in paths:
        p = os.path.normpath(os.path.expanduser(p))
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def repos_to_update(machine, extra):
    conf = machine.get("update") if isinstance(machine.get("update"), dict) else {}
    found = [ROOT]
    found += [os.path.expanduser(p) for p in conf.get("repos") or [] if isinstance(p, str)]
    hooks = machine.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get("module"), str):
        found.append(os.path.expanduser(hooks["module"]))
    roles = os.path.join(CONFIG_DIR, "roles.json")
    if os.path.islink(roles):
        found.append(os.path.realpath(roles))
    found += [os.path.expanduser(p) for p in extra]
    seen, out = set(), []
    for p in found:
        top = toplevel(p)
        key = os.path.normcase(top) if top else None
        if top and key not in seen:
            seen.add(key)
            out.append(top)
    return out


def same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def busy(repo):
    """The operation in progress in the clone, or None."""
    code, out, _ = git(repo, "rev-parse", "--git-dir", timeout=20)
    gd = out.strip()
    if code != 0 or not gd:
        return "not a git clone"
    gd = gd if os.path.isabs(gd) else os.path.join(repo, gd)
    for name, what in (("MERGE_HEAD", "a merge"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
                       ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert"), ("BISECT_LOG", "a bisect")):
        if os.path.exists(os.path.join(gd, name)):
            return f"{what} is in progress"
    return None


def local_paths(repo):
    """Paths with edits (staged or not) or untracked, as git status names them (both sides of a rename)."""
    out = git_ok(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths, parts, i = set(), out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":  # the next field is the rename's source
            if i < len(parts) and parts[i]:
                paths.add(parts[i])
            i += 1
    return paths


def update_repo(repo, dry_run=False):
    r = {"name": os.path.basename(repo), "path": repo, "state": "current", "note": None, "from": None, "to": None,
         "subjects": [], "changed": [], "needs_new_session": False, "why": None, "detail": []}
    try:
        why = busy(repo)
        if why:
            return {**r, "state": "skipped", "note": why, "why": "busy"}
        branch = git_ok(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch != BRANCH:
            return {**r, "state": "skipped", "note": f"on {branch}, not {BRANCH}", "why": "branch", "detail": [branch]}
        git_ok(repo, "fetch", "--quiet", "--no-tags", REMOTE, f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}")
        upstream = f"{REMOTE}/{BRANCH}"
        head = git_ok(repo, "rev-parse", "HEAD").strip()
        new = git_ok(repo, "rev-parse", upstream).strip()
        r["from"], r["to"] = head[:9], new[:9]
        if head == new:
            return r
        ahead = int(git_ok(repo, "rev-list", "--count", f"{upstream}..HEAD").strip() or 0)
        if ahead:
            return {**r, "state": "skipped", "note": f"{ahead} local commit(s) not on {upstream}", "why": "ahead",
                    "detail": [str(ahead)]}
        changed = [p for p in git_ok(repo, "diff", "--name-only", "-z", "--no-renames", head, new).split("\0") if p]
        r["changed"] = changed
        clash = sorted(local_paths(repo) & set(changed))
        if clash:
            more = f" (+{len(clash) - 3})" if len(clash) > 3 else ""
            return {**r, "state": "skipped", "note": "local edits in files the update changes: " + ", ".join(clash[:3]) + more,
                    "why": "clash", "detail": clash}
        r["subjects"] = [s for s in git_ok(repo, "log", "--no-merges", "--format=%s", f"{head}..{new}").splitlines() if s][:8]
        r["needs_new_session"] = same(repo, ROOT) and any(p.startswith(SESSION_PATHS) for p in changed)
        if dry_run:
            return {**r, "state": "skipped", "note": "dry run", "why": "dry"}
        git_ok(repo, "merge", "--ff-only", "--quiet", new)
        r["state"] = "updated"
        return r
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
        return {**r, "state": "error", "note": str(e)[:200], "why": "error"}


def norm(data):
    return data.replace(b"\r\n", b"\n")


def refresh_copies(updated, copies, dry_run=False):
    """A watched copy still holding the old version of a changed file gets the new one; edited copies are named."""
    out = []
    for path in copies:
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                mine = norm(f.read())
        except OSError:
            continue
        for repo in updated:
            for rel in repo["changed"]:
                old = git_bytes(repo["path"], "show", f"{repo['from_full']}:{rel}")
                if old is None or norm(old) != mine:
                    continue
                new = git_bytes(repo["path"], "show", f"{repo['to_full']}:{rel}")
                if new is None:
                    out.append({"path": path, "source": f"{repo['name']}/{rel}", "state": "skipped",
                                "note": "the file was removed upstream; the copy is left"})
                    break
                if not dry_run:
                    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".update-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(new)
                    os.replace(tmp, path)
                out.append({"path": path, "source": f"{repo['name']}/{rel}", "state": "updated", "note": None})
                break
            else:
                continue
            break
    return out


def read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, prefix=".update-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def take_lock():
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        if time.time() - os.path.getmtime(LOCK_PATH) > LOCK_STALE_S:
            os.remove(LOCK_PATH)
    except OSError:
        pass
    try:
        os.close(os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def drop_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def run(every=DEFAULT_EVERY, force=False, extra=(), dry_run=False, now=None):
    now = time.time() if now is None else now
    machine = load_machine()
    conf = machine.get("update") if isinstance(machine.get("update"), dict) else {}
    result = {"checked": False, "reason": None, "repos": [], "copies": []}
    if not force:
        if os.environ.get("SWITCHBOARD_AUTOUPDATE", "").strip() == "0" or conf.get("auto") is False:
            return {**result, "reason": "off"}
        last = read_state().get("checked_at")
        if isinstance(last, (int, float)) and 0 <= now - last < every:
            return {**result, "reason": "checked recently"}
    if not take_lock():
        return {**result, "reason": "another check is running"}
    try:
        repos = []
        for repo in repos_to_update(machine, extra):
            before = git(repo, "rev-parse", "HEAD", timeout=20)[1].strip()
            r = update_repo(repo, dry_run=dry_run)
            r["from_full"], r["to_full"] = before, git(repo, "rev-parse", f"{REMOTE}/{BRANCH}", timeout=20)[1].strip()
            repos.append(r)
        changed = [r for r in repos if r["state"] == "updated" or (dry_run and r["changed"])]
        result["copies"] = refresh_copies(changed, watched_copies(machine), dry_run=dry_run) if changed else []
        for r in repos:
            r.pop("from_full", None)
            r.pop("to_full", None)
        result["repos"] = repos
        result["checked"] = True
        if not dry_run:
            write_state({"checked_at": now, "repos": {r["path"]: {k: r[k] for k in ("state", "note", "from", "to")}
                                                       for r in repos}})
        return result
    finally:
        drop_lock()


def main(argv=None):
    p = argparse.ArgumentParser(description="Fast-forward switchboard and the repositories it reads from to origin/main, when safe.")
    p.add_argument("--every", type=int, default=DEFAULT_EVERY, help="seconds between checks (default 3600)")
    p.add_argument("--force", action="store_true", help="check now, whatever the interval and the off switch say")
    p.add_argument("--repo", action="append", default=[], help="one more clone to update (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="fetch and say what would change; move nothing")
    a = p.parse_args(argv)
    if a.every < 0:
        p.error("--every must be 0 or more")
    print(json.dumps(run(a.every, a.force, a.repo, a.dry_run), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
