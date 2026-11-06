"""What the limits band above the prompt shows, as one JSON line: the week's pools, the linked sessions, the age of
the Claude numbers (the mod `limits-band` runs it once a minute).

    python ~/switchboard/mods/limits-band/band.py [--session <the CLI session id>] [--text]

Reads only local files and the router's cloud_state (agy's /usage and Codex's rate limits, both quota reads, no
model call; cached here for TTL_S so that a band in several sessions asks them at most every few minutes). The
sessions come from the desktop app's own session files: the family of the given session (its root and everyone
spawned under it, archived ones left out).
"""
import argparse
import glob
import json
import os
import sys
import time

# The router beside this mod (<root>/skills/ask), in the user's repo and in the shipped plugin alike.
ASK = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "ask")
if ASK not in sys.path:
    sys.path.insert(0, ASK)

import limits  # noqa: E402
import route  # noqa: E402

TTL_S = 180
STALE_S = 3600
SHORT = {"claude-subscription": "Claude", "codex": "ChatGPT", "agy": "agy"}


def short_name(pool):
    prov = pool.get("provider")
    name = limits.pool_name(pool)
    if prov == "claude-subscription":
        return "Fable" if route._claude_scope(pool) == "fable" else "Claude"
    if prov == "codex":
        return "ChatGPT"
    if prov == "agy":
        return name.replace("agy: ", "agy ")
    return name


def provider_and_model(pool):
    """Whose quota it is and what it covers: ("Anthropic", "Claude"), ("Google", "Claude and GPT")."""
    prov = pool.get("provider")
    if prov == "claude-subscription":
        return "Anthropic", ("Fable" if route._claude_scope(pool) == "fable" else "Claude")
    if prov == "codex":
        return "OpenAI", "GPT"
    if prov == "agy":
        return "Google", limits.pool_name(pool).replace("agy: ", "")
    return str(prov or "?"), limits.pool_name(pool)


def compact_span(seconds):
    """4.9 d; 17 h; 25 min (in the reader's language: 4,9 д in Ukrainian)."""
    if seconds is None:
        return "?"
    s = max(0.0, float(seconds))
    if s >= 86400:
        return f"{s / 86400:.1f}".replace(".", limits.text("decimal")) + " " + limits.text("d")
    if s >= 3600:
        return f"{int(s // 3600)} {limits.text('h')}"
    return f"{int(s // 60)} {limits.text('min')}"


def skip_reason(pool, rules):
    """Why the router refuses this pool (route._check_allowed's tests), or None. A stale pool it does not test."""
    if pool.get("stale"):
        return None
    if pool.get("quota_reached"):
        return limits.text("spent")
    floors = rules.get("floors") or {}
    for w in pool.get("windows") or []:
        wtype = str(w.get("window", "weekly"))
        floor = float(floors.get(wtype, floors.get("weekly", 0.10)))
        if route._available(w) < floor:
            return limits.text("below_floor") if "week" in wtype.lower() else limits.text("5h_below_floor")
    return None


def pace_over(pool, rules):
    """The weekly pace over pace_skip: the router then passes the pool by only while a calmer candidate comes later
    (route.pick), so it is a warning, not a refusal (Fable's review)."""
    if pool.get("stale"):
        return False
    return any("week" in str(w.get("window", "")).lower() and float(w.get("pace") or 0.0) > float(rules.get("pace_skip", 2.0))
               for w in pool.get("windows") or [])


def pools_part(cs, rules):
    pools = sorted(cs.get("pools", []), key=lambda p: (limits.PROVIDER_ORDER.get(p.get("provider"), 9),
                                                        bool(p.get("nested_in")), limits.pool_name(p)))
    burn = route.burning_pools(pools)
    first_id = burn[0][1].get("id") if burn else None
    by_id = {p.get("id"): p for p in pools}
    out = []
    for p in pools:
        weekly = [w for w in p.get("windows") or [] if "week" in str(w.get("window", "")).lower()]
        if not weekly:
            continue
        w = weekly[0]
        if p.get("nested_in"):
            # Fable inside all models: shown only when its own leftover is the smaller one.
            parent = by_id.get(p["nested_in"])
            own = w.get("own_remaining_fraction")
            if own is None or parent is None or not parent.get("windows"):
                continue
            if float(own) >= route._available(parent["windows"][0]) - 0.005:
                continue
        provider, model = provider_and_model(p)
        out.append({
            "name": short_name(p),
            "provider": provider,
            "model": model,
            "left_pct": round(route._available(w) * 100),
            "resets": compact_span(w.get("resets_in_s")),
            "burns_first": p.get("id") == first_id,
            "skip": skip_reason(p, rules),
            "pace_over": pace_over(p, rules),
            "stale": bool(p.get("stale")),
        })
    return out


