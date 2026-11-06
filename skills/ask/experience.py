"""Small, owner-bounded adjustments from public priors and local outcomes."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # route.py beside it, also under python -I


def load_benchmarks(path=None):
    try:
        with open(path or os.path.join(os.path.dirname(__file__), "benchmarks.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("sources", {}), dict) or not isinstance(data.get("models", {}), dict):
            return {}
        return data
    except (OSError, ValueError, TypeError):
        return {}


def family(model_id):
    return model_id if ":" in model_id else re.sub(r"-(low|medium|high|xhigh|max)$", "", model_id)


def _scores(model_id, category, bench):
    scores = []
    entries = bench.get("models", {}).get(family(model_id), {})
    if not isinstance(entries, dict):
        return scores
    for key, source in bench.get("sources", {}).items():
        if not isinstance(source, dict) or source.get("category") != category or key not in entries:
            continue
        try:
            top, score = float(source["top"]), float(entries[key])
            if top > 0 and 0 <= score < float("inf") and top < float("inf"):
                scores.append(score / top)
        except (KeyError, TypeError, ValueError):
            continue
    return scores


def prior(model_id, role, rules, bench):
    config = rules.get("experience", {})
    category = config.get("role_category", {}).get(role)
    scores = _scores(model_id, category, bench) if category else []
    if not scores:
        return 0.5
    p = config.get("prior", {})
    q0 = 0.5 + p.get("scale", 0.5) * (sum(scores) / len(scores) - p.get("center", 0.75))
    return max(0.0, min(1.0, max(p.get("lo", 0.3), min(p.get("hi", 0.7), q0))))


def _timestamp(row):
    try:
        if row.get("ts") is not None:
            return float(row["ts"])
        dt = datetime.fromisoformat(row.get("time", "").replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return float("-inf")


# Run statuses counted when no verdict judges the run. "deadline" is not: the caller sets --deadline and a short one
# says nothing of the model.
RUN_OUTCOMES = ("capped", "timeout")
DEFAULT_VALUES = {"ok": 1.0, "fixed": 0.6, "rejected": 0.0, "capped": 0.2, "timeout": 0.2}


def evidence(role, model_id, provider, rows, rules, now=None):
    config = rules.get("experience", {})
    values = dict(DEFAULT_VALUES, **config.get("values", {}))
    now = time.time() if now is None else float(now)
    since = now - config.get("window_days", 30) * 86400
    rows = [r for r in rows if isinstance(r, dict)]
    runs, verdicts = {}, set()
    for row in rows:
        conversation = row.get("conversation")
        if not conversation:
            continue
        if row.get("type") == "verdict":
            verdicts.add(conversation)
        elif "type" not in row and (conversation not in runs or _timestamp(row) >= _timestamp(runs[conversation])):
            # Look outside the window too: a recent verdict can judge an older run.
            runs[conversation] = row
    result = {"n": 0.0, "n_role": 0, "sum": 0.0, "items": []}
    for row in rows:
        if row.get("model") != model_id or row.get("provider") != provider or not since <= _timestamp(row) <= now:
            continue
        conversation = row.get("conversation")
        if row.get("type") == "verdict":
            outcome = row.get("verdict")
            row_role = runs.get(conversation, {}).get("role")
        elif "type" not in row and row.get("status") in RUN_OUTCOMES and (not conversation or conversation not in verdicts):
            # A run with no verdict of its own; ask.py rows often carry no conversation.
            outcome, row_role = row["status"], row.get("role")
        else:
            continue
        if outcome not in values:
            continue
        weight = 1.0 if row_role == role else config.get("other_role_weight", 0.25)
        result["n"] += weight
        result["n_role"] += int(row_role == role)
        result["sum"] += weight * values[outcome]
        result["items"].append({"verdict": outcome, "role": row_role, "conversation": conversation,
                                "time": row.get("time", row.get("ts"))})
    return result


def _verdict_count(e):
    return sum(item["verdict"] not in RUN_OUTCOMES for item in e["items"])


def _probation(e, rules, tokens):
    # Use the same window as quality so old verdicts do not hide recent unfamiliarity.
    config = rules.get("experience", {})
    return (_verdict_count(e) < config.get("probation_verdicts", 3)
            and tokens >= config.get("probation_tokens", 200000))


def shift(q, e, rules, tokens=0):
    config = rules.get("experience", {})
    demotion = 0
    if e["n_role"] >= config.get("min_evidence", 2):
        if q < config.get("demote_twice_below", 0.35):
            demotion = 2
        elif q < config.get("demote_below", 0.5):
            demotion = 1
    probation = int(_probation(e, rules, tokens))
    # Demotion and probation add, but a row never moves more than two places (roles.json experience).
    if demotion or probation:
        return min(2, demotion + probation)
    if e["n_role"] >= config.get("promote_min_evidence", 3) and q >= config.get("promote_above", 0.8):
        return -1
    return 0


def quality(role, model_id, provider, rows, rules, bench, now=None, tokens=None):
    q0 = prior(model_id, role, rules, bench)
    e = evidence(role, model_id, provider, rows, rules, now=now)
    k = rules.get("experience", {}).get("prior_weight", 2)
    q = (k * q0 + e["sum"]) / (k + e["n"]) if k + e["n"] else q0
    if tokens is None:
        import route
        tokens = route.estimate_tokens(role, provider=provider, model=model_id, rules=rules, fake={"journal": rows})
    return {"q": q, "q0": q0, "evidence": e, "shift": shift(q, e, rules, tokens),
            "probation": _probation(e, rules, tokens), "tokens": tokens}


def summary(result, role=None):
    items = result["evidence"]["items"]
    counts = Counter(item["verdict"] for item in items if role is None or item["role"] == role)
    parts = [f"{counts[key]} {key}" for key in ("ok", "fixed", "rejected") + RUN_OUTCOMES if counts[key]]
    detail = ", ".join(parts) or "no outcomes"
    if role is not None:
        elsewhere = sum(item["role"] != role for item in items)
        detail += f" in role, {elsewhere} elsewhere"
    return f"q {result['q']:.2f} (prior {result['q0']:.2f}; {detail})"


def _raw_role(role, rules):
    spec = rules.get("roles", {}).get(role, {})
    return spec if isinstance(spec, list) else spec.get("models", spec.get("names", []))


def reorder(role, raw_list, rules, rows, bench, tokens=None, now=None):
    spec = rules.get("roles", {}).get(role, {})
    if (not rules.get("experience", {}).get("enabled", False)
            or isinstance(spec, dict) and (spec.get("pinned") or spec.get("local_only"))):
        return list(raw_list), []
    import route
    identities = [route.resolve_model(row, rules=rules) if not isinstance(row, dict) or "any" not in row
                  else (None, None) for row in raw_list]
    results, ordered = {}, list(range(len(raw_list)))
    # Subscription rows are barriers; any-groups retain their exact slots.
    barriers = [-1] + [i for i, (provider, _) in enumerate(identities) if provider == "claude-subscription"] + [len(raw_list)]
    for start, end in zip(barriers, barriers[1:]):
        slots = [i for i in range(start + 1, end) if all(identities[i])]
        for i in slots:
            provider, mid = identities[i]
            results[i] = quality(role, mid, provider, rows, rules, bench, now=now, tokens=tokens)
        # Half a place more, so a shift of one passes its neighbour instead of tying with it (ties keep the user's order).
        ranked = sorted(slots, key=lambda i: i + results[i]["shift"] + 0.5 * ((results[i]["shift"] > 0) - (results[i]["shift"] < 0)))
        for slot, original in zip(slots, ranked):
            ordered[slot] = original
    changed = {original: slot for slot, original in enumerate(ordered) if slot != original}
    causes = [identities[i][1] for i in changed if results[i]["shift"]]
    notes = []
    for original, slot in changed.items():
        result = results[original]
        detail = summary(result)
        if result["probation"]:
            n = _verdict_count(result["evidence"])
            probation = f"probation ({n} verdict{'s' if n != 1 else ''}, est. {result['tokens'] / 1000:.0f}K billed)"
            detail = probation if result["shift"] == 1 else detail + "; " + probation
        if not result["shift"]:
            # A neighbour moves too; name the cause instead of implying its own quality changed.
            detail += "; displaced by " + ", ".join(causes)
        notes.append(f"{identities[original][1]} {'down' if slot > original else 'up'} {abs(slot - original)} in {role}: {detail}")
    return [raw_list[i] for i in ordered], notes


def check(rules, bench, now=None):
    now = time.time() if now is None else float(now)
    lines = []
    for name, source in bench.get("sources", {}).items():
        if not isinstance(source, dict) or now - _timestamp({"time": source.get("read_at")}) > 14 * 86400:
            lines.append(f"stale source: {name} (older than 14 days or no read_at)")
    import route
    used = set()
    for role in rules.get("roles", {}):
        category = rules.get("experience", {}).get("role_category", {}).get(role)
        for row in route.role_candidates(_raw_role(role, rules))[0]:
            _, mid = route.resolve_model(row, rules=rules)
            if mid:
                used.add(family(mid))
                if category and not _scores(mid, category, bench):
                    lines.append(f"missing benchmark: {role}: {mid} ({category})")
    lines.extend(f"unused benchmark model: {mid}" for mid in bench.get("models", {}) if mid not in used)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--role")
    args = parser.parse_args(argv)
    import route
    if args.check:
        try:
            print("\n".join(check(route.load_roles(), load_benchmarks())) or "benchmarks: current and covered")
        except Exception as exc:
            print(f"benchmark check unavailable: {exc}")
        return 0
    rules, bench = route.load_roles(), load_benchmarks()
    if args.role not in rules.get("roles", {}):
        parser.error(f"unknown role: {args.role}")
    rows = route._read_journal_rows()
    raw = _raw_role(args.role, rules)
    print(f"{'Model':<34} {'q0':>5} {'n':>5} {'role':>5} {'q':>5} {'shift':>6}  outcomes")
    spec = rules["roles"][args.role]
    held_role = (not rules.get("experience", {}).get("enabled", False)
                 or isinstance(spec, dict) and (spec.get("pinned") or spec.get("local_only")))
    models, groups = route.role_candidates(raw)
    for row, group in zip(models, groups):
        provider, mid = route.resolve_model(row, rules=rules)
        if not mid:
            continue
        result = quality(args.role, mid, provider, rows, rules, bench)
        e = result["evidence"]
        held = (held_role or provider == "claude-subscription"
                or isinstance(raw[group], dict) and "any" in raw[group])
        row_shift = 0 if held else result["shift"]
        print(f"{mid:<34} {result['q0']:5.2f} {e['n']:5.2f} {e['n_role']:5d} {result['q']:5.2f} {row_shift:+6d}  "
              + summary(result, args.role))
    ordered, notes = reorder(args.role, raw, rules, rows, bench)
    print("Experience order (before policy and availability): " + json.dumps(ordered, ensure_ascii=False))
    for note in notes:
        print(f"[experience: {note}]")
    import score
    if score.config(args.role, rules):
        evaluated = [{"ok": True, "provider": p, "model": m} for p, m in
                     (route.resolve_model(row, rules=rules) for row in models) if m]
        ranked, filtered, _ = score.rank(args.role, evaluated, rules, rows, bench, {"pools": []})
        print("Score table (before policy/availability; default pool rates):")
        print(f"{'Model':<34} {'Q':>5} {'min':>7} {'budget':>7} {'C % wk':>8} {'S':>6}  status")
        for row in ranked + filtered:
            s = row["score"]
            minutes = '?' if s['seconds'] is None else f"{s['seconds']/60:.1f}"
            print(f"{row['model']:<34} {s['Q']:5.2f} {minutes:>7} {s['budget']/60:7g} {s['C']*100:8.2f} {s['S']:6.2f}  "
                  + (row['reason'] if not row['ok'] else 'eligible'))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
