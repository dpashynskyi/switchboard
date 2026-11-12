"""Pass the user's question to another model, word for word, and print its answer as it came: the models of the Antigravity CLI
(`agy`, a Google account's quota), Codex (ChatGPT plan), and the local ones on Ollama.

    python ~/switchboard/skills/ask/ask.py gemini-3.8-flash-high "Why ..."
    python ~/switchboard/skills/ask/ask.py --role review --file plan.md "Review this"
    python ~/switchboard/skills/ask/ask.py claude-opus-5-5-high --tokens 120000 "Question" # override token estimate
    python ~/switchboard/skills/ask/ask.py claude-opus-5-5-high --force "Question" # run even if estimate crosses floor
    python ~/switchboard/skills/ask/ask.py claude-opus-5-5-high --continue "And if ..." # the same conversation, carried on
    python ~/switchboard/skills/ask/ask.py gemma4:12b --file notes.md "Summarise" # a file appended after the question
    python ~/switchboard/skills/ask/ask.py qwen3:14b --wait "Index this" # wait until GPU is free
    python ~/switchboard/skills/ask/ask.py gemma4:12b --keep 10m "Question 1" # keep model in VRAM for a series
    python ~/switchboard/skills/ask/ask.py --unload # unload whatever Ollama holds
    python ~/switchboard/skills/ask/ask.py --status # all pools, reservations, GPU, Ollama, 24h journal
    python ~/switchboard/skills/ask/ask.py --list # live models from agy, Ollama, subscription
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import route
import providers as adapters
import handoff
import hooks
import init_config

HOME = os.path.expanduser("~")
CONFIG = route.CONFIG_PATH

AGY = route.AGY_DEFAULT
OLLAMA = route.OLLAMA_DEFAULT
ASK_ROOM = route.ASK_ROOM
AGY_BRAIN = os.path.join(HOME, ".gemini", "antigravity-cli", "brain")
DRAW_SETTINGS = '{"permissions":{"allow":["invoke_subagent(*)","write_file(*)","subagent(*)"]}}'


def load_config():
    return route.load_config()


def parse_duration_s(val):
    if val is None:
        return 7200
    s = str(val).strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("s"):
        return int(float(s[:-1]))
    return int(float(s))


def step_note(s):
    state, tool = s.get("state", ""), s.get("tool_name") or s.get("step_type", "")
    if state not in ("ACTIVE", "ERROR") or not tool:
        return ""
    info = s.get("tool_info", {}) or {}
    params = info.get("parameters", {}) or {}
    what = params.get("CommandLine") or next(
        (v for v in params.values() if isinstance(v, str) and ("\\" in v or "/" in v) and len(v) < 300), "")
    if not what and params:
        what = str(next(iter(params.values())))
    line = f"#{s.get('step_index', '?')} {tool} {what[:200]}".rstrip()
    if state == "ERROR":
        line += f"  ERROR: {str(info.get('error', {}).get('message', ''))[:200]}"
    return line


RETRY_NO_TOOLS_PROMPT = "Answer in words only, from what you know; do not use any tool or command."


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


def _parse_agy_details(stdout_text, stderr_text=""):
    """Extract (response, tokens_dict, conversation_id, denied_actions) from agy JSON or stream-json output."""
    s = (stdout_text or "").strip()
    if s:
        # Handle stream-json (multiple JSON lines) or single JSON object
        lines = [ln.strip() for ln in s.splitlines() if ln.strip().startswith("{")]
        for cand in reversed(lines if len(lines) > 1 else [s[s.find("{"):] if "{" in s else ""]):
            if not cand:
                continue
            try:
                obj = json.loads(cand)
                if obj.get("event") and obj.get("event") != "result":
                    continue
                res = obj.get("result") if isinstance(obj.get("result"), dict) else obj
                resp = res.get("response")
                if resp is None:
                    resp = obj.get("response", "")
                tok = _extract_tokens(res.get("usage") or obj.get("usage") or {})
                conv_id = str(res.get("conversation_id") or obj.get("conversation_id") or "").strip()
                denied = list(res.get("denied_actions") or obj.get("denied_actions") or [])
                return str(resp).strip(), tok, conv_id, denied
            except Exception:
                continue
    fallback = s or (stderr_text or "").strip()
    return fallback, {"in": None, "out": None, "total": None}, "", []


def _parse_agy_json(stdout_text, stderr_text):
    resp, tok, _, _ = _parse_agy_details(stdout_text, stderr_text)
    tokens = {"in": tok.get("in"), "out": tok.get("out"), "total": tok.get("total")}
    if "cached_in" in tok:
        tokens["cached_in"] = tok["cached_in"]
    return resp, tokens


def ask_agy(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
    os.makedirs(ASK_ROOM, exist_ok=True)
    fmt = "stream-json" if verbose else "json"
    # The question goes by stdin, never on the command line: Windows caps a command line at ~32K characters,
    # and a question with a file appended overran it (WinError 206). Without -p agy reads stdin.
    args = [agy, "--model", model, "--output-format", fmt, "--print-timeout", "%ds" % timeout_s, "--sandbox"]
    if isinstance(carry_on, str) and carry_on:
        args.extend(["--conversation", carry_on])
    elif carry_on:
        args.insert(1, "--continue")
    question = text.encode("utf-8")

    if not verbose:
        try:
            done = subprocess.run(args, input=question, capture_output=True, timeout=timeout_s + 60, cwd=ASK_ROOM)
            raw_out = done.stdout.decode("utf-8", errors="replace")
            raw_err = done.stderr.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as e:
            # The process is killed by subprocess.run; what it printed so far is kept, and the call is a timeout.
            done = subprocess.CompletedProcess(args, 124, e.stdout or b"", e.stderr or b"")
            raw_out = (e.stdout or b"").decode("utf-8", errors="replace")
            raw_err = (e.stderr or b"").decode("utf-8", errors="replace") + f"\nprint timeout after {timeout_s + 60} s"
        raw_all = raw_out + ("\n--- stderr ---\n" + raw_err if raw_err.strip() else "")
        log_path = route.save_raw_log(model or name, raw_all)
        resp, tokens = _parse_agy_json(raw_out, raw_err)
        if done.returncode != 0 and not resp:
            resp = raw_err.strip() or raw_out.strip()
        return done.returncode, resp, tokens, log_path, raw_all

    color = route.use_color(sys.stderr)
    p = subprocess.Popen(args, cwd=ASK_ROOM, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")

    def feed():
        # In a thread: a long question and an agy that prints while still reading it would otherwise deadlock on the pipes.
        try:
            p.stdin.write(text)
        except (OSError, ValueError, UnicodeError):  # agy ended before reading the question, or a stray surrogate in it
            pass
        finally:
            try:
                p.stdin.close()  # EOF whatever happened: agy must not wait for the rest of the question
            except OSError:
                pass
    import threading
    threading.Thread(target=feed, daemon=True).start()
    raw_lines, other, result, seen = [], [], {}, set()
    try:
        for line in p.stdout:
            raw_lines.append(line)
            sline = line.rstrip("\n")
            if not sline.startswith("{"):
                other.append(sline)
                print(route.dim(f"  note: {sline[:240]}", color), file=sys.stderr, flush=True)
                continue
            try:
                ev = json.loads(sline)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "result":
                result = ev.get("result", {}) or {}
            elif ev.get("event") == "step_update":
                note = step_note(ev.get("step_update", {}) or {})
                if note and note not in seen:
                    seen.add(note)
                    styled = route.red(f"  {note}", color) if "ERROR:" in note else route.dim(f"  {note}", color)
                    print(styled, file=sys.stderr, flush=True)
        p.wait()
    except BaseException:  # Ctrl-C or a broken pipe, in the loop or the wait: agy must not outlive the call
        p.kill()
        raise
    raw_all = "".join(raw_lines)
    log_path = route.save_raw_log(model or name, raw_all)
    resp = (result.get("response") or "\n".join(other)).strip()
    tok = _extract_tokens(result.get("usage") or {})
    tokens = {"in": tok.get("in"), "out": tok.get("out"), "total": tok.get("total")}
    if "cached_in" in tok:
        tokens["cached_in"] = tok["cached_in"]
    return p.returncode, resp, tokens, log_path, raw_all


def is_timeout(raw):
    """The provider ran out of its time with the turn unfinished: agy's print timeout (its answer, if any, is partial)
    or Codex killed after --timeout. Journalled as "timeout", which experience.py counts against the model
    (three reviews by gemini-4-argon-high hit the 5-minute print timeout)."""
    return bool(re.search(r'"reason"\s*:\s*"print_timeout"|print timeout after|Codex timed out after', raw or ""))


def _stdout_part(raw_all):
    """The stdout of a raw log: the stderr part after the marker holds its own JSON lines, which are not the result."""
    return (raw_all or "").split("\n--- stderr ---\n", 1)[0]


def _run_agy_with_retry_and_accounting(agy, model, text, carry_on, timeout_s, name="agy", verbose=False, conversations_path=None):
    """Run ask_agy, track per-conversation token deltas in agy_conversations.json, and if response is empty
    while denied_actions is non-empty, continue the same conversation once with RETRY_NO_TOOLS_PROMPT.
    """
    code, out, tok1, log_path, raw_all = ask_agy(agy, model, text, carry_on, timeout_s, name=name, verbose=verbose)
    _, parsed_tok1, conv_id1, denied1 = _parse_agy_details(_stdout_part(raw_all), "")
    use_tok1 = parsed_tok1 if parsed_tok1.get("total") is not None else (tok1 or {})
    run1_tok, conv1_tok, j_conv_id = route.record_conversation_usage(
        conv_id1, use_tok1, carried_on=bool(carry_on), conversations_path=conversations_path
    )

    retried = False
    if not (out or "").strip() and denied1 and not route.is_quota_error(code, raw_all):
        retried = True
        cont_arg = conv_id1 if conv_id1 else True
        code2, out2, tok2, log_path2, raw_all2 = ask_agy(
            agy, model, RETRY_NO_TOOLS_PROMPT, cont_arg, timeout_s, name=name, verbose=verbose
        )
        _, parsed_tok2, conv_id2, _ = _parse_agy_details(_stdout_part(raw_all2), "")
        use_tok2 = parsed_tok2 if parsed_tok2.get("total") is not None else (tok2 or {})
        cid = conv_id2 or conv_id1
        run2_tok, conv2_tok, _ = route.record_conversation_usage(
            cid, use_tok2, carried_on=True, conversations_path=conversations_path
        )
        t_in = (run1_tok.get("in") or 0) + (run2_tok.get("in") or 0)
        t_out = (run1_tok.get("out") or 0) + (run2_tok.get("out") or 0)
        t_tot = (run1_tok.get("total") or 0) + (run2_tok.get("total") or 0)
        final_tokens = {"in": t_in, "out": t_out, "total": t_tot}
        if "cached_in" in run1_tok or "cached_in" in run2_tok:
            final_tokens["cached_in"] = (run1_tok.get("cached_in") or 0) + (run2_tok.get("cached_in") or 0)
        final_conv_tokens = conv2_tok if carry_on else None
        final_conv_id = cid if carry_on else None
        code, out, log_path, raw_all = code2, out2, log_path2, raw_all + "\n" + raw_all2
    else:
        final_tokens = run1_tok
        final_conv_tokens = conv1_tok
        final_conv_id = j_conv_id

    # (A call that answered is never a quota failure: an answer or a writer's output may itself hold "429" or "limit";
    # three successful writers were journalled as quota so.)
    # A timeout first: its partial answer may itself hold "limit".
    if code != 0 and is_timeout(raw_all):
        status = "timeout"
    elif (code != 0 or not (out or "").strip()) and route.is_quota_error(code, raw_all):
        status = "quota"
    elif not (out or "").strip():
        status = "no_text"
    elif retried:
        status = "ok (retried)"
    elif code == 0:
        status = "ok"
    else:
        status = "error"

    return code, out, final_tokens, log_path, raw_all, status, final_conv_id, final_conv_tokens


def _parse_codex_events(raw):
    """Keep usage per turn; Codex resume reports turn usage, not agy's cumulative totals."""
    answer, thread_id, quota, failed = [], None, False, False
    tokens = {"in": None, "out": None, "total": None, "cached_in": None}

    def limit_reached(obj):
        if isinstance(obj, dict):
            return ("rateLimitReachedType" in obj and obj["rateLimitReachedType"] is not None) or any(
                limit_reached(v) for v in obj.values())
        return isinstance(obj, list) and any(limit_reached(v) for v in obj)

    for line in (raw or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            answer.append(event["item"].get("text") or "")
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            t_in = usage.get("input_tokens")
            t_out = (usage.get("output_tokens") or 0) + (usage.get("reasoning_output_tokens") or 0)
            for key, value in (("in", t_in), ("out", t_out), ("cached_in", usage.get("cached_input_tokens"))):
                if value is not None:
                    tokens[key] = (tokens[key] or 0) + value
            tokens["total"] = (tokens["in"] or 0) + (tokens["out"] or 0)
        if kind in ("error", "turn.failed"):
            failed = True
            quota = quota or route.is_quota_error(1, line)
        quota = quota or limit_reached(event)
    return "\n\n".join(answer).strip(), tokens, thread_id, quota, failed


def ask_codex(model, text, carry_on, timeout_s, effort=None, executable=None, home=None):
    """Ask in a read-only empty room, the question by stdin, preserving raw events.
    home: another account's CODEX_HOME (None: the active one)."""
    room = route.CODEX_ROOM
    os.makedirs(room, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="answer-", dir=room) as tmp:
        answer_path = os.path.join(tmp, "answer.txt")
        cli = executable or route.codex_path()
        if carry_on:
            args = [cli, "-s", "read-only", "-C", room, "exec", "resume"]
        else:
            args = [cli, "exec", "-s", "read-only", "-C", room]
        args.extend(["-m", model, "--skip-git-repo-check", "--json", "-o", answer_path])
        if effort:
            args.extend(["-c", f"model_reasoning_effort={effort}"])
        if isinstance(carry_on, str) and carry_on:
            args.append(carry_on)
        elif carry_on:
            args.append("--last")
        args.append("-")  # the question by stdin, never on the command line (WinError 206)
        try:
            done = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout_s, cwd=room,
                                  **({"env": dict(os.environ, CODEX_HOME=home)} if home else {}))
            code, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as e:
            code = 4
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            stderr += f"\nCodex timed out after {timeout_s} s"
        except OSError as e:
            code, stdout, stderr = 4, "", str(e)
        raw = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
        log_path = route.save_raw_log(model, raw)
        out, tokens, thread_id, quota, failed = _parse_codex_events(stdout)
        if os.path.exists(answer_path):
            with open(answer_path, encoding="utf-8", errors="replace") as f:
                out = f.read().strip() or out
        quota = quota or route.is_quota_error(code, stderr) or (code != 0 and route.is_quota_error(code, stdout))
        timed_out = bool(code) and is_timeout(stderr)
        # A call that answered is never a quota failure (as the agy path holds): a 429 retried on stderr is noise.
        quota = quota and not timed_out and not (code == 0 and out)
        status = ("timeout" if timed_out else "quota" if quota
                  else "no_text" if not out else "error" if code or failed else "ok")
        if quota or failed or not out:
            code = code or 2
        if not out and (quota or code != 2):
            out = stderr.strip()
        return code, out, tokens, log_path, raw, status, thread_id, None


