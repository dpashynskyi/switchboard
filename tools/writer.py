"""Run briefs through interactive ACP/app-server, with reservations and a usage journal.

Exit codes:
- 0: ok; 1: missing required run context (sys.exit with a message).
- 2: Codex failed or usage/model error; 3: router waiting for local capacity.
- 4: the run never got going.
- 5: --max-tokens hard stop; 6: quota or router fallback; 7: router policy refusal.
- 8: router role unsupported on this machine; 9: --deadline passed. Legacy CLI process exit codes are also passed through.

--deadline counts agent time, excluding human waits: ACP session/cancel or Codex turn/interrupt,
then process-group kill after a grace; legacy CLI counts wall time and kills directly.

The default broker writes live progress, redacted protocol events and human requests beside --log.
--legacy-cli keeps the headless behavior documented below.

    python tools/writer.py --cwd <folder> --brief <BRIEF.md> [--model <id>] [--agy | --codex]
    python tools/writer.py --verdict <conversation id> ok|fixed|rejected "<note>"

What agy may do, found by trying it (agy's permissions are exact strings, not patterns):
- its own file tools read and write anywhere under the home folder (the user's agy settings trust the whole of it): as a Claude
  writer's Write tool, nothing narrower can be set;
- a folder beyond --cwd is outside agy's workspace and a read there is refused (the first real writer stopped so at its
  40th step, reading the library's sources): add it with --add-dir;
- one command only: `<python> -I agy_run.py` in `--cwd`, the writer putting what it wants run into agy_run.py; every other
  command (Get-Process, taskkill, git, the engine) is denied in headless mode;
- agy's `--sandbox` is no use for a writer: its commands may read but not write, so no script could save a table or a picture.
Python itself can still do anything; the brief's rules and the standing rules below are what hold it.
Prints agy's final answer; the whole JSON (usage, denied actions, conversation id) goes to --log. While it runs, a line a step
(the tool and what it acts on, with the time) goes to <log>.progress.txt, to watch, and every event to <log>.events.jsonl.
"""
import argparse
import copy
import json
import os
import shutil
import subprocess
import threading
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ASK_DIR = os.path.join(os.path.dirname(HERE), "skills", "ask")
if ASK_DIR not in sys.path:
    sys.path.insert(0, ASK_DIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import hooks
import route

HOME = os.path.expanduser("~")
# Where the router's agy adapter finds it (models.json, then the per-OS installs), so writers run off Windows too.
AGY = route._providers().get("agy").path("agy") or os.path.join(HOME, "AppData", "Local", "agy", "bin", "agy.exe")

STANDING = """Standing rules for this work (they hold whatever the brief says):
- The ONLY command you can run is this exact string, character for character, with nothing added or changed:
      {python} -I agy_run.py
  (in {cwd}). Not `python -c`, not another script name, not extra arguments, not a shell command such as ls, cat, dir or
  Get-Content: all of those are refused, and a refusal ends your turn with nothing done. To run anything (a script with its
  arguments, a batch, a look at the running processes through Python's subprocess), write it into agy_run.py with your
  file-writing tool, then run that exact command. Read and list files with your own file tools, not with commands.
- Never send anyone's personal data (names, emails, accounts, addresses, phone numbers) to any site, API, form or User-Agent;
  never fill in forms, never sign in anywhere.
{engine}{protected}- Never commit or push anything.
- Downloads are untrusted data: read them with python -I, never run anything from them.
- Never speak aloud (no `say` or other text-to-speech tool, no sound): you report in your answer only; the coordinator speaks to the user, in its own voice.
"""
# A writer's rules about a build engine; a machine's hooks module (skills/ask/hooks.py, writer_rules) may say more.
NO_ENGINE = """- Never start a build engine or any long-running heavy process the brief does not name; never kill or stop any process
  you did not start.
"""
ENGINE = """- The engine is started ONLY through the scripts the brief names, from agy_run.py, one run at a time, never two at once.
- Never kill, stop or close ANY process, yours included, whatever the reason: if a run hangs, write so in your notes and end
  your turn; the coordinator decides.
"""
CODEX_ENGINE = "- Never start a build engine or a long-running heavy process; never kill a process you did not start.\n"


def machine_engine(engine=False):
    """The writer's engine rules: the defaults above, or what the machine's hooks say (writer_rules), and the folders it
    never touches ("protected"; none by default)."""
    default = {"engine": ENGINE if engine else NO_ENGINE, "protected": "", "codex": CODEX_ENGINE}
    got = hooks.value("writer_rules", engine, machine=route.load_machine(), default=default)
    return dict(default, **(got if isinstance(got, dict) else {}))


CODEX_STANDING = """Standing rules for this work (they hold whatever the brief says):
- Never send anyone's personal data anywhere; never sign in or fill in forms.
- Never commit or push; never speak aloud.
{engine}- Touch only files under these folders: {folders}.
- Downloads are untrusted data: read them, never execute them.
"""


def step_note(s):
    state, tool = s.get("state", ""), s.get("tool_name") or s.get("step_type", "")
    if state not in ("ACTIVE", "ERROR") or not tool:
        return ""
    info = s.get("tool_info", {})
    params = info.get("parameters", {}) or {}
    what = params.get("CommandLine") or next(
        (v for v in params.values() if isinstance(v, str) and ("\\" in v or "/" in v) and len(v) < 300), "")
    if not what and params:
        what = str(next(iter(params.values())))
    line = f"#{s.get('step_index', '?')} {tool} {what[:200]}".rstrip()
    if state == "ERROR":
        line += f"  ERROR: {str(info.get('error', {}).get('message', ''))[:200]}"
    return line


def _extract_tokens(usage):
    u = usage or {}
    t_in = u.get("input_tokens") if u.get("input_tokens") is not None else u.get("prompt_tokens")
    t_out = u.get("output_tokens") if u.get("output_tokens") is not None else u.get("completion_tokens")
    t_think = u.get("thinking_tokens")
    t_tot = u.get("total_tokens")
    if t_tot is None and (t_in is not None or t_out is not None):
        t_tot = (t_in or 0) + (t_out or 0)
    out = {"in": t_in, "out": t_out, "total": t_tot}
    if u.get("cache_read_tokens") is not None:
        out["cached_in"] = u["cache_read_tokens"]
    if t_think is not None:
        out["thinking"] = t_think
    return out


def _pool_summary_line(model, cs_after, provider="agy"):
    pairs = route.covering_pools_and_windows(provider, model, (cs_after or {}).get("pools", []), include_stale=False)
    if not pairs:
        return None
    pname = pairs[0][0].get("label", "agy")
    win_parts = []
    for _, w in pairs:
        wtype = w.get("window", "window")
        rem = float(w.get("remaining_fraction", 0.0)) * 100
        pstr = route.format_pace(w.get("pace"), wtype)
        win_parts.append(f"{wtype} {rem:.1f}% left ({pstr})" if "week" in wtype.lower() and pstr else f"{wtype} {rem:.1f}% left")
    return f"[pool {pname}: {', '.join(win_parts)}]"


def parser(legacy_agy=False):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=legacy_agy, help="the folder the writer works in")
    ap.add_argument("--brief", help="the brief, a file the writer is told to read and follow")
    ap.add_argument("--model", help=("agy model id (default: gemini-4-argon-high); given again with --continue"
                                   if legacy_agy else "model id; given again with --continue"))
    ap.add_argument("--tokens", type=int, default=None, help="override estimated billed tokens for pre-run fit and reservation")
    ap.add_argument("--needs", type=route.parse_needs, default=[], help="comma-separated needs: python, engine, big-files")
    ap.add_argument("--force", action="store_true", help="run an explicitly named model even if its estimate crosses the floor")
    ap.add_argument("--python", help="the interpreter for agy_run.py (default: <cwd>/.venv's python, else python or python3)")
    ap.add_argument("--add-dir", action="append", default=[], help="another folder the writer may read and write (repeatable)")
    ap.add_argument("--continue", dest="conversation", help=("carry on an agy conversation by its id" if legacy_agy
                                                           else "carry on a conversation or thread by its id"))
    ap.add_argument("--log", help=("where the JSON result goes (default: <cwd>/writer_last.json)" if legacy_agy
                                 else "where the JSON result goes (default: <cwd>/writer_last.json)"))
    ap.add_argument("--engine", action="store_true", help="the writer may build and run the engine, through the brief's scripts only")
    ap.add_argument("--allow-never", action="store_true", help="allow a model in roles.json's never list")
    ap.add_argument("--role", default="writer",
                    help="the workload's role in roles.json (default: writer; analysis, engine-writer, ...)")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="hard stop: the writer is stopped once this run's billed tokens (fresh + cached x w) pass N; "
                         "carry on with --continue")
    ap.add_argument("--deadline", type=float, default=3600,
                    help="agent seconds before stopping (default: 3600; 0 disables); time spent waiting for a human answer does not count")
    if not legacy_agy:
        # The old headless CLIs (codex exec -s workspace-write) run without the broker's permission policy: a fallback
        # and a debugging aid, not offered in --help.
        ap.add_argument("--legacy-cli", action="store_true", help=argparse.SUPPRESS)
        ap.add_argument("--ask", choices=("human", "never"), default="human", help="broker approvals (default: human)")
        ap.add_argument("--ask-timeout", type=float, default=600, help="seconds to wait for a human (default: 600)")
        ap.add_argument("--answer", nargs="+", metavar="VALUE", help="<base> <option> [text]: answer a pending request")
        ap.epilog = "Broker: agy's usage is read from its transcript after each model step, so --max-tokens stops it there."
        providers = ap.add_mutually_exclusive_group()
        providers.add_argument("--agy", dest="provider", action="store_const", const="agy", help="use agy for --model")
        providers.add_argument("--codex", dest="provider", action="store_const", const="codex", help="use Codex for --model")
        ap.add_argument("--verdict", nargs=3, metavar=("ID", "VERDICT", "NOTE"), help="record ok, fixed or rejected for a writer run")
    ap.add_argument("text", nargs="?", default="", help="the message (with --continue), or added after the brief")
    return ap


