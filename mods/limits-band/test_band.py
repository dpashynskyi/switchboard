"""Tests of what the limits band shows. python -m unittest discover -s ~/switchboard/mods/limits-band"""
import json
import os

# The tests read a frozen roles file, not the live roles.json.
os.environ["ASK_ROLES"] = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "ask"), "test_roles.json")
os.environ["ASK_LANG"] = "uk"  # the tests read the Ukrainian words; test_limits checks the English ones too
import tempfile as _tempfile
# A machine's own machine.json, roles.json and journal never decide a test.
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import band  # noqa: E402
import route  # noqa: E402

RULES = {"floors": {"weekly": 0.10, "5h": 0.10}, "pace_skip": 2.0}


def window(kind, remaining, resets_s, pace, length=604800.0):
    return {"window": kind, "remaining_fraction": remaining, "reserved_fraction": 0.0, "resets_in_s": resets_s,
            "length_s": length, "pace": pace}


class BandTests(unittest.TestCase):
    def test_preset_words(self):
        for language, words in [('en', ['manual', 'thrifty', 'strong', 'fast']),
                                ('uk', ['ручний', 'ощадливий', 'сильний', 'швидкий'])]:
            with mock.patch.dict(os.environ, ASK_LANG=language):
                for name, word in zip(['manual', 'thrifty', 'strong', 'fast'], words):
                    self.assertIn(word, band.text({'preset': name}))
                self.assertIn('custom', band.text({'preset': 'custom'}))

    def test_span(self):
        self.assertEqual(band.compact_span(4.94 * 86400), "4,9 д")
        self.assertEqual(band.compact_span(17 * 3600 + 3000), "17 год")
        self.assertEqual(band.compact_span(25 * 60), "25 хв")

    def test_pools_marks_and_skips(self):
        cs = {"pools": [
            {"provider": "agy", "id": "gemini", "label": "Gemini Models", "windows": [window("weekly", 0.44, 430000, 2.3)]},
            {"provider": "agy", "id": "cg", "label": "Claude and GPT models", "windows": [
                window("weekly", 0.48, 430000, 1.8), window("5h", 0.05, 3600, 0.5, length=18000)]},
            {"provider": "codex", "id": "codex", "label": "ChatGPT (prolite)", "windows": [window("weekly", 0.97, 515000, 0.2)]},
            {"provider": "codex", "id": "codex-2", "label": "ChatGPT", "quota_reached": True, "windows": [window("weekly", 0.5, 515000, 0.2)]},
            {"provider": "claude-subscription", "id": "all", "label": "Claude (Ultra) · all models",
             "windows": [window("weekly", 0.14, 62000, 1.0)]},
        ]}
        out = band.pools_part(cs, RULES)
        rows = dict(zip(["all", "codex", "codex-2", "cg", "gemini"], out))
        self.assertEqual(rows["all"]["name"], "Claude")
        self.assertTrue(rows["all"]["burns_first"])
        # A fast pace is a warning: the router still takes the pool when nothing calmer comes later.
        self.assertIsNone(rows["gemini"]["skip"])
        self.assertTrue(rows["gemini"]["pace_over"])
        self.assertEqual(rows["cg"]["skip"], "5 год нижче порогу")
        self.assertEqual(rows["codex-2"]["skip"], "вичерпано")
        self.assertIsNone(rows["codex"]["skip"])
        self.assertEqual(rows["cg"]["name"], "agy Claude і GPT")
        # The router does not test a stale pool, so the band does not say it refuses one.
        stale = {"provider": "claude-subscription", "id": "old", "label": "Claude (Ultra) · all models", "stale": True,
                 "windows": [window("weekly", 0.05, 62000, 3.0)]}
        self.assertEqual((band.skip_reason(stale, RULES), band.pace_over(stale, RULES)), (None, False))
        self.assertEqual([(r["provider"], r["model"]) for r in band.pools_part(cs, RULES)], [
            ("Anthropic", "Claude"), ("OpenAI", "GPT"), ("OpenAI", "GPT"),
            ("Google", "Claude і GPT"), ("Google", "Gemini")])

    def test_fable_only_when_it_differs(self):
        def reading(fable_used):
            pools, _ = route._pools_from_claude_usage({"read_at": "2026-11-04T23:00:00Z", "plan": "Ultra", "windows": [
                {"label": "Weekly · all models", "percentUsed": 86, "resetsAt": "2026-11-05T17:00:00Z"},
                {"label": "Weekly · Fable", "percentUsed": fable_used, "resetsAt": "2026-11-05T17:00:00Z"}]}, 1793833200.0)
            return [p["name"] for p in band.pools_part({"pools": pools}, RULES)]
        self.assertEqual(reading(15), ["Claude"])
        self.assertEqual(reading(95), ["Claude", "Fable"])

    def test_sessions_of_the_family(self):
        root, now = "local_root", 1793835000000
        rows = {
            root: {"cliSessionId": "cli-root"},
            "local_a": {"spawnedFrom": {"sessionId": root, "name": "a"}, "lastActivityAt": now, "lastTurnEdgeAt": now - 60000},
            "local_b": {"spawnedFrom": {"sessionId": root, "name": "b"}, "lastActivityAt": now, "lastTurnEdgeAt": now},
            "local_c": {"spawnedFrom": {"sessionId": "local_a"}, "lastActivityAt": now, "lastTurnEdgeAt": now},
            "local_old": {"spawnedFrom": {"sessionId": root}, "isArchived": True, "lastActivityAt": now, "lastTurnEdgeAt": 0},
            "local_other": {"lastActivityAt": now, "lastTurnEdgeAt": 0},
            # No turn edge yet: working only if active in the last ten minutes.
            "local_new": {"cliSessionId": "cli-new", "spawnedFrom": {"sessionId": root}, "lastActivityAt": now},
            "local_quiet": {"spawnedFrom": {"sessionId": root}, "lastActivityAt": now - 3600 * 1000},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for sid, r in rows.items():
                with open(os.path.join(tmp, sid + ".json"), "w", encoding="utf-8") as h:
                    json.dump(r, h)
            with mock.patch.object(band, "session_dirs", return_value=[tmp]), \
                    mock.patch.object(band.time, "time", return_value=now / 1000):
                # From the root: a, c and new working... a works, b and c idle, new works, quiet idle.
                self.assertEqual(band.read_sessions("cli-root"), {"working": 2, "idle": 3})
                # From a child: the root and the child itself are left out.
                self.assertEqual(band.read_sessions("cli-new"), {"working": 1, "idle": 3})
                self.assertIsNone(band.read_sessions("cli-nobody"))
                self.assertIsNone(band.read_sessions(None))

    def test_text(self):
        def pool(provider, left, **kw):
            return {"provider": provider, "left_pct": left, "burns_first": False, "skip": None, "pace_over": False, **kw}
        data = {"pools": [pool("Anthropic", 14, burns_first=True), pool("OpenAI", 97),
                          pool("Google", 48, skip="нижче порогу"), pool("Google", 44, pace_over=True)],
                "sessions": {"working": 1, "idle": 11}, "claude": {"status": "ok", "age_s": 7300}, "errors": []}
        self.assertEqual(band.text(data), "Anthropic 14% ⚠️ · OpenAI 97% · Google 48% ❌ / 44% ⚠️   │   ручний   │   "
                                          "1 сесія працює, 11 вільних   │   ⚠️ Claude: цифри 2 год тому")
        self.assertEqual(band.text({"pools": [], "sessions": {"working": 0, "idle": 0}, "claude": {}}), "квот немає   │   ручний")
        self.assertEqual([band.plural(n, "a", "b", "c") for n in (1, 2, 5, 11, 12, 21, 22, 25, 111)],
                         ["a", "b", "c", "c", "c", "a", "b", "c", "c"])


if __name__ == "__main__":
    unittest.main()