def claude_cli():
    """The claude CLI as a list; ASK_CLAUDE_CMD replaces it (the tests' fake, or another install)."""
    raw = os.environ.get("ASK_CLAUDE_CMD")
    if raw:
        return shlex.split(raw, posix=os.name != "nt")
    return [shutil.which("claude") or "claude"]


def claude_model_id(name, rules=None):
    """'opus' or 'Fable' -> the subscription's id that names it (claude-opus-5-5); a full id or an unknown name as given."""
    raw = str(name or "").strip()
    ids = (rules or {}).get("subscription_models") or route.DEFAULT_SUBSCRIPTION_MODELS
    if raw in ids:
        return raw
    found = [m for m in ids if raw.lower() in m.lower().split("-")]
    return found[0] if len(found) == 1 else raw


def ask_claude(model, text, carry_on, timeout_s, effort=None):
    """A Claude subscription model called by name (--claude): `claude -p` in an empty room, no tools, the question by
    stdin. carry_on: a session id to resume. -> (code, text, tokens, log path, status, session id)."""
    room = os.path.join(route.CACHE_DIR, "claude-room")
    os.makedirs(room, exist_ok=True)
    args = claude_cli() + ["-p", "--model", model, "--output-format", "json", "--tools", ""]
    if effort:
        args += ["--effort", effort]
    if isinstance(carry_on, str) and carry_on:
        args += ["--resume", carry_on]
    try:
        done = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_s, cwd=room)
        code, stdout, stderr = done.returncode, done.stdout or "", done.stderr or ""
    except subprocess.TimeoutExpired:
        code, stdout, stderr = 4, "", f"claude timed out after {timeout_s} s"
    except OSError as e:
        code, stdout, stderr = 4, "", f"claude: {e} (install Claude Code, or set ASK_CLAUDE_CMD)"
    log_path = route.save_raw_log(model, stdout + ("\n--- stderr ---\n" + stderr if stderr else ""))
    try:
        reply = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (ValueError, IndexError):
        reply = {}
    if not isinstance(reply, dict):
        reply = {}
    out = str(reply.get("result") or "").strip()
    session = reply.get("session_id")
    usage = reply.get("usage") or {}
    tokens = {"input": usage.get("input_tokens"), "output": usage.get("output_tokens")}
    tokens["total"] = sum(v for v in tokens.values() if isinstance(v, int))
    failed = bool(reply.get("is_error")) or code != 0
    quota = failed and (route.is_quota_error(code, out) or route.is_quota_error(code, stderr))
    status = "quota" if quota else "timeout" if code == 4 and "timed out" in stderr else (
        "error" if failed else "no_text" if not out else "ok")
    if failed:
        code = code or 2
        out = out or stderr.strip() or stdout.strip() or "claude gave no reply"
    return code, out, tokens, log_path, status, session