def build_agy_command(model, prompt, settings, conversation=None, add_dirs=()):
    # Preserve agy's exact permission and streaming flags for existing callers.
    cmd = [AGY, "-p", prompt, "--model", model, "--settings", settings, "--mode", "accept-edits",
           "--output-format", "stream-json", "--print-timeout", "0s"]
    if conversation:
        cmd += ["--conversation", conversation]
    for directory in add_dirs:
        cmd += ["--add-dir", os.path.abspath(directory)]
    return cmd


def build_codex_command(model, prompt, cwd, answer_path, conversation=None, add_dirs=(), executable=None, effort=None):
    # Resume has no exec sandbox/directory flags; Popen supplies cwd instead.
    cmd = [executable or route.codex_path(), "exec"]
    if conversation:
        cmd += ["resume", conversation]
    cmd += ["-m", model, "-c", f"model_reasoning_effort={effort or 'high'}"]
    if not conversation:
        cmd += ["-s", "workspace-write", "-C", cwd, "--skip-git-repo-check"]
    cmd += ["--json", "-o", answer_path]
    if not conversation:
        for directory in add_dirs:
            cmd += ["--add-dir", os.path.abspath(directory)]
    cmd += ["--", prompt]
    return cmd


CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")


class AgyMeter:
    """Sum each step once, with cache weighted in the run's pool."""

    def __init__(self, pool=None, rules=None):
        self.steps = {}
        self.conversation = None
        self.pool, self.rules = pool, rules

    def consume(self, step):
        self.conversation = step.get("conversation_id") or self.conversation
        usage = step.get("usage")
        if usage:
            self.steps[step.get("step_index", len(self.steps))] = usage

    def usage(self):
        keys = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
        return {k: sum(int(u.get(k) or 0) for u in self.steps.values()) for k in keys}

    def fresh(self):
        return self.usage()["total_tokens"]

    def billed(self):
        usage = self.usage()
        return route.billed_tokens({"fresh": usage["total_tokens"], "cached_in": usage["cache_read_tokens"]},
                                   "agy", pool=self.pool, rules=self.rules)["billed"]


