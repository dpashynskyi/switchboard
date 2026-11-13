"""Push queued journal rows and changed quota snapshots to a private stats server."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.parse
import urllib.request

import route
from stats_store import MACHINE, MAX_BODY, atomic_json, canonical

BATCH_BYTES = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(url, token, body):
    request = urllib.request.Request(url.rstrip("/") + "/ingest", data=canonical(body).encode("utf-8"),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    # Do not send a private server's token through environment-configured proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise OSError("Push rejected")


def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def journal_batches(path, state, machine):
    """Yield rows, end byte offset, and skipped count. Partial lines stay queued."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return
    with handle:
        size = os.fstat(handle.fileno()).st_size
        offset = int(state.get("offset", 0))
        head_len = int(state.get("head_len", min(size, 4096)))
        head = hashlib.sha256(handle.read(head_len)).hexdigest()
        if offset > size or offset < 0 or head != state.get("head", head):
            offset = 0
        handle.seek(offset)
        rows, skipped = [], 0
        envelope_bytes = len(canonical({"machine": machine, "rows": []}).encode("utf-8"))
        batch_bytes = envelope_bytes
        end = offset
        while True:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                break
            next_offset = handle.tell()
            try:
                row = json.loads(line.rstrip(b"\r\n"))
                if not isinstance(row, dict):
                    raise ValueError()
                row.pop("log", None)
                row_bytes = len(canonical(row).encode("utf-8"))
                if row_bytes + envelope_bytes > MAX_BODY:
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                skipped += 1
                end = next_offset
                continue
            if rows and (len(rows) >= 2000 or batch_bytes + 1 + row_bytes > BATCH_BYTES):
                yield rows, end, skipped
                rows, skipped = [], 0
                batch_bytes = envelope_bytes
            batch_bytes += row_bytes + (1 if rows else 0)
            rows.append(row)
            end = next_offset
        if rows or end != offset:
            yield rows, end, skipped


def head_state(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read(4096)
    except FileNotFoundError:
        data = b""
    return {"head_len": len(data), "head": hashlib.sha256(data).hexdigest()}


def snapshot_changes(state):
    snapshots, hashes = {}, {}
    paths = {"agy_usage": route.USAGE_CACHE, "codex_usage": route.CODEX_USAGE_CACHE,
             "claude_usage": route.CLAUDE_USAGE_PATH, "pool_history": route.POOL_HISTORY,
             "reservations": route.RESERVATIONS_PATH}
    for name, path in paths.items():
        if not os.path.exists(path):
            continue
        try:
            value = route.read_reservations() if name == "reservations" else json.loads(Path(path).read_text(encoding="utf-8"))
            hashed = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
            if hashed != state.get("snapshots", {}).get(name):
                snapshots[name], hashes[name] = value, hashed
        except (OSError, ValueError, TypeError):
            continue
    return snapshots, hashes


def push_once(config, token, args):
    state_path = Path(route.CACHE_DIR) / "stats_push.json"
    state = load_state(state_path)
    machine = config["machine"]
    skipped_total, rows_total = 0, 0
    snapshots, hashes = snapshot_changes(state)
    sent_rows = False
    # Capture the head before reading: appends during a push must not become part of its rotation fingerprint.
    fingerprint = head_state(route.JOURNAL_PATH)
    if not args.snapshots_only:
        for rows, end, skipped in journal_batches(route.JOURNAL_PATH, state, machine):
            skipped_total += skipped
            rows_total += len(rows)
            if not args.dry_run:
                if rows:
                    post(config["url"], token, {"machine": machine, "rows": rows})
                    sent_rows = True
                state.update(fingerprint, offset=end)
                atomic_json(state_path, state)
    should_snapshot = snapshots and (sent_rows or time.time() - float(state.get("snapshot_at", 0)) >= 60)
    if args.dry_run:
        print(f"Would send {rows_total} rows; skipped {skipped_total}; snapshots: " + ", ".join(sorted(snapshots)))
        return
    if should_snapshot:
        post(config["url"], token, {"machine": machine, "snapshots": snapshots})
        state.setdefault("snapshots", {}).update(hashes)
        state["snapshot_at"] = time.time()
        atomic_json(state_path, state)
    if not args.quiet:
        print(f"Sent {rows_total} rows; skipped {skipped_total}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--snapshots-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    machine = route.load_machine()
    if "stats" not in machine:
        return 0
    try:
        config = dict(machine["stats"])
        url = urllib.parse.urlsplit(config.get("url", ""))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        fallback = re.sub(r"[^a-z0-9_-]", "", socket.gethostname().split(".")[0].lower()).lstrip("_-")[:32] or "machine"
        config["machine"] = config.get("machine", fallback)
        if not isinstance(config["machine"], str) or not MACHINE.fullmatch(config["machine"]):
            raise ValueError()
        token = Path(os.path.expanduser(config["token_file"])).read_text(encoding="utf-8").strip()
        if not token or "\n" in token or "\r" in token:
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        print("Stats configuration or token_file is missing, empty or invalid", file=sys.stderr)
        return 2
    lock = os.path.join(route.CACHE_DIR, "stats_push.lock")
    marker = Path(route.CACHE_DIR) / "stats_push.again"
    try:
        # Unlike the router's millisecond locks a push can take over 60 seconds;
        # a live holder must never be evicted merely because it is old.
        held = acquire_lock(lock)
        if not held:
            marker.touch()
            return 0
        for take in range(2):
            try:
                for attempt in range(3 if take == 0 else 1):
                    if attempt or take:
                        marker.unlink(missing_ok=True)
                    push_once(config, token, args)
                    if not marker.exists() or args.dry_run:
                        break
            finally:
                route._release_file_lock(lock)
            if take or not marker.exists() or args.dry_run or not acquire_lock(lock):
                break
    except Exception:
        if not args.quiet:
            print("Stats push failed; queued rows will be retried", file=sys.stderr)
        return 1
    return 0


def acquire_lock(path):
    """Nonblocking PID lock; reclaim only a confirmed dead holder."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                pid = int(Path(path).read_text(encoding="utf-8"))
                if attempt == 0 and pid > 0 and not route.pid_alive(pid):
                    # Use the router's takeover serialization, but only for a dead holder.
                    return route._acquire_file_lock(path, timeout_s=0)
            except (OSError, ValueError):
                pass
            return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
