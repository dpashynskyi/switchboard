"""Interactive writer protocols. Standard library only; no authentication."""
import copy
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

AGY_BRAIN = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "brain")


def agy_transcript_path(session_id):
    return os.path.join(AGY_BRAIN, session_id, ".system_generated", "logs", "transcript.jsonl")


def agy_brain_checklist(session_id, since):
    """The plan agy keeps as a markdown artifact in its brain folder (agy 1.5.3 sends no ACP plan): the checklist of the
    newest top-level .md file written since `since` (wall clock), or [] when there is none."""
    folder = os.path.join(AGY_BRAIN, session_id or "")
    try:
        names = [n for n in os.listdir(folder) if n.lower().endswith(".md")]
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            mtime = os.path.getmtime(path)
            if mtime >= since:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    entries = checklist_entries(handle.read())
                if entries:
                    found.append((mtime, entries))
        except OSError:
            continue
    return max(found, key=lambda f: f[0])[1] if found else []


NO_HUMAN = "No human is available: decide yourself and say what you assumed."
# Plan item states that need no more work (ACP: completed; Codex: completed; the rest as some agents spell them).
TODO_CLOSED = ("completed", "complete", "done", "cancelled", "canceled", "skipped")
# A markdown checklist line, the plan agents write as text when they use no plan tool: "- [ ] a", "- [x] b", "1. [/] c".
CHECKLIST_LINE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[([ xX/~-])\][ \t]+(\S.*?)[ \t]*$", re.M)
CHECKLIST_MARK = {" ": "pending", "/": "in_progress", "x": "completed", "X": "completed", "-": "cancelled", "~": "cancelled"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
API_TOKEN = re.compile(r"sk-[A-Za-z0-9_-]{16,}|gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}"
                       r"|ya29\.[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9-]+"
                       r"|Bearer\s+(?=[A-Za-z._~+/-]*\d)[A-Za-z0-9._~+/-]{6,}=*|Bearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.I)
SENSITIVE = ("token", "secret", "authorization", "cookie", "apikey", "api_key", "api-key", "password", "passwd", "credential")
# KEY=value and Python-repr forms in free text: OPENAI_API_KEY=..., {'refresh_token': '...'}.
SENSITIVE_WORDS = "token|secret|authorization|cookie|api[_-]?key|password|passwd|credential"
# Two assignment shapes. A credential key (password, secret, api key, credential) hides any value, a bare number too
# (`password=hunter2`, `PIN_SECRET=123456`). A token key hides a value of 8+ characters with a letter, since `tokens=42` and
# `reasoningOutputTokens=3` are counts, not secrets. Quoted values honour escaped quotes (`PASSWORD="ab\"cd"`).
_QUOTED = r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'"
ASSIGNED = re.compile(r"((?:^|[\s,{(])[\w.-]*(?:secret|authorization|cookie|api[_-]?key|password|passwd|credential)(?!s\b)[\w.-]*\s*=\s*)"
                      r"(?:%s|[^\s,;]+)" % _QUOTED, re.I)
ASSIGNED_TOKEN = re.compile(r"((?:^|[\s,{(])[\w.-]*token(?!s)[\w.-]*\s*=\s*)(?:%s|(?=[^\s,;]*[A-Za-z])[^\s,;]{8,})" % _QUOTED, re.I)
REPR_PAIR = re.compile(r"('[^'\n]*(?:%s)[^'\n]*'\s*:\s*)'(?:\\.|[^'\\])*'" % SENSITIVE_WORDS, re.I)
PROTECTED_FOLDERS = (".ssh", ".gemini", ".codex", ".claude", ".aws", ".gnupg", ".kube", ".docker", ".config/gh",
                     ".config/ask", ".config/pii-guard", ".netrc", ".npmrc", ".pypirc", ".git-credentials")


def redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if isinstance(v, str) and any(s in str(k).lower() for s in SENSITIVE)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Also handle embedded JSON in stderr, tool output and progress strings.
        value = re.sub(r'("[^"\n]*(?:%s)[^"\n]*"\s*:\s*)"(?:\\.|[^"\\])*"' % SENSITIVE_WORDS,
                       r'\1"<redacted>"', value, flags=re.I)
        value = REPR_PAIR.sub(r"\1'<redacted>'", value)
        value = ASSIGNED_TOKEN.sub(r"\1<redacted>", ASSIGNED.sub(r"\1<redacted>", value))
        return API_TOKEN.sub("<redacted>", EMAIL.sub("<email>", JWT.sub("<jwt>", value)))
    return value


def mask_reasoning(value):
    """Replace reasoning text in place: items of type reasoning, agy thought chunks, Codex reasoning deltas."""
    if isinstance(value, dict):
        if value.get("type") == "reasoning":
            for field in ("summary", "content", "text"):
                if field in value:
                    value[field] = "<reasoning>"
        if value.get("sessionUpdate") == "agent_thought_chunk" and "content" in value:
            value["content"] = "<reasoning>"
        for child in value.values():
            mask_reasoning(child)
    elif isinstance(value, list):
        for child in value:
            mask_reasoning(child)
    return value


class ReasoningLog:
    """Line-buffered, redacted transcript shared by /ask and writer."""
    @staticmethod
    def safe_id(conversation):
        return re.sub(r"[^A-Za-z0-9._-]", "_", str(conversation))

    def __init__(self, directory, conversation, provider, model, kind_hint=None):
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, self.safe_id(conversation) + ".md")
        existing = ""
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as source:
                existing = source.read()
        self.turn = sum(line.startswith("## Turn ") for line in existing.splitlines()) + 1
        self.handle = open(self.path, "a", encoding="utf-8")
        self.buffer, self.pieces = "", []
        if not existing:
            self.handle.write(redact(f"# Reasoning: {provider} {model}, conversation {conversation}\n\n"))
        self.handle.write(redact(f"## Turn {self.turn}, {time.strftime('%Y-%m-%d %H:%M')}, {provider} {model}\n\n"))
        self.handle.flush()

    def _flush(self, text):
        if text.startswith("## Turn "):
            text = " " + text
        clean = redact(text)
        self.pieces.append(clean)
        self.handle.write(clean)
        self.handle.flush()

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._flush(line + "\n")
        if len(self.buffer) > 4000:
            # Cut at whitespace (emails and tokens hold none); with no whitespace keep a tail long enough for any token.
            cut = max(self.buffer.rfind(" ", 0, 3500), self.buffer.rfind("\t", 0, 3500))
            cut = cut + 1 if cut > 0 else len(self.buffer) - 512
            self._flush(self.buffer[:cut])
            self.buffer = self.buffer[cut:]

    @property
    def text(self):
        return "".join(self.pieces)

    def close(self, kind, empty_note=True):
        if self.handle.closed:
            return self.path
        if self.buffer:
            self._flush(self.buffer)
            self.buffer = ""
        if not self.pieces and empty_note:
            self.handle.write("(no reasoning from the provider this turn)")
        self.handle.write(f"\n\n_kind: {kind or 'none'}_\n\n")
        self.handle.close()
        return self.path


def log_base(path):
    return os.path.abspath(path[:-5] if path.endswith(".json") else path)


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".broker-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def answer_pending(base, option, text=""):
    base = log_base(base)
    try:
        with open(base + ".pending.json", encoding="utf-8") as handle:
            pending = json.load(handle)
    except (OSError, ValueError):
        raise ValueError("nothing pending for " + base)
    if (not isinstance(pending, dict) or not isinstance(pending.get("expires"), (int, float))
            or not isinstance(pending.get("options"), list) or "request" not in pending):
        raise ValueError("invalid pending request for " + base)
    if time.time() >= pending["expires"]:
        raise ValueError("pending request has expired")
    if option not in [o.get("id") for o in pending["options"] if isinstance(o, dict)]:
        raise ValueError("bad option: " + option)
    if pending.get("kind") == "question" and option == "other" and not text.strip():
        raise ValueError("text is required for other")
    atomic_json(base + ".answer.json", {"request": pending["request"], "option": option, "text": text})
    return pending


class RpcError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False))