class CodexMeter:
    """A Codex thread's tokens while it runs: `codex exec --json` gives usage only when the turn ends, but the thread's
    rollout file in ~/.codex/sessions gets a token_count event after every model call."""

    def __init__(self, pool="ChatGPT", rules=None, home=None):
        self.path, self.offset, self.total = None, 0, None
        self.pool, self.rules = pool, rules
        self.home = home or CODEX_HOME  # the account's CODEX_HOME, where its rollouts are

    def read(self, thread_id):
        """Billed tokens of the whole thread so far, or None."""
        if not thread_id:
            return None
        if self.path is None:
            import glob
            hits = glob.glob(os.path.join(self.home, "sessions", "**", f"rollout-*{thread_id}.jsonl"), recursive=True)
            if not hits:
                return None
            self.path = max(hits, key=os.path.getmtime)
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return self.billed_of(self.total)
        cut = chunk.rfind(b"\n") + 1  # a line still being written waits for the next read
        self.offset += cut
        for line in chunk[:cut].decode("utf-8", errors="replace").splitlines():
            if '"token_count"' not in line:
                continue
            try:
                info = json.loads(line)["payload"]["info"]["total_token_usage"]
            except (ValueError, KeyError, TypeError):
                continue
            self.total = info
        return self.billed_of(self.total)

    def billed_of(self, total, baseline=None):
        if not total:
            return None
        # Difference components before rounding so resumed runs use the journal's unit.
        def part(key):
            return max(0, int(total.get(key) or 0) - int((baseline or {}).get(key) or 0))
        return route.billed_tokens({"in": part("input_tokens"), "cached_in": part("cached_input_tokens"),
                                    "out": part("output_tokens") + part("reasoning_output_tokens")},
                                   "codex", pool=self.pool, rules=self.rules)["billed"]

    @staticmethod
    def fresh_of(total):
        if not total:
            return None
        return route.billed_tokens({"in": total.get("input_tokens"), "cached_in": total.get("cached_input_tokens"),
                                    "out": int(total.get("output_tokens") or 0) + int(total.get("reasoning_output_tokens") or 0)},
                                   "codex", rules={})["fresh"]


def stop_tree(process):
    """Stop the writer and whatever it started (Windows: taskkill /T; elsewhere its process group, which it was started as
    its own)."""
    if process.poll() is not None:
        return
    if sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        return
    import signal
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()


