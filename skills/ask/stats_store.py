"""File-only shared dashboard storage. No provider discovery or live probes."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

import route

MACHINE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
SNAPSHOTS = ("agy_usage", "codex_usage", "claude_usage", "pool_history", "reservations")
MAX_BODY = 8 * 1024 * 1024


def canonical(row):
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(row):
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".stats-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def rows_from(path):
    try:
        with open(path, "rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        row.pop("log", None)
                        yield row
                except (ValueError, UnicodeError):
                    continue
    except FileNotFoundError:
        return


def timestamp(value):
    parsed = route._parse_iso_utc(value) if isinstance(value, str) else None
    try:
        result = parsed.timestamp() if parsed else float(value)
        return result if math.isfinite(result) else float("-inf")
    except (ValueError, TypeError, OverflowError):
        return float("-inf")


def account_check(left, right):
    """Pure identity check on normalized pools; unused windows provide no evidence."""
    if (left["provider"], left["label"]) != (right["provider"], right["label"]):
        return "different provider or label"
    comparable = False
    for a in left["windows"]:
        for b in right["windows"]:
            if a.get("label") != b.get("label"):
                continue
            if not (a.get("remaining_fraction", 1) < 1 or b.get("remaining_fraction", 1) < 1):
                continue
            comparable = True
            ta, tb = timestamp(a.get("reset_time")), timestamp(b.get("reset_time"))
            if not (math.isfinite(ta) and math.isfinite(tb)) or abs(ta - tb) > 600:
                return "resets differ or missing"
    return "resets match" if comparable else "no used window to compare"


def account_clusters(entries):
    """Connected components, including singleton pools, ordered by machine name."""
    groups = []
    for entry in sorted(entries, key=lambda e: e[0]):
        matches = [g for g in groups if any(account_check(entry[1], old[1]) == "resets match" for old in g)]
        merged = [entry]
        for group in matches:
            merged.extend(group)
            groups.remove(group)
        groups.append(sorted(merged, key=lambda e: e[0]))
    return sorted(groups, key=lambda g: g[0][0])


def pool_accounts(machines, rules):
    from dashboard import window_status
    entries = [(view["machine"], pool) for view in machines for pool in view["pools"]
               if pool["provider"] in ("agy", "codex")]
    accounts, removed = [], set()
    for members in account_clusters(entries):
        if len(members) == 1:
            pool = members[0][1]
            candidates = [other for _, other in entries if other is not pool and
                          (other["provider"], other["label"]) == (pool["provider"], pool["label"])]
            if all(account_check(pool, other) == "no used window to compare"
                   for other in candidates or [pool]):
                pool["account_note"] = "account not checked: no used window to compare"
            continue
        picked = {}
        for machine, pool in members:
            removed.add(id(pool))
            for window in pool["windows"]:
                key = window["label"]
                if key not in picked or pool["snapshot_at"] > picked[key][0]:
                    picked[key] = (pool["snapshot_at"], dict(window, machine=machine, age_s=pool["age_s"]))
        reservations = [dict(item, machine=machine) for machine, pool in members for item in pool["reservations"]]
        windows = []
        for _, window in picked.values():
            window["reservations"] = reservations
            # These fractions have already been applied to each machine's own pools.
            window["reserved_fraction"] = round(sum(w["reserved_fraction"] for _, pool in members
                                                    for w in pool["windows"] if w["label"] == window["label"]), 6)
            window["available_fraction"] = max(0, window["remaining_fraction"] - window["reserved_fraction"])
            window["used_percent"] = round((1 - window["remaining_fraction"]) * 100, 1)
            window["status"] = window_status(window, rules)
            windows.append(window)
        pool = members[0][1]
        accounts.append({"provider": pool["provider"], "label": pool["label"],
                         "machines": sorted({machine for machine, _ in members}),
                         "checked": "resets match", "windows": windows})
    for view in machines:
        view["pools"] = [pool for pool in view["pools"] if id(pool) not in removed]
        for pool in view["pools"]:
            pool.pop("snapshot_at", None)
    return sorted(accounts, key=lambda a: (0 if a["provider"] == "agy" else 1, a["machines"], a["label"]))


def claude_accounts(snapshots, now):
    """Merge proven accounts, retaining the newest reading for each Claude window."""
    def stamp(value):
        parsed = route._parse_iso_utc(value) if isinstance(value, str) else None
        return parsed.timestamp() if parsed else float("-inf")

    def weekly_all(window):
        label = str(window.get("label", "")).lower()
        return "week" in label and "all models" in label

    entries = []
    for machine, data in snapshots:
        windows = data.get("windows")
        if not isinstance(windows, list) or not windows:
            continue
        plan = str(data.get("plan") or "subscription")
        normalized = {"provider": "claude-subscription", "label": "Claude (" + plan + ")", "windows": []}
        for w in windows:
            if not isinstance(w, dict) or not isinstance(w.get("label"), str):
                continue
            used = w.get("percentUsed")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
                continue
            normalized["windows"].append({"label": w["label"], "remaining_fraction": 1 - used / 100,
                                           "reset_time": w.get("resetsAt")})
        entries.append((machine, normalized, data, plan))

    accounts = []
    for group in account_clusters(entries):
        plan = group[0][3]
        members = [(entry[0], entry[2]) for entry in group]
        picked, extras = {}, []
        for machine, data in members:
            if isinstance(data.get("extra_usage"), dict):
                extras.append((stamp(data.get("read_at")), machine, data["extra_usage"]))
            for raw in data["windows"]:
                if not isinstance(raw, dict) or not isinstance(raw.get("label"), str):
                    continue
                used = raw.get("percentUsed")
                if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
                    continue
                read_at = raw.get("read_at", data.get("read_at"))
                read_ts = stamp(read_at)
                label = raw["label"]
                if label in picked and read_ts <= picked[label][0]:
                    continue
                try:
                    win = route._build_window({k: raw.get(k) for k in ("label", "percentUsed", "resetsAt")}, now)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    continue
                age = max(0, now - read_ts)
                picked[label] = (read_ts, {
                    "label": label, "used_percent": used, "left_percent": 100 - used,
                    "resets_at": raw.get("resetsAt"), "resets_in": win["resets_in"],
                    "pace_str": win["pace_str"], "window": win["window"],
                    "read_at": read_at, "age_s": age if math.isfinite(age) else None,
                    "stale": age > route.CLAUDE_STALE_SECONDS,
                    # A window whose own read time differs from the file's was not read by the file's source: the
                    # status line keeps get_usage's windows beside its own (and older files carry no source per window).
                    "source": raw.get("source") or ("get_usage" if raw.get("read_at") not in (None, data.get("read_at"))
                                                     else data.get("source", "get_usage")), "machine": machine,
                })
        def order(win):
            return (0 if weekly_all(win) else 1 if win["window"] == "weekly" else
                    2 if win["window"] == "5h" else 3, win["label"])
        account = {"plan": plan, "label": "Claude (" + plan + ")",
                   "machines": sorted(machine for machine, _ in members),
                   "windows": sorted((w for _, w in picked.values()), key=order)}
        if extras:
            account["extra_usage"] = max(extras, key=lambda e: (e[0], e[1]))[2]
        accounts.append(account)
    return accounts


class StatsStore:
    def __init__(self, directory, ttl=30):
        self.root = Path(directory) / "machines"
        self.ttl = ttl
        self.lock = threading.RLock()
        self.hashes = {}
        self.cache = {}

    def ingest(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("machine"), str) or not MACHINE.fullmatch(body["machine"]):
            raise ValueError("Invalid machine")
        rows, snapshots = body.get("rows", []), body.get("snapshots", {})
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Rows must be objects")
        if not isinstance(snapshots, dict) or any(key not in SNAPSHOTS for key in snapshots):
            raise ValueError("Invalid snapshot name")
        machine = body["machine"]
        with self.lock:
            folder = self.root / machine
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "usage.jsonl"
            if machine not in self.hashes:
                self.hashes[machine] = {digest(row) for row in rows_from(path)}
            known = self.hashes[machine]
            pending, added, duplicates = set(), [], 0
            for raw in rows:
                row = {key: value for key, value in raw.items() if key != "log"}
                hashed = digest(row)
                if hashed in known or hashed in pending:
                    duplicates += 1
                else:
                    added.append(row)
                    pending.add(hashed)
            try:
                # Repair a partial final write before appending complete records.
                if path.exists():
                    with path.open("rb+") as handle:
                        data = handle.read()
                        if data and not data.endswith(b"\n"):
                            handle.truncate(data.rfind(b"\n") + 1)
                with path.open("ab") as handle:
                    handle.write("".join(canonical(row) + "\n" for row in added).encode("utf-8"))
                    handle.flush()
                known.update(pending)
                for name, snapshot in snapshots.items():
                    atomic_json(folder / "snapshots" / (name + ".json"), snapshot)
                atomic_json(folder / "meta.json", {"last_ingest": time.time(), "rows": sum(1 for _ in rows_from(path))})
            finally:
                self.cache.clear()
            return {"ok": True, "added": len(added), "duplicates": duplicates}

    def get(self, machine="all"):
        if machine != "all" and not MACHINE.fullmatch(machine):
            raise ValueError("Invalid machine")
        with self.lock:
            cached = self.cache.get(machine)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1]
            state = self.gather(machine)
            self.cache[machine] = (time.monotonic(), state)
            return state

    def gather(self, selected="all", now=None):
        from dashboard import journal_views, window_status
        from datetime import datetime, timezone
        now = time.time() if now is None else now
        rules = route.load_roles()
        names = sorted(p.name for p in self.root.iterdir() if p.is_dir() and MACHINE.fullmatch(p.name)) if self.root.exists() else []
        machines, rows, claude = [], [], []
        for name in names:
            if selected not in ("all", name):
                continue
            folder = self.root / name
            view = {"machine": name, "pools": [], "snapshot_ages": {}, "errors": []}
            try:
                meta = read_json(folder / "meta.json", {})
                age = max(0, now - float(meta.get("last_ingest", 0)))
            except (OSError, ValueError, TypeError, AttributeError):
                age = now
                view["errors"].append("Unreadable metadata")
            view.update(age_s=age, stale=age > 900)
            snapshots = {}
            for key in SNAPSHOTS:
                try:
                    data = read_json(folder / "snapshots" / (key + ".json"), [] if key == "reservations" else {})
                    if not isinstance(data, list if key == "reservations" else dict):
                        raise ValueError()
                    snapshots[key] = data
                    stamp = data.get("fetched_at", data.get("read_at")) if isinstance(data, dict) else None
                    if stamp is not None:
                        parsed = route._parse_iso_utc(stamp) if isinstance(stamp, str) else None
                        view["snapshot_ages"][key] = max(0, now - (parsed.timestamp() if parsed else float(stamp)))
                except (OSError, ValueError, TypeError):
                    view["errors"].append("Unreadable " + key + " snapshot")
            for key, convert in (
                ("agy_usage", lambda data: route._pools_from_agy_groups(data.get("groups", []), now, rules=rules)),
                ("codex_usage", lambda data: route._pools_from_codex_usage(data, now, rules=rules)),
                ("claude_usage", lambda data: route._pools_from_claude_usage(data, now)[0]),
            ):
                try:
                    converted = convert(snapshots.get(key, {}))
                    fetched = timestamp(snapshots.get(key, {}).get("fetched_at"))
                    for pool in converted:
                        pool["snapshot_at"] = fetched
                        pool["age_s"] = max(0, now - fetched) if math.isfinite(fetched) else None
                    view["pools"].extend(converted)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    view["errors"].append("Invalid " + key + " snapshot")
            claude.append((name, snapshots.get("claude_usage", {})))
            view["pools"] = [p for p in view["pools"] if p.get("provider") != "claude-subscription"]
            try:
                route._apply_reservations_to_pools(view["pools"], snapshots.get("reservations", []))
            except (ValueError, TypeError, KeyError, AttributeError):
                view["errors"].append("Invalid reservations snapshot")
            for pool in view["pools"]:
                pool["machine"] = name
                for window in pool.get("windows", []):
                    window["status"] = window_status(window, rules)
                    window["remaining_percent"] = f"{float(window.get('remaining_fraction', 0)) * 100:.1f}%"
                    window["recent_rate"] = route.recent_rate(pool["label"], window, now, rules, history=snapshots.get("pool_history", {}))
            try:
                rows.extend(dict(row, machine=name) for row in rows_from(folder / "usage.jsonl"))
            except OSError:
                view["errors"].append("Unreadable journal")
            machines.append(view)
        accounts = pool_accounts(machines, rules)
        pools = [pool for view in machines for pool in view["pools"]]
        claude = claude_accounts(claude, now)
        chart, recent = journal_views(rows, now)
        return {"server_mode": True, "machine": selected, "machine_names": names, "machines": machines,
                "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "claude": claude, "accounts": claude + accounts,
                "cloud": {"ok": True, "pools": pools}, "chart": chart, "journal": recent}
