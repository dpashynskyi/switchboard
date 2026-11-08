"""Refresh the weekly Claude usage cache from status-line stdin, without a model call."""
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone


WEEKLY_LABEL = "Weekly · all models"
# The words a person reads, in the language ASK_LANG names (as in limits.py, not imported: the line runs often).
LINE_WORDS = {
    "uk": {"week": "тиждень", "five_hour": "5 год", "left": "лишилось {pct}%", "reset": "скидання {at}",
           "days": ("пн", "вт", "ср", "чт", "пт", "сб", "нд")},
    "en": {"week": "week", "five_hour": "5h", "left": "{pct}% left", "reset": "resets {at}",
           "days": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")},
}
STALE_S = 6 * 3600  # the router's CLAUDE_STALE_SECONDS: a window older than this is not passed off as fresh


def _object(value):
    return value if isinstance(value, dict) else {}


def _reset(value):
    """Accept epoch seconds/milliseconds or ISO-8601; return an aware UTC datetime."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 1e12:
                value /= 1000
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)
            return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _percent(window):
    value = window.get("used_percentage")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return round(value)
        except (ValueError, OverflowError):
            pass
    return None


def _words():
    value = (os.environ.get("ASK_LANG") or "").strip().lower()
    return LINE_WORDS.get(value, LINE_WORDS["en"])


def _window_text(name, window, now, words):
    """'week: 59% left, resets Thu 17:00' for one window, or None when it has no usable percentage."""
    used = _percent(window)
    if used is None:
        return None
    text = "{}: {}".format(words[name], words["left"].format(pct=min(100, max(0, 100 - used))))
    reset = _reset(window.get("resets_at"))
    if reset is not None:
        local = reset.astimezone()
        at = local.strftime("%H:%M")
        if (reset - now).total_seconds() > 86400:
            at = "{} {}".format(words["days"][local.weekday()], at)
        text += ", " + words["reset"].format(at=at)
    return text


def _write_usage(percent, reset, now):
    path = os.path.expanduser(os.environ.get("ASK_CLAUDE_USAGE_PATH") or
                              "~/.cache/ask/claude_usage.json")
    try:
        with open(path, encoding="utf-8") as handle:
            previous = _object(json.load(handle))
    except (OSError, ValueError):
        previous = {}
    windows = previous.get("windows")
    windows = windows if isinstance(windows, list) else []
    old_weekly = next((w for w in windows if isinstance(w, dict) and
                       w.get("label") == WEEKLY_LABEL), {})
    reset_iso = reset.isoformat().replace("+00:00", "Z") if reset else None
    read_at = _reset(previous.get("read_at"))
    if (old_weekly.get("percentUsed") == percent and old_weekly.get("resetsAt") == reset_iso
            and read_at is not None and (now - read_at).total_seconds() <= 300):
        return

    kept = []
    for window in windows:
        if not isinstance(window, dict) or window.get("label") == WEEKLY_LABEL:
            continue
        expires = _reset(window.get("resetsAt"))
        if expires is None or expires <= now:
            continue
        # The file's read_at becomes this write's; a window the status line did not read keeps the time it was read
        # (get_usage), so the router judges it by its own age, not by the fresh weekly number beside it.
        window = dict(window)
        own = _reset(window.get("read_at"))
        if own is None:
            if window.get("read_at") is not None:
                continue  # an unreadable time of its own: the window's age is unknown, so it is not kept as fresh
            if read_at is not None:
                window["read_at"], own = previous.get("read_at"), read_at  # the file's time becomes the window's
        if own is not None and (now - own).total_seconds() > STALE_S:
            continue
        kept.append(window)
    entry = {
        "plan": previous.get("plan", "subscription"),
        "read_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "statusline",
        "windows": [{"label": WEEKLY_LABEL, "percentUsed": percent, "resetsAt": reset_iso}] + kept,
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".claude_usage-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            json.dump(entry, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def main(stdin_text, now=None):
    """Print one status line and best-effort cache usage; always return success."""
    parts = []
    try:
        data = _object(json.loads(stdin_text))
        now = datetime.now(timezone.utc) if now is None else now
        if not isinstance(now, datetime):
            now = datetime.fromtimestamp(now, timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        model = _object(data.get("model")).get("display_name")
        if isinstance(model, str) and model.strip():
            parts.append(" ".join(model.split()))
        rates = _object(data.get("rate_limits"))
        weekly = _object(rates.get("seven_day"))
        percent = _percent(weekly)
        reset = _reset(weekly.get("resets_at"))
        words = _words()
        for name, window in (("week", weekly), ("five_hour", _object(rates.get("five_hour")))):
            text = _window_text(name, window, now, words)
            if text is not None:
                parts.append(text)
        if percent is not None:
            try:
                _write_usage(percent, reset, now)
            except Exception:
                pass  # A cache failure must not break the displayed status line.
    except Exception:
        pass
    try:
        print(" · ".join(parts), flush=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    # Claude Code speaks UTF-8 both ways, whatever the console's code page (cp1251 on a Ukrainian Windows).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    try:
        stdin_text = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    except Exception:
        stdin_text = ""
    sys.exit(main(stdin_text))