class ResumeError(RpcError):
    """The writer may retry this run on its unchanged legacy path."""


class Transport:
    """One reader, one serialized request worker; humans never block response/usage delivery."""
    def __init__(self, command, cwd, handler, notification, event, progress, stop_tree, env=None):
        self.handler, self.notification = handler, notification
        def log_event(direction, value):
            # Reasoning text belongs only in its dedicated, line-redacted file, wherever it sits in a message
            # (a delta notification, an item, or the earlier turns a thread/resume result carries).
            if isinstance(value, dict):
                value = copy.deepcopy(value)
                if str(value.get("method", "")).startswith("item/reasoning/") and isinstance(value.get("params"), dict) \
                        and "delta" in value["params"]:
                    value["params"]["delta"] = "<reasoning>"
                mask_reasoning(value)
            event(direction, redact(value))
        self.event = log_event
        self.progress, self.stop_tree = progress, stop_tree
        self.write_lock, self.pending_lock = threading.Lock(), threading.Lock()
        self.pending, self.next_id = {}, 0
        self.requests = queue.Queue()
        self.closed = threading.Event()
        self.eof = threading.Event()
        self.failure = None
        self.cleaned = False
        kw = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, env=env, **kw)
        self.threads = [threading.Thread(target=target, daemon=True) for target in
                        (self._read, self._serve, self._stderr)]
        for thread in self.threads:
            thread.start()

    def send(self, message):
        with self.write_lock:
            self.event("out", message)
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params, timeout=None):
        response = queue.Queue()
        with self.pending_lock:
            self.next_id += 1
            request_id = self.next_id
            self.pending[request_id] = response
        try:
            self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout if timeout is not None else None
            while True:
                try:
                    message = response.get(timeout=.1)
                    if "error" in message:
                        raise RpcError(message["error"])
                    return message.get("result", {})
                except queue.Empty:
                    if self.failure:
                        raise self.failure
                    if self.eof.is_set() or self.closed.is_set():
                        raise RuntimeError("agent closed stdio before " + method + " completed")
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(method + " timed out")
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    self.event("in", {"raw": line.rstrip()})
                    self.progress("note: " + line.rstrip())
                    continue
                self.event("in", message)
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    if "id" in message:
                        self.requests.put(message)
                    else:
                        try:
                            self.notification(message["method"], message.get("params") or {})
                        except Exception as error:  # one odd notification must not end the reader for the whole run
                            self.event("error", {"notification": message.get("method"), "error": repr(error)})
                elif "id" in message:
                    with self.pending_lock:
                        waiter = self.pending.get(message["id"])
                    if waiter:
                        waiter.put(message)
        except Exception as error:
            if not self.closed.is_set():
                self.failure = error
        finally:
            self.eof.set()

    def _serve(self):
        while not self.closed.is_set():
            try:
                message = self.requests.get(timeout=.1)
            except queue.Empty:
                continue
            reply = {"jsonrpc": "2.0", "id": message["id"]}
            try:
                reply["result"] = self.handler(message["method"], message.get("params") or {}, message["id"])
            except RpcError as error:
                reply["error"] = error.error
            except Exception as error:
                self.progress("ERROR: request handler: " + str(error))
                reply["error"] = {"code": -32603, "message": "request handler failed"}
            if not self.closed.is_set():
                try:
                    self.send(reply)
                except OSError:
                    return

    def _stderr(self):
        for line in self.process.stderr:
            self.event("stderr", {"raw": line.rstrip()})
            self.progress("note: " + line.rstrip())

    def close(self):
        if self.cleaned:
            return
        self.cleaned = True
        self.closed.set()
        # Kill before closing stdin: a server exiting on EOF could orphan children.
        self.stop_tree(self.process)
        if os.name != "nt" and self.process.poll() is not None:
            # The group is ours even if its leader exited before cleanup.
            import signal
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.wait()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        # stdin is ours to close. stdout and stderr are closed only once their reader thread is done: a detached grandchild
        # that still holds the pipe's write end would otherwise block close() for as long as it lives.
        try:
            self.process.stdin.close()
        except OSError:
            pass
        for stream, thread in ((self.process.stdout, self.threads[0]), (self.process.stderr, self.threads[2])):
            if not thread.is_alive():
                stream.close()


