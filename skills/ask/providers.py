"""Provider adapters and the automatic inventory of /ask.

Each provider (agy, Codex, Ollama, the Claude subscription) is one adapter with the same face:

    detect() whether the provider is on this machine and where (machine.json "paths", PATH, usual places, API)
    accounts() its accounts: id, email when it can be read, the folder or profile
    models(account) the live list of the account's models
    efforts(model) the model's reasoning efforts
    usage(account) its quota pools
    run(account, model, effort, prompt, ...) the call itself (ask.py's own functions)

The inventory (provider -> account -> model -> efforts) is built from them; it knows nothing of roles or of the policy, which
are read beside it by --list. The live lists keep the providers' own caches (agy_models.json, codex_models*.json, a day).
Standard library only; with a fake state (ASK_ROUTE_FAKE) nothing outside is asked.
"""
import base64
import glob
import json
import os
import re
import shutil
import shlex
import sys
import urllib.request
import urllib.error

import route

# A model's efforts, weakest first: agy names them in the model id, Codex lists them, the Claude subagent takes them.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
_AGY_EFFORT = re.compile(r"^(?P<family>.+)-(?P<effort>" + "|".join(EFFORT_ORDER) + r")$")


def effort_rank(effort):
    return EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)


def sort_efforts(efforts):
    seen = []
    for e in efforts:
        if e and e not in seen:
            seen.append(e)
    return sorted(seen, key=effort_rank)


def split_agy_id(model_id):
    """'gemini-4-argon-high' -> ('gemini-4-argon', 'high'); an id without an effort suffix -> (id, None)."""
    m = _AGY_EFFORT.match(str(model_id or ""))
    return (m.group("family"), m.group("effort")) if m else (str(model_id or ""), None)


def _home():
    # Patched by the tests, so that no real ~/.codex is read there.
    return getattr(route, "ACCOUNTS_HOME", route.HOME)


def expand(where):
    """'~/.codex-work' -> the folder, under route.ACCOUNTS_HOME rather than the real home when the tests patch it."""
    s = str(where or "")
    if s == "~" or s.startswith("~/") or s.startswith("~" + os.sep):
        s = os.path.join(_home(), s[2:])
    return os.path.normpath(os.path.expandvars(s))


def _configured(name, fake=None):
    """What machine.json "paths" (over the old models.json) says of a tool: (value, source).
    value: a path or URL; "auto" (look for it); None (null: not on this machine, its models are never offered)."""
    machine = route.load_machine(fake=fake)
    paths = machine.get("paths") if isinstance(machine.get("paths"), dict) else {}
    if name in paths:
        v = paths[name]
        if v is None:
            return None, "machine.json"
        if isinstance(v, str) and v.strip():
            return v.strip(), "machine.json"
    if route.resolve_fake(fake) is None:
        try:
            with open(route.CONFIG_PATH, encoding="utf-8") as f:
                old = json.load(f)
            v = old.get(name) if isinstance(old, dict) else None
            if isinstance(v, str) and v.strip():
                return v.strip(), "models.json"
        except (OSError, ValueError):
            pass
    return "auto", "auto"


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def _terminal_line(parts, room, home=None):
    values = [room] + parts + ([home] if home is not None else [])
    if any(not isinstance(v, str) or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in values):
        return None
    prefix = "CODEX_HOME=" + shlex.quote(home) + " " if home is not None else ""
    return "cd " + shlex.quote(room) + " && " + prefix + " ".join(shlex.quote(v) for v in parts)


