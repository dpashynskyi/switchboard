"""Local terminal handoffs. Transcript text is disclosed only by an explicit back mode."""
import json
import os
from pathlib import Path
import sys
import time

import providers
import route

TOOLS = str(Path(__file__).resolve().parents[2] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import broker


class PolicyRefusal(ValueError):
    pass


def _json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _rows():
    # Explicit path: fake provider state must not replace the actual local journal.
    return [r for r in route._read_journal_rows(journal_path=route.JOURNAL_PATH)
            if isinstance(r, dict) and "type" not in r and r.get("conversation")]


def find_conversation(arg):
    rows = _rows()
    newest = sorted(enumerate(rows), key=lambda item: (
        item[1].get("ts") or (route._parse_iso_utc(item[1].get("time", "")).timestamp()
                             if route._parse_iso_utc(item[1].get("time", "")) else 0), item[0]), reverse=True)
    if not arg:
        lasts = _json(Path(route.CACHE_DIR) / "ask_last_conversation.json").values()
        row = next((r for _, r in newest if r.get("script") == "ask.py" and r["conversation"] in lasts), None)
        arg = row["conversation"] if row else _json(Path(route.CACHE_DIR) / "reasoning" / "last.json").get("conversation")
        if not arg:
            raise ValueError("No last /ask conversation")
    ids = {r["conversation"] for r in rows}
    matches = [arg] if arg in ids else sorted(cid for cid in ids if cid.startswith(arg))
    if len(matches) != 1:
        raise ValueError(("Ambiguous conversation: " + ", ".join(matches)) if matches else "Unknown conversation: " + arg)
    row = next(r for _, r in newest if r["conversation"] == matches[0])
    result = {key: row.get(key) for key in ("conversation", "provider", "model", "account", "context", "crossed")}
    result["account"] = result["account"] or route.account_of(result["provider"])
    return result


def _path(conversation):
    safe = broker.ReasoningLog.safe_id(conversation)
    if safe in ("", ".", ".."):
        raise ValueError("Invalid conversation id")
    return Path(route.CACHE_DIR) / "handoff" / (safe + ".json")


def is_open(conversation):
    state = _json(_path(conversation))
    return bool(state.get("open") and state.get("conversation") == conversation)


def acp_history(executable, session_id, cwd, timeout=60):
    """Replay ACP without prompting a model or accepting any tool requests."""
    turns = []
    last_kind = None

    def notification(method, params):
        nonlocal last_kind
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        field = {"user_message_chunk": "user", "agent_thought_chunk": "reasoning",
                 "agent_message_chunk": "answer"}.get(kind)
        if not field:
            return
        if field == "user" and (not turns or last_kind != "user"):
            turns.append({"user": "", "reasoning": "", "answer": ""})
        if turns:
            turns[-1][field] += (update.get("content") or {}).get("text", "")
        last_kind = field

    def deny(method, params, request_id):
        raise broker.RpcError({"code": -32601, "message": "History replay accepts no requests"})

    def stop(process):
        if process.poll() is None:
            process.kill()

    transport = broker.Transport([executable, "acp"], cwd, deny, notification,
                                 lambda *a: None, lambda *a: None, stop)
    deadline = time.monotonic() + timeout
    try:
        transport.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}},
                          timeout=max(.01, deadline - time.monotonic()))
        transport.request("session/load", {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
                          timeout=max(.01, deadline - time.monotonic()))
        return turns
    finally:
        transport.close()


def _rollout(home, conversation):
    # Do not interpolate an untrusted id into a glob.
    files = [p for p in (Path(home) / "sessions").glob("**/rollout-*.jsonl")
             if p.name.endswith("-" + conversation + ".jsonl")]
    if len(files) != 1:
        raise ValueError("Cannot locate a unique Codex rollout for " + conversation)
    return files[0]


def _lines(path):
    return Path(path).read_text(encoding="utf-8").splitlines()


def _records(lines):
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue  # An actively appended rollout can end with a partial line.
        if isinstance(value, dict):
            yield value


def _usage(lines):
    usage = {}
    for row in _records(lines):
        payload = row.get("payload") or {}
        if row.get("type") == "event_msg" and payload.get("type") == "token_count":
            usage = (payload.get("info") or {}).get("total_token_usage") or usage
    return usage


def _codex_turns(lines, model):
    turns, current = [], None
    for row in _records(lines):
        payload = row.get("payload") or {}
        kind = row.get("type")
        if kind == "turn_context":
            model = payload.get("model") or model
            if current is not None:
                current["model"] = model
        elif kind == "event_msg" and payload.get("type") in ("task_started", "task_complete"):
            current = None
        elif kind == "response_item":
            if payload.get("type") == "message":
                text = "".join(c.get("text", "") for c in payload.get("content", [])
                               if c.get("type") in ("input_text", "output_text"))
                role = payload.get("role")
                if role == "user":
                    if not text.strip() or text.lstrip().startswith("<"):
                        continue
                    if current is None or current["answer"] or current["reasoning"]:
                        current = {"user": "", "answer": "", "reasoning": "", "model": model}
                        turns.append(current)
                    current["user"] += text
                elif role == "assistant" and current is not None:
                    current["answer"] += ("\n\n" if current["answer"] else "") + text
            elif payload.get("type") == "reasoning" and current is not None:
                current["reasoning"] += "\n".join(p.get("text", "") for p in payload.get("summary", []))
    return turns


def _event_fields(state):
    return {key: state.get(key) for key in ("conversation", "provider", "model", "account", "context", "crossed")}


def start(arg, context_flag=None):
    """Check the policy, mark where /ask's part ends, and return (command, conversation id)."""
    info = find_conversation(arg)
    cid, provider, account = info["conversation"], info["provider"], info["account"]
    ctx = route.resolve_context(context_flag)
    pol = route.policy_for(provider, context=ctx, account=account)
    info.update(context=ctx["context"], crossed=bool(pol["crossed"] or any(
        r.get("crossed") for r in _rows() if r["conversation"] == cid)))
    if not pol["allowed"]:
        route.journal_event("refused", **info, reason=pol["reason"])
        raise PolicyRefusal(pol["reason"])
    if pol["crossed"]:
        print(route.crossing_line(pol), file=sys.stderr)
    elif info["crossed"]:
        print("Warning: this conversation previously crossed work → personal account " + account, file=sys.stderr)
    if is_open(cid):
        raise ValueError("An open handoff already exists for " + cid)
    adapter = providers.get(provider)
    command = adapter.handoff(account, cid, info["model"])
    if command is None:
        raise ValueError("this provider cannot carry a conversation on in a terminal (or a command part is not printable ASCII)")
    room = route.CODEX_ROOM if provider == "codex" else route.ASK_ROOM
    state = dict(info, open=True, room=room, command=command, started=time.time())
    if provider == "codex":
        acc = next(a for a in adapter.accounts() if a["id"] == account)
        state["home"] = providers.expand(acc["where"])
        path = _rollout(state["home"], cid)
        lines = _lines(path)
        state["mark"] = {"path": str(path), "lines": len(lines), "total_token_usage": _usage(lines)}
    else:
        try:
            state["mark"] = len(acp_history(adapter.path(), cid, room))
            state["mark_from"] = "acp_history"
        except Exception:
            path = Path(route.CACHE_DIR) / "reasoning" / (broker.ReasoningLog.safe_id(cid) + ".md")
            state["mark"] = sum(line.startswith("## Turn ") for line in
                                (path.read_text(encoding="utf-8").splitlines() if path.exists() else []))
            state["mark_from"] = "reasoning"
            print("ACP history unavailable; marking by reasoning turn count", file=sys.stderr)
    path = _path(cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    broker.atomic_json(str(path), state)
    route.journal_event("open", **_event_fields(state))
    return command, cid


def back(conversation, mode):
    if mode not in ("full", "summary", "none"):
        raise ValueError("handoff mode must be full, summary or none")
    state = _json(_path(conversation))
    if not is_open(conversation):
        raise ValueError("No open handoff for " + conversation)
    terminal_tokens = None
    if state["provider"] == "codex":
        mark = state["mark"]
        lines = _lines(mark["path"])
        if len(lines) < mark["lines"]:
            raise ValueError("Codex rollout was truncated since handoff")
        turns = _codex_turns(lines[mark["lines"]:], state["model"])
        now, before = _usage(lines), mark["total_token_usage"]
        terminal_tokens = {short: now.get(long, 0) - before.get(long, 0) for short, long in (
            ("in", "input_tokens"), ("cached_in", "cached_input_tokens"), ("out", "output_tokens"),
            ("reasoning", "reasoning_output_tokens"), ("total", "total_tokens"))}
    else:
        turns = acp_history(providers.get("agy").path(), conversation, state["room"])[state["mark"]:]
    output = []
    if mode == "none":
        output.append(f"The user finished in the terminal and passed nothing on ({len(turns)} turns).")
    else:
        if mode == "summary":
            output.append(f"Terminal conversation: {len(turns)} turns.")
        for k, turn in enumerate(turns, 1):
            user, answer = broker.redact(turn["user"]), broker.redact(turn["answer"])
            if mode == "summary":
                user, answer = user[:300], answer.strip().split("\n\n", 1)[0][:400]
            output.append(f"### Terminal turn {k}\nOwner: {user}\nModel: {answer}")
        if mode == "summary":
            output.append("The full mode gives everything.")
    for turn in turns:
        log = broker.ReasoningLog(str(Path(route.CACHE_DIR) / "reasoning"), conversation,
                                  state["provider"], str(turn.get("model") or state["model"]) + " (terminal)")
        log.write(turn["reasoning"])
        log.close("summary" if turn["reasoning"] else "none")
    state.update(open=False, closed=time.time(), mode=mode, turns=len(turns))
    broker.atomic_json(str(_path(conversation)), state)
    fields = dict(_event_fields(state), mode=mode, turns=len(turns))
    if terminal_tokens is not None:
        fields["terminal_tokens"] = terminal_tokens
    route.journal_event("back", **fields)
    return broker.redact("\n\n".join(output))