def within(path, root):
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class Policy:
    def __init__(self, cwd, add_dirs=(), python="python", engine=False, home=None, read_only=False, engine_markers=()):
        self.cwd = os.path.realpath(cwd)
        self.folders = [self.cwd] + [os.path.realpath(d) for d in add_dirs]
        self.home = os.path.realpath(home or os.path.expanduser("~"))
        self.command = python + " -I agy_run.py"
        self.engine = engine
        # Words of a command that starts the machine's engine (its hooks' engine_markers): refused without --engine.
        # None: the machine's hooks failed, so an engine command cannot be told apart and every command but the exact
        # agy_run.py one is refused without --engine (fail closed, review of).
        self.engine_markers = None if engine_markers is None else tuple(str(m).lower() for m in engine_markers if m)
        self.read_only = read_only

    def path(self, value, cwd=None):
        if value.startswith("file://"):
            value = urllib.request.url2pathname(urllib.parse.urlparse(value).path)
        value = os.path.expanduser(value)
        # A relative request cwd is relative to the run folder, never to the broker's own folder.
        base = self.cwd if not cwd else os.path.join(self.cwd, os.path.expanduser(cwd))
        return os.path.realpath(value if os.path.isabs(value) else os.path.join(base, value))

    def decide(self, provider, kind, summary, paths=(), command=None, cwd=None, write=None):
        if kind == "question":
            return "human", "questions belong to a human"
        if kind == "file" and write == "destructive":
            return "human", "deleting or moving files needs a human decision"
        text = " ".join(([summary] if kind == "permission" else []) + [command or ""] + list(paths)).lower().replace("\\", "/")
        blocked = ("git push", "git commit", "taskkill", "kill ", "pkill", "security ", "rm -rf /")
        if (any(s in text for s in blocked) or re.search(r"(?:^|/)\.codex[^/\s]*", text)
                or any(re.search(r"(?:^|/)" + re.escape(f) + r"(?:/|$|\s)", text) for f in PROTECTED_FOLDERS)
                or re.search(r"\bgit\b[^;&|]*\b(?:push|commit)\b|\b(?:kill|security)\s", text)):
            return "deny", "standing rule forbids this command or path"
        if not self.engine and self.engine_markers is None and kind == "command" and command != self.command:
            return "deny", "the machine's hooks failed: an engine command cannot be told apart, so commands are refused"
        if not self.engine and any(m in text for m in self.engine_markers or ()):
            return "deny", "engine processes are forbidden"
        resolved = [self.path(p, cwd) for p in paths]
        if cwd:
            resolved.append(self.path(cwd))
        # Extract named absolute paths in commands as well as provider file locations.
        if command:
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            if len(words) > 2 and any(flag in words for flag in ("-c", "-lc")):
                try:
                    words += shlex.split(words[-1])
                except ValueError:
                    pass
            for word in words:
                if word.startswith(("/", "~/")) or word == ".." or word.startswith("../") or "/../" in word:
                    # An executable may live outside home; named file operands may not.
                    if not (word == words[0] and provider == "agy" and command == self.command):
                        resolved.append(self.path(word, cwd))
        canonical = " ".join(resolved).lower().replace("\\", "/")
        if any(re.search(r"/" + re.escape(f) + r"(?:/|$|\s)", canonical) for f in PROTECTED_FOLDERS) or "/.codex" in canonical:
            return "deny", "resolved path names a protected configuration folder"
        # The run's own folders may lie outside home (a scratch folder under /private/tmp).
        if any(not within(p, self.home) and not any(within(p, d) for d in self.folders) for p in resolved):
            return "deny", "path is outside the home folder"
        if self.read_only and (kind == "command" or write):
            return "human", "read-only run: writes and commands need a human"
        if provider == "agy" and kind == "command" and command == self.command and self.path(cwd or self.cwd) == self.cwd:
            return "allow", "exact agy_run.py command in the writer folder"
        if kind == "file" and paths and all(any(within(self.path(p, cwd), d) for d in self.folders) for p in paths):
            return "allow", "every file is under the run folders"
        return "human", "permission needs a human decision"


class HumanRequests:
    def __init__(self, base, provider, policy, progress, ask="human", timeout=600):
        self.base, self.provider, self.policy, self.progress = log_base(base), provider, policy, progress
        self.ask, self.timeout = ask, timeout
        self.lock, self.cancelled = threading.Lock(), threading.Event()
        self.last_reason = ""
        self.wait_lock = threading.Lock()
        self.waited = 0.0
        self.wait_started = None

    def waited_seconds(self):
        with self.wait_lock:
            return self.waited + (time.monotonic() - self.wait_started if self.wait_started is not None else 0)

    def choose(self, request_id, kind, summary, options, allow, deny, paths=(), command=None, cwd=None, write=None):
        with self.lock:
            decision, reason = self.policy.decide(self.provider, kind, summary, paths, command, cwd, write)
            if decision == "human" and self.ask == "never":
                decision, reason = "deny", "--ask never: no human is available"
            if decision == "allow" and allow is None:
                decision, reason = "deny", "provider offered no supported one-time allowance"
            self.progress("POLICY " + decision + ": " + reason)
            self.last_reason = reason
            if decision != "human":
                chosen = allow if decision == "allow" else deny
                self.progress(f"ANSWERED {kind} {chosen} by rule")
                return chosen, NO_HUMAN if kind == "question" else ""
            # A nonce makes stale answers harmless even when providers reuse their ids.
            nonce = str(request_id) + ":" + uuid.uuid4().hex
            now = time.time()
            pending = {"run": self.base, "request": nonce, "provider": self.provider, "kind": kind,
                       "summary": redact(summary), "options": options, "created": now, "expires": now + self.timeout}
            pending_path, answer_path = self.base + ".pending.json", self.base + ".answer.json"
            atomic_json(pending_path, pending)
            with self.wait_lock:
                self.wait_started = time.monotonic()
            line = (f"WAITING {kind}: {redact(summary).splitlines()[0] if summary else ''} "
                    f"(answer: python tools/writer.py --answer {shlex.quote(self.base)} <option id> [text])")
            self.progress(line)
            print(line, file=sys.stderr, flush=True)
            chosen, text, source = deny, NO_HUMAN if kind == "question" else "", "timeout"
            try:
                while time.time() < pending["expires"] and not self.cancelled.is_set():
                    try:
                        with open(answer_path, encoding="utf-8") as handle:
                            answer = json.load(handle)
                        os.unlink(answer_path)
                        valid = (answer.get("request") == nonce and answer.get("option") in [o["id"] for o in options]
                                 and isinstance(answer.get("text", ""), str)
                                 and (kind != "question" or answer.get("option") != "other" or str(answer.get("text") or "").strip()))
                        if valid:
                            chosen, text, source = answer["option"], answer.get("text") or "", "human"
                            break
                        self.progress("ignored invalid answer")
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError, TypeError, AttributeError):
                        self.progress("ignored invalid answer")
                    self.cancelled.wait(min(.5, max(0, pending["expires"] - time.time())))
            finally:
                with self.wait_lock:
                    self.waited += time.monotonic() - self.wait_started
                    self.wait_started = None
                try:
                    os.unlink(pending_path)
                except FileNotFoundError:
                    pass
                try:
                    # A late answer to this request must not stay on disk.
                    with open(answer_path, encoding="utf-8") as handle:
                        late = json.load(handle)
                    if isinstance(late, dict) and late.get("request") == nonce:
                        os.unlink(answer_path)
                except (OSError, ValueError):
                    pass
            self.last_reason = "refused by human" if source == "human" else "no human answer before timeout"
            self.progress(f"ANSWERED {kind} {chosen} by {source}")
            return chosen, text