def _last_conversations():
    try:
        with open(os.path.join(route.CACHE_DIR, "ask_last_conversation.json"), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


LAST_REASONING = {}
# providers.py does `import ask`; run as a script this file is __main__, and a second copy
# would keep its own LAST_REASONING, so --reasoning show would print nothing (live run).
if __name__ == "__main__":
    sys.modules.setdefault("ask", sys.modules["__main__"])


def reasoning_dir():
    return os.path.join(route.CACHE_DIR, "reasoning")


def _open_for_human(path):
    if sys.platform == "darwin":
        subprocess.run(["open", "-t", path], check=False)
    elif os.name == "nt":
        os.startfile(path)
    else:
        subprocess.run(["xdg-open", path], check=False)


def _reasoning_lookup(conversation):
    from pathlib import Path
    directory = Path(reasoning_dir())
    if not conversation:
        try:
            last = json.loads((directory / "last.json").read_text(encoding="utf-8"))
            path = directory / Path(last["path"]).name
            if path.is_file():
                return path
        except (OSError, ValueError, KeyError, TypeError):
            pass
        raise ValueError("No last reasoning conversation")
    sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "tools")))
    import broker
    safe = broker.ReasoningLog.safe_id(conversation)
    files = sorted(p for p in directory.glob("*.md") if p.stem.startswith(safe))
    exact = next((p for p in files if p.stem == safe), None)
    if exact:
        return exact
    if len(files) == 1:
        return files[0]
    if files:
        raise ValueError("Ambiguous reasoning conversation: " + ", ".join(p.stem for p in files))
    raise ValueError("Unknown reasoning conversation: " + conversation)


def _print_reasoning(mode):
    if mode == "show" and LAST_REASONING:
        r = LAST_REASONING
        print(f"--- reasoning ({r['kind']}, {r['provider']} {r['model']}, turn {r['turn']}) ---")
        text = r["text"] or "(no reasoning from the provider this turn)"
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        print("--- answer ---")


def ask_broker(provider, model, text, conversation, *, effort=None, home=None, deadline=1800, ask="never",
               ask_timeout=600, max_tokens=None, verbose=False, executable=None, command=None, rules=None, pool=None, reasoning="file"):
    LAST_REASONING.clear()
    reasoning_mode = reasoning
    import threading
    tools_dir = os.path.abspath(os.path.join(HERE, "..", "..", "tools"))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import broker

    if conversation is True:
        conversation = _last_conversations().get(provider)
        if not conversation:
            print(f"No stored /ask conversation for {provider}; use --thread <id>.", file=sys.stderr)
            return 2, "", {}, None, "", "error", None, None
    if conversation and handoff.is_open(conversation):
        print(f"The user has {conversation} in a terminal; ask.py --handoff-back {conversation} ... first", file=sys.stderr)
        return 2, "", {}, None, "", "error", None, None
    room = route.ASK_ROOM if provider == "agy" else route.CODEX_ROOM
    os.makedirs(room, exist_ok=True)
    os.makedirs(route.CACHE_DIR, exist_ok=True)
    # Reserve the same timestamp/model name used by save_raw_log for all three logs.
    log_path = route.save_raw_log(model, "")
    base = log_path[:-4]
    cmd = command if command is not None else ([executable or route.AGY_DEFAULT, "acp"] if provider == "agy" else
                                              [executable or route.codex_path(), "app-server"])
    if command is None and provider == "codex" and ask == "human":
        cmd += ["--enable", "default_mode_request_user_input"]

    def stop_tree(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:  # gone already, or a zombie leader on macOS
                pass

    raw_lines, lock = [], threading.RLock()
    fallback = False
    print(f"[live log: {base}.progress.txt]", file=sys.stderr, flush=True)
    with open(base + ".progress.txt", "w", encoding="utf-8") as pf, \
         open(base + ".events.jsonl", "w", encoding="utf-8") as ef:
        def progress(note):
            with lock:
                note = broker.redact(note)
                line = f"{time.strftime('%H:%M:%S')} {note}"
                pf.write(line + "\n")
                pf.flush()
                if verbose:
                    color = route.use_color(sys.stderr)
                    print(route.red(line, color) if "ERROR" in note else route.dim(line, color),
                          file=sys.stderr, flush=True)

        def event(direction, message):
            with lock:
                line = json.dumps(broker.redact({"dir": direction, "msg": message}), ensure_ascii=False) + "\n"
                ef.write(line)
                ef.flush()
                raw_lines.append(line)

        cls = broker.AcpDriver if provider == "agy" else broker.CodexAppDriver
        opts = {} if provider == "agy" else {"sandbox": "read-only", "effort": effort, "reasoning_summary": "detailed"}
        driver = cls(room, model, base, progress, event, stop_tree, command=cmd, read_only=True,
                     engine_markers=hooks.engine_markers(route.load_machine()),
                     env=dict(os.environ, CODEX_HOME=home) if home else None,
                     ask=ask, ask_timeout=ask_timeout, deadline=deadline or None, max_tokens=max_tokens,
                     billed=lambda t: route.billed_tokens(t, provider, pool=pool, rules=rules)["billed"], **opts)
        reasoning_log = None

        def open_reasoning(cid):
            nonlocal reasoning_log
            if cid and reasoning_log is None:
                reasoning_log = broker.ReasoningLog(reasoning_dir(), cid, provider, model)
                driver.reasoning_sink = reasoning_log.write
                if reasoning_mode == "live":
                    import shlex
                    print(f"[reasoning live: tail -f {shlex.quote(reasoning_log.path)}]", file=sys.stderr, flush=True)

        if conversation:
            open_reasoning(conversation)
        # Store the id when it arrives, even if this turn later stops or fails.
        def remember(cid):
            # The writer's hint names writer.py flags; /ask carries on with --thread (or --continue for the last one).
            driver.conversation = cid
            open_reasoning(cid)
            progress("conversation " + str(cid))
            print(f"[conversation {cid}: carry on with --thread {cid}]", file=sys.stderr, flush=True)
            if cid:
                last = _last_conversations()
                last[provider] = cid
                broker.atomic_json(os.path.join(route.CACHE_DIR, "ask_last_conversation.json"), last)
        driver.known = remember
        progress(f"start, {provider} {model}, in {room}")
        try:
            driver.run(text, conversation or None)
        except broker.ResumeError as error:
            if provider != "codex":
                raise
            progress("ERROR: Codex resume failed; falling back to --legacy-cli: " + str(error))
            print("[Codex resume failed; falling back to --legacy-cli]", file=sys.stderr)
            fallback = True
        except (RuntimeError, OSError) as error:
            driver.errors.append(str(error))
            driver.failed = True
            progress("ERROR: " + str(error))
        finally:
            driver.close()
            if reasoning_log is not None:
                path = reasoning_log.close(driver.reasoning_kind)
                broker.atomic_json(os.path.join(reasoning_dir(), "last.json"), {
                    "conversation": driver.conversation or conversation, "path": path, "provider": provider, "model": model})
                print(f"[reasoning: {path}]", file=sys.stderr, flush=True)
                LAST_REASONING.update(text=reasoning_log.text, kind=driver.reasoning_kind or "none",
                                      provider=provider, model=model, turn=reasoning_log.turn)
        result = driver.result()
        out, cid = result["response"], driver.conversation
        conv_tok = None
        if provider == "agy":
            usage = _extract_tokens(driver.usage)
            prev = route.load_agy_conversations().get(str(cid or "")) if conversation else None
            # A failed turn with no usage must not be booked as a zero-token run of the conversation.
            if prev and usage.get("total"):
                for key, stored in (("in", "in"), ("out", "out"), ("total", "total"),
                                    ("cached_in", "cached"), ("thinking", "thinking")):
                    usage[key] = (usage.get(key) or 0) + int(prev.get(stored) or 0)
            tokens, conv_tok, _ = route.record_conversation_usage(cid, usage, carried_on=bool(conversation))
        else:
            tokens = driver.tokens
        status = ("quota" if driver.quota or (not out and route.is_quota_error(1, "\n".join(driver.errors))) else
                  "deadline" if driver.timed_out is not None else "capped" if driver.capped is not None else
                  "no_text" if not out else "error" if driver.failed else "ok")
        code = {"ok": 0, "deadline": 9, "capped": 5}.get(status, 2)
        reasoning = ""
        if provider == "codex" and "reasoningOutputTokens" in driver.total:
            reasoning = f", reasoningOutputTokens={max(0, driver.total['reasoningOutputTokens'] - driver.baseline.get('reasoningOutputTokens', 0))}"
        progress(f"end, {status}, tokens={tokens}{reasoning}")
        if status in ("deadline", "capped"):
            print(f"[{status}: carry on with --thread {cid}]", file=sys.stderr)
    raw_all = "".join(raw_lines)
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(raw_all)
    if fallback:
        LAST_REASONING.clear()
        return ask_codex(model, text, conversation, int(deadline) if deadline else 300, effort=effort, executable=executable, home=home)
    return code, out, tokens, log_path, raw_all, status, cid, conv_tok


def _run_cloud(provider, agy, model, text, carry_on, timeout_s, effort=None, verbose=False, account=None, **broker_kw):
    """Through the provider's adapter (providers.py), on the account the policy chose (None: the active one); agy's effort is
    already in its id."""
    LAST_REASONING.clear()
    # The open-handoff check holds on every path, the legacy CLI too: two clients on one thread would collide.
    conversation = carry_on if isinstance(carry_on, str) else (_last_conversations().get(provider) if carry_on else None)
    if conversation and handoff.is_open(conversation):
        print(f"The user has {conversation} in a terminal; ask.py --handoff-back {conversation} ... first", file=sys.stderr)
        return 2, "", {}, None, "", "error", None, None
    if conversation:
        carry_on = conversation  # the one checked is the one resumed, on the legacy CLIs too (not their own "last")
    if provider == "codex":
        return adapters.get("codex").run(account, model, effort, text, carry_on=carry_on, timeout_s=timeout_s, verbose=verbose, **broker_kw)
    return adapters.get("agy").run(None, model, None, text, carry_on=carry_on, timeout_s=timeout_s, verbose=verbose, agy=agy, **broker_kw)


def _run_local(provider, ollama, model, text, a):
    """A local provider's call, under the GPU lock the caller holds: Ollama by its own API, another local server through
    its adapter (providers.py). -> (code, text, tokens, log path)."""
    if provider == "ollama":
        return ask_ollama(ollama, model, text, a.think, a.timeout, name=model, keep=a.keep)
    return adapters.get(provider).run(None, model, a.effort, text, timeout_s=a.timeout)


def ollama_up(url):
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=2):
            return True
    except Exception:
        return False