class CodexEvents:
    """Keep turn usage separate from agy's running conversation totals."""

    def __init__(self):
        self.conversation = None
        self.answer = []
        self.tokens = {"in": None, "out": None, "total": None, "cached_in": None}
        self.failed = False
        self.quota = False

    def consume(self, event):
        kind = event.get("type")
        notes = []
        if kind == "thread.started":
            self.conversation = event.get("thread_id")
        elif kind in ("item.started", "item.completed"):
            item = event.get("item") or {}
            item_type = item.get("type")
            if item_type == "command_execution":
                state = item.get("status") or kind
                detail = f"command {item.get('command', '')} [{state}"
                if item.get("exit_code") is not None:
                    detail += f", exit {item['exit_code']}"
                notes.append(detail + "]")
            elif item_type == "file_change" and kind == "item.completed":
                notes += [f"file {change.get('kind', '')} {change.get('path', '')}"
                          for change in item.get("changes") or []]
            elif item_type == "agent_message" and kind == "item.completed":
                self.answer.append(item.get("text") or "")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            values = {"in": usage.get("input_tokens"), "cached_in": usage.get("cached_input_tokens"),
                      "out": (usage.get("output_tokens") or 0) + (usage.get("reasoning_output_tokens") or 0)}
            for key, value in values.items():
                if value is not None:
                    self.tokens[key] = (self.tokens[key] or 0) + value
            self.tokens["total"] = (self.tokens["in"] or 0) + (self.tokens["out"] or 0)
        if kind in ("error", "turn.failed"):
            self.failed = True
            self.quota = self.quota or route.is_quota_error(1, json.dumps(event))
            notes.append("ERROR: " + json.dumps(event, ensure_ascii=False))
        self.quota = self.quota or self._limit_reached(event)
        return notes

    @staticmethod
    def _limit_reached(value):
        if isinstance(value, dict):
            return (("rateLimitReachedType" in value and value["rateLimitReachedType"] is not None)
                    or any(CodexEvents._limit_reached(v) for v in value.values()))
        return isinstance(value, list) and any(CodexEvents._limit_reached(v) for v in value)


def _score_kw(decision):
    return {key: decision[key] for key in ("score_pick", "score_notes", "score_table")
            if decision.get(key) is not None}


def _say_score(decision, color):
    for note in decision.get("score_notes", []):
        print(route.dim(f"[score: {note}]", color), file=sys.stderr)
    if decision.get("score_mode") == "shadow" and decision.get("score_pick"):
        print(route.dim(f"[score would pick: {decision['score_pick']}]", color), file=sys.stderr)