def file_details(item):
    """Do not mistake new file contents for paths."""
    paths, diffs = [], []
    for change in (item.get("locations") or []) + (item.get("changes") or []) + (item.get("content") or []):
        if not isinstance(change, dict):
            continue
        kind = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        for source in (change, kind):
            for key in ("path", "movePath", "move_path", "oldPath", "newPath", "renameTo"):
                if isinstance(source.get(key), str):
                    paths.append(source[key])
        for key in ("diff", "oldText", "newText"):
            if change.get(key):
                diffs.append(str(change[key]))
    raw = item.get("rawInput") or item.get("args") or {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            normalized = key.replace("_", "").lower()
            if normalized.endswith(("path", "paths", "directory")) or normalized in ("targetfile", "filename", "cwd"):
                if isinstance(value, str):
                    paths.append(value)
                elif isinstance(value, list):
                    paths.extend(p for p in value if isinstance(p, str))
    paths = list(dict.fromkeys(paths))
    summary = "\n".join(paths) or item.get("title") or "file paths unavailable"
    if diffs:
        summary += "\n" + "\n".join("\n".join(diffs).splitlines()[:40])
    return paths, summary


def plan_entries(entries, text_key):
    """[{"text", "status"}] from an agent's plan update (ACP entries[].content, Codex plan[].step); bad rows skipped."""
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get(text_key), str) and entry[text_key].strip():
            out.append({"text": entry[text_key].strip(), "status": str(entry.get("status") or "pending")})
    return out


def checklist_entries(text):
    """[{"text", "status"}] from a markdown checklist ([ ] open, [/] in progress, [x] done, [-] or [~] given up)."""
    return [{"text": m.group(2), "status": CHECKLIST_MARK[m.group(1)]}
            for m in CHECKLIST_LINE.finditer(text if isinstance(text, str) else "")]


def acp_usage(raw):
    mapping = {"input_tokens": "inputTokens", "output_tokens": "outputTokens", "thinking_tokens": "thoughtTokens",
               "cache_read_tokens": "cachedReadTokens", "total_tokens": "totalTokens"}
    return {key: int(raw.get(source) or 0) for key, source in mapping.items()}


def codex_usage(raw):
    def part(camel, snake):
        return int(raw.get(camel, raw.get(snake)) or 0)
    t_in = part("inputTokens", "input_tokens")
    t_out = part("outputTokens", "output_tokens")  # reasoning is part of output (89307 + 703 = 90010 measured)
    # The measured app-server total already includes reasoning (e.g. 89307 + 703 = 90010).
    # Use its authoritative total; do not charge reasoning twice.
    total = raw.get("totalTokens", raw.get("total_tokens"))
    if total is not None:
        t_out = max(0, int(total) - t_in)
    return {"in": t_in, "out": t_out, "total": t_in + t_out,
            "cached_in": part("cachedInputTokens", "cached_input_tokens")}


