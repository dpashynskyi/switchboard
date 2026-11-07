"""Offline checks for the Claude status-line usage cache."""
import contextlib
import io
import json
import os

# The tests read a frozen roles file, not the live roles.json.
os.environ["ASK_ROLES"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_roles.json")
os.environ["ASK_LANG"] = "uk"  # the tests read the Ukrainian words; test_limits checks the English ones too
import tempfile as _tempfile
# A machine's own machine.json, roles.json and journal never decide a test.
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import limits
import route
import statusline_usage


NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
WEEKLY = statusline_usage.WEEKLY_LABEL
RESET = NOW + timedelta(hours=5)


class StatuslineUsageTests(unittest.TestCase):
    def setUp(self):
        room = tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(__file__)))
        self.addCleanup(room.cleanup)
        self.path = os.path.join(room.name, "cache", "claude_usage.json")
        env = mock.patch.dict(os.environ, {"ASK_CLAUDE_USAGE_PATH": self.path})
        env.start()
        self.addCleanup(env.stop)

    def payload(self, reset=None, percent=88.6):
        return {"model": {"display_name": "Opus 5.5"}, "rate_limits": {
            "seven_day": {"used_percentage": percent, "resets_at": reset or RESET.timestamp()},
            "five_hour": {"used_percentage": 40},
        }}

    def run_main(self, data, now=NOW):
        out, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
            code = statusline_usage.main(data if isinstance(data, str) else json.dumps(data), now=now)
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        return out.getvalue()

    def saved(self):
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)

    def seed(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_epoch_iso_and_millisecond_resets(self):
        for reset in (RESET.timestamp(), RESET.timestamp() * 1000,
                      "2026-11-05T17:00:00Z", "2026-11-05T19:00:00+02:00"):
            with self.subTest(reset=reset):
                output = self.run_main(self.payload(reset))
                self.assertEqual(output, "Opus 5.5 · тиждень: лишилось 11%, скидання {} · 5 год: лишилось 60%\n".format(
                    RESET.astimezone().strftime("%H:%M")))
                data = self.saved()
                self.assertEqual(data, {
                    "read_at": "2026-11-05T12:00:00Z", "plan": "subscription", "source": "statusline",
                    "windows": [{"label": "Weekly · all models", "percentUsed": 89,
                                 "resetsAt": "2026-11-05T17:00:00Z"}],
                })

    def test_reset_beyond_24_hours_includes_local_weekday(self):
        reset = NOW + timedelta(hours=25)
        output = self.run_main(self.payload(reset.timestamp()))
        local = reset.astimezone()
        day = statusline_usage.LINE_WORDS["uk"]["days"][local.weekday()]
        self.assertIn("скидання {} {} ·".format(day, local.strftime("%H:%M")), output)
        at_boundary = NOW + timedelta(hours=24)
        output = self.run_main(self.payload(at_boundary.timestamp()))
        self.assertIn("скидання {} ·".format(at_boundary.astimezone().strftime("%H:%M")), output)

    def test_missing_rate_limits_prints_model_without_writing(self):
        self.assertEqual(self.run_main({"model": {"display_name": "Opus 5.5"}}), "Opus 5.5\n")
        self.assertFalse(os.path.exists(self.path))

    def test_garbage_and_missing_fields_are_harmless(self):
        for data in ("{garbage", "", "null", "[]", {}, {"model": None, "rate_limits": []},
                     {"rate_limits": {"seven_day": None, "five_hour": "unknown"}}):
            with self.subTest(data=data):
                self.assertEqual(self.run_main(data), "\n")
                self.assertFalse(os.path.exists(self.path))

    def test_only_five_hour_usage_is_displayed_without_writing(self):
        self.assertEqual(self.run_main({"rate_limits": {"five_hour": {"used_percentage": 0}}}),
                         "5 год: лишилось 100%\n")
        self.assertFalse(os.path.exists(self.path))

    def test_missing_or_invalid_reset_still_saves_weekly_percentage(self):
        for reset in (None, "not a date", True, float("inf"), 10**100):
            with self.subTest(reset=reset):
                data = {"rate_limits": {"seven_day": {"used_percentage": 0, "resets_at": reset}}}
                self.assertEqual(self.run_main(data), "тиждень: лишилось 100%\n")
                self.assertIsNone(self.saved()["windows"][0]["resetsAt"])

    def test_invalid_percentages_do_not_write(self):
        for percent in (None, "40", True, float("nan"), float("inf"), 10**1000):
            with self.subTest(percent=str(percent)):
                self.assertEqual(self.run_main(self.payload(percent=percent)), "Opus 5.5 · 5 год: лишилось 60%\n")
                self.assertFalse(os.path.exists(self.path))

    def test_future_fable_kept_and_expired_or_unknown_windows_dropped(self):
        fable = {"label": "Weekly · Fable", "percentUsed": 15, "resetsAt": "2026-11-06T17:00:00Z"}
        self.seed({"plan": "Ultra", "windows": [
            fable,
            {"label": "Weekly · expired", "resetsAt": "2026-11-05T11:59:59Z"},
            {"label": "Weekly · boundary", "resetsAt": "2026-11-05T12:00:00Z"},
            {"label": "Weekly · unknown"},
            None,
        ]})
        self.run_main(self.payload())
        self.assertEqual(self.saved()["plan"], "Ultra")
        self.assertEqual(self.saved()["windows"][1:], [fable])
        self.run_main(self.payload(), now=NOW + timedelta(days=2))
        self.assertEqual(len(self.saved()["windows"]), 1)

    def test_five_hour_reset_english_words_and_clamped_percent(self):
        data = {"rate_limits": {"seven_day": {"used_percentage": 104},
                                "five_hour": {"used_percentage": 25, "resets_at": RESET.timestamp()}}}
        with mock.patch.dict(os.environ, {"ASK_LANG": "en"}):
            self.assertEqual(self.run_main(data), "week: 0% left · 5h: 75% left, resets {}\n".format(
                RESET.astimezone().strftime("%H:%M")))
        with mock.patch.dict(os.environ, {"ASK_LANG": "xx"}):
            self.assertIn("week: 0% left", self.run_main(data))

    def test_no_rewrite_when_unchanged_and_fresh(self):
        self.run_main(self.payload())
        with open(self.path, "rb") as handle:
            before = handle.read()
        with mock.patch.object(statusline_usage.os, "replace") as replace:
            self.run_main(self.payload(percent=89.1), now=NOW + timedelta(seconds=300))
        replace.assert_not_called()
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_rewrite_when_older_than_300_seconds(self):
        self.run_main(self.payload())
        self.run_main(self.payload(), now=NOW + timedelta(seconds=301))
        self.assertEqual(self.saved()["read_at"], "2026-11-05T12:05:01Z")

    def test_weekly_change_rewrites_while_fresh(self):
        self.run_main(self.payload())
        self.run_main(self.payload(percent=90))
        self.assertEqual(self.saved()["windows"][0]["percentUsed"], 90)
        self.run_main(self.payload("2026-11-06T17:00:00Z", percent=90))
        self.assertEqual(self.saved()["windows"][0]["resetsAt"], "2026-11-06T17:00:00Z")

    def test_bad_cache_is_replaced(self):
        for previous in ("{broken", "[]", '{"windows": null, "read_at": "bad"}'):
            with self.subTest(previous=previous):
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(previous)
                self.run_main(self.payload())
                self.assertEqual(self.saved()["source"], "statusline")

    def test_atomic_replace_uses_same_directory(self):
        real_replace = os.replace

        def replace(source, destination):
            self.assertEqual(os.path.dirname(source), os.path.dirname(destination))
            with open(source, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["source"], "statusline")
            real_replace(source, destination)

        with mock.patch.object(statusline_usage.os, "replace", side_effect=replace) as replace_call:
            self.run_main(self.payload())
        replace_call.assert_called_once()
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["claude_usage.json"])

    def test_failed_write_preserves_cache_and_status_line(self):
        self.run_main(self.payload())
        before = self.saved()
        with mock.patch.object(statusline_usage.os, "replace", side_effect=OSError("unwritable")):
            self.assertIn("тиждень: лишилось 10%", self.run_main(self.payload(percent=90)))
        self.assertEqual(self.saved(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["claude_usage.json"])
        with mock.patch.object(statusline_usage.os, "makedirs", side_effect=OSError("unwritable")):
            self.assertIn("тиждень: лишилось 10%", self.run_main(self.payload(percent=90)))

    def test_old_get_usage_windows_are_not_passed_off_as_fresh(self):
        fable = {"label": "Weekly · Fable", "percentUsed": 95, "resetsAt": "2026-11-12T17:00:00Z"}
        self.seed({"plan": "Ultra", "read_at": (NOW - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "windows": [fable]})
        self.run_main(self.payload())
        self.assertEqual([w["label"] for w in self.saved()["windows"]], [WEEKLY])  # 20 h old: dropped, not refreshed
        self.seed({"plan": "Ultra", "read_at": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "windows": [fable]})
        self.run_main(self.payload())
        kept = self.saved()["windows"][1]
        self.assertEqual(kept["read_at"], (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        pools, meta = route.load_claude_usage(self.path, now=(NOW + timedelta(hours=5, minutes=30)).timestamp())
        by_id = {p["id"]: p for p in pools}
        self.assertFalse(by_id["claude-sub-all-models"]["stale"])
        self.assertTrue(by_id["claude-sub-fable"]["stale"])  # 6.5 h by its own read_at, while the file is 5.5 h old
        self.assertNotIn("nested_in", by_id["claude-sub-fable"])
        # An unreadable read_at never borrows the file's freshness; "stale": false in the file does not override age.
        self.seed({"plan": "Ultra", "read_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "stale": False,
                   "windows": [dict(fable, read_at="invalid"), dict(fable, label="Weekly · Old", read_at=(NOW - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))]})
        pools, _ = route.load_claude_usage(self.path, now=NOW.timestamp())
        self.assertEqual([p["stale"] for p in pools], [True, True])
        self.run_main(self.payload())
        self.assertEqual([w["label"] for w in self.saved()["windows"]], [WEEKLY])  # unreadable or 7 h old: neither is kept

    def test_saved_output_loads_as_router_all_models_pool(self):
        self.run_main(self.payload())
        pools, meta = route.load_claude_usage(self.path, now=NOW.timestamp())
        self.assertEqual(meta["status"], "ok")
        self.assertEqual([p["id"] for p in pools], ["claude-sub-all-models"])
        self.assertAlmostEqual(pools[0]["windows"][0]["remaining_fraction"], 0.11)

    def test_get_usage_writer_sets_source(self):
        limits.write_claude_usage(json.dumps({"plan": "Ultra", "windows": [
            {"label": "Weekly · Fable", "percentUsed": 15, "resetsAt": "2026-11-06T17:00:00Z"},
        ]}), path=self.path)
        self.assertEqual(self.saved()["source"], "get_usage")


if __name__ == "__main__":
    unittest.main()
