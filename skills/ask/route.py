"""Router for ask.py and writer.py: list-based quota pools (agy, Codex and Claude subscription), pre-run token/rate
fit checks, in-flight quota reservations under lock, weekly/5h pace, live model discovery across providers, GPU/VRAM/lock
checks for Ollama's local models (a machine's own, such as an engine, through hooks.py), raw call logs, and a JSONL usage journal.

Everything uses the Python 3 standard library only and accepts a fake state dictionary or ASK_ROUTE_FAKE=<json file>
so tests run offline without touching the GPU or cloud providers.
"""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import fnmatch
import glob
import json
import os
import queue
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

HOME = os.path.expanduser("~")
ACCOUNTS_HOME = HOME  # where "~/.codex*" accounts are looked for; the tests point it at their own room
# ASK_CACHE_DIR and ASK_CONFIG_DIR move the two folders (the tests point them at empty ones of their own, so a
# machine's own machine.json or journal never decides a test).
CACHE_DIR = os.environ.get("ASK_CACHE_DIR") or os.path.join(HOME, ".cache", "ask")
CONFIG_DIR = os.environ.get("ASK_CONFIG_DIR") or os.path.join(HOME, ".config", "ask")
USAGE_CACHE = os.path.join(CACHE_DIR, "agy_usage.json")
POOL_HISTORY = os.path.join(CACHE_DIR, "pool_history.json")
AGY_MODELS_CACHE = os.path.join(CACHE_DIR, "agy_models.json")
CODEX_USAGE_CACHE = os.path.join(CACHE_DIR, "codex_usage.json")
CODEX_MODELS_CACHE = os.path.join(CACHE_DIR, "codex_models.json")
AGY_CONVERSATIONS_PATH = os.path.join(CACHE_DIR, "agy_conversations.json")
CLAUDE_USAGE_PATH = os.path.join(CACHE_DIR, "claude_usage.json")
VRAM_CACHE = os.path.join(CACHE_DIR, "vram.json")
LOCAL_LOCK = os.path.join(CACHE_DIR, "local.lock")
RESERVATIONS_PATH = os.path.join(CACHE_DIR, "reservations.json")
RESERVATIONS_LOCK = os.path.join(CACHE_DIR, "reservations.lock")
JOURNAL_PATH = os.path.join(CACHE_DIR, "usage.jsonl")
LOG_DIR = os.path.join(CACHE_DIR, "logs")
ASK_ROOM = os.path.join(HOME, ".cache", "ask-agy")
CODEX_ROOM = os.path.join(HOME, ".cache", "ask-codex")

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES_PATH = os.path.join(HERE, "roles.json")
CONFIG_PATH = os.path.join(CONFIG_DIR, "models.json")
MACHINE_PATH = os.path.join(CONFIG_DIR, "machine.json")

AGY_DEFAULT = os.path.join(HOME, "AppData", "Local", "agy", "bin", "agy.exe")
OLLAMA_DEFAULT = "http://127.0.0.1:11434"


DEFAULT_SUBSCRIPTION_MODELS = [
    "claude-fable-5-2",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
]

CLAUDE_STALE_SECONDS = 6 * 3600


# --- ANSI terminal styling (only when stdout is a TTY or FORCE_COLOR is set, never with NO_COLOR) ---

def enable_vt_mode():
    if not sys.platform.startswith("win"):
        return True
    try:
        k32 = ctypes.windll.kernel32
        for std_id in (-11, -12):
            h = k32.GetStdHandle(std_id)
            mode = wintypes.DWORD(0)
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


def use_color(stream=None):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        enable_vt_mode()
        return True
    s = stream if stream is not None else sys.stdout
    try:
        if hasattr(s, "isatty") and s.isatty():
            enable_vt_mode()
            return True
    except Exception:
        pass
    return False


def style(text, code, enabled=None):
    if enabled is None:
        enabled = use_color()
    if not enabled:
        return str(text)
    return f"\033[{code}m{text}\033[0m"


def bold(text, enabled=None):
    return style(text, "1", enabled)


def dim(text, enabled=None):
    return style(text, "2", enabled)


def yellow(text, enabled=None):
    return style(text, "33", enabled)


def red(text, enabled=None):
    return style(text, "31", enabled)


def green(text, enabled=None):
    return style(text, "32", enabled)


# --- Config, roles, and model id resolution ---

USER_ROLES_PATH = os.path.join(CONFIG_DIR, "roles.json")


def merge_roles(base, over):
    """The user's roles.json over the shipped roles: a role there replaces the role of that name whole (null removes it), a
    dict at the top merges key by key, anything else replaces. So an update of switchboard brings its new defaults and
    keeps the user's own order."""
    out = dict(base)
    for key, value in (over or {}).items():
        if key.startswith("_"):
            continue
        if key == "roles" and isinstance(value, dict):
            roles = dict(base.get("roles") or {})
            for name, spec in value.items():
                if spec is None:
                    roles.pop(name, None)
                else:
                    roles[name] = spec
            out["roles"] = roles
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            out[key] = dict(base[key], **value)
        else:
            out[key] = value
    return out


def load_user_roles(path=None):
    """~/.config/ask/roles.json, or {} when absent; a file that cannot be read is an error, never silently skipped."""
    p = path or USER_ROLES_PATH
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: not a JSON object")
    return data


def load_roles(path=None):
    """roles.json beside this file with the user's ~/.config/ask/roles.json over it; or exactly the file ASK_ROLES names
    (the tests' frozen copy, or a whole roles file of one's own), with nothing over it."""
    p = path or os.environ.get("ASK_ROLES") or ROLES_PATH
    with open(p, encoding="utf-8") as f:
        rules = json.load(f)
    if path is None and not os.environ.get("ASK_ROLES"):
        rules = merge_roles(rules, load_user_roles())
    return rules


def resolve_preset(flag=None, rules=None, env=None, machine=None):
    """Resolve the active name and source; blank values are unset."""
    rules = load_roles() if rules is None else rules
    env = os.environ if env is None else env
    machine = load_machine() if machine is None else machine
    known = dict(manual={}, **{k: v for k, v in (rules.get("presets") or {}).items() if k != "manual"})
    for value, source in ((flag, "flag"), (env.get("ASK_PRESET"), "ASK_PRESET"),
                          (machine.get("preset"), "machine.json"), ("manual", "default")):
        name = str(value or "").strip()
        if name:
            if name not in known:
                raise ValueError(f"unknown preset {name!r}; known presets: {', '.join(known)}")
            return {"preset": name, "source": source}


def model_table(rules=None):
    """Keep provider metadata from roles for offline routing."""
    rules = rules or load_roles()
    tbl = {mid: {"provider": info["provider"]}
           for mid, info in (rules.get("models") or {}).items() if info.get("provider")}
    for prov, mid in role_usage(rules):
        tbl.setdefault(mid, {"provider": prov})
    return tbl


def _config_paths():
    """Paths of the CLIs: machine.json "paths" over the old models.json, which is still read."""
    paths = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c, dict):
            paths.update({k: v for k, v in c.items() if isinstance(v, str) and v})
    except (OSError, ValueError):
        pass
    try:
        with open(MACHINE_PATH, encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and isinstance(m.get("paths"), dict):
            paths.update({k: v for k, v in m["paths"].items() if isinstance(v, str) and v})
    except (OSError, ValueError):
        pass
    return paths


def _providers():
    import providers  # the adapters (providers.py) import this module; imported late so either can load first
    return providers


def load_config():
    """agy's path and Ollama's URL, as each adapter's detect() finds them (machine.json "paths", PATH, the usual places)."""
    pv = _providers()
    return pv.get("agy").path(AGY_DEFAULT), pv.get("ollama").url()


def codex_path():
    # (The configured path, then the newest Windows install, PATH and ChatGPT.app's bundled CLI: providers.CodexAdapter.)
    return _providers().get("codex").path("codex")


def is_local(provider, fake=None):
    """Whether the provider runs on this machine's GPU (its adapter's `local`): the GPU lock, the hooks, local_only roles
    and the "local" account label key off this kind, not off a provider's name."""
    try:
        return bool(getattr(_providers().get(provider, fake=fake), "local", False))
    except KeyError:
        return False


def local_providers(fake=None):
    return [ad.name for ad in _providers().all_adapters(fake=fake) if getattr(ad, "local", False)]


def provider_present(provider, fake=None):
    """detect() of the provider's adapter: False when machine.json says null or it is nowhere to be found."""
    try:
        return _providers().get(provider, fake=fake).detect()
    except KeyError:
        return {"present": True, "path": None, "source": "unknown", "note": None}


# --- Context and account policy ---
# One Mac holds work and personal tasks: work code must not reach a personal AI account by accident, and a personal task must
# not spend the company's quota. The context says which task this is; each account carries a label; the policy says which
# labels a context may use and in what order. Until the inventory of accounts (stage 2) each provider is one account.

CONTEXTS = ("work", "personal")
LABELS = ("work", "personal", "local", "session")
EXIT_POLICY = 7  # refused by the policy: a code of its own, so a caller can tell it from quota (2) or a busy GPU (3)
EXIT_UNSUPPORTED = 8  # the role cannot run on this machine (local_only with no local provider): waiting will not help


def load_machine(path=None, fake=None):
    """~/.config/ask/machine.json, or {} when absent; a file that cannot be read keeps its error under "_error"."""
    fk = resolve_fake(fake)
    if isinstance(fk, dict) and isinstance(fk.get("machine"), dict):
        return fk["machine"]
    p = path or MACHINE_PATH
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        return {"_error": f"{p}: {e}"}
    return data if isinstance(data, dict) else {"_error": f"{p}: not a JSON object"}


def context_by_path(cwd, machine):
    """machine.json "context_paths" ({"~/work": "work", "~": "personal"}): the longest folder holding cwd wins.
    For sessions direnv never reached, such as Claude Desktop's. None when no rule fits."""
    rules = (machine or {}).get("context_paths")
    if not isinstance(rules, dict) or not cwd:
        return None
    here = os.path.normcase(os.path.realpath(os.path.expanduser(str(cwd))))
    best = None
    for folder, ctx in rules.items():
        ctx = str(ctx).strip().lower()
        if ctx not in CONTEXTS:
            continue
        root = os.path.normcase(os.path.realpath(os.path.expanduser(str(folder))))
        if here == root or here.startswith(root.rstrip(os.sep) + os.sep):
            if best is None or len(root) > len(best[0]):
                best = (root, ctx, folder)
    return None if best is None else {"context": best[1], "folder": best[2]}


CONTEXT_MARKER = ".switchboard-context"


def context_marker(cwd):
    """The nearest .switchboard-context from cwd up to the root: {"context" (as written, lowered), "path"} or None.
    A folder says what it is wherever it lies and whatever it is called; it moves with the folder."""
    here = os.path.realpath(os.path.expanduser(str(cwd)))
    while True:
        path = os.path.join(here, CONTEXT_MARKER)
        # lexists: a marker that is there but cannot be read (a broken link, no access) is a wrong word, so work,
        # never a reason to go on up to a personal one above it.
        if os.path.lexists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    word = (f.read().split() or [""])[0].strip().lower()
            except (OSError, UnicodeDecodeError):
                word = ""
            return {"context": word, "path": path}
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


def resolve_context(flag=None, env=None, cwd=None, machine=None):
    """--context first, then ASK_CONTEXT, then the nearest .switchboard-context up from the folder, then the folder by
    machine.json "context_paths"; anything else (unset, empty, a typo, a folder no rule holds) is work, the strict side.
    With env given (tests), machine is read only when it is given too, and a marker only under a cwd given too, so a
    real machine.json or a marker above the tests never leaks in."""
    real = env is None
    env = os.environ if env is None else env
    if flag is not None:  # an empty --context is a context given wrong, so work, not the env's
        raw, source = str(flag), "--context"
    elif env.get("ASK_CONTEXT"):
        raw, source = str(env["ASK_CONTEXT"]), "ASK_CONTEXT"
    else:
        raw, source = "", None
    ctx = raw.strip().lower()
    if ctx in CONTEXTS:
        return {"context": ctx, "source": source, "strict_default": False, "raw": raw}
    if source is None and (real or cwd is not None):
        marker = context_marker(cwd if cwd is not None else os.getcwd())
        if marker:
            if marker["context"] in CONTEXTS:
                return {"context": marker["context"], "source": "marker", "strict_default": False, "raw": "",
                        "note": f"ASK_CONTEXT is not set: {marker['context']} by {marker['path']}"}
            return {"context": "work", "source": "marker", "strict_default": True, "raw": marker["context"],
                    "note": f"{marker['path']} says {marker['context']!r}, not work or personal: treated as work"}
    if source is None:
        if machine is None and real:
            machine = load_machine()
        by_path = context_by_path(cwd if cwd is not None else os.getcwd(), machine)
        if by_path:
            return {"context": by_path["context"], "source": "context_paths", "strict_default": False, "raw": "",
                    "note": f"ASK_CONTEXT is not set: {by_path['context']} by folder {by_path['folder']} "
                            f"(context_paths in ~/.config/ask/machine.json)"}
    why = f"{source}={raw!r} is not work or personal" if source else "ASK_CONTEXT is not set"
    return {"context": "work", "source": source or "default", "strict_default": True, "raw": raw,
            "note": f"{why}: treated as work"}


def _context_dict(context):
    """The context as resolve_context gives it, so the policy knows whether work was set or only fallen back to."""
    if isinstance(context, dict):
        if context.get("context") in CONTEXTS:
            return context
        return {"context": "work", "strict_default": True, "note": "an unknown context: treated as work"}
    if context in CONTEXTS:
        return {"context": context, "strict_default": False}
    return resolve_context(context)


def _context_name(context):
    return _context_dict(context)["context"]


def account_of(provider):
    """Stage 1: one account per provider. Codex's is its CODEX_HOME, so a later work profile can be told apart."""
    if provider == "codex":
        return "codex:" + (os.environ.get("CODEX_HOME") or "~/.codex")
    return provider or "unknown"


def _same_account(key, account):
    def norm(a):
        prov, _, where = str(a).partition(":")
        return prov.lower(), os.path.normpath(os.path.expanduser(where)) if where else ""
    kp, kw = norm(key)
    ap, aw = norm(account)
    return kp == ap and (not kw or kw == aw)


def account_info(account, machine=None, email=None):
    """The account's label: explicit in machine.json "accounts", else by its email's domain ("work_domains"; the email from
    machine.json, else the one the inventory read, as Codex's auth.json), else local for Ollama, the session's own account
    for the Claude subscription, else personal."""
    machine = machine or {}
    accounts = machine.get("accounts") or {}
    provider = str(account).partition(":")[0]
    entry = accounts.get(account)
    if entry is None:
        # An exact key wins over a bare provider key ("codex"), whatever their order in the file.
        keys = sorted((k for k in accounts if _same_account(k, account)), key=lambda k: ":" not in k)
        entry = accounts[keys[0]] if keys else None
    if not isinstance(entry, dict):
        entry = {"label": entry} if isinstance(entry, str) else {}
    label, source = str(entry.get("label") or "").lower(), "machine.json"
    email = str(entry.get("email") or email or "")
    if label not in LABELS:
        domain = email.rpartition("@")[2].lower() if "@" in email else ""
        work_domains = [str(d).lower().lstrip("@") for d in machine.get("work_domains") or []]
        if domain and any(domain == d or domain.endswith("." + d) for d in work_domains):
            label, source = "work", f"email domain {domain}"
        elif is_local(provider, fake={"machine": machine}):
            label, source = "local", "local provider"
        elif provider == "claude-subscription":
            label, source = "session", "the session's own account"
        else:
            label, source = "personal", "default"
    work_ok = label in ("work", "local", "session") or (label == "personal" and entry.get("work_ok") is True)
    return {"account": account, "label": label, "source": source, "email": email or None, "work_ok": work_ok}


def policy_for(provider, context=None, machine=None, fake=None, account=None, email=None):
    """Whether this context may use the provider's account, its group rank (lower first) and whether it crosses work → personal.

    work: work accounts → local → personal ones marked work_ok: true (false by default); personal without it is refused.
    personal: work accounts are refused (the company's quota is not for personal tasks); the role's own order stays.
    """
    cinfo = _context_dict(context)
    ctx = cinfo["context"]
    if machine is None:
        machine = load_machine(fake=fake)
    account = account or account_of(provider)
    if email is None:
        email = _providers().account_email(account, fake=fake)
    acc = account_info(account, machine, email=email)
    label = acc["label"]
    allowed, rank, crossed, reason = True, 0, False, None
    if ctx == "work":
        if label == "work":
            rank = 0
        elif label == "local":
            rank = 1
        elif label == "session":
            rank = 3
        elif acc["work_ok"]:
            rank, crossed = 2, True
        else:
            allowed = False
            if cinfo.get("strict_default"):
                # No context was set, so work is only the strict fallback: the fix is to set the context, never to open
                # a personal account to work tasks with work_ok (an agent would take the hint).
                reason = (f"{acc['account']} is a personal account and no context is set "
                          f"({cinfo.get('note') or 'treated as work'}). For a personal task or machine set the context: "
                          f"ask.py --init, ask.py --context-here personal (a {CONTEXT_MARKER} in the folder), "
                          f"ASK_CONTEXT=personal, --context personal, or context_paths in ~/.config/ask/machine.json "
                          f"(e.g. {{\"~\": \"personal\"}}). Do not set work_ok for this")
            else:
                reason = (f"{acc['account']} is a personal account and the context is work "
                          f"(work_ok: true for it in ~/.config/ask/machine.json allows it, as a conscious choice)")
    elif label == "work":
        allowed = False
        reason = f"{acc['account']} is a work account and the context is personal: the company's quota is not for personal tasks"
    return dict(acc, context=ctx, provider=provider, allowed=allowed, rank=rank, crossed=crossed, reason=reason)


def provider_accounts(provider, fake=None):
    """The provider's accounts as its adapter lists them (Codex: every ~/.codex* with a sign-in), at least the active one."""
    try:
        accs = _providers().get(provider, fake=fake).accounts()
    except Exception:
        accs = []
    return accs or [{"id": account_of(provider), "email": None, "active": True}]


def account_policies(provider, context=None, machine=None, fake=None):
    """Every account of the provider with its policy, in the order the policy ranks them:
    allowed before refused, then the group (work → local → personal with work_ok), then the active account, then the
    adapter's order. A call goes to the first allowed one, not blindly to CODEX_HOME."""
    if machine is None:
        machine = load_machine(fake=fake)
    rows = []
    for i, acc in enumerate(provider_accounts(provider, fake=fake)):
        pol = policy_for(provider, context=context, machine=machine, fake=fake, account=acc["id"],
                         email=acc.get("email") or "")
        rows.append(((not pol["allowed"], pol["rank"], not acc.get("active"), i), dict(pol, active=bool(acc.get("active")))))
    return [pol for _, pol in sorted(rows, key=lambda t: t[0])]


def choose_account(provider, context=None, machine=None, fake=None):
    """The policy of the account a call to the provider goes to: the first allowed one, else the first refusal."""
    return account_policies(provider, context=context, machine=machine, fake=fake)[0]


def account_home(account, fake=None):
    """The CODEX_HOME to run a Codex account with; None for the active account (its caches and env are the old ones) and for
    every other provider."""
    prov, _, where = str(account or "").partition(":")
    if prov != "codex" or not where or _same_account(account_of("codex"), account):
        return None
    return _providers().expand(where)


def crossing_line(pol):
    return (f"crossing: work context → personal account {pol['account']} (work_ok: true); "
            f"the question and its files go to {pol['provider']}")


def policy_summary(context=None, machine=None, providers=("agy", "codex", "ollama", "claude-subscription"), fake=None):
    """Rows for --status: each provider's account, its label and what this context does with it."""
    if machine is None:
        machine = load_machine(fake=fake)
    rows = []
    for prov in providers:
        pol = policy_for(prov, context=context, machine=machine)
        if not pol["allowed"]:
            verdict = "refused"
        elif pol["crossed"]:
            verdict = "allowed, crossing (journalled)"
        else:
            verdict = "allowed"
        rows.append(dict(pol, verdict=verdict))
    order = sorted(rows, key=lambda r: (not r["allowed"], r["rank"]))
    return order


def resolve_model(spec, provider=None, rules=None):
    """Resolve role metadata without guessing providers from spelling."""
    rules = rules or load_roles()
    if isinstance(spec, dict):
        provider = spec.get("provider") or provider
        spec = spec.get("model") or spec.get("id") or ""
    raw = str(spec or "").strip()
    if not raw:
        return None, None
    if provider:
        return provider, raw
    matches = {(prov, mid) for prov, mid in role_usage(rules) if mid == raw}
    matches.update((info["provider"], mid) for mid, info in model_table(rules).items() if mid == raw)
    if raw in rules.get("subscription_models", DEFAULT_SUBSCRIPTION_MODELS):
        matches.add(("claude-subscription", raw))
    return next(iter(matches)) if len(matches) == 1 else (None, None)


def fetch_openai_models(name, fake=None):
    ok, rows = _providers().get(name, fake=fake).models()
    return ok, [{"id": row["id"], "label": row["label"]} for row in rows]


def offered_providers(model_id, fake=None, rules=None, agy_path=None, ollama_url=None):
    """Consult every live list so a shared id cannot silently pick a provider."""
    rules = rules or load_roles()
    matches = []
    for prov, (ok, items) in (
        ("agy", fetch_agy_models(agy_path=agy_path, fake=fake)),
        ("ollama", fetch_ollama_models(ollama_url=ollama_url, fake=fake, include_show=False)),
        ("codex", fetch_codex_models(fake=fake)),
    ):
        if ok and any(model_id == item["id"] or model_id == item.get("raw_name") for item in items):
            matches.append(prov)
    for name in _providers().local_servers(fake):
        ok, items = fetch_openai_models(name, fake=fake)
        if ok and any(model_id == item["id"] for item in items):
            matches.append(name)
    if model_id in rules.get("subscription_models", DEFAULT_SUBSCRIPTION_MODELS):
        matches.append("claude-subscription")
    return matches


def resolve_fake(fake=None):
    if isinstance(fake, dict):
        return fake
    src = fake or os.environ.get("ASK_ROUTE_FAKE")
    if not src:
        return None
    if isinstance(src, str):
        s = src.strip()
        if s.startswith("{"):
            return json.loads(s)
        with open(s, encoding="utf-8") as f:
            return json.load(f)
    return None


def _parse_iso_utc(s):
    if not s:
        return None
    try:
        s = str(s).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def format_duration(seconds):
    if seconds is None:
        return "?"
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


# --- Live model discovery across providers ---

def _codex_rpc(methods, executable=None, timeout_s=15, home=None):
    """Read app-server metadata without a model turn; home: another account's CODEX_HOME."""
    env = dict(os.environ, CODEX_HOME=home) if home else None
    proc = subprocess.Popen(
        [executable or codex_path(), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env,
    )
    lines = queue.Queue()

    def read_lines():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_s

    def send(obj):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
        proc.stdin.flush()

    def receive(pending):
        replies = {}
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server metadata timed out")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("Codex app-server metadata timed out") from None
            if line is None:
                raise RuntimeError("Codex app-server closed before replying")
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("id") in pending:
                if "error" in obj:
                    raise RuntimeError(str(obj["error"]))
                rid = obj["id"]
                replies[rid] = obj.get("result") or {}
                pending.remove(rid)
        return replies

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "switchboard", "version": "0.1"}}})
        receive({1})
        send({"method": "initialized", "params": {}})
        for rid, method in enumerate(methods, 2):
            send({"id": rid, "method": method, "params": {}})
        replies = receive(set(range(2, len(methods) + 2)))
        return {method: replies[rid] for rid, method in enumerate(methods, 2)}
    finally:
        # Only this helper's child is stopped; never touch another CLI.
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=1)
        proc.stdin.close()
        proc.stdout.close()