def session_dirs():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return glob.glob(os.path.join(base, "Claude-Data", "*", "claude-code-sessions", "*", "*"))


def read_sessions(cli_id):
    """{"working", "idle"}: the other sessions of the family of the session whose CLI id is cli_id (its root and
    everyone spawned under it, this session and archived ones left out), or None."""
    if not cli_id:
        return None
    for d in session_dirs():
        rows = {}
        for f in glob.glob(os.path.join(d, "local_*.json")):
            try:
                with open(f, encoding="utf-8") as h:
                    rows[os.path.basename(f)[:-5]] = json.load(h)
            except Exception:
                continue
        me = next((sid for sid, r in rows.items() if r.get("cliSessionId") == cli_id), None)
        if me is None:
            continue

        def parent(sid):
            return ((rows.get(sid) or {}).get("spawnedFrom") or {}).get("sessionId")

        root, seen = me, set()
        while parent(root) in rows and root not in seen:
            seen.add(root)
            root = parent(root)

        def in_family(sid):
            hops = 0
            while sid and hops < 32:
                if sid == root:
                    return True
                sid, hops = parent(sid), hops + 1
            return False

        working, idle = 0, 0
        now_ms = time.time() * 1000
        for sid, r in rows.items():
            if sid in (root, me) or r.get("isArchived") or not in_family(sid):
                continue
            # A turn is running while the app saw activity after the last turn's edge. A session with no edge yet
            # (its first turn, or a file from before the app wrote it) is working only if it was active lately.
            act, edge = r.get("lastActivityAt") or 0, r.get("lastTurnEdgeAt")
            busy = act > edge + 1000 if edge else now_ms - act < 10 * 60 * 1000
            if busy:
                working += 1
            else:
                idle += 1
        return {"working": working, "idle": idle}
    return None


def claude_age(cs):
    sub = cs.get("claude_subscription") or {}
    if sub.get("status") == "missing":
        return {"status": "missing", "age_s": None}
    return {"status": sub.get("status"), "age_s": round(float(sub.get("age_s") or 0))}


def build(cli_id=None):
    rules = route.load_roles()
    out = {"at": time.strftime("%H:%M"), "errors": []}
    try:
        cs = route.cloud_state(rules=rules, ttl=TTL_S)
        out["pools"] = pools_part(cs, rules)
        out["claude"] = claude_age(cs)
        if cs.get("error"):
            out["errors"].append("agy: " + str(cs["error"])[:80])
        if cs.get("codex_error"):
            out["errors"].append("codex: " + str(cs["codex_error"])[:80])
    except Exception as e:
        out["pools"], out["claude"] = [], {"status": "missing", "age_s": None}
        out["errors"].append("cloud_state: " + str(e)[:80])
    try:
        out["sessions"] = read_sessions(cli_id)
    except Exception as e:
        out["sessions"] = None
        out["errors"].append("sessions: " + str(e)[:80])
    return out


def plural(n, one, few, many):
    """1 сесія, 2 сесії, 5 сесій (and 11 to 14 take the third); English: one, then many."""
    if limits.lang() != "uk":
        return one if n == 1 else many
    if 11 <= n % 100 <= 14:
        return many
    return one if n % 10 == 1 else (few if 2 <= n % 10 <= 4 else many)


def mark(p):
    return " ❌" if p["skip"] else (" ⚠️" if p["burns_first"] or p.get("pace_over") else "")


def text(data):
    """The band's collapsed line as plain text, as the mod draws it (for a check in a terminal)."""
    providers = []
    for p in data.get("pools", []):
        if p["provider"] not in providers:
            providers.append(p["provider"])
    summary = " · ".join(f"{name} " + " / ".join(f"{p['left_pct']}%{mark(p)}" for p in data["pools"] if p["provider"] == name)
                         for name in providers) or limits.text("no_quotas")
    tail = []
    s = data.get("sessions")
    if s:
        said = ([f"{s['working']} {plural(s['working'], *limits.text('sessions_working'))}"] if s["working"] else [])
        said += [f"{s['idle']} {plural(s['idle'], *limits.text('sessions_idle'))}"] if s["idle"] else []
        if said:
            tail.append(", ".join(said))
    c = data.get("claude") or {}
    if c.get("status") == "missing":
        tail.append(limits.text("claude_missing"))
    elif (c.get("age_s") or 0) > STALE_S:
        tail.append(limits.text("claude_old", span=compact_span(c["age_s"])))
    tail += [f"⚠️ {e}" for e in data.get("errors", [])[:1]]
    return "   │   ".join([summary] + tail)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", help="the CLI session id whose family of sessions to count")
    ap.add_argument("--text", action="store_true", help="print the band as text instead of JSON")
    a = ap.parse_args(argv)
    data = build(a.session)
    print(text(data) if a.text else json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