class Adapter:
    name = ""
    title = ""
    local = False  # runs on this machine's GPU: the GPU lock, the hooks and local_only roles apply
    # The usual places an installer puts the tool, tried after PATH (and after the configured path).
    exe_names = ()

    def __init__(self, fake=None):
        self.fake = fake
        self.fk = route.resolve_fake(fake)

    # --- detect ---
    def candidates(self):
        return []

    def detect(self):
        """{'present', 'path', 'source', 'note'}: the configured path first; "auto" looks on PATH and the usual places;
        null in machine.json says the provider is not on this machine."""
        value, source = _configured(self.name, self.fake)
        if value is None:
            return {"present": False, "path": None, "source": source,
                    "note": f"paths.{self.name} is null in ~/.config/ask/machine.json: not on this machine"}
        if self.fk is not None:
            forced = (self.fk.get("detect") or {}).get(self.name)
            if isinstance(forced, dict):
                return dict({"present": True, "path": self.name, "source": "fake", "note": None}, **forced)
            return {"present": True, "path": value if value != "auto" else self.name, "source": "fake", "note": None}
        if value != "auto":
            return {"present": True, "path": value, "source": source, "note": None}
        return self._auto()

    def _auto(self):
        for exe in self.exe_names:
            found = shutil.which(exe)
            if found:
                return {"present": True, "path": found, "source": "PATH", "note": None}
        found = _first_existing(self.candidates())
        if found:
            return {"present": True, "path": found, "source": "usual place", "note": None}
        return {"present": False, "path": None, "source": "auto",
                "note": f"not found on PATH or in {', '.join(self.candidates()) or 'the usual places'}"}

    def path(self, default=None):
        d = self.detect()
        return d["path"] if d["present"] and d["path"] else default

    # --- accounts ---
    def accounts(self):
        return [{"id": self.name, "where": None, "email": None, "active": True}]

    def active_account(self):
        accs = self.accounts()
        return next((a for a in accs if a.get("active")), accs[0] if accs else None)

    def email(self, account_id):
        return next((a.get("email") for a in self.accounts() if a["id"] == account_id), None)

    # --- models, efforts, usage ---
    def models(self, account=None, force_refresh=False):
        return False, []

    def from_roles(self):
        """When the live list cannot be read: the ids roles.json names for this provider (as route.models did)."""
        rules = route.load_roles()
        ids = [mid for (prov, mid) in route.role_usage(rules) if prov == self.name]
        ids += [mid for mid, inf in (rules.get("models") or {}).items() if inf.get("provider") == self.name and mid not in ids]
        return [{"id": mid, "label": mid} for mid in ids]

    def efforts(self, model):
        return []

    def usage(self, account=None, cs=None):
        cs = cs or route.cloud_state(fake=self.fake)
        return [p for p in cs.get("pools", []) if p.get("provider") == self.name]

    def handoff(self, account, conversation, model=None):
        return None

    def run(self, account, model, effort, prompt, **kw):
        raise NotImplementedError(f"{self.name}: no run")


class AgyAdapter(Adapter):
    """Antigravity CLI. One account per machine: agy 1.5.0 signs in once, shared with the desktop Antigravity (~/.gemini), and
    names no profile; its email is in neither `agy models` nor `/usage` (open questions)."""
    name = "agy"
    title = "Antigravity CLI"
    exe_names = ("agy",)

    def handoff(self, account, conversation, model=None):
        parts = [self.path(), "--conversation", conversation]
        if model:
            parts += ["--model", model]
        return _terminal_line(parts, route.ASK_ROOM)

    def candidates(self):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(route.HOME, "AppData", "Local")
        return [os.path.join(route.HOME, ".local", "bin", "agy"), os.path.join(local, "agy", "bin", "agy.exe")]

    def accounts(self):
        return [{"id": "agy", "where": "~/.gemini", "email": None, "active": True}]

    def models(self, account=None, force_refresh=False):
        """Each agy id is one model at one effort ('gemini-4-argon-high'): grouped here by family, the efforts beside it."""
        ok, items = route.fetch_agy_models(agy_path=self.path(), force_refresh=force_refresh, fake=self.fake)
        if not ok:
            items = self.from_roles()
        fams = {}
        for it in items:
            fam, eff = split_agy_id(it["id"])
            row = fams.setdefault(fam, {"id": fam, "family": fam, "ids": {}, "label": it.get("label", fam)})
            row["ids"][eff or ""] = it["id"]
        out = []
        for row in fams.values():
            row["efforts"] = sort_efforts(e for e in row["ids"] if e)
            row["label"] = re.sub(r"\s*\((?:%s)\)\s*$" % "|".join(EFFORT_ORDER), "", row["label"], flags=re.I)
            out.append(row)
        return ok, out

    def efforts(self, model):
        fam, _ = split_agy_id(model)
        ok, rows = self.models()
        row = next((r for r in rows if r["family"] in (fam, model)), None) if ok else None
        return list(row["efforts"]) if row else []

    def run(self, account, model, effort, prompt, carry_on=False, timeout_s=300, verbose=False, agy=None, legacy=False, **kw):
        """agy's effort is part of the id: an effort given picks the family's id at that effort."""
        import ask
        if effort:
            fam, _ = split_agy_id(model)
            model = f"{fam}-{effort}"
        if not legacy:
            return ask.ask_broker("agy", model, prompt, carry_on, verbose=verbose,
                                  executable=agy or self.path(route.AGY_DEFAULT), **kw)
        return ask._run_agy_with_retry_and_accounting(agy or self.path(route.AGY_DEFAULT), model, prompt, carry_on,
                                                      timeout_s, name=model, verbose=verbose)