def _codex_cache(path, home=None):
    """The active account keeps the old cache files; another CODEX_HOME gets its own, named after its folder."""
    if not home:
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}@{re.sub(r'[^A-Za-z0-9._-]+', '-', os.path.basename(os.path.normpath(home)))}{ext}"


def _codex_metadata(method, cache_path, ttl, force_refresh=False, executable=None, home=None):
    cache_path = _codex_cache(cache_path, home)
    now_ts = time.time()
    if not force_refresh and ttl > 0:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if now_ts - float(cached.get("fetched_at", 0)) <= ttl:
                return cached["result"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # Fetch both free metadata replies in one handshake.
    results = _codex_rpc(["account/rateLimits/read", "model/list"], executable=executable, home=home)
    paths = {"account/rateLimits/read": _codex_cache(CODEX_USAGE_CACHE, home), "model/list": _codex_cache(CODEX_MODELS_CACHE, home)}
    for name, result in results.items():
        path = cache_path if name == method else paths[name]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": now_ts, "result": result}, f, indent=2)
    return results[method]


def _parse_codex_models(data):
    result = data.get("result", data) if isinstance(data, dict) else {"data": data}
    return [{**m, "label": m.get("displayName") or m["id"]} if isinstance(m, dict)
            else {"id": str(m), "label": str(m)}
            for m in result.get("data", []) if not isinstance(m, dict) or not m.get("hidden")]


def fetch_codex_models(executable=None, force_refresh=False, ttl=86400, fake=None, home=None, account=None):
    """Codex's model list; account: another account's (its CODEX_HOME), which a fake gives as codex_models_by_account."""
    fk = resolve_fake(fake)
    if account is not None and home is None:
        home = account_home(account, fake=fk)
    if fk is not None:
        by_account = fk.get("codex_models_by_account") or {}
        data = by_account[account] if home and account in by_account else fk.get("codex_models", (fk.get("offered") or {}).get("codex"))
        return (False, []) if data is None or data is False else (True, _parse_codex_models(data))
    try:
        data = _codex_metadata("model/list", CODEX_MODELS_CACHE, ttl, force_refresh, executable, home=home)
        return True, _parse_codex_models(data)
    except Exception:
        return False, []


AGY_RETRY_S = 900  # after agy failed (most often: not signed in), it is not run again for this long


def _agy_down_path():
    return os.path.join(CACHE_DIR, "agy_unavailable.json")


def agy_unavailable(now=None):
    """Why agy failed within AGY_RETRY_S, or None. An agy that is not signed in opens a sign-in window on every run, so a
    failure is remembered instead of tried again on each call; --refresh tries at once."""
    try:
        with open(_agy_down_path(), encoding="utf-8") as f:
            data = json.load(f)
        if (now or time.time()) - float(data.get("at", 0)) < AGY_RETRY_S:
            return str(data.get("why") or "agy failed")
    except (OSError, ValueError, TypeError):
        pass
    return None


def _mark_agy(why):
    try:
        if why is None:
            if os.path.exists(_agy_down_path()):
                os.remove(_agy_down_path())
        else:
            _atomic_write_json(_agy_down_path(), {"at": time.time(), "why": why})
    except OSError:
        pass


def fetch_agy_models(agy_path=None, force_refresh=False, ttl=86400, fake=None):
    """Read agy's offered models from 'agy models' (0 tokens, cached 1 day in ~/.cache/ask/agy_models.json).
    Returns (ok: bool, [{'id': str, 'label': str}]).
    """
    fk = resolve_fake(fake)
    if fk is not None:
        if "agy_models" in fk:
            am = fk["agy_models"]
            if am is None or am is False:
                return False, []
            out = []
            for item in am:
                if isinstance(item, dict):
                    out.append({"id": item["id"], "label": item.get("label", item["id"])})
                else:
                    out.append({"id": str(item), "label": str(item)})
            return True, out
        if isinstance(fk.get("offered"), dict) and "agy" in fk["offered"]:
            am = fk["offered"]["agy"]
            if am is None or am is False:
                return False, []
            return True, [{"id": str(x), "label": str(x)} for x in am]
        # When fake state does not specify offered models, do not reject standard models
        return False, []

    now_ts = time.time()
    if not force_refresh and ttl > 0 and os.path.exists(AGY_MODELS_CACHE):
        try:
            with open(AGY_MODELS_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            if now_ts - float(cached.get("fetched_at", 0)) <= ttl and "models" in cached:
                return True, list(cached["models"])
        except Exception:
            pass

    if not force_refresh and agy_unavailable(now_ts):
        return False, []
    if not agy_path:
        agy_path, _ = load_config()
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        done = subprocess.run(
            [agy_path, "models"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        if done.returncode != 0:
            _mark_agy(f"agy models exited {done.returncode} (signed in?)")
            return False, []
        parsed = []
        for line in done.stdout.splitlines():
            line = line.strip()
            if not line or "\t" not in line or line.lower().startswith("fetching"):
                continue
            mid, lbl = line.split("\t", 1)
            mid, lbl = mid.strip(), lbl.strip()
            if mid:
                parsed.append({"id": mid, "label": lbl})
        if not parsed:
            _mark_agy("agy models listed nothing (signed in?)")
            return False, []
        with open(AGY_MODELS_CACHE, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": now_ts, "models": parsed}, f, indent=2)
        _mark_agy(None)
        return True, parsed
    except Exception as e:
        _mark_agy(f"agy models failed: {str(e)[:80]}")
        return False, []


def fetch_ollama_models(ollama_url=None, fake=None, include_show=True):
    """Read Ollama's models from /api/tags (and /api/show for shared weights blobs).
    Returns (ok: bool, [{'id': str, 'size': int, 'parameter_size': str, 'quantization': str,
                         'digest': str, 'weights_blob': str, 'shared_with': [str]}]).
    """
    fk = resolve_fake(fake)
    if fk is not None:
        loc = fk.get("local", fk)
        if "ollama_models" in fk:
            om = fk["ollama_models"]
            if om is None or om is False:
                return False, []
            return True, [{"id": str(x["id"] if isinstance(x, dict) else x)} for x in om]
        if isinstance(fk.get("offered"), dict) and "ollama" in fk["offered"]:
            om = fk["offered"]["ollama"]
            if om is None or om is False:
                return False, []
            return True, [{"id": str(x)} for x in om]
        if "ollama_tags" in loc and isinstance(loc["ollama_tags"], dict):
            return True, [{"id": k, "size": v} for k, v in loc["ollama_tags"].items()]
        return False, []

    url = ollama_url or OLLAMA_DEFAULT
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False, []

    raw_models = data.get("models") or []
    entries = []
    blob_groups = {}
    for m in raw_models:
        name = m.get("name") or m.get("model") or ""
        if not name:
            continue
        size_b = int(m.get("size") or 0)
        digest = str(m.get("digest") or "")
        details = m.get("details") or {}
        param_sz = details.get("parameter_size") or ""
        quant = details.get("quantization_level") or ""
        parent = details.get("parent_model") or ""
        weights_blob = ""
        if include_show:
            try:
                req = urllib.request.Request(
                    url + "/api/show",
                    data=json.dumps({"name": name}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3) as sr:
                    sdata = json.loads(sr.read().decode("utf-8"))
                mfile = sdata.get("modelfile") or ""
                for ln in mfile.splitlines():
                    if ln.upper().startswith("FROM "):
                        target = ln[5:].strip()
                        m_sha = re.search(r"sha256[-:]([0-9a-fA-F]{12,64})", target)
                        if m_sha:
                            weights_blob = m_sha.group(1)[:12].lower()
                        break
            except Exception:
                pass
        if not weights_blob:
            m_sha = re.search(r"sha256[-:]([0-9a-fA-F]{12,64})", parent)
            if m_sha:
                weights_blob = m_sha.group(1)[:12].lower()
            elif digest:
                weights_blob = digest[:12].lower()

        # Tags are part of the model id, including :latest.
        short_name = name
        entry = {
            "id": short_name,
            "raw_name": name,
            "size": size_b,
            "size_gb": round(size_b / 1_000_000_000.0, 1) if size_b else 0.0,
            "parameter_size": param_sz,
            "quantization": quant,
            "digest": digest[:12],
            "weights_blob": weights_blob,
            "shared_with": [],
        }
        entries.append(entry)
        if weights_blob:
            blob_groups.setdefault(weights_blob, []).append(short_name)

    for e in entries:
        wb = e["weights_blob"]
        if wb and len(blob_groups.get(wb, [])) > 1:
            e["shared_with"] = [other for other in blob_groups[wb] if other != e["id"]]

    return True, entries


def is_model_offered(provider, model_id, fake=None, rules=None, agy_path=None, ollama_url=None, account=None):
    """Return True if the provider offers model_id right now (or if the provider's list could not be read); account: the
    Codex account whose list is read (None: the active one)."""
    rules = rules or load_roles()
    if provider in _providers().local_servers(fake):
        ok, items = fetch_openai_models(provider, fake=fake)
        return ok and model_id in {item["id"] for item in items}
    m_low = (model_id or "").lower()
    if provider == "agy":
        ok, items = fetch_agy_models(agy_path=agy_path, fake=fake)
        if not ok:
            return True
        offered_ids = {x["id"].lower() for x in items}
        return m_low in offered_ids
    if provider == "codex":
        ok, items = fetch_codex_models(fake=fake, account=account)
        return not ok or m_low in {x["id"].lower() for x in items}
    if provider == "ollama":
        ok, items = fetch_ollama_models(ollama_url=ollama_url, fake=fake, include_show=False)
        if not ok:
            return True
        offered_ids = {x["id"].lower() for x in items}
        return m_low in offered_ids
    if provider == "claude-subscription":
        sub_list = {x.lower() for x in (rules.get("subscription_models") or DEFAULT_SUBSCRIPTION_MODELS)}
        return m_low in sub_list
    return True


def role_usage(rules=None):
    """{(provider, model id): [roles that name it]}: so that --list shows which offered models no role uses yet (a new one from a
    provider is seen at once; whether it joins a role is the user's call), and which a role names but no provider offers now."""
    rules = rules or load_roles()
    used = {}
    for role, spec in (rules.get("roles") or {}).items():
        for m in role_candidates(role_rows(spec) if isinstance(spec, dict) else [])[0]:
            if isinstance(m, dict) and m.get("model"):
                used.setdefault((m.get("provider") or "", m["model"]), []).append(role)
    return used


def models(fake=None, rules=None, agy_path=None, ollama_url=None):
    """Return one combined table across agy, Codex, Ollama, and Claude subscription."""
    rules = rules or load_roles()
    tbl = model_table(rules)
    cs = cloud_state(fake=fake, agy_path=agy_path, rules=rules)
    pools = cs.get("pools", [])
    rows = []

    def pool_for(prov, mid):
        pairs = covering_pools_and_windows(prov, mid, pools, include_stale=True)
        pnames = []
        for p, _ in pairs:
            lbl = p.get("label") or p.get("id")
            if lbl and lbl not in pnames:
                pnames.append(lbl)
        if pnames:
            return ", ".join(pnames)
        # (pool_covers names other providers' pools too: the subscription's claude-* is not agy's "Claude and GPT models".)
        for gname, pats in (rules.get("pool_covers") or {}).items():
            if pool_provider(gname) == prov and any(fnmatch.fnmatchcase(mid.lower(), pat.lower()) for pat in pats):
                return gname
        return "Claude subscription" if prov == "claude-subscription" else prov

    here = {pv: provider_present(pv, fake=fake)["present"] for pv in ("agy", "codex", "ollama", "claude-subscription")}

    # 1. agy models
    ok_agy, agy_list = fetch_agy_models(agy_path=agy_path, fake=fake) if here["agy"] else (True, [])
    if not ok_agy:
        agy_list = [{"id": mid, "label": mid} for mid, inf in tbl.items() if inf["provider"] == "agy"]
    for item in agy_list:
        mid = item["id"]
        rows.append({
            "provider": "agy",
            "model": mid,
            "pool": pool_for("agy", mid),
            "details": item.get("label", ""),
        })

    # Discover provider IDs for the combined list.
    ok_codex, codex_list = fetch_codex_models(fake=fake) if here["codex"] else (True, [])
    if not ok_codex:
        codex_list = [{"id": mid} for (prov, mid) in role_usage(rules) if prov == "codex"]
    for item in codex_list:
        mid = item["id"]
        rows.append({
            "provider": "codex", "model": mid,
            "pool": pool_for("codex", mid),
            "details": item.get("description") or item.get("label", ""),
            "default_reasoning_effort": item.get("defaultReasoningEffort"),
            "supported_reasoning_efforts": item.get("supportedReasoningEfforts", []),
        })

    # 2. Ollama models
    ok_ol, ol_list = fetch_ollama_models(ollama_url=ollama_url, fake=fake, include_show=True) if here["ollama"] else (True, [])
    if not ok_ol:
        ol_list = [
            {"id": mid, "size": 0, "size_gb": 0.0,
             "parameter_size": "", "quantization": "", "weights_blob": "", "shared_with": []}
            for mid, inf in tbl.items() if inf["provider"] == "ollama"
        ]
    for item in ol_list:
        mid = item["id"]
        sz_gb = item.get("size_gb") or (round(item.get("size", 0) / 1e9, 1) if item.get("size") else 0.0)
        parts = [f"{sz_gb:.1f} GB"] if sz_gb else []
        if item.get("parameter_size"):
            parts.append(item["parameter_size"])
        if item.get("quantization"):
            parts.append(item["quantization"])
        if item.get("weights_blob"):
            if item.get("shared_with"):
                parts.append(f"weights {item['weights_blob']} (shared with {', '.join(item['shared_with'])})")
            else:
                parts.append(f"weights {item['weights_blob']}")
        rows.append({
            "provider": "ollama",
            "model": mid,
            "pool": "ollama, local",
            "details": ", ".join(parts),
            "size_gb": sz_gb,
            "weights_blob": item.get("weights_blob", ""),
            "shared_with": item.get("shared_with", []),
        })

    for name in _providers().local_servers(fake):
        ok, items = fetch_openai_models(name, fake=fake)
        if ok:
            rows.extend({"provider": name, "model": it["id"], "pool": name + ", local",
                         "details": it["label"]} for it in items)

    # 3. Anthropic subscription models (from roles.json, static)
    for smid in (rules.get("subscription_models") or DEFAULT_SUBSCRIPTION_MODELS) if here["claude-subscription"] else []:
        rows.append({
            "provider": "claude-subscription",
            "model": smid,
            "pool": pool_for("claude-subscription", smid),
            "details": "Claude subagent only",
        })

    return rows


# --- Pools, windows, pace, token rates, and reservations ---

def _window_length_s(window_name, label="", explicit_length=None):
    if explicit_length:
        return float(explicit_length)
    s = f"{window_name or ''} {label or ''}".lower()
    if "week" in s or "7d" in s:
        return 7 * 86400.0
    m_h = re.search(r"(\d+)\s*h", s)
    if m_h:
        return float(m_h.group(1)) * 3600.0
    m_d = re.search(r"(\d+)\s*d", s)
    if m_d:
        return float(m_d.group(1)) * 86400.0
    return 7 * 86400.0


def format_pace(pace, window_type="weekly"):
    """Format pace as 'x2.2 (faster than the week)' / 'x1.0 (on pace)' / 'x0.8 (slower)'."""
    if pace is None:
        return "?"
    p = float(pace)
    is_weekly = "week" in str(window_type or "").lower()
    if 0.9 <= p <= 1.1:
        suffix = "on pace"
    elif p > 1.1:
        suffix = "faster than the week" if is_weekly else "faster"
    else:
        suffix = "slower"
    return f"x{p:.1f} ({suffix})"


def _build_window(win_raw, now_ts, default_covers=None):
    wtype = win_raw.get("window") or ""
    wlabel = win_raw.get("label") or wtype or "window"
    if not wtype:
        low_lbl = wlabel.lower()
        if "week" in low_lbl:
            wtype = "weekly"
        elif "5h" in low_lbl or "5-hour" in low_lbl:
            wtype = "5h"
        else:
            wtype = wlabel

    length_s = _window_length_s(wtype, wlabel, win_raw.get("length_s"))
    if "remaining_fraction" in win_raw and win_raw["remaining_fraction"] is not None:
        rem = float(win_raw["remaining_fraction"])
    elif "percentUsed" in win_raw and win_raw["percentUsed"] is not None:
        rem = max(0.0, min(1.0, (100.0 - float(win_raw["percentUsed"])) / 100.0))
    else:
        rem = 1.0

    reset_time = win_raw.get("reset_time") or win_raw.get("resetsAt") or ""
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    rst_dt = _parse_iso_utc(reset_time)
    if "resets_in_s" in win_raw and win_raw["resets_in_s"] is not None:
        resets_in_s = max(0.0, float(win_raw["resets_in_s"]))
    elif rst_dt is not None:
        resets_in_s = max(0.0, (rst_dt - now_dt).total_seconds())
    else:
        resets_in_s = None

    used_share = max(0.0, 1.0 - rem)
    if "pace" in win_raw and win_raw["pace"] is not None:
        pace = float(win_raw["pace"])
    else:
        if "time_gone_fraction" in win_raw and win_raw["time_gone_fraction"] is not None:
            gone_share = float(win_raw["time_gone_fraction"])
        elif "week_left_fraction" in win_raw and win_raw["week_left_fraction"] is not None:
            gone_share = max(0.0, 1.0 - float(win_raw["week_left_fraction"]))
        elif resets_in_s is not None and length_s > 0:
            clamped_rem_s = min(length_s, max(0.0, resets_in_s))
            gone_share = (length_s - clamped_rem_s) / length_s
        else:
            gone_share = 0.5
        if gone_share <= 1e-6:
            pace = 0.0 if used_share <= 1e-6 else 99.9
        else:
            pace = used_share / gone_share

    covers = win_raw.get("covers")
    if covers is None:
        covers = _covers_from_window_label(wlabel, default_covers)

    return {
        "window": wtype,
        "label": wlabel,
        "remaining_fraction": rem,
        "reserved_fraction": 0.0,
        "available_fraction": rem,
        "reset_time": reset_time,
        "length_s": length_s,
        "resets_in_s": resets_in_s,
        "resets_in": format_duration(resets_in_s),
        "pace": pace,
        "pace_str": format_pace(pace, wtype),
        "covers": list(covers or []),
    }


def _covers_from_window_label(wlabel, default_covers=None):
    s = str(wlabel or "")
    if "·" in s:
        scope = s.split("·", 1)[1].strip().lower()
        if scope in ("all models", "all", "*"):
            return ["claude-*"]
        fam = re.sub(r"[^a-z0-9\-]", "", scope)
        if fam:
            return [f"claude-{fam}*"]
    return list(default_covers or [])


def _covers_for_agy_group(gname, rules):
    pc = (rules or {}).get("pool_covers") or {
        "Gemini Models": ["gemini-*"],
        "Claude and GPT models": ["claude-*", "gpt-oss-*"],
    }
    if gname in pc:
        return list(pc[gname])
    for k, v in pc.items():
        if k.lower() == str(gname or "").lower():
            return list(v)
    return []


def _pools_from_agy_groups(groups, now_ts, rules=None):
    pools = []
    for idx, g in enumerate(groups or []):
        gname = g.get("name") or g.get("label") or f"agy-group-{idx}"
        gid = g.get("id") or re.sub(r"[^a-z0-9]+", "-", gname.lower()).strip("-") or f"agy-{idx}"
        covers = g.get("covers")
        if covers is None:
            covers = _covers_for_agy_group(gname, rules)
        raw_wins = g.get("buckets") if "buckets" in g else g.get("windows", [])
        windows = [_build_window(b, now_ts, default_covers=covers) for b in (raw_wins or [])]
        pools.append({
            "provider": g.get("provider", "agy"),
            "id": gid,
            "label": gname,
            "covers": list(covers),
            "stale": bool(g.get("stale", False)),
            "windows": windows,
            "reservations": [],
        })
    return pools


def _pools_from_codex_usage(data, now_ts, rules=None):
    """Turn primary/secondary into ordinary quota windows."""
    result = data.get("result", data) or {}
    limits = result.get("rateLimits") or (result.get("rateLimitsByLimitId") or {}).get("codex")
    if not isinstance(limits, dict):
        return []
    label = f"ChatGPT ({limits.get('planType') or 'subscription'})"
    covers = list(((rules or {}).get("pool_covers") or {}).get("ChatGPT", ["gpt-6*", "gpt-5.6*"]))
    reached_type = limits.get("rateLimitReachedType")
    reached = reached_type is not None
    windows = []
    for key in ("primary", "secondary"):
        raw = limits.get(key)
        if not isinstance(raw, dict):
            continue
        minutes = float(raw["windowDurationMins"])
        wtype = "weekly" if minutes == 10080 else ("5h" if minutes == 300 else f"{minutes:g}m")
        # Only the window the server names is at zero; the other keeps its own usedPercent, so a reached 5-hour window
        # does not zero the week and teach the pool rate a drop that never happened. An unnamed type zeroes both.
        rt = str(reached_type or "").lower()
        names_this = rt in (key, wtype) or ("week" in rt and wtype == "weekly") or (("hour" in rt or "5h" in rt) and wtype == "5h")
        names_other = rt and not names_this and (rt in ("primary", "secondary", "weekly", "5h") or "week" in rt or "hour" in rt or "5h" in rt)
        this_reached = reached and not names_other
        reset = raw.get("resetsAt")
        reset_iso = datetime.fromtimestamp(float(reset), timezone.utc).isoformat() if reset is not None else ""
        windows.append(_build_window({
            "window": wtype, "label": wtype, "length_s": minutes * 60,
            "remaining_fraction": 0.0 if this_reached else max(0.0, min(1.0, 1 - float(raw["usedPercent"]) / 100)),
            "reset_time": reset_iso,
        }, now_ts, default_covers=covers))
    return [{
        "provider": "codex", "id": "codex", "label": label, "covers": covers,
        "stale": False, "windows": windows, "reservations": [], "quota_reached": reached,
    }]


def fetch_codex_usage(force_refresh=False, ttl=60, fake=None, home=None, account=None):
    """Codex's rate limits; account: another account's (its CODEX_HOME), which a fake gives as codex_usage_by_account."""
    fk = resolve_fake(fake)
    if account is not None and home is None:
        home = account_home(account, fake=fk)
    if fk is not None:
        by_account = fk.get("codex_usage_by_account") or {}
        if home and account in by_account:
            return by_account[account] or {}
        cloud = fk.get("cloud")
        return fk.get("codex_usage") or (cloud.get("codex_usage") if isinstance(cloud, dict) else None) or {}
    return _codex_metadata("account/rateLimits/read", CODEX_USAGE_CACHE, ttl, force_refresh, home=home)


def _pools_from_claude_usage(cu_data, now_ts):
    if not cu_data or not isinstance(cu_data, dict):
        return [], {"status": "missing"}
    read_at = cu_data.get("read_at") or ""
    read_dt = _parse_iso_utc(read_at)
    age_s = max(0.0, now_ts - read_dt.timestamp()) if read_dt else 0.0
    stale = bool(cu_data.get("stale", age_s > CLAUDE_STALE_SECONDS))
    plan = cu_data.get("plan") or "subscription"
    raw_wins = cu_data.get("windows") or []
    pools = []
    for idx, w in enumerate(raw_wins):
        wlabel = w.get("label") or w.get("window") or f"window-{idx}"
        # A window read at another time than the file (the status line rewrites the file around get_usage's windows)
        # is stale by its own age.
        if w.get("read_at"):
            w_read_dt = _parse_iso_utc(str(w["read_at"]))
            # Its own time, or an unreadable one: never the fresh file's age.
            w_age_s = max(0.0, now_ts - w_read_dt.timestamp()) if w_read_dt else float("inf")
        else:
            w_age_s = age_s
        w_stale = bool(cu_data.get("stale")) or w_age_s > CLAUDE_STALE_SECONDS
        covers = w.get("covers")
        if covers is None:
            covers = _covers_from_window_label(wlabel, ["claude-*"])
        win_obj = _build_window(w, now_ts, default_covers=covers)
        scope_suffix = wlabel.split("·", 1)[1].strip() if "·" in wlabel else wlabel
        pid = "claude-sub-" + re.sub(r"[^a-z0-9]+", "-", scope_suffix.lower()).strip("-")
        plabel = f"Claude ({plan}) · {scope_suffix}"
        pools.append({
            "provider": "claude-subscription",
            "id": pid,
            "label": plabel,
            "covers": list(covers),
            "stale": w_stale,
            "read_at": w.get("read_at") or read_at,
            "age_s": w_age_s,
            "windows": [win_obj],
            "reservations": [],
        })
    _nest_claude_pools([p for p in pools if not p["stale"]])
    return pools, {"status": "stale" if stale else "ok", "read_at": read_at, "age_s": age_s, "plan": plan}


# Windows whose use also counts in "Weekly · all models" (Fable is not a pool of its own).
CLAUDE_NESTED_SCOPES = ("fable",)


def _claude_scope(pool):
    label = str(pool.get("label") or "")
    return label.split("·", 1)[1].strip().lower() if "·" in label else label.lower()


def _nest_claude_pools(pools):
    """What a nested window can spend is the smaller of its own leftover and the all-models leftover: its
    remaining_fraction becomes that min (the own one is kept as own_remaining_fraction) and the pool says
    nested_in the all-models pool. Without a fresh-enough all-models window in the same reading nothing is nested."""
    parent = next((p for p in pools if _claude_scope(p) in ("all models", "all", "*") and p.get("windows")), None)
    if parent is None:
        return
    cap = _available(parent["windows"][0])
    for p in pools:
        if p is parent or _claude_scope(p) not in CLAUDE_NESTED_SCOPES:
            continue
        for w in p.get("windows") or []:
            w["own_remaining_fraction"] = w["remaining_fraction"]
            w["remaining_fraction"] = min(w["remaining_fraction"], cap)
            w["available_fraction"] = w["remaining_fraction"]
        p["nested_in"] = parent["id"]


def load_claude_usage(path=None, now=None):
    # The same file /limits and the status line write: ASK_CLAUDE_USAGE_PATH moves all three.
    p = path or os.path.expanduser(os.environ.get("ASK_CLAUDE_USAGE_PATH") or CLAUDE_USAGE_PATH)
    now_ts = float(now if now is not None else time.time())
    if not os.path.exists(p):
        return [], {"status": "missing"}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return _pools_from_claude_usage(data, now_ts)
    except Exception as e:
        return [], {"status": "missing", "error": str(e)}


# --- Reservations under lock (~/.cache/ask/reservations.json) ---

def _acquire_file_lock(lock_path, timeout_s=5.0):
    """O_EXCL lock file holding the pid. A stale lock (its holder dead; unreadable and 10 s old; or a live holder 60 s
    old, since a hold lasts milliseconds) is taken over only by whoever first creates the takeover token (O_EXCL too),
    finds it still stale under that token, moves it aside and sees the same content it judged: so two takers cannot
    both remove it, and a fresh lock created meanwhile through O_EXCL is put back rather than deleted."""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    deadline = time.time() + timeout_s
    token = lock_path + ".takeover"

    def read():
        with open(lock_path, encoding="utf-8") as f:
            return f.read().strip()

    def stale(content):
        lpid = int(content or "0")
        age = time.time() - os.path.getmtime(lock_path)
        if lpid:
            return (not pid_alive(lpid)) or age > 60.0
        return age > 10.0

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except OSError:
            try:
                if stale(read()):
                    try:
                        tfd = os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    except FileExistsError:
                        if time.time() - os.path.getmtime(token) > 10.0:
                            os.remove(token)  # a taker that died in the middle
                        tfd = None
                    if tfd is not None:
                        os.close(tfd)
                        try:
                            seen = read()
                            if stale(seen):
                                taken = f"{token}.{os.getpid()}.{threading.get_ident()}"
                                os.replace(lock_path, taken)
                                with open(taken, encoding="utf-8") as f:
                                    got = f.read().strip()
                                if got != seen:
                                    try:
                                        os.replace(taken, lock_path)  # a fresh lock slipped in: it stays the holder's
                                    except OSError:
                                        os.remove(taken)
                                else:
                                    os.remove(taken)
                        finally:
                            os.remove(token)
                        continue
            except Exception:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.02)


def _atomic_write_json(path, value, indent=2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def _release_file_lock(lock_path):
    """On Windows a lock file another taker is reading at that moment cannot be removed (a sharing violation): the
    removal is tried again for up to 2 s, or the lock would stand 60 s with a live holder's pid."""
    deadline = time.time() + 2.0
    while True:
        try:
            os.remove(lock_path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.time() >= deadline:
                return
            time.sleep(0.005)
        except OSError:
            return


def _modify_reservations(fn, reservations_path=None, lock_path=None, procs=None):
    """Read-modify-write ~/.cache/ask/reservations.json in one go under a lock file, dropping dead PIDs."""
    rpath = reservations_path or RESERVATIONS_PATH
    lpath = lock_path or (rpath + ".lock" if reservations_path else RESERVATIONS_LOCK)
    locked = _acquire_file_lock(lpath)
    try:
        raw_list = []
        if os.path.exists(rpath):
            try:
                with open(rpath, encoding="utf-8") as f:
                    loaded = json.load(f)
                raw_list = loaded.get("reservations", []) if isinstance(loaded, dict) else list(loaded)
            except Exception:
                raw_list = []
        live_list = [r for r in raw_list if isinstance(r, dict) and pid_alive(int(r.get("pid") or 0), procs=procs)]
        updated = fn(list(live_list))
        if updated is None:
            updated = live_list
        if not locked:
            # Without the lock the file is not touched: a write beside another's would lose a reservation. The caller
            # keeps its result (its own run goes on); the file catches up on the next locked change.
            print(f"[reservations: lock {os.path.basename(lpath)} not acquired in time; this change is not written]",
                  file=sys.stderr)
            return updated
        if updated != raw_list or not os.path.exists(rpath):
            _atomic_write_json(rpath, updated)
        return updated
    finally:
        if locked:
            _release_file_lock(lpath)


def read_reservations(reservations_path=None, procs=None, fake=None):
    """Return active reservations, dropping any whose PID is gone."""
    fk = resolve_fake(fake)
    if fk is not None:
        if procs is None and "local" in fk and isinstance(fk["local"], dict) and "processes" in fk["local"]:
            procs = fk["local"]["processes"]
        if "reservations_path" in fk:
            return _modify_reservations(lambda x: x, reservations_path=fk["reservations_path"], procs=procs)
        if "reservations" in fk:
            raw = fk["reservations"]
            if isinstance(raw, str) and os.path.exists(raw):
                return _modify_reservations(lambda x: x, reservations_path=raw, procs=procs)
            if isinstance(raw, list):
                return [
                    r for r in raw
                    if isinstance(r, dict) and (r.get("alive", True) if "alive" in r else pid_alive(int(r.get("pid") or 0), procs=procs))
                ]
        return []
    return _modify_reservations(lambda x: x, reservations_path=reservations_path, procs=procs)


def _apply_reservations_to_pools(pools, reservations):
    """Subtract active reservations from each pool's windows and attach matching reservations to pool['reservations']."""
    for p in pools or []:
        p["reservations"] = []
        for w in p.get("windows") or []:
            w["reserved_fraction"] = 0.0
            w["available_fraction"] = float(w.get("remaining_fraction", 1.0))

    for r in reservations or []:
        r_prov = r.get("provider", "agy")
        r_model = (r.get("model") or "").lower()
        r_pid_pool = (r.get("pool_id") or "").lower()
        r_plabel = (r.get("pool") or "").lower()
        shares = r.get("share") or {}

        for p in pools or []:
            if p.get("provider") != r_prov:
                continue
            p_id = (p.get("id") or "").lower()
            p_lbl = (p.get("label") or "").lower()
            p_covers = p.get("covers") or []
            pool_matched = (
                (r_pid_pool and r_pid_pool == p_id)
                or (r_plabel and r_plabel == p_lbl)
                or (r_model and any(fnmatch.fnmatchcase(r_model, pat.lower()) for pat in p_covers))
            )
            if not pool_matched:
                continue
            if r not in p["reservations"]:
                p["reservations"].append(r)
            for w in p.get("windows") or []:
                wtype = w.get("window", "weekly")
                s_val = float(shares.get(wtype, 0.0))
                if s_val > 0:
                    w["reserved_fraction"] = round(float(w.get("reserved_fraction", 0.0)) + s_val, 6)
                    w["available_fraction"] = max(0.0, float(w.get("remaining_fraction", 1.0)) - w["reserved_fraction"])


def _normalize_fake_pools(c, now_ts, rules=None):
    pools = []
    sub_meta = {"status": "missing"}

    if isinstance(c, list):
        for idx, p in enumerate(c):
            cov = list(p.get("covers") if p.get("covers") is not None else _covers_for_agy_group(p.get("label") or p.get("name"), rules))
            wins = [_build_window(w, now_ts, default_covers=cov) for w in (p.get("windows") or p.get("buckets") or [])]
            pools.append({
                "provider": p.get("provider", "agy"),
                "id": p.get("id") or f"pool-{idx}",
                "label": p.get("label") or p.get("name") or f"Pool {idx}",
                "covers": cov,
                "stale": bool(p.get("stale", False)),
                "windows": wins,
                "reservations": [],
            })
        return pools, sub_meta

    if "pools" in c and isinstance(c["pools"], list):
        pools, _ = _normalize_fake_pools(c["pools"], now_ts, rules=rules)
    elif "groups" in c:
        pools.extend(_pools_from_agy_groups(c["groups"], now_ts, rules=rules))
    else:
        raw_p = c.get("pools") if isinstance(c.get("pools"), dict) else c
        shorthand_defs = [
            ("gemini", "Gemini Models", ["gemini-*"]),
            ("3p", "Claude and GPT models", ["claude-*", "gpt-oss-*"]),
        ]
        for key, lbl, cov in shorthand_defs:
            if key in raw_p and isinstance(raw_p[key], dict):
                p = raw_p[key]
                w_rem = p.get("weekly_remaining", p.get("weekly", 1.0))
                h_rem = p.get("five_hour_remaining", p.get("5h_remaining", p.get("5h", p.get("five_hour", 1.0))))
                wl_frac = p.get("week_left_fraction", 0.5)
                w_pace = p.get("pace", p.get("weekly_pace"))
                h_pace = p.get("5h_pace")
                wins = [
                    _build_window({
                        "window": "weekly",
                        "remaining_fraction": w_rem,
                        "reset_time": p.get("weekly_reset", ""),
                        "week_left_fraction": wl_frac,
                        "pace": w_pace,
                    }, now_ts, default_covers=cov),
                    _build_window({
                        "window": "5h",
                        "remaining_fraction": h_rem,
                        "reset_time": p.get("five_hour_reset", p.get("5h_reset", "")),
                        "pace": h_pace,
                    }, now_ts, default_covers=cov),
                ]
                pools.append({
                    "provider": "agy",
                    "id": key,
                    "label": p.get("name", p.get("label", lbl)),
                    "covers": list(p.get("covers", cov)),
                    "stale": False,
                    "windows": wins,
                    "reservations": [],
                })

    cu = c.get("claude_usage") if "claude_usage" in c else c.get("subscription")
    if cu is not None:
        if isinstance(cu, str) and os.path.exists(cu):
            sub_pools, sub_meta = load_claude_usage(cu, now=now_ts)
        else:
            sub_pools, sub_meta = _pools_from_claude_usage(cu, now_ts)
        pools.extend(sub_pools)

    return pools, sub_meta


def read_pool_history(path=None):
    """History is advisory; missing or corrupt data means no recent estimate."""
    try:
        with open(path or POOL_HISTORY, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _window_reset_epoch(window, now_ts):
    reset = _parse_iso_utc(window.get("reset_time") or window.get("resetsAt") or "")
    if reset is not None:
        return reset.timestamp()
    left = window.get("resets_in_s")
    return now_ts + left if left is not None else None


def record_pool_history(pools, now_ts, path=None, rules=None):
    """Record unreserved weekly shares, under a short advisory lock."""
    path = os.path.abspath(path or POOL_HISTORY)
    locked = False
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        locked = _acquire_file_lock(path + ".lock", timeout_s=.1)
        if not locked:
            return
        history = read_pool_history(path)
        old = sum(len(samples) for samples in history.values())
        history = {label: [s for s in samples if s[0] >= now_ts - 8 * 86400]
                   for label, samples in history.items()}
        gap = (rules or load_roles()).get("score_defaults", {}).get("history_min_gap_s", 600)
        changed = sum(len(samples) for samples in history.values()) != old
        for pool in pools:
            if pool.get("stale"):
                continue
            for window in pool.get("windows", []):
                if "week" not in str(window.get("window") or "").lower():
                    continue
                samples = history.setdefault(pool["label"], [])
                remaining = window["remaining_fraction"]
                if not samples or now_ts - samples[-1][0] >= gap or abs(remaining - samples[-1][1]) > .001:
                    samples.append([now_ts, remaining, _window_reset_epoch(window, now_ts)])
                    changed = True
        if changed:  # the band reads the pools every minute: no rewrite when nothing was added
            _atomic_write_json(path, history)
    except Exception:
        pass
    finally:
        try:
            if locked:
                if os.path.exists(tmp):
                    os.remove(tmp)
                _release_file_lock(path + ".lock")
        except Exception:
            pass


def recent_rate(label, window, now_ts, rules, path=None, history=None):
    """Share used per second in the newest uninterrupted recent window series."""
    try:
        data = read_pool_history(path) if history is None else history
        defaults = rules.get("score_defaults", {})
        cutoff = now_ts - defaults.get("recent_hours", 24) * 3600
        reset = _window_reset_epoch(window, now_ts)
        series = []
        for sample in sorted(data.get(label, []), key=lambda s: s[0]):
            ts, remaining, sample_reset = sample
            if ts > now_ts:
                continue
            if reset is not None and sample_reset is not None and abs(reset - sample_reset) > 3600:
                series = []
                continue
            if series and remaining - series[-1][1] > .02:
                series = []
            series.append(sample)
        remaining = window.get("remaining_fraction", window.get("available_fraction", 1))
        if series and remaining - series[-1][1] > .02:
            return None
        series = [s for s in series if s[0] >= cutoff]
        if not series:
            return None
        span = now_ts - series[0][0]
        if span <= 0 or span < defaults.get("history_min_span_h", 2) * 3600:
            return None
        return max(0., (series[0][1] - remaining) / span)
    except Exception:
        return None


def projected_unused(window, rate):
    left = window.get("resets_in_s")
    if rate is None or left is None:
        return None
    return max(0., window.get("available_fraction", window.get("remaining_fraction", 1)) - rate * left)


def cloud_state(fake=None, agy_path=None, force_refresh=False, ttl=60, now=None, rules=None, claude_usage_path=None, reservations_path=None,
                account=None):
    """Return all quota pools (agy and Codex cached 60 s, plus ~/.cache/ask/claude_usage.json),
    with active reservations subtracted from each window. account: the Codex account whose pool is read (None: the active one).
    """
    now_ts = float(now if now is not None else time.time())
    rules = rules or load_roles()
    fk = resolve_fake(fake)
    if fk is not None:
        c = fk.get("cloud", fk)
        pools, sub_meta = _normalize_fake_pools(c, now_ts, rules=rules)
        if sub_meta.get("status") == "missing" and ("claude_usage" in fk or "subscription" in fk):
            cu_top = fk.get("claude_usage") if "claude_usage" in fk else fk.get("subscription")
            if isinstance(cu_top, str) and os.path.exists(cu_top):
                sub_pools, sub_meta = load_claude_usage(cu_top, now=now_ts)
            else:
                sub_pools, sub_meta = _pools_from_claude_usage(cu_top, now_ts)
            pools.extend(sub_pools)
        pools.extend(_pools_from_codex_usage(fetch_codex_usage(fake=fk, account=account), now_ts, rules=rules))
        res_list = read_reservations(reservations_path=reservations_path, fake=fk)
        _apply_reservations_to_pools(pools, res_list)
        return {
            "ok": c.get("ok", True) if isinstance(c, dict) else True,
            "error": c.get("error") if isinstance(c, dict) else None,
            "fetched_at": now_ts,
            "cached": False,
            "pools": pools,
            "pool_history": c.get("pool_history") if isinstance(c, dict) else None,
            "now_ts": now_ts,
            "reservations": res_list,
            "claude_subscription": sub_meta,
            "account": account,
        }

    agy_groups = None
    cached_flag = False
    err_msg = None
    fetched_ts = now_ts

    if not force_refresh and ttl > 0 and os.path.exists(USAGE_CACHE):
        try:
            with open(USAGE_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            ts = float(cached.get("fetched_at", 0))
            if now_ts - ts <= ttl and "groups" in cached:
                agy_groups = cached["groups"]
                fetched_ts = ts
                cached_flag = True
        except Exception:
            pass

    if agy_groups is None and (not provider_present("agy")["present"] or (not force_refresh and agy_unavailable(now_ts))):
        # Not on this machine (null in machine.json, or nowhere to be found): never run it. An agy that is there but not
        # signed in opens a sign-in window per run, so "not here" must mean not asked at all.
        agy_groups = []
    if agy_groups is None:
        if not agy_path:
            agy_path, _ = load_config()
        os.makedirs(ASK_ROOM, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            done = subprocess.run(
                [agy_path, "-p", "/usage", "--output-format", "json", "--print-timeout", "15s"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, cwd=ASK_ROOM,
            )
            out = done.stdout.strip()
            idx = out.find("{")
            if idx >= 0:
                out = out[idx:]
            data = json.loads(out)
            agy_groups = (
                data.get("command", {}).get("data", {}).get("groups")
                or data.get("result", {}).get("command", {}).get("data", {}).get("groups")
                or data.get("groups")
                or []
            )
            with open(USAGE_CACHE, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now_ts, "groups": agy_groups}, f, indent=2)
        except Exception as e:
            err_msg = str(e)
            agy_groups = []
            _mark_agy(f"agy /usage failed: {err_msg[:80]}")

    pools = _pools_from_agy_groups(agy_groups, now_ts, rules=rules)
    codex_error = None
    try:
        pools.extend(_pools_from_codex_usage(fetch_codex_usage(force_refresh=force_refresh, ttl=ttl, account=account),
                                             now_ts, rules=rules))
    except Exception as e:
        codex_error = str(e)
    sub_pools, sub_meta = load_claude_usage(claude_usage_path, now=now_ts)
    pools.extend(sub_pools)
    record_pool_history(pools, now_ts, rules=rules)
    pool_history = read_pool_history() or None
    res_list = read_reservations(reservations_path=reservations_path)
    _apply_reservations_to_pools(pools, res_list)
    return {
        "ok": err_msg is None,
        "error": err_msg,
        "fetched_at": fetched_ts,
        "cached": cached_flag,
        "pools": pools,
        "pool_history": pool_history,
        "now_ts": now_ts,
        "reservations": res_list,
        "claude_subscription": sub_meta,
        "codex_error": codex_error,
        "account": account,
    }


def covering_pools_and_windows(provider, model_id, pools, include_stale=False):
    """Return [(pool, window)] for every pool and window that covers (provider, model_id)."""
    m_low = (model_id or "").lower()
    matches = []
    for p in pools or []:
        if p.get("provider") != provider:
            continue
        if p.get("stale") and not include_stale:
            continue
        p_covers = p.get("covers") or []
        for w in p.get("windows") or []:
            w_covers = w.get("covers") if w.get("covers") is not None else p_covers
            if any(fnmatch.fnmatchcase(m_low, pat.lower()) for pat in w_covers):
                matches.append((p, w))
    return matches


def pool_fractions_for_model(provider, model_id, cs):
    """Extract {'weekly': float, '5h': float, ...} remaining fractions for (provider, model_id) from cloud_state."""
    out = {}
    for _, w in covering_pools_and_windows(provider, model_id, (cs or {}).get("pools", []), include_stale=False):
        wtype = w.get("window")
        if wtype and wtype not in out:
            out[wtype] = round(float(w.get("remaining_fraction", 1.0)), 6)
    return out


def pool_provider(pool_name):
    """Whose pool a pool_covers name is: "ChatGPT ..." Codex's, "Claude (plan) ..." the subscription's, the rest agy's groups."""
    low = str(pool_name or "").lower()
    if low.startswith("chatgpt"):
        return "codex"
    if low.startswith("claude (") or low.startswith("claude subscription"):
        return "claude-subscription"
    return "agy"


def short_pool_name(label):
    """Compact pool name for reason lines: 'Gemini Models' -> 'Gemini', 'Claude and GPT models' -> 'Claude/GPT'."""
    s = str(label or "pool")
    low = s.lower()
    if low.startswith("gemini"):
        return "Gemini"
    if "claude" in low and "gpt" in low:
        return "Claude/GPT"
    if low.startswith("chatgpt"):
        return "ChatGPT"
    return s


# --- Token & rate estimation from usage.jsonl ---

def _read_journal_rows(journal_path=None, fake=None):
    fk = resolve_fake(fake)
    if fk is not None:
        if "journal" in fk and isinstance(fk["journal"], list):
            return list(fk["journal"])
        if "journal_path" in fk and os.path.exists(fk["journal_path"]):
            journal_path = fk["journal_path"]
        elif journal_path is None:
            return []
    p = journal_path or JOURNAL_PATH
    if not os.path.exists(p):
        return []
    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _token_pool(row, provider):
    """Old rows may lack a pool label."""
    return row.get("pool") or ("ChatGPT" if provider == "codex" else
                               "Gemini Models" if str(row.get("model") or "").lower().startswith("gemini") else
                               "Claude and GPT models" if provider == "agy" else None)


def billed_tokens(row_or_tokens, provider, pool=None, rules=None):
    """One cost unit for estimates, journal and caps; preserve recorded units."""
    rules = rules if rules is not None else load_roles()
    row = row_or_tokens or {}
    tok = (row.get("tokens") or {}) if "tokens" in row else row
    pool = pool or row.get("pool") or _token_pool(row, provider)  # no pool label (a meter, an old row): by provider
    weights = rules.get("cache_weight") or {}
    weight = next((v for k, v in weights.items() if k != "_comment" and
                   short_pool_name(k).lower() == short_pool_name(pool).lower()), weights.get("default", 0.0))
    cached = int(tok.get("cached_in") or 0)
    # Some legacy rows only retained total; it is the best available input.
    total = ((int(tok.get("in") or 0) + int(tok.get("out") or 0))
             if tok.get("in") is not None or tok.get("out") is not None else int(tok.get("total") or 0))
    fresh = int(tok["fresh"]) if tok.get("fresh") is not None else max(0, total - (cached if provider == "codex" else 0))
    billed = int(tok["billed"]) if tok.get("billed") is not None else fresh + round(float(weight) * cached)
    return {"fresh": fresh, "cached": cached, "billed": billed}


def pool_window_rate(pool_label, window_type="weekly", rules=None, journal_path=None, fake=None, now=None):
    """Share per million billed tokens over 14 days, split at resets.
    Whole-percent readings add one rounding step per segment. Use outside this journal
    also lowers readings, so the learned rate is on the high, safe side.
    """
    rules = rules or load_roles()
    rows = _read_journal_rows(journal_path=journal_path, fake=fake)
    target_short = short_pool_name(pool_label).lower()
    target_low = str(pool_label or "").lower()

    samples = []
    now_ts = float(now if now is not None else time.time())
    for r in rows:
        if r.get("provider") not in ("agy", "codex") or "type" in r:
            continue
        ts = r.get("ts")
        if ts is None:
            dt = _parse_iso_utc(r.get("time", ""))
            ts = dt.timestamp() if dt else 0.0
        if not now_ts - 14 * 86400 <= float(ts) <= now_ts:
            continue
        r_pool = _token_pool(r, r.get("provider"))
        if r_pool.lower() != target_low and short_pool_name(r_pool).lower() != target_short:
            continue
        tok = billed_tokens(r, r.get("provider"), pool=r_pool, rules=rules)["billed"]
        if tok <= 0:
            continue
        before = r.get("before") or {}
        after = r.get("after") or {}
        if window_type in before and window_type in after and before[window_type] is not None and after[window_type] is not None:
            samples.append((float(ts), float(before[window_type]), float(after[window_type]), tok))

    tokens = sum(s[3] for s in samples)
    if len(samples) >= 3 and tokens >= 1_000_000:
        samples.sort(key=lambda s: s[0])
        segments, drop = 1, 0.0
        first, last = samples[0][1:3]
        for _, before, after, _ in samples[1:]:
            if before - last > 0.005:
                drop += max(0.0, first - last)
                first = before
                segments += 1
            last = after
        drop += max(0.0, first - last)
        step = 0.01 if all(abs(v * 100 - round(v * 100)) <= 1e-9
                           for s in samples for v in s[1:3]) else 0.0
        return (drop + step * segments) / (tokens / 1_000_000.0)

    rdefs = rules.get("rate_default") or {}
    p_def = rdefs.get(pool_label) or rdefs.get(short_pool_name(pool_label)) or rdefs.get("default") or {"weekly": 0.05, "5h": 0.15}
    return float(p_def.get(window_type, p_def.get("weekly", 0.05)))


def estimate_tokens(role=None, override_tokens=None, rules=None, journal_path=None, fake=None, provider=None, model=None):
    """Billed median of last 10 role/provider runs (at least 3), else role, else default."""
    if override_tokens is not None:
        return int(override_tokens)
    rules = rules or load_roles()
    rows = _read_journal_rows(journal_path=journal_path, fake=fake)
    if role:
        samples = [(r.get("provider"), billed_tokens(r, r.get("provider"),
                    pool=_token_pool(r, r.get("provider")), rules=rules)["billed"])
                   for r in rows if r.get("role") == role and "type" not in r]
        role_toks = [tok for _, tok in samples if tok > 0]
        provider_toks = [tok for prov, tok in samples if tok > 0 and prov == provider]
        if provider is not None and len(provider_toks) >= 3:
            return int(statistics.median(provider_toks[-10:]))
        if role_toks:
            return int(statistics.median(role_toks[-10:]))
    tdefs = rules.get("tokens_default") or {}
    if role and role in tdefs:
        return int(tdefs[role])
    return int(tdefs.get("default", 20000))


def compute_needed_shares(provider, model_id, est_tokens, cs, rules=None, journal_path=None, fake=None, role=None):
    """Compute {window_type: share_needed} for (provider, model_id) given est_tokens."""
    rules = rules or load_roles()
    est_tokens = estimate_tokens(role, override_tokens=est_tokens, rules=rules, journal_path=journal_path,
                                 fake=fake, provider=provider, model=model_id)
    shares = {}
    pairs = covering_pools_and_windows(provider, model_id, (cs or {}).get("pools", []), include_stale=False)
    for p, w in pairs:
        wtype = w.get("window", "weekly")
        rate = pool_window_rate(p.get("label"), wtype, rules=rules, journal_path=journal_path, fake=fake)
        need = (float(est_tokens) / 1_000_000.0) * rate
        shares[wtype] = max(shares.get(wtype, 0.0), round(need, 6))
    return shares


def reserve_quota(script, role, provider, model_id, tokens=None, rules=None, fake=None, reservations_path=None, pid=None, cs=None,
                  force=False):
    """Reserve the estimated share of each window of the model's pool at run start under a lock file. The fit is checked
    again there against every live reservation: two runs that each saw room above the floor cannot both reserve it. A
    refused reservation is returned with "refused": the reason (nothing written); force skips the check."""
    rules = rules or load_roles()
    if provider not in ("agy", "codex"):
        return None
    _, mid = resolve_model(model_id, provider=provider, rules=rules)
    mid = mid or model_id
    cs = cs or cloud_state(fake=fake, rules=rules, reservations_path=reservations_path)
    est_tok = estimate_tokens(role=role, override_tokens=tokens, rules=rules, fake=fake, provider=provider, model=mid)
    shares = compute_needed_shares(provider, mid, est_tok, cs, rules=rules, fake=fake)
    pairs = covering_pools_and_windows(provider, mid, cs.get("pools", []), include_stale=False)
    pool_id = pairs[0][0].get("id", "") if pairs else ""
    pool_lbl = pairs[0][0].get("label", "") if pairs else ""
    now_ts = time.time()
    use_pid = int(pid if pid is not None else os.getpid())
    res_id = f"{use_pid}-{now_ts:.6f}"
    entry = {
        "id": res_id,
        "pid": use_pid,
        "script": script,
        "role": role,
        "provider": provider,
        "model": mid,
        "pool_id": pool_id,
        "pool": pool_lbl,
        "tokens": est_tok,
        "share": shares,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts": round(now_ts, 3),
    }
    procs = None
    fk = resolve_fake(fake)
    if fk is not None and "local" in fk and isinstance(fk["local"], dict):
        procs = fk["local"].get("processes")
    floors = rules.get("floors", {"weekly": 0.10, "5h": 0.10})

    def add(cur):
        if not force:
            for p, w in pairs:
                wtype = w.get("window", "weekly")
                need = float(shares.get(wtype, 0.0))
                reserved = sum(float((r.get("share") or {}).get(wtype, 0.0)) for r in cur
                               if r.get("provider") == provider and (r.get("pool_id") or "") == pool_id)
                left = _frac(w, "remaining_fraction", 1.0) - reserved - need
                floor_val = float(floors.get(wtype, floors.get("weekly", 0.10)))
                if left < floor_val:
                    entry["refused"] = (f"{mid}: {pool_lbl or pool_id} {wtype} would be left at {left * 100:.1f}% "
                                        f"(floor {floor_val * 100:.0f}%) once {len(cur)} live reservation(s) and this run's "
                                        f"~{need * 100:.1f}% are counted")
                    return cur
        return cur + [entry]
    _modify_reservations(add, reservations_path=reservations_path, procs=procs)
    return entry


def release_reservation(reservation_or_id, reservations_path=None, procs=None):
    """Remove a completed reservation from ~/.cache/ask/reservations.json under the lock."""
    if not reservation_or_id:
        return
    target_id = reservation_or_id.get("id") if isinstance(reservation_or_id, dict) else str(reservation_or_id)
    _modify_reservations(
        lambda cur: [r for r in cur if r.get("id") != target_id],
        reservations_path=reservations_path,
        procs=procs,
    )


# --- Processes, GPU and Ollama checks ---

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _list_processes_posix():
    """macOS and Linux: every process by `ps -axo pid=,comm=`."""
    try:
        r = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            out.append({"pid": int(parts[0]), "name": os.path.basename(parts[1]), "path": parts[1]})
    return out


def list_processes():
    if not sys.platform.startswith("win"):
        return _list_processes_posix()
    try:
        k32 = ctypes.windll.kernel32
        # Without these, a 64-bit HANDLE comes back truncated to a C int and INVALID_HANDLE_VALUE (-1) never matches
        # the comparison below, so a failed snapshot read as an empty process list.
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)]
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snap is not None and snap != wintypes.HANDLE(-1).value:
            out = []
            try:
                pe = _PROCESSENTRY32W()
                pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
                ok = k32.Process32FirstW(snap, ctypes.byref(pe))
                buf = ctypes.create_unicode_buffer(1024)
                while ok:
                    pid = int(pe.th32ProcessID)
                    name = pe.szExeFile
                    path = ""
                    if pid > 4:
                        h = k32.OpenProcess(0x1000, False, pid)
                        if h:
                            try:
                                sz = wintypes.DWORD(1024)
                                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
                                    path = buf.value
                            finally:
                                k32.CloseHandle(h)
                    out.append({"pid": pid, "name": name, "path": path})
                    ok = k32.Process32NextW(snap, ctypes.byref(pe))
                return out
            finally:
                k32.CloseHandle(snap)
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15,
        )
        rows = json.loads(r.stdout)
        if isinstance(rows, dict):
            rows = [rows]
        return [{"pid": int(p.get("ProcessId") or 0), "name": p.get("Name") or "", "path": p.get("ExecutablePath") or ""}
                for p in rows]
    except Exception:
        return []


def pid_alive(pid, procs=None):
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if procs is not None:
        return any(int(p.get("pid", -1)) == int(pid) for p in procs)
    if sys.platform.startswith("win"):
        try:
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return ctypes.get_last_error() == 5 or k32.GetLastError() == 5  # ERROR_ACCESS_DENIED: it exists
            try:
                code = wintypes.DWORD(0)
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True  # it exists; it is another user's or elevated, so its lock or reservation is not taken over
    except OSError:
        return False


def machine_gpu(machine=None):
    """machine.json "hardware": {"gpu": {"kind": ..., "model_budget_gb": 12}}. kind: nvidia (nvidia-smi says the free
    memory), amd (rocm-smi says it), apple or unified (local models get model_budget_gb of the memory, less what Ollama
    holds: a Mac, an integrated GPU, a GPU no tool reads, or the CPU), none (no local models). By default: apple on macOS,
    else nvidia or amd when their tool is on PATH, else unified."""
    if machine is None:
        machine = load_machine()
    hw = machine.get("hardware") if isinstance(machine.get("hardware"), dict) else {}
    gpu = dict(hw.get("gpu")) if isinstance(hw.get("gpu"), dict) else {}
    if gpu.get("kind"):
        gpu["kind"] = str(gpu["kind"]).lower()
    elif host_platform() == "darwin":
        gpu["kind"] = "apple"
    elif shutil.which("nvidia-smi"):
        gpu["kind"], gpu["_guessed"] = "nvidia", True
    elif shutil.which("rocm-smi"):
        gpu["kind"], gpu["_guessed"] = "amd", True
    else:
        gpu["kind"] = "unified"
    if gpu["kind"] in ("apple", "unified") and gpu.get("model_budget_gb") is None:  # 0 is the machine's own "no memory"
        # Unified memory with no budget set: half of the RAM, so a first run is not "no local model fits"; the
        # machine's own model_budget_gb always wins (macOS may take more for itself than half suggests).
        ram = _ram_bytes()
        if ram:
            gpu["model_budget_gb"] = round(ram / 2 / 1024 ** 3, 1)
            gpu["budget_source"] = "half of the RAM"
    return gpu


def _ram_bytes():
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        return None


def pick_card(cards, device=None):
    """Of [(used_mb, total_mb, util)] the card a model goes to: hardware.gpu.device by its index, else the one with the
    most free memory (where Ollama and the servers put a model) ."""
    if not cards:
        return None
    if device is not None:
        try:
            return cards[int(device)]
        except (ValueError, IndexError, TypeError):
            return None
    return max(cards, key=lambda c: c[1] - c[0])


def query_rocm_smi(device=None):
    """AMD's VRAM by rocm-smi: (used_mb, total_mb, 0, []), total None when it could not be read (the caller then blocks)."""
    cards = []
    try:
        r = subprocess.run(["rocm-smi", "--showmeminfo", "vram", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
        for name in sorted((data or {}), key=lambda n: int(re.sub(r"\D", "", n) or 0)):
            card = data[name]
            if not isinstance(card, dict):
                continue
            total = next((v for k, v in card.items() if "total" in k.lower() and "used" not in k.lower()), None)
            used = next((v for k, v in card.items() if "used" in k.lower()), None)
            if total is not None and used is not None:
                cards.append((int(int(used) / 1024 ** 2), int(int(total) / 1024 ** 2), 0))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    card = pick_card(cards, device)
    return (card[0], card[1], 0, []) if card else (0, None, 0, [])


def _available_ram_mb():
    """What the system says is available to a new program now, in MB, or None: Linux MemAvailable, Windows
    GlobalMemoryStatusEx, macOS vm_stat (free, inactive and speculative pages)."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        elif sys.platform.startswith("win"):
            class _MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MEMSTATUS()
            st.dwLength = ctypes.sizeof(_MEMSTATUS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullAvailPhys // 1024 ** 2)
        elif sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))
            pages = sum(int(m.group(2)) for m in re.finditer(r"Pages (free|inactive|speculative):\s+(\d+)", out))
            return pages * page // 1024 ** 2
    except Exception:
        return None
    return None


def query_nvidia_smi(pid_map=None, device=None):
    # total None until nvidia-smi says it: an unread GPU is not 16 GB free (it was), the caller blocks.
    used_mb, total_mb, util_pct = 0, None, 0
    gpu_procs = {}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            cards = []
            for line in r.stdout.strip().splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 3:
                    cards.append((int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))))
            card = pick_card(cards, device)
            if card:
                used_mb, total_mb, util_pct = card
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) >= 3 and parts[0].isdigit():
                    pid = int(parts[0])
                    raw_name = parts[1]
                    mem_s = re.sub(r"[^\d.]", "", parts[2])
                    mem_mb = int(float(mem_s)) if mem_s else 0
                    name = (pid_map or {}).get(pid, {}).get("name") or os.path.basename(raw_name)
                    gpu_procs[pid] = {"pid": pid, "name": name, "used_mb": mem_mb, "type": "C"}
    except Exception:
        pass

    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            pat = re.compile(r"^\|\s*\d+\s+\S+\s+\S+\s+(\d+)\s+(C\+G|G|C)\s+(.+?)\s+(N/A|\d+\s*MiB)\s*\|")
            for line in r.stdout.splitlines():
                m = pat.match(line)
                if m:
                    pid = int(m.group(1))
                    ptype = m.group(2)
                    raw_name = m.group(3).strip()
                    mem_str = m.group(4).strip()
                    mem_s = re.sub(r"[^\d.]", "", mem_str)
                    mem_mb = int(float(mem_s)) if mem_s else 0
                    name = (pid_map or {}).get(pid, {}).get("name") or os.path.basename(raw_name)
                    prev = gpu_procs.get(pid, {})
                    gpu_procs[pid] = {
                        "pid": pid,
                        "name": name,
                        "used_mb": max(mem_mb, prev.get("used_mb", 0)),
                        "type": ptype,
                    }
    except Exception:
        pass

    return used_mb, total_mb, util_pct, list(gpu_procs.values())


def query_ollama(ollama_url=None):
    url = ollama_url or OLLAMA_DEFAULT
    tags_sizes = {}
    loaded = []
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        for m in data.get("models", []):
            n = m.get("name", "")
            sz = int(m.get("size") or 0)
            if n and sz:
                tags_sizes[n] = sz
    except Exception:
        return False, tags_sizes, loaded

    try:
        with urllib.request.urlopen(url + "/api/ps", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        for m in data.get("models", []):
            loaded.append({
                "name": m.get("name") or m.get("model") or "",
                "size_vram": int(m.get("size_vram") or m.get("size") or 0),
            })
    except Exception:
        pass
    return True, tags_sizes, loaded


def load_vram_cache(path=None):
    p = path or VRAM_CACHE
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def record_vram_from_ps(ollama_url=None, vram_path=None):
    url = ollama_url or OLLAMA_DEFAULT
    p = vram_path or VRAM_CACHE
    try:
        with urllib.request.urlopen(url + "/api/ps", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
        models_ps = data.get("models", [])
        if not models_ps:
            return {}
        cache = load_vram_cache(p)
        for m in models_ps:
            name = m.get("name") or m.get("model")
            vram = int(m.get("size_vram") or 0)
            if name and vram > 0:
                cache[name] = vram
                if ":" in name and name.endswith(":latest"):
                    cache[name[:-7]] = vram
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        return cache
    except Exception:
        return {}


def _to_gb(val):
    v = float(val)
    if v >= 1_000_000:
        return v / 1_000_000_000.0
    if v > 100:
        return v / 1024.0
    return v


def needed_vram_gb(model, vram_learned=None, file_sizes=None):
    if not model:
        return 0.0
    learned = vram_learned if vram_learned is not None else load_vram_cache()
    if model in learned and learned[model]:
        return _to_gb(learned[model])
    fsize = (file_sizes or {}).get(model)
    if fsize:
        return _to_gb(fsize) * 1.3 + 1.0
    # No size known: from the parameter count in the id ("gpt-oss:20b" -> 19 GB, about what a 4-bit file needs with
    # its context), never under 12 GB, so an unknown model is not let in on a guess that is too small.
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![a-z])", str(model).lower())
    return max(12.0, float(m.group(1)) * 0.9 + 1.0) if m else 12.0


def inspect_local_lock(lock_path=None, procs=None, take_stale=True):
    p = lock_path or LOCAL_LOCK
    if not os.path.exists(p):
        return None, None
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.read().strip()
        if raw.startswith("{"):
            pid = int(json.loads(raw).get("pid") or 0)
        else:
            pid = int(raw)
    except Exception:
        pid = 0
    if not pid or pid == os.getpid():
        return None, None
    if pid_alive(pid, procs=procs):
        return pid, None
    if take_stale:
        try:
            os.remove(p)
        except OSError:
            pass
    return None, pid


def acquire_local_lock(model=None, lock_path=None):
    """Take the GPU lock atomically (O_EXCL): two processes never both load a model; a dead holder's lock is taken over."""
    p = lock_path or LOCAL_LOCK
    os.makedirs(os.path.dirname(p), exist_ok=True)
    stale_pid = None
    for _ in range(3):
        held_by, stale = inspect_local_lock(p, take_stale=True)
        stale_pid = stale_pid or stale
        if held_by is not None:
            return False, held_by, stale_pid
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue  # another process won the race: read it again
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "model": model or "", "time": time.time()}, f)
        return True, os.getpid(), stale_pid
    held_by, _ = inspect_local_lock(p, take_stale=False)
    return False, held_by or 0, stale_pid


def _hooks():
    import hooks  # skills/ask/hooks.py: the machine's optional hooks, named in machine.json
    return hooks


def claim_gpu(model=None, lock_path=None, machine=None, fake=None):
    """Take local.lock first, then ask the machine's hooks for a last look (an engine about to start or running); if they
    object, give the lock back. Both sides set their own flag before reading the other's, so the two can never both go; the other side wins a tie. Returns (ok, reason)."""
    ok, held_by, _ = acquire_local_lock(model, lock_path=lock_path)
    if not ok:
        return False, f"local.lock held by pid {held_by}"
    if machine is None:
        machine = load_machine(fake=fake)
    reasons = _hooks().blockers("claim_blockers", model, machine=machine)
    if reasons:
        release_local_lock(lock_path)
        return False, "; ".join(reasons)
    return True, ""


def build_blockers(lock_path=None, machine=None, fake=None, procs=None):
    """What blocks a build now: a live local model's GPU lock, and whatever the machine's hooks say ([] = a build may go).
    For a machine's own engine scripts: `python3 skills/ask/hooks.py build-blockers` (exit 1 with the reasons)."""
    reasons = []
    held_by, _ = inspect_local_lock(lock_path or LOCAL_LOCK, procs=procs, take_stale=False)
    if held_by:
        reasons.append(f"a local model holds the GPU (local.lock, pid {held_by})")
    if machine is None:
        machine = load_machine(fake=fake)
    return reasons + _hooks().blockers("build_blockers", machine=machine)


def release_local_lock(lock_path=None):
    p = lock_path or LOCAL_LOCK
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            raw = f.read().strip()
        pid = int(json.loads(raw).get("pid") or 0) if raw.startswith("{") else int(raw)
        if pid == os.getpid():
            os.remove(p)
    except Exception:
        pass


def local_state(model=None, fake=None, ollama_url=None, rules=None, provider="ollama"):
    rules = rules or load_roles()
    fk = resolve_fake(fake)
    loc_fk = fk.get("local", fk) if fk is not None else None

    memory_unread = None
    stale_lock_pid = None
    if loc_fk is not None and any(k in loc_fk for k in ("processes", "gpu_used_mb", "gpu_processes", "lock")):
        procs = list(loc_fk.get("processes", []))

        used_mb = int(loc_fk.get("gpu_used_mb", int(float(loc_fk.get("gpu_used_gb", 1.2)) * 1024)))
        total_mb = int(loc_fk.get("gpu_total_mb", int(float(loc_fk.get("gpu_total_gb", 16.0)) * 1024)))
        util_pct = int(loc_fk.get("gpu_util", 5))
        gpu_procs = list(loc_fk.get("gpu_processes", []))
        ollama_up = bool(loc_fk.get("ollama_up", True))
        tags_sizes = {}
        tags_sizes.update(loc_fk.get("ollama_tags", {}))
        loaded = list(loc_fk.get("ollama_ps", loc_fk.get("loaded", [])))
        vram_learned = dict(loc_fk.get("vram_learned", {
            "qwen3:14b": 12_000_000_000,
            "gemma4:12b": 9_500_000_000,
            "gpt-oss:20b": 12_700_000_000,
        }))

        lock_info = loc_fk.get("lock")
        held_by_pid = None
        if isinstance(lock_info, dict):
            lpid = int(lock_info.get("pid") or 0)
            alive = lock_info.get("alive")
            if alive is None:
                alive = pid_alive(lpid, procs=procs)
            if alive and lpid != os.getpid():
                held_by_pid = lpid
            elif lpid:
                stale_lock_pid = lpid
        elif isinstance(lock_info, str) and os.path.exists(lock_info):
            held_by_pid, stale_lock_pid = inspect_local_lock(lock_info, procs=procs, take_stale=True)
        elif isinstance(lock_info, int) and lock_info > 0:
            if pid_alive(lock_info, procs=procs):
                held_by_pid = lock_info
            else:
                stale_lock_pid = lock_info
    elif fk is not None:
        procs = []
        used_mb, total_mb, util_pct, gpu_procs = 1200, 16376, 4, []
        ollama_up = True
        tags_sizes = {}
        loaded = []
        vram_learned = {
            "qwen3:14b": 12_000_000_000,
            "gemma4:12b": 9_500_000_000,
            "gpt-oss:20b": 12_700_000_000,
        }
        held_by_pid = None
    else:
        procs = list_processes()
        pid_map = {p["pid"]: p for p in procs}
        # Ollama's loaded models take memory whoever the call is for: asked whenever Ollama is here (review: a call to
        # a local server did not count them).
        ollama_here = provider == "ollama" or provider_present("ollama")["present"]
        ollama_up, tags_sizes, loaded = query_ollama(ollama_url) if ollama_here else (False, {}, [])
        gpu = machine_gpu()
        kind = gpu["kind"]
        if kind == "nvidia":
            used_mb, total_mb, util_pct, gpu_procs = query_nvidia_smi(pid_map=pid_map, device=gpu.get("device"))
            if total_mb is None and gpu.get("_guessed") and shutil.which("rocm-smi"):
                kind = "amd"  # a guessed kind whose tool reads nothing is not the end: the other tool may 
        if kind == "amd":
            used_mb, total_mb, util_pct, gpu_procs = query_rocm_smi(device=gpu.get("device"))
        if kind in ("nvidia", "amd") and total_mb is None:
            if gpu.get("_guessed"):
                kind = "unified"  # neither tool reads this machine's GPU: its budget, as for any other
                if gpu.get("model_budget_gb") is None:
                    ram = _ram_bytes()
                    gpu["model_budget_gb"] = round(ram / 2 / 1024 ** 3, 1) if ram else 0
            else:
                memory_unread = f"the GPU's memory could not be read ({'nvidia-smi' if kind == 'nvidia' else 'rocm-smi'})"
                used_mb, total_mb = 0, 0
        if kind not in ("nvidia", "amd"):
            # Shared memory, or a GPU no tool reads: the models get the machine's budget, less what Ollama already holds.
            total_mb = int(float(gpu.get("model_budget_gb") or 0) * 1024) if kind != "none" else 0
            used_mb = int(sum(_to_gb(lm.get("size_vram") or lm.get("size") or 0) for lm in loaded) * 1024)
            util_pct, gpu_procs = 0, []
            # The budget is a share of the RAM, not a promise of it: never more than the system has available now
            # (other programs hold memory too; review). What Ollama holds is already outside what is available.
            available = _available_ram_mb()
            if available is not None and total_mb - used_mb > available:
                used_mb = max(0, total_mb - available)
        vram_learned = load_vram_cache()
        held_by_pid, stale_lock_pid = inspect_local_lock(LOCAL_LOCK, procs=procs, take_stale=True)

    reasons = []
    if memory_unread:
        reasons.append(memory_unread)

    # The machine's own blockers: its hooks module, if machine.json names one (none: the plain checks above and below).
    reasons.extend(_hooks().blockers("local_blockers", {"model": model, "processes": procs, "gpu_processes": gpu_procs,
                                                        "fake": loc_fk, "rules": rules},
                                     machine=load_machine(fake=fake)))

    used_gb = used_mb / 1024.0
    total_gb = total_mb / 1024.0
    free_gb = max(0.0, (total_mb - used_mb) / 1024.0)
    already_loaded_gb = 0.0
    if model:
        for lm in loaded:
            lname = lm.get("name") or ""
            if lname == model or lname.startswith(model + ":"):
                already_loaded_gb = _to_gb(lm.get("size_vram") or 0)
    req_gb = needed_vram_gb(model, vram_learned=vram_learned, file_sizes=tags_sizes) if model and provider == "ollama" else 0.0
    if provider != "ollama":
        config = _providers().local_servers(fake).get(provider, {})
        size = config.get("size_gb")
        if isinstance(size, dict):
            size = size.get(model)
        # A static server loaded its one model when it started: its memory is already taken, not still needed.
        req_gb = float(size) if isinstance(size, (int, float)) and not config.get("static") else 0.0
        already_loaded_gb = 0.0
    avail_gb = free_gb + already_loaded_gb
    if model and avail_gb < req_gb:
        reasons.append(f"low VRAM for {model} ({free_gb:.1f} GB free, needs {req_gb:.1f} GB)")

    if held_by_pid is not None:
        reasons.append(f"local.lock held by pid {held_by_pid}")

    return {
        "free": len(reasons) == 0,
        "reasons": reasons,
        "reason_str": "; ".join(reasons) if reasons else "free",
        "used_mb": used_mb,
        "total_mb": total_mb,
        "used_gb": used_gb,
        "total_gb": total_gb,
        "free_gb": free_gb,
        "needed_gb": req_gb,
        "utilization": util_pct,
        "ollama_up": ollama_up,
        "loaded": loaded,
        "stale_lock_taken": stale_lock_pid,
        "lock_pid": held_by_pid,
    }


# --- Role and explicit-name routing ---

def role_candidates(raw_list):
    """A role's list flattened: ([candidate, ...], [group, ...]). An entry {"any": [a, b]} puts a and b in one group,
    equally fit for the role; within a group the quota that burns first goes first."""
    models, groups = [], []
    for gi, item in enumerate(raw_list or []):
        if isinstance(item, dict) and isinstance(item.get("any"), list):
            # The group's own fields ("effort": ">=high", ...) are defaults for each member; a member's own win.
            shared = {k: v for k, v in item.items() if k != "any"}
            members = [dict(shared, **m) if isinstance(m, dict) else m for m in item["any"]]
        else:
            members = [item]
        for m in members:
            models.append(m)
            groups.append(gi)
    return models, groups


def filter_role(raw_list, keep):
    """A role's list with only the candidates keep() accepts, its "any" groups kept as groups (an emptied group goes,
    a group of one is still a group). The group's fields count as the member's, as in role_candidates."""
    out = []
    for item in raw_list or []:
        if isinstance(item, dict) and isinstance(item.get("any"), list):
            shared = {k: v for k, v in item.items() if k != "any"}
            kept = [m for m in item["any"] if keep(dict(shared, **m) if isinstance(m, dict) else m)]
            if kept:
                out.append(dict(item, any=kept))
        elif keep(item):
            out.append(item)
    return out


# --- Roles as wishes ---
# A role row names a model by its id ({"provider", "model"}) or wishes for one: {"family": "gemini-4-argon*", "effort":
# ">=high"}, with an optional "provider". A wish is matched against the live inventory when the role is called, so a new
# version that fits the glob is taken without an edit, and a machine without the provider says so and goes on. After the
# match a role is an ordinary list of ids: any groups, pinned, local_only, never, experience, score, traits and capabilities
# all work on what matched.

WISH_KEYS = ("family", "effort", "provider")
_EFFORT_WISH = re.compile(r"^\s*(>=|<=|=)?\s*([a-z]+)\s*$")


def role_rows(entry):
    """A role's rows: "prefer" (wishes), else "models", else the old "names"; a bare list is its own rows."""
    if isinstance(entry, list):
        return entry
    if not isinstance(entry, dict):
        return []
    for key in ("prefer", "models", "names"):
        if key in entry:
            return entry.get(key) or []
    return []


def is_wish(row):
    return isinstance(row, dict) and bool(row.get("family")) and not row.get("model")


def role_has_wishes(entry):
    for row in role_rows(entry):
        if is_wish(row):
            return True
        if isinstance(row, dict) and isinstance(row.get("any"), list):
            shared = {k: v for k, v in row.items() if k != "any"}
            if any(is_wish(dict(shared, **m)) for m in row["any"] if isinstance(m, dict)):
                return True
    return False


def parse_effort_wish(value):
    """'>=high' -> ('>=', 'high'), 'high' -> ('=', 'high'), None or '' -> None; an effort the order does not know -> ValueError."""
    if value is None or str(value).strip() == "":
        return None
    m = _EFFORT_WISH.match(str(value).lower())
    order = _providers().EFFORT_ORDER
    if not m or m.group(2) not in order:
        raise ValueError(f"effort {value!r}: expected one of {', '.join(order)}, optionally after >= or <=")
    return (m.group(1) or "="), m.group(2)


def call_effort(value):
    """The effort a candidate row is called with: a plain effort name; a bound such as '>=high' is a wish, not an effort."""
    v = str(value or "").strip().lower()
    return v if v in _providers().EFFORT_ORDER else None


def effort_choice(want, efforts):
    """The effort a wish takes among a model's efforts: >= the weakest at or above the bound (the cheapest that meets it),
    <= the strongest at or below it, = exactly it. None when none fits."""
    pv = _providers()
    op, bound = want
    b = pv.effort_rank(bound)
    ranked = pv.sort_efforts(efforts or [])
    if op == ">=":
        return next((e for e in ranked if pv.effort_rank(e) >= b), None)
    if op == "<=":
        return next((e for e in reversed(ranked) if pv.effort_rank(e) <= b), None)
    return bound if bound in ranked else None


def live_inventory(fake=None, rules=None, agy_path=None, ollama_url=None):
    """What this machine offers now, for matching wishes: ([{"provider", "id", "family", "effort" | "efforts"}], {provider:
    why it is absent}). agy: one row per id, its effort from the id; Codex: its listed efforts (None while unknown, when its
    list could not be read); Ollama: no efforts; the subscription: the subagent's efforts."""
    rules = rules or load_roles()
    pv = _providers()
    absent = {}
    for prov in ("agy", "codex", "ollama", "claude-subscription"):
        det = provider_present(prov, fake=fake)
        if not det["present"]:
            absent[prov] = det.get("note") or "not found"
    rows = []
    if "agy" not in absent:
        ok, items = fetch_agy_models(agy_path=agy_path, fake=fake)
        ids = [it["id"] for it in items] if ok else [m for m, inf in model_table(rules).items() if inf["provider"] == "agy"]
        for mid in ids:
            fam, eff = pv.split_agy_id(mid)
            rows.append({"provider": "agy", "id": mid, "family": fam, "effort": eff})
    if "codex" not in absent:
        # Each account has its own list and efforts: a wish is matched per account, and its row
        # then names that account, so the call goes where the id and the effort were found.
        accs = provider_accounts("codex", fake=fake)
        for acc in accs:
            other = account_home(acc["id"], fake=fake) is not None
            ok, items = fetch_codex_models(fake=fake, account=acc["id"] if other else None)
            if not ok:
                items = [{"id": m} for (p, m) in role_usage(rules) if p == "codex"]
            for it in items:
                effs = [e.get("reasoningEffort") if isinstance(e, dict) else str(e)
                        for e in it.get("supportedReasoningEfforts") or []]
                rows.append(dict({"provider": "codex", "id": it["id"], "family": it["id"],
                                  "efforts": pv.sort_efforts(effs) or None},
                                 **({"account": acc["id"]} if len(accs) > 1 else {})))
    if "ollama" not in absent:
        ok, items = fetch_ollama_models(ollama_url=ollama_url, fake=fake, include_show=False)
        ids = [it["id"] for it in items] if ok else [m for m, inf in model_table(rules).items() if inf["provider"] == "ollama"]
        rows.extend({"provider": "ollama", "id": mid, "family": mid, "efforts": []} for mid in ids)
    if "claude-subscription" not in absent:
        rows.extend({"provider": "claude-subscription", "id": mid, "family": mid, "efforts": list(pv.CLAUDE_EFFORTS)}
                    for mid in rules.get("subscription_models") or DEFAULT_SUBSCRIPTION_MODELS)
    for name in pv.local_servers(fake):
        ok, items = fetch_openai_models(name, fake=fake)
        if ok:
            rows.extend({"provider": name, "id": it["id"], "family": it["id"], "efforts": []} for it in items)
        else:
            absent[name] = pv.get(name, fake=fake).detect()["note"]
    return rows, absent


def _version_key(text):
    return tuple(int(n) for n in re.findall(r"\d+", str(text)))


def match_wish(wish, inventory, absent):
    """The ids a wish matches, as role rows, and a note when it matched none. The glob is matched against the id (and, for
    agy, the family without its effort suffix), case aside. Of one family agy gives the id at the chosen effort (no wish:
    the strongest); Codex and the subscription carry the chosen effort in the row (no wish: the provider's default).
    Order: the newest version first (by the numbers in the id), then the shorter id, then the inventory's provider order."""
    pat = str(wish.get("family") or "").lower()
    prov = wish.get("provider")
    want = parse_effort_wish(wish.get("effort"))
    extra = {k: v for k, v in wish.items() if k not in WISH_KEYS}
    if prov and prov in absent:
        return [], f"{wish['family']} ({prov}): {prov} is not on this machine ({absent[prov]})"
    agy_families, found = {}, []
    for r in inventory:
        if prov and r["provider"] != prov:
            continue
        if r["provider"] == "agy":
            if fnmatch.fnmatchcase(r["family"].lower(), pat) or fnmatch.fnmatchcase(r["id"].lower(), pat):
                agy_families.setdefault(r["family"], []).append(r)
            continue
        if not fnmatch.fnmatchcase(r["id"].lower(), pat):
            continue
        efforts = r.get("efforts")
        eff = None
        if want is not None:
            eff = want[1] if efforts is None else effort_choice(want, efforts)
            if eff is None:
                continue
        found.append(dict(extra, provider=r["provider"], model=r["id"], wish=wish["family"], **({"effort": eff} if eff else {}),
                          **({"account": r["account"]} if r.get("account") else {})))
    pv = _providers()
    for fam, rs in agy_families.items():
        by_effort = {r["effort"]: r for r in rs}
        efforts = pv.sort_efforts(e for e in by_effort if e)
        if want is None:
            pick_eff = efforts[-1] if efforts else None
        else:
            pick_eff = effort_choice(want, efforts)
            if pick_eff is None:
                continue
        r = by_effort.get(pick_eff) or rs[0]
        found.append(dict(extra, provider="agy", model=r["id"], wish=wish["family"]))
    order = {p: i for i, p in enumerate(("agy", "codex", "ollama", "claude-subscription"))}
    found.sort(key=lambda row: order.get(row["provider"], 9))
    found.sort(key=lambda row: (_version_key(pv.split_agy_id(row["model"])[0] if row["provider"] == "agy" else row["model"]),
                                -len(row["model"])), reverse=True)
    if found:
        return found, None
    at = f" at effort {wish['effort']}" if wish.get("effort") else ""
    where = f" ({prov})" if prov else ""
    gone = "" if prov else "".join(f"; {p} is not on this machine" for p in absent if p != "claude-subscription")
    return [], f"{wish['family']}{where}: no model on this machine matches{at}{gone}"


def expand_wishes(rows, inventory, absent):
    """A role's rows with every wish replaced by the ids it matches: one id stays a plain row, several make an any group
    (equally fit, so the quota that burns first goes first), none leaves a note. Inside an any group the matches join the
    group. An id already given by an earlier row is not given again. -> (rows, notes)."""
    out, notes, seen = [], [], set()

    def fresh(row):
        if not isinstance(row, dict) or not row.get("model"):
            return True
        key = (row.get("provider"), row["model"], row.get("account"))
        if key in seen:
            return False
        seen.add(key)
        return True

    for item in rows or []:
        if isinstance(item, dict) and isinstance(item.get("any"), list):
            shared = {k: v for k, v in item.items() if k != "any"}
            members = []
            for m in item["any"]:
                mm = dict(shared, **m) if isinstance(m, dict) else m
                if is_wish(mm):
                    got, note = match_wish(mm, inventory, absent)
                    members.extend(r for r in got if fresh(r))
                    if note:
                        notes.append(note)
                elif fresh(mm):
                    # The group's fields are already merged in; a bound such as ">=high" is not an effort to call with.
                    members.append({k: v for k, v in mm.items() if k != "effort" or call_effort(v)}
                                   if isinstance(mm, dict) else mm)
            if members:
                out.append({"any": members, **{k: v for k, v in shared.items() if k not in WISH_KEYS}})
        elif is_wish(item):
            got, note = match_wish(item, inventory, absent)
            got = [r for r in got if fresh(r)]
            if note:
                notes.append(note)
            if len(got) == 1:
                out.append(got[0])
            elif got:
                out.append({"any": got})
        elif fresh(item):
            out.append(item)
    return out, notes


def with_wishes(rules, role, fake=None, inventory=None):
    """rules with the role's wishes matched to this machine's ids (the role's "prefer" becomes its "models"), and the notes of
    the wishes that matched nothing. A role without wishes comes back as it is."""
    entry = (rules.get("roles") or {}).get(role)
    if not role_has_wishes(entry):
        return rules, []
    inv, absent = inventory if inventory is not None else live_inventory(fake=fake, rules=rules)
    rows, notes = expand_wishes(role_rows(entry), inv, absent)
    new_entry = {k: v for k, v in entry.items() if k not in ("prefer", "names")} if isinstance(entry, dict) else {}
    new_entry["models"] = rows
    out = dict(rules, roles=dict(rules.get("roles") or {}, **{role: new_entry}))
    return out, notes


def wish_roles(provider, model_id, rules=None):
    """The roles whose wishes would take this id (for --list); agy's effort suffix is held to the wish's bound."""
    rules = rules or load_roles()
    pv = _providers()
    fam, eff = pv.split_agy_id(model_id) if provider == "agy" else (model_id, None)
    out = []
    for role, entry in (rules.get("roles") or {}).items():
        for row in role_candidates(role_rows(entry))[0]:
            if not is_wish(row) or (row.get("provider") and row["provider"] != provider):
                continue
            pat = str(row["family"]).lower()
            if not (fnmatch.fnmatchcase(model_id.lower(), pat) or fnmatch.fnmatchcase(fam.lower(), pat)):
                continue
            try:
                want = parse_effort_wish(row.get("effort"))
            except ValueError:
                continue
            if provider == "agy" and want is not None and (not eff or effort_choice(want, [eff]) is None):
                continue
            if role not in out:
                out.append(role)
    return out


def _frac(w, key, default):
    v = w.get(key)
    return default if v is None else float(v)


def _available(w):
    return max(0.0, _frac(w, "remaining_fraction", 1.0) - _frac(w, "reserved_fraction", 0.0))


def window_urgency(w):
    """What of a window burns per hour if left unused: (remaining - reserved) / hours to its reset. None when the
    reset is unknown, or due or past: such a window is stale data, not the most urgent one ."""
    rs = w.get("resets_in_s")
    if rs is None or float(rs) <= 0:
        return None
    return _available(w) / max(float(rs) / 3600.0, 1.0 / 60.0)


def burn_window(windows):
    """Of a pool's windows, the one whose leftover is lost at its reset: the weekly one, else the longest; the 5-hour
    one refills within the week, so its leftover is not what burns. Ties: the more urgent."""
    best = None
    for w in windows or []:
        u = window_urgency(w)
        if u is None:
            continue
        key = ("week" in str(w.get("window", "")).lower(), float(w.get("length_s") or 0), u)
        if best is None or key > best[0]:
            best = (key, w, u)
    return (best[1], best[2]) if best else (None, None)


def burning_pools(pools):
    """[(urgency, pool, window)] of the fresh pools, the most urgent first. A nested pool (Fable inside all models)
    is left out: its leftover is what the pool it sits in already counts, so it never shows above it."""
    out = []
    for p in pools or []:
        if p.get("stale") or p.get("nested_in"):
            continue
        w, u = burn_window(p.get("windows"))
        if w is not None:
            out.append((u, p, w))
    return sorted(out, key=lambda t: -t[0])


def expiry_of(provider, model_id, cs):
    """The burning window over every pool that covers the model (see burn_window). None for local models, stale
    pools (not included) or no reset known."""
    pairs = covering_pools_and_windows(provider, model_id, (cs or {}).get("pools", []), include_stale=False)
    best = None
    for p, w in pairs:
        bw, u = burn_window([w])
        if bw is None:
            continue
        key = ("week" in str(bw.get("window", "")).lower(), float(bw.get("length_s") or 0), u)
        if best is None or key > best[0]:
            best = (key, {"pool": p.get("label"), "window": bw.get("window"), "remaining": _available(bw),
                          "resets_in": bw.get("resets_in"), "urgency": u})
    return best[1] if best else None


def _order_by_expiry(evaluated, keys, cs):
    """Within each run of one (policy rank, group) the candidates that may go now and whose window is known
    (ok, not over the pace, fresh expiry) trade places among their own slots: the quota that burns first comes
    first. Every other candidate keeps its place (failed checks, pace_over, no data such as a stale pool or a
    local model), so the rule only orders what already passed.
    "why" marks the candidate only when it took the first such slot from another."""
    out, i = [], 0
    while i < len(evaluated):
        j = i
        while j < len(evaluated) and keys[j] == keys[i]:
            j += 1
        run = list(evaluated[i:j])
        if len(run) > 1:
            slots = []
            for k, e in enumerate(run):
                if e.get("ok") and not e.get("pace_over") and e.get("provider") in ("agy", "codex", "claude-subscription"):
                    e["expiry"] = expiry_of(e["provider"], e["model"], e.get("_cs") or cs)
                    if e["expiry"] is not None:
                        slots.append(k)
            if len(slots) > 1:
                members = [run[k] for k in slots]
                ranked = sorted(members, key=lambda e: -e["expiry"]["urgency"])  # stable: ties keep the role's order
                if ranked[0] is not members[0]:
                    ranked[0]["why"] = "expires_first"
                for k, e in zip(slots, ranked):
                    run[k] = e
        out.extend(run)
        i = j
    return out


def _role_spec(role_name, rules):
    roles = rules.get("roles", {})
    if role_name not in roles:
        return None
    entry = roles[role_name]
    if isinstance(entry, list):
        models, groups = role_candidates(entry)
        return {"models": models, "groups": groups, "needs": [], "local_only": False, "exit_code": 2}
    models, groups = role_candidates(role_rows(entry))
    return {
        "models": models,
        "groups": groups,
        "needs": list(entry.get("needs") or []),
        "local_only": bool(entry.get("local_only", False)),
        "fallback": entry.get("fallback") or entry.get("if_none"),
        "exit_code": int(entry.get("exit_code", 2)),
    }


KNOWN_NEEDS = ("python", "engine", "big-files")


def parse_needs(value):
    """Reject typos before a CLI can launch work on an unsuitable model."""
    import argparse
    needs = {part.strip() for part in value.split(",") if part.strip()}
    unknown = needs.difference(KNOWN_NEEDS)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown need: {', '.join(sorted(unknown))} (known needs: {', '.join(KNOWN_NEEDS)})")
    return sorted(needs)


def role_needs(role, needs, rules):
    spec = _role_spec(role, rules) or {}
    return sorted(set(spec.get("needs", [])) | set(needs or []))


def host_platform():
    """The platform the router judges for: the traits in roles.json (cannot) and the GPU guess. Tests patch this,
    never sys.platform: the standard library reads sys.platform too (Python 3.14's shutil.which calls _winapi when
    it says win32)."""
    return sys.platform


def cannot(provider, model_id, rules=None, platform=None):
    """The session's tools are available to its subagent; other limits add up."""
    if provider == "claude-subscription":
        return set()
    rules = load_roles() if rules is None else rules
    platform = host_platform() if platform is None else platform
    traits = (rules.get("traits") or {}).get("cannot") or {}
    result = set()
    # "local" holds for every local provider (Ollama, a local server), beside the provider's own name and the model's.
    for key in (("local",) if is_local(provider) else ()) + (provider, model_id):
        limits = traits.get(key) or {}
        result.update(limits.get("*", []))
        result.update(limits.get(platform, []))
    return result


def lacking(provider, model_id, needs, rules, platform=None):
    return sorted(cannot(provider, model_id, rules=rules, platform=platform) & set(needs or []))


def is_never(provider_or_name, model_id=None, rules=None):
    rules = rules or load_roles()
    reason = rules.get("never_reason", "model is in roles.json never list")
    if model_id is None:
        prov, mid = resolve_model(provider_or_name, rules=rules)
        if not mid:
            prov, mid = "agy", str(provider_or_name or "")
    else:
        prov, mid = provider_or_name or "agy", str(model_id or "")

    mid_low = mid.lower()
    prov_low = (prov or "").lower()
    for item in rules.get("never", []):
        if isinstance(item, dict):
            iprov = (item.get("provider") or "").lower()
            ipat = (item.get("model") or "").lower()
            if iprov and prov_low and iprov != prov_low:
                continue
            if ipat and fnmatch.fnmatchcase(mid_low, ipat):
                return True, reason
        elif isinstance(item, str):
            ipat = item.lower()
            if fnmatch.fnmatchcase(mid_low, ipat) or ipat == str(provider_or_name or "").lower():
                return True, reason
    return False, ""


def check_name(spec, fake=None, rules=None, allow_never=False, provider=None, cs=None,
               role=None, tokens=None, force=False, journal_path=None, reservations_path=None,
               agy_path=None, ollama_url=None, context=None, machine=None, needs=None, account=None):
    """Validate a model id or {'provider', 'model'}:
    checks never-list, offered-by-provider, quota floor and estimated run share after reservations, and weekly pace.
    account: the account to check it on; None: the one the policy ranks first among the provider's accounts.
    """
    rules = rules or load_roles()
    raw = spec.get("model") or spec.get("id") or "" if isinstance(spec, dict) else spec
    raw = str(raw or "").strip()
    explicit_provider = (spec.get("provider") if isinstance(spec, dict) else None) or provider
    reason = None
    status = "error"
    if explicit_provider:
        prov, model_id = resolve_model(raw, provider=explicit_provider, rules=rules)
    else:
        providers = offered_providers(raw, fake=fake, rules=rules, agy_path=agy_path, ollama_url=ollama_url)
        if len(providers) > 1:
            flags = " / ".join(f"--{pv} {raw}" for pv in providers if pv != "claude-subscription")
            reason = f"{raw} is offered by {', '.join(providers)}: choose {flags}"
            status = "ambiguous"
        elif providers:
            prov, model_id = providers[0], raw
        else:
            # (No live list knows it, or none could be read: the roles and the subscription's list still name its provider; whether
            # it is offered now is checked just below.)
            prov, model_id = resolve_model(raw, rules=rules)
            if not prov:
                reason = f"no model is called {raw}: --list"
    if reason or not raw:
        return {
            "ok": False,
            "name": raw,
            "model": None,
            "label": raw,
            "provider": None,
            "status": status,
            "exit_code": 2,
            "reason": reason or f"no model is called {raw}: --list",
        }

    # The policy comes before anything asks the provider: a refused account is not even asked for its usage.
    if account:
        pol = policy_for(prov, context=context, machine=machine, fake=fake, account=account)
    else:
        pol = choose_account(prov, context=context, machine=machine, fake=fake)
    pol_fields = {"context": pol["context"], "account": pol["account"], "account_label": pol["label"],
                  "crossed": pol["crossed"], "rank": pol["rank"]}
    if not pol["allowed"] and prov is not None:
        return dict(pol_fields, ok=False, name=model_id, model=model_id, label=model_id, provider=prov,
                    status="policy", exit_code=EXIT_POLICY, reason=f"{model_id}: refused by policy: {pol['reason']}")
    # Traits precede quota and GPU checks: an incapable candidate must reserve nothing.
    missing = lacking(prov, model_id, role_needs(role, needs, rules), rules)
    if missing:
        return dict(pol_fields, ok=False, name=model_id, model=model_id, label=model_id, provider=prov,
                    status="cannot", exit_code=2,
                    reason=f"{model_id} cannot {', '.join(missing)} here (traits in roles.json)")
    # Not the active account: its label says which, so two accounts of one model read apart in passed-over lines and headers.
    label = f"{model_id} ({pol['account']})" if account_home(pol["account"], fake=fake) else None
    res = _check_allowed(prov, model_id, fake=fake, rules=rules, allow_never=allow_never, cs=cs, role=role, tokens=tokens,
                         force=force, journal_path=journal_path, reservations_path=reservations_path,
                         agy_path=agy_path, ollama_url=ollama_url, account=pol["account"], label=label)
    res.update(pol_fields)
    return res


def _check_allowed(prov, model_id, fake=None, rules=None, allow_never=False, cs=None, role=None, tokens=None, force=False,
                   journal_path=None, reservations_path=None, agy_path=None, ollama_url=None, account=None, label=None):
    """The checks of check_name once the policy allows the account: never-list, offered, quota and pace, GPU. A Codex account
    other than the active one is checked on its own list and its own pool."""
    if prov == "codex" and account_home(account, fake=fake) and (cs or {}).get("account") != account:
        cs = cloud_state(fake=fake, rules=rules, reservations_path=reservations_path, account=account)
    label = label or model_id
    never_hit, never_why = is_never(prov, model_id, rules=rules)
    if never_hit and not allow_never:
        return {
            "ok": False,
            "name": model_id,
            "model": model_id,
            "label": label,
            "provider": prov,
            "status": "never",
            "exit_code": 2,
            "reason": f"{label} refused: {never_why} (pass --allow-never to override)",
        }

    # (Not on this machine: null in machine.json "paths", or nowhere to be found.)
    det = provider_present(prov, fake=fake)
    if not det["present"]:
        return {"ok": False, "name": model_id, "model": model_id, "label": label, "provider": prov,
                "status": "not_offered", "exit_code": 2, "reason": f"{label}: {prov} is not on this machine ({det['note']})"}

    # Check if the provider currently offers this model (when the provider's list is readable)
    if not is_model_offered(prov, model_id, fake=fake, rules=rules, agy_path=agy_path, ollama_url=ollama_url, account=account):
        return {
            "ok": False,
            "name": model_id,
            "model": model_id,
            "label": label,
            "provider": prov,
            "status": "not_offered",
            "exit_code": 2,
            "reason": f"{label}: not offered by {prov}",
        }

    floors = rules.get("floors", {"weekly": 0.10, "5h": 0.10})
    pace_skip = float(rules.get("pace_skip", 2.0))
    est_tok = estimate_tokens(role=role, override_tokens=tokens, rules=rules, journal_path=journal_path,
                              fake=fake, provider=prov, model=model_id)

    if prov in ("agy", "codex", "claude-subscription"):
        cs = cs or cloud_state(fake=fake, rules=rules, reservations_path=reservations_path)
        pairs = covering_pools_and_windows(prov, model_id, cs.get("pools", []), include_stale=False)
        pool_names = []
        for p, _ in pairs:
            if p["label"] not in pool_names:
                pool_names.append(p["label"])
        pool_desc = ", ".join(pool_names) if pool_names else ("Claude subscription" if prov == "claude-subscription" else prov)

        # A server can report the limit reached even without windows.
        reached = any(p.get("provider") == prov and p.get("quota_reached") and
                      any(fnmatch.fnmatchcase(model_id.lower(), pat.lower()) for pat in p.get("covers", []))
                      for p in cs.get("pools", []))
        if reached and not force:
            return {"ok": False, "name": model_id, "model": model_id, "label": label,
                    "provider": prov, "pool": pool_desc, "status": "quota", "exit_code": 2,
                    "reason": f"{label}: {prov} usage limit reached"}

        forced_warning = None
        for p, w in pairs:
            wtype = w.get("window", "weekly")
            floor_val = float(floors.get(wtype, floors.get("weekly", 0.10)))
            rem_raw = float(w.get("remaining_fraction", 1.0))
            res_frac = float(w.get("reserved_fraction", 0.0))
            avail_rem = max(0.0, rem_raw - res_frac)
            above_floor = avail_rem - floor_val

            # Both CLI providers reserve estimated pool share.
            need_share = 0.0
            if prov in ("agy", "codex"):
                rate = pool_window_rate(p.get("label"), wtype, rules=rules, journal_path=journal_path, fake=fake)
                need_share = (float(est_tok) / 1_000_000.0) * rate

            if avail_rem < floor_val or (avail_rem - need_share) < floor_val - 1e-9:
                sp = short_pool_name(p.get("label"))
                win_word = "week" if "week" in wtype.lower() else (w.get("label") or wtype)
                if need_share > 0 and rem_raw >= floor_val:
                    reason_line = f"{label}: needs ~{need_share * 100:.1f}% of the {sp} {win_word}, {max(0.0, above_floor) * 100:.1f}% above the floor"
                else:
                    wname = w.get("label") or wtype
                    reason_line = f"{label}: {p['label']} {wname} quota {avail_rem * 100:.1f}% < {floor_val * 100:.0f}% floor"
                if force:
                    forced_warning = f"warning (forced): {reason_line}"
                else:
                    return {
                        "ok": False,
                        "name": model_id,
                        "model": model_id,
                        "label": label,
                        "provider": prov,
                        "pool": pool_desc,
                        "status": "quota",
                        "exit_code": 2,
                        "estimated_tokens": est_tok,
                        "needed_share": need_share,
                        "reason": reason_line,
                    }

        pace_over = False
        pace_reason = None
        pace_warning = forced_warning
        for p, w in pairs:
            if "week" in str(w.get("window", "")).lower():
                wpace = float(w.get("pace") or 0.0)
                if wpace > pace_skip:
                    pace_over = True
                    sp = short_pool_name(p.get("label"))
                    pace_reason = f"{label}: {sp} pool x{wpace:.1f} faster than the week"
                    if not pace_warning:
                        pace_warning = f"warning: {pace_reason}"
                    break

        return {
            "ok": True,
            "name": model_id,
            "model": model_id,
            "label": label,
            "provider": prov,
            "pool": pool_desc,
            "status": "ok",
            "exit_code": 0,
            "estimated_tokens": est_tok,
            "reason": "ok",
            "pace_over": pace_over,
            "pace_reason": pace_reason,
            "warning": pace_warning,
        }

    # Local Ollama model
    loc = local_state(model=model_id, fake=fake, rules=rules, provider=prov)
    if not loc["free"] and not force:
        return {
            "ok": False,
            "name": model_id,
            "model": model_id,
            "label": label,
            "provider": prov,
            "pool": prov + ", local",
            "status": "busy",
            "exit_code": 3,
            "estimated_tokens": est_tok,
            "reason": f"{label}: GPU busy ({loc['reason_str']})",
            "reasons": loc["reasons"],
        }
    return {
        "ok": True,
        "name": model_id,
        "model": model_id,
        "label": label,
        "provider": prov,
        "pool": prov + ", local",
        "status": "ok",
        "exit_code": 0,
        "estimated_tokens": est_tok,
        "reason": "ok",
        "pace_over": False,
        "pace_reason": None,
        "warning": f"warning (forced): {label}: GPU busy ({loc['reason_str']})" if (not loc["free"] and force) else None,
        "stale_lock_taken": loc.get("stale_lock_taken"),
    }


def _no_pick(role):
    return {"role": role, "name": None, "picked": None, "model": None, "label": None, "provider": None, "pool": None,
            "passed_over": [], "warning": None, "wait": False, "fallback": False, "local_only": False, "experience": []}


def _pick(role, fake=None, rules=None, exclude=None, allow_never=False, tokens=None,
          journal_path=None, reservations_path=None, context=None, machine=None, needs=None, prompt_tokens=None, effort=None):
    """Pick the first candidate of the role that fits in its pool (after reservations) and passes GPU and pace_skip.
    The policy goes first: it drops the accounts the context may not use and, in work, orders the rest by group
    (work → local → personal with work_ok); the role's order holds within a group."""
    rules = rules or load_roles()
    spec = _role_spec(role, rules)
    if spec is None:
        return {
            "role": role,
            "name": None,
            "picked": None,
            "model": None,
            "label": None,
            "provider": None,
            "pool": None,
            "passed_over": [],
            "warning": None,
            "wait": False,
            "fallback": False,
            "local_only": False,
            "exit_code": 2,
            "message": f"unknown role {role!r} (roles: {', '.join(rules.get('roles', {}).keys())})",
            "experience": [],
        }

    # Wishes first: what the role wishes for becomes this machine's ids, and every rule below works on those.
    unmatched = []
    try:
        rules, unmatched = with_wishes(rules, role, fake=fake)
    except ValueError as e:
        return dict(_no_pick(role), exit_code=2, message=f"role {role!r}: {e} (roles.json)")
    spec = _role_spec(role, rules)

    experience_notes, experience_moved = [], set()
    try:
        # Reorder raw rows before expanding any-groups or applying policy.
        import experience
        entry = rules["roles"][role]
        raw = role_rows(entry)
        ordered, notes = experience.reorder(
            role, raw, rules, _read_journal_rows(journal_path=journal_path, fake=fake),
            experience.load_benchmarks(), tokens=tokens)
        # Only a row that came up owes its pick to experience: one moved down and picked anyway (the rows above it
        # passed over) was picked despite it.
        moved = {resolve_model(row, rules=rules) for i, row in enumerate(ordered)
                 if next(j for j, r in enumerate(raw) if r is row) > i
                 and not (isinstance(row, dict) and "any" in row)}
        models, groups = role_candidates(ordered)
        spec = dict(spec, models=models, groups=groups)
        experience_notes, experience_moved = notes, moved
    except Exception:
        # Experience is advisory: broken optional data must not stop a call.
        pass

    excluded = {str(x).lower() for x in (exclude or [])}
    local_only = spec["local_only"]
    cinfo = _context_dict(context)
    ctx = cinfo["context"]
    if machine is None:
        machine = load_machine(fake=fake)

    # Each candidate goes on every account of its provider the policy allows, in the policy's order (Codex: ~/.codex-work in a
    # work context before a personal ~/.codex with work_ok), so a call goes where the policy says, not blindly to CODEX_HOME.
    candidates, refused, accounts = [], [], {}
    groups = spec.get("groups") or list(range(len(spec["models"])))
    for cand, group in zip(spec["models"], groups):
        prov, mid = resolve_model(cand, rules=rules)
        if not mid:
            continue
        if mid.lower() in excluded or f"{prov}:{mid}".lower() in excluded:
            continue
        if prov not in accounts:
            accounts[prov] = account_policies(prov, context=cinfo, machine=machine, fake=fake)
        allowed = [p for p in accounts[prov] if p["allowed"]]
        only = cand.get("account") if isinstance(cand, dict) else None
        if only:
            # A wish matched on that account's own list and efforts: it goes there or nowhere.
            pols = [p for p in accounts[prov] if p["account"] == only]
            allowed = [p for p in pols if p["allowed"]]
            if pols and not allowed:
                refused.append(f"{mid} ({only}): refused by policy: {pols[0]['reason']}")
                continue
        eff = call_effort(cand.get("effort")) if isinstance(cand, dict) else None
        for pol in allowed:
            if f"{pol['account']}/{mid}".lower() in excluded:
                continue
            candidates.append((pol["rank"], group, len(candidates), prov, mid, pol["account"], eff))
        if not allowed:
            refused.append(f"{mid}: refused by policy: {accounts[prov][0]['reason']}")
    candidates.sort(key=lambda c: c[:3])
    if refused and not candidates:
        return {
            "role": role, "name": None, "picked": None, "model": None, "label": None, "provider": None, "pool": None,
            "passed_over": unmatched + refused, "warning": None, "wait": False, "fallback": False, "local_only": local_only,
            "context": ctx, "exit_code": EXIT_POLICY,
            "experience": experience_notes,
            "message": f"role {role!r}: no model is allowed in the {ctx} context",
        }

    cs = cloud_state(fake=fake, rules=rules, reservations_path=reservations_path)
    account_cs = {}

    def cs_of(prov, acc):
        # Another Codex account's own pool, read once per pick: its checks, its expiry and its score use it.
        if prov != "codex" or account_home(acc, fake=fake) is None:
            return cs
        if acc not in account_cs:
            account_cs[acc] = cloud_state(fake=fake, rules=rules, reservations_path=reservations_path, account=acc)
        return account_cs[acc]

    evaluated, keys = [], []
    for rank, group, _, prov, mid, acc, eff in candidates:
        keys.append((rank, group))
        if local_only and not is_local(prov, fake=fake):
            evaluated.append({
                "ok": False,
                "model": mid,
                "label": mid,
                "provider": prov,
                "reason": f"{mid}: skipped (role {role!r} is local_only)",
            })
            continue
        chk = check_name(
            {"provider": prov, "model": mid},
            fake=fake,
            rules=rules,
            allow_never=allow_never,
            cs=cs_of(prov, acc),
            role=role,
            tokens=tokens,
            journal_path=journal_path,
            reservations_path=reservations_path,
            context=cinfo,
            machine=machine,
            needs=needs,
            account=acc,
        )
        if eff:
            chk["effort"] = eff
        if cs_of(prov, acc) is not cs:
            chk["_cs"] = cs_of(prov, acc)
        evaluated.append(chk)
    score_fields = {"score_notes": [], "score_pick": None}
    scored = None
    try:
        import score
        cfg = score.config(role, rules)
        entry = rules["roles"][role]
        if cfg.get("mode") in ("live", "shadow") and not local_only and not entry.get("pinned"):
            rows = _read_journal_rows(journal_path=journal_path, fake=fake)
            bench = experience.load_benchmarks()
            raw = entry.get("models", entry.get("names", []))
            owner = [resolve_model(m, rules=rules) for m in role_candidates(raw)[0]]
            scored, filtered, notes = [], [], []
            # Policy rank wins over score; subscriptions remain the last resort.
            for policy_rank in sorted(set(k[0] for k in keys)):
                members = [dict(e, **({"effort": effort} if effort else {}))
                           for e, key in zip(evaluated, keys) if key[0] == policy_rank]
                members.sort(key=lambda e: owner.index((e["provider"], e["model"]))
                             if (e["provider"], e["model"]) in owner else len(owner))
                ranked, dropped, parts = score.rank(role, members, rules, rows, bench, cs, prompt_tokens)
                scored.extend(ranked)
                filtered.extend(dropped)
                notes.extend(parts)
            calm = [e for e in scored if not e.get("pace_over")]
            proposed = (calm or scored or [None])[0]
            score_fields = {"score_notes": notes, "score_pick": proposed["model"] if proposed else None,
                            "score_mode": cfg["mode"],
                            "score_table": [e["score"] | {"model": e["model"], "filtered": not e["ok"]}
                                            for e in scored + filtered]}
            if cfg["mode"] == "live":
                first_ok = next((e for e in sorted(evaluated, key=lambda e: owner.index((e["provider"], e["model"]))
                                if (e["provider"], e["model"]) in owner else len(owner))
                                if e["ok"] and e["provider"] != "claude-subscription"), None)
                if proposed and first_ok and (proposed["provider"], proposed["model"]) != (first_ok["provider"], first_ok["model"]):
                    proposed["why"] = "score"
                scored += [e for e in evaluated if not e["ok"] or e["provider"] == "claude-subscription"]
                scored += filtered
    except Exception:
        # Scoring is advisory; restore the entire legacy order on any failure.
        scored = None
        score_fields = {"score_notes": [], "score_pick": None}
    try:
        entry = rules["roles"][role]
        if isinstance(entry, dict) and "score" in entry and not local_only and not entry.get("pinned"):
            import score
            rows = _read_journal_rows(journal_path=journal_path, fake=fake)
            bench = experience.load_benchmarks()
            picks = {}
            for name, weights in (rules.get("presets") or {}).items():
                if name == "manual":
                    continue
                ranked_all = []
                for policy_rank in sorted(set(k[0] for k in keys)):
                    members = [dict(e, **({"effort": effort} if effort else {}))
                               for e, key in zip(evaluated, keys) if key[0] == policy_rank]
                    ranked, _, _ = score.rank(role, members, rules, rows, bench, cs, prompt_tokens, weights=weights)
                    ranked_all.extend(ranked)
                calm = [e for e in ranked_all if not e.get("pace_over")]
                proposed = (calm or ranked_all or [None])[0]
                picks[name] = proposed["model"] if proposed else None
            score_fields["preset_picks"] = picks
    except Exception:
        pass  # Preset suggestions cannot affect the manual decision.
    if scored is not None and score_fields.get("score_mode") == "live":
        evaluated = scored
        experience_notes, experience_moved = [], set()
    else:
        evaluated = _order_by_expiry(evaluated, keys, cs)

    callable_ok = [e for e in evaluated if e["ok"] and e["provider"] != "claude-subscription"]
    has_calm_later = {}
    for idx, e in enumerate(evaluated):
        calm_after = any(
            later["ok"] and not later.get("pace_over") and later["provider"] != "claude-subscription"
            for later in evaluated[idx + 1:]
        )
        has_calm_later[idx] = calm_after

    passed_over = list(unmatched) + list(refused)
    if scored is not None and score_fields.get("score_mode") == "live":
        passed_over.extend(e["reason"] for e in evaluated if not e["ok"])
    for idx, e in enumerate(evaluated):
        if not e["ok"]:
            if e["reason"] not in passed_over:
                passed_over.append(e["reason"])
            continue
        if e["provider"] == "claude-subscription":
            if callable_ok:
                continue
            msg = f"use a Claude subagent ({e['model']})"
            return {
                "role": role,
                "name": e["model"],
                "picked": None,
                "fallback": True,
                "fallback_model": e["model"],
                "model": e["model"],
                "label": e["label"],
                "provider": "claude-subscription",
                "pool": e.get("pool"),
                "passed_over": passed_over,
                "warning": e.get("warning"),
                "wait": False,
                "local_only": local_only,
                "exit_code": 6,
                "estimated_tokens": e.get("estimated_tokens"),
                "effort": e.get("effort"),
                "context": e.get("context"), "account": e.get("account"), "account_label": e.get("account_label"),
                "crossed": bool(e.get("crossed")),
                "message": msg,
                "experience": experience_notes, **score_fields,
            }
        if e.get("pace_over") and has_calm_later.get(idx, False):
            passed_over.append(e["pace_reason"])
            continue
        return {
            "role": role,
            "name": e["model"],
            "picked": e["model"],
            "fallback": False,
            "model": e["model"],
            "label": e["label"],
            "provider": e["provider"],
            "pool": e.get("pool"),
            "passed_over": passed_over,
            "warning": e.get("warning") if e.get("pace_over") else None,
            "wait": False,
            "local_only": local_only,
            "exit_code": 0,
            "estimated_tokens": e.get("estimated_tokens"),
            "effort": e.get("effort"),
            "message": "ok",
            "context": e.get("context"), "account": e.get("account"), "account_label": e.get("account_label"),
            "crossed": bool(e.get("crossed")),
            "stale_lock_taken": e.get("stale_lock_taken"),
            "why": (e.get("why") if e.get("why") == "expires_first" else
                    "experience" if (e["provider"], e["model"]) in experience_moved else e.get("why")),
            "expiry": e.get("expiry"), "experience": experience_notes, **score_fields,
        }

    if local_only:
        first_cand = spec["models"][0] if spec["models"] else None
        first_prov, first_mid = resolve_model(first_cand, rules=rules) if first_cand else (None, None)
        # Only the role's own local providers count: another one on the machine will never serve this role.
        role_locals = {resolve_model(c, rules=rules)[0] for c in spec["models"]} & set(local_providers(fake=fake))
        locals_here = [p for p in role_locals if provider_present(p, fake=fake)["present"]]
        det = provider_present(first_prov if first_prov and is_local(first_prov, fake=fake) else "ollama", fake=fake)
        if not locals_here:
            # No local provider on this machine: no GPU will ever free, so say so now instead of waiting up to
            # --max-wait (2 h) under "GPU still busy" (review of main).
            return {
                "role": role, "name": None, "picked": None, "fallback": False, "model": first_mid, "label": first_mid,
                "provider": first_prov or "ollama", "pool": "local", "passed_over": passed_over, "warning": None,
                "wait": False, "local_only": True, "context": ctx, "exit_code": EXIT_UNSUPPORTED,
                "experience": experience_notes, **score_fields,
                "message": (f"role {role!r} is local_only and this machine has no local provider "
                            f"({det.get('note') or 'none found'}): it cannot run here"),
            }
        loc = local_state(model=first_mid, fake=fake, rules=rules, provider=first_prov) if first_mid else {}
        if loc.get("total_mb") == 0 and (first_prov == "ollama" or loc.get("needed_gb")):
            # Unified memory with no budget set: no local model will ever fit, so say so now rather than wait --max-wait.
            return {
                "role": role, "name": None, "picked": None, "fallback": False, "model": first_mid, "label": first_mid,
                "provider": first_prov or "ollama", "pool": "local", "passed_over": passed_over, "warning": None,
                "wait": False, "local_only": True, "context": ctx, "exit_code": EXIT_UNSUPPORTED,
                "experience": experience_notes, **score_fields,
                "message": (f"role {role!r} is local_only and this machine gives local models no memory "
                            f"(hardware.gpu.model_budget_gb in ~/.config/ask/machine.json is unset): it cannot run here"),
            }
        return {
            "role": role,
            "name": None,
            "picked": None,
            "fallback": False,
            "model": first_mid,
            "label": first_mid,
            "provider": first_prov or "ollama",
            "pool": "local",
            "passed_over": passed_over,
            "warning": None,
            "wait": True,
            "wait_for": first_mid,
            "experience": experience_notes, **score_fields,
            "local_only": True,
            "exit_code": 3,
            "message": f"local-only role {role!r} waiting for GPU ({'; '.join(passed_over)})",
        }

    fallback_msg = spec.get("fallback")
    exit_code = spec.get("exit_code", 2)
    msg = fallback_msg if fallback_msg else f"no model available for role {role!r}"
    return {
        "role": role,
        "name": None,
        "picked": None,
        "fallback": False,
        "model": None,
        "label": None,
        "provider": None,
        "pool": None,
        "passed_over": passed_over,
        "warning": None,
        "wait": False,
        "local_only": False,
        "exit_code": exit_code,
        "message": msg,
        "experience": experience_notes, **score_fields,
    }


def pick(role, fake=None, rules=None, exclude=None, allow_never=False, tokens=None,
         journal_path=None, reservations_path=None, context=None, machine=None, needs=None,
         prompt_tokens=None, effort=None, preset=None):
    rules = rules or load_roles()
    try:
        active = resolve_preset(preset, rules, machine=machine if machine is not None else load_machine(fake=fake))
    except ValueError:
        # The scripts refuse a wrong name before they pick; a direct caller still gets a decision.
        active = {"preset": "manual", "source": "default"}
    decision = _pick(role, fake=fake, rules=rules, exclude=exclude, allow_never=allow_never, tokens=tokens,
                     journal_path=journal_path, reservations_path=reservations_path, context=context,
                     machine=machine, needs=needs, prompt_tokens=prompt_tokens, effort=effort)
    decision["preset"] = active["preset"]
    decision.setdefault("score_notes", [])
    decision.setdefault("score_pick", None)
    return decision


def wait_until_free(role=None, name=None, max_wait_s=7200, poll_s=30, fake=None, rules=None, stream=None, sleep_fn=None, tokens=None, context=None, needs=None,
                    exclude=None, allow_never=False, prompt_tokens=None, effort=None, preset=None):
    out_stream = stream if stream is not None else sys.stderr
    sleeper = sleep_fn or time.sleep
    started = time.time()
    last_reason = None
    color = use_color(out_stream)

    while True:
        if role:
            res = pick(role, fake=fake, rules=rules, tokens=tokens, context=context, needs=needs, exclude=exclude,
                       allow_never=allow_never, prompt_tokens=prompt_tokens, effort=effort, preset=preset)
            if res["picked"] or res.get("exit_code") in (EXIT_UNSUPPORTED, EXIT_POLICY):  # waiting cannot change these
                res["waited_s"] = int(round(time.time() - started))
                return res
            reason = "; ".join(res["passed_over"]) or res["message"]
        else:
            res = check_name(name, fake=fake, rules=rules, tokens=tokens, context=context, needs=needs)
            if res["ok"] or res.get("status") == "cannot":
                res["waited_s"] = int(round(time.time() - started))
                return res
            reason = res["reason"]

        elapsed = time.time() - started
        if reason != last_reason:
            line = f"[{time.strftime('%H:%M:%S')}] waiting for GPU: {reason}"
            print(yellow(line, color), file=out_stream, flush=True)
            last_reason = reason

        if elapsed >= max_wait_s:
            return {
                "ok": False,
                "picked": None,
                "name": name,
                "role": role,
                "exit_code": 3,
                "status": "busy",
                "waited_s": int(round(elapsed)),
                "reason": f"GPU still busy after {int(round(elapsed))} s: {reason}",
                "passed_over": [reason],
            }
        sleeper(min(poll_s, max(0.1, max_wait_s - elapsed)))


# --- Raw call logs and JSONL journal ---

def save_raw_log(name, raw_text):
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_name = re.sub(r"[^a-zA-Z0-9_\-\u0400-\u04FF]", "_", str(name or "call"))
    path = os.path.join(LOG_DIR, f"{stamp}-{safe_name}.log")
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(raw_text or "")
    return path


def load_agy_conversations(path=None):
    p = path or AGY_CONVERSATIONS_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_agy_conversations(data, path=None):
    _atomic_write_json(path or AGY_CONVERSATIONS_PATH, data)  # a reader never sees half a file


def record_conversation_usage(conv_id, raw_tokens, carried_on=False, conversations_path=None):
    """Track running conversation totals, including cache since.
    For a carried-on conversation, returns (diff_tokens, running_total_tokens, conv_id).
    For a new conversation, stores its totals and returns (run_tokens, None, None).
    The read-modify-write of agy_conversations.json runs under its lock file: two conversations ending at once
    cannot lose each other's running total."""
    lock = (conversations_path or AGY_CONVERSATIONS_PATH) + ".lock"
    locked = _acquire_file_lock(lock, timeout_s=10.0)
    try:
        if not locked:
            # Never a write beside another's: this run's share is worked out but the running total is not stored.
            print(f"[conversations: lock {os.path.basename(lock)} not acquired in time; the running total is not updated]",
                  file=sys.stderr)
        return _record_conversation_usage(conv_id, raw_tokens, carried_on, conversations_path, save=locked)
    finally:
        if locked:
            _release_file_lock(lock)


def _record_conversation_usage(conv_id, raw_tokens, carried_on=False, conversations_path=None, save=True):
    u = raw_tokens or {}
    c_in = u.get("in") if "in" in u else u.get("input_tokens")
    c_out = u.get("out") if "out" in u else u.get("output_tokens")
    c_think = u.get("thinking") if "thinking" in u else u.get("thinking_tokens")
    c_tot = u.get("total") if "total" in u else u.get("total_tokens")
    c_cached = u.get("cached_in") if "cached_in" in u else u.get("cache_read_tokens")
    if c_tot is None and (c_in is not None or c_out is not None):
        c_tot = (c_in or 0) + (c_out or 0)

    convs = load_agy_conversations(conversations_path)
    cid = str(conv_id or "").strip()

    if carried_on and cid and cid in convs:
        prev = convs[cid]
        p_in = prev.get("in") or 0
        p_out = prev.get("out") or 0
        p_think = prev.get("thinking") or 0
        p_tot = prev.get("total") or 0
        p_cached = prev.get("cached") or 0
        if c_tot is not None and c_tot >= p_tot:
            d_in = max(0, (c_in or 0) - p_in) if c_in is not None else None
            d_out = max(0, (c_out or 0) - p_out) if c_out is not None else None
            d_tot = max(0, (c_tot or 0) - p_tot)
            d_cached = max(0, c_cached - p_cached) if c_cached is not None else None
            tot_record = {
                "in": int(c_in or 0),
                "out": int(c_out or 0),
                "thinking": int(c_think if c_think is not None else p_think),
                "total": int(c_tot or 0),
                "cached": int(c_cached if c_cached is not None else p_cached),
            }
        else:
            d_in, d_out, d_tot = c_in, c_out, c_tot
            d_cached = c_cached
            tot_record = {
                "in": int(p_in + (c_in or 0)),
                "out": int(p_out + (c_out or 0)),
                "thinking": int(p_think + (c_think or 0)),
                "total": int(p_tot + (c_tot or 0)),
                "cached": int(p_cached + (c_cached or 0)),
            }
        if c_cached is None and "cached" not in prev:
            tot_record.pop("cached", None)
        convs[cid] = tot_record
        if save:
            save_agy_conversations(convs, conversations_path)
        conv_tokens = {"in": tot_record["in"], "out": tot_record["out"], "total": tot_record["total"]}
        run_tokens = {"in": d_in, "out": d_out, "total": d_tot}
        if c_cached is not None:
            run_tokens["cached_in"] = d_cached
            conv_tokens["cached_in"] = tot_record["cached"]
        return run_tokens, conv_tokens, cid

    run_tokens = {"in": c_in, "out": c_out, "total": c_tot}
    if c_cached is not None:
        run_tokens["cached_in"] = c_cached
    if cid and c_tot is not None:
        tot_record = {
            "in": int(c_in or 0),
            "out": int(c_out or 0),
            "thinking": int(c_think or 0),
            "total": int(c_tot or 0),
            "cached": int(c_cached or 0),
        }
        if c_cached is None:
            tot_record.pop("cached", None)
        convs[cid] = tot_record
        if save:
            save_agy_conversations(convs, conversations_path)
        if carried_on:
            conv_tokens = {"in": tot_record["in"], "out": tot_record["out"], "total": tot_record["total"]}
            if c_cached is not None:
                conv_tokens["cached_in"] = tot_record["cached"]
            return run_tokens, conv_tokens, cid

    return run_tokens, None, (cid if carried_on and cid else None)


def journal(script, role, name, provider, model, tokens=None, seconds=0.0, status="ok", log=None,
            pool=None, before=None, after=None, conversation=None, conversation_tokens=None, journal_path=None,
            context=None, account=None, crossed=None, why=None, rules=None, score_pick=None, score_notes=None, score_table=None, preset="manual", preset_picks=None, comments=None):
    """Write one JSON line to ~/.cache/ask/usage.jsonl with model, tokens, and CLI pool before/after
    and carried-on conversation_tokens/conversation.
    """
    p = journal_path or JOURNAL_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    model = model or name
    tok = tokens or {}
    t_in = tok.get("in")
    t_out = tok.get("out")
    t_tot = tok.get("total")
    if t_tot is None and (t_in is not None or t_out is not None):
        t_tot = (t_in or 0) + (t_out or 0)
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts": round(time.time(), 3),
        "script": script,
        "role": role,
        "model": model,
        "name": model or "",
        "provider": provider,
        "tokens": {"in": t_in, "out": t_out, "total": t_tot},
        "seconds": round(float(seconds or 0.0), 2),
        "status": status,
        "log": log,
    }
    entry["preset"] = preset
    if comments is not None:
        entry["comments"] = comments
    if preset_picks is not None:
        entry["preset_picks"] = preset_picks
    if score_pick is not None:
        entry["score_pick"] = score_pick
    if score_notes:
        entry["score_notes"] = score_notes
    if score_table:
        entry["score_table"] = score_table
    if conversation_tokens is not None:
        entry["conversation_tokens"] = conversation_tokens
    unit = billed_tokens(tok, provider, pool=pool or _token_pool(entry, provider), rules=rules)
    entry["tokens"].update(fresh=unit["fresh"], billed=unit["billed"])
    if tok.get("cached_in") is not None:
        entry["tokens"]["cached_in"] = tok["cached_in"]
    if conversation is not None:
        entry["conversation"] = conversation
    if pool is not None:
        entry["pool"] = pool
    if before is not None:
        entry["before"] = before
    if after is not None:
        entry["after"] = after
    if context is not None:
        entry["context"] = context
    if account is not None:
        entry["account"] = account
    if crossed is not None:
        entry["crossed"] = bool(crossed)  # true: work context, personal account (work_ok);
    if why is not None:
        entry["why"] = why  # "expires_first": its quota burns first among targets equally fit
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def journal_event(kind, journal_path=None, **fields):
    entry = dict(fields, time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 ts=round(time.time(), 3), type="handoff", event=kind)
    path = journal_path or JOURNAL_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def writer_verdict(conversation, verdict, note, model=None, provider=None, journal_path=None):
    """Resolve identity from the writer run so a verdict cannot relabel it."""
    if verdict not in ("ok", "fixed", "rejected"):
        raise ValueError("verdict must be ok, fixed or rejected")
    if not str(conversation).strip() or not str(note).strip():
        raise ValueError("a verdict needs a conversation id and a note")
    rows = _read_journal_rows(journal_path=journal_path)
    known = [row for row in rows if row.get("conversation") == conversation and "type" not in row]
    runs = [row for row in known if row.get("role") == "writer"
            or row.get("script") == "writer.py"]
    if runs:
        if model is not None or provider is not None:
            raise ValueError("--model/--agy/--codex are only allowed for an id absent from the journal")
        model, provider = runs[-1].get("model"), runs[-1].get("provider")
    elif known or any(row.get("conversation") == conversation for row in rows):
        raise ValueError("conversation has no writer run in the journal")
    elif not model or provider not in ("agy", "codex"):
        raise ValueError("unknown conversation id; a manual run needs --model and --agy or --codex")
    if not model or provider not in ("agy", "codex"):
        raise ValueError("writer run has no agy/codex model identity")
    entry = {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "verdict",
             "conversation": conversation, "model": model, "provider": provider, "verdict": verdict, "note": note}
    path = journal_path or JOURNAL_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def verdict_counts(now=None, journal_path=None, fake=None):
    """Keep verdicts out of call totals and count only the last 30 days."""
    now_ts = float(now if now is not None else time.time())
    counts = {}
    for row in _read_journal_rows(journal_path=journal_path, fake=fake):
        if row.get("type") != "verdict" or row.get("verdict") not in ("ok", "fixed", "rejected"):
            continue
        stamp = _parse_iso_utc(row.get("time"))
        if stamp is None or not now_ts - 30 * 86400 <= stamp.timestamp() <= now_ts:
            continue
        key = (row.get("provider"), row.get("model"))
        bucket = counts.setdefault(key, {"ok": 0, "fixed": 0, "rejected": 0})
        bucket[row["verdict"]] += 1
    return counts


def format_verdicts(counts):
    return ", ".join(f"{counts.get(key, 0)} {key}" for key in ("ok", "fixed", "rejected")) if counts else ""


def read_journal_24h(now=None, journal_path=None, rules=None):
    p = journal_path or JOURNAL_PATH
    if not os.path.exists(p):
        return {}
    rules = rules or load_roles()
    now_ts = float(now if now is not None else time.time())
    cutoff = now_ts - 86400.0
    by_label = {}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                # Verdicts describe quality, not another paid call.
                if not isinstance(row, dict) or "type" in row:
                    continue
                try:
                    ts = row.get("ts")
                    if ts is None:
                        dt = _parse_iso_utc(row.get("time", ""))
                        ts = dt.timestamp() if dt else 0.0
                    if float(ts) < cutoff:
                        continue
                    raw_model = row.get("model") or row.get("name") or "?"
                    # Old alias fields are ignored so all calls group by id.
                    slot = by_label.setdefault(raw_model, {"model": raw_model, "calls": 0, "tokens": 0, "quota_hits": 0})
                    slot["calls"] += 1
                    slot["tokens"] += billed_tokens(row, row.get("provider"),
                                                    pool=_token_pool(row, row.get("provider")), rules=rules)["billed"]
                    if str(row.get("status", "")).startswith("quota"):
                        slot["quota_hits"] += 1
                except Exception:
                    continue  # one row with a bad ts or tokens does not end the day's count
    except OSError:
        pass
    return by_label


def is_quota_error(returncode, raw_text):
    # 429 as a status code, not as digits inside a count or an id ("1429 lines", a UUID). A call that exited 0 (it may
    # have answered) needs the status in context: "HTTP 429", "status 429", "429 Too Many Requests", not "Reviewed 429 lines".
    http_429 = bool(re.search(r"(?<![\w.])429(?![\w.])", raw_text or ""))
    status_429 = bool(re.search(r"(?i)(?:http|status|code|error)\W{0,3}429(?![\w.])|(?<![\w.])429\W{0,3}(?:too many|rate|quota|resource)",
                                raw_text or ""))
    if returncode == 0 and raw_text and not ("RESOURCE_EXHAUSTED" in raw_text or status_429):
        return False
    low = (raw_text or "").lower()
    if http_429:
        return True
    for pat in ("resource_exhausted", "quota", "rate limit", "usage limit", "limit exceeded", "limit reached", "too many requests"):
        if pat in low:
            return True
    if returncode != 0 and "limit" in low:
        return True
    return False


def format_local_check(loc=None, model=None, rules=None):
    rules = rules or load_roles()
    loc = loc or local_state(model=model, rules=rules)
    verdict = "FREE" if loc["free"] else "BUSY"
    loaded_items = []
    for m in loc["loaded"]:
        mname = m.get("name") or ""
        loaded_items.append(f"{mname} ({_to_gb(m.get('size_vram') or 0):.1f} GB)")
    loaded_str = ", ".join(loaded_items) if loaded_items else "none"
    lines = [
        f"Verdict: {verdict}" + ("" if loc["free"] else f" ({loc['reason_str']})"),
        f"GPU:     {loc['used_gb']:.1f} / {loc['total_gb']:.1f} GB VRAM ({loc['free_gb']:.1f} GB free), {loc['utilization']}% util",
        f"Ollama:  {'up' if loc['ollama_up'] else 'down'}, loaded: {loaded_str}",
    ]
    if loc["reasons"]:
        for r in loc["reasons"]:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def main():
    import argparse
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Check local GPU state or pick a model for a role.")
    ap.add_argument("--role", help="pick a model for this role")
    ap.add_argument("--model", help="check local state for this Ollama model")
    ap.add_argument("--tokens", type=int, help="override estimated tokens for the run")
    ap.add_argument("--fake", help="path to fake state JSON")
    ap.add_argument("--context", help="work or personal (default: ASK_CONTEXT; unknown is work)")
    a = ap.parse_args()
    if a.role:
        res = pick(a.role, fake=a.fake, tokens=a.tokens, context=resolve_context(a.context))
        for p in res["passed_over"]:
            print(f"passed over: {p}")
        if res.get("warning"):
            print(res["warning"])
        if res["picked"]:
            print(f"picked: {res['label']} [{res['provider']}]")
            return 0
        print(res["message"])
        return res["exit_code"]
    loc = local_state(model=a.model, fake=a.fake)
    print(format_local_check(loc, model=a.model))
    return 0 if loc["free"] else 3


if __name__ == "__main__":
    sys.exit(main())
