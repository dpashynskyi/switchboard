"""File-only shared dashboard storage. No provider discovery or live probes."""
import hashlib
import json
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
        machines, rows, pools = [], [], []
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
                    view["pools"].extend(convert(snapshots.get(key, {})))
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    view["errors"].append("Invalid " + key + " snapshot")
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
            pools.extend(view["pools"])
            machines.append(view)
        chart, recent = journal_views(rows, now)
        return {"server_mode": True, "machine": selected, "machine_names": names, "machines": machines,
                "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "cloud": {"ok": True, "pools": pools}, "chart": chart, "journal": recent}