def ollama_exe():
    """The ollama binary, by the local adapter's usual places (PATH, Ollama.app, Programs\\Ollama), or None."""
    found = adapters.get("ollama")._auto()
    return found["path"] if found["present"] else None


def start_ollama(url):
    exe = ollama_exe()
    if not exe or not os.path.exists(exe):
        return False
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
    try:
        subprocess.Popen([exe, "serve"], creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=not sys.platform.startswith("win"))
    except Exception:
        return False
    for _ in range(60):
        if ollama_up(url):
            return True
        time.sleep(0.5)
    return False


def unload_ollama_model(url, model):
    body = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(url + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception:
        return False


def unload_all_ollama(url):
    if not ollama_up(url):
        return 0, "Ollama is not running"
    route.record_vram_from_ps(url)
    try:
        with urllib.request.urlopen(url + "/api/ps", timeout=5) as r:
            ps = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 4, f"Ollama /api/ps failed: {e}"
    models_ps = [m.get("name") or m.get("model") for m in ps.get("models", []) if m.get("name") or m.get("model")]
    if not models_ps:
        return 0, "Ollama holds no models in video memory"
    for m in models_ps:
        unload_ollama_model(url, m)
    return 0, "Unloaded from Ollama: " + ", ".join(models_ps)


def ask_ollama(url, model, text, think, timeout_s, name="ollama", keep=None):
    if not ollama_up(url) and not start_ollama(url):
        msg = "Ollama does not answer at %s and could not be started" % url
        log_path = route.save_raw_log(model or name, msg)
        return 4, msg, {"in": None, "out": None, "total": None}, log_path
    body = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
        "keep_alive": keep if keep is not None else "15s",
    }
    if model.startswith("qwen3"):
        body["think"] = think
    req = urllib.request.Request(url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw_bytes = r.read()
        raw_str = raw_bytes.decode("utf-8", errors="replace")
        log_path = route.save_raw_log(model or name, raw_str)
        route.record_vram_from_ps(url)
        if keep is None or str(keep) == "0":
            unload_ollama_model(url, model)
        data = json.loads(raw_str)
        ans = data.get("message", {}).get("content", "").strip()
        t_in = data.get("prompt_eval_count")
        t_out = data.get("eval_count")
        t_tot = ((t_in or 0) + (t_out or 0)) if (t_in is not None or t_out is not None) else None
        return 0, ans, {"in": t_in, "out": t_out, "total": t_tot}, log_path
    except Exception as e:
        if keep is None or str(keep) == "0":
            unload_ollama_model(url, model)
        err_msg = "Ollama failed: %s" % e
        log_path = route.save_raw_log(model or name, err_msg)
        return 4, err_msg, {"in": None, "out": None, "total": None}, log_path


def draw(agy, text, out_path, timeout_s):
    import glob
    import shutil
    os.makedirs(ASK_ROOM, exist_ok=True)
    started = time.time()
    ask = ("Use the image-generator subagent to create one image: %s. Do not run any terminal command. "
           "Then say in one line the image file's full path and which model made it." % text)
    try:
        # The prompt goes by stdin as every other call's does (Windows caps a command line at ~32K characters).
        done = subprocess.run([agy, "--settings", DRAW_SETTINGS, "--print-timeout", "%ds" % timeout_s],
                              input=ask.encode("utf-8"), capture_output=True, timeout=timeout_s + 60, cwd=ASK_ROOM)
    except subprocess.TimeoutExpired:
        return 4, "agy did not finish within %d s" % (timeout_s + 60)
    said = done.stdout.decode("utf-8", errors="replace").strip()
    made = [p for ext in ("png", "jpg", "jpeg", "webp") for p in glob.glob(os.path.join(AGY_BRAIN, "*", "*." + ext))
            if os.path.getmtime(p) >= started - 1]
    if not made:
        return 5, said or "no image was made"
    newest = max(made, key=os.path.getmtime)
    root, _ = os.path.splitext(out_path)
    out_path = root + os.path.splitext(newest)[1]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    shutil.copyfile(newest, out_path)
    return 0, "%s\n(the image: %s)" % (said, os.path.abspath(out_path))


def print_status(agy, ollama, ctx=None):
    """Print one screen: context and policy, all cloud & subscription pools, active reservations, the local GPU verdict,
    Ollama, and 24h journal."""
    color = route.use_color(sys.stdout)
    print_policy(ctx or route.resolve_context(), color)
    print()
    rules = route.load_roles()
    floors = rules.get("floors", {"weekly": 0.10, "5h": 0.10})
    pace_skip = float(rules.get("pace_skip", 2.0))

    cs = route.cloud_state(agy_path=agy, rules=rules)
    loc = route.local_state(ollama_url=ollama, rules=rules)
    j24 = route.read_journal_24h(rules=rules)

    print(route.bold("Quota pools and windows", color))
    hdr = f"  {'Pool':<32} {'Window':<8} {'Remaining':>9}  {'Resets in':<10} {'Pace':<28} {'Covers'}"
    print(route.dim(hdr, color))

    for p in cs.get("pools", []):
        plabel = p.get("label") or p.get("id") or "pool"
        if p.get("stale"):
            plabel += " [stale]"
        covers_str = ", ".join(p.get("covers") or []) or "(none)"
        wins = p.get("windows") or []
        if not wins:
            row_txt = f"  {plabel:<32} {'-':<8} {'-':>9}  {'-':<10} {'-':<28} {covers_str}"
            print(route.dim(row_txt, color))
        for w in wins:
            wtype = w.get("window") or "window"
            rem = float(w.get("remaining_fraction", 0.0))
            fl = float(floors.get(wtype, floors.get("weekly", 0.10)))
            rem_str = f"{rem * 100:8.1f}%"
            pace_val = w.get("pace")
            pace_str = f"{w.get('pace_str', ''):<28}"
            w_covers = ", ".join(w.get("covers") or p.get("covers") or []) or "(none)"
            if w.get("own_remaining_fraction") is not None:
                w_covers += f"  [inside all models: min of own {float(w['own_remaining_fraction']) * 100:.0f}% and theirs]"
            if p.get("stale"):
                row_txt = f"  {plabel:<32} {wtype:<8} {rem_str}  {w.get('resets_in', '?'):<10} {pace_str} {w_covers}"
                print(route.dim(row_txt, color))
            else:
                if color:
                    if rem < fl:
                        rem_str = route.red(rem_str, color)
                    elif rem < 0.25:
                        rem_str = route.yellow(rem_str, color)
                    if "week" in wtype.lower() and pace_val is not None and float(pace_val) > pace_skip:
                        pace_str = route.yellow(pace_str, color)
                print(f"  {plabel:<32} {wtype:<8} {rem_str}  {w.get('resets_in', '?'):<10} {pace_str} {w_covers}")
        for r in p.get("reservations") or []:
            rlbl = r.get("model")
            sh = r.get("share") or {}
            sh_parts = [f"{k} {float(v) * 100:.1f}%" for k, v in sh.items()]
            rline = (
                f"    reserved: pid {r.get('pid')} {r.get('script', '')} "
                f"({r.get('role') or 'direct'}) {rlbl} ~{int(r.get('tokens') or 0):,} billed tok "
                f"[{', '.join(sh_parts)}]"
            )
            print(route.yellow(rline, color) if color else rline)

    burn = route.burning_pools(cs.get("pools", []))  # the same window and sum as the router's choice
    if burn:
        parts = [f"{p.get('label') or p.get('id')} {w.get('window')} {route._available(w) * 100:.0f}% in {w.get('resets_in', '?')}"
                 for _, p, w in burn]
        print(route.dim("  Burns first (leftover lost at reset, after reservations): " + " > ".join(parts), color))

    if not cs.get("ok"):
        print(route.yellow(f"  (agy /usage warning: {cs.get('error')})", color))
    if cs.get("codex_error"):
        print(route.yellow(f"  (Codex usage warning: {cs['codex_error']})", color))
    sub_meta = cs.get("claude_subscription") or {}
    if sub_meta.get("status") == "missing":
        print(route.dim("  (Claude subscription file ~/.cache/ask/claude_usage.json is missing.)", color))
    elif sub_meta.get("status") == "stale":
        print(route.dim(f"  (Claude subscription file is stale: read_at {sub_meta.get('read_at')}, >6h old; not used to pass over.)", color))

    print()
    print(route.bold("GPU and local models (Ollama)", color))
    verdict = route.green("FREE", color) if loc["free"] else route.yellow("BUSY: " + loc["reason_str"], color)
    print(f"  {'GPU':<10} {loc['used_gb']:.1f} / {loc['total_gb']:.1f} GB VRAM ({loc['free_gb']:.1f} GB free), {loc['utilization']}% util")
    print(f"  {'Local GPU':<10} {verdict}")
    loaded_items = [
        f"{m['name']} ({route._to_gb(m['size_vram']):.1f} GB)"
        for m in loc["loaded"]
    ]
    loaded_str = ", ".join(loaded_items) if loaded_items else "none"
    ollama_state = route.green("up", color) if loc["ollama_up"] else route.red("down", color)
    print(f"  {'Ollama':<10} {ollama_state} ({ollama}), loaded: {loaded_str}")

    print()
    print(route.bold("Last 24h journal (~/.cache/ask/usage.jsonl)", color))
    if not j24:
        print(route.dim("  (no calls recorded in the last 24h)", color))
    else:
        print(route.dim(f"  {'Model':<34} {'Calls':>7} {'Billed tokens':>13} {'Quota hits':>12}", color))
        for lbl in sorted(j24.keys()):
            row = j24[lbl]
            qhits = row["quota_hits"]
            q_str = f"{qhits:>12}"
            if color and qhits > 0:
                q_str = route.yellow(q_str, color)
            print(f"  {lbl:<34} {row['calls']:>7} {row['tokens']:>13} {q_str}")
    return 0


def print_presets(active, context, rules=None, fake=None):
    """Read routing state without calling a model, reserving quota or waiting."""
    import preset_edit
    rules = rules or route.load_roles()
    names = ["manual"] + [n for n in rules.get("presets", {}) if n != "manual"]
    print(" | ".join(["role"] + [n + (" *" if n == active else "") for n in names]))
    claude_models = set()
    for role, row in preset_edit.picks_table(rules, fake=fake, context=context).items():
        cells = [row["picks"].get(name) or "-" for name in names]
        manual = row["picks"].get("manual") or ""
        if manual.startswith("claude:"):
            claude_models.add(manual[len("claude:"):])
        print(" | ".join([role] + cells))
    print("* active; weighted presets are shadow only. Roles without preset scoring repeat manual; - means no pick.")
    if claude_models:
        cs = route.cloud_state(rules=rules, fake=fake)
        shown = set()
        for model in sorted(claude_models):
            for pool, window in route.covering_pools_and_windows("claude-subscription", model, cs.get("pools", []), include_stale=True):
                if "week" not in str(window.get("window", "")) or pool["id"] in shown:
                    continue
                shown.add(pool["id"])
                label = pool.get("label", pool["id"]).split("·")[-1].strip()
                pace = window.get("pace")
                pace_text = f"x{pace:.1f}" if pace is not None else "pace unknown"
                print(f"Claude, {label}: {route._available(window) * 100:.0f}% left, {pace_text}")
    return 0


def _policy_kw(decision):
    """What the journal keeps of the policy for a call that went out."""
    kw = {"context": decision.get("context"), "account": decision.get("account"), "crossed": bool(decision.get("crossed"))}
    if decision.get("why"):
        kw["why"] = decision["why"]
    kw.update({key: decision[key] for key in ("score_pick", "score_notes", "score_table", "preset", "preset_picks") if decision.get(key) is not None})
    return kw


def _say_score(decision, color):
    active = decision.get("preset", "manual")
    if active != "manual" and "preset_picks" in decision:
        proposed = decision.get("preset_picks", {}).get(active, decision.get("model"))
        print(route.dim(f"[preset {active} (shadow) would pick: {proposed or '-'}]", color), file=sys.stderr)
    for note in decision.get("score_notes", []):
        print(route.dim(f"[score: {note}]", color), file=sys.stderr)
    if decision.get("score_mode") == "shadow" and decision.get("score_pick"):
        print(route.dim(f"[score would pick: {decision['score_pick']}]", color), file=sys.stderr)


def _say_account(decision, color):
    """Before a call goes out, name its account; a work → personal crossing is a warning of its own."""
    if decision.get("crossed"):
        pol = route.policy_for(decision.get("provider"), context=decision.get("context"), account=decision.get("account"))
        print(route.yellow(f"[{route.crossing_line(pol)}]", color), file=sys.stderr)
    elif decision.get("context") == "work" and decision.get("account") and not route.is_local(decision.get("provider")):
        print(route.dim(f"[{decision.get('context')} context: account {decision['account']} "
                        f"({decision.get('account_label')})]", color), file=sys.stderr)
    if decision.get("why") == "expires_first" and decision.get("expiry"):
        x = decision["expiry"]
        print(route.dim(f"[expires first: {x['pool']} {x['window']} {x['remaining'] * 100:.1f}% left, "
                        f"resets in {x.get('resets_in') or '?'}]", color), file=sys.stderr)
    for note in decision.get("experience", []):
        print(route.dim(f"[experience: {note}]", color), file=sys.stderr)


def _refused(script, role, decision, ctx):
    route.journal(script, role, decision.get("model"), decision.get("provider"), decision.get("model"),
                  seconds=0.0, status="refused", context=ctx["context"], account=decision.get("account"))
    return route.EXIT_POLICY


def print_policy(ctx, color):
    """The context, where it came from, and what each provider's account may do in it."""
    machine = route.load_machine()
    where = ctx.get("note", "default") if ctx["strict_default"] or ctx["source"] == "context_paths" else ctx["source"]
    print(route.bold(f"Context: {ctx['context']}", color) + route.dim(f"  ({where})", color))
    if machine.get("_error"):
        print(route.red(f"  machine.json unreadable: {machine['_error']} (every account counts as personal)", color))
    elif not machine:
        print(route.dim("  (no ~/.config/ask/machine.json: every cloud account counts as personal)", color))
    print(route.dim(f"  {'Account':<26} {'Label':<9} {'From':<26} {'In this context'}", color))
    for r in route.policy_summary(context=ctx["context"], machine=machine):
        verdict = r["verdict"]
        if not r["allowed"]:
            verdict = route.red(verdict, color)
        elif r["crossed"]:
            verdict = route.yellow(verdict, color)
        else:
            verdict = route.green(verdict, color)
        print(f"  {r['account']:<26} {r['label']:<9} {r['source']:<26} {verdict}")
    order = ("work → local → personal with work_ok: true" if ctx["context"] == "work"
             else "the role's own order; work accounts refused")
    print(route.dim(f"  order: {order}", color))


def _print_header(model_id, pool_desc, passed_over, warning=None):
    color = route.use_color(sys.stdout)
    for reason in passed_over or []:
        print(route.dim(f"[passed over: {reason}]", color))
    if warning:
        print(route.yellow(f"[{warning}]", color))
    head = f"[{route.bold(model_id, color)}, {route.dim(pool_desc, color)}]"
    print(head)


def _email_hint(email):
    # (Only the domain: what the label is decided by. The address itself stays out of stdout, which Claude reads.)
    return f"@{email.rpartition('@')[2]}" if email and "@" in email else ""


def print_list(agy, ollama, notes=False, ctx=None, refresh=False):
    """The inventory: provider -> account (label, what this context allows) -> model -> efforts, with each model id's roles,
    pool and writer verdicts."""
    color = route.use_color(sys.stdout)
    rules = route.load_roles()
    ctx = ctx or route.resolve_context()
    fake = route.resolve_fake()
    inv = adapters.inventory(fake=fake, force_refresh=refresh, context=ctx, rules=rules)
    rows = {(r["provider"], r["model"]): r for r in route.models(agy_path=agy, ollama_url=ollama, rules=rules)}
    used = route.role_usage(rules)
    verdicts = route.verdict_counts(fake=fake)
    where = ctx.get("note", "default") if ctx.get("strict_default") or ctx.get("source") == "context_paths" else ctx.get("source")
    print(route.bold(f"Context: {ctx['context']}", color) + route.dim(f"  ({where})", color))
    print(route.dim(f"      {'Model':<34} {'Roles':<36} {'Pool':<30} Verdicts", color))
    offered, unused = set(), 0

    def model_line(provider, mid, indent, allowed):
        nonlocal unused
        offered.add((provider, mid))
        named = list(used.get((provider, mid), []))
        roles = ", ".join(named + [f"{r} (wish)" for r in route.wish_roles(provider, mid, rules) if r not in named])
        if not roles:
            unused += 1
        cell = f"{roles or '-- in no role':<36}"
        counts = route.format_verdicts(verdicts.get((provider, mid)))
        pool = (rows.get((provider, mid)) or {}).get("pool", "")
        name = f"{mid:<{40 - indent}}"
        line = f"{' ' * indent}{name if allowed else route.dim(name, color)} {cell if roles else route.yellow(cell, color)} {pool:<30} {counts}"
        print(line.rstrip())
        if notes:
            limits = route.cannot(provider, mid, rules=rules)
            if limits:
                print(route.dim(f"{' ' * (indent + 2)}cannot here: {', '.join(n for n in route.KNOWN_NEEDS if n in limits)}", color))
            # Evidence stays with its model so the reader can judge each note.
            for note in (rules.get("capabilities") or {}).get(mid, []):
                print(route.dim(f"{' ' * (indent + 2)}{note['task']}: {note['note']} ({note['evidence']}, {note['date']})", color))
            if counts:
                print(route.dim(f"{' ' * (indent + 2)}writer: {counts}", color))

    for prov in inv:
        det = prov["detect"]
        if not det["present"]:
            print(route.bold(prov["provider"], color) + route.dim(f"  {prov['title']}: {det['note']}", color))
            continue
        print(route.bold(prov["provider"], color) + route.dim(f"  {prov['title']}, {det['path']} ({det['source']})", color))
        for acc in prov["accounts"]:
            if not acc["allowed"]:
                verdict = route.red(f"refused: {acc['reason']}", color)
            elif acc["crossed"]:
                verdict = route.yellow("allowed, crossing (journalled)", color)
            else:
                verdict = route.green("allowed", color)
            src = acc["label_source"] + (f", {_email_hint(acc.get('email'))}" if _email_hint(acc.get("email")) else "")
            active = "" if acc.get("active") else route.dim("  (not CODEX_HOME: a call comes here when the policy ranks it first)", color)
            print(f"  {acc['id']} " + route.dim(f"[{acc['label']}, {src}]", color) + f"  {verdict}{active}")
            if not acc["models_ok"]:
                hint = {"agy": "is agy signed in? run agy once in a terminal",
                        "codex": "is Codex signed in? codex login"}.get(prov["provider"], "is it running?")
                why = route.agy_unavailable() if prov["provider"] == "agy" else None
                print(route.dim(f"    (live list not read: {hint}" + (f"; {why}, asked again in 15 min or with --refresh"
                                                                       if why else "") + ")", color))
            for m in acc["models"]:
                if m.get("ids"):  # agy: one id per effort
                    effs = ", ".join(m["efforts"]) or "-"
                    print(f"    {m['family']} " + route.dim(f"efforts {effs}", color))
                    for eff in sorted(m["ids"], key=adapters.effort_rank):
                        model_line(prov["provider"], m["ids"][eff], 6, acc["allowed"])
                else:
                    effs = ", ".join(e + ("*" if e == m.get("default_effort") else "") for e in m.get("efforts") or [])
                    model_line(prov["provider"], m["id"], 4, acc["allowed"])
                    if effs:
                        print(route.dim(f"      efforts {effs}" + ("  (* the model's default)" if "*" in effs else ""), color))
                    details = (rows.get((prov["provider"], m["id"])) or {}).get("details")
                    if prov["provider"] == "ollama" and details:
                        print(route.dim(f"      {details}", color))
    # (A role naming a model its provider no longer lists: the router passes it over, so it is said here too.)
    missing = [(pv, m, rs) for (pv, m), rs in sorted(used.items()) if (pv, m) not in offered and pv != "claude-subscription"]
    print()
    print(route.dim(f"{unused} offered model(s) in no role (yellow): whether one joins a role is your call.", color))
    absent = {p["provider"] for p in inv if not p["detect"]["present"]}
    for pv, m, rs in missing:
        why = f"{pv} is not on this machine" if pv in absent else "not offered now"
        print(route.red(f"{m} ({pv}) is named by {', '.join(rs)} but {why}: passed over.", color))
    if notes:
        try:
            # Keep role-specific evidence together, including models absent from live lists.
            import experience
            journal_rows = route._read_journal_rows(fake=fake)
            bench = experience.load_benchmarks()
            lines = []
            for role in rules.get("roles", {}):
                lines.append(f"  {role}:")
                for cand in route._role_spec(role, rules)["models"]:
                    provider, mid = route.resolve_model(cand, rules=rules)
                    if mid:
                        result = experience.quality(role, mid, provider, journal_rows, rules, bench)
                        lines.append(f"    {mid}: {experience.summary(result, role)}")
            for line in lines:
                print(route.dim(line, color))
        except Exception:
            pass
    return 0


def main():
    LAST_REASONING.clear()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        # ask.py --preset edit ...: preset_edit.py's own command line (edit is never a preset's name).
        if (arg == "--preset" and argv[i + 1:i + 2] == ["edit"]) or arg == "--preset=edit":
            import preset_edit
            return preset_edit.main(argv[:i] + argv[i + (1 if "=" in arg else 2):])
    agy, ollama = load_config()
    if len(sys.argv) > 1 and sys.argv[1].lower() == "draw":
        d = argparse.ArgumentParser()
        d.add_argument("verb")
        d.add_argument("text", nargs="+")
        d.add_argument("--out", required=True, help="where the image goes (its extension is the image's own)")
        d.add_argument("--timeout", type=int, default=400)
        d.add_argument("--context", help="work or personal (default: ASK_CONTEXT; unknown is work)")
        b = d.parse_args()
        dctx = route.resolve_context(b.context)
        pol = route.policy_for("agy", context=dctx)
        color = route.use_color(sys.stderr)
        if not pol["allowed"]:
            print(route.red(f"draw: refused by policy: {pol['reason']}", color), file=sys.stderr)
            route.journal("ask.py", None, "image-generator", "agy", "image-generator", status="refused",
                          context=dctx["context"], account=pol["account"])
            return route.EXIT_POLICY
        if pol["crossed"]:
            print(route.yellow(f"[{route.crossing_line(pol)}]", color), file=sys.stderr)
        t0 = time.time()
        code, out = draw(agy, " ".join(b.text), b.out, b.timeout)
        route.journal("ask.py", None, "image-generator", "agy", "image-generator", seconds=time.time() - t0,
                      status="ok" if code == 0 else "error", context=dctx["context"], account=pol["account"],
                      crossed=bool(pol["crossed"]))
        print(out)
        return code

    p = argparse.ArgumentParser()
    p.add_argument("name", nargs="?", help="model id (see --list)")
    p.add_argument("text", nargs="*")
    p.add_argument("--role", help="route by role: review, second-opinion, first-read, draft, index, writer, code-writer, analysis, hard, engine-writer (roles.json)")
    p.add_argument("--tokens", type=int, default=None, help="override estimated billed tokens for pre-run fit and reservation")
    p.add_argument("--force", action="store_true", help="run an explicitly named model even if its estimate crosses the floor")
    p.add_argument("--continue", dest="carry_on", action="store_true", help="agy/Codex: carry on the last /ask conversation")
    p.add_argument("--thread", help="agy/Codex: resume this session/thread id")
    p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"),
                   help="Codex reasoning effort (the model's own list: --list); default is the model's own")
    providers = p.add_mutually_exclusive_group()
    providers.add_argument("--agy", metavar="ID", help="explicit agy model id")
    providers.add_argument("--ollama", metavar="ID", help="explicit Ollama model id")
    providers.add_argument("--server", metavar="NAME", help="configured local OpenAI-compatible server")
    providers.add_argument("--codex", metavar="ID", help="explicit Codex model id")
    providers.add_argument("--claude", metavar="ID", help="explicit Claude model id (claude -p on the subscription)")
    p.add_argument("--file", action="append", default=[], help="a file appended after the question (repeatable)")
    p.add_argument("--think", action="store_true", help="qwen3: think first (slower)")
    p.add_argument("--reasoning", choices=("file", "live", "show"), default="file")
    retrieval = p.add_mutually_exclusive_group()
    retrieval.add_argument("--handoff", nargs="?", const="")
    retrieval.add_argument("--handoff-back", nargs=2, metavar=("ID", "MODE"))
    retrieval.add_argument("--reasoning-read", nargs="?", const="")
    retrieval.add_argument("--reasoning-open", nargs="?", const="")
    # The old one-shot CLIs (agy -p, codex exec), without the broker's protocol: kept as the fallback a failed Codex resume
    # takes and for debugging, not offered in --help.
    p.add_argument("--legacy-cli", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--deadline", type=float, default=None, help="agent seconds; 0 disables (role budget in role mode, otherwise 1800)")
    p.add_argument("--ask", choices=("never", "human"), default="never")
    p.add_argument("--ask-timeout", type=float, default=600)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--answer", nargs="+", metavar="VALUE")
    p.add_argument("--timeout", type=int, default=None, help=argparse.SUPPRESS)  # --legacy-cli's seconds
    p.add_argument("--wait", action="store_true", help="wait every 30 s while GPU is busy (default on local_only roles)")
    p.add_argument("--max-wait", default="7200", help="max seconds or duration (e.g. 2h) to wait for GPU (default: 2h)")
    p.add_argument("--keep", default=None, help="Ollama keep_alive duration (e.g. 10m) for a series of questions; default 0")
    p.add_argument("--unload", action="store_true", help="unload whatever Ollama holds in video memory")
    p.add_argument("--preset", nargs="?", const="", help="preset name; without a question, compare all presets (shadow); --preset edit tunes them")
    p.add_argument("--status", action="store_true", help="show pools, reservations, GPU, Ollama and last 24h journal")
    p.add_argument("--scorecard", nargs="?", const="judged", choices=("judged", "all"),
                   help="writer verdict share per provider, model and role (30 days); all: also rows with no verdict")
    p.add_argument("--verbose", action="store_true", help="stream agy's steps one line each as they happen")
    p.add_argument("--allow-never", action="store_true", help="allow calling a model in roles.json's never list")
    p.add_argument("--list", action="store_true", help="provider -> account (label) -> model -> efforts, and what this context allows")
    p.add_argument("--refresh", action="store_true", help="with --list: read the providers' live lists again, past their caches")
    p.add_argument("--context", help="work or personal; over ASK_CONTEXT; anything else is work (the strict side)")
    p.add_argument("--notes", action="store_true", help="include model capabilities and recent writer verdicts in --list")
    p.add_argument("--context-here", metavar="WORK|PERSONAL",
                   help="write .switchboard-context in this folder: it and every folder under it are that context")
    p.add_argument("--init", action="store_true", help="look at this machine, propose ~/.config/ask/machine.json, write it on a yes")
    p.add_argument("--machine", choices=("personal", "work", "both"), help="with --init: what this machine is for")
    p.add_argument("--work-domain", action="append", default=[], help="with --init: your employer's email domain (repeatable)")
    p.add_argument("--roles", action="store_true",
                   help="propose the roles from this machine's models, show the change, write ~/.config/ask/roles.json on a yes")
    p.add_argument("--yes", action="store_true", help="with --init or --roles: write without asking")
    p.add_argument("--needs", type=route.parse_needs, default=[], help="comma-separated needs: python, engine, big-files")
    # Model IDs and question words may surround flags.
    a = p.parse_intermixed_args()
    try:
        active = route.resolve_preset(a.preset)
    except ValueError as error:
        p.error(str(error))

    if a.context_here is not None:
        return init_config.context_here(a.context_here)
    if a.init:
        return init_config.init(kind=a.machine, work_domains=a.work_domain, yes=a.yes)
    if a.roles:
        return init_config.refresh_roles(yes=a.yes)

    if a.handoff is not None or a.handoff_back is not None:
        try:
            if a.handoff_back is not None:
                print(handoff.back(*a.handoff_back))
            else:
                command, cid = handoff.start(a.handoff, a.context)
                print(command)
                print(f"Open this in a new terminal tab for the user; read nothing of the conversation until they say so, then run ask.py --handoff-back {cid} full|summary|none as they choose.")
        except (ValueError, OSError, RuntimeError) as error:  # RuntimeError: the ACP history call failed
            print(str(error), file=sys.stderr)
            return route.EXIT_POLICY if isinstance(error, handoff.PolicyRefusal) else 2
        return 0

    if a.reasoning_read is not None or a.reasoning_open is not None:
        try:
            path = _reasoning_lookup(a.reasoning_read if a.reasoning_read is not None else a.reasoning_open)
            if a.reasoning_read is not None:
                sys.stdout.write(path.read_text(encoding="utf-8"))
            else:
                _open_for_human(str(path))
                print(f"[reasoning: {path}]", file=sys.stderr)
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0

    if a.answer:
        sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "tools")))
        import broker
        if len(a.answer) not in (2, 3):
            p.error("--answer needs <base> <option> [text]")
        try:
            pending = broker.answer_pending(*a.answer)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        print(f"Answered {pending['kind']} {a.answer[1]}")
        return 0

    ctx = route.resolve_context(a.context)
    if a.status:
        print(f"Preset: {active['preset']} ({active['source']})" +
              (" (shadow)" if active['preset'] != "manual" else ""))
        return print_status(agy, ollama, ctx)
    if a.scorecard:
        import scorecard
        rules = route.load_roles()
        found = scorecard.cards(route._read_journal_rows(fake=route.resolve_fake()), rules)
        print(scorecard.table(found, rules, everything=a.scorecard == "all"))
        return 0
    if a.unload:
        code, msg = unload_all_ollama(ollama)
        print(msg)
        return code

    if a.preset is not None and not a.name and not a.text and not a.role:
        return print_presets(active["preset"], ctx)

    raw_words = list(a.text)
    target_name = a.name
    explicit_provider = next((pv for pv in ("agy", "ollama", "codex") if getattr(a, pv)), None)
    if a.claude:
        a.claude = claude_model_id(a.claude, route.load_roles())
    if a.server:
        if a.role or not a.name or a.server not in adapters.local_servers():
            p.error("--server requires a configured local server and model, without --role")
        target_name = {"provider": a.server, "model": a.name}
    if explicit_provider:
        if a.role:
            p.error(f"--{explicit_provider} and --role cannot be combined")
        if target_name is not None:
            raw_words.insert(0, target_name)
        target_name = {"provider": explicit_provider, "model": getattr(a, explicit_provider)}
    elif a.claude:
        if a.role:
            p.error("--claude and --role cannot be combined")
        if target_name is not None:
            raw_words.insert(0, target_name)
        target_name = {"provider": "claude-subscription", "model": a.claude}
    if a.role and target_name is not None:
        raw_words = [target_name] + raw_words
        target_name = None

    if a.list or (not a.role and not target_name):
        return print_list(agy, ollama, notes=a.notes, ctx=ctx, refresh=a.refresh)

    text = " ".join(raw_words).strip()
    for path in a.file:
        with open(path, encoding="utf-8", errors="replace") as f:
            text += "\n\n--- %s ---\n%s" % (os.path.basename(path), f.read())
    if not text:
        print("no question", file=sys.stderr)
        return 2

    max_wait_s = parse_duration_s(a.max_wait)
    err_color = route.use_color(sys.stderr)
    rules = route.load_roles()
    # In role mode the role's time budget is both limits unless given: a run past it is the model's "timeout",
    # counted against it, while a caller's own --deadline stays "deadline", not counted.
    role_spec = rules.get("roles", {}).get(a.role, {}) if a.role else {}
    budget = role_spec.get("score", {}).get("time_budget_s") if isinstance(role_spec, dict) else None
    budget_deadline = a.deadline is None and budget is not None
    if a.deadline is None:
        a.deadline = float(budget) if budget is not None else 1800
    if a.timeout is None:
        a.timeout = int(budget) if budget is not None else 300
    if ctx.get("note"):
        print(route.dim(f"[{ctx['note']}]", err_color), file=sys.stderr)

    # --- Role mode: route across the role's ordered list, falling over on quota errors ---
    if a.role:
        excluded = []
        carried_reasons = []
        while True:
            decision = route.pick(a.role, preset=active["preset"], exclude=excluded, allow_never=a.allow_never, tokens=a.tokens, rules=rules,
                                  context=ctx, needs=a.needs, prompt_tokens=len(text) // 4, effort=a.effort)
            _say_score(decision, err_color)
            passed_over = carried_reasons + list(decision.get("passed_over", []))
            waited_s = 0

            if decision.get("fallback"):
                for note in decision.get("experience", []):
                    print(route.dim(f"[experience: {note}]", err_color), file=sys.stderr)
                for r in passed_over:
                    print(route.dim(f"[passed over: {r}]", err_color), file=sys.stderr)
                print(route.yellow(decision["message"], err_color), file=sys.stderr)
                route.journal("ask.py", a.role, decision.get("model"), decision.get("provider"), decision.get("model"),
                              seconds=0.0, status="fallback", pool=decision.get("pool"), **_policy_kw(decision))
                return decision.get("exit_code", 6)

            if decision.get("exit_code") == route.EXIT_POLICY:  # no GPU will free a refused account: do not wait
                for r in passed_over:
                    print(route.dim(f"[passed over: {r}]", err_color), file=sys.stderr)
                print(route.red(decision["message"], err_color), file=sys.stderr)
                return _refused("ask.py", a.role, decision, ctx)

            if decision.get("exit_code") == route.EXIT_UNSUPPORTED:  # no local provider here: waiting will not help
                for r in passed_over:
                    print(route.dim(f"[passed over: {r}]", err_color), file=sys.stderr)
                print(route.red(decision["message"], err_color), file=sys.stderr)
                route.journal("ask.py", a.role, decision.get("model"), decision.get("provider") or "ollama", decision.get("model"),
                              seconds=0.0, status="unsupported", context=ctx["context"])
                return route.EXIT_UNSUPPORTED

            if not decision["picked"] and (decision.get("wait") or a.wait):
                wres = route.wait_until_free(role=a.role, preset=active["preset"], max_wait_s=max_wait_s, tokens=a.tokens, rules=rules, context=ctx, needs=a.needs,
                                             exclude=excluded, allow_never=a.allow_never, prompt_tokens=len(text) // 4, effort=a.effort)
                waited_s = wres.get("waited_s", 0)
                if not wres.get("picked"):
                    for r in passed_over:
                        print(route.dim(f"[passed over: {r}]", err_color), file=sys.stderr)
                    print(route.red(wres.get("reason", "GPU busy"), err_color), file=sys.stderr)
                    route.journal("ask.py", a.role, decision.get("model"), decision.get("provider") or "ollama",
                                  decision.get("model"), seconds=waited_s, status="busy")
                    return 3
                decision = wres
                passed_over = carried_reasons + list(decision.get("passed_over", []))

            if not decision["picked"]:
                for r in passed_over:
                    print(route.dim(f"[passed over: {r}]", err_color), file=sys.stderr)
                print(route.red(decision["message"], err_color), file=sys.stderr)
                if decision.get("exit_code") == route.EXIT_POLICY:
                    return _refused("ask.py", a.role, decision, ctx)
                return decision.get("exit_code", 2)

            model = decision["model"]
            label = decision.get("label") or model
            where = decision["provider"]
            pool_desc = decision.get("pool") or where
            warning = decision.get("warning")
            pol_kw = _policy_kw(decision)
            _say_account(decision, err_color)
            t0 = time.time()

            if route.is_local(where):
                ok_lock, why = route.claim_gpu(model)
                if not ok_lock:
                    carried_reasons.append(f"{label}: {why}")
                    excluded.append(model)
                    continue
                try:
                    code, out, tokens_used, log_path = _run_local(where, ollama, model, text, a)
                finally:
                    route.release_local_lock()
                dt = time.time() - t0
                st = f"waited {waited_s} s" if (code == 0 and waited_s > 0) else ("ok" if code == 0 else "error")
                route.journal("ask.py", a.role, model, where, model, tokens=tokens_used, seconds=dt, status=st, log=log_path, pool=pool_desc, **pol_kw)
                _print_header(model, pool_desc, passed_over, warning=warning)
                print(out or "(%s gave no text)" % label)
                return code
            else:
                cs_before = route.cloud_state(agy_path=agy, rules=rules, account=decision.get("account"))
                before_fracs = route.pool_fractions_for_model(where, model, cs_before)
                res_entry = route.reserve_quota("ask.py", a.role, where, model, tokens=a.tokens, rules=rules, cs=cs_before)
                if res_entry and res_entry.get("refused"):
                    print(route.dim(f"[passed over: {res_entry['refused']}]", err_color), file=sys.stderr)
                    carried_reasons.append(res_entry["refused"])
                    # The pool of this account is spent; the same model on another allowed account may still go.
                    excluded.append(f"{decision['account']}/{model}" if decision.get("account") else model)
                    continue
                # A conversation belongs to the model it was had with: after a fall-over the next model starts afresh,
                # rather than resuming its own unrelated last conversation or another provider's thread id.
                carry = a.thread or a.carry_on
                if carry and excluded:
                    print(route.yellow(f"[{label}: a new conversation; the carried one belonged to the model passed over]",
                                       err_color), file=sys.stderr)
                    carry = False
                try:
                    code, out, tokens_used, log_path, raw_all, st, conv_id, conv_tok = _run_cloud(
                        where, agy, model, text, carry,
                        a.timeout, effort=a.effort or decision.get("effort"), verbose=a.verbose, legacy=a.legacy_cli,
                        account=decision.get("account"),
                        deadline=a.deadline, ask=a.ask, ask_timeout=a.ask_timeout, max_tokens=a.max_tokens,
                        rules=rules, pool=pool_desc, reasoning=a.reasoning
                    )
                finally:
                    route.release_reservation(res_entry)
                dt = time.time() - t0
                if st == "deadline" and budget_deadline:
                    st = "timeout"
                tot_tok = (tokens_used or {}).get("total") or 0
                cs_after = route.cloud_state(agy_path=agy, force_refresh=(tot_tok > 50_000), rules=rules, account=pol_kw.get("account")) if os.environ.get("ASK_ROUTE_FAKE") is None else cs_before
                after_fracs = route.pool_fractions_for_model(where, model, cs_after)
                if st == "quota":
                    route.journal("ask.py", a.role, model, where, model, tokens=tokens_used, seconds=dt, status="quota",
                                  log=log_path, pool=pool_desc, before=before_fracs, after=after_fracs,
                                  conversation=conv_id, conversation_tokens=conv_tok, **pol_kw)
                    carried_reasons.append(f"{label}: quota/limit error from {where}")
                    # The pool of this account is spent; the same model on another allowed account may still go.
                    excluded.append(f"{decision['account']}/{model}" if decision.get("account") else model)
                    continue
                if st == "no_text":
                    route.journal("ask.py", a.role, model, where, model, tokens=tokens_used, seconds=dt, status="no_text",
                                  log=log_path, pool=pool_desc, before=before_fracs, after=after_fracs,
                                  conversation=conv_id, conversation_tokens=conv_tok, **pol_kw)
                    carried_reasons.append(f"{label}: empty answer from {where}")
                    excluded.append(model)
                    continue
                route.journal("ask.py", a.role, model, where, model, tokens=tokens_used, seconds=dt, status=st,
                              log=log_path, pool=pool_desc, before=before_fracs, after=after_fracs,
                              conversation=conv_id, conversation_tokens=conv_tok, **pol_kw)
                _print_header(model, pool_desc, passed_over, warning=warning)
                _print_reasoning(a.reasoning)
                print(out or "(%s gave no text: an agent's turn may end in a tool, not words; ask again, plainly)" % label)
                return code

    # --- Explicit model name mode: never replaced by another (--force overrides fit check) ---
    chk = route.check_name(target_name, allow_never=a.allow_never, tokens=a.tokens, force=a.force, rules=rules,
                           agy_path=agy, ollama_url=ollama, context=ctx, needs=a.needs)
    waited_s = 0
    if not chk["ok"]:
        if chk["status"] == "busy" and a.wait:
            wres = route.wait_until_free(name=target_name, max_wait_s=max_wait_s, tokens=a.tokens, rules=rules, context=ctx, needs=a.needs)
            waited_s = wres.get("waited_s", 0)
            if not wres.get("ok"):
                print(route.red(wres.get("reason", chk["reason"]), err_color), file=sys.stderr)
                route.journal("ask.py", None, chk.get("model"), chk["provider"], chk["model"],
                              seconds=waited_s, status="busy")
                return 3
            chk = wres
        else:
            print(route.red(chk["reason"], err_color), file=sys.stderr)
            if chk["status"] == "policy":
                return _refused("ask.py", None, chk, ctx)
            if chk["status"] in ("quota", "busy"):
                route.journal("ask.py", None, chk.get("model"), chk["provider"], chk["model"],
                              seconds=0.0, status=chk["status"], pool=chk.get("pool"))
            return chk.get("exit_code", 2)

    where, model = chk["provider"], chk["model"]
    label = chk.get("label") or model
    pool_desc = chk.get("pool") or where
    warning = chk.get("warning")
    chk["preset"] = active["preset"]
    pol_kw = _policy_kw(chk)

    if a.effort and where == "codex":
        # (The model's own list, from Codex's model/list. A warning, not a refusal: Codex itself is the judge, as before.)
        offered = adapters.get("codex").efforts(model)
        if offered and a.effort not in offered:
            print(route.yellow(f"[{label}: its list has no effort {a.effort} ({', '.join(offered)}); Codex decides]",
                               err_color), file=sys.stderr)

    if where == "claude-subscription" and a.claude:
        # Named with --claude: the user asked for this very model, so it is called (claude -p), not handed back.
        _say_account(chk, err_color)
        t0 = time.time()
        code, out, tokens_used, log_path, st, session = ask_claude(model, text, a.thread, a.deadline or None,
                                                                    effort=a.effort)
        if session:
            print(f"[conversation {session}: carry on with --thread {session}]", file=sys.stderr, flush=True)
        route.journal("ask.py", None, model, where, model, tokens=tokens_used, seconds=time.time() - t0, status=st,
                      log=log_path, pool=pool_desc, conversation=session, **pol_kw)
        if code != 0:
            print(route.red(f"{label}: {out[:300]}", err_color), file=sys.stderr)
            return code
        _print_header(model, pool_desc, [], warning=warning)
        print(out or "(%s gave no text; ask again, plainly)" % label)
        return 0

    if where == "claude-subscription":
        msg = f"use a Claude subagent ({model})"
        print(route.yellow(msg, err_color), file=sys.stderr)
        route.journal("ask.py", None, model, where, model, seconds=0.0, status="fallback", pool=pool_desc)
        return 6

    _say_account(chk, err_color)
    t0 = time.time()
    if route.is_local(where):
        ok_lock, why = route.claim_gpu(model)
        if not ok_lock:
            msg = f"{label}: GPU busy ({why})"
            print(route.red(msg, err_color), file=sys.stderr)
            route.journal("ask.py", None, model, where, model, seconds=0.0, status="busy")
            return 3
        try:
            code, out, tokens_used, log_path = _run_local(where, ollama, model, text, a)
        finally:
            route.release_local_lock()
        dt = time.time() - t0
        st = f"waited {waited_s} s" if (code == 0 and waited_s > 0) else ("ok" if code == 0 else "error")
        route.journal("ask.py", None, model, where, model, tokens=tokens_used, seconds=dt, status=st, log=log_path, pool=pool_desc, **pol_kw)
    else:
        cs_before = route.cloud_state(agy_path=agy, rules=rules, account=chk.get("account"))
        before_fracs = route.pool_fractions_for_model(where, model, cs_before)
        res_entry = route.reserve_quota("ask.py", None, where, model, tokens=a.tokens, rules=rules, cs=cs_before, force=a.force)
        if res_entry and res_entry.get("refused"):
            print(route.red(res_entry["refused"] + " (--force runs anyway)", err_color), file=sys.stderr)
            route.journal("ask.py", None, model, where, model, seconds=0.0, status="quota", pool=pool_desc)
            return 2
        try:
            code, out, tokens_used, log_path, raw_all, st, conv_id, conv_tok = _run_cloud(
                where, agy, model, text, a.thread or a.carry_on,
                a.timeout, effort=a.effort, verbose=a.verbose, legacy=a.legacy_cli, account=chk.get("account"),
                        deadline=a.deadline, ask=a.ask, ask_timeout=a.ask_timeout, max_tokens=a.max_tokens,
                        rules=rules, pool=pool_desc, reasoning=a.reasoning
            )
        finally:
            route.release_reservation(res_entry)
        dt = time.time() - t0
        tot_tok = (tokens_used or {}).get("total") or 0
        cs_after = route.cloud_state(agy_path=agy, force_refresh=(tot_tok > 50_000), rules=rules, account=pol_kw.get("account")) if os.environ.get("ASK_ROUTE_FAKE") is None else cs_before
        after_fracs = route.pool_fractions_for_model(where, model, cs_after)
        if st == "quota":
            route.journal("ask.py", None, model, where, model, tokens=tokens_used, seconds=dt, status="quota",
                          log=log_path, pool=pool_desc, before=before_fracs, after=after_fracs,
                          conversation=conv_id, conversation_tokens=conv_tok, **pol_kw)
            print(route.red(f"{label}: quota exhausted ({out[:240]})", err_color), file=sys.stderr)
            return 2
        route.journal("ask.py", None, model, where, model, tokens=tokens_used, seconds=dt, status=st,
                      log=log_path, pool=pool_desc, before=before_fracs, after=after_fracs,
                      conversation=conv_id, conversation_tokens=conv_tok, **pol_kw)

    _print_header(model, pool_desc, [], warning=warning)
    _print_reasoning(a.reasoning)
    print(out or "(%s gave no text: an agent's turn may end in a tool, not words; ask again, plainly)" % label)
    return code


if __name__ == "__main__":
    sys.exit(main())