def main(argv=None, legacy_agy=False):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = parser(legacy_agy)
    a = ap.parse_args(argv)
    if a.deadline < 0:
        ap.error("--deadline must be nonnegative")
    broker_mode = not legacy_agy and not a.legacy_cli
    if not legacy_agy and a.answer is not None:
        import broker
        if len(a.answer) not in (2, 3):
            ap.error("--answer needs <base> <option> [text]")
        try:
            pending = broker.answer_pending(*a.answer)
        except ValueError as error:
            ap.error(str(error))
        print(f"Answered {pending['kind']} {a.answer[1]} for {pending['run']}")
        return 0
    if broker_mode and a.ask_timeout < 0:
        ap.error("--ask-timeout must be nonnegative")
    provider = "agy" if legacy_agy else a.provider
    script = "writer.py"
    if not legacy_agy and a.verdict:
        try:
            entry = route.writer_verdict(*a.verdict, model=a.model, provider=provider)
        except ValueError as error:
            ap.error(str(error))
        print(f"[{entry['provider']} {entry['model']}: conversation {entry['conversation']}, verdict {entry['verdict']}]")
        return 0
    if not a.cwd:
        ap.error("--cwd is required for a writer run")
    if a.conversation and not a.model:
        sys.exit("--continue needs --model, the model the conversation began on")
    if provider and not a.model and not legacy_agy:
        ap.error(f"--{provider} needs --model")
    if not a.conversation and not a.brief and not legacy_agy:
        sys.exit("--brief is needed for a new writer")
    color_out, color_err = route.use_color(sys.stdout), route.use_color(sys.stderr)
    cwd = os.path.abspath(a.cwd)
    venvs = [os.path.join(cwd, ".venv", "Scripts", "python.exe"), os.path.join(cwd, ".venv", "bin", "python")]
    python = a.python or next((v for v in venvs if os.path.exists(v)), None) or (
        "python" if os.name == "nt" else (shutil.which("python") or "python3"))
    rules = route.load_roles()
    role = a.role
    if role not in rules.get("roles", {}):
        ap.error(f"unknown role {role!r} (roles: {', '.join(rules.get('roles', {}))})")
    needs = route.role_needs(role, a.needs + (["engine"] if a.engine else []), rules)
    if a.engine and rules["roles"][role].get("no_engine"):
        # A role that writes without the engine; engine-writer or the session builds and runs.
        ap.error(f"role {role!r} works without the engine: --engine is refused (use --role engine-writer)")
    # The subagent a role ends on, for the line that sends the work to the session.
    subagent = next((mid for prov, mid in (route.resolve_model(c, rules=rules) for c in route._role_spec(role, rules)["models"])
                     if prov == "claude-subscription"), None) or "claude-opus-5-5"
    if legacy_agy:
        # Legacy sessions must never switch providers when the shared role changes.
        rules = copy.deepcopy(rules)
        rules["roles"][role]["models"] = route.filter_role(
            rules["roles"][role]["models"], lambda m: m.get("provider") in ("agy", "claude-subscription"))
    if a.model:
        spec = {"provider": provider, "model": a.model} if provider else a.model
        chk = route.check_name(spec, rules=rules, allow_never=a.allow_never, role=role,
                               tokens=a.tokens, force=a.force, agy_path=AGY, needs=needs)
        if not chk["ok"]:
            if chk["status"] == "policy":
                print(route.red(chk["reason"], color_err), file=sys.stderr)
                route.journal(script, role, a.model, chk.get("provider"), a.model, status="refused",
                              context=chk.get("context"), account=chk.get("account"))
                sys.exit(route.EXIT_POLICY)
            if chk["status"] in ("never", "not_offered", "error", "ambiguous", "cannot"):
                print(route.red(chk["reason"], color_err), file=sys.stderr)
                sys.exit(2)
            print(route.red(f"{chk['reason']}; use a Claude subagent ({subagent})", color_err), file=sys.stderr)
            route.journal(script, role, a.model, chk.get("provider"), a.model,
                          seconds=0.0, status="quota", pool=chk.get("pool"))
            sys.exit(6)
        decision = chk
    else:
        decision = route.pick(role, rules=rules, allow_never=a.allow_never, tokens=a.tokens, needs=needs)
        _say_score(decision, color_err)
        for note in decision.get("experience", []):
            print(route.dim(f"[experience: {note}]", color_err), file=sys.stderr)
        for reason in decision.get("passed_over", []):
            print(route.dim(f"[passed over: {reason}]", color_err), file=sys.stderr)
        if decision.get("fallback") or not decision.get("picked"):
            print(route.yellow(decision.get("message") or f"use a Claude subagent ({subagent})", color_err), file=sys.stderr)
            if decision.get("fallback"):
                route.journal(script, role, decision.get("model"), decision.get("provider"), decision.get("model"),
                              seconds=0.0, status="fallback", pool=decision.get("pool"), **_score_kw(decision))
            sys.exit(decision.get("exit_code", 6))
    model, provider = decision["model"], decision["provider"]
    if provider == "claude-subscription":
        print(route.yellow(f"use a Claude subagent ({model})", color_err), file=sys.stderr)
        route.journal(script, role, model, provider, model, status="fallback", pool=decision.get("pool"), **_score_kw(decision))
        return 6
    if provider not in ("agy", "codex"):
        ap.error("writers require an agy or codex model")
    if decision.get("warning"):
        print(route.yellow(f"[{decision['warning']}]", color_err), file=sys.stderr)
    if decision.get("crossed"):
        pol = route.policy_for(provider, context=decision.get("context"), account=decision.get("account"))
        print(route.yellow(f"[{route.crossing_line(pol)}]", color_err), file=sys.stderr)
    pol_kw = {"context": decision.get("context"), "account": decision.get("account"),
              "crossed": bool(decision.get("crossed")), **_score_kw(decision)}
    if decision.get("why"):
        pol_kw["why"] = decision["why"]
        x = decision.get("expiry") or {}
        if x:
            print(route.dim(f"[expires first: {x.get('pool')} {x.get('window')} {x.get('remaining', 0) * 100:.1f}% left, "
                            f"resets in {x.get('resets_in') or '?'}]", color_err), file=sys.stderr)
    pname = decision.get("pool") or provider
    log = a.log or os.path.join(cwd, "writer_last.json")
    base = log[:-5] if log.endswith(".json") else log
    answer_path = os.path.abspath(base + ".answer.txt")
    dirs = [cwd] + [os.path.abspath(d) for d in a.add_dir]
    if broker_mode and provider == "agy":
        standing_template = STANDING[:STANDING.index("- The ONLY")] + (
            "- Prefer the exact command {python} -I agy_run.py (in {cwd}); it is allowed at once. "
            "Any other command or a file outside the work folders goes to a human for approval and may be refused. "
            "Read and list files with your own file tools.\n") + STANDING[STANDING.index("- Never send"):]
    else:
        standing_template = STANDING
    machine = machine_engine(a.engine)
    engine_rules = machine["engine"]
    if a.conversation:
        prompt = a.text
        if provider == "codex":
            prompt += "\n\n" + CODEX_STANDING.format(folders=", ".join(dirs), engine=machine["codex"])
        elif broker_mode:
            prompt += "\n\n" + standing_template.format(python=python, cwd=cwd, engine=engine_rules, protected=machine["protected"])
    else:
        if not a.brief:
            sys.exit("--brief is needed for a new writer")
        standing = (standing_template.format(python=python, cwd=cwd, engine=engine_rules, protected=machine["protected"])
                    if provider == "agy" else CODEX_STANDING.format(folders=", ".join(dirs), engine=machine["codex"]))
        prompt = (f"Your brief is the file {os.path.abspath(a.brief)}. Read it first and follow it exactly.\n\n"
                  + standing + ("\n" + a.text if a.text else ""))
    if broker_mode:
        import broker
        cmd = ([AGY, "acp"] if provider == "agy" else
               [route.codex_path(), "app-server", "--enable", "default_mode_request_user_input"])
        if provider == "agy":
            for directory in a.add_dir:
                cmd += ["--add-dir", os.path.abspath(directory)]
    elif provider == "agy":
        settings = os.path.join(cwd, "writer_agy_settings.json")
        allow = [f"command({python} -I agy_run.py)"]
        allow += [f"{act}({d}\\*)" for d in dirs for act in ("read_file", "write_file")]
        with open(settings, "w", encoding="utf-8") as handle:
            json.dump({"permissions": {"allow": allow}}, handle)
        cmd = build_agy_command(model, prompt, settings, a.conversation, a.add_dir)
    else:
        print(route.dim("[Codex's Windows sandbox could not start Python in two runs today; the caller runs the tests.]", color_err),
              file=sys.stderr)
        # A failed launch must not print a previous run's answer.
        if os.path.exists(answer_path):
            os.remove(answer_path)
        cmd = build_codex_command(model, prompt, cwd, answer_path, a.conversation, a.add_dir, effort=decision.get("effort"))

    # The account the policy chose: another Codex account than CODEX_HOME runs with its own folder.
    codex_home = route.account_home(decision.get("account")) if provider == "codex" else None
    codex_env = dict(os.environ, CODEX_HOME=codex_home) if codex_home else None
    if codex_home:
        print(route.dim(f"[account {decision['account']}: CODEX_HOME={codex_home}]", color_err), file=sys.stderr)
    cs_before = route.cloud_state(agy_path=AGY, rules=rules, account=decision.get("account"))
    before_fracs = route.pool_fractions_for_model(provider, model, cs_before)
    result, other, raw_lines = {}, [], []
    codex = CodexEvents()
    agy_meter, codex_meter = AgyMeter(pname, rules), CodexMeter(pname, rules, home=codex_home)
    cap, stopped, told = a.max_tokens, [], []
    expired = []
    codex_base = None
    if not broker_mode and provider == "codex" and a.conversation:
        codex_meter.read(a.conversation)  # a resumed thread's rollout already holds its earlier turns
        codex_base = codex_meter.total
    started = time.time()
    fallback = False
    driver = None
    returncode = 4
    res_entry = route.reserve_quota(script, role, provider, model, tokens=a.tokens, rules=rules, cs=cs_before, force=a.force)
    if res_entry and res_entry.get("refused"):
        print(route.red(res_entry["refused"] + " (--force runs anyway)", color_err), file=sys.stderr)
        route.journal(script, role, model, provider, model, seconds=0.0, status="quota", pool=pname)
        return 6
    try:
        with open(base + ".progress.txt", "w", encoding="utf-8") as pf, open(base + ".events.jsonl", "w", encoding="utf-8") as ef:
            def progress(note):
                if broker_mode:
                    note = broker.redact(note)
                line = f"{time.strftime('%H:%M:%S')} {note}"
                pf.write(line + "\n")
                pf.flush()
                if color_err and not (broker_mode and note.startswith("WAITING ")):
                    print(route.red(line, color_err) if "ERROR:" in note else route.dim(line, color_err),
                          file=sys.stderr, flush=True)

            progress(f"start, {provider} {model}, in {cwd}")
            if broker_mode:
                log_lock = threading.RLock()
                old_progress = progress

                def progress(note):
                    with log_lock:
                        old_progress(broker.redact(note))

                def event(direction, message):
                    with log_lock:
                        clean = broker.redact({"dir": direction, "msg": message})
                        line = json.dumps(clean, ensure_ascii=False)
                        ef.write(line + "\n")
                        ef.flush()
                        raw_lines.append(line + "\n")

                cls = broker.AcpDriver if provider == "agy" else broker.CodexAppDriver
                opts = {"reasoning_summary": "detailed"} if provider == "codex" else {}
                if provider == "codex" and decision.get("effort"):
                    opts["effort"] = decision["effort"]  # what the role's wish chose (">=high" -> high)
                driver = cls(cwd, model, base, progress, event, stop_tree, **opts, add_dirs=a.add_dir, python=python,
                             engine=a.engine, ask=a.ask, ask_timeout=a.ask_timeout, max_tokens=cap, command=cmd,
                             deadline=a.deadline or None, env=codex_env,
                             engine_markers=hooks.engine_markers(route.load_machine()),
                             billed=lambda tok: route.billed_tokens(tok, provider, pool=pname, rules=rules)["billed"])
                reasoning_log = None
                known = driver.known

                def open_reasoning(cid):
                    nonlocal reasoning_log
                    if cid and reasoning_log is None:
                        reasoning_log = broker.ReasoningLog(os.path.join(route.CACHE_DIR, "reasoning"), cid, provider, model)
                        driver.reasoning_sink = reasoning_log.write

                def remember(cid):
                    open_reasoning(cid)
                    known(cid)

                driver.known = remember
                open_reasoning(a.conversation)
                result = None
                try:
                    result = driver.run(prompt, a.conversation)
                    returncode = 0
                except broker.ResumeError as error:
                    progress("ERROR: Codex resume failed; falling back to --legacy-cli: " + str(error))
                    print(broker.redact("[Codex resume failed; falling back to --legacy-cli: " + str(error) + "]"),
                          file=sys.stderr)
                    fallback = True
                except (OSError, RuntimeError, TimeoutError) as error:
                    other.append(broker.redact(str(error)))
                    progress("ERROR: " + str(error))
                    driver.failed = True
                    result = driver.result()
                finally:
                    driver.close()
                    if reasoning_log is not None:
                        path = reasoning_log.close(driver.reasoning_kind)
                        # last.json stays /ask's: a background writer must not take over "the last conversation".
                        if isinstance(result, dict):
                            result["reasoning_path"] = path
                        print(f"[reasoning: {path}]", file=sys.stderr, flush=True)
                if isinstance(result, dict):
                    result.pop("reasoning", None)
                    result.pop("reasoning_kind", None)
                if driver.timed_out is not None:
                    expired.append(driver.timed_out)
                if driver.capped is not None:
                    stopped.append(driver.capped)
                codex.failed = driver.failed if provider == "codex" else False
                codex.quota = driver.quota or any(route.is_quota_error(1, error) for error in driver.errors)
                other.extend(broker.redact(driver.errors))
                if provider == "codex":
                    codex.conversation, codex.tokens = driver.conversation, driver.tokens
                result = broker.redact(result)
            else:
                timer = None
                try:
                    process = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL if provider == "codex" else None,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                               encoding="utf-8", errors="replace", env=codex_env,
                                               start_new_session=not sys.platform.startswith("win"))
                    ended, stop_lock = [], threading.Lock()
                    if a.deadline:
                        process_started = time.monotonic()
                        def expire():
                            with stop_lock:
                                if ended or process.poll() is not None or stopped:
                                    return
                                expired.append(round(time.monotonic() - process_started, 2))
                            progress(f"ERROR: deadline {expired[0]}s passed (legacy path: wall time); stopping the process group")
                            stop_tree(process)
                        timer = threading.Timer(a.deadline, expire)
                        timer.daemon = True
                        timer.start()
                    if provider == "codex" and cap:
                        def watch():
                            while process.poll() is None:
                                time.sleep(5)
                                codex_meter.read(codex.conversation or a.conversation)
                                now = codex_meter.billed_of(codex_meter.total, codex_base)
                                if now is not None and now > cap:
                                    with stop_lock:
                                        if expired:
                                            return
                                        stopped.append(now)
                                    stop_tree(process)
                                    return
                        threading.Thread(target=watch, daemon=True).start()
                    seen = set()
                    for raw in process.stdout:
                        raw_lines.append(raw)
                        line = raw.rstrip("\n")
                        try:
                            event = json.loads(line)
                        except ValueError:
                            other.append(line)
                            progress("note: " + line[:300])
                            continue
                        ef.write(line + "\n")
                        ef.flush()
                        if not isinstance(event, dict):
                            continue
                        notes = []
                        if provider == "codex":
                            notes = codex.consume(event)
                        elif event.get("event") == "result":
                            result = event.get("result") or {}
                        elif event.get("event") == "step_update":
                            step = event.get("step_update") or {}
                            agy_meter.consume(step)
                            note = step_note(step)
                            if note and note not in seen:
                                seen.add(note)
                                notes.append(note)
                        known = agy_meter.conversation or codex.conversation
                        if known and not told:
                            # Printed as soon as it is known, so a stopped or lost run can still be carried on.
                            told.append(known)
                            progress(f"conversation {known}")
                            print(route.dim(f"[conversation {known}: carry on with --continue {known} --model {model}]",
                                            color_err), file=sys.stderr, flush=True)
                        for note in notes:
                            progress(note)
                        if provider == "agy" and cap and not stopped and agy_meter.billed() > cap:
                            with stop_lock:
                                if not expired:
                                    stopped.append(agy_meter.billed())
                            if stopped:
                                stop_tree(process)
                    process.wait()
                    with stop_lock:
                        ended.append(True)
                    if timer:
                        timer.cancel()
                        timer.join()
                    if stopped:
                        progress(f"ERROR: hard stop at {stopped[0]:,} billed tokens (fresh + cached x w), over --max-tokens {cap:,}")
                    returncode = process.returncode
                except OSError as error:
                    raw_lines.append(str(error))
                    other.append(str(error))
                    progress("ERROR: " + str(error))
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()
                if provider == "codex":
                    response = "\n\n".join(codex.answer).strip()
                    if os.path.exists(answer_path):
                        with open(answer_path, encoding="utf-8", errors="replace") as handle:
                            response = handle.read().strip() or response
                    result = {"response": response, "conversation_id": codex.conversation or a.conversation,
                              "usage": codex.tokens, "failed": codex.failed}
    finally:
        route.release_reservation(res_entry)

    if fallback:
        return main(list(sys.argv[1:] if argv is None else argv) + ["--legacy-cli"])
    elapsed = time.time() - started
    raw_all = "".join(raw_lines)
    raw_log_path = route.save_raw_log(model, raw_all)
    if not result:
        result = {"raw": "\n".join(other)[-4000:]}
    with open(log, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
    cs_after = route.cloud_state(agy_path=AGY, force_refresh=True, rules=rules)
    after_fracs = route.pool_fractions_for_model(provider, model, cs_after)
    conv_id = result.get("conversation_id") or agy_meter.conversation or codex.conversation or a.conversation
    conv_tokens = None
    if provider == "agy":
        usage = result.get("usage")
        if broker_mode or not usage:
            # ACP gives per-turn usage; legacy missing-result steps give a run sum.
            # Stopped before agy's result: the steps' sum is this run; a carried-on conversation's total is running.
            usage = (result.get("usage") or driver.usage) if broker_mode else agy_meter.usage()
            prev = route.load_agy_conversations().get(str(conv_id or "")) if a.conversation else None
            if prev:
                usage = {"input_tokens": usage["input_tokens"] + int(prev.get("in") or 0),
                         "output_tokens": usage["output_tokens"] + int(prev.get("out") or 0),
                         "thinking_tokens": usage["thinking_tokens"] + int(prev.get("thinking") or 0),
                         "cache_read_tokens": usage["cache_read_tokens"] + int(prev.get("cached") or 0),
                         "total_tokens": usage["total_tokens"] + int(prev.get("total") or 0)}
        tokens, conv_tokens, _ = route.record_conversation_usage(
            conv_id, _extract_tokens(usage), carried_on=bool(a.conversation))
    else:
        tokens = codex.tokens
        if tokens.get("total") is None and codex_meter.total:
            # Stopped before the turn ended: this run is the rollout's total less what the thread held before.
            def part(key):
                return int(codex_meter.total.get(key) or 0) - int((codex_base or {}).get(key) or 0)
            t_in, t_out = part("input_tokens"), part("output_tokens") + part("reasoning_output_tokens")
            tokens = {"in": t_in, "out": t_out, "total": t_in + t_out, "cached_in": part("cached_input_tokens")}
    # (Only agy's own notes are read for a quota failure, and only when no answer came: the event stream holds the writer's
    # work, whose text may say "429" or "limit"; three finished writers were journalled as quota so.)
    answered = bool(str(result.get("response") or "").strip())
    quota = not stopped and not expired and (codex.quota or (not answered and route.is_quota_error(returncode or 1, "\n".join(other))))
    status = "capped" if stopped else "deadline" if expired else "quota" if quota else ("error" if returncode or codex.failed else "ok")
    route.journal(script, role, model, provider, model, tokens=tokens, seconds=elapsed, status=status,
                  log=raw_log_path, pool=pname, before=before_fracs, after=after_fracs,
                  conversation=conv_id, conversation_tokens=conv_tokens, rules=rules, **pol_kw)
    print(result.get("response", result.get("raw", "")))
    unit = route.billed_tokens(tokens, provider, pool=pname, rules=rules)
    tok_disp = f"{unit['billed']} billed tokens (fresh {unit['fresh']}, cached {unit['cached']})"
    outcome = f"denied {result.get('denied_actions', [])}"
    if provider == "codex":
        outcome = f"failed {bool(returncode or codex.failed or codex.quota)}"
    with open(base + ".progress.txt", "a", encoding="utf-8") as pf:
        progress(f"end, {tok_disp}, {outcome}")
    print(f"\n[{provider} {route.bold(model, color_out)}: conversation {conv_id}, {tok_disp}, {outcome}, log {log}]")
    pool_line = _pool_summary_line(model, cs_after, provider)
    if pool_line:
        print(route.dim(pool_line, color_out))
    if stopped:
        print(route.red(f"[hard stop: {stopped[0]:,} billed tokens (fresh + cached x w), over --max-tokens {cap:,}; the work so far is in "
                        f"{cwd}; carry on with --continue {conv_id} --model {model}]", color_out))
        return 5
    if expired:
        message = (f"[deadline: {expired[0]}s {'of agent time ' if broker_mode else ''}passed, over --deadline {a.deadline:g}s; "
                   f"the work so far is in {cwd}; carry on with --continue {conv_id} --model {model}]")
        print(route.red(message, color_out))
        print(route.red(message, color_err), file=sys.stderr)
        return 9
    if broker_mode and quota:
        return 6
    return returncode or (2 if codex.failed or codex.quota else 0)


if __name__ == "__main__":
    sys.exit(main())
