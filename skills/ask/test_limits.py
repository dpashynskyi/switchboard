"""Tests of the limits table."""
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

import limits
import route


def window(kind, remaining, resets_s, pace, reserved=0.0, length=604800.0):
    return {"window": kind, "remaining_fraction": remaining, "reserved_fraction": reserved, "resets_in_s": resets_s,
            "length_s": length, "pace": pace}


class LimitsTests(unittest.TestCase):
    def test_span_and_names(self):
        self.assertEqual(limits.span(4 * 86400 + 23 * 3600 + 5), "4 д 23 год")
        self.assertEqual(limits.span(17 * 3600 + 48 * 60), "17 год 48 хв")
        self.assertEqual(limits.span(25 * 60), "25 хв")
        self.assertEqual(limits.pool_name({"provider": "claude-subscription", "label": "Claude (Ultra) · all models"}),
                         "Claude, усі моделі")
        self.assertEqual(limits.pool_name({"provider": "agy", "label": "Claude and GPT models"}), "agy: Claude і GPT")
        self.assertEqual(limits.pool_name({"provider": "codex", "label": "ChatGPT (prolite)"}), "ChatGPT (Codex)")

    def test_table(self):
        cs = {"pools": [
            {"provider": "agy", "label": "Gemini Models", "windows": [
                window("weekly", 0.44, 430000, 2.3), window("5h", 0.3, 3600, 0.5, length=18000)]},
            {"provider": "codex", "label": "ChatGPT (prolite)", "windows": [window("weekly", 0.97, 515000, 0.2, reserved=0.002)]},
            {"provider": "claude-subscription", "label": "Claude (Ultra) · Fable", "windows": [window("weekly", 0.85, 64000, 0.2)]},
            {"provider": "agy", "label": "Claude and GPT models", "quota_reached": True, "windows": [window("weekly", 0.05, 430000, 1.0)]},
        ], "reservations": [{"model": "gpt-6.1-sol"}], "claude_subscription": {"status": "ok"}}
        text = limits.table(cs=cs, rules={"floors": {"weekly": 0.1}, "pace_skip": 2.0}, now=0)
        lines = text.splitlines()
        rows = [l for l in lines if l.startswith("| ") and "Пул" not in l]
        self.assertEqual([r.split(" | ")[0][2:] for r in rows], ["Claude, Fable", "ChatGPT (Codex)", "agy: Claude і GPT", "agy: Gemini"])
        self.assertIn("| 97% |", rows[1])  # after reservations, rounded
        self.assertTrue(rows[2].endswith("| вичерпано |") and rows[3].endswith("| обходиться |"))
        self.assertIn("Згорить першим: Claude, Fable, 85% за 17 год 46 хв.", text)
        self.assertIn("Резерви: 1 (gpt-6.1-sol).", text)
        self.assertIn("agy: Gemini 30% (скидання через 1 год 0 хв)", text)

    def test_english_words_with_ask_lang_en(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"ASK_LANG": "en"}):
            self.assertEqual(limits.span(4 * 86400 + 23 * 3600 + 5), "4 d 23 h")
            self.assertEqual(limits.pool_name({"provider": "agy", "label": "Claude and GPT models"}), "agy: Claude and GPT")
            cs = {"pools": [{"provider": "agy", "label": "Gemini Models", "windows": [window("weekly", 0.44, 430000, 2.3)]}],
                  "reservations": [], "claude_subscription": {"status": "missing"}}
            text = limits.table(cs=cs, rules={"floors": {"weekly": 0.1}, "pace_skip": 2.0}, now=0)
        self.assertIn("| Pool | Left | Resets in | Pace | State |", text)
        self.assertIn("| agy: Gemini | 44% | 4 d 23 h | ×2.3 | skipped |", text)
        self.assertIn("Burns first: agy: Gemini, 44% in 4 d 23 h.", text)
        self.assertIn("Claude: no numbers", text)
        self.assertFalse(any("\u0400" <= ch <= "\u04ff" for ch in text))
        with mock.patch.dict(os.environ, {"ASK_LANG": "xx"}):
            self.assertEqual(limits.lang(), "en")
        with mock.patch.dict(os.environ, {"ASK_LANG": "  "}):
            self.assertEqual(limits.lang(), limits.DEFAULT_LANG)

    def test_fable_nested_in_all_models(self):
        pools, meta = route._pools_from_claude_usage({"read_at": "2026-11-03T19:00:00Z", "plan": "Ultra", "windows": [
            {"label": "Weekly · all models", "percentUsed": 86, "resetsAt": "2026-11-05T19:00:00Z"},
            {"label": "Weekly · Fable", "percentUsed": 15, "resetsAt": "2026-11-05T19:00:00Z"}]}, 1793734200.0)
        cs = {"pools": pools, "reservations": [], "claude_subscription": meta}
        text = limits.table(cs=cs, rules={"floors": {"weekly": 0.1}, "pace_skip": 2.0}, now=0)
        rows = [l for l in text.splitlines() if l.startswith("| ") and "Пул" not in l]
        self.assertTrue(any(r.startswith("| Claude, Fable | 14% |") for r in rows), rows)
        self.assertIn("Згорить першим: Claude, усі моделі, 14%", text)
        self.assertNotIn("Fable, 85%", text)
        self.assertIn("Claude, Fable входить у «усі моделі»: власний залишок 85%, доступно 14% (менше з двох).", text)

    def test_claude_usage_from_the_whole_reply_or_its_plan(self):
        plan = {"status": "ok", "plan": "Ultra", "windows": [
            {"label": "Weekly · all models", "percentUsed": 86, "resetsAt": "2026-11-05T16:59:59Z", "resetsIn": "17h"}]}
        for raw in (json.dumps({"plan": plan, "context": {}}), json.dumps(plan)):
            with self.subTest(raw=raw[:20]), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "claude_usage.json")
                limits.write_claude_usage(raw, path)
                with open(path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.assertEqual((saved["plan"], saved["windows"][0]["percentUsed"]), ("Ultra", 86))
                self.assertNotIn("resetsIn", saved["windows"][0])
        with self.assertRaises(ValueError):
            limits.write_claude_usage(json.dumps({"plan": {"status": "unavailable"}}), os.devnull)

    def test_claude_json_from_stdin_is_utf8_whatever_the_console(self):
        import io
        from unittest import mock
        reply = {"plan": "Ultra", "windows": [{"label": "Weekly · all models", "percentUsed": 17}]}
        # A Ukrainian Windows reads a pipe as cp1251: "·" came out as "В·".
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(reply, ensure_ascii=False).encode("utf-8")), encoding="cp1251")
        with mock.patch("sys.stdin", stdin), mock.patch.object(limits, "table", return_value=""), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(limits.main(["--claude-json", "-"]), 0)
        with open(route.CLAUDE_USAGE_PATH, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["windows"][0]["label"], "Weekly · all models")


if __name__ == "__main__":
    unittest.main()
