"""The scorecard (ASK-04 v1): writer verdict share from journal rows, offline."""
import os
import subprocess
import sys
import tempfile
import json
import unittest

import scorecard
import score

NOW = 1_800_000_000.0
DAY = 86400.0


def iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cid, days_ago, provider="codex", model="gpt-x", role="writer"):
    return {"time": iso(NOW - days_ago * DAY), "script": "writer.py", "role": role, "provider": provider,
            "model": model, "conversation": cid, "status": "ok"}


def verdict(cid, value, days_ago, provider="codex", model="gpt-x"):
    return {"time": iso(NOW - days_ago * DAY), "type": "verdict", "conversation": cid, "provider": provider,
            "model": model, "verdict": value, "note": "n"}


class CardTests(unittest.TestCase):
    def test_latest_verdict_counts_once(self):
        rows = [run("a", 5), verdict("a", "rejected", 4), verdict("a", "ok", 3), run("b", 2), verdict("b", "fixed", 1)]
        c = scorecard.cards(rows, now=NOW)[("codex", "gpt-x", "writer")]
        self.assertEqual((c["ok"], c["fixed"], c["rejected"], c["judged"], c["total"]), (1, 1, 0, 2, 2))
        self.assertEqual((c["share"], c["clean"]), (1.0, 0.5))
        self.assertTrue(c["provisional"])

    def test_old_run_joins_a_recent_verdict_and_old_verdicts_drop_out(self):
        rows = [run("a", 60), verdict("a", "ok", 2), run("b", 50), verdict("b", "rejected", 40)]
        c = scorecard.cards(rows, now=NOW)[("codex", "gpt-x", "writer")]
        self.assertEqual((c["judged"], c["ok"], c["rejected"], c["total"]), (1, 1, 0, 1))

    def test_unjudged_runs_are_in_total_only(self):
        rows = [run("a", 1), run("b", 1), verdict("a", "rejected", 0.5)]
        c = scorecard.cards(rows, now=NOW)[("codex", "gpt-x", "writer")]
        self.assertEqual((c["judged"], c["total"], c["share"]), (1, 2, 0.0))

    def test_rows_split_by_role_and_model(self):
        rows = [run("a", 1, role="writer"), verdict("a", "ok", 1), run("b", 1, role="code-writer"),
                verdict("b", "ok", 1), run("c", 1, provider="agy", model="g"), verdict("c", "fixed", 1, "agy", "g")]
        keys = set(scorecard.cards(rows, now=NOW))
        self.assertEqual(keys, {("codex", "gpt-x", "writer"), ("codex", "gpt-x", "code-writer"), ("agy", "g", "writer")})

    def test_verdict_without_a_run_keeps_its_identity(self):
        c = scorecard.cards([verdict("m", "ok", 1)], now=NOW)
        self.assertIn(("codex", "gpt-x", None), c)

    def test_threshold_and_settings(self):
        rows = []
        for i in range(10):
            rows += [run(f"c{i}", 2), verdict(f"c{i}", "ok" if i < 7 else "rejected", 1)]
        self.assertEqual(scorecard.verdict_quality("writer", "codex", "gpt-x", rows, now=NOW), 0.7)
        rules = {"scorecard": {"min_judged": 11}}
        self.assertIsNone(scorecard.verdict_quality("writer", "codex", "gpt-x", rows, rules, now=NOW))
        self.assertIsNone(scorecard.verdict_quality("review", "codex", "gpt-x", rows, now=NOW))

    def test_table(self):
        rows = [run("a", 1), verdict("a", "ok", 0.5)]
        text = scorecard.table(scorecard.cards(rows, now=NOW))
        self.assertIn("100%", text)
        self.assertIn("1/1", text)
        self.assertIn("provisional", text)
        self.assertIn("No writer verdicts", scorecard.table({}))

    def test_rows_without_verdicts_are_hidden_unless_all(self):
        rows = [run("a", 1), verdict("a", "ok", 0.5), run("r", 1, role="review")]
        found = scorecard.cards(rows, now=NOW)
        self.assertNotIn("review", scorecard.table(found).split("\n\n")[0])
        self.assertIn("1 rows with runs but no verdict are hidden", scorecard.table(found))
        self.assertIn("review", scorecard.table(found, everything=True))
        only = scorecard.cards([run("r", 1, role="review")], now=NOW)
        self.assertIn("No writer verdicts", scorecard.table(only))
        self.assertIn("--scorecard all", scorecard.table(only))


class ScoreTests(unittest.TestCase):
    def test_quality_takes_the_share_only_past_the_threshold(self):
        import time
        now = time.time()
        rows = []
        for i in range(10):
            stamp = iso(now - 2 * DAY)
            rows += [dict(run(f"c{i}", 0), time=stamp), dict(verdict(f"c{i}", "rejected" if i < 4 else "fixed", 0),
                                                            time=iso(now - DAY))]
        bench = {"models": {}, "sources": {}}
        self.assertAlmostEqual(score.quality("writer", "codex", "gpt-x", rows, {}, bench), 0.6)
        few = rows[:4]
        self.assertNotEqual(score.quality("writer", "codex", "gpt-x", few, {}, bench), 0.0)


class CliTests(unittest.TestCase):
    def test_ask_scorecard(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = os.path.join(tmp, "usage.jsonl")
            import time
            now = time.time()
            with open(journal, "w", encoding="utf-8") as handle:
                for row in (dict(run("a", 0), time=iso(now - DAY)), dict(verdict("a", "ok", 0), time=iso(now - 3600))):
                    handle.write(json.dumps(row) + "\n")
            env = dict(os.environ, ASK_CACHE_DIR=tmp, ASK_CONFIG_DIR=os.path.join(tmp, "config"), NO_COLOR="1")
            env.pop("ASK_FAKE", None)
            done = subprocess.run([sys.executable, "-B", os.path.join(os.path.dirname(__file__), "ask.py"), "--scorecard"],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("gpt-x", done.stdout)
            self.assertIn("1/1", done.stdout)


if __name__ == "__main__":
    unittest.main()
