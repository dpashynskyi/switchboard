"""Offline evidence and ordering tests; the session runs Python."""
import copy
import io
import os

# The tests read a frozen roles file, not the live roles.json.
os.environ["ASK_ROLES"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_roles.json")
os.environ["ASK_LANG"] = "uk"  # the tests read the Ukrainian words; test_limits checks the English ones too
import tempfile as _tempfile
# A machine's own machine.json, roles.json and journal never decide a test.
os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import experience
import route


NOW = 1793836800.0


def rules_fixture():
    return {
        "experience": {
            "enabled": True,
            "values": {"ok": 1.0, "fixed": 0.6, "rejected": 0.0, "capped": 0.2},
            "prior": {"scale": 0.5, "center": 0.75, "lo": 0.3, "hi": 0.7},
            "prior_weight": 2, "other_role_weight": 0.25, "window_days": 30,
            "min_evidence": 2, "demote_below": 0.5, "demote_twice_below": 0.35,
            "promote_min_evidence": 3, "promote_above": 0.8,
            "probation_verdicts": 3, "probation_tokens": 200000,
            "role_category": {"analysis": "code"},
        },
        "roles": {"analysis": {"models": [model("a"), model("b"), model("c")]}},
        "tokens_default": {"analysis": 10000},
    }


def model(mid):
    return {"provider": "codex", "model": mid}


def run(cid="one", role="analysis", mid="a", **kwargs):
    return dict({"conversation": cid, "role": role, "model": mid, "provider": "codex",
                 "ts": NOW - 100, "status": "ok"}, **kwargs)


def verdict(cid="one", value="rejected", mid="a", **kwargs):
    return dict({"conversation": cid, "type": "verdict", "model": mid, "provider": "codex",
                 "ts": NOW - 50, "verdict": value}, **kwargs)


def rejected(mid="a"):
    return [run(str(i), mid=mid) for i in range(2)] + [verdict(str(i), mid=mid) for i in range(2)]


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.rules = rules_fixture()
        self.bench = {
            "sources": {"one": {"category": "code", "top": 100, "read_at": "2026-11-05"},
                        "two": {"category": "code", "top": 50, "read_at": "2026-11-05"}},
            "models": {"a": {"one": 80, "two": 30}, "b": {"one": 90}},
        }

    def test_family(self):
        for suffix in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(experience.family("gemini-4-argon-" + suffix), "gemini-4-argon")
        self.assertEqual(experience.family("claude-sonnet-5-5-high"), "claude-sonnet-5-5")
        self.assertEqual(experience.family("gpt-6-astra"), "gpt-6-astra")
        self.assertEqual(experience.family("ollama:tag-high"), "ollama:tag-high")

    def test_load_missing_broken_and_valid(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertEqual(experience.load_benchmarks(), {})
        for content in ("{", "[]", '{"sources": []}', '{"models": null}'):
            with mock.patch("builtins.open", mock.mock_open(read_data=content)):
                self.assertEqual(experience.load_benchmarks("unused"), {})
        with mock.patch("builtins.open", mock.mock_open(read_data='{"sources": {}, "models": {}}')):
            self.assertEqual(experience.load_benchmarks(), {"sources": {}, "models": {}})

    def test_prior_sources_missing_category_and_clipping(self):
        # Mean .8 and .6 is .7; .5 + .5 * (7 - .75) = .475.
        self.assertAlmostEqual(experience.prior("a-high", "analysis", self.rules, self.bench), .475)
        self.assertAlmostEqual(experience.prior("b", "analysis", self.rules, self.bench), .575)
        self.assertEqual(experience.prior("absent", "analysis", self.rules, self.bench), .5)
        self.assertEqual(experience.prior("a", "draft", self.rules, self.bench), .5)
        self.bench["models"]["a"] = {"one": 0}
        self.assertEqual(experience.prior("a", "analysis", self.rules, self.bench), .3)
        self.bench["models"]["a"] = {"one": 200}
        self.assertEqual(experience.prior("a", "analysis", self.rules, self.bench), .7)
        self.bench["sources"]["one"]["top"] = 0
        self.assertEqual(experience.prior("a", "analysis", self.rules, self.bench), .5)

    def test_evidence_role_latest_run_or_none(self):
        rows = [run(role="review", ts=NOW - 200), verdict(value="ok"), run(),
                verdict("manual", "fixed")]
        e = experience.evidence("analysis", "a", "codex", rows, self.rules, NOW)
        self.assertEqual(e["n"], 1.25)
        self.assertEqual(e["n_role"], 1)
        self.assertAlmostEqual(e["sum"], 1.15)
        self.assertEqual(e["items"][0], {"verdict": "ok", "role": "analysis", "conversation": "one", "time": NOW - 50})
        self.assertIsNone(e["items"][1]["role"])
        old_run = run(ts=NOW - 40 * 86400)
        self.assertEqual(experience.evidence("analysis", "a", "codex", [old_run, verdict()], self.rules, NOW)["n_role"], 1)

    def test_capped_superseded_and_other_roles(self):
        rows = [run(status="capped"), run("two", "review", status="capped"), verdict(value="fixed")]
        e = experience.evidence("analysis", "a", "codex", rows, self.rules, NOW)
        self.assertEqual(e["n"], 1.25)
        self.assertAlmostEqual(e["sum"], .65)
        self.assertEqual([i["verdict"] for i in e["items"]], ["capped", "fixed"])
        self.assertEqual(e["n_role"], 1)

    def test_timeout_counts_deadline_does_not(self):
        # ask.py rows carry no conversation; a timeout still counts, a writer's --deadline never does.
        rows = [run(None, "analysis", status="timeout"), run("two", status="timeout"),
                run("three", status="deadline"), run("four", status="error")]
        e = experience.evidence("analysis", "a", "codex", rows, self.rules, NOW)
        self.assertEqual([i["verdict"] for i in e["items"]], ["timeout", "timeout"])
        self.assertAlmostEqual(e["sum"], .4)
        self.assertEqual(e["n_role"], 2)
        self.assertEqual(experience._verdict_count(e), 0)
        judged = experience.evidence("analysis", "a", "codex", rows + [verdict("two", "ok")], self.rules, NOW)
        self.assertEqual(sorted(i["verdict"] for i in judged["items"]), ["ok", "timeout"])
        self.assertIn("2 timeout", experience.summary({"q": .3, "q0": .5, "evidence": e}))

    def test_two_review_timeouts_demote_the_first_row(self):
        # The fixture's values have no "timeout": the default 0.2 fills in. Prior .5: two give q .35 (down one),
        # three give q .32 (down two).
        raw = self.rules["roles"]["analysis"]["models"]
        rows = [run(None, mid="a", status="timeout") for _ in range(2)]
        ordered, notes = experience.reorder("analysis", raw, self.rules, rows, {}, tokens=0, now=NOW)
        self.assertEqual([m["model"] for m in ordered], ["b", "a", "c"])
        self.assertTrue(any(n.startswith("a down 1") and "2 timeout" in n for n in notes), notes)
        ordered, _ = experience.reorder("analysis", raw, self.rules, rows + rows[:1], {}, tokens=0, now=NOW)
        self.assertEqual([m["model"] for m in ordered], ["b", "c", "a"])

    def test_window_identity_and_invalid_rows(self):
        rows = [run(status="capped", ts=NOW - 30 * 86400),
                run("old", status="capped", ts=NOW - 30 * 86400 - 1),
                run("future", status="capped", ts=NOW + 1),
                run("wrong-id", mid="a-high", status="capped"),
                run("wrong-provider", provider="agy", status="capped"),
                verdict("old-verdict", ts=NOW - 30 * 86400 - 1),
                verdict("other-verdict", mid="a-high"),
                run("bad-time", ts=None, time="broken", status="capped"), None]
        e = experience.evidence("analysis", "a", "codex", rows, self.rules, NOW)
        self.assertEqual(e["n"], 1)
        self.assertEqual(e["sum"], .2)
        iso = run(status="capped", ts=None, time="2026-11-05T00:00:00Z")
        self.assertEqual(experience.evidence("analysis", "a", "codex", [iso], self.rules, NOW)["n"], 1)

    def test_quality_worked_example_and_estimate(self):
        rows = [run(), verdict(value="ok"), run("two", "review"), verdict("two", "fixed")]
        # q0=.475, k=2, n=1+.25=1.25, sum=1+.25*.6=1.15;
        # q=(2*.475+1.15)/(2+1.25)=2.1/3.25.
        with mock.patch.object(route, "estimate_tokens", return_value=311000) as estimate:
            q = experience.quality("analysis", "a", "codex", rows, self.rules, self.bench, NOW)
        self.assertAlmostEqual(q["q"], 2.1 / 3.25)
        self.assertAlmostEqual(q["q0"], .475)
        self.assertEqual(q["shift"], 1)
        self.assertEqual(estimate.call_args.kwargs["fake"], {"journal": rows})
        self.assertEqual(estimate.call_args.kwargs["provider"], "codex")
        self.assertEqual(estimate.call_args.kwargs["model"], "a")

    def test_shift_boundaries_and_probation(self):
        e = {"n_role": 2, "items": [{"verdict": "rejected"}] * 2}
        for q, expected in ((.349999, 2), (.35, 1), (.499999, 1), (.5, 0)):
            self.assertEqual(experience.shift(q, e, self.rules), expected)
        e["n_role"] = 1
        self.assertEqual(experience.shift(0, e, self.rules), 0)
        e["n_role"] = 2
        self.assertEqual(experience.shift(.8, e, self.rules), 0)
        e["n_role"] = 3
        self.assertEqual(experience.shift(.799999, e, self.rules), 0)
        self.assertEqual(experience.shift(.8, e, self.rules), -1)
        self.assertEqual(experience.shift(.8, e, self.rules, 199999), -1)
        self.assertEqual(experience.shift(.8, e, self.rules, 200000), 1)
        self.assertEqual(experience.shift(.2, e, self.rules, 200000), 2)  # never more than two places
        e["items"].append({"verdict": "ok"})
        self.assertEqual(experience.shift(.8, e, self.rules, 200000), -1)
        e = {"n_role": 0, "items": []}
        self.assertEqual(experience.shift(.5, e, self.rules, 199999), 0)
        self.assertEqual(experience.shift(.5, e, self.rules, 200000), 1)
        elsewhere = experience.evidence("analysis", "a", "codex",
                                         [verdict(str(i), "ok") for i in range(3)], self.rules, NOW)
        self.assertEqual(elsewhere["n_role"], 0)
        self.assertEqual(experience.shift(.5, elsewhere, self.rules, 200000), 0)

    def test_reorder_demotion_ties_notes_and_no_mutation(self):
        raw = self.rules["roles"]["analysis"]["models"]
        before = copy.deepcopy((raw, self.rules))
        ordered, notes = experience.reorder("analysis", raw, self.rules, rejected(), {}, tokens=0, now=NOW)
        # q 0.25 is under demote_twice_below: two places down (it tied at one before the half-place rule).
        self.assertEqual(ordered, [raw[1], raw[2], raw[0]])
        self.assertIn("a down 2 in analysis: q 0.25 (prior 0.50; 2 rejected)", notes)
        self.assertEqual(len(notes), 3)
        self.assertEqual((raw, self.rules), before)
        rows = [run(), verdict(value="fixed"), run("two"), verdict("two")]
        # q 0.40 (one fixed, one rejected) is under demote_below: one place down, past b, not tied with it.
        ordered, notes = experience.reorder("analysis", raw, self.rules, rows, {}, tokens=0, now=NOW)
        self.assertEqual(ordered, [raw[1], raw[0], raw[2]])
        self.assertTrue(any(note.startswith("a down 1 in analysis: q 0.40") for note in notes))
        rows = [run(), verdict(value="ok"), run("two"), verdict("two", "fixed")]
        self.assertEqual(experience.reorder("analysis", raw, self.rules, rows, {}, tokens=0, now=NOW), (raw, []))

    def test_probation_note_and_promotion(self):
        raw = self.rules["roles"]["analysis"]["models"]
        rows = [run(str(i), mid="b") for i in range(5)] + [verdict(str(i), "ok", "b") for i in range(5)]
        ordered, notes = experience.reorder("analysis", raw, self.rules, rows, {}, tokens=311000, now=NOW)
        self.assertEqual(ordered[0], raw[1])
        self.assertTrue(any("probation (0 verdicts, est. 311K billed)" in note for note in notes))
        self.assertTrue(any("b up 1" in note for note in notes))

    def test_promotion_alone_passes_its_neighbour(self):
        # A shift of -1 must overtake the row above, not tie with it (the session's check of).
        raw = self.rules["roles"]["analysis"]["models"]
        rows = [run(str(i), mid="b") for i in range(5)] + [verdict(str(i), "ok", "b") for i in range(5)]
        ordered, notes = experience.reorder("analysis", raw, self.rules, rows, {}, tokens=0, now=NOW)
        self.assertEqual(ordered, [raw[1], raw[0], raw[2]])
        self.assertTrue(any(note.startswith("b up 1 in analysis") for note in notes))

    def test_groups_and_subscription_stay_at_their_indices(self):
        group = {"any": [model("x"), model("y")]}
        sub = {"provider": "claude-subscription", "model": "fallback"}
        raw = [model("a"), group, model("b"), sub, model("c")]
        self.rules["roles"]["analysis"]["models"] = raw
        rows = rejected() + [run("b" + str(i), mid="b") for i in range(3)]
        rows += [verdict("b" + str(i), "ok", "b") for i in range(3)]
        ordered, _ = experience.reorder("analysis", raw, self.rules, rows, {}, tokens=300000, now=NOW)
        self.assertIs(ordered[1], group)
        self.assertIs(ordered[3], sub)
        self.assertIs(ordered[4], raw[4])
        self.assertEqual([r["model"] for r in (ordered[0], ordered[2])], ["b", "a"])
        # A model after fallback cannot promote across it.
        with mock.patch.object(experience, "shift", return_value=-100):
            ordered, _ = experience.reorder("analysis", raw, self.rules, [], {}, tokens=0, now=NOW)
        self.assertIs(ordered[3], sub)
        self.assertIs(ordered[4], raw[4])

    def test_pinned_local_and_disabled(self):
        raw = self.rules["roles"]["analysis"]["models"]
        for flag in ("pinned", "local_only"):
            rules = copy.deepcopy(self.rules)
            rules["roles"]["analysis"][flag] = True
            self.assertEqual(experience.reorder("analysis", raw, rules, rejected(), {}, now=NOW), (raw, []))
        self.rules["experience"]["enabled"] = False
        self.assertEqual(experience.reorder("analysis", raw, self.rules, rejected(), {}, now=NOW), (raw, []))

    def test_check_and_cli_are_offline(self):
        self.bench["sources"]["one"]["read_at"] = "2026-10-01"
        self.bench["models"]["unused"] = {"two": 42}
        lines = experience.check(self.rules, self.bench, now=NOW)
        self.assertTrue(any("stale source: one" in line for line in lines))
        self.assertIn("missing benchmark: analysis: c (code)", lines)
        self.assertIn("unused benchmark model: unused", lines)
        with mock.patch.object(route, "load_roles", return_value=self.rules), \
             mock.patch.object(route, "_read_journal_rows", return_value=rejected()), \
             mock.patch.object(experience, "load_benchmarks", return_value=self.bench), \
             mock.patch.object(experience.time, "time", return_value=NOW), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(experience.main(["--role", "analysis"]), 0)
            self.assertIn("Experience order", out.getvalue())
            self.assertEqual(experience.main(["--check"]), 0)
        with mock.patch.object(route, "load_roles", side_effect=ValueError("broken")), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(experience.main(["--check"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
