"""The scorecard of models and providers: the writer verdict share from the journal (ASK-04 v1).

What the first live room agreed (2026-11-08-scorecard, plan p1):
- a verdict is joined by its conversation to the writer run it judges, for that run's provider, model and role; the run
  may be older than the window, only the verdict has to be inside it;
- each conversation counts once, by its latest verdict, so a corrected judgment does not inflate the count;
- share = (ok + fixed) / (ok + fixed + rejected); the clean ok share is shown beside it;
- below `min_judged` judged conversations a row is provisional and routing keeps its usual quality.
Time per role, errors and 429s, quota per call are for a later version.
"""
import time

import experience

VERDICTS = ("ok", "fixed", "rejected")
DEFAULTS = {"window_days": 30, "min_judged": 10}


def settings(rules=None):
    return dict(DEFAULTS, **((rules or {}).get("scorecard") or {}))


def _runs(rows):
    """The latest run of every conversation: its provider, model and role."""
    runs = {}
    for row in rows:
        if not isinstance(row, dict) or "type" in row or not row.get("conversation"):
            continue
        cid = row["conversation"]
        if cid not in runs or experience._timestamp(row) >= experience._timestamp(runs[cid]):
            runs[cid] = row
    return runs


def cards(rows, rules=None, now=None):
    """{(provider, model, role): card} over the window. A card: ok, fixed, rejected, judged, total, share, clean,
    provisional. total counts the writer conversations run in the window, judged or not."""
    config = settings(rules)
    now = time.time() if now is None else float(now)
    since = now - float(config["window_days"]) * 86400
    rows = [r for r in rows if isinstance(r, dict)]
    runs = _runs(rows)
    latest = {}
    for row in rows:
        if row.get("type") != "verdict" or row.get("verdict") not in VERDICTS or not row.get("conversation"):
            continue
        stamp = experience._timestamp(row)
        if not since <= stamp <= now:
            continue
        cid = row["conversation"]
        if cid not in latest or stamp >= experience._timestamp(latest[cid]):
            latest[cid] = row
    out = {}

    def card(key):
        return out.setdefault(key, {"ok": 0, "fixed": 0, "rejected": 0, "judged": 0, "total": 0})

    for cid, run in runs.items():
        if since <= experience._timestamp(run) <= now:
            card((run.get("provider"), run.get("model"), run.get("role")))["total"] += 1
    for cid, verdict in latest.items():
        run = runs.get(cid, {})
        # A verdict for a run the journal does not hold (a manual one) keeps its own identity and has no role.
        key = (run.get("provider", verdict.get("provider")), run.get("model", verdict.get("model")), run.get("role"))
        c = card(key)
        c[verdict["verdict"]] += 1
        c["judged"] += 1
        if not (since <= experience._timestamp(run) <= now):
            c["total"] += 1  # judged inside the window, run before it
    for c in out.values():
        c["share"] = (c["ok"] + c["fixed"]) / c["judged"] if c["judged"] else None
        c["clean"] = c["ok"] / c["judged"] if c["judged"] else None
        c["provisional"] = c["judged"] < int(config["min_judged"])
    return out


def verdict_quality(role, provider, model, rows, rules=None, now=None):
    """The verdict share that replaces the role's quality, or None while the row is provisional."""
    c = cards(rows, rules, now).get((provider, model, role))
    return None if c is None or c["provisional"] else c["share"]


def table(found, rules=None, everything=False):
    """The table; rows no verdict ever judged (review, second-opinion, runs without a role) only with everything."""
    config = settings(rules)
    hidden = 0 if everything else sum(1 for c in found.values() if not c["judged"])
    if not everything:
        found = {k: c for k, c in found.items() if c["judged"]}
    if not found:
        none = f"No writer verdicts in the last {config['window_days']} days (writer.py --verdict <id> ok|fixed|rejected)."
        return none + (f" {hidden} rows with runs but no verdict: ask.py --scorecard all." if hidden else "")
    head = ("provider", "model", "role", "share", "clean ok", "ok", "fixed", "rejected", "judged/total", "")
    body = []
    order = sorted(found.items(), key=lambda kv: (-(kv[1]["share"] or -1), str(kv[0])))
    for (provider, model, role), c in order:
        pct = (lambda v: "-" if v is None else f"{v:.0%}")
        body.append((str(provider or "?"), str(model or "?"), str(role or "-"), pct(c["share"]), pct(c["clean"]),
                     str(c["ok"]), str(c["fixed"]), str(c["rejected"]), f"{c['judged']}/{c['total']}",
                     "provisional" if c["provisional"] else ""))
    widths = [max(len(r[i]) for r in [head] + body) for i in range(len(head))]
    line = lambda r: "  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip()
    foot = (f"Last {config['window_days']} days, the latest verdict of each conversation. share = (ok + fixed) / judged. "
            f"Under {config['min_judged']} judged a row is provisional: routing keeps its usual quality.")
    if hidden:
        foot += f"\n{hidden} rows with runs but no verdict are hidden: ask.py --scorecard all."
    return "\n".join([line(head)] + [line(r) for r in body] + ["", foot])