class Driver:
    provider = None

    def __init__(self, cwd, model, base, progress, event, stop_tree, add_dirs=(), python="python", engine=False,
                 ask="human", ask_timeout=600, max_tokens=None, billed=None, command=None, home=None,
                 deadline=None, deadline_grace=10, env=None, read_only=False, reasoning_sink=None, engine_markers=(),
                 todo_turns=3):
        self.reasoning, self.reasoning_kind = [], None
        self.reasoning_sink = reasoning_sink
        self.env = env
        self.cwd, self.model, self.progress = cwd, model, progress
        self.policy = Policy(cwd, add_dirs, python, engine, home, read_only, engine_markers)
        self.human = HumanRequests(base, self.provider, self.policy, progress, ask, ask_timeout)
        self.event, self.stop_tree, self.command = event, stop_tree, command
        self.add_dirs, self.cap, self.billed = add_dirs, max_tokens, billed or (lambda t: t.get("total") or 0)
        self.conversation, self.transport = None, None
        self.answer, self.denied = [], []
        self.failed, self.quota, self.capped = False, False, None
        self.errors = []
        self.deadline, self.deadline_grace = deadline, deadline_grace
        self.timed_out, self.watchdog = None, None
        # The agent's own plan (its todo list) as it last sent it, and the turns that sent it back to the open items.
        self.plan, self.todo_turns, self.todo_carries = [], max(0, int(todo_turns or 0)), 0
        self.todo_last = None
        # Where the plan came from: "tool" (ACP plan, Codex turn/plan/updated) wins over "text" (a markdown checklist).
        self.plan_source = None
        self.started_wall = time.time()
        self.turn_ended = threading.Event()
        # One stop reason wins: the turn's end, the token cap or the deadline are claimed under this lock.
        self.stop_lock = threading.Lock()

    def add_reasoning(self, text, kind="summary"):
        # Local adapters may use kind="full" for raw reasoning.
        if not isinstance(text, str) or not text:
            return
        self.reasoning.append(text)
        if self.reasoning_kind != "full":
            self.reasoning_kind = kind
        if self.reasoning_sink:
            self.reasoning_sink(text)

    def start(self):
        self.started = time.monotonic()
        self.transport = Transport(self.command, self.cwd, self.handle, self.notification,
                                   self.event, self.progress, self.stop_tree, env=self.env)
        if self.deadline:
            self.watchdog = threading.Thread(target=self._watch_deadline, daemon=True)
            self.watchdog.start()

    def _watch_deadline(self):
        rpc = self.transport
        while not rpc.closed.wait(.2):
            if rpc.eof.is_set() or self.turn_ended.is_set() or self.capped is not None:
                return
            elapsed = time.monotonic() - self.started - self.human.waited_seconds()
            if elapsed < self.deadline:
                continue
            with self.stop_lock:
                if self.turn_ended.is_set() or self.capped is not None:
                    return
                self.timed_out = round(elapsed, 2)
            self.progress(f"ERROR: deadline {self.timed_out}s of agent time passed (human waits not counted); cancelling the turn")
            self.human.cancelled.set()
            until = time.monotonic() + self.deadline_grace
            try:
                self.cancel()
            except Exception as error:
                self.progress("note: deadline cancel failed: " + str(error))
            while not (self.turn_ended.is_set() or rpc.eof.is_set() or rpc.closed.is_set()):
                remaining = until - time.monotonic()
                if remaining <= 0:
                    break
                rpc.closed.wait(min(.05, remaining))
            if not rpc.closed.is_set() and rpc.process.poll() is None:
                self.stop_tree(rpc.process)
            return

    def set_plan(self, entries, source="tool"):
        if source == "text" and (self.plan_source == "tool" or not entries or entries == self.plan):
            return
        self.plan, self.plan_source = entries, source
        done = sum(e["status"].lower() in TODO_CLOSED for e in entries)
        self.progress(f"plan {done}/{len(entries)} done")

    def open_todos(self):
        return [e["text"] for e in self.plan if e["status"].lower() not in TODO_CLOSED]

    def todo_prompt(self):
        """The prompt that sends an agent whose turn ended back to the open items of its plan, or None when it may stop:
        nothing open, a stop (deadline, cap, quota, failure), --todo-turns spent, or a carry-on turn that closed nothing."""
        todos = self.open_todos()
        if (not todos or self.timed_out is not None or self.capped is not None or self.quota or self.failed
                or self.todo_carries >= self.todo_turns):
            return None
        if self.todo_last is not None and todos == self.todo_last:
            self.progress(f"note: the plan still has {len(todos)} open item(s) and the last carry-on closed none; stopping")
            return None
        self.todo_carries += 1
        self.todo_last = todos
        self.progress(f"carry on {self.todo_carries}/{self.todo_turns}: the turn ended with {len(todos)} open plan item(s)")
        marks = " (in your checklist, [x] for done, [-] for given up)" if self.plan_source == "text" else ""
        listed = "\n".join("- " + t for t in todos[:20]) + ("\n- ..." if len(todos) > 20 else "")
        return (f"Your turn ended, but your plan still has {len(todos)} open item(s):\n{listed}\n"
                f"Carry on with them now, as the brief says, and keep the plan up to date{marks}. If an item cannot be done, "
                "mark it so in your plan and say why; stop once every item is done or given up.")

    def run(self, prompt, conversation=None):
        try:
            return self._run(prompt, conversation)
        except (RuntimeError, TimeoutError):
            if self.timed_out is None:
                raise
            return self.result()

    def end_turn(self):
        with self.stop_lock:
            self.turn_ended.set()

    def known(self, conversation):
        self.conversation = conversation
        self.progress("conversation " + conversation)
        print(f"[conversation {conversation}: carry on with --continue {conversation} --model {self.model}]",
              file=sys.stderr, flush=True)

    def check_cap(self, tokens, live=False):
        amount = self.billed(tokens)
        with self.stop_lock:
            if self.cap is None or amount <= self.cap or self.capped is not None or self.timed_out is not None:
                return
            # A live reading that lost the race to the turn's end must not stop a finished run.
            if live and self.turn_ended.is_set():
                return
            self.capped = amount
            self.progress(f"ERROR: hard stop at {amount:,} billed tokens, over --max-tokens {self.cap:,}")
            self.human.cancelled.set()
            self.stop_tree(self.transport.process)

    def close(self):
        self.human.cancelled.set()
        if self.transport:
            self.transport.close()
        if self.watchdog and self.watchdog is not threading.current_thread():
            self.watchdog.join()


