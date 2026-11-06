"""Install switchboard as a new user would and check it, on macOS, Linux or Windows.

    python3 tools/clean_install_check.py [--repo <path or URL>] [--live]

--repo may be a local path: a clone of a branch before it is pushed.

A fresh home folder (HOME, and USERPROFILE on Windows) under the system's temp folder; a clone of the release repo
into it; the README's steps (ask.py --init, --list); the offline suites; claude plugin validate and test when the
claude CLI is here. agy is turned off in the fresh home's machine.json: an agy that is not signed in there asks to sign
in (a window per call), and that sign-in would land in the test folder. --live adds one real call by role through
your signed-in Codex account (CODEX_HOME pointed at it; nothing is copied). Your own settings are never touched: the
fresh home is all it writes to, and it stays for a look (the last line says where).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = "https://github.com/dpashynskyi/switchboard"


def fresh_env(home):
    """Only what a new user's shell would have, with home as the home folder. Windows' own system variables stay:
    every shell there has them, and without SYSTEMDRIVE each taskkill took 1.1 s instead of 0.09 s."""
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
            "PROGRAMFILES", "ProgramFiles", "WINDIR", "LANG", "SYSTEMDRIVE", "PROGRAMDATA", "ALLUSERSPROFILE",
            "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
            "PUBLIC", "OS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL",
            "PROCESSOR_REVISION")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(HOME=home, USERPROFILE=home, TERM="dumb", NO_COLOR="1", PYTHONIOENCODING="utf-8")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


LOGS = None  # the fresh home's logs folder: every step's whole output, so a failure can be read after


def run(name, cmd, cwd, env, results, timeout=900, ok_codes=(0,), stdin=subprocess.DEVNULL):
    t0 = time.time()
    print(f"...   {name}", flush=True)
    try:
        done = subprocess.run(cmd, cwd=cwd, env=env, stdin=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        out = (done.stdout + done.stderr).strip()
        code = done.returncode
    except (OSError, subprocess.SubprocessError) as e:
        out, code = str(e), -1
    last = [line for line in out.splitlines() if line.strip()][-1:] or [""]
    good = code in ok_codes
    results.append((name, good, code, round(time.time() - t0, 1), last[0][:120]))
    print(f"{'ok ' if good else 'FAIL'}  {name}: exit {code}, {results[-1][3]} s  {last[0][:120]}", flush=True)
    if LOGS:
        path = os.path.join(LOGS, "".join(c if c.isalnum() else "-" for c in name).strip("-")[:60] + ".log")
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)
        if not good:
            # The failing tests by name, and where the whole output is.
            for line in out.splitlines():
                if line.startswith(("FAIL:", "ERROR:")) or "Error:" in line and line.startswith(" "):
                    print(f"        {line.strip()[:160]}", flush=True)
            print(f"        whole output: {path}", flush=True)
    return code, out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--live", action="store_true", help="one real call by role through your signed-in Codex account")
    a = ap.parse_args(argv)
    # A Windows console or a file in its code page (cp1251) cannot print what claude prints ("✔"): replaced, not a crash.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    home = tempfile.mkdtemp(prefix="switchboard-clean-")
    root = os.path.join(home, "switchboard")
    py = sys.executable
    env = fresh_env(home)
    results = []
    print(f"fresh home: {home}", flush=True)
    global LOGS
    LOGS = os.path.join(home, "logs")
    os.makedirs(LOGS, exist_ok=True)
    # The clone uses your own git credentials (a private fork too); everything after it runs as the fresh user.
    run("git clone", ["git", "clone", "-q", a.repo, root], home, dict(os.environ), results, timeout=300)
    if not os.path.isdir(root):
        print("the clone failed: nothing more to check")
        return 1
    ask = [py, "-B", os.path.join(root, "skills", "ask", "ask.py")]
    code, out = run("--init without a terminal writes nothing", ask + ["--init"], home, env, results)
    machine = os.path.join(home, ".config", "ask", "machine.json")
    if os.path.exists(machine):
        results.append(("--init wrote without consent", False, 0, 0, machine))
        print("FAIL  --init wrote machine.json without --yes")
    # agy off before anything asks it (--init's roles step reads its list): no sign-in windows from the fresh home.
    os.makedirs(os.path.dirname(machine), exist_ok=True)
    with open(machine, "w", encoding="utf-8") as f:
        json.dump({"paths": {"agy": None}}, f)
    run("--init --machine personal --yes", ask + ["--init", "--machine", "personal", "--yes"], home, env, results)
    try:
        with open(machine, encoding="utf-8") as f:
            m = json.load(f)
        kept = m.get("paths", {}).get("agy", "missing") is None and m.get("context_paths", {}).get("~") == "personal"
        results.append(("--init kept paths and set the context", kept, 0, 0, json.dumps(m)[:120]))
        print(f"{'ok ' if kept else 'FAIL'}  --init kept paths and set the context: {json.dumps(m)[:120]}")
    except (OSError, ValueError) as e:
        results.append(("machine.json readable", False, -1, 0, str(e)))
    run("--list", ask + ["--list"], home, env, results)
    run("--status", ask + ["--status"], home, env, results)
    run("--roles (show only)", ask + ["--roles"], home, env, results)
    run("suite skills/ask", [py, "-B", "-m", "unittest"], os.path.join(root, "skills", "ask"), env, results)
    run("suite tools/test_broker.py", [py, "-B", "tools/test_broker.py"], root, env, results)
    run("suite mods/limits-band/test_band.py", [py, "-B", "mods/limits-band/test_band.py"], root, env, results)
    run("suite tools/test_pii_sweep.py", [py, "-B", "tools/test_pii_sweep.py"], root, env, results)
    claude = shutil.which("claude")
    if claude:
        # The CLI's own sign-in is not needed for these two; they read the plugin folder only.
        run("claude plugin validate", [claude, "plugin", "validate", root], home, dict(os.environ), results)
        run("claude plugin test", [claude, "plugin", "test", root], home, dict(os.environ), results)
    else:
        print("skip  claude plugin validate/test: no claude CLI on PATH")
    if a.live:
        codex_home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
        live_env = dict(env, CODEX_HOME=codex_home)
        code, out = run("live --role second-opinion (Codex)", ask + ["--role", "second-opinion", "Reply with one word: ok"],
                        home, live_env, results, timeout=900)
        if code == 0 and "ok" not in out.lower():
            results.append(("live answer", False, 0, 0, out[-120:]))
    status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True).stdout.strip()
    results.append(("the clone is clean after the tests", not status, 0, 0, status[:120]))
    print(f"{'ok ' if not status else 'FAIL'}  the clone is clean after the tests" + (f": {status[:120]}" if status else ""))
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} passed; the fresh home stays at {home}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
