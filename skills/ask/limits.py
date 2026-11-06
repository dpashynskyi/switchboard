"""The week's limits of every pool as one small table.

    python ~/switchboard/skills/ask/limits.py # the table (markdown: it reads in a terminal too)
    python ~/switchboard/skills/ask/limits.py --claude-json FILE # first write the session's get_usage reply (a file, or - for
                                                                   # stdin) to ~/.cache/ask/claude_usage.json

The Claude subscription's limits only the session can read (its get_usage tool); the rest comes from the router's
cloud_state, the same numbers as --status and the dashboard.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import route

PROVIDER_ORDER = {"claude-subscription": 0, "codex": 1, "agy": 2}

# The words a person reads, in the language ASK_LANG names (uk, en); the limits band takes them from here too.
DEFAULT_LANG = "en"
WORDS = {
    "uk": {"all_models": "усі моделі", "and": " і ", "d": "д", "h": "год", "min": "хв", "spent": "вичерпано",
           "skipped": "обходиться", "fast": "швидко", "calm": "спокійно", "resets_in": "скидання через {span}",
           "nested": "{name} входить у «усі моделі»: власний залишок {own}%, доступно {avail}% (менше з двох).",
           "title": "Тижневі ліміти, {stamp}", "head": "| Пул | Лишилось | Скидання через | Темп | Стан |",
           "burns_first": "Згорить першим: {name}, {pct}% за {span}.", "reservations": "Резерви: {n} ({models}).",
           "five_hour": "П'ятигодинні вікна: ", "over_half": "понад половину в кожному",
           "claude_stale": "Claude: цифр немає або вони старші за 6 год; сесія оновлює їх через get_usage (`--claude-json`).",
           "below_floor": "нижче порогу", "5h_below_floor": "5 год нижче порогу", "no_quotas": "квот немає",
           "sessions_working": ("сесія працює", "сесії працюють", "сесій працюють"),
           "sessions_idle": ("вільна", "вільні", "вільних"),
           "claude_missing": "⚠️ Claude: цифр немає", "claude_old": "⚠️ Claude: цифри {span} тому", "decimal": ","},
    "en": {"all_models": "all models", "and": " and ", "d": "d", "h": "h", "min": "min", "spent": "spent",
           "skipped": "skipped", "fast": "fast", "calm": "calm", "resets_in": "resets in {span}",
           "nested": "{name} sits inside \"all models\": its own {own}% left, {avail}% available (the smaller of the two).",
           "title": "Weekly limits, {stamp}", "head": "| Pool | Left | Resets in | Pace | State |",
           "burns_first": "Burns first: {name}, {pct}% in {span}.", "reservations": "Reservations: {n} ({models}).",
           "five_hour": "Five-hour windows: ", "over_half": "over half in each",
           "claude_stale": "Claude: no numbers, or older than 6 h; the session refreshes them with get_usage (`--claude-json`).",
           "below_floor": "below the floor", "5h_below_floor": "5 h below the floor", "no_quotas": "no quotas",
           "sessions_working": ("session working", "sessions working", "sessions working"),
           "sessions_idle": ("idle", "idle", "idle"),
           "claude_missing": "⚠️ Claude: no numbers", "claude_old": "⚠️ Claude: numbers {span} old", "decimal": "."},
}


def lang():
    # Blank is unset, in the band's TypeScript as here: the default, not English.
    value = (os.environ.get("ASK_LANG") or "").strip().lower() or DEFAULT_LANG
    return value if value in WORDS else "en"


def text(key, **fields):
    word = WORDS[lang()][key]
    return word.format(**fields) if fields and isinstance(word, str) else word


def pool_name(pool):
    label = str(pool.get("label") or pool.get("id") or "?")
    prov = pool.get("provider")
    if prov == "claude-subscription":
        part = label.split("·", 1)[1].strip() if "·" in label else label
        return "Claude, " + (text("all_models") if part.lower() == "all models" else part)
    if prov == "codex":
        return "ChatGPT (Codex)"
    if prov == "agy":
        short = label.replace(" Models", "").replace(" models", "").replace(" and ", text("and"))
        return "agy: " + short
    return label


def span(seconds):
    """4 d 23 h, 17 h 48 min, 25 min (in the reader's language)."""
    if seconds is None:
        return "?"
    m = max(0, int(seconds // 60))
    d, h, mins = m // 1440, (m // 60) % 24, m % 60
    if d:
        return f"{d} {text('d')} {h} {text('h')}"
    if h:
        return f"{h} {text('h')} {mins} {text('min')}"
    return f"{mins} {text('min')}"


def state_word(window, pool, rules):
    floor = float((rules.get("floors") or {}).get("weekly", 0.10))
    avail = route._available(window)
    pace = window.get("pace")
    if pool.get("quota_reached") or avail < floor:
        return text("spent")
    if pace is not None and float(pace) > float(rules.get("pace_skip", 2.0)):
        return text("skipped")
    if pace is not None and float(pace) > 1.5:
        return text("fast")
    return text("calm")


def write_claude_usage(raw, path=None):
    """Store get_usage's plan (the whole reply or its "plan") as the router reads it."""
    data = json.loads(raw)
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else data
    windows = [{"label": w.get("label"), "percentUsed": w.get("percentUsed"), "resetsAt": w.get("resetsAt")}
               for w in plan.get("windows") or []]
    if not windows:
        raise ValueError("no windows in the get_usage reply")
    entry = {"read_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "plan": plan.get("plan"), "windows": windows, "source": "get_usage"}
    path = path or os.path.expanduser(os.environ.get("ASK_CLAUDE_USAGE_PATH") or route.CLAUDE_USAGE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic: the status line and the band read this file at any moment.
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(entry, handle, ensure_ascii=False)
    os.replace(tmp, path)
    return entry


def table(cs=None, rules=None, now=None):
    rules = rules or route.load_roles()
    cs = cs or route.cloud_state(rules=rules)
    rows, nested, short = [], [], []
    pools = sorted(cs.get("pools", []), key=lambda p: (PROVIDER_ORDER.get(p.get("provider"), 9), pool_name(p)))
    for p in pools:
        weekly = [w for w in p.get("windows") or [] if "week" in str(w.get("window", "")).lower()]
        for w in weekly[:1]:
            pace = w.get("pace")
            rows.append((pool_name(p), f"{route._available(w) * 100:.0f}%", span(w.get("resets_in_s")),
                         "?" if pace is None else f"×{float(pace):.1f}", state_word(w, p, rules)))
        for w in p.get("windows") or []:
            if "5h" in str(w.get("window", "")) and route._available(w) < 0.5:
                short.append(f"{pool_name(p)} {route._available(w) * 100:.0f}% ({text('resets_in', span=span(w.get('resets_in_s')))})")
        for w in p.get("windows") or []:
            if w.get("own_remaining_fraction") is not None and not p.get("stale"):
                nested.append(text("nested", name=pool_name(p), own=f"{float(w['own_remaining_fraction']) * 100:.0f}",
                                   avail=f"{route._available(w) * 100:.0f}"))
    burn = [(u, pool_name(p), w) for u, p, w in route.burning_pools(pools)]
    stamp = time.strftime("%H:%M", time.localtime(now)) if now else time.strftime("%H:%M")
    out = [text("title", stamp=stamp), "",
           text("head"),
           "|---|---:|---:|---:|---|"]
    out += [f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in rows]
    out.append("")
    if burn:
        _, name, w = max(burn, key=lambda t: t[0])
        out.append(text("burns_first", name=name, pct=f"{route._available(w) * 100:.0f}", span=span(w.get("resets_in_s"))))
    out += nested
    res = cs.get("reservations") or []
    if res:
        models = sorted({str(r.get("model") or r.get("name") or "?") for r in res})
        out.append(text("reservations", n=len(res), models=", ".join(models)))
    out.append(text("five_hour") + ("; ".join(short) if short else text("over_half")) + ".")
    sub = cs.get("claude_subscription") or {}
    if sub.get("status") in ("stale", "missing"):
        out.append(text("claude_stale"))
    return "\n".join(out)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--claude-json", help="get_usage's reply to store first: a file, or - for stdin")
    a = ap.parse_args(argv)
    if a.claude_json:
        raw = sys.stdin.read() if a.claude_json == "-" else open(a.claude_json, encoding="utf-8").read()
        write_claude_usage(raw)
    print(table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