class AcpDriver(Driver):
    provider = "agy"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.usage = acp_usage({})
        self.last_denial = None
        self.tools = {}
        self.tools_lock = threading.Lock()
        self.denied_tool_id = None
        self.usage_watchdog = None
        self.live_tokens = {"in": 0, "out": 0, "total": 0, "cached_in": 0}

    def _read_transcript(self):
        try:
            with open(self.transcript_path, "rb") as handle:
                handle.seek(self.transcript_offset)
                data = handle.read()
                self.transcript_offset = handle.tell()
                self.transcript_found = True
        except FileNotFoundError:
            return []
        except OSError as error:  # there but unreadable (a sharing violation): said once, at the turn's end
            self.transcript_error = str(error)
            return []
        lines = (self.transcript_pending + data).split(b"\n")
        self.transcript_pending = lines.pop()
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict) and type(row.get("step_index")) is int:
                rows.append(row)
        return rows

    def _start_usage_watchdog(self):
        self.transcript_path = agy_transcript_path(self.conversation)
        self.transcript_offset, self.transcript_pending = 0, b""
        self.transcript_found, self.transcript_error = False, None
        # Resumed sessions retain earlier steps, including later revisions of those steps.
        baseline = {row["step_index"] for row in self._read_transcript()}
        # A line left unfinished by an earlier process is not this run's step.
        self.transcript_pending = b""
        self.usage_watchdog = threading.Thread(target=self._watch_usage, args=(baseline,), daemon=True)
        self.usage_watchdog.start()

    def _watch_usage(self, baseline):
        rows = {}
        rpc = self.transport
        while True:
            # One more read after the stop, so a run stopped between readings keeps its last steps.
            done = (self.turn_ended.wait(1) or self.capped is not None or self.timed_out is not None
                    or rpc.closed.is_set() or rpc.eof.is_set())
            for row in self._read_transcript():
                index = row["step_index"]
                if index in baseline or "input_tokens" not in row or row.get("source", "MODEL") != "MODEL":
                    continue
                usage = tuple(row.get(key, 0) for key in ("input_tokens", "output_tokens", "cache_read_tokens"))
                if all(type(value) is int and value >= 0 for value in usage):
                    rows[index] = usage
            incoming, outgoing, cached = (sum(usage[i] for usage in rows.values()) for i in range(3))
            self.live_tokens = {"in": incoming, "out": outgoing, "total": incoming + outgoing, "cached_in": cached}
            if done:
                return
            self.check_cap(self.live_tokens, live=True)

    def _usage_watchdog_done(self):
        if self.usage_watchdog and self.usage_watchdog is not threading.current_thread():
            self.usage_watchdog.join(5)

    def end_turn(self):
        ended = self.turn_ended.is_set()
        super().end_turn()
        self._usage_watchdog_done()
        if not ended and self.usage_watchdog and self.transcript_error:
            self.progress("note: agy transcript unreadable (" + self.transcript_error + "); --max-tokens was checked only at the turn's end")
        elif (not ended and self.usage_watchdog and not self.transcript_found
                and not os.path.isfile(self.transcript_path)):
            self.progress("note: agy transcript unavailable; --max-tokens was checked only at the turn's end")

    def close(self):
        super().close()
        if self.usage_watchdog and self.usage_watchdog is not threading.current_thread():
            self.usage_watchdog.join()

    def notification(self, method, params):
        if method != "session/update":
            self.progress(method)
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self.answer.append((update.get("content") or {}).get("text") or "")
        elif kind == "agent_thought_chunk":
            content = update.get("content")
            self.add_reasoning(content.get("text") if isinstance(content, dict) else None)
            self.progress("session/update agent_thought_chunk")
        elif kind in ("tool_call", "tool_call_update"):
            if kind == "tool_call" and self.last_denial and update.get("toolCallId") != self.denied_tool_id:
                self.last_denial = None
            with self.tools_lock:
                tool = self.tools.setdefault(update.get("toolCallId"), {})
                tool.update(update)
                tool = copy.deepcopy(tool)
            self.progress(f"{tool.get('name') or tool.get('title') or 'tool'} {tool.get('status', '')} " +
                          json.dumps(redact({k: tool[k] for k in ("rawInput", "rawOutput") if k in tool}), ensure_ascii=False))
        elif kind == "plan":
            self.set_plan(plan_entries(update.get("entries"), "content"))
        elif kind != "user_message_chunk":
            self.progress("session/update " + str(kind))

    def handle(self, method, params, request_id):
        if method != "session/request_permission":
            raise RpcError({"code": -32601, "message": "unknown server method: " + method})
        if self.timed_out is not None:  # the turn was cancelled: ACP's answer for a request left hanging
            return {"outcome": {"outcome": "cancelled"}}
        with self.tools_lock:
            tool = copy.deepcopy(self.tools.get((params.get("toolCall") or {}).get("toolCallId"), {}))
        tool.update(params.get("toolCall") or {})
        raw = tool.get("rawInput") or tool.get("args") or {}
        command = raw.get("CommandLine") or raw.get("command") if isinstance(raw, dict) else None
        kind = "command" if tool.get("kind") == "execute" else "file" if tool.get("kind") in ("read", "edit", "delete", "move") else "permission"
        paths, summary = file_details(tool)
        if kind == "command":
            summary = command or tool.get("title") or "command unavailable"
        offered = [o for o in params.get("options", []) if o.get("kind") in ("allow_once", "reject_once")]
        allow = next((o["optionId"] for o in offered if o["kind"] == "allow_once"), None)
        deny = next((o["optionId"] for o in offered if o["kind"] == "reject_once"), None)
        if deny is None:
            raise RpcError({"code": -32602, "message": "no reject_once option offered"})
        options = [{"id": o["optionId"], "label": o.get("name") or o["optionId"]} for o in offered]
        chosen, _ = self.human.choose(request_id, kind, summary, options, allow, deny, paths, command,
                                      (raw.get("cwd") or raw.get("Cwd") or raw.get("WorkingDirectory")) if isinstance(raw, dict) else None,
                                      write="destructive" if tool.get("kind") in ("delete", "move") else tool.get("kind") == "edit")
        if chosen != allow:
            self.denied_tool_id = tool.get("toolCallId")
            self.last_denial = (tool.get("name") or tool.get("title") or "tool", summary.splitlines()[0], self.human.last_reason)
            self.denied.append({"tool": self.last_denial[0], "target": self.last_denial[1], "reason": self.last_denial[2]})
        return {"outcome": {"outcome": "selected", "optionId": chosen}}

    def cancel(self):
        if self.conversation:
            self.transport.notify("session/cancel", {"sessionId": self.conversation})

    def _run(self, prompt, conversation=None):
        self.start()
        rpc = self.transport
        rpc.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, 30)
        params = {"cwd": self.cwd, "mcpServers": []}
        if conversation:
            params["sessionId"] = conversation
        session = rpc.request("session/resume" if conversation else "session/new", params, 30)
        self.known(session.get("sessionId") or conversation)
        if self.cap is not None:
            self._start_usage_watchdog()
        rpc.request("session/set_model", {"sessionId": self.conversation, "modelId": self.model}, 30)
        # Up to three carry-on turns after a refusal, and up to --todo-turns more while the plan has open items.
        refusals = 0
        while self.timed_out is None:
            self.last_denial = None
            answer_mark = len(self.answer)
            result = rpc.request("session/prompt", {"sessionId": self.conversation,
                                 "prompt": [{"type": "text", "text": prompt}]})
            # agy sends no ACP plan: its checklist in this turn's answer, then (newer) the plan artifact in its brain folder.
            self.set_plan(checklist_entries("".join(self.answer[answer_mark:])), "text")
            self.set_plan(agy_brain_checklist(self.conversation, self.started_wall - 2), "text")
            for key, value in acp_usage(result.get("usage") or {}).items():
                self.usage[key] += value
            tokens = {"in": self.usage["input_tokens"], "out": self.usage["output_tokens"],
                      "total": self.usage["total_tokens"], "cached_in": self.usage["cache_read_tokens"]}
            self.check_cap(tokens)
            if self.timed_out is not None or self.capped is not None or result.get("stopReason") != "end_turn":
                break
            if self.last_denial and refusals < 3:
                refusals += 1
                tool, target, reason = self.last_denial
                prompt = (f"Your {tool} on {target} was refused: {reason}. Carry on with the rest of the brief without it; "
                          "if you cannot, say what you need and stop.")
                continue
            prompt = self.todo_prompt()
            if prompt is None:
                break
        self.end_turn()
        return self.result()

    def result(self):
        if self.turn_ended.is_set() or self.capped is not None or self.timed_out is not None:
            self._usage_watchdog_done()  # its last reading first
        usage, live = self.usage, self.live_tokens
        # Stopped inside a turn (the cap, the deadline), its result never came: the transcript's steps are this run's usage,
        # or the journal would count the run as nothing.
        if live["total"] > usage["input_tokens"] + usage["output_tokens"]:
            usage = {"input_tokens": live["in"], "output_tokens": live["out"], "thinking_tokens": usage["thinking_tokens"],
                     "cache_read_tokens": live["cached_in"], "total_tokens": live["total"] + live["cached_in"]}
        return {"response": "".join(self.answer).strip(), "conversation_id": self.conversation,
                "reasoning": "".join(self.reasoning), "reasoning_kind": self.reasoning_kind,
                "usage": usage, "denied_actions": self.denied, "failed": self.failed, "deadline": self.timed_out,
                "open_todos": self.open_todos(), "todo_turns": self.todo_carries}