def _jwt_claims(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def codex_email(folder):
    """The email of a Codex sign-in, from the id_token in <CODEX_HOME>/auth.json; None when there is none. Read only for the
    account's label (work_domains); it is never sent anywhere."""
    try:
        with open(os.path.join(folder, "auth.json"), encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return None
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str):
        return None
    claims = _jwt_claims(token)
    email = claims.get("email") or (claims.get("https://api.openai.com/profile") or {}).get("email")
    return str(email) if email else None


class CodexAdapter(Adapter):
    """Codex CLI (ChatGPT plan). An account is a CODEX_HOME: ~/.codex, ~/.codex-work, ..."""
    name = "codex"
    title = "Codex"
    exe_names = ("codex",)

    def candidates(self):
        return ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                os.path.join(route.HOME, "Applications", "ChatGPT.app", "Contents", "Resources", "codex-cli", "bin", "codex")]

    def _auto(self):
        # Windows: updates change the bin hash, so the newest install; before PATH, which may hold an old one.
        root = os.environ.get("LOCALAPPDATA") or os.path.join(route.HOME, "AppData", "Local")
        found = glob.glob(os.path.join(root, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if found:
            return {"present": True, "path": max(found, key=os.path.getmtime), "source": "usual place", "note": None}
        return super()._auto()

    def accounts(self):
        """The active CODEX_HOME (else ~/.codex) and every other ~/.codex* folder that holds a sign-in (auth.json)."""
        active = os.environ.get("CODEX_HOME") or "~/.codex"
        if self.fk is not None:
            fake_accs = self.fk.get("codex_accounts")
            if isinstance(fake_accs, list):
                return [dict({"active": a.get("id") == route.account_of("codex")}, **a) for a in fake_accs]
            return [{"id": "codex:" + active, "where": active, "email": (self.fk.get("emails") or {}).get("codex:" + active),
                     "active": True}]
        out, seen = [], set()

        def add(where, is_active):
            folder = expand(where)
            if folder in seen:
                return
            seen.add(folder)
            out.append({"id": "codex:" + where, "where": where, "email": codex_email(folder), "active": is_active})

        add(active, True)
        for folder in sorted(glob.glob(os.path.join(_home(), ".codex*"))):
            if os.path.isdir(folder) and os.path.exists(os.path.join(folder, "auth.json")):
                add("~/" + os.path.basename(folder), False)
        return out

    def handoff(self, account, conversation, model=None):
        acc = account if isinstance(account, dict) else next((a for a in self.accounts() if a["id"] == account), None)
        if not acc or not acc.get("where"):
            return None
        return _terminal_line([self.path(), "resume", "-C", route.CODEX_ROOM, "-s", "read-only", conversation],
                              route.CODEX_ROOM, expand(acc["where"]))

    def _home_env(self, account):
        """None for the active account (its caches are the old ones); else the folder to run with as CODEX_HOME."""
        if not account:
            return None
        if not isinstance(account, dict):
            # By its id, so an account the policy chose runs there even if a listing missed it (never on the active by mistake).
            return route.account_home(account, fake=self.fake)
        if account.get("active"):
            return None
        return expand(account["where"])

    def models(self, account=None, force_refresh=False):
        ok, items = route.fetch_codex_models(executable=self.path(), force_refresh=force_refresh, fake=self.fake,
                                             home=self._home_env(account))
        if not ok:
            items = self.from_roles()
        out = []
        for it in items:
            efforts = []
            for e in it.get("supportedReasoningEfforts") or []:
                efforts.append(e.get("reasoningEffort") if isinstance(e, dict) else str(e))
            out.append(dict(it, family=it["id"], efforts=sort_efforts(efforts),
                            default_effort=it.get("defaultReasoningEffort")))
        return ok, out

    def efforts(self, model, account=None):
        ok, rows = self.models(account)
        row = next((r for r in rows if r["id"] == model), None) if ok else None
        return list(row["efforts"]) if row else []

    def run(self, account, model, effort, prompt, carry_on=False, timeout_s=300, legacy=False, verbose=False, **kw):
        import ask
        if not legacy:
            return ask.ask_broker("codex", model, prompt, carry_on, effort=effort,
                                  home=self._home_env(account), verbose=verbose, **kw)
        return ask.ask_codex(model, prompt, carry_on, timeout_s, effort=effort, home=self._home_env(account))


class OllamaAdapter(Adapter):
    """Local models through Ollama's API; one local account. paths.ollama is the API's URL (as before), or null."""
    name = "ollama"
    local = True
    title = "Ollama (local)"
    exe_names = ("ollama",)

    def candidates(self):
        local = os.environ.get("LOCALAPPDATA") or os.path.join(route.HOME, "AppData", "Local")
        return [os.path.join(local, "Programs", "Ollama", "ollama.exe"), "/Applications/Ollama.app/Contents/Resources/ollama",
                "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"]

    def url(self):
        value, _ = _configured(self.name, self.fake)
        return value if isinstance(value, str) and value != "auto" else route.OLLAMA_DEFAULT

    def detect(self):
        value, source = _configured(self.name, self.fake)
        if value is None or self.fk is not None:
            return super().detect()
        url = self.url()
        ok, _ = route.fetch_ollama_models(ollama_url=url, include_show=False)
        if ok:
            return {"present": True, "path": url, "source": "API", "note": None}
        binary = super()._auto()
        if binary["present"]:
            return dict(binary, path=url, binary=binary["path"], note=f"the server at {url} is not up; ask.py starts it")
        return {"present": False, "path": None, "source": "auto", "note": f"no API at {url} and no ollama binary"}

    def accounts(self):
        return [{"id": "ollama", "where": self.url(), "email": None, "active": True}]

    def models(self, account=None, force_refresh=False):
        ok, items = route.fetch_ollama_models(ollama_url=self.url(), fake=self.fake, include_show=True)
        if not ok:
            items = [dict(it, size=0) for it in self.from_roles()]
        return ok, [dict(it, family=it["id"], efforts=[]) for it in items]

    def usage(self, account=None, cs=None):
        return []

    def run(self, account, model, effort, prompt, think=False, timeout_s=300, keep=None, **kw):
        import ask
        return ask.ask_ollama(self.url(), model, prompt, think, timeout_s, name=model, keep=keep)


def _redact(value, key):
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    return value


_SERVER_MODELS = {}  # url -> (read at, ok, ids): a pick asks a server's list many times; a stopped one costs 2 s each
SERVER_MODELS_TTL_S = 30


class OpenAIAdapter(Adapter):
    """A configured local OpenAI-compatible endpoint; discovery never starts a server."""
    local = True

    def __init__(self, name, config, fake=None):
        super().__init__(fake)
        self.name = name
        self.title = name + " (local)"
        self.config = config

    def url(self):
        return str(self.config.get("url") or "").rstrip("/")

    def _key(self):
        return os.environ.get(self.config.get("api_key_env") or "", "")

    def _request(self, endpoint, data=None):
        headers = {"Content-Type": "application/json"}
        if self._key():
            headers["Authorization"] = "Bearer " + self._key()
        return urllib.request.Request(self.url() + endpoint, data=data, headers=headers)

    def _cache_key(self):
        # One gateway can serve two instances with different keys and lists: the key's variable is part of it.
        return self.url(), self.config.get("api_key_env") or ""

    def _model_ids(self, force_refresh=False):
        if self.fk is not None:
            state = (self.fk.get("openai_servers") or {}).get(self.name, {})
            return bool(state.get("up")), state.get("models") or []
        import time
        cached = _SERVER_MODELS.get(self._cache_key())
        if cached and not force_refresh and time.time() - cached[0] < SERVER_MODELS_TTL_S:
            return cached[1], list(cached[2])
        try:
            with urllib.request.urlopen(self._request("/models"), timeout=2) as response:
                data = json.loads(response.read().decode("utf-8"))
            found = True, [row.get("id", "") for row in data.get("data", [])] if isinstance(data, dict) else []
        except (OSError, ValueError, TypeError, AttributeError):
            found = False, []
        _SERVER_MODELS[self._cache_key()] = (time.time(),) + found
        return found

    def detect(self):
        ok, _ = self._model_ids()
        return {"present": ok, "path": self.url(), "source": "API", "note": None if ok else
                "no OpenAI-compatible API at " + self.url()}

    def accounts(self):
        return [{"id": self.name, "where": self.url(), "email": None, "active": True, "label": "local"}]

    def models(self, account=None, force_refresh=False):
        ok, ids = self._model_ids(force_refresh=force_refresh)
        configured = self.config.get("models") or []
        static = self.config.get("static") or len(ids) == 1
        normalized, wire = [], {}
        # A configured alias cannot switch a static server to another advertised model.
        for mid in ids[:1] if static else ids:
            mid = str(mid or "")
            if not mid or "/" in mid or "\\" in mid or mid.lower().endswith((".gguf", ".bin")):
                replacements = configured or [os.path.splitext(mid.replace("\\", "/").rsplit("/", 1)[-1])[0]]
            else:
                replacements = [mid]
            for value in replacements:
                if value and value not in normalized and (not configured or value in configured):
                    normalized.append(value)
                    # The name shown is not always the name the server takes: a file path stays the wire id.
                    wire.setdefault(value, mid or value)
        if static:
            normalized = normalized[:1]
        return ok, [{"id": mid, "label": mid, "family": mid, "efforts": [], "wire": wire.get(mid, mid)} for mid in normalized]

    def usage(self, account=None, cs=None):
        return []

    def run(self, account, model, effort, prompt, timeout_s=300, **kw):
        ok, rows = self.models()
        row = next((r for r in rows if r["id"] == model), None) if ok else None
        if row is None:
            return 2, f"{model}: not offered by {self.name}", None, None
        payload = json.dumps({"model": row.get("wire") or model, "messages": [{"role": "user", "content": prompt}],
                              "stream": False}).encode("utf-8")
        try:
            with urllib.request.urlopen(self._request("/chat/completions", payload),
                                        timeout=float(self.config.get("load_timeout_s", 120)) + timeout_s) as response:
                raw = response.read().decode("utf-8")
            # A server may echo credentials, even \u-escaped: redact the decoded values before logging or answering.
            key = self._key()
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if key:
                if data is not None:
                    data = _redact(data, key)
                    raw = json.dumps(data, ensure_ascii=False)
                raw = raw.replace(key, "[redacted]")
            log = route.save_raw_log(model, raw)
            if data is None:
                raise ValueError("not JSON")
            answer = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage")
            if isinstance(usage, dict):
                tokens = {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0),
                          "total": usage.get("total_tokens", 0)}
            else:
                tokens = {"in": len(prompt) // 4, "out": len(answer) // 4, "estimated": True}
                tokens["total"] = tokens["in"] + tokens["out"]
            return 0, answer, tokens, log
        except urllib.error.HTTPError as exc:
            return 2, f"{self.name}: HTTP {exc.code}", None, None
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return 2, f"{self.name}: request failed or invalid reply", None, None


class ClaudeSubscriptionAdapter(Adapter):
    """The Claude plan the session itself runs on: its models are called as Claude subagents by the session, never by a script."""
    name = "claude-subscription"
    title = "Claude subscription"
    exe_names = ("claude",)

    def detect(self):
        value, source = _configured(self.name, self.fake)
        if value is None:
            return super().detect()
        # (The subagents need no CLI: the session that runs /ask is Claude. The CLI's path is only shown.)
        found = shutil.which("claude") if self.fk is None else None
        return {"present": True, "path": found or "the session", "source": "PATH" if found else "session", "note": None}

    def accounts(self):
        return [{"id": "claude-subscription", "where": os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude", "email": None,
                 "active": True}]

    def models(self, account=None, force_refresh=False):
        rules = route.load_roles()
        ids = rules.get("subscription_models") or route.DEFAULT_SUBSCRIPTION_MODELS
        return True, [{"id": m, "family": m, "efforts": list(CLAUDE_EFFORTS), "label": "Claude subagent only"} for m in ids]

    def efforts(self, model):
        return list(CLAUDE_EFFORTS)

    def run(self, account, model, effort, prompt, **kw):
        raise NotImplementedError(f"{model}: Claude subagent only (the session calls it with the Agent tool)")


ADAPTERS = (AgyAdapter, CodexAdapter, OllamaAdapter, ClaudeSubscriptionAdapter)
BUILTIN_NAMES = tuple(a.name for a in ADAPTERS)


def local_servers(fake=None):
    entries = route.load_machine(fake=fake).get("local_servers") or {}
    return {name: config for name, config in entries.items()
            if name not in BUILTIN_NAMES and ":" not in name and isinstance(config, dict)}


class _Names:
    # Read lazily: tests and long-running callers can switch machine configurations.
    def __iter__(self):
        return iter(BUILTIN_NAMES + tuple(local_servers()))

    def __contains__(self, name):
        return name in tuple(self)


NAMES = _Names()


def get(name, fake=None):
    for cls in ADAPTERS:
        if cls.name == name:
            return cls(fake=fake)
    if name in local_servers(fake):
        return OpenAIAdapter(name, local_servers(fake)[name], fake=fake)
    raise KeyError(name)


def all_adapters(fake=None):
    return [cls(fake=fake) for cls in ADAPTERS] + [
        OpenAIAdapter(name, config, fake=fake) for name, config in local_servers(fake).items()]


def account_email(account_id, fake=None):
    """The email the inventory reads for an account (Codex: auth.json), for its label by work_domains; None if unknown."""
    provider = str(account_id).partition(":")[0]
    try:
        return get(provider, fake=fake).email(account_id)
    except Exception:
        return None


def inventory(fake=None, force_refresh=False, context=None, machine=None, rules=None):
    """provider -> account (label, policy) -> model -> efforts, plus the roles that name each model.
    [{'provider', 'title', 'detect', 'accounts': [{'id', 'label', 'allowed', 'verdict', 'models': [...]}, ...]}]"""
    rules = rules or route.load_roles()
    if machine is None:
        machine = route.load_machine(fake=fake)
    used = route.role_usage(rules)
    out = []
    for ad in all_adapters(fake=fake):
        det = ad.detect()
        entry = {"provider": ad.name, "title": ad.title, "detect": det, "accounts": []}
        out.append(entry)
        if not det["present"]:
            continue
        for acc in ad.accounts():
            info = route.account_info(acc["id"], machine, email=acc.get("email"))
            pol = route.policy_for(ad.name, context=context, machine=machine, fake=fake, account=acc["id"],
                                   email=acc.get("email"))
            ok, rows = ad.models(acc if not acc.get("active") else None, force_refresh=force_refresh)
            for r in rows:
                ids = r.get("ids") or {"": r["id"]}
                r["roles"] = []
                for eff, mid in sorted(ids.items(), key=lambda kv: effort_rank(kv[0])):
                    for role in used.get((ad.name, mid), []):
                        r["roles"].append(f"{role}@{eff}" if eff else role)
            entry["accounts"].append(dict(acc, label=info["label"], label_source=info["source"],
                                          allowed=pol["allowed"], crossed=pol["crossed"], reason=pol["reason"],
                                          models_ok=ok, models=rows))
    for name in (machine.get("local_servers") or {}):
        if name in BUILTIN_NAMES or ":" in name:
            out.append({"provider": name, "title": name, "accounts": [],
                        "detect": {"present": False, "path": None, "source": "machine.json",
                                   "note": "local_servers entry skipped: built-in name or ':' in name"}})
    return out


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(inventory(), indent=2, ensure_ascii=False, default=str))