class CodexAppDriver(Driver):
    provider = "codex"

    def __init__(self, *args, sandbox="workspace-write", effort="high", reasoning_summary=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sandbox, self.effort = sandbox, effort
        self.reasoning_summary = reasoning_summary
        self.reasoning_deltas, self.reasoning_parts = set(), {}
        self.last_reasoning_item, self.last_reasoning_kind = None, None
        self.items, self.rates, self.final = {}, None, []
        self.total, self.baseline = {}, {}
        self.tokens = codex_usage({})
        self.active = False
        self.usage_ready = threading.Event()
        self.completed = self.turn_ended
        # A turn that ended with open plan items: the run goes on with next_prompt instead of ending.
        self.turn_done, self.next_prompt = threading.Event(), None
        self.turn_id = None

    def reasoning_piece(self, item_id, text, kind="summary", index=None):
        if not isinstance(text, str) or not text:
            return
        if self.last_reasoning_item == item_id and self.last_reasoning_kind not in (None, kind):
            self.add_reasoning("\n\n", kind)
        elif self.last_reasoning_item != item_id:
            if self.reasoning:
                self.add_reasoning("\n\n", kind)
            self.last_reasoning_item = item_id
        elif index is not None and self.reasoning_parts.get((item_id, kind), index) != index:
            self.add_reasoning("\n\n", kind)
        self.last_reasoning_kind = kind
        if index is not None:
            self.reasoning_parts[item_id, kind] = index
        self.add_reasoning(text, kind)

    def update_tokens(self):
        delta = {k: max(0, v - self.baseline.get(k, 0)) for k, v in self.total.items()}
        self.tokens = codex_usage(delta)
        if self.active:
            self.check_cap(self.tokens)

    def notification(self, method, params):
        if method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            item_id = params.get("itemId")
            if params.get("delta"):
                self.reasoning_deltas.add(item_id)
            full = method == "item/reasoning/textDelta"
            self.reasoning_piece(item_id, params.get("delta"), "full" if full else "summary",
                                 params.get("contentIndex" if full else "summaryIndex"))
        elif method == "item/reasoning/summaryPartAdded":
            item_id, index = params.get("itemId"), params.get("summaryIndex", 0)
            if index > 0 and self.last_reasoning_item == item_id and self.reasoning_parts.get((item_id, "summary")) != index:
                self.add_reasoning("\n\n")
                self.reasoning_parts[item_id, "summary"] = index
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            self.items.setdefault(item.get("id"), {}).update(item)
            kind = item.get("type")
            if kind == "reasoning" and method == "item/completed" and item.get("id") not in self.reasoning_deltas:
                for field, reasoning_kind in (("summary", "summary"), ("content", "full")):
                    parts = item.get(field) or []
                    text = "\n\n".join(p if isinstance(p, str) else p.get("text", "") for p in parts if isinstance(p, (str, dict)))
                    self.reasoning_piece(item.get("id"), text, reasoning_kind)
                self.reasoning_deltas.add(item.get("id"))
            elif kind == "commandExecution":
                self.progress(f"command {item.get('command', '')} [{item.get('status', method)}, exit {item.get('exitCode')}]")
            elif kind == "fileChange":
                for change in item.get("changes") or []:
                    self.progress(f"file {change.get('kind', '')} {change.get('path', '')}")
            elif kind == "agentMessage" and method == "item/completed":
                # Commentary ("I'm reading the brief first") is not the answer when a final one comes.
                (self.final if item.get("phase") == "final_answer" else self.answer).append(item.get("text") or "")
                self.set_plan(checklist_entries(item.get("text")), "text")  # a checklist written as text, no plan tool
        elif method == "thread/tokenUsage/updated":
            self.total = (params.get("tokenUsage") or {}).get("total") or {}
            self.update_tokens()
            self.usage_ready.set()
        elif method == "account/rateLimits/updated":
            limits = params.get("rateLimits") or {}
            rates = sorted((k, v.get("usedPercent")) for k, v in limits.items() if isinstance(v, dict) and "usedPercent" in v)
            if rates and rates != self.rates:
                self.rates = rates
                self.progress("rate limits " + ", ".join(f"{k} {v}% used" for k, v in rates))
            self.quota |= limits.get("rateLimitReachedType") is not None
        elif method == "turn/plan/updated":
            self.set_plan(plan_entries(params.get("plan"), "step"))
        elif method == "turn/started":
            self.turn_id = (params.get("turn") or {}).get("id") or self.turn_id
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if self.timed_out is None:
                self.failed |= turn.get("status") == "failed" or bool(turn.get("error"))
            if turn.get("error"):
                self.errors.append(json.dumps(turn["error"]))
            self.next_prompt = self.todo_prompt() if turn.get("status", "completed") == "completed" else None
            if self.next_prompt is None:
                self.end_turn()
            else:
                self.turn_done.set()
        elif method == "error":
            self.errors.append(json.dumps(params))
            self.progress("ERROR: " + json.dumps(params))
        else:
            self.progress(method)

    def handle(self, method, params, request_id):
        if method == "mcpServer/elicitation/request":
            self.progress("POLICY deny: elicitation is disabled")
            self.progress("ANSWERED permission decline by rule")
            return {"action": "decline"}
        if method == "item/tool/requestUserInput":
            answers = {}
            for question in params.get("questions") or []:
                options = [{"id": o["label"], "label": o["label"]} for o in question.get("options") or []]
                if question.get("isOther") or not options:
                    options.append({"id": "other", "label": "other"})
                chosen, text = self.human.choose(request_id, "question", question.get("question") or "Question",
                                                 options, None, None)
                answers[question["id"]] = {"answers": [text if chosen in (None, "other") else chosen]}
            return {"answers": answers}
        if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                          "item/permissions/requestApproval"):
            raise RpcError({"code": -32601, "message": "unknown server method: " + method})
        command, paths, write = None, [], None
        if method == "item/commandExecution/requestApproval":
            kind, command = "command", params.get("command")
            summary = command or "command unavailable"
            offered = params.get("availableDecisions") or []
            # Persistent amendments are never offered to the human.
            allow = "accept" if "accept" in offered else None
            deny = "decline" if "decline" in offered else "cancel" if "cancel" in offered else None
            if deny is None:
                raise RpcError({"code": -32602, "message": "no supported refusal decision offered"})
        elif method == "item/fileChange/requestApproval":
            kind, write = "file", True
            paths, summary = file_details(self.items.get(params.get("itemId"), {}))
            allow, deny = "accept", "decline"
        else:
            kind = "permission"
            summary = json.dumps(params.get("permissions") or {}, ensure_ascii=False)
            # Permission profiles grant capabilities rather than an accept/decline enum.
            allow, deny = "allow", "deny"
            permissions = params.get("permissions") or {}
            fs = permissions.get("fileSystem") or {}
            write = bool(fs.get("write"))
            paths = list(fs.get("read") or []) + list(fs.get("write") or [])
            # Only a pure read request is a file read; any other capability stays a permission.
            if self.policy.read_only and paths and not write and set(permissions) <= {"fileSystem"} and set(fs) <= {"read"}:
                kind = "file"
        options = [{"id": v, "label": v} for v in (allow, deny) if v is not None]
        chosen, _ = self.human.choose(request_id, kind, summary, options, allow, deny, paths,
                                      command, params.get("cwd"), write=write)
        if chosen != allow:
            self.denied.append({"kind": kind, "target": summary, "reason": self.human.last_reason})
        if method == "item/permissions/requestApproval":
            return {"permissions": copy.deepcopy(params.get("permissions") or {}) if chosen == allow else {}, "scope": "turn"}
        return {"decision": chosen}

    def cancel(self):
        if self.conversation and self.turn_id:
            self.transport.request("turn/interrupt", {"threadId": self.conversation, "turnId": self.turn_id},
                                   timeout=self.deadline_grace)

    def _run(self, prompt, conversation=None):
        self.start()
        rpc = self.transport
        rpc.request("initialize", {"clientInfo": {"name": "switchboard-writer", "version": "1.0"}}, 30)
        rpc.notify("initialized")
        try:
            start_params = {"cwd": self.cwd, "model": self.model, "sandbox": self.sandbox,
                            "approvalPolicy": "on-request"}
            if self.add_dirs:
                start_params["config"] = {"sandbox_workspace_write.writable_roots": [os.path.abspath(d) for d in self.add_dirs]}
            # Resume with the same sandbox and approvals, so a resumed thread cannot keep wider ones.
            result = rpc.request("thread/resume", dict(start_params, threadId=conversation), 30) if conversation else rpc.request(
                "thread/start", start_params, 30)
        except RpcError as error:
            if conversation:
                raise ResumeError(error.error)
            raise
        thread = result.get("thread") or {}
        self.known(thread.get("id") or conversation)
        if conversation:
            if not self.total and not thread.get("tokenUsage"):
                self.usage_ready.wait(1)
            self.total = (thread.get("tokenUsage") or {}).get("total") or self.total
            self.baseline = copy.deepcopy(self.total)
            self.update_tokens()  # a run that ends before the first update bills nothing of the earlier turns
            if not self.baseline:
                # Never silently bill earlier turns as this run. Older servers can use exec resume.
                raise ResumeError({"message": "resume supplied no pre-run token total"})
        if self.timed_out is not None:
            return self.result()
        self.active = True
        turn_params = {"threadId": self.conversation, "input": [{"type": "text", "text": prompt}]}
        if self.effort is not None:
            turn_params["effort"] = self.effort
        if self.reasoning_summary is not None:
            turn_params["summary"] = self.reasoning_summary
        while True:
            self.turn_done.clear()
            turn_result = rpc.request("turn/start", turn_params)
            self.turn_id = (turn_result.get("turn") or {}).get("id") or self.turn_id
            while not (self.completed.is_set() or self.turn_done.wait(.1)):
                if self.capped is not None:
                    break
                if rpc.failure:
                    raise rpc.failure
                if rpc.eof.is_set():
                    raise RuntimeError("agent closed stdio before turn/completed")
            if self.completed.is_set() or self.capped is not None or self.timed_out is not None:
                break
            turn_params = dict(turn_params, input=[{"type": "text", "text": self.next_prompt}])
        self.end_turn()
        return self.result()

    def result(self):
        return {"response": "\n\n".join(self.final or self.answer).strip(), "conversation_id": self.conversation,
                "reasoning": "".join(self.reasoning), "reasoning_kind": self.reasoning_kind,
                "usage": self.tokens, "failed": self.failed, "denied_actions": self.denied, "deadline": self.timed_out,
                "open_todos": self.open_todos(), "todo_turns": self.todo_carries}
