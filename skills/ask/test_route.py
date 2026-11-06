"""Unit tests for route.py, ask.py and writer.py.

Run with:
    python -I skills/ask/test_route.py
"""
import copy
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
import sys
import time
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools")
for _p in (HERE, TOOLS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ask
import providers
import route
import writer


def setUpModule():
    # These regression tests assert the pre-score order, experience and expiry rules.
    # Keep them in legacy mode; test_score exercises the live/shadow rollout separately.
    load_roles = route.load_roles
    def legacy_roles(path=None):
        rules = load_roles(path)
        for spec in rules.get("roles", {}).values():
            if isinstance(spec, dict):
                spec.pop("score", None)
        return rules
    patcher = mock.patch.object(route, "load_roles", side_effect=legacy_roles)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    # Keep even legacy accounting tests inside the test room.
    tmp = tempfile.TemporaryDirectory(dir=HERE)
    unittest.addModuleCleanup(tmp.cleanup)
    # Keep temporary fixtures within this worktree.
    temp_patcher = mock.patch.object(tempfile, "tempdir", tmp.name)
    temp_patcher.start()
    unittest.addModuleCleanup(temp_patcher.stop)
    patches = {
        "CACHE_DIR": tmp.name, "LOG_DIR": os.path.join(tmp.name, "logs"),
        "RESERVATIONS_PATH": os.path.join(tmp.name, "reservations.json"),
        "RESERVATIONS_LOCK": os.path.join(tmp.name, "reservations.lock"),
        "AGY_CONVERSATIONS_PATH": os.path.join(tmp.name, "agy_conversations.json"),
        "JOURNAL_PATH": os.path.join(tmp.name, "usage.jsonl"),
        "CONFIG_PATH": os.path.join(tmp.name, "models.json"),
        "MACHINE_PATH": os.path.join(tmp.name, "machine.json"),
        "ACCOUNTS_HOME": os.path.join(tmp.name, "home"),
        "CODEX_ROOM": os.path.join(tmp.name, "codex-room"),
        "CODEX_USAGE_CACHE": os.path.join(tmp.name, "codex_usage.json"),
        "CODEX_MODELS_CACHE": os.path.join(tmp.name, "codex_models.json"),
    }
    for key, value in patches.items():
        patcher = mock.patch.object(route, key, value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)
    # The routing tests predate the context policy: they run in the personal context, where the role's
    # order stands; PolicyTests cover the policy itself.
    env_patcher = mock.patch.dict(os.environ, {"ASK_CONTEXT": "personal"})
    env_patcher.start()
    unittest.addModuleCleanup(env_patcher.stop)
    for target in ("subprocess.run", "subprocess.Popen", "urllib.request.urlopen"):
        patcher = mock.patch(target, side_effect=AssertionError("Tests must use fake providers"))
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def tearDownModule():
    # Python 3.9 only drains addModuleCleanup when this hook exists.
    pass


def hook_module(body):
    """A hooks module of the test's own, in the test room; its path for machine.json "hooks"."""
    fd, path = tempfile.mkstemp(suffix="_hooks.py")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def free_state():
    """Fake machine state where cloud pools have calm pace (x0.8) and the GPU is completely free."""
    return {
        "cloud": {
            "gemini": {"weekly": 0.80, "5h": 0.90, "pace": 0.8},
            "3p": {"weekly": 0.75, "5h": 0.85, "pace": 0.8},
        },
        "local": {
            "processes": [
                {"pid": 100, "name": "explorer.exe", "path": "C:\\Windows\\explorer.exe"},
                {"pid": 200, "name": "chrome.exe", "path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"},
            ],
            "quns": 5,  # QUNS_ACCEPTS_NOTIFICATIONS
            "gpu_used_mb": 1200,
            "gpu_total_mb": 16376,
            "gpu_util": 4,
            "gpu_processes": [
                {"pid": 200, "name": "chrome.exe", "used_mb": 400, "type": "C+G"},
            ],
            "ollama_up": True,
            "ollama_ps": [],
            "vram_learned": {
                "qwen3:14b": 12_000_000_000,
                "gemma4:12b": 9_500_000_000,
                "gpt-oss:20b": 12_700_000_000,
            },
            "lock": None,
        },
        "reservations": [],
        "journal": [],
        # Legacy agy cases still exercise their original fallbacks.
        "codex_models": [],
        # Explicit inputs are resolved from provider lists, even in tests.
        "agy_models": [mid for mid, info in route.model_table().items() if info["provider"] == "agy"],
        "ollama_models": [mid for mid, info in route.model_table().items() if info["provider"] == "ollama"],
    }


class RouteTests(unittest.TestCase):

    def test_everything_free_picks_first_model_id(self):
        st = free_state()
        expected = {
            "review": "gemini-4-argon-high",
            "second-opinion": "gemini-3.8-flash-high",
            "first-read": "qwen3:14b",
            "draft": "gemma4:12b",
            "writer": "gemini-4-argon-high",
            "index": "qwen3:14b",
        }
        for role, model in expected.items():
            res = route.pick(role, fake=st)
            self.assertEqual(res["picked"], model, f"role={role}")
            self.assertEqual(res["model"], model, f"role={role}")
            self.assertEqual(res["label"], model, f"role={role}")
            self.assertNotIn("alias", res)
            self.assertEqual(res["passed_over"], [], f"role={role}")
            self.assertIsNone(res["warning"], f"role={role}")
            self.assertEqual(res["exit_code"], 0, f"role={role}")

    def test_ids_allowed_and_never_mechanism_preserved(self):
        st = free_state()
        for model, info in route.model_table().items():
            if info["provider"] == "codex":
                continue
            chk = route.check_name(model, fake=st)
            self.assertTrue(chk["ok"], model)
            self.assertEqual(chk["model"], model)
            self.assertEqual(chk["label"], model)
            self.assertNotIn("alias", chk)

        # Model patterns still enforce the user's exclusions.
        custom_rules = copy.deepcopy(route.load_roles())
        custom_rules["never"] = [{"provider": "agy", "model": "claude-sonnet-5-5-*"}]
        model = "claude-sonnet-5-5-medium"
        blocked = route.check_name(model, fake=st, rules=custom_rules)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "never")
        self.assertTrue(route.check_name(model, fake=st, rules=custom_rules, allow_never=True)["ok"])

    def test_gemini_5h_below_floor_and_writer_sonnet_opus_fallbacks(self):
        st = free_state()
        st["cloud"]["gemini"]["5h"] = 0.05  # below 0.10 floor

        # Both Gemini models are below floor; this fixture offers no Codex.
        rev = route.pick("review", fake=st)
        self.assertIsNone(rev["picked"])
        self.assertEqual(len(rev["passed_over"]), 3)
        self.assertIn("gemini-4-argon-high", rev["passed_over"][0])
        self.assertIn("gpt-6.1-sol: not offered by codex", rev["passed_over"][1])
        self.assertIn("gemini-3.8-flash-high", rev["passed_over"][2])

        # Excluding a model must advance to the next role id.
        sec = route.pick("second-opinion", fake=st)
        self.assertEqual(sec["picked"], "gpt-oss-120b-medium")
        self.assertEqual(sec["label"], "gpt-oss-120b-medium")
        sec2 = route.pick("second-opinion", fake=st, exclude=["gpt-oss-120b-medium"])
        self.assertEqual(sec2["picked"], "claude-sonnet-5-5-medium")
        self.assertEqual(sec2["label"], "claude-sonnet-5-5-medium")

        # A blocked Gemini pool must preserve writer fallback order.
        wr1 = route.pick("writer", fake=st)
        self.assertEqual(wr1["picked"], "claude-sonnet-5-5-high")
        self.assertEqual(wr1["label"], "claude-sonnet-5-5-high")
        self.assertEqual(wr1["provider"], "agy")
        self.assertEqual(len(wr1["passed_over"]), 2)

        wr2 = route.pick("writer", fake=st, exclude=["claude-sonnet-5-5-high"])
        self.assertEqual(wr2["picked"], "claude-opus-5-5-high")
        self.assertEqual(wr2["label"], "claude-opus-5-5-high")
        self.assertEqual(wr2["provider"], "agy")

    def test_claude_subscription_reached_exits_6_and_journals_fallback(self):
        # Both agy pools below floor -> writer passes over Argon, Sonnet, Opus on agy and reaches claude-subscription
        st = free_state()
        st["cloud"]["gemini"]["5h"] = 0.04
        st["cloud"]["3p"]["weekly"] = 0.02

        wr = route.pick("writer", fake=st)
        self.assertIsNone(wr["picked"])
        self.assertTrue(wr["fallback"])
        self.assertEqual(wr["provider"], "claude-subscription")
        self.assertEqual(wr["model"], "claude-opus-5-5")
        self.assertEqual(wr["exit_code"], 6)
        self.assertEqual(wr["message"], "use a Claude subagent (claude-opus-5-5)")
        # The new Codex candidate is unavailable in this legacy fixture.
        self.assertEqual(len(wr["passed_over"]), 4)

        # Verify ask.py --role writer prints the line, journals 'fallback', and returns 6
        with tempfile.TemporaryDirectory() as tmp:
            jpath = os.path.join(tmp, "usage.jsonl")
            orig_journal = route.journal
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)), \
                 mock.patch.object(sys, "argv", ["ask.py", "--role", "writer", "Brief task"]):
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "stdout", out_buf), mock.patch.object(sys, "stderr", err_buf):
                    rc = ask.main()
            self.assertEqual(rc, 6)
            self.assertIn("use a Claude subagent (claude-opus-5-5)", err_buf.getvalue())
            with open(jpath, encoding="utf-8") as f:
                jrow = json.loads(f.readline())
            self.assertEqual(jrow["status"], "fallback")
            self.assertEqual(jrow["model"], "claude-opus-5-5")

    def test_rate_learned_from_journal_vs_default_and_token_estimates(self):
        # Fewer than 3 runs or 1M billed tokens uses the default.
        st = free_state()
        st["journal"] = [
            {"provider": "agy", "pool": "Gemini Models", "tokens": {"total": 250_000},
             "before": {"weekly": 0.740, "5h": 0.900}, "after": {"weekly": 0.730, "5h": 0.870}},
            {"provider": "agy", "pool": "Gemini Models", "tokens": {"total": 30_000},
             "before": {"weekly": 0.730, "5h": 0.870}, "after": {"weekly": 0.700, "5h": 0.800}},
        ]
        for i, row in enumerate(st["journal"]):
            row["ts"] = route.time.time() - 3 + i
        self.assertAlmostEqual(route.pool_window_rate("Gemini Models", "weekly", fake=st), 0.05)
        self.assertAlmostEqual(route.pool_window_rate("Gemini Models", "5h", fake=st), 0.15)

        # Three runs still fall back below 1M billed tokens.
        st["journal"] = [
            {"provider": "agy", "pool": "Gemini Models", "role": "writer", "tokens": {"total": 250_000},
             "before": {"weekly": 0.800}, "after": {"weekly": 0.795}},
            {"provider": "agy", "pool": "Gemini Models", "role": "writer", "tokens": {"total": 300_000},
             "before": {"weekly": 0.795}, "after": {"weekly": 0.786}},
            {"provider": "agy", "pool": "Gemini Models", "role": "writer", "tokens": {"total": 200_000},
             "before": {"weekly": 0.786}, "after": {"weekly": 0.778}},
        ]
        for i, row in enumerate(st["journal"]):
            row["ts"] = route.time.time() - 3 + i
        self.assertAlmostEqual(route.pool_window_rate("Gemini Models", "weekly", fake=st), 0.05)

        # 3. Token estimate: median of last 10 calls for that role (250k) vs default vs --tokens override
        self.assertEqual(route.estimate_tokens(role="writer", fake=st), 250_000)
        self.assertEqual(route.estimate_tokens(role="review", fake=st), 60_000)
        self.assertEqual(route.estimate_tokens(role="writer", override_tokens=400_000, fake=st), 400_000)

    def test_run_that_does_not_fit_passed_over_in_role_refused_by_name_and_force_runs_it(self):
        # Claude/GPT weekly has 10.4% remaining (0.4% above the 10% floor).
        # With default weekly rate = 5% per 1M (0.05) and 120,000 tokens (--tokens 120000), a run needs 0.6% (0.006).
        # 10.4% - 0.6% = 9.8% < 10.0% floor -> does not fit!
        st = free_state()
        st["cloud"]["gemini"]["5h"] = 0.05  # Gemini out so writer tests Claude/GPT models
        st["cloud"]["3p"]["weekly"] = 0.104

        # 1. Role mode (writer, with --tokens 120000): passes over claude-sonnet-5-5-high and claude-opus-5-5-high
        # with the estimate reason, and reaches claude-subscription fallback!
        wr = route.pick("writer", fake=st, tokens=120_000)
        self.assertTrue(wr["fallback"])
        self.assertEqual(wr["provider"], "claude-subscription")
        self.assertIn(
            "claude-sonnet-5-5-high: needs ~0.6% of the Claude/GPT week, 0.4% above the floor",
            wr["passed_over"][2],
        )
        self.assertIn(
            "claude-opus-5-5-high: needs ~0.6% of the Claude/GPT week, 0.4% above the floor",
            wr["passed_over"][3],
        )

        # Meanwhile a smaller role (second-opinion = 20,000 tokens -> needs ~0.1% of the week) DOES fit in 10.4%!
        sec = route.pick("second-opinion", fake=st)
        self.assertEqual(sec["picked"], "gpt-oss-120b-medium")

        # 2. Explicit name with --tokens 120000: refused with the numbers; --force overrides and runs it!
        chk_refused = route.check_name("claude-opus-5-5-high", fake=st, tokens=120_000, force=False)
        self.assertFalse(chk_refused["ok"])
        self.assertEqual(chk_refused["status"], "quota")
        self.assertEqual(
            chk_refused["reason"],
            "claude-opus-5-5-high: needs ~0.6% of the Claude/GPT week, 0.4% above the floor",
        )

        chk_forced = route.check_name("claude-opus-5-5-high", fake=st, tokens=120_000, force=True)
        self.assertTrue(chk_forced["ok"])
        self.assertIn("warning (forced): claude-opus-5-5-high: needs ~0.6%", chk_forced["warning"])

        # Verify ask.py opus --tokens 120000 refuses with exit 2, while ask.py opus --tokens 120000 --force runs it
        with tempfile.TemporaryDirectory() as tmp:
            jpath = os.path.join(tmp, "usage.jsonl")
            rpath = os.path.join(tmp, "reservations.json")
            st_run = copy.deepcopy(st)
            st_run["reservations_path"] = rpath

            def fake_ask_agy(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
                raw = '{"response": "Forced OK", "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}'
                return 0, "Forced OK", {"in": 100, "out": 50, "total": 150}, os.path.join(tmp, "opus.log"), raw

            orig_journal = route.journal
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st_run), "NO_COLOR": "1"}), \
                 mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)):
                # Without --force -> refused (exit 2)
                out1, err1 = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "claude-opus-5-5-high", "--tokens", "120000", "Question"]), \
                     mock.patch.object(sys, "stdout", out1), mock.patch.object(sys, "stderr", err1):
                    rc1 = ask.main()
                self.assertEqual(rc1, 2)
                self.assertIn("needs ~0.6% of the Claude/GPT week, 0.4% above the floor", err1.getvalue())

                # With --force -> runs (exit 0)
                out2, err2 = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "claude-opus-5-5-high", "--tokens", "120000", "--force", "Question"]), \
                     mock.patch.object(sys, "stdout", out2), mock.patch.object(sys, "stderr", err2):
                    rc2 = ask.main()
                self.assertEqual(rc2, 0)
                self.assertIn("Forced OK", out2.getvalue())

    def test_two_reservations_side_by_side_and_dead_pid_dropped(self):
        # Gemini weekly has 10.9% remaining (0.9% above the 10% floor).
        # One run of 120,000 tokens (0.6% weekly share at 5%/1M) fits (10.9% - 0.6% = 10.3% >= 10%).
        # While the first reservation is live, a second 120,000-token run sees 10.3% available (0.3% above floor)
        # and needs 0.6% -> does not fit, so Gemini is passed over!
        with tempfile.TemporaryDirectory() as tmp:
            rpath = os.path.join(tmp, "reservations.json")
            st = free_state()
            st["cloud"]["gemini"]["weekly"] = 0.109
            st["reservations_path"] = rpath

            # First writer fits in Gemini (picks gemini-4-argon-high)
            pick1 = route.pick("writer", fake=st, tokens=120_000, reservations_path=rpath)
            self.assertEqual(pick1["picked"], "gemini-4-argon-high")

            # Reserve quota for the first writer (using live pid 200 from st["local"]["processes"])
            res1 = route.reserve_quota(
                "writer.py", "writer", "agy", "gemini-4-argon-high",
                tokens=120_000, fake=st, reservations_path=rpath, pid=200,
            )
            self.assertAlmostEqual(res1["share"]["weekly"], 0.006)

            # Now a second writer side by side has only 0.3% above the floor in Gemini, so it passes over Argon
            # and picks claude-sonnet-5-5-high in the Claude/GPT pool!
            pick2 = route.pick("writer", fake=st, tokens=120_000, reservations_path=rpath)
            self.assertEqual(pick2["picked"], "claude-sonnet-5-5-high")
            self.assertIn(
                "gemini-4-argon-high: needs ~0.6% of the Gemini week, 0.3% above the floor",
                pick2["passed_over"][0],
            )

            # And an explicit check for артем at 120k tokens is refused while reservation 1 is in flight:
            chk_artem = route.check_name("gemini-4-argon-high", fake=st, tokens=120_000, reservations_path=rpath)
            self.assertFalse(chk_artem["ok"])
            self.assertIn("needs ~0.6% of the Gemini week, 0.3% above the floor", chk_artem["reason"])

            # Add a reservation with a dead PID (999999, not in processes): on the next read it is dropped!
            route.reserve_quota(
                "writer.py", "writer", "agy", "claude-sonnet-5-5-high",
                tokens=120_000, fake=st, reservations_path=rpath, pid=999999,
            )
            live_after = route.read_reservations(reservations_path=rpath, fake=st)
            self.assertEqual(len(live_after), 1)
            self.assertEqual(live_after[0]["pid"], 200)

            # Release the first reservation -> Gemini has room again!
            route.release_reservation(res1, reservations_path=rpath, procs=st["local"]["processes"])
            pick3 = route.pick("writer", fake=st, tokens=120_000, reservations_path=rpath)
            self.assertEqual(pick3["picked"], "gemini-4-argon-high")

    def test_unoffered_model_passed_over_and_refused_unless_list_unread(self):
        st = free_state()
        st["agy_models"] = [
            {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
            {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
        ]
        # gemini-4-argon-high is NOT in agy_models:
        # 1. In role 'review', gemini-4-argon-high is passed over ('not offered by agy') and gemini-3.8-flash-high is picked
        rev = route.pick("review", fake=st)
        self.assertEqual(rev["picked"], "gemini-3.8-flash-high")
        self.assertEqual(rev["passed_over"], ["gemini-4-argon-high: not offered by agy",
                                              "gpt-6.1-sol: not offered by codex"])

        # A provider-qualified id must still check that provider's list.
        chk = route.check_name({"provider": "agy", "model": "gemini-4-argon-high"}, fake=st)
        self.assertFalse(chk["ok"])
        self.assertEqual(chk["status"], "not_offered")
        self.assertEqual(chk["reason"], "gemini-4-argon-high: not offered by agy")

        # 3. When the list could not be read (agy_models=None), nothing is passed over for that reason
        st["agy_models"] = None
        chk_ok = route.check_name({"provider": "agy", "model": "gemini-4-argon-high"}, fake=st)
        self.assertTrue(chk_ok["ok"])

    def test_pace_formatting_on_pace_and_the_writer_summary_line(self):
        # Pace between 0.9 and 1.1 reads 'x1.0 (on pace)', not 'slower'
        self.assertEqual(route.format_pace(1.0, "weekly"), "x1.0 (on pace)")
        self.assertEqual(route.format_pace(0.96, "weekly"), "x1.0 (on pace)")
        self.assertEqual(route.format_pace(0.9, "weekly"), "x0.9 (on pace)")
        self.assertEqual(route.format_pace(1.08, "5h"), "x1.1 (on pace)")
        self.assertEqual(route.format_pace(0.8, "weekly"), "x0.8 (slower)")
        self.assertEqual(route.format_pace(2.2, "weekly"), "x2.2 (faster than the week)")


    def test_pools_of_any_count_and_unmatched_group(self):
        now_ts = 1793734000.0
        st = {
            "cloud": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [{"window": "weekly", "remaining_fraction": 0.80, "reset_time": "2026-11-08T19:00:00Z"}],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [{"window": "weekly", "remaining_fraction": 0.70, "reset_time": "2026-11-08T19:00:00Z"}],
                    },
                    {
                        "name": "Dedicated Opus Pool",
                        "covers": ["claude-opus-*"],
                        "buckets": [{"window": "5h", "remaining_fraction": 0.03, "reset_time": "2026-11-03T21:00:00Z"}],
                    },
                    {
                        "name": "Experimental Mystery Models",
                        "buckets": [{"window": "weekly", "remaining_fraction": 0.01, "reset_time": "2026-11-08T19:00:00Z"}],
                    },
                ]
            },
            "local": free_state()["local"],
        }
        cs = route.cloud_state(fake=st, now=now_ts)
        self.assertEqual(len(cs["pools"]), 4)
        self.assertEqual(cs["pools"][3]["covers"], [])
        chk_opus = route.check_name("claude-opus-5-5-high", fake=st)
        self.assertFalse(chk_opus["ok"])
        self.assertIn("Dedicated Opus Pool", chk_opus["reason"])
        chk_gpt = route.check_name("gpt-oss-120b-medium", fake=st)
        self.assertTrue(chk_gpt["ok"])

    def test_per_model_subscription_window_and_stale_subscription_file(self):
        now_ts = 1793734200.0  # 2026-11-03T19:30:00Z
        # The files' ages are measured from now_ts, not the clock: the test went stale itself 6 h later.
        clock = mock.patch.object(route.time, "time", return_value=now_ts)
        clock.start()
        self.addCleanup(clock.stop)
        fresh_cu = {
            "read_at": "2026-11-03T19:00:00Z",
            "plan": "Ultra",
            "windows": [
                {"label": "Weekly · all models", "percentUsed": 40, "resetsAt": "2026-11-06T19:00:00Z"},
                {"label": "Weekly · Fable", "percentUsed": 95, "resetsAt": "2026-11-06T19:00:00Z"},
                {"label": "Weekly · Opus", "percentUsed": 20, "resetsAt": "2026-11-06T19:00:00Z"},
            ],
        }
        st = free_state()
        st["cloud"]["claude_usage"] = fresh_cu

        chk_fable = route.check_name({"provider": "claude-subscription", "model": "claude-fable-5-1"}, fake=st)
        self.assertFalse(chk_fable["ok"])
        self.assertIn("Fable", chk_fable["reason"])

        chk_opus = route.check_name({"provider": "claude-subscription", "model": "claude-opus-5-5"}, fake=st)
        self.assertTrue(chk_opus["ok"])

        stale_cu = {
            "read_at": "2026-11-03T10:00:00Z",  # 9.5 h old (stale)
            "plan": "Ultra",
            "windows": [
                {"label": "Weekly · all models", "percentUsed": 98, "resetsAt": "2026-11-06T19:00:00Z"},
            ],
        }
        cs_stale = route.cloud_state(fake={"cloud": {"claude_usage": stale_cu}}, now=now_ts)
        self.assertEqual(cs_stale["claude_subscription"]["status"], "stale")
        self.assertTrue(cs_stale["pools"][0]["stale"])
        chk_stale = route.check_name(
            {"provider": "claude-subscription", "model": "claude-opus-5-5"},
            cs=cs_stale,
        )
        self.assertTrue(chk_stale["ok"])

    def test_pace_skip_with_later_name_free_and_every_name_over_pace(self):
        st = free_state()
        st["cloud"]["gemini"]["pace"] = 2.2
        st["cloud"]["3p"]["pace"] = 0.8

        sec = route.pick("second-opinion", fake=st)
        self.assertEqual(sec["picked"], "gpt-oss-120b-medium")
        self.assertEqual(len(sec["passed_over"]), 3)
        self.assertEqual(sec["passed_over"][0], "gemini-3.8-flash-high: Gemini pool x2.2 faster than the week")
        self.assertEqual(sec["passed_over"][1], "gpt-6.1-sol: not offered by codex")
        self.assertEqual(sec["passed_over"][2], "gemini-4-argon-high: Gemini pool x2.2 faster than the week")
        self.assertIsNone(sec["warning"])

        rev = route.pick("review", fake=st)
        self.assertEqual(rev["picked"], "gemini-4-argon-high")
        self.assertEqual(rev["passed_over"], [])
        self.assertIsNotNone(rev["warning"])
        self.assertIn("gemini-4-argon-high: Gemini pool x2.2 faster than the week", rev["warning"])

        chk = route.check_name("gemini-4-argon-high", fake=st)
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["model"], "gemini-4-argon-high")
        self.assertIn("x2.2 faster than the week", chk["warning"])

    def test_hook_blockers_vram_and_stale_lock(self):
        st_busy = free_state()
        st_busy["machine"] = {"hooks": {"module": hook_module("def local_blockers(state, settings):\n"
                                                              "    return ['the machine is busy']\n")}}
        idx = route.pick("index", fake=st_busy)
        self.assertIsNone(idx["picked"])
        self.assertTrue(idx["wait"])
        self.assertEqual(idx["exit_code"], 3)

        dr = route.pick("draft", fake=st_busy)
        self.assertEqual(dr["picked"], "gemini-3.8-flash-low")
        self.assertEqual(dr["label"], "gemini-3.8-flash-low")
        self.assertIn("the machine is busy", route.local_state(model="qwen3:14b", fake=st_busy)["reason_str"])

        st_vram = free_state()
        st_vram["local"]["gpu_used_mb"] = 8000
        loc_vram = route.local_state(model="gemma4:12b", fake=st_vram)
        self.assertFalse(loc_vram["free"])
        self.assertIn("low VRAM", loc_vram["reason_str"])

        with tempfile.TemporaryDirectory() as tmp:
            lock_file = os.path.join(tmp, "local.lock")
            with open(lock_file, "w", encoding="utf-8") as f:
                json.dump({"pid": 999999, "model": "qwen3:14b"}, f)
            st_lock = free_state()
            st_lock["local"]["lock"] = lock_file
            res_stale = route.pick("draft", fake=st_lock)
            self.assertEqual(res_stale["picked"], "gemma4:12b")
            self.assertFalse(os.path.exists(lock_file))

            with open(lock_file, "w", encoding="utf-8") as f:
                json.dump({"pid": 200, "model": "qwen3:14b"}, f)
            res_live = route.pick("draft", fake=st_lock)
            self.assertEqual(res_live["picked"], "gemini-3.8-flash-low")
            self.assertIn("local.lock held by pid 200", res_live["passed_over"][0])

    def test_quota_failure_falls_over_in_role_mode_and_not_with_explicit_name(self):
        st = free_state()
        with tempfile.TemporaryDirectory() as tmp:
            jpath = os.path.join(tmp, "usage.jsonl")
            rpath = os.path.join(tmp, "reservations.json")
            st["reservations_path"] = rpath
            calls = []

            def fake_ask_agy(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
                calls.append(model)
                if model == "gemini-4-argon-high":
                    raw = "Error 429: RESOURCE_EXHAUSTED: quota exceeded"
                    return 1, raw, {"in": 0, "out": 0, "total": 0}, os.path.join(tmp, "argon.log"), raw
                raw = '{"response": "Відповідь від Жені", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}'
                return 0, "Відповідь від Жені", {"in": 10, "out": 5, "total": 15}, os.path.join(tmp, "flash.log"), raw

            orig_journal = route.journal
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)), \
                 mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "--role", "review", "Перевір цей код"]):
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "stdout", out_buf), mock.patch.object(sys, "stderr", err_buf):
                    rc = ask.main()
            self.assertEqual(rc, 0)
            self.assertEqual(calls, ["gemini-4-argon-high", "gemini-3.8-flash-high"])
            stdout_str = out_buf.getvalue()
            self.assertIn("passed over: gemini-4-argon-high: quota/limit error from agy", stdout_str)
            self.assertIn("[gemini-3.8-flash-high, Gemini Models]", stdout_str)

            # Explicit ids must remain selected after a quota error.
            calls.clear()
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)), \
                 mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "gemini-4-argon-high", "Питання"]):
                out_buf2, err_buf2 = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "stdout", out_buf2), mock.patch.object(sys, "stderr", err_buf2):
                    rc2 = ask.main()
            self.assertEqual(rc2, 2)
            self.assertEqual(calls, ["gemini-4-argon-high"])
            self.assertIn("gemini-4-argon-high: quota exhausted", err_buf2.getvalue())

            summary = route.read_journal_24h(journal_path=jpath)
            self.assertEqual(summary["gemini-4-argon-high"]["quota_hits"], 2)
            self.assertEqual(summary["gemini-3.8-flash-high"]["calls"], 1)

    def test_carried_on_conversation_journals_difference_and_keeps_totals(self):
        st = free_state()
        with tempfile.TemporaryDirectory() as tmp:
            jpath = os.path.join(tmp, "usage.jsonl")
            rpath = os.path.join(tmp, "reservations.json")
            cpath = os.path.join(tmp, "agy_conversations.json")
            st["reservations_path"] = rpath
            call_num = [0]

            def fake_ask_agy(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
                call_num[0] += 1
                if call_num[0] == 1:
                    self.assertFalse(carry_on)
                    raw = json.dumps({
                        "conversation_id": "conv-123",
                        "response": "Перша відповідь",
                        "usage": {"input_tokens": 10000, "output_tokens": 2000, "thinking_tokens": 800, "total_tokens": 12000},
                    })
                    return 0, "Перша відповідь", {"in": 10000, "out": 2000, "total": 12000}, os.path.join(tmp, "1.log"), raw
                self.assertTrue(carry_on)
                raw = json.dumps({
                    "conversation_id": "conv-123",
                    "response": "Друга відповідь",
                    "usage": {"input_tokens": 14500, "output_tokens": 3100, "thinking_tokens": 1200, "total_tokens": 17600},
                })
                return 0, "Друга відповідь", {"in": 14500, "out": 3100, "total": 17600}, os.path.join(tmp, "2.log"), raw

            orig_journal = route.journal
            orig_rec = route.record_conversation_usage
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)), \
                 mock.patch.object(route, "record_conversation_usage", side_effect=lambda *a, **kw: orig_rec(*a, **{**kw, "conversations_path": cpath})):
                # 1. New conversation: journals usage as is and saves in agy_conversations.json
                out1, err1 = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "gemini-3.8-flash-high", "Перше питання"]), \
                     mock.patch.object(sys, "stdout", out1), mock.patch.object(sys, "stderr", err1):
                    rc1 = ask.main()
                self.assertEqual(rc1, 0)

                # 2. Carried-on conversation (--continue): journals difference as tokens, running total as conversation_tokens
                out2, err2 = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "gemini-3.8-flash-high", "--continue", "Уточнення"]), \
                     mock.patch.object(sys, "stdout", out2), mock.patch.object(sys, "stderr", err2):
                    rc2 = ask.main()
                self.assertEqual(rc2, 0)

            with open(jpath, encoding="utf-8") as f:
                jlines = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(jlines), 2)
            # First run: as is
            self.assertEqual(jlines[0]["tokens"], {"in": 10000, "out": 2000, "total": 12000, "fresh": 12000, "billed": 12000})
            self.assertNotIn("conversation_tokens", jlines[0])
            # Second run: difference in tokens, running total in conversation_tokens, id in conversation
            self.assertEqual(jlines[1]["tokens"], {"in": 4500, "out": 1100, "total": 5600, "fresh": 5600, "billed": 5600})
            self.assertEqual(jlines[1]["conversation_tokens"], {"in": 14500, "out": 3100, "total": 17600})
            self.assertEqual(jlines[1]["conversation"], "conv-123")

            convs = route.load_agy_conversations(cpath)
            self.assertEqual(convs["conv-123"], {"in": 14500, "out": 3100, "thinking": 1200, "total": 17600})

    def test_empty_answer_retry_on_denied_tool_and_no_text_failover_and_header(self):
        st = free_state()
        with tempfile.TemporaryDirectory() as tmp:
            jpath = os.path.join(tmp, "usage.jsonl")
            rpath = os.path.join(tmp, "reservations.json")
            cpath = os.path.join(tmp, "agy_conversations.json")
            st["reservations_path"] = rpath
            seen_calls = []

            def fake_ask_agy_retry_ok(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
                seen_calls.append((model, text, carry_on))
                if len(seen_calls) == 1:
                    raw = json.dumps({
                        "conversation_id": "dac94ab3-b38f-4495-b73c-322d5d7e801b",
                        "status": "SUCCESS",
                        "response": "",
                        "usage": {"input_tokens": 22321, "output_tokens": 1574, "thinking_tokens": 1451, "total_tokens": 23895},
                        "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
                    })
                    return 0, "", {"in": 22321, "out": 1574, "total": 23895}, os.path.join(tmp, "try1.log"), raw
                raw = json.dumps({
                    "conversation_id": "dac94ab3-b38f-4495-b73c-322d5d7e801b",
                    "status": "SUCCESS",
                    "response": "так",
                    "usage": {"input_tokens": 32934, "output_tokens": 1919, "thinking_tokens": 1795, "total_tokens": 34853},
                    "denied_actions": [],
                })
                return 0, "так", {"in": 32934, "out": 1919, "total": 34853}, os.path.join(tmp, "try2.log"), raw

            orig_journal = route.journal
            orig_rec = route.record_conversation_usage
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy_retry_ok), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)), \
                 mock.patch.object(route, "record_conversation_usage", side_effect=lambda *a, **kw: orig_rec(*a, **{**kw, "conversations_path": cpath})):
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "--role", "second-opinion", "Скажи одне слово: так"]), \
                     mock.patch.object(sys, "stdout", out_buf), mock.patch.object(sys, "stderr", err_buf):
                    rc = ask.main()

            self.assertEqual(rc, 0)
            self.assertEqual(len(seen_calls), 2)
            # Question itself stays word for word on call 1; call 2 carries on the same conversation with the words-only prompt
            self.assertEqual(seen_calls[0], ("gemini-3.8-flash-high", "Скажи одне слово: так", False))
            self.assertEqual(
                seen_calls[1],
                (
                    "gemini-3.8-flash-high",
                    "Answer in words only, from what you know; do not use any tool or command.",
                    "dac94ab3-b38f-4495-b73c-322d5d7e801b",
                ),
            )
            # The header must identify the selected id and pool.
            self.assertIn("[gemini-3.8-flash-high, Gemini Models]\nтак", out_buf.getvalue())

            # Journal line is ONE, its tokens the sum of both (22321+10613=32934, 1574+345=1919, 23895+10958=34853), status 'ok (retried)'
            with open(jpath, encoding="utf-8") as f:
                jrows = [json.loads(ln) for ln in f if ln.strip()]
            self.assertEqual(len(jrows), 1)
            self.assertEqual(jrows[0]["status"], "ok (retried)")
            self.assertEqual(jrows[0]["tokens"], {"in": 32934, "out": 1919, "total": 34853, "fresh": 34853, "billed": 34853})

            # Subtest 2: still empty after retry -> journals 'no_text' and in role mode moves to next name (gemini-4-argon-high)
            jpath2 = os.path.join(tmp, "usage2.jsonl")
            seen_calls2 = []

            def fake_ask_agy_still_empty(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
                seen_calls2.append((model, text, carry_on))
                if model == "gemini-3.8-flash-high":
                    raw = json.dumps({
                        "conversation_id": "conv-empty",
                        "response": "",
                        "usage": {"input_tokens": 5000, "output_tokens": 200, "total_tokens": 5200},
                        "denied_actions": [{"action": "command"}],
                    })
                    return 0, "", {"in": 5000, "out": 200, "total": 5200}, os.path.join(tmp, "e.log"), raw
                raw = json.dumps({
                    "conversation_id": "conv-argon",
                    "response": "Відповідь Артема",
                    "usage": {"input_tokens": 3000, "output_tokens": 100, "total_tokens": 3100},
                })
                return 0, "Відповідь Артема", {"in": 3000, "out": 100, "total": 3100}, os.path.join(tmp, "a.log"), raw

            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy_still_empty), \
                 mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath2, **kw)), \
                 mock.patch.object(route, "record_conversation_usage", side_effect=lambda *a, **kw: orig_rec(*a, **{**kw, "conversations_path": cpath})):
                out_buf2, err_buf2 = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "--role", "second-opinion", "Скажи одне слово: так"]), \
                     mock.patch.object(sys, "stdout", out_buf2), mock.patch.object(sys, "stderr", err_buf2):
                    rc2 = ask.main()

            self.assertEqual(rc2, 0)
            self.assertEqual([c[0] for c in seen_calls2], ["gemini-3.8-flash-high", "gemini-3.8-flash-high", "gemini-4-argon-high"])
            self.assertIn("[gemini-4-argon-high, Gemini Models]\nВідповідь Артема", out_buf2.getvalue())
            with open(jpath2, encoding="utf-8") as f:
                jrows2 = [json.loads(ln) for ln in f if ln.strip()]
            self.assertEqual(len(jrows2), 2)
            self.assertEqual(jrows2[0]["model"], "gemini-3.8-flash-high")
            self.assertEqual(jrows2[0]["status"], "no_text")
            self.assertEqual(jrows2[1]["model"], "gemini-4-argon-high")
            self.assertEqual(jrows2[1]["status"], "ok")


class CodexTests(unittest.TestCase):
    """Offline contract checks for the user's Codex provider."""

    def usage(self, used=20, secondary=None, reached=None):
        return {"result": {"rateLimits": {
            "limitId": "codex", "planType": "prolite", "rateLimitReachedType": reached,
            "primary": {"usedPercent": used, "windowDurationMins": 10080,
                        "resetsAt": 1800000000}, "secondary": secondary,
        }}}

    def state(self, used=0):
        st = free_state()
        st["codex_models"] = {"result": {"data": [
            {"id": "gpt-6.1-sol", "displayName": "Workhorse", "description": "Coding",
             "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
             "isDefault": True, "hidden": False},
            {"id": "gpt-5.6-luna", "displayName": "Fast", "hidden": False},
            {"id": "hidden-model", "hidden": True},
        ], "nextCursor": None}}
        st["codex_usage"] = self.usage(used)
        return st

    def events(self, answer="Codex answer"):
        return "\n".join(json.dumps(e) for e in [
            {"type": "thread.started", "thread_id": "thread-test"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": answer}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
             "cache_write_input_tokens": 5, "output_tokens": 20, "reasoning_output_tokens": 10}},
        ])

    def test_one_window_and_secondary_null_and_two_windows(self):
        now = 1800000000 - 3 * 86400
        st = self.state(25)
        cs = route.cloud_state(fake=st, now=now)
        pool = next(p for p in cs["pools"] if p["provider"] == "codex")
        self.assertEqual(pool["label"], "ChatGPT (prolite)")
        self.assertEqual(pool["covers"], ["gpt-6*", "gpt-5.6*"])
        self.assertEqual(len(pool["windows"]), 1)
        weekly = pool["windows"][0]
        self.assertEqual(weekly["window"], "weekly")
        self.assertEqual(weekly["length_s"], 604800)
        self.assertEqual(weekly["remaining_fraction"], 0.75)
        self.assertEqual(weekly["resets_in_s"], 3 * 86400)
        self.assertAlmostEqual(weekly["pace"], 0.25 / (4 / 7))
        secondary = {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": now + 3600}
        st["codex_usage"] = self.usage(25, secondary)
        pool = route._pools_from_codex_usage(st["codex_usage"], now)[0]
        self.assertEqual(len(pool["windows"]), 2)
        self.assertEqual(pool["windows"][1]["window"], "5h")
        self.assertEqual(pool["windows"][1]["length_s"], 18000)
        self.assertAlmostEqual(pool["windows"][1]["remaining_fraction"], 0.9)
        by_id = {"rateLimitsByLimitId": {"codex": st["codex_usage"]["result"]["rateLimits"]}}
        self.assertEqual(route._pools_from_codex_usage(by_id, now), [pool])

    def test_floor_reached_flag_name_resolution_and_role_order(self):
        st = self.state(99.9)
        chk = route.check_name("gpt-6.1-sol", fake=st)
        self.assertEqual(chk["status"], "quota")
        self.assertEqual(chk["provider"], "codex")
        self.assertTrue(route.check_name("gpt-6.1-sol", fake=st, force=True)["ok"])
        self.assertEqual(route.resolve_model("gpt-5.6-luna", provider="codex"), ("codex", "gpt-5.6-luna"))
        self.assertEqual(route.resolve_model("gpt-oss-120b-medium")[0], "agy")
        st["codex_usage"] = self.usage(0, reached="weekly")
        self.assertEqual(route.check_name("gpt-6.1-sol", fake=st)["status"], "quota")
        st = self.state()
        st["cloud"]["gemini"]["5h"] = 0.01
        for role in ("second-opinion", "review"):
            decision = route.pick(role, fake=st)
            self.assertEqual(decision["picked"], "gpt-6.1-sol")
            self.assertEqual(decision["provider"], "codex")
            self.assertEqual(decision["pool"], "ChatGPT (prolite)")
            self.assertNotIn("alias", decision)
        st["codex_usage"] = self.usage(99)
        self.assertEqual(route.pick("second-opinion", fake=st)["provider"], "agy")
        self.assertIsNone(route.pick("review", fake=st)["picked"])

    def test_model_list_and_roles_and_unoffered_name(self):
        st = self.state()
        ok, items = route.fetch_codex_models(fake=st)
        self.assertTrue(ok)
        self.assertEqual([m["id"] for m in items], ["gpt-6.1-sol", "gpt-5.6-luna"])
        self.assertEqual(items[0]["defaultReasoningEffort"], "medium")
        rows = [r for r in route.models(fake=st) if r["provider"] == "codex"]
        self.assertEqual(rows[0]["pool"], "ChatGPT (prolite)")
        self.assertEqual(rows[0]["default_reasoning_effort"], "medium")
        self.assertEqual(route.role_usage()[("codex", "gpt-6.1-sol")], ["review", "second-opinion", "writer", "analysis", "code-writer"])
        self.assertEqual(route.check_name("gpt-6-missing", fake=st)["status"], "error")
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st)}), \
             mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(ask.print_list("unused", "unused"), 0)
        self.assertIn("review, second-opinion", output.getvalue())
        self.assertIn("gpt-5.6-luna", output.getvalue())
        self.assertIn("-- in no role", output.getvalue())

    def test_event_tokens_raw_log_and_journal(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ask.subprocess, "run", return_value=mock.Mock(
                 returncode=0, stdout=self.events(), stderr="")) as run:
            result = ask.ask_codex("gpt-6.1-sol", "Question", False, 20, executable="fake-codex")
            code, answer, tokens, log, raw, status, thread, _ = result
            self.assertEqual((code, answer, status, thread), (0, "Codex answer", "ok", "thread-test"))
            self.assertEqual(tokens, {"in": 100, "out": 30, "total": 130, "cached_in": 40})
            with open(log, encoding="utf-8") as f:
                self.assertEqual(f.read(), raw)
            args = run.call_args.args[0]
            self.assertEqual(args[args.index("-s") + 1], "read-only")
            self.assertEqual(args[args.index("-C") + 1], route.CODEX_ROOM)
            self.assertEqual(args[-1], "-")
            self.assertEqual(run.call_args.kwargs["input"], "Question")
            self.assertNotIn("-c", args)
            row = route.journal("ask.py", "review", None, "codex", "gpt-6.1-sol", tokens=tokens,
                                pool="ChatGPT (prolite)", conversation=thread,
                                journal_path=os.path.join(tmp, "usage.jsonl"))
            self.assertEqual(row["tokens"], dict(tokens, fresh=90, billed=94))
            self.assertEqual(row["conversation"], thread)

    def test_output_file_effort_and_resume_and_empty_response(self):
        def output_file(args, **kwargs):
            with open(args[args.index("-o") + 1], "w", encoding="utf-8") as f:
                f.write("Final file answer")
            return mock.Mock(returncode=0, stdout=self.events(""), stderr="")
        with mock.patch.object(ask.subprocess, "run", side_effect=output_file) as run:
            result = ask.ask_codex("gpt-6.1-sol", "Follow-up", "thread-test", 10, effort="high", executable="fake")
            self.assertEqual(result[1], "Final file answer")
            args = run.call_args.args[0]
            self.assertIn("resume", args)
            self.assertIn("thread-test", args)
            self.assertIn("model_reasoning_effort=high", args)
            self.assertIn("read-only", args)
            self.assertEqual(result[2]["total"], 130)
        with mock.patch.object(ask.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=self.events(""), stderr="")) as run:
            result = ask.ask_codex("gpt-6.1-sol", "Question", True, 10, executable="fake")
            self.assertEqual(result[5], "no_text")
            self.assertNotEqual(result[0], 0)
            self.assertIn("--last", run.call_args.args[0])

    def test_quota_and_empty_answer_fall_over_in_role_but_explicit_stops(self):
        st = self.state()
        st["agy_models"] = ["gemini-3.8-flash-high"]
        for raw, expected in [
            (json.dumps({"type": "error", "message": "429 rate limit exceeded"}), "quota"),
            (json.dumps({"type": "turn.failed", "error": {"message": "usage limit reached"}}), "quota"),
            (json.dumps({"type": "error", "rateLimitReachedType": "weekly"}), "quota"),
            (self.events(""), "no_text"),
        ]:
            with self.subTest(status=expected, raw=raw), tempfile.TemporaryDirectory() as tmp:
                rows = []
                real_journal = route.journal
                def record(*args, **kwargs):
                    row = real_journal(*args, **kwargs, journal_path=os.path.join(tmp, "usage.jsonl"))
                    rows.append(row)
                    return row
                agy_raw = '{"response":"Gemini answer","usage":{"input_tokens":1,"output_tokens":1}}'
                with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                     mock.patch.object(ask.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=raw, stderr="")), \
                     mock.patch.object(ask, "ask_agy", return_value=(0, "Gemini answer", {}, "fake.log", agy_raw)), \
                     mock.patch.object(route, "journal", side_effect=record), \
                     mock.patch.object(sys, "stdout", io.StringIO()) as output, \
                     mock.patch.object(sys, "stderr", io.StringIO()), \
                     mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "--role", "review", "Check"]):
                    self.assertEqual(ask.main(), 0)
                    self.assertIn("Gemini answer", output.getvalue())
                self.assertEqual([r["provider"] for r in rows], ["codex", "agy"])
                self.assertEqual(rows[0]["status"], expected)
                self.assertEqual(route.read_reservations(fake={"reservations_path": route.RESERVATIONS_PATH}), [])
                rows.clear()
                with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                     mock.patch.object(ask.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=raw, stderr="")), \
                     mock.patch.object(route, "journal", side_effect=record), \
                     mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()), \
                     mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "--codex", "gpt-6.1-sol", "Check"]):
                    self.assertEqual(ask.main(), 2)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], expected)

    def test_reservations_token_estimates_and_measured_rate(self):
        st = self.state()
        st["codex_usage"] = self.usage(89.1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reservations.json")
            st["reservations_path"] = path
            self.assertTrue(route.check_name("gpt-6.1-sol", fake=st, tokens=120000)["ok"])
            reservation = route.reserve_quota("ask.py", "review", "codex", "gpt-6.1-sol",
                                               tokens=120000, fake=st, reservations_path=path, pid=200)
            self.assertAlmostEqual(reservation["share"]["weekly"], 0.006)
            self.assertEqual(route.check_name("gpt-6.1-sol", fake=st, tokens=120000)["status"], "quota")
            route.release_reservation(reservation, reservations_path=path, procs=st["local"]["processes"])
            self.assertTrue(route.check_name("gpt-6.1-sol", fake=st, tokens=120000)["ok"])
        st["journal"] = [{"provider": "codex", "model": "gpt-6.1-sol", "pool": "ChatGPT (prolite)",
                          "role": "review", "tokens": {"total": 100000},
                          "before": {"weekly": 0.8}, "after": {"weekly": 0.79}} for _ in range(3)]
        # The series is below 1M billed tokens, so no learned rate yet.
        for i, row in enumerate(st["journal"]):
            row["ts"] = route.time.time() - 3 + i
        self.assertAlmostEqual(route.pool_window_rate("ChatGPT (prolite)", fake=st), 0.05)
        self.assertEqual(route.estimate_tokens("review", fake=st), 100000)

    def test_metadata_handshake_cache_and_own_process_cleanup(self):
        replies = [
            {"id": 1, "result": {}},
            {"id": 3, "result": self.state()["codex_models"]["result"]},
            {"method": "notification", "params": {}},
            {"id": 2, "result": self.usage()["result"]},
        ]
        proc = mock.Mock(stdout=io.StringIO("\n".join(json.dumps(r) for r in replies)), stdin=io.StringIO())
        proc.poll.return_value = None
        sent = []
        proc.stdin = mock.Mock(write=lambda s: sent.append(json.loads(s)))
        with mock.patch.object(route.subprocess, "Popen", return_value=proc) as start:
            results = route._codex_rpc(["account/rateLimits/read", "model/list"], executable="fake")
        self.assertEqual(start.call_args.args[0], ["fake", "app-server"])
        self.assertEqual([r["method"] for r in sent], ["initialize", "initialized", "account/rateLimits/read", "model/list"])
        self.assertEqual(results["account/rateLimits/read"], self.usage()["result"])
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(route, "CODEX_USAGE_CACHE", os.path.join(tmp, "usage.json")), \
             mock.patch.object(route, "CODEX_MODELS_CACHE", os.path.join(tmp, "models.json")), \
             mock.patch.object(route, "_codex_rpc", return_value=results) as rpc:
            self.assertEqual(route.fetch_codex_usage(), self.usage()["result"])
            self.assertTrue(route.fetch_codex_models()[0])
            self.assertEqual(rpc.call_count, 1)
            route.fetch_codex_usage(force_refresh=True)
            self.assertEqual(rpc.call_count, 2)


    def test_explicit_id_flags_effort_and_status_pool(self):
        st = self.state()
        for args in (["gpt-6.1-sol", "--effort", "xhigh", "Question"],
                     ["--codex", "gpt-6.1-sol", "--effort", "xhigh", "Question"]):
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
                 mock.patch.object(ask.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=self.events(), stderr="")) as run, \
                 mock.patch.object(sys, "stdout", io.StringIO()) as output, mock.patch.object(sys, "stderr", io.StringIO()), \
                 mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", *args]):
                self.assertEqual(ask.main(), 0)
                self.assertIn("[gpt-6.1-sol, ChatGPT (prolite)]", output.getvalue())
                self.assertIn("model_reasoning_effort=xhigh", run.call_args.args[0])
                self.assertEqual(run.call_args.args[0][-1], "-")
                self.assertEqual(run.call_args.kwargs["input"], "Question")
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
             mock.patch.object(sys, "stdout", io.StringIO()) as output:
            self.assertEqual(ask.print_status("unused", "unused"), 0)
        self.assertIn("ChatGPT (prolite)", output.getvalue())
        self.assertIn("weekly", output.getvalue())

    def test_discovery_override_and_newest_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.json")
            older, newer = os.path.join(tmp, "old.exe"), os.path.join(tmp, "new.exe")
            for path in (older, newer):
                with open(path, "w") as f:
                    f.write("")
            os.utime(older, (100, 100))
            os.utime(newer, (200, 200))
            with mock.patch.object(route, "CONFIG_PATH", config), \
                 mock.patch.object(route.glob, "glob", return_value=[older, newer]):
                self.assertEqual(route.codex_path(), newer)
                with open(config, "w", encoding="utf-8") as f:
                    json.dump({"codex": older}, f)
                self.assertEqual(route.codex_path(), older)

    def test_timeout_and_launch_failure_still_save_raw_logs(self):
        for error in (OSError("fake missing CLI"), ask.subprocess.TimeoutExpired(
                "fake", 1, output=self.events().encode(), stderr=b"partial stderr")):
            with mock.patch.object(ask.subprocess, "run", side_effect=error):
                result = ask.ask_codex("gpt-6.1-sol", "Question", False, 1, executable="fake")
            self.assertNotEqual(result[0], 0)
            self.assertTrue(os.path.isfile(result[3]))
            self.assertEqual(result[5], "timeout" if isinstance(error, ask.subprocess.TimeoutExpired) else "no_text")
        proc = mock.Mock(stdout=io.StringIO(""), stdin=mock.Mock())
        proc.poll.return_value = None
        with mock.patch.object(route.subprocess, "Popen", return_value=proc):
            with self.assertRaises(TimeoutError):
                route._codex_rpc(["model/list"], executable="fake", timeout_s=0)
        proc.terminate.assert_called_once()


class HookTests(unittest.TestCase):
    """The machine's optional hooks (skills/ask/hooks.py): without them the plain GPU check only; with them
    their reasons; a broken module or a raising blocker blocks (fail closed); advisory hooks fall back to defaults."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lock = os.path.join(self.tmp, "local.lock")

    def test_without_hooks_only_the_plain_checks_count(self):
        st = free_state()
        st["local"]["processes"].append({"pid": 4444, "name": "SomeGame.exe",
                                         "path": "D:\\Games\\SomeGame\\SomeGame.exe"})
        st["local"]["gpu_processes"].append({"pid": 4444, "name": "SomeGame.exe", "used_mb": 4096})
        self.assertTrue(route.local_state(model="qwen3:14b", fake=st)["free"])
        ok, why = route.claim_gpu("qwen3:14b", lock_path=self.lock, machine={})
        self.assertTrue(ok, why)
        route.release_local_lock(self.lock)
        self.assertEqual(route.build_blockers(lock_path=self.lock, machine={}), [])

    def test_claim_and_build_blockers_go_through_the_hooks(self):
        machine = {"hooks": {"module": hook_module(
            "def claim_blockers(model, settings):\n    return [settings['why'] + ' ' + model]\n"
            "def build_blockers(settings):\n    return ['a build waits']\n"), "why": "engine starting for"}}
        ok, why = route.claim_gpu("qwen3:14b", lock_path=self.lock, machine=machine)
        self.assertFalse(ok)
        self.assertEqual(why, "engine starting for qwen3:14b")
        self.assertFalse(os.path.exists(self.lock))  # the lock goes back
        self.assertEqual(route.build_blockers(lock_path=self.lock, machine=machine), ["a build waits"])
        with open(self.lock, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getppid(), "model": "qwen3:14b"}, f)  # another live process's lock
        self.assertEqual(route.build_blockers(lock_path=self.lock, machine={}),
                         [f"a local model holds the GPU (local.lock, pid {os.getppid()})"])
        os.remove(self.lock)

    def test_a_broken_module_or_hook_blocks_and_advisory_hooks_fall_back(self):
        import hooks
        missing = {"hooks": {"module": os.path.join(self.tmp, "nowhere.py")}}
        self.assertTrue(hooks.blockers("local_blockers", {}, machine=missing)[0].startswith("hooks module"))
        raising = {"hooks": {"module": hook_module("def local_blockers(state, settings):\n    raise RuntimeError('x')\n"
                                                   "def writer_rules(engine, settings):\n    raise RuntimeError('y')\n")}}
        self.assertEqual(hooks.blockers("local_blockers", {}, machine=raising), ["hooks local_blockers failed: x"])
        with mock.patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertEqual(hooks.value("writer_rules", False, machine=raising, default={"engine": "d"}), {"engine": "d"})
        self.assertIn("writer_rules failed", err.getvalue())
        self.assertEqual(hooks.engine_markers({}), [])
        marked = {"hooks": {"module": hook_module("def engine_markers(settings):\n    return ['Big-Engine']\n")}}
        self.assertEqual(hooks.engine_markers(marked), ["big-engine"])
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertIsNone(hooks.engine_markers(missing))  # unknown: the broker then refuses commands 
            broken = {"hooks": {"module": hook_module("def engine_markers(settings):\n    raise OSError('z')\n")}}
            self.assertIsNone(hooks.engine_markers(broken))
        import broker
        policy = broker.Policy(self.tmp, python="py", home=self.tmp, engine_markers=None)
        self.assertEqual(policy.decide("codex", "command", "", command="big-engine -run")[0], "deny")
        self.assertEqual(policy.decide("agy", "command", "", command="py -I agy_run.py")[0], "allow")
        self.assertNotEqual(broker.Policy(self.tmp, python="py", home=self.tmp, engine=True, engine_markers=None)
                            .decide("codex", "command", "", command="big-engine -run")[0], "deny")

    def test_build_blockers_cli(self):
        import hooks
        with mock.patch.object(route, "build_blockers", return_value=["busy"]), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            self.assertEqual(hooks.main(["build-blockers"]), 1)
        self.assertEqual(out.getvalue(), "busy\n")
        with mock.patch.object(route, "build_blockers", return_value=[]):
            self.assertEqual(hooks.main(["build-blockers"]), 0)


class TestRoleUsage(unittest.TestCase):
    """--list shows which offered models no role names."""

    def test_role_usage_maps_each_model_to_its_roles(self):
        rules = {"roles": {"review": {"models": [{"provider": "agy", "model": "a"}, {"provider": "agy", "model": "b"}]},
                           "draft": {"models": [{"provider": "ollama", "model": "b"}, {"provider": "agy", "model": "a"}]},
                           "odd": "not a dict"}}
        used = route.role_usage(rules)
        self.assertEqual(used[("agy", "a")], ["review", "draft"])
        self.assertEqual(used[("agy", "b")], ["review"])
        self.assertEqual(used[("ollama", "b")], ["draft"])
        self.assertNotIn(("agy", "c"), used)

    def test_every_role_model_in_the_real_roles_file_is_counted(self):
        used = route.role_usage()
        self.assertIn(("ollama", "qwen3:14b"), used)
        self.assertIn("index", used[("ollama", "qwen3:14b")])


class ModelIdTests(unittest.TestCase):
    """Keep ids as the only model names across both CLIs."""

    def test_ask_rejects_unknown_ids(self):
        for name in ("gemini-unknown", "custom-old-name"):
            with self.subTest(name=name), \
                 mock.patch.object(sys, "argv", ["ask.py", name, "Question"]), \
                 mock.patch.object(sys, "stderr", io.StringIO()) as err, \
                 mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(free_state()), "NO_COLOR": "1"}):
                self.assertEqual(ask.main(), 2)
                self.assertEqual(err.getvalue(), f"no model is called {name}: --list\n")

    def test_shared_id_requires_provider_and_flags_keep_question(self):
        st = free_state()
        model = "shared-model-id"
        st.update(agy_models=[model], ollama_models=[model], codex_models=[model])
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}):
            with mock.patch.object(sys, "argv", ["ask.py", model, "Question"]), \
                 mock.patch.object(sys, "stderr", io.StringIO()) as err:
                self.assertEqual(ask.main(), 2)
                for provider in ("agy", "ollama", "codex"):
                    self.assertIn(provider, err.getvalue())
                    self.assertIn(f"--{provider} {model}", err.getvalue())
            for provider in ("agy", "ollama", "codex"):
                with self.subTest(provider=provider), \
                     mock.patch.object(sys, "argv", ["ask.py", f"--{provider}", model, "Question words"]), \
                     mock.patch.object(sys, "stdout", io.StringIO()) as out, \
                     mock.patch.object(ask, "_run_cloud", return_value=(0, "Answer", {}, None, "", "ok", None, None)) as cloud, \
                     mock.patch.object(ask, "ask_ollama", return_value=(0, "Answer", {}, None)) as local, \
                     mock.patch.object(route, "claim_gpu", return_value=(True, "ok")), \
                     mock.patch.object(route, "release_local_lock"):
                    self.assertEqual(ask.main(), 0)
                    if provider == "ollama":
                        self.assertEqual(local.call_args.args[1:3], (model, "Question words"))
                    else:
                        self.assertEqual(cloud.call_args.args[0], provider)
                        self.assertEqual(cloud.call_args.args[2:4], (model, "Question words"))
                    self.assertIn(f"[{model},", out.getvalue())

    def test_ids_discovered_without_role_metadata_and_subscription(self):
        st = free_state()
        for provider, key in (("agy", "agy_models"), ("ollama", "ollama_models"), ("codex", "codex_models")):
            st[key] = ["brand-new-id"]
            chk = route.check_name("brand-new-id", fake=st)
            self.assertTrue(chk["ok"], chk)
            self.assertEqual(chk["provider"], provider)
            st[key] = []
        self.assertEqual(route.check_name("claude-fable-5-1", fake=st)["provider"], "claude-subscription")

    def test_old_config_names_are_ignored_but_paths_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "models.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"agy": "custom-agy", "ollama": "custom-ollama", "codex": "custom-codex",
                           "names": {"custom-old-name": ["agy", "gemini-4-argon-high"]}}, f)
            with mock.patch.object(route, "CONFIG_PATH", path):
                self.assertEqual(ask.load_config(), ("custom-agy", "custom-ollama"))
                self.assertEqual(route.codex_path(), "custom-codex")
                self.assertFalse(route.check_name("custom-old-name", fake=free_state())["ok"])
                self.assertNotIn("custom-old-name", route.model_table())

    def test_ollama_tag_is_part_of_the_id(self):
        st = free_state()
        st["ollama_models"] = ["local-model:latest"]
        self.assertTrue(route.check_name("local-model:latest", fake=st)["ok"])
        self.assertEqual(route.check_name("local-model", fake=st)["reason"],
                         "no model is called local-model: --list")

    def test_journal_has_no_alias_and_old_lines_group_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            model = "gemini-4-argon-high"
            row = route.journal("ask.py", None, model, "agy", model,
                                tokens={"total": 12}, journal_path=path)
            self.assertNotIn("alias", row)
            self.assertEqual(row["name"], model)
            old = {**row, "alias": "Артем", "name": "артем", "tokens": {"total": 8}, "status": "quota"}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(old, ensure_ascii=False) + "\n")
            with open(path, encoding="utf-8") as f:
                original = f.read()
            totals = route.read_journal_24h(now=row["ts"] + 1, journal_path=path)
            self.assertEqual(totals, {model: {"model": model, "calls": 2, "tokens": 20, "quota_hits": 1}})
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(free_state()), "NO_COLOR": "1"}), \
                 mock.patch.object(route, "JOURNAL_PATH", path), \
                 mock.patch.object(sys, "stdout", io.StringIO()) as out:
                self.assertEqual(ask.print_status("unused", "unused"), 0)
            self.assertIn(model, out.getvalue())
            self.assertNotIn("Артем", out.getvalue())
            self.assertNotIn("Alias", out.getvalue())
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), original)

    def test_header_and_model_list_show_only_ids(self):
        st = free_state()
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            ask._print_header("gemini-3.8-flash-high", "Gemini Models", [])
            self.assertEqual(out.getvalue(), "[gemini-3.8-flash-high, Gemini Models]\n")
            out.seek(0)
            out.truncate()
            ask.print_list("unused", "unused")
            for row in route.models(fake=st):
                self.assertNotIn("alias", row)
                self.assertNotIn("aliases", row)
                self.assertIn(row["model"], out.getvalue())
            for text in ("Nickname", "Alias", "Артем", "Женя", "Льолік", "Болік", "(Sonnet)", "(Opus)"):
                self.assertNotIn(text, out.getvalue())
        self.assertFalse(hasattr(route, "format_model"))



class WriterTests(unittest.TestCase):
    """Exercise command and accounting boundaries without provider calls."""

    def state(self):
        state = CodexTests().state()
        state["cloud"]["gemini"]["pace"] = 2.6
        return state

    def test_commands_and_legacy_help(self):
        self.assertEqual(writer.build_agy_command("model", "prompt", "settings", "conversation", [HERE]),
                         [writer.AGY, "-p", "prompt", "--model", "model", "--settings", "settings",
                          "--mode", "accept-edits", "--output-format", "stream-json", "--print-timeout", "0s",
                          "--conversation", "conversation", "--add-dir", HERE])
        args = writer.build_codex_command("gpt-6.1-sol", "prompt", HERE, "answer", add_dirs=[HERE, TOOLS_DIR], executable="codex")
        self.assertEqual(args, ["codex", "exec", "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=high",
                                "-s", "workspace-write", "-C", HERE, "--skip-git-repo-check", "--json", "-o", "answer",
                                "--add-dir", HERE, "--add-dir", TOOLS_DIR, "--", "prompt"])
        resumed = writer.build_codex_command("gpt-6.1-sol", "follow-up", HERE, "answer", "thread", executable="codex")
        self.assertEqual(resumed, ["codex", "exec", "resume", "thread", "-m", "gpt-6.1-sol", "-c",
                                  "model_reasoning_effort=high", "--json", "-o", "answer", "--", "follow-up"])
        expected_flags = {"--help", "--cwd", "--brief", "--model", "--tokens", "--force", "--python", "--add-dir",
                          "--continue", "--log", "--engine", "--allow-never", "--max-tokens", "--role", "--needs",
                          "--deadline"}
        self.assertEqual({flag for action in writer.parser(True)._actions for flag in action.option_strings
                          if flag.startswith("--")}, expected_flags)

    def test_role_pick_stream_progress_journal_and_resume_usage(self):
        state = self.state()
        self.assertEqual(route.pick("writer", fake=state)["model"], "gpt-6.1-sol")
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                log = os.path.join(tmp, "writer.json")
                journal_path = os.path.join(tmp, "usage.jsonl")
                events = [
                    {"type": "thread.started", "thread_id": "writer-thread"},
                    {"type": "item.started", "item": {"id": "cmd", "type": "command_execution", "command": "inspect", "status": "in_progress"}},
                    {"type": "item.completed", "item": {"id": "cmd", "type": "command_execution", "command": "inspect", "exit_code": 0, "status": "completed"}},
                    {"type": "item.completed", "item": {"type": "file_change", "changes": [
                        {"path": "one.py", "kind": "update"}, {"path": "two.py", "kind": "add"}]}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "streamed answer"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                         "output_tokens": 20, "reasoning_output_tokens": 10}},
                ]
                proc = mock.Mock(stdout=io.StringIO("\n".join(json.dumps(e) for e in events)), returncode=0)

                def launch_command(command, **kwargs):
                    with open(command[command.index("-o") + 1], "w", encoding="utf-8") as handle:
                        handle.write("final answer")
                    return proc

                args = ["--legacy-cli", "--cwd", tmp, "--log", log, "--add-dir", HERE]
                args += (["--model", "gpt-6.1-sol", "--codex", "--continue", "writer-thread", "Continue"] if resume
                         else ["--brief", os.path.join(tmp, "brief.md")])
                with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(state), "NO_COLOR": "1"}), \
                     mock.patch.object(route, "JOURNAL_PATH", journal_path), \
                     mock.patch.object(route, "codex_path", return_value="fake-codex"), \
                     mock.patch.object(writer.subprocess, "Popen", side_effect=launch_command) as launch, \
                     mock.patch.object(route, "release_reservation", wraps=route.release_reservation) as release, \
                     mock.patch.object(sys, "stdout", io.StringIO()) as output, \
                     mock.patch.object(sys, "stderr", io.StringIO()) as errors:
                    self.assertEqual(writer.main(args), 0)
                self.assertIn("final answer", output.getvalue())
                self.assertIn("conversation writer-thread, 94 billed tokens (fresh 90, cached 40), failed False", output.getvalue())
                self.assertIn("[pool ChatGPT", output.getvalue())
                self.assertIn("the caller runs the tests", errors.getvalue())
                self.assertEqual(launch.call_args.kwargs["stdin"], writer.subprocess.DEVNULL)
                self.assertEqual(launch.call_args.kwargs["cwd"], tmp)
                prompt = launch.call_args.args[0][-1]
                self.assertIn("Never commit or push", prompt)
                self.assertIn(HERE, prompt)
                if not resume:
                    self.assertTrue(prompt.startswith("Your brief is the file "))
                release.assert_called_once()
                self.assertEqual(release.call_args.args[0]["provider"], "codex")
                with open(log[:-5] + ".progress.txt", encoding="utf-8") as handle:
                    progress = handle.read()
                for text in ("command inspect", "exit 0", "file update one.py", "file add two.py"):
                    self.assertIn(text, progress)
                with open(log[:-5] + ".events.jsonl", encoding="utf-8") as handle:
                    self.assertEqual([json.loads(line) for line in handle], events)
                with open(journal_path, encoding="utf-8") as handle:
                    row = json.loads(handle.readline())
                self.assertEqual(row["conversation"], "writer-thread")
                self.assertEqual(row["tokens"], {"in": 100, "out": 30, "total": 130, "cached_in": 40, "fresh": 90, "billed": 94})
                self.assertEqual(row["provider"], "codex")
                self.assertEqual(row["status"], "ok")
                self.assertIn("weekly", row["before"])
                self.assertIn("weekly", row["after"])
                self.assertTrue(os.path.isfile(row["log"]))
                self.assertNotIn("conversation_tokens", row)

    def test_failed_event_and_failed_launch_release_and_journal(self):
        for failure in (OSError("cannot start CLI"), None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "usage.jsonl")
                proc = mock.Mock(returncode=0, stdout=io.StringIO(json.dumps(
                    {"type": "turn.failed", "error": {"message": "quota exceeded"}}) + "\n"))
                with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state()), "NO_COLOR": "1"}), \
                     mock.patch.object(route, "JOURNAL_PATH", path), \
                     mock.patch.object(route, "codex_path", return_value="fake-codex"), \
                     mock.patch.object(writer.subprocess, "Popen", side_effect=failure, return_value=proc), \
                     mock.patch.object(route, "release_reservation", wraps=route.release_reservation) as release, \
                     mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
                    result = writer.main(["--legacy-cli", "--cwd", tmp, "--brief", "brief.md", "--model", "gpt-6.1-sol", "--codex"])
                self.assertNotEqual(result, 0)
                release.assert_called_once()
                with open(path, encoding="utf-8") as handle:
                    row = json.loads(handle.readline())
                self.assertEqual(row["status"], "error" if failure else "quota")

    def test_codex_engine_and_missing_resume_model_refused_before_launch(self):
        # The user's platform traits now explain the refusal, through check_name.
        for provider_flags in ([], ["--codex"]):
            with self.subTest(provider_flags=provider_flags), \
                 mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state()), "NO_COLOR": "1"}), \
                 mock.patch.object(route, "host_platform", return_value="win32"), \
                 mock.patch.object(route, "check_name", wraps=route.check_name) as check, \
                 mock.patch.object(writer.subprocess, "Popen") as launch, \
                 mock.patch.object(sys, "stderr", io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    writer.main(["--cwd", HERE, "--brief", "brief.md", "--model", "gpt-6.1-sol", "--engine"] + provider_flags)
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("gpt-6.1-sol cannot engine here (traits in roles.json)", errors.getvalue())
                self.assertEqual(check.call_args.kwargs["needs"], ["engine"])
                launch.assert_not_called()
        with mock.patch.object(route, "check_name") as check:
            with self.assertRaises(SystemExit) as stopped:
                writer.main(["--cwd", HERE, "--brief", "brief.md", "--continue", "thread"])
            self.assertIn("--continue needs --model", str(stopped.exception.code))
            check.assert_not_called()


    def test_verdict_identity_refusal_manual_runs_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            route.journal("writer.py", "writer", "gpt-6.1-sol", "codex", "gpt-6.1-sol",
                          tokens={"total": 100}, conversation="known", journal_path=path)
            with mock.patch.object(route, "JOURNAL_PATH", path), mock.patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(writer.main(["--verdict", "known", "fixed", "coordinator's fixes"]), 0)
            for kwargs in ({}, {"model": "gpt-6.1-sol"}, {"provider": "codex"}):
                with self.assertRaisesRegex(ValueError, "unknown conversation"):
                    route.writer_verdict("unknown", "ok", "note", journal_path=path, **kwargs)
            with self.assertRaisesRegex(ValueError, "only allowed"):
                route.writer_verdict("known", "ok", "note", model="other", provider="agy", journal_path=path)
            with mock.patch.object(route, "JOURNAL_PATH", path), mock.patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(writer.main(["--verdict", "manual", "ok", "35 tests passed",
                                              "--model", "gpt-6.1-sol", "--codex"]), 0)
            with self.assertRaisesRegex(ValueError, "ok, fixed or rejected"):
                route.writer_verdict("known", "maybe", "note", journal_path=path)
            with open(path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle]
            self.assertEqual(set(rows[1]), {"time", "type", "conversation", "model", "provider", "verdict", "note"})
            self.assertEqual(rows[1]["model"], "gpt-6.1-sol")
            self.assertEqual(rows[1]["provider"], "codex")
            self.assertEqual(route.read_journal_24h(journal_path=path)["gpt-6.1-sol"]["calls"], 1)

    def test_notes_and_30_day_counts_in_list(self):
        state = self.state()
        now = route.datetime.now(route.timezone.utc)
        recent = {"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "type": "verdict", "provider": "codex",
                  "model": "gpt-6.1-sol", "conversation": "thread", "verdict": "fixed"}
        state["journal"] = [recent, {**recent, "verdict": "ok"},
                            {**recent, "time": "2000-01-01T00:00:00Z", "verdict": "rejected"}]
        counts = route.verdict_counts(fake=state)
        self.assertEqual(counts[("codex", "gpt-6.1-sol")], {"ok": 1, "fixed": 1, "rejected": 0})
        rules = route.load_roles()
        self.assertEqual(sum(len(notes) for notes in rules["capabilities"].values()), 6)
        for with_notes in (False, True):
            args = ["ask.py", "--list"] + (["--notes"] if with_notes else [])
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(state), "NO_COLOR": "1"}), \
                 mock.patch.object(sys, "argv", args), mock.patch.object(sys, "stdout", io.StringIO()) as output:
                self.assertEqual(ask.main(), 0)
            text = output.getvalue()
            self.assertIn("Verdicts", text)
            self.assertIn("1 ok, 1 fixed, 0 rejected", text)
            if with_notes:
                for notes in rules["capabilities"].values():
                    for note in notes:
                        self.assertIn(f"{note['task']}: {note['note']} ({note['evidence']}, {note['date']})", text)
                self.assertIn("writer: 1 ok, 1 fixed, 0 rejected", text)
            else:
                self.assertNotIn("Windows sandbox cannot start Python", text)


class TestLongQuestion(unittest.TestCase):
    """A question with a file appended went on agy's command line and overran Windows' ~32K cap."""

    def test_agy_question_goes_by_stdin(self):
        long_text = "рядок\n" * 20000
        done = mock.Mock(returncode=0, stdout=json.dumps({"response": "ok", "usage": {}}).encode(), stderr=b"")
        with mock.patch.object(ask.subprocess, "run", return_value=done) as run, \
             mock.patch.object(route, "save_raw_log", return_value="log"):
            ask.ask_agy("agy", "gemini-3.8-flash-low", long_text, "conv-1", 30)
        args = run.call_args.args[0]
        self.assertNotIn("-p", args)
        self.assertLess(sum(len(x) for x in args), 1000)
        self.assertEqual(args[args.index("--conversation") + 1], "conv-1")
        self.assertEqual(run.call_args.kwargs["input"], long_text.encode("utf-8"))


class TestBilledTokens(unittest.TestCase):
    """One unit and rates learned across rounded readings."""

    def setUp(self):
        self.rules = route.load_roles()
        self.now = 1793880000.0

    def journal_file(self, folder, rows):
        path = os.path.join(folder, "usage.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path

    def row(self, index, before, after, tokens=1_000_000, **extra):
        return dict({"ts": self.now - 100 + index, "provider": "codex", "role": "analysis",
                     "pool": "ChatGPT (prolite)", "tokens": {"billed": tokens},
                     "before": {"weekly": before}, "after": {"weekly": after}}, **extra)

    def test_billed_tokens_and_recorded_values(self):
        tok = {"in": 1_974_118, "cached_in": 1_866_496, "out": 16_593}
        expected = {"fresh": 124_215, "cached": 1_866_496, "billed": 310_865}
        self.assertEqual(route.billed_tokens(tok, "codex", "cHaTgPt (prolite)", self.rules), expected)
        self.assertEqual(route.billed_tokens({"tokens": tok, "pool": "ChatGPT (prolite)"},
                                             "codex", rules=self.rules), expected)
        # No pool given: Codex is the ChatGPT pool, so its cache weight holds (the review of).
        self.assertEqual(route.billed_tokens(tok, "codex", rules=self.rules)["billed"], 310_865)
        self.assertEqual(route.billed_tokens(tok, "codex", "", self.rules)["billed"], 310_865)
        self.assertEqual(route.billed_tokens({"tokens": {"in": 100, "out": 25}}, "agy", rules=self.rules),
                         {"fresh": 125, "cached": 0, "billed": 125})
        self.assertEqual(route.billed_tokens(dict(tok, fresh=12, billed=34), "codex", rules=self.rules),
                         {"fresh": 12, "cached": 1_866_496, "billed": 34})
        self.assertEqual(route.billed_tokens(dict(tok, billed=0), "codex", rules=self.rules)["billed"], 0)
        self.assertEqual(route.billed_tokens({"in": 10, "out": 5, "cached_in": 100}, "agy",
                                             "Other", {"cache_weight": {"default": 0.2}})["billed"], 35)

    def test_journal_and_status_use_billed_preserve_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            tok = {"in": 1_974_118, "cached_in": 1_866_496, "out": 16_593, "total": 1_990_711}
            row = route.journal("writer.py", "analysis", None, "codex", "gpt-6.1-sol", tokens=tok,
                                pool="ChatGPT (prolite)", journal_path=path, rules=self.rules)
            self.assertEqual(row["tokens"], dict(tok, fresh=124_215, billed=310_865))
            self.assertNotIn("fresh", tok)
            totals = route.read_journal_24h(now=row["ts"] + 1, journal_path=path, rules=self.rules)
            self.assertEqual(totals["gpt-6.1-sol"]["tokens"], 310_865)

    def test_estimate_provider_then_role_then_default(self):
        rows = [dict(self.row(i, 1, 1, tokens=n), provider=prov)
                for i, (prov, n) in enumerate([("agy", 10), ("agy", 20), ("agy", 30),
                                               ("codex", 100), ("codex", 200), ("codex", 300)])]
        rows += [self.row(7, 1, 1, tokens=999999, type="verdict"), self.row(8, 1, 1, tokens=0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.journal_file(tmp, rows)
            def estimate(provider, role="analysis"):
                return route.estimate_tokens(role, provider=provider, model="a-different-model",
                                             rules=self.rules, journal_path=path)
            self.assertEqual(estimate("codex"), 200)
            self.assertEqual(estimate("agy"), 20)
            self.assertEqual(estimate("ollama"), 65)
            self.assertEqual(estimate("codex", "review"), self.rules["tokens_default"]["review"])
            self.journal_file(tmp, rows[:5] + rows[6:])
            self.assertEqual(estimate("codex"), 30)
            self.journal_file(tmp, [self.row(i, 1, 1, tokens=i + 1) for i in range(20)])
            self.assertEqual(estimate("codex"), 15)

    def test_series_rounding_resets_and_fine_readings(self):
        cases = [([(1.00, 1.00), (0.99, 0.99), (0.98, 0.98)], 0.03 / 3),
                 ([(1.00, 0.99), (0.99, 0.99), (0.99, 0.98)], 0.03 / 3),
                 ([(1.00, 1.00), (0.99, 0.99), (1.00, 0.99)], 0.04 / 3),
                 ([(0.8123, 0.8113), (0.8103, 0.8103), (0.8093, 0.8083)], 0.004 / 3)]
        with tempfile.TemporaryDirectory() as tmp:
            for readings, expected in cases:
                with self.subTest(readings=readings):
                    rows = [self.row(i, before, after) for i, (before, after) in enumerate(readings)]
                    # Timestamp fallback and sorting must preserve the series.
                    ts = rows[1].pop("ts")
                    rows[1]["time"] = route.datetime.fromtimestamp(ts, route.timezone.utc).isoformat()
                    path = self.journal_file(tmp, list(reversed(rows)))
                    self.assertAlmostEqual(route.pool_window_rate("CHATGPT", rules=self.rules,
                                                                  journal_path=path, now=self.now), expected)
            rows = [dict(self.row(i, b, a), provider="agy", pool="Gemini Models")
                    for i, (b, a) in enumerate(cases[-1][0])]
            path = self.journal_file(tmp, rows)
            self.assertAlmostEqual(route.pool_window_rate("Gemini Models", rules=self.rules,
                                                          journal_path=path, now=self.now), 0.004 / 3)

    def test_rate_thresholds_and_filtered_rows(self):
        good = [self.row(i, 1 - i / 100, 1 - i / 100) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            for rows in (good[:2], [self.row(i, 1, .99, tokens=300000) for i in range(3)],
                         [dict(r, ts=self.now - 15 * 86400) for r in good]):
                path = self.journal_file(tmp, rows)
                self.assertEqual(route.pool_window_rate("ChatGPT", rules=self.rules,
                                                        journal_path=path, now=self.now), 0.05)
            ignored = [self.row(5, 1, 0, type="verdict"), self.row(6, 1, 0, tokens=0),
                       self.row(7, 1, 0, pool="Gemini Models"), self.row(8, 1, 0, provider="ollama"),
                       self.row(9, 1, 0, ts=self.now - 15 * 86400),
                       dict(self.row(10, 1, 0), after={})]
            path = self.journal_file(tmp, ignored + good)
            self.assertAlmostEqual(route.pool_window_rate("ChatGPT", rules=self.rules,
                                                          journal_path=path, now=self.now), .01)

    def test_old_codex_rows_use_same_billed_unit_for_rate_and_estimate(self):
        rows = [dict(self.row(i, 1 - i / 100, 1 - i / 100),
                     tokens={"in": 4_000_000, "cached_in": 3_000_000, "out": 0, "total": 4_000_000})
                for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.journal_file(tmp, rows)
            self.assertAlmostEqual(route.pool_window_rate("ChatGPT", rules=self.rules,
                                                          journal_path=path, now=self.now), .03 / 3.9)
            self.assertEqual(route.estimate_tokens("analysis", provider="codex", rules=self.rules,
                                                   journal_path=path), 1_300_000)
            for row, tokens in zip(rows, (333333, 333333, 333334)):
                row["tokens"] = {"billed": tokens}
            self.journal_file(tmp, rows)
            self.assertAlmostEqual(route.pool_window_rate("ChatGPT", rules=self.rules,
                                                          journal_path=path, now=self.now), .03)

    def test_meters_weight_cache_and_difference_before_rounding(self):
        meter = writer.AgyMeter("Gemini Models", {"cache_weight": {"Gemini Models": .1}})
        for i, cached in enumerate((100, 200)):
            meter.consume({"step_index": i, "usage": {"input_tokens": 90, "output_tokens": 10,
                                                       "total_tokens": 100, "cache_read_tokens": cached}})
        self.assertEqual(meter.billed(), 230)
        meter.rules = self.rules
        self.assertEqual(meter.billed(), 200)
        codex = writer.CodexMeter(rules=self.rules)
        total = {"input_tokens": 100, "cached_input_tokens": 15, "output_tokens": 10,
                 "reasoning_output_tokens": 5}
        baseline = {"input_tokens": 10, "cached_input_tokens": 5}
        self.assertEqual(codex.billed_of(total, baseline), 96)
        self.assertEqual(codex.billed_of(total), 102)

    def test_agy_cache_parsing_and_conversation_deltas(self):
        raw = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "cache_read_tokens": 500}
        for extract in (ask._extract_tokens, writer._extract_tokens):
            self.assertEqual(extract(raw)["cached_in"], 500)
        _, parsed = ask._parse_agy_json(json.dumps({"response": "done", "usage": raw}), "")
        self.assertEqual(parsed["cached_in"], 500)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "conversations.json")
            first, _, _ = route.record_conversation_usage("c", ask._extract_tokens(raw), conversations_path=path)
            self.assertEqual(first["cached_in"], 500)
            raw.update(input_tokens=200, output_tokens=40, total_tokens=240, cache_read_tokens=900)
            delta, total, _ = route.record_conversation_usage("c", writer._extract_tokens(raw), True, path)
            self.assertEqual(delta, {"in": 100, "out": 20, "total": 120, "cached_in": 400})
            self.assertEqual(total["cached_in"], 900)
            self.assertEqual(route.load_agy_conversations(path)["c"]["cached"], 900)
            route.save_agy_conversations({"c": {"in": 100, "out": 20, "total": 120}}, path)
            delta, _, _ = route.record_conversation_usage("c", ask._extract_tokens(raw), True, path)
            self.assertEqual(delta["cached_in"], 900)

    def test_agy_print_timeout_is_journalled_as_timeout(self):
        raw = ('{"response": "partial", "finished_reason": "print_timeout"}\n--- stderr ---\n'
               'AGY_ERROR: {"status":"DEADLINE_EXCEEDED","retryable":false,"reason":"print_timeout"}')
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ask, "ask_agy", return_value=(1, "partial", {}, "fake.log", raw)):
            result = ask._run_agy_with_retry_and_accounting("fake", "gemini-4-argon-high", "q", False, 10,
                                                           conversations_path=os.path.join(tmp, "c.json"))
        self.assertEqual(result[5], "timeout")
        limit = raw.replace('"partial"', '"raise the rate limit (429)"')
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(ask, "ask_agy", return_value=(1, "partial", {}, "fake.log", limit)):
            result = ask._run_agy_with_retry_and_accounting("fake", "gemini-4-argon-high", "q", False, 10,
                                                           conversations_path=os.path.join(tmp, "c.json"))
        self.assertEqual(result[5], "timeout")
        self.assertFalse(ask.is_timeout('{"response": "fine"}'))

    def test_agy_retry_sums_cache_deltas(self):
        replies = []
        for answer, total, cached in (("", 100, 500), ("done", 150, 800)):
            raw = json.dumps({"conversation_id": "retry", "response": answer,
                              "denied_actions": ["tool"] if not answer else [],
                              "usage": {"input_tokens": total, "output_tokens": 0,
                                        "total_tokens": total, "cache_read_tokens": cached}})
            replies.append((0, answer, {}, "fake.log", raw))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ask, "ask_agy", side_effect=replies):
            result = ask._run_agy_with_retry_and_accounting("fake", "gemini-4-argon-high", "q", False, 10,
                                                           conversations_path=os.path.join(tmp, "conversations.json"))
        self.assertEqual(result[2], {"in": 150, "out": 0, "total": 150, "cached_in": 800})

    def test_provider_estimate_reaches_fit_reservation_and_shares(self):
        state = WriterTests().state()
        state["journal"] = [dict(self.row(i, 1, 1, tokens=100000), provider="codex") for i in range(3)]
        state["journal"] += [dict(self.row(i, 1, 1, tokens=900000), provider="agy", pool="Gemini Models")
                             for i in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            state["reservations_path"] = os.path.join(tmp, "reservations.json")
            choice = route.pick("analysis", rules=self.rules, fake=state)
            self.assertEqual(choice["provider"], "codex")
            self.assertEqual(choice["estimated_tokens"], 100000)
            check = route.check_name("gpt-6.1-sol", role="analysis", rules=self.rules, fake=state)
            self.assertEqual(check["estimated_tokens"], 100000)
            reservation = route.reserve_quota("writer.py", "analysis", "codex", "gpt-6.1-sol",
                                               rules=self.rules, fake=state, reservations_path=state["reservations_path"])
            self.assertEqual(reservation["tokens"], 100000)
            cs = route.cloud_state(fake=state, rules=self.rules)
            shares = route.compute_needed_shares("codex", "gpt-6.1-sol", None, cs, role="analysis",
                                                  rules=self.rules, fake=state)
            self.assertEqual(shares, reservation["share"])


class TestWriterHardStop(unittest.TestCase):
    """--max-tokens stops a writer mid-run: gemini-4-argon-high ran 1.4M against a 400K cap in a brief."""

    def test_agy_meter_counts_each_step_once(self):
        meter = writer.AgyMeter()
        for index, total in ((1, 100), (2, 50), (2, 50), (3, 25)):
            meter.consume({"conversation_id": "c", "step_index": index,
                           "usage": {"input_tokens": total - 1, "output_tokens": 1, "total_tokens": total}})
        meter.consume({"step_index": 4, "step_type": "tool"})
        self.assertEqual((meter.fresh(), meter.conversation), (175, "c"))

    def test_codex_meter_reads_the_rollout_as_it_grows(self):
        with tempfile.TemporaryDirectory() as home:
            folder = os.path.join(home, "sessions", "2026", "11", "04")
            os.makedirs(folder)
            path = os.path.join(folder, "rollout-2026-11-04T11-00-00-thread-x.jsonl")

            def count(inp, cached, out, reasoning):
                return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
                    "reasoning_output_tokens": reasoning}}}}) + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(count(1000, 600, 10, 5))
            with mock.patch.object(writer, "CODEX_HOME", home):
                meter = writer.CodexMeter()
                self.assertIsNone(meter.read("other-thread"))
                self.assertEqual(meter.read("thread-x"), 475)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(count(5000, 3000, 100, 50))
                    f.write('{"type": "event_msg", "payload": {"type": "token_co')  # a line still being written
                self.assertEqual(meter.read("thread-x"), 2450)

    def test_legacy_agy_run_over_the_cap_is_stopped_and_journalled(self):
        state = WriterTests().state()
        steps = [{"event": "step_update", "step_update": {"conversation_id": "conv-cap", "step_index": i, "step_type": "x",
                  "usage": {"input_tokens": 9000, "output_tokens": 1000, "total_tokens": 10000}}} for i in range(1, 6)]
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = os.path.join(tmp, "usage.jsonl")
            proc = mock.Mock(stdout=io.StringIO("\n".join(json.dumps(e) for e in steps)), returncode=1)
            proc.poll.return_value = None
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(state), "NO_COLOR": "1"}), \
                 mock.patch.object(route, "JOURNAL_PATH", journal_path), \
                 mock.patch.object(route, "AGY_CONVERSATIONS_PATH", os.path.join(tmp, "conv.json"), create=True), \
                 mock.patch.object(route, "load_agy_conversations", return_value={}), \
                 mock.patch.object(route, "save_agy_conversations"), \
                 mock.patch.object(writer.subprocess, "Popen", return_value=proc), \
                 mock.patch.object(writer, "stop_tree") as stop, \
                 mock.patch.object(sys, "stdout", io.StringIO()) as output, mock.patch.object(sys, "stderr", io.StringIO()):
                code = writer.main(["--legacy-cli", "--cwd", tmp, "--log", os.path.join(tmp, "w.json"), "--brief", "b.md",
                                    "--model", "gemini-4-argon-high", "--agy", "--max-tokens", "25000"])
            self.assertEqual(code, 5)
            stop.assert_called_once_with(proc)
            self.assertIn("hard stop: 30,000 billed tokens (fresh + cached x w), over --max-tokens 25,000", output.getvalue())
            self.assertIn("--continue conv-cap", output.getvalue())
            with open(journal_path, encoding="utf-8") as f:
                row = json.loads(f.readline())
            self.assertEqual((row["status"], row["conversation"], row["tokens"]["total"]), ("capped", "conv-cap", 50000))


class TestWorkloadRoles(unittest.TestCase):
    """The coordinator names workloads, not models."""

    def test_analysis_goes_to_codex_first_and_engine_writer_to_a_subagent(self):
        state = CodexTests().state()
        self.assertEqual(route.pick("analysis", fake=state)["model"], "gpt-6.1-sol")
        rules = copy.deepcopy(route.load_roles())
        rules["roles"]["analysis"]["models"] = [m for m in rules["roles"]["analysis"]["models"] if m["provider"] != "codex"]
        self.assertEqual(route.pick("analysis", fake=state, rules=rules)["model"], "gemini-4-argon-high")
        engine = route.pick("engine-writer", fake=state)
        self.assertTrue(engine["fallback"])
        self.assertEqual((engine["exit_code"], engine["message"]), (6, "use a Claude subagent (claude-sonnet-5-5)"))

    def test_code_writer_goes_to_codex_and_refuses_the_engine(self):
        state = CodexTests().state()
        self.assertEqual(route.pick("code-writer", fake=state)["model"], "gpt-6.1-sol")  # astra not offered
        state["codex_models"]["result"]["data"].append({"id": "gpt-6-astra", "hidden": False})
        self.assertEqual(route.pick("code-writer", fake=state)["model"], "gpt-6-astra")
        for role in ("code-writer", "analysis"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp,                  mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(state), "NO_COLOR": "1"}),                  mock.patch.object(writer.subprocess, "Popen") as launch,                  mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    writer.main(["--role", role, "--engine", "--cwd", tmp, "--brief", "b.md"])
                self.assertEqual(stopped.exception.code, 2)
                launch.assert_not_called()
                self.assertIn("--engine is refused", errors.getvalue())

    def test_writer_role_flag(self):
        state = CodexTests().state()
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = os.path.join(tmp, "usage.jsonl")
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(state), "NO_COLOR": "1"}), \
                 mock.patch.object(route, "JOURNAL_PATH", journal_path), \
                 mock.patch.object(writer.subprocess, "Popen") as launch, \
                 mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as stopped:
                    writer.main(["--role", "engine-writer", "--cwd", tmp, "--brief", "b.md"])
                self.assertEqual(stopped.exception.code, 6)
                launch.assert_not_called()
                self.assertIn("use a Claude subagent (claude-sonnet-5-5)", errors.getvalue())
                with self.assertRaises(SystemExit) as bad:
                    writer.main(["--role", "nonsense", "--cwd", tmp, "--brief", "b.md"])
                self.assertEqual(bad.exception.code, 2)
            with open(journal_path, encoding="utf-8") as f:
                row = json.loads(f.readline())
            self.assertEqual((row["role"], row["status"], row["model"]), ("engine-writer", "fallback", "claude-sonnet-5-5"))


class TestNoFalseQuota(unittest.TestCase):
    """An answer that itself says "429" or "limit" is not a quota failure (three finished writers were
    journalled as quota because their work's text held "429")."""

    def test_an_answer_with_429_in_it_is_ok_and_not_passed_over(self):
        st = free_state()
        with tempfile.TemporaryDirectory() as tmp:
            jpath = os.path.join(tmp, "usage.jsonl")
            st["reservations_path"] = os.path.join(tmp, "reservations.json")
            calls = []

            def fake_ask_agy(agy, model, text, carry_on, timeout_s, name="agy", verbose=False):
                calls.append(model)
                answer = "The table gives 4290 kg/ha; the rate limit of the pump is 429 l/h."
                raw = json.dumps({"response": answer, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}})
                return 0, answer, {"in": 10, "out": 5, "total": 15}, os.path.join(tmp, "a.log"), raw

            orig_journal = route.journal
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}),                  mock.patch.object(ask, "ask_agy", side_effect=fake_ask_agy),                  mock.patch.object(route, "journal", side_effect=lambda *a, **kw: orig_journal(*a, journal_path=jpath, **kw)),                  mock.patch.object(sys, "argv", ["ask.py", "--legacy-cli", "--role", "review", "Скільки?"]):
                with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
                    rc = ask.main()
            self.assertEqual(rc, 0)
            self.assertEqual(calls, ["gemini-4-argon-high"])
            with open(jpath, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(rows[-1]["status"], "ok")


class PolicyTests(unittest.TestCase):
    """Context, account labels and the policy over them: work code never reaches a personal account by accident, and a
    personal task never spends the company's quota."""

    WORK_DOMAIN = "corp.example"

    def state(self, machine=None):
        st = CodexTests().state()
        st["machine"] = machine if machine is not None else {}
        return st

    def env(self, st, context=None):
        e = {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}
        if context is not None:
            e["ASK_CONTEXT"] = context
        return e

    def last_journal(self):
        with open(route.JOURNAL_PATH, encoding="utf-8") as f:
            return json.loads(f.read().splitlines()[-1])

    def run_ask(self, argv, st, context="work", extra_env=None):
        env = self.env(st, context)
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(sys, "argv", ["ask.py"] + argv), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out, \
             mock.patch.object(sys, "stderr", io.StringIO()) as err, \
             mock.patch.object(ask, "_run_cloud", return_value=(0, "Answer", {"total": 10}, None, "", "ok", None, None)) as cloud, \
             mock.patch.object(ask, "ask_ollama", return_value=(0, "Local answer", {}, None)) as local, \
             mock.patch.object(route, "claim_gpu", return_value=(True, "ok")), \
             mock.patch.object(route, "release_local_lock"):
            code = ask.main()
        return code, out.getvalue(), err.getvalue(), cloud, local

    # --- context ---

    def test_context_flag_over_env_and_unknown_is_work(self):
        self.assertEqual(route.resolve_context("personal", env={"ASK_CONTEXT": "work"})["context"], "personal")
        self.assertEqual(route.resolve_context(None, env={"ASK_CONTEXT": "Personal"})["context"], "personal")
        self.assertEqual(route.resolve_context(None, env={"ASK_CONTEXT": "work"})["source"], "ASK_CONTEXT")
        for flag, env in ((None, {}), (None, {"ASK_CONTEXT": ""}), (None, {"ASK_CONTEXT": "persnal"}),
                          ("home", {"ASK_CONTEXT": "personal"})):
            with self.subTest(flag=flag, env=env):
                ctx = route.resolve_context(flag, env=env)
                self.assertEqual(ctx["context"], "work")
                self.assertTrue(ctx["strict_default"])
                self.assertIn("treated as work", ctx["note"])
        self.assertEqual(route.resolve_context("", env={"ASK_CONTEXT": "personal"})["context"], "work")
        self.assertEqual(route.policy_for("codex", context={"context": "unknown"}, machine={})["context"], "work")

    def test_context_by_folder_when_env_is_unset(self):
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            work = os.path.join(home, "work", "repo")
            mine = os.path.join(home, "code")
            outside = tempfile.mkdtemp()
            for d in (work, mine):
                os.makedirs(d)
            machine = {"context_paths": {home: "personal", os.path.join(home, "work"): "work", "/x": "bogus"}}
            self.addCleanup(os.rmdir, outside)
            with self.subTest("longest folder wins"):
                ctx = route.resolve_context(None, env={}, cwd=work, machine=machine)
                self.assertEqual((ctx["context"], ctx["source"], ctx["strict_default"]), ("work", "context_paths", False))
                self.assertIn("by folder", ctx["note"])
            with self.subTest("home rule"):
                self.assertEqual(route.resolve_context(None, env={}, cwd=mine, machine=machine)["context"], "personal")
            with self.subTest("the folder itself"):
                self.assertEqual(route.resolve_context(None, env={}, cwd=home, machine=machine)["context"], "personal")
            with self.subTest("no rule holds it: strict"):
                ctx = route.resolve_context(None, env={}, cwd=outside, machine=machine)
                self.assertEqual((ctx["context"], ctx["strict_default"]), ("work", True))
            with self.subTest("a prefix that is not a parent folder"):
                sibling = home + "-other"
                self.assertIsNone(route.context_by_path(sibling, machine))
            with self.subTest("ASK_CONTEXT and --context still win"):
                self.assertEqual(route.resolve_context(None, env={"ASK_CONTEXT": "work"}, cwd=mine,
                                                       machine=machine)["context"], "work")
                self.assertEqual(route.resolve_context("work", env={}, cwd=mine, machine=machine)["context"], "work")
            with self.subTest("a typo in ASK_CONTEXT is not rescued by the folder"):
                ctx = route.resolve_context(None, env={"ASK_CONTEXT": "persnal"}, cwd=mine, machine=machine)
                self.assertEqual((ctx["context"], ctx["strict_default"]), ("work", True))
            with self.subTest("env given without machine: no real machine.json read"):
                with mock.patch.object(route, "load_machine", side_effect=AssertionError("read")):
                    self.assertEqual(route.resolve_context(None, env={}, cwd=mine)["context"], "work")
        self.assertFalse(route.policy_for("codex", context={"context": "unknown"}, machine={})["allowed"])

    # --- labels ---

    def test_labels_explicit_then_email_domain_then_personal(self):
        machine = {"work_domains": [self.WORK_DOMAIN],
                   "accounts": {"agy": {"email": "someone@" + self.WORK_DOMAIN},
                                "codex": {"label": "personal", "email": "someone@sub." + self.WORK_DOMAIN}}}
        agy = route.account_info("agy", machine)
        self.assertEqual((agy["label"], agy["source"]), ("work", "email domain " + self.WORK_DOMAIN))
        codex = route.account_info("codex:~/.codex", machine)  # explicit label wins over a work domain
        self.assertEqual((codex["label"], codex["source"]), ("personal", "machine.json"))
        self.assertFalse(codex["work_ok"])  # personal is not allowed at work unless said so
        self.assertEqual(route.account_info("codex:~/.codex", {"work_domains": [self.WORK_DOMAIN], "accounts": {
            "codex": {"email": "someone@sub." + self.WORK_DOMAIN}}})["label"], "work")  # a subdomain is the company's too
        self.assertEqual(route.account_info("agy", {"accounts": {"agy": {"email": "me@example.org"}}})["label"], "personal")
        self.assertEqual(route.account_info("agy", {})["label"], "personal")
        self.assertEqual(route.account_info("ollama", {})["label"], "local")
        self.assertEqual(route.account_info("claude-subscription", {})["label"], "session")
        self.assertEqual(route.account_info("agy", {"accounts": {"agy": "work"}})["label"], "work")  # a bare label

    def test_codex_account_follows_codex_home_and_exact_key_wins(self):
        machine = {"accounts": {"codex": {"label": "personal"}, "codex:~/.codex-work": {"label": "work"}}}
        with mock.patch.dict(os.environ, {"CODEX_HOME": "~/.codex-work"}):
            self.assertEqual(route.account_of("codex"), "codex:~/.codex-work")
            self.assertEqual(route.account_info(route.account_of("codex"), machine)["label"], "work")
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(route.account_of("codex"), "codex:~/.codex")
            self.assertEqual(route.account_info(route.account_of("codex"), machine)["label"], "personal")

    def test_unreadable_machine_json_counts_every_account_personal(self):
        path = os.path.join(route.CACHE_DIR, "broken-machine.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        machine = route.load_machine(path=path, fake={"cloud": {}})
        self.assertIn("_error", machine)
        self.assertEqual(route.account_info("codex:~/.codex", machine)["label"], "personal")
        self.assertEqual(route.load_machine(path=os.path.join(route.CACHE_DIR, "absent.json"), fake={"cloud": {}}), {})

    # --- policy ---

    def test_policy_table(self):
        machine = {"accounts": {"agy": {"label": "work"}, "codex": {"label": "personal", "work_ok": True}}}
        work = {p: route.policy_for(p, context="work", machine=machine) for p in ("agy", "ollama", "codex")}
        self.assertEqual([work[p]["rank"] for p in ("agy", "ollama", "codex")], [0, 1, 2])
        self.assertTrue(all(w["allowed"] for w in work.values()))
        self.assertEqual([p for p, w in work.items() if w["crossed"]], ["codex"])
        bare = route.policy_for("codex", context="work", machine={})
        self.assertFalse(bare["allowed"])
        self.assertIn("work_ok: true", bare["reason"])
        personal = {p: route.policy_for(p, context="personal", machine=machine) for p in ("agy", "ollama", "codex")}
        self.assertFalse(personal["agy"]["allowed"])
        self.assertIn("company's quota", personal["agy"]["reason"])
        self.assertTrue(personal["codex"]["allowed"] and personal["ollama"]["allowed"])
        self.assertFalse(any(p["crossed"] for p in personal.values()))

    def test_role_in_work_with_only_personal_accounts_is_refused_with_its_own_code(self):
        st = self.state()
        res = route.pick("review", fake=st, context="work")
        self.assertIsNone(res["picked"])
        self.assertEqual(res["exit_code"], route.EXIT_POLICY)
        self.assertEqual(len(res["passed_over"]), 3)
        self.assertTrue(all("refused by policy" in r for r in res["passed_over"]))
        code, out, err, cloud, _ = self.run_ask(["--role", "review", "Перевір"], st)
        self.assertEqual(code, route.EXIT_POLICY)
        self.assertIn("no model is allowed in the work context", err)
        cloud.assert_not_called()
        row = self.last_journal()
        self.assertEqual((row["status"], row["context"], row["role"]), ("refused", "work", "review"))

    def test_wait_does_not_wait_on_a_policy_refusal(self):
        with mock.patch.object(route, "wait_until_free", side_effect=AssertionError("waited on a refused role")):
            code, _, err, cloud, _ = self.run_ask(["--role", "review", "--wait", "Перевір"], self.state())
        self.assertEqual(code, route.EXIT_POLICY)
        cloud.assert_not_called()

    def test_work_orders_groups_and_drops_personal_without_work_ok(self):
        machine = {"accounts": {"agy": {"label": "work"}}}
        res = route.pick("first-read", fake=self.state(machine), context="work")
        self.assertEqual((res["picked"], res["provider"], res["account_label"]), ("gemini-3.8-flash-high", "agy", "work"))
        self.assertFalse(res["crossed"])
        machine = {"accounts": {"codex": {"label": "personal", "work_ok": True}}}
        res = route.pick("review", fake=self.state(machine), context="work")
        self.assertEqual((res["picked"], res["provider"]), ("gpt-6.1-sol", "codex"))
        self.assertTrue(res["crossed"])
        self.assertEqual(sum("refused by policy" in r for r in res["passed_over"]), 2)
        res = route.pick("draft", fake=self.state({}), context="work")  # local first, then personal agy is refused
        self.assertEqual(res["provider"], "ollama")

    def test_personal_refuses_work_accounts_and_keeps_role_order(self):
        machine = {"work_domains": [self.WORK_DOMAIN], "accounts": {"agy": {"email": "someone@" + self.WORK_DOMAIN}}}
        res = route.pick("review", fake=self.state(machine), context="personal")
        self.assertEqual(res["picked"], "gpt-6.1-sol")
        self.assertIn("gemini-4-argon-high: refused by policy", res["passed_over"][0])
        res = route.pick("first-read", fake=self.state({}), context="personal")
        self.assertEqual(res["picked"], "qwen3:14b")  # the role's own order: local first

    def test_writer_role_at_work_without_work_ok_goes_to_a_claude_subagent(self):
        res = route.pick("writer", fake=self.state(), context="work")
        self.assertTrue(res["fallback"])
        self.assertEqual((res["provider"], res["exit_code"]), ("claude-subscription", 6))

    def test_direct_model_refused_before_any_quota_is_read(self):
        st = self.state()
        with mock.patch.object(route, "cloud_state", side_effect=AssertionError("asked a refused account")):
            chk = route.check_name({"provider": "codex", "model": "gpt-6.1-sol"}, fake=st, context="work")
        self.assertEqual((chk["ok"], chk["status"], chk["exit_code"]), (False, "policy", route.EXIT_POLICY))
        code, out, err, cloud, _ = self.run_ask(["--codex", "gpt-6.1-sol", "Питання"], st)
        self.assertEqual(code, route.EXIT_POLICY)
        self.assertIn("refused by policy", err)
        self.assertEqual(out, "")
        cloud.assert_not_called()
        self.assertEqual(self.last_journal()["status"], "refused")

    def test_unset_context_is_work_and_says_so(self):
        env = {k: v for k, v in os.environ.items() if k != "ASK_CONTEXT"}
        st = self.state()
        with mock.patch.dict(os.environ, env, clear=True):
            code, _, err, cloud, _ = self.run_ask(["--codex", "gpt-6.1-sol", "Питання"], st, context=None)
        self.assertEqual(code, route.EXIT_POLICY)
        self.assertIn("ASK_CONTEXT is not set: treated as work", err)
        cloud.assert_not_called()

    def test_refusal_without_a_context_says_set_it_not_work_ok(self):
        # An agent follows the hint it is given: with no context set, opening work_ok is the wrong fix.
        env = {k: v for k, v in os.environ.items() if k != "ASK_CONTEXT"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, _, err, _, _ = self.run_ask(["--role", "review", "Перевір"], self.state(), context=None)
        self.assertEqual(code, route.EXIT_POLICY)
        self.assertIn("no context is set", err)
        self.assertIn("context_paths", err)
        self.assertIn('{"~": "personal"}', err)
        self.assertIn("Do not set work_ok", err)
        self.assertNotIn("allows it", err)
        for ctx in ("persnal", {"context": "persnal"}):
            with self.subTest(ctx=ctx):
                reason = route.policy_for("codex", context=ctx, machine={})["reason"]
                self.assertIn("Do not set work_ok", reason)
        explicit = route.policy_for("codex", context="work", machine={})["reason"]  # work was set: work_ok is the choice
        self.assertIn("work_ok: true for it", explicit)
        self.assertNotIn("Do not set work_ok", explicit)

    def test_context_flag_over_env_allows_a_personal_call(self):
        code, out, err, cloud, _ = self.run_ask(["--context", "personal", "--codex", "gpt-6.1-sol", "Питання"], self.state())
        self.assertEqual(code, 0)
        self.assertIn("Answer", out)
        cloud.assert_called_once()
        row = self.last_journal()
        self.assertEqual((row["context"], row["crossed"], row["account"]), ("personal", False, "codex:~/.codex"))

    def test_crossing_warns_on_stderr_and_is_journalled(self):
        st = self.state({"accounts": {"codex": {"label": "personal", "work_ok": True}}})
        for argv in (["--codex", "gpt-6.1-sol", "Питання"], ["--role", "review", "Перевір"]):
            with self.subTest(argv=argv):
                code, out, err, cloud, _ = self.run_ask(argv, st)
                self.assertEqual(code, 0)
                self.assertIn("crossing: work context → personal account codex:~/.codex", err)
                self.assertNotIn("crossing", out)
                row = self.last_journal()
                self.assertEqual((row["status"], row["context"], row["crossed"]), ("ok", "work", True))

    def test_work_call_to_a_work_account_names_it_and_does_not_cross(self):
        st = self.state({"accounts": {"codex": {"label": "work"}}})
        code, _, err, _, _ = self.run_ask(["--codex", "gpt-6.1-sol", "Питання"], st)
        self.assertEqual(code, 0)
        self.assertIn("[work context: account codex:~/.codex (work)]", err)
        self.assertNotIn("crossing", err)
        self.assertFalse(self.last_journal()["crossed"])

    def test_local_model_in_work_is_allowed(self):
        code, out, err, cloud, local = self.run_ask(["--ollama", "qwen3:14b", "Питання"], self.state())
        self.assertEqual(code, 0)
        local.assert_called_once()
        row = self.last_journal()
        self.assertEqual((row["provider"], row["context"], row["crossed"]), ("ollama", "work", False))

    def test_status_shows_context_and_what_is_allowed(self):
        st = self.state({"accounts": {"codex": {"label": "personal", "work_ok": True}}})
        with mock.patch.object(ask, "print_status", wraps=ask.print_status), \
             mock.patch.object(route, "cloud_state", return_value={"pools": [], "ok": True}), \
             mock.patch.object(route, "local_state", return_value={
                 "free": True, "reason_str": "", "used_gb": 0.0, "total_gb": 0.0, "free_gb": 0.0, "utilization": 0,
                 "quns": "-", "loaded": [], "ollama_up": False}), \
             mock.patch.object(route, "read_journal_24h", return_value={}):
            code, out, _, _, _ = self.run_ask(["--status"], st)
        self.assertEqual(code, 0)
        self.assertIn("Context: work", out)
        lines = {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("  ") and ln.split()[0] in (
            "agy", "codex:~/.codex", "ollama")}
        self.assertIn("refused", lines["agy"])
        self.assertIn("crossing", lines["codex:~/.codex"])
        self.assertIn("allowed", lines["ollama"])
        self.assertIn("order: work → local → personal with work_ok: true", out)

    def test_draw_is_refused_at_work_on_a_personal_agy(self):
        with mock.patch.dict(os.environ, self.env(self.state(), "work")), \
             mock.patch.object(sys, "argv", ["ask.py", "draw", "кіт", "--out", "x.png"]), \
             mock.patch.object(sys, "stderr", io.StringIO()) as err, \
             mock.patch.object(ask, "draw") as draw:
            self.assertEqual(ask.main(), route.EXIT_POLICY)
        draw.assert_not_called()
        self.assertIn("refused by policy", err.getvalue())

    def test_writer_named_model_refused_by_policy_exits_7_not_6(self):
        with mock.patch.dict(os.environ, self.env(self.state(), "work")), \
             mock.patch.object(sys, "argv", ["writer.py", "--cwd", HERE, "--brief", "B.md", "--codex", "--model", "gpt-6.1-sol"]), \
             mock.patch.object(sys, "stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit) as stopped:
                writer.main()
        self.assertEqual(stopped.exception.code, route.EXIT_POLICY)
        self.assertIn("refused by policy", err.getvalue())
        self.assertNotIn("Claude subagent", err.getvalue())

    def test_paths_from_old_models_json_and_machine_json_over_it(self):
        with open(route.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"agy": "/old/agy", "codex": "/old/codex"}, f)
        try:
            self.assertEqual(route.load_config()[0], "/old/agy")
            self.assertEqual(route.codex_path(), "/old/codex")
            with open(route.MACHINE_PATH, "w", encoding="utf-8") as f:
                json.dump({"paths": {"codex": "/new/codex"}}, f)
            self.assertEqual(route.codex_path(), "/new/codex")
            self.assertEqual(route.load_config()[0], "/old/agy")
        finally:
            for path in (route.CONFIG_PATH, route.MACHINE_PATH):
                if os.path.exists(path):
                    os.remove(path)


def fake_jwt(claims):
    import base64
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


class ProviderTests(unittest.TestCase):
    """The adapters and the inventory."""

    def state(self, machine=None):
        st = CodexTests().state()
        st["machine"] = machine if machine is not None else {}
        return st

    def test_agy_ids_split_into_family_and_effort(self):
        self.assertEqual(providers.split_agy_id("gemini-4-argon-high"), ("gemini-4-argon", "high"))
        self.assertEqual(providers.split_agy_id("gpt-oss-120b-medium"), ("gpt-oss-120b", "medium"))
        self.assertEqual(providers.split_agy_id("image-generator"), ("image-generator", None))
        st = self.state()
        st["agy_models"] = ["gemini-4-argon-high", "gemini-4-argon-low", "gemini-4-argon-medium", "gpt-oss-120b-medium"]
        agy = providers.get("agy", fake=st)
        ok, rows = agy.models()
        self.assertTrue(ok)
        argon = next(r for r in rows if r["family"] == "gemini-4-argon")
        self.assertEqual(argon["efforts"], ["low", "medium", "high"])
        self.assertEqual(argon["ids"]["high"], "gemini-4-argon-high")
        self.assertEqual(agy.efforts("gemini-4-argon-low"), ["low", "medium", "high"])

    def test_codex_efforts_from_its_live_list(self):
        codex = providers.get("codex", fake=self.state())
        self.assertEqual(codex.efforts("gpt-6.1-sol"), ["high"])
        self.assertEqual(codex.efforts("gpt-5.6-luna"), [])
        self.assertEqual(providers.get("claude-subscription", fake=self.state()).efforts("claude-opus-5-5"),
                         providers.CLAUDE_EFFORTS)

    def test_local_only_role_without_a_local_provider_fails_fast(self):
        st = self.state({"paths": {"ollama": None}})
        res = route.pick("index", fake=st)
        self.assertEqual((res["exit_code"], res["wait"], res["picked"]), (route.EXIT_UNSUPPORTED, False, None))
        self.assertIn("no local provider", res["message"])
        never = mock.Mock(side_effect=AssertionError("waited"))
        w = route.wait_until_free(role="index", max_wait_s=7200, fake=st, stream=io.StringIO(), sleep_fn=never)
        self.assertEqual(w["exit_code"], route.EXIT_UNSUPPORTED)
        never.assert_not_called()
        env = {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1", "ASK_CONTEXT": "personal"}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["ask.py", "--role", "index", "x"]), \
             mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()) as err, \
             mock.patch.object(route.time, "sleep", side_effect=AssertionError("waited")):
            code = ask.main()
        self.assertEqual(code, route.EXIT_UNSUPPORTED)
        self.assertIn("cannot run here", err.getvalue())

    def test_null_path_means_not_on_this_machine(self):
        st = self.state({"paths": {"ollama": None, "codex": None}})
        self.assertFalse(providers.get("ollama", fake=st).detect()["present"])
        self.assertTrue(providers.get("agy", fake=st).detect()["present"])
        self.assertFalse(any(r["provider"] in ("ollama", "codex") for r in route.models(fake=st)))
        chk = route.check_name({"provider": "codex", "model": "gpt-6.1-sol"}, fake=st)
        self.assertEqual(chk["status"], "not_offered")
        self.assertIn("not on this machine", chk["reason"])
        inv = {p["provider"]: p for p in providers.inventory(fake=st, context="personal")}
        self.assertEqual(inv["codex"]["accounts"], [])
        self.assertTrue(inv["agy"]["accounts"])

    def test_detect_configured_then_path_then_usual_places(self):
        with tempfile.TemporaryDirectory() as tmp:
            usual = os.path.join(tmp, "agy")
            with open(usual, "w") as f:
                f.write("")
            agy = providers.get("agy")
            with mock.patch.object(providers.AgyAdapter, "candidates", return_value=[usual]), \
                 mock.patch.object(providers.shutil, "which", return_value=None):
                self.assertEqual(agy.detect(), {"present": True, "path": usual, "source": "usual place", "note": None})
            with mock.patch.object(providers.shutil, "which", return_value="/bin/agy"):
                self.assertEqual(agy.detect()["source"], "PATH")
            with open(route.MACHINE_PATH, "w", encoding="utf-8") as f:
                json.dump({"paths": {"agy": "/opt/agy", "codex": None}}, f)
            try:
                self.assertEqual(agy.detect()["path"], "/opt/agy")
                self.assertEqual(route.load_config()[0], "/opt/agy")
                self.assertFalse(providers.get("codex").detect()["present"])
                self.assertEqual(route.codex_path(), "codex")
            finally:
                os.remove(route.MACHINE_PATH)

    def test_codex_accounts_by_codex_home_with_email_for_the_label(self):
        home = route.ACCOUNTS_HOME
        for folder, email in ((".codex", "me@example.org"), (".codex-work", "me@corp.example"), (".codex-empty", None)):
            os.makedirs(os.path.join(home, folder), exist_ok=True)
            if email:
                with open(os.path.join(home, folder, "auth.json"), "w", encoding="utf-8") as f:
                    json.dump({"tokens": {"id_token": fake_jwt({"email": email})}}, f)
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "ASK_ROUTE_FAKE")}
            with mock.patch.dict(os.environ, env, clear=True):
                accs = providers.get("codex").accounts()
                self.assertEqual([(a["id"], a["email"], a["active"]) for a in accs],
                                 [("codex:~/.codex", "me@example.org", True), ("codex:~/.codex-work", "me@corp.example", False)])
                machine = {"work_domains": ["corp.example"]}
                self.assertEqual(route.policy_for("codex", context="work", machine=machine)["label"], "personal")
                pol = route.policy_for("codex", context="work", machine=machine, account="codex:~/.codex-work")
                self.assertEqual((pol["label"], pol["allowed"]), ("work", True))
                self.assertEqual(providers.get("codex")._home_env("codex:~/.codex-work"), os.path.join(home, ".codex-work"))
            with mock.patch.dict(os.environ, dict(env, CODEX_HOME="~/.codex-work"), clear=True):
                pol = route.policy_for("codex", context="work", machine={"work_domains": ["corp.example"]})
                self.assertEqual((pol["account"], pol["label"], pol["source"]), ("codex:~/.codex-work", "work",
                                                                                 "email domain corp.example"))
        finally:
            import shutil
            shutil.rmtree(home)

    def test_other_codex_home_gets_its_own_cache(self):
        self.assertEqual(route._codex_cache("/c/codex_models.json"), "/c/codex_models.json")
        self.assertEqual(route._codex_cache("/c/codex_models.json", "/u/.codex-work"), "/c/codex_models@.codex-work.json")

    def test_subscription_models_are_not_in_agys_pool(self):
        st = self.state()
        rows = {(r["provider"], r["model"]): r for r in route.models(fake=st)}
        self.assertEqual(rows[("claude-subscription", "claude-opus-5-5")]["pool"], "Claude subscription")
        self.assertEqual(route.pool_provider("Claude and GPT models"), "agy")
        self.assertEqual(route.pool_provider("ChatGPT"), "codex")

    def test_list_shows_provider_account_label_and_efforts(self):
        st = self.state({"paths": {"ollama": None}, "accounts": {"agy": {"label": "work"}}})
        st["agy_models"] = ["gemini-4-argon-high", "gemini-4-argon-low"]
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1"}), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            self.assertEqual(ask.print_list("unused", "unused", ctx=route.resolve_context("personal")), 0)
        text = out.getvalue()
        self.assertIn("Context: personal", text)
        self.assertIn("agy [work, machine.json]  refused", text)
        self.assertIn("gemini-4-argon efforts low, high", text)
        self.assertIn("codex:~/.codex [personal, default]  allowed", text)
        self.assertIn("efforts high", text)
        self.assertIn("ollama  Ollama (local): paths.ollama is null", text)
        self.assertIn("ollama is not on this machine: passed over", text)
        self.assertNotIn("Claude and GPT models", text.split("claude-subscription", 1)[1].split("\n\n")[0])

    def test_posix_processes_from_ps(self):
        done = mock.Mock(stdout="  1 /sbin/launchd\n 512 /Applications/Foo.app/Contents/MacOS/Foo\nbad line\n")
        with mock.patch.object(route.subprocess, "run", return_value=done) as run:
            procs = route._list_processes_posix()
        self.assertEqual(run.call_args.args[0], ["ps", "-axo", "pid=,comm="])
        self.assertEqual(procs[1], {"pid": 512, "name": "Foo", "path": "/Applications/Foo.app/Contents/MacOS/Foo"})

    def test_gpu_kind_and_budget_from_machine_json(self):
        self.assertEqual(route.machine_gpu({"hardware": {"gpu": {"kind": "apple", "model_budget_gb": 12}}}),
                         {"kind": "apple", "model_budget_gb": 12})
        for platform, tools, kind in (("darwin", (), "apple"), ("win32", ("nvidia-smi",), "nvidia"),
                                      ("linux", ("rocm-smi",), "amd"), ("linux", (), "unified"), ("win32", (), "unified")):
            with self.subTest(platform=platform, tools=tools), mock.patch.object(route, "host_platform", return_value=platform), \
                 mock.patch.object(route.shutil, "which", side_effect=lambda name: name if name in tools else None), \
                 mock.patch.object(route, "_ram_bytes", return_value=16 * 1024 ** 3):
                gpu = route.machine_gpu({})
                self.assertEqual(gpu["kind"], kind)
                self.assertEqual(gpu.get("model_budget_gb"), 8.0 if kind in ("apple", "unified") else None)

    @unittest.skipIf(sys.platform.startswith("win"), "the process group is the POSIX way")
    def test_writer_stops_its_whole_process_group(self):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        with mock.patch.object(writer.os, "getpgid", return_value=4242), \
             mock.patch.object(writer.os, "killpg") as killpg:
            writer.stop_tree(proc)
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 4242)


class ExpiryTests(unittest.TestCase):
    """Quota windows: among targets equally fit for a role (one "any" group), the one whose leftover burns first goes
    first; floors, pace and the policy still decide what may go at all."""

    DAY = 86400

    def pool(self, provider, label, covers, weekly_rem, weekly_reset_days, h5=None):
        wins = [{"window": "weekly", "label": "weekly", "length_s": 7 * self.DAY, "remaining_fraction": weekly_rem,
                 "reserved_fraction": 0.0, "resets_in_s": weekly_reset_days * self.DAY, "resets_in": f"{weekly_reset_days}d"}]
        if h5 is not None:
            rem, hours = h5
            wins.append({"window": "5h", "label": "5h", "length_s": 5 * 3600, "remaining_fraction": rem,
                         "reserved_fraction": 0.0, "resets_in_s": hours * 3600, "resets_in": f"{hours}h"})
        return {"provider": provider, "label": label, "covers": covers, "stale": False, "windows": wins}

    def cs(self, gemini=(0.5, 6), chatgpt=(0.6, 1)):
        return {"ok": True, "pools": [
            self.pool("agy", "Gemini Models", ["gemini-*"], *gemini, h5=(0.9, 1)),
            self.pool("codex", "ChatGPT (prolite)", ["gpt-6*"], *chatgpt),
        ]}

    def ok(self, prov, mid, **kw):
        return dict({"ok": True, "model": mid, "label": mid, "provider": prov, "pool": prov, "reason": "ok",
                     "pace_over": False, "context": "personal", "account": prov, "account_label": "personal",
                     "crossed": False}, **kw)

    def test_role_candidates_flatten_groups(self):
        a, b, c = ({"provider": "agy", "model": "x"}, {"provider": "codex", "model": "y"}, {"provider": "agy", "model": "z"})
        self.assertEqual(route.role_candidates([{"any": [a, b]}, c]), ([a, b, c], [0, 0, 1]))
        rules = {"roles": {"review": {"models": [{"any": [a, b]}, c]}}}
        self.assertEqual(route.role_usage(rules)[("codex", "y")], ["review"])

    def test_weekly_window_is_what_burns(self):
        x = route.expiry_of("agy", "gemini-4-argon-high", self.cs())
        self.assertEqual((x["pool"], x["window"]), ("Gemini Models", "weekly"))
        self.assertAlmostEqual(x["urgency"], 0.5 / (6 * 24))
        self.assertIsNone(route.expiry_of("ollama", "qwen3:14b", self.cs()))
        w = {"remaining_fraction": 0.5, "reserved_fraction": 0.2, "resets_in_s": 3 * 3600}
        self.assertAlmostEqual(route.window_urgency(w), 0.1)
        self.assertIsNone(route.window_urgency({"remaining_fraction": 0.5}))

    def claude_pools(self, all_used=86, fable_used=15):
        """The subscription's pools as get_usage's windows give them: Fable is nested in all models."""
        now = 1793734200.0  # 2026-11-03T19:30:00Z
        cu = {"read_at": "2026-11-03T19:00:00Z", "plan": "Ultra", "windows": [
            {"label": "Weekly · all models", "percentUsed": all_used, "resetsAt": "2026-11-05T19:00:00Z"},
            {"label": "Weekly · Fable", "percentUsed": fable_used, "resetsAt": "2026-11-05T19:00:00Z"},
            {"label": "Weekly · Opus", "percentUsed": 20, "resetsAt": "2026-11-05T19:00:00Z"}]}
        pools, _ = route._pools_from_claude_usage(cu, now)
        return pools

    def test_fable_is_nested_in_all_models(self):
        pools = self.claude_pools()
        by = {p["id"]: p for p in pools}
        fable, allm, opus = by["claude-sub-fable"], by["claude-sub-all-models"], by["claude-sub-opus"]
        self.assertAlmostEqual(fable["windows"][0]["remaining_fraction"], 0.14)  # min(85%, 14%)
        self.assertAlmostEqual(fable["windows"][0]["own_remaining_fraction"], 0.85)
        self.assertEqual(fable["nested_in"], allm["id"])
        self.assertNotIn("nested_in", allm)
        self.assertNotIn("nested_in", opus)  # only what the user confirmed is nested
        self.assertAlmostEqual(opus["windows"][0]["remaining_fraction"], 0.80)
        with self.subTest("Fable left smaller than all models: its own"):
            fable = {p["id"]: p for p in self.claude_pools(all_used=40, fable_used=95)}["claude-sub-fable"]
            self.assertAlmostEqual(fable["windows"][0]["remaining_fraction"], 0.05)
        with self.subTest("no all-models window in the reading: nothing nested"):
            cu = {"read_at": "2026-11-03T19:00:00Z", "windows": [
                {"label": "Weekly · Fable", "percentUsed": 15, "resetsAt": "2026-11-05T19:00:00Z"}]}
            pools, _ = route._pools_from_claude_usage(cu, 1793734200.0)
            self.assertAlmostEqual(pools[0]["windows"][0]["remaining_fraction"], 0.85)
            self.assertNotIn("nested_in", pools[0])

    def test_fable_never_burns_first_above_all_models(self):
        pools = self.claude_pools()
        burn = route.burning_pools(pools)
        self.assertNotIn("claude-sub-fable", [p["id"] for _, p, _ in burn])
        self.assertEqual(burn[0][1]["id"], "claude-sub-opus")  # 80% left; Fable is not listed at its own 85%
        x = route.expiry_of("claude-subscription", "claude-fable-5-1", {"pools": pools})
        self.assertAlmostEqual(x["remaining"], 0.14)

    def test_router_does_not_pick_fable_to_use_up_its_window(self):
        pools = self.claude_pools()  # all models 14% left, Fable's own window 85%
        pools.append(self.pool("agy", "Gemini Models", ["gemini-*"], 0.5, 6))
        cs = {"ok": True, "pools": pools}
        ev = [self.ok("claude-subscription", "claude-fable-5-1"), self.ok("agy", "gemini-4-argon-high")]
        out = route._order_by_expiry([dict(e) for e in ev], [(0, 0), (0, 0)], cs)
        # Fable at its own 85% in 47.5 h would burn first (0.0179/h); at the 14% all models allows it is 0.0029/h,
        # below Gemini's 0.5 in 6 days (0.0035/h)
        self.assertEqual((out[0]["model"], out[0]["why"]), ("gemini-4-argon-high", "expires_first"))
        self.assertEqual(out[1]["expiry"]["remaining"], pools[1]["windows"][0]["remaining_fraction"])
        with self.subTest("all models under the floor: Fable's check fails whatever its own window holds"):
            pools = self.claude_pools(all_used=95)
            chk = route.check_name({"provider": "claude-subscription", "model": "claude-fable-5-1"}, cs={"pools": pools})
            self.assertFalse(chk["ok"])

    def test_order_within_a_group_only(self):
        cs = self.cs()
        ev = [self.ok("agy", "gemini-4-argon-high"), self.ok("codex", "gpt-6-astra"), self.ok("agy", "gemini-3.8-flash-high")]
        out = route._order_by_expiry([dict(e) for e in ev], [(0, 0), (0, 0), (0, 1)], cs)
        self.assertEqual([e["model"] for e in out], ["gpt-6-astra", "gemini-4-argon-high", "gemini-3.8-flash-high"])
        self.assertEqual(out[0]["why"], "expires_first")
        self.assertNotIn("why", out[1])
        with self.subTest("no group: the role's order holds"):
            out = route._order_by_expiry([dict(e) for e in ev], [(0, 0), (0, 1), (0, 2)], cs)
            self.assertEqual([e["model"] for e in out], [e["model"] for e in ev])
        with self.subTest("the policy's groups are not crossed"):
            out = route._order_by_expiry([dict(e) for e in ev], [(0, 0), (1, 0), (1, 1)], cs)
            self.assertEqual(out[0]["model"], "gemini-4-argon-high")
        with self.subTest("a failed check stays behind and is not moved forward"):
            bad = dict(ev[1], ok=False, reason="gpt-6-astra: quota below floor")
            out = route._order_by_expiry([dict(ev[0]), bad], [(0, 0), (0, 0)], cs)
            self.assertEqual([e["model"] for e in out], ["gemini-4-argon-high", "gpt-6-astra"])
            self.assertNotIn("why", out[0])
        with self.subTest("already first: no why"):
            cs2 = self.cs(gemini=(0.9, 1), chatgpt=(0.3, 6))
            out = route._order_by_expiry([dict(e) for e in ev[:2]], [(0, 0), (0, 0)], cs2)
            self.assertEqual(out[0]["model"], "gemini-4-argon-high")
            self.assertNotIn("why", out[0])

    def pick(self, cs, checks):
        rules = {"roles": {"review": {"models": [
            {"any": [{"provider": "agy", "model": "gemini-4-argon-high"}, {"provider": "codex", "model": "gpt-6-astra"}]},
            {"provider": "agy", "model": "gemini-3.8-flash-high"}]}}}
        allow = {"allowed": True, "rank": 0, "reason": "", "account": "x", "label": "personal", "crossed": False,
                 "context": "personal"}
        with mock.patch.object(route, "cloud_state", return_value=cs), \
             mock.patch.object(route, "policy_for", return_value=allow), \
             mock.patch.object(route, "check_name", side_effect=lambda spec, **kw: dict(checks[spec["model"]])):
            return route.pick("review", rules=rules, context="personal", machine={})

    def test_pick_takes_the_quota_that_burns_first(self):
        checks = {m: self.ok(p, m) for p, m in (("agy", "gemini-4-argon-high"), ("codex", "gpt-6-astra"),
                                                ("agy", "gemini-3.8-flash-high"))}
        res = self.pick(self.cs(), checks)
        self.assertEqual((res["model"], res["why"], res["exit_code"]), ("gpt-6-astra", "expires_first", 0))
        self.assertEqual(res["expiry"]["pool"], "ChatGPT (prolite)")
        with self.subTest("below the floor: the other one of the group"):
            checks["gpt-6-astra"] = dict(checks["gpt-6-astra"], ok=False, reason="gpt-6-astra: below floor")
            res = self.pick(self.cs(), checks)
            self.assertEqual((res["model"], res["why"]), ("gemini-4-argon-high", None))

    # --- the review of gpt-6-astra and gemini-4-argon-high ---

    def test_first_failed_does_not_make_a_false_why(self):
        cs = self.cs()
        ev = [dict(self.ok("agy", "gemini-4-argon-high"), ok=False, reason="below floor"), self.ok("codex", "gpt-6-astra")]
        out = route._order_by_expiry(ev, [(0, 0), (0, 0)], cs)
        self.assertEqual([e["model"] for e in out], ["gemini-4-argon-high", "gpt-6-astra"])
        self.assertNotIn("why", out[1])

    def test_pace_over_is_not_ordered_and_keeps_its_place(self):
        cs = self.cs(gemini=(0.2, 6), chatgpt=(0.9, 1))  # ChatGPT burns first, but it runs over the pace
        ev = [self.ok("agy", "gemini-4-argon-high"), self.ok("codex", "gpt-6-astra", pace_over=True, pace_reason="fast"),
              self.ok("agy", "gemini-3.8-flash-high")]
        out = route._order_by_expiry([dict(e) for e in ev], [(0, 0)] * 3, cs)
        self.assertEqual(out[1]["model"], "gpt-6-astra")  # not moved up
        self.assertTrue(all("why" not in e for e in out))

    def test_no_data_keeps_its_place(self):
        cs = self.cs()
        cs["pools"][1]["stale"] = True  # ChatGPT pool stale: no expiry for astra
        ev = [self.ok("codex", "gpt-6-astra"), self.ok("agy", "gemini-4-argon-high"), self.ok("ollama", "qwen3:14b")]
        out = route._order_by_expiry([dict(e) for e in ev], [(0, 0)] * 3, cs)
        self.assertEqual([e["model"] for e in out], ["gpt-6-astra", "gemini-4-argon-high", "qwen3:14b"])
        self.assertIsNone(out[0]["expiry"])

    def test_zero_urgency_is_not_no_data(self):
        cs = self.cs(gemini=(0.0, 1), chatgpt=(0.5, 6))  # Gemini fully used: urgency 0.0, still a known value
        ev = [self.ok("agy", "gemini-4-argon-high"), self.ok("codex", "gpt-6-astra")]
        out = route._order_by_expiry([dict(e) for e in ev], [(0, 0)] * 2, cs)
        self.assertEqual(out[0]["model"], "gpt-6-astra")
        self.assertEqual(out[1]["expiry"]["urgency"], 0.0)
        self.assertEqual(out[0]["why"], "expires_first")

    def test_reset_due_or_past_is_stale_not_urgent(self):
        for rs in (0, -5):
            with self.subTest(resets_in_s=rs):
                self.assertIsNone(route.window_urgency({"remaining_fraction": 0.9, "resets_in_s": rs}))
        self.assertAlmostEqual(route.window_urgency({"remaining_fraction": None, "reserved_fraction": None,
                                                     "resets_in_s": 3600}), 1.0)

    def test_reservations_change_the_order(self):
        cs = self.cs(gemini=(0.5, 2), chatgpt=(0.5, 2))
        cs["pools"][0]["windows"][0]["reserved_fraction"] = 0.4  # Gemini mostly promised to a running call
        ev = [self.ok("agy", "gemini-4-argon-high"), self.ok("codex", "gpt-6-astra")]
        out = route._order_by_expiry([dict(e) for e in ev], [(0, 0)] * 2, cs)
        self.assertEqual(out[0]["model"], "gpt-6-astra")

    def test_two_weekly_windows_the_more_urgent_counts(self):
        cs = {"pools": [
            self.pool("agy", "All", ["claude-*"], 0.9, 6),
            self.pool("agy", "Sonnet", ["claude-sonnet*"], 0.9, 1),
        ]}
        self.assertEqual(route.expiry_of("agy", "claude-sonnet-5-5-high", cs)["pool"], "Sonnet")
        cs["pools"].reverse()
        self.assertEqual(route.expiry_of("agy", "claude-sonnet-5-5-high", cs)["pool"], "Sonnet")
        w, _ = route.burn_window([{"window": "5h", "length_s": 18000, "remaining_fraction": 1, "resets_in_s": 600},
                                  {"window": "weekly", "length_s": 604800, "remaining_fraction": 0.5, "resets_in_s": 86400}])
        self.assertEqual(w["window"], "weekly")

    def test_group_fields_are_member_defaults(self):
        models, groups = route.role_candidates([{"any": [{"family": "a*"}, {"family": "b*", "effort": "low"}],
                                                  "effort": ">=high"}])
        self.assertEqual([m.get("effort") for m in models], [">=high", "low"])
        self.assertEqual(groups, [0, 0])

    def test_filter_role_keeps_groups(self):
        raw = [{"any": [{"provider": "agy", "model": "a"}, {"provider": "codex", "model": "b"},
                        {"provider": "agy", "model": "c"}]},
               {"any": [{"provider": "codex", "model": "d"}]},
               {"provider": "claude-subscription", "model": "e"}]
        out = route.filter_role(raw, lambda m: m.get("provider") in ("agy", "claude-subscription"))
        self.assertEqual(out, [{"any": [{"provider": "agy", "model": "a"}, {"provider": "agy", "model": "c"}]},
                               {"provider": "claude-subscription", "model": "e"}])
        self.assertEqual(route.role_candidates(out)[1], [0, 0, 1])

    def test_status_burn_line_counts_reservations(self):
        cs = self.cs(gemini=(0.5, 6), chatgpt=(0.6, 1))
        cs["pools"][1]["windows"][0]["reserved_fraction"] = 0.5
        with mock.patch.object(route, "cloud_state", return_value=cs), \
             mock.patch.object(route, "local_state", return_value={"free": True, "reasons": [], "reason_str": ""}), \
             mock.patch.object(sys, "stdout", io.StringIO()) as out:
            try:
                ask.print_status(None, None)
            except Exception:
                pass  # later parts of the status may need more of the fake; the burn line is printed before them
        line = next((l for l in out.getvalue().splitlines() if "Burns first" in l), "")
        self.assertIn("ChatGPT (prolite) weekly 10%", line)

    def test_journal_keeps_why(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "usage.jsonl")
            row = route.journal("ask.py", "review", "m", "codex", "m", journal_path=p, why="expires_first")
            self.assertEqual(row["why"], "expires_first")
            self.assertNotIn("why", route.journal("ask.py", "review", "m", "codex", "m", journal_path=p))
        self.assertEqual(ask._policy_kw({"context": "personal", "why": "expires_first"})["why"], "expires_first")
        self.assertNotIn("why", ask._policy_kw({"context": "personal"}))


class ExperienceRoutingTests(unittest.TestCase):
    def setUp(self):
        import experience
        self.experience = experience
        self.rules = copy.deepcopy(route.load_roles())
        self.first, self.second = "gemini-4-argon-high", "gemini-3.8-flash-high"
        self.rules["roles"]["review"]["models"] = [
            {"provider": "agy", "model": mid} for mid in (self.first, self.second)]
        self.state = free_state()
        now = route.time.time()
        self.state["journal"] = [
            row for i in range(2) for row in (
                {"ts": now - 100, "conversation": str(i), "provider": "agy", "model": self.first,
                 "role": "review", "status": "ok"},
                {"ts": now - 50, "conversation": str(i), "provider": "agy", "model": self.first,
                 "type": "verdict", "verdict": "rejected"})]

    def pick(self):
        return route.pick("review", fake=self.state, rules=self.rules, tokens=10000,
                          context="personal", machine={})

    def test_demoted_first_picks_second_with_reason(self):
        result = self.pick()
        self.assertEqual(result["picked"], self.second)
        self.assertEqual(result["why"], "experience")
        self.assertTrue(any(f"{self.first} down 1 in review" in note and "2 rejected" in note
                            for note in result["experience"]))
        self.assertEqual(self.rules["roles"]["review"]["models"][0]["model"], self.first)

    def test_demoted_model_picked_anyway_is_not_marked_experience(self):
        # The second cannot meet a need, so the demoted first is picked despite experience, not because of it.
        self.rules["traits"] = {"cannot": {self.second: {"*": ["python"]}}}
        result = route.pick("review", fake=self.state, rules=self.rules, tokens=10000,
                            context="personal", machine={}, needs=["python"])
        self.assertEqual(result["picked"], self.first)
        self.assertIsNone(result["why"])

    def test_disabled_and_broken_experience_keep_owner_order(self):
        self.rules["experience"]["enabled"] = False
        result = self.pick()
        self.assertEqual(result["picked"], self.first)
        self.assertIsNone(result["why"])
        self.assertEqual(result["experience"], [])
        self.rules["experience"]["enabled"] = True
        with mock.patch.object(self.experience, "reorder", side_effect=ValueError("broken")), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            result = self.pick()
        self.assertEqual(result["picked"], self.first)
        self.assertEqual(result["experience"], [])
        self.assertEqual(err.getvalue(), "")

    def test_experience_does_not_bypass_never_or_policy(self):
        self.rules["never"] = [self.second]
        self.assertEqual(self.pick()["picked"], self.first)
        result = route.pick("review", fake=self.state, rules=self.rules, tokens=10000,
                            context="work", machine={})
        self.assertEqual(result["exit_code"], route.EXIT_POLICY)
        self.assertIsNone(result["picked"])

    def test_expiry_reason_takes_precedence(self):
        original = route._order_by_expiry

        def expires(evaluated, keys, cs):
            ordered = original(evaluated, keys, cs)
            ordered[0]["why"] = "expires_first"
            return ordered

        with mock.patch.object(route, "_order_by_expiry", side_effect=expires):
            result = self.pick()
        self.assertEqual(result["why"], "expires_first")
        self.assertTrue(result["experience"])

    def test_experience_note_printed_and_list_quality_by_role(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            ask._say_account({"experience": ["a down 1 in review: q 0.25"]}, False)
        self.assertIn("[experience: a down 1 in review: q 0.25]", err.getvalue())
        with mock.patch.object(route, "load_roles", return_value=self.rules), \
             mock.patch.object(route, "resolve_fake", return_value=self.state), \
             mock.patch.object(ask.adapters, "inventory", return_value=[]), \
             mock.patch.object(route, "models", return_value=[]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            ask.print_list(None, None, notes=True, ctx={"context": "personal", "source": "explicit"})
        self.assertIn("  review:", out.getvalue())
        self.assertIn("2 rejected in role, 0 elsewhere", out.getvalue())


class TraitsTests(unittest.TestCase):
    """Work requirements must survive routing and CLI boundaries."""

    def setUp(self):
        self.rules = route.load_roles()
        self.rules["experience"]["enabled"] = False
        self.rules["roles"]["writer"] = {"models": [
            {"provider": "codex", "model": "gpt-6.1-sol"},
            {"provider": "agy", "model": "gemini-4-argon-high"},
            {"provider": "claude-subscription", "model": "claude-opus-5-5"},
        ]}
        self.state = CodexTests().state()

    def test_provider_model_platform_and_subscription_traits(self):
        self.rules["traits"]["cannot"]["gpt-6.1-sol"] = {"*": ["big-files"], "linux": ["engine"]}
        self.assertEqual(route.cannot("codex", "other", self.rules, "win32"), {"python", "engine"})
        self.assertEqual(route.cannot("codex", "other", self.rules, "linux"), set())
        self.assertEqual(route.cannot("codex", "gpt-6.1-sol", self.rules, "win32"), set(route.KNOWN_NEEDS))
        self.assertEqual(route.cannot("codex", "gpt-6.1-sol", self.rules, "linux"), {"engine", "big-files"})
        for platform in ("win32", "linux"):
            self.assertEqual(route.cannot("ollama", "qwen3:14b", self.rules, platform), set(route.KNOWN_NEEDS))
        self.assertEqual(route.lacking("codex", "gpt-6.1-sol", ["python", "engine"], self.rules, "win32"),
                         ["engine", "python"])
        self.assertEqual(route.lacking("agy", "gemini-4-argon-high", route.KNOWN_NEEDS, self.rules), [])
        self.rules["traits"]["cannot"]["claude-subscription"] = {"*": list(route.KNOWN_NEEDS)}
        self.rules["traits"]["cannot"]["claude-opus-5-5"] = {"win32": ["engine"]}
        self.assertEqual(route.cannot("claude-subscription", "claude-opus-5-5", self.rules, "win32"), set())
        self.assertEqual(route.lacking("claude-subscription", "claude-opus-5-5", route.KNOWN_NEEDS,
                                       self.rules, "win32"), [])
        self.assertEqual(route.cannot("codex", "gpt-6.1-sol", {}, "win32"), set())

    def test_role_spec_defaults_and_combined_needs(self):
        self.assertEqual(route._role_spec("analysis", self.rules)["needs"], ["big-files"])
        self.assertEqual(route._role_spec("engine-writer", self.rules)["needs"], ["engine"])
        self.assertEqual(route._role_spec("writer", self.rules)["needs"], [])
        self.rules["roles"]["old-style"] = ["gpt-6.1-sol"]
        self.assertEqual(route._role_spec("old-style", self.rules)["needs"], [])
        self.assertEqual(route.role_needs("analysis", ["python", "big-files"], self.rules), ["big-files", "python"])

    def test_writer_passes_codex_over_only_on_windows(self):
        for need in ("engine", "python"):
            for platform in ("win32", "linux"):
                with self.subTest(need=need, platform=platform), mock.patch.object(route, "host_platform", return_value=platform), \
                     mock.patch.object(route, "_check_allowed", wraps=route._check_allowed) as allowed, \
                     mock.patch.object(route, "reserve_quota") as reserve:
                    result = route.pick("writer", needs=[need], rules=self.rules, fake=self.state, context="personal")
                if platform == "win32":
                    self.assertEqual(result["model"], "gemini-4-argon-high")
                    self.assertIn(f"gpt-6.1-sol cannot {need} here (traits in roles.json)", result["passed_over"])
                    self.assertNotIn("codex", [call.args[0] for call in allowed.call_args_list])
                else:
                    self.assertEqual(result["model"], "gpt-6.1-sol")
                    self.assertEqual(result["passed_over"], [])
                reserve.assert_not_called()

    def test_all_cloud_candidates_lacking_fall_back_to_subscription(self):
        self.rules["traits"]["cannot"]["agy"] = {"*": ["engine"]}
        with mock.patch.object(route, "host_platform", return_value="win32"):
            result = route.pick("writer", needs=["engine"], rules=self.rules, fake=self.state, context="personal")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["exit_code"], 6)
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["passed_over"], [
            "gpt-6.1-sol cannot engine here (traits in roles.json)",
            "gemini-4-argon-high cannot engine here (traits in roles.json)",
        ])

    def test_analysis_default_passes_ollama_over_without_gpu_check(self):
        self.rules["roles"]["analysis"]["models"].insert(0, {"provider": "ollama", "model": "qwen3:14b"})
        with mock.patch.object(route, "local_state") as gpu:
            result = route.pick("analysis", rules=self.rules, fake=self.state, context="personal")
        self.assertEqual(result["model"], "gpt-6.1-sol")
        self.assertIn("qwen3:14b cannot big-files here (traits in roles.json)", result["passed_over"])
        gpu.assert_not_called()

    def test_named_model_cannot_even_when_forced_or_role_needs_only(self):
        for kwargs in ({"needs": ["engine"], "force": True}, {"role": "engine-writer"}):
            with self.subTest(kwargs=kwargs), mock.patch.object(route, "host_platform", return_value="win32"), \
                 mock.patch.object(route, "_check_allowed") as allowed:
                result = route.check_name({"provider": "codex", "model": "gpt-6.1-sol"}, rules=self.rules,
                                          fake=self.state, context="personal", **kwargs)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "cannot")
            self.assertEqual(result["exit_code"], 2)
            self.assertEqual(result["reason"], "gpt-6.1-sol cannot engine here (traits in roles.json)")
            allowed.assert_not_called()

    def test_needs_parsing_and_unknown_cli_need(self):
        parsed = writer.parser().parse_args(["--needs", " python,engine, python, "])
        self.assertEqual(parsed.needs, ["engine", "python"])
        for cli in ("writer", "ask"):
            with self.subTest(cli=cli), mock.patch.object(sys, "stderr", io.StringIO()) as errors, \
                 mock.patch.object(writer.subprocess, "Popen") as launch, \
                 mock.patch.object(ask, "load_config", return_value=("agy", "ollama")), \
                 mock.patch.object(sys, "argv", ["ask.py", "--needs", "pythno"]):
                with self.assertRaises(SystemExit) as stopped:
                    writer.main(["--needs", "pythno"]) if cli == "writer" else ask.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn("unknown need: pythno", errors.getvalue())
                self.assertIn("known needs: python, engine, big-files", errors.getvalue())
                launch.assert_not_called()

    def test_wait_rechecks_preserve_needs(self):
        with mock.patch.object(route, "host_platform", return_value="win32"):
            result = route.wait_until_free(role="writer", needs=["engine"], rules=self.rules,
                                           fake=self.state, context="personal", stream=io.StringIO())
        self.assertEqual(result["model"], "gemini-4-argon-high")
        self.assertIn("gpt-6.1-sol cannot engine here (traits in roles.json)", result["passed_over"])
        with mock.patch.object(route, "host_platform", return_value="win32"), mock.patch.object(route.time, "sleep") as sleep:
            result = route.wait_until_free(name={"provider": "codex", "model": "gpt-6.1-sol"}, needs=["engine"],
                                           rules=self.rules, fake=self.state, context="personal", stream=io.StringIO())
        self.assertEqual(result["status"], "cannot")
        sleep.assert_not_called()

    def test_writer_combines_role_cli_and_engine_needs_before_launch(self):
        self.rules["roles"]["writer"]["needs"] = ["big-files"]
        for named in (False, True):
            with self.subTest(named=named), tempfile.TemporaryDirectory() as tmp:
                proc = mock.Mock(returncode=0, stdout=io.StringIO(json.dumps({"event": "result", "result": {
                    "conversation_id": "traits-thread", "response": "Done", "usage": {"total_tokens": 10}}}) + "\n"))
                args = ["--legacy-cli", "--cwd", tmp, "--brief", "brief.md", "--needs", "python", "--engine"]
                if named:
                    args += ["--model", "gemini-4-argon-high", "--agy"]
                with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state), "NO_COLOR": "1"}), \
                     mock.patch.object(route, "host_platform", return_value="win32"), \
                     mock.patch.object(route, "load_roles", return_value=self.rules), \
                     mock.patch.object(route, "pick", wraps=route.pick) as pick, \
                     mock.patch.object(route, "check_name", wraps=route.check_name) as check, \
                     mock.patch.object(route, "reserve_quota", wraps=route.reserve_quota) as reserve, \
                     mock.patch.object(writer.subprocess, "Popen", return_value=proc) as launch, \
                     mock.patch.object(sys, "stdout", io.StringIO()), \
                     mock.patch.object(sys, "stderr", io.StringIO()) as errors:
                    self.assertEqual(writer.main(args), 0)
                routed = check if named else pick
                self.assertEqual(routed.call_args.kwargs["needs"], ["big-files", "engine", "python"])
                command = launch.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], "gemini-4-argon-high")
                self.assertEqual(reserve.call_args.args[2], "agy")
                if not named:
                    self.assertIn("gpt-6.1-sol cannot engine, python here (traits in roles.json)", errors.getvalue())

    def test_ask_named_refusal_and_role_forwarding(self):
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state), "NO_COLOR": "1"}), \
             mock.patch.object(route, "host_platform", return_value="win32"), \
             mock.patch.object(sys, "argv", ["ask.py", "--codex", "gpt-6.1-sol", "--needs", "engine", "question"]), \
             mock.patch.object(ask, "ask_codex") as launch, \
             mock.patch.object(sys, "stderr", io.StringIO()) as errors:
            self.assertEqual(ask.main(), 2)
        self.assertIn("gpt-6.1-sol cannot engine here (traits in roles.json)", errors.getvalue())
        launch.assert_not_called()
        self.rules["traits"]["cannot"]["agy"] = {"*": ["engine"]}
        with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state), "NO_COLOR": "1"}), \
             mock.patch.object(route, "host_platform", return_value="win32"), \
             mock.patch.object(route, "load_roles", return_value=self.rules), \
             mock.patch.object(sys, "argv", ["ask.py", "--role", "writer", "--needs", "engine", "question"]), \
             mock.patch.object(sys, "stderr", io.StringIO()) as errors:
            self.assertEqual(ask.main(), 6)
        self.assertIn("gpt-6.1-sol cannot engine here (traits in roles.json)", errors.getvalue())
        self.assertIn("gemini-4-argon-high cannot engine here (traits in roles.json)", errors.getvalue())

    def test_list_notes_show_platform_traits_only_when_requested(self):
        for notes in (False, True):
            with self.subTest(notes=notes), \
                 mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(self.state), "NO_COLOR": "1"}), \
                 mock.patch.object(route, "host_platform", return_value="win32"), \
                 mock.patch.object(sys, "argv", ["ask.py", "--list"] + (["--notes"] if notes else [])), \
                 mock.patch.object(sys, "stdout", io.StringIO()) as output:
                self.assertEqual(ask.main(), 0)
            if notes:
                self.assertIn("cannot here: python, engine", output.getvalue())
                self.assertIn("cannot here: python, engine, big-files", output.getvalue())
            else:
                self.assertNotIn("cannot here:", output.getvalue())

    def test_no_test_patches_sys_platform(self):
        import glob
        import re
        # patch.object on any sys with the attribute name platform, any spacing; and patch on the dotted name.
        leak = re.compile(r"""\bsys\s*,\s*["']platform["']|["']sys\.platform["']""")
        for sample in ("route.sys" + ',"platform"', "sys" + " , 'platform'", '"sys' + '.platform"'):
            self.assertRegex(sample, leak)
        paths = sorted(glob.glob(os.path.join(HERE, "test_*.py")) + glob.glob(os.path.join(TOOLS_DIR, "test_*.py")))
        self.assertTrue(paths)
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                found = leak.search(handle.read())
            self.assertIsNone(found, f"{os.path.basename(path)} patches sys.platform ({found and found.group(0)}); "
                                     "patch route.host_platform instead")


if __name__ == "__main__":
    unittest.main(verbosity=2)



class ReviewFixes(unittest.TestCase):
    """Fixes from the review: the GPU lock is atomic, 429 is a status code, a reached 5-hour window keeps the week."""

    def test_local_lock_is_atomic(self):
        import concurrent.futures, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "local.lock")
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda _: route.acquire_local_lock("m", lock_path=path)[0], range(8)))
            self.assertEqual(results.count(True), 1)

    def test_429_inside_a_count_is_not_a_quota_error(self):
        self.assertFalse(route.is_quota_error(0, "Reviewed 1429 lines"))
        self.assertFalse(route.is_quota_error(0, "session 3f429a"))
        self.assertFalse(route.is_quota_error(0, "Reviewed 429 lines"))
        self.assertTrue(route.is_quota_error(0, "HTTP 429 Too Many Requests"))
        self.assertTrue(route.is_quota_error(1, "warn: 429"))
        self.assertTrue(route.is_quota_error(1, "rate limit reached"))

    def test_reached_5h_window_keeps_the_week(self):
        data = {"result": {"rateLimits": {"planType": "p", "rateLimitReachedType": "primary",
                                           "primary": {"windowDurationMins": 300, "usedPercent": 100, "resetsAt": 0},
                                           "secondary": {"windowDurationMins": 10080, "usedPercent": 40, "resetsAt": 0}}}}
        pools = route._pools_from_codex_usage(data, 0)
        by_type = {w["window"]: w for w in pools[0]["windows"]}
        self.assertEqual(by_type["5h"]["remaining_fraction"], 0.0)
        self.assertAlmostEqual(by_type["weekly"]["remaining_fraction"], 0.6)
        self.assertTrue(pools[0]["quota_reached"])


    def test_legacy_paths_from_the_review(self):
        self.assertEqual(ask._stdout_part('{"a":1}\n--- stderr ---\n{"level":"warn"}'), '{"a":1}')
        events = CodexTests().events("Codex answer")
        run = mock.Mock(returncode=0, stdout=events, stderr="warn: 429 Too Many Requests, retrying\n")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ask.subprocess, "run", return_value=run), \
             mock.patch.object(route, "LOG_DIR", tmp):
            result = ask.ask_codex("gpt-6.1-sol", "Question", False, 10, executable="fake")
        self.assertEqual((result[0], result[1], result[5]), (0, "Codex answer", "ok"))


class LocksFromTheReview(unittest.TestCase):
    """: the reservations lock takeover is atomic, writes are atomic, conversations are recorded under a lock."""

    def test_stale_lock_takeover_leaves_one_holder(self):
        import concurrent.futures
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "r.lock")
            with open(lock, "w") as f:
                f.write("999999")  # a dead holder
            os.utime(lock, (time.time() - 60, time.time() - 60))
            with mock.patch.object(route, "pid_alive", lambda pid, procs=None: pid == os.getpid()):
                with concurrent.futures.ThreadPoolExecutor(6) as pool:
                    got = list(pool.map(lambda _: route._acquire_file_lock(lock, timeout_s=0.3), range(6)))
            self.assertEqual(got.count(True), 1)
            self.assertEqual(os.listdir(tmp), ["r.lock"])

    def test_unlocked_change_is_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, lock = os.path.join(tmp, "reservations.json"), os.path.join(tmp, "reservations.json.lock")
            with open(lock, "w") as f:
                f.write(str(os.getpid()))  # held by a live process, forever
            err = io.StringIO()
            with mock.patch.object(route, "_acquire_file_lock", return_value=False), contextlib.redirect_stderr(err):
                out = route._modify_reservations(lambda items: items + [{"pid": os.getpid(), "model": "m"}], reservations_path=path)
            self.assertEqual(len(out), 1)
            self.assertFalse(os.path.exists(path))
            self.assertIn("not written", err.getvalue())

    def test_conversations_recorded_at_once_keep_both(self):
        import concurrent.futures
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "conv.json")
            def record(i):
                return route.record_conversation_usage(f"c{i}", {"in": 10, "out": 5}, conversations_path=path)
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                list(pool.map(record, range(8)))
            self.assertEqual(sorted(route.load_agy_conversations(path)), [f"c{i}" for i in range(8)])
            self.assertFalse([n for n in os.listdir(tmp) if n.endswith(".tmp")])

    def test_pid_alive_reads_permission_denied_as_alive(self):
        if sys.platform.startswith("win"):
            self.skipTest("POSIX os.kill")
        with mock.patch.object(route.os, "kill", side_effect=PermissionError()):
            self.assertTrue(route.pid_alive(4242))
        with mock.patch.object(route.os, "kill", side_effect=ProcessLookupError()):
            self.assertFalse(route.pid_alive(4242))


class SmallFindingsFromTheReview(unittest.TestCase):
    """: the fit under the reservations lock, exit 8 without a GPU budget, a bad journal row, journal_event's path."""

    def test_reservation_is_refused_under_the_lock_when_live_reservations_fill_the_window(self):
        st = free_state()
        st["cloud"]["gemini"]["weekly"] = 0.20
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reservations.json")
            cs = route.cloud_state(fake=st)
            first = route.reserve_quota("t", "review", "agy", "gemini-4-argon-high", tokens=2_000_000, fake=st,
                                        reservations_path=path, cs=cs)
            self.assertNotIn("refused", first)
            second = route.reserve_quota("t", "review", "agy", "gemini-4-argon-high", tokens=2_000_000, fake=st,
                                         reservations_path=path, cs=cs)
            self.assertIn("floor", second.get("refused", ""))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 1)  # nothing written for the refused one
            forced = route.reserve_quota("t", "review", "agy", "gemini-4-argon-high", tokens=2_000_000, fake=st,
                                         reservations_path=path, cs=cs, force=True)
            self.assertNotIn("refused", forced)

    def test_local_only_role_without_gpu_memory_exits_8(self):
        st = free_state()
        st["local"]["gpu_total_mb"] = 0
        with mock.patch.object(route, "provider_present", return_value={"present": True}):
            res = route.pick("index", fake=st)
        self.assertEqual(res.get("exit_code"), route.EXIT_UNSUPPORTED)
        self.assertIn("model_budget_gb", res["message"])

    def test_bad_journal_row_and_event_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.jsonl")
            now = 1800000000
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ts": "not-a-number", "model": "x", "tokens": {"total": 1}}) + "\n")
                f.write(json.dumps({"ts": now - 10, "model": "m", "provider": "agy", "tokens": {"in": 5, "out": 5, "total": 10}, "status": "ok"}) + "\n")
            by = route.read_journal_24h(now=now, journal_path=path, rules={})
            self.assertEqual(by["m"]["calls"], 1)  # the bad row is skipped, not the rest of the file
            route.journal_event("open", journal_path=path, conversation="c1")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.loads(f.read().splitlines()[-1])["event"], "open")


class WishTests(unittest.TestCase):
    """Roles as wishes: {"family", "effort"} rows are matched against the
    live inventory when the role is called, and every older rule works on the ids they matched."""

    def state(self, agy=None, codex=None, machine=None):
        st = free_state()
        st["agy_models"] = agy if agy is not None else [
            "gemini-4-argon-low", "gemini-4-argon-high", "gemini-4-argon-xhigh", "gemini-3.8-flash-high"]
        efforts = [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh")]
        st["codex_models"] = {"result": {"data": [
            dict({"id": mid, "hidden": False, "defaultReasoningEffort": "medium", "supportedReasoningEfforts": efforts})
            for mid in (codex if codex is not None else ["gpt-6.1-sol"])]}}
        st["codex_usage"] = CodexTests().usage(10)
        st["machine"] = machine or {}
        return st

    def rules(self, role_entry, **extra):
        rules = route.load_roles()
        rules["roles"] = {"r": role_entry}
        rules.update(extra)
        return rules

    def test_a_new_version_that_fits_the_glob_is_taken_without_an_edit(self):
        rules = self.rules({"prefer": [{"family": "gemini-*-argon*", "effort": ">=high"}]})
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertEqual(res["picked"], "gemini-4-argon-high")
        st = self.state(agy=["gemini-4-argon-high", "gemini-4.5-argon-high", "gemini-4.5-argon-low"])
        res = route.pick("r", fake=st, rules=rules)
        self.assertEqual(res["picked"], "gemini-4.5-argon-high")  # the newest version first
        rules = self.rules({"prefer": [{"family": "gpt-6*", "provider": "codex"}]})
        res = route.pick("r", fake=self.state(codex=["gpt-6.1-sol", "gpt-6.2-sol", "gpt-6.2-sol-mini"]), rules=rules)
        self.assertEqual(res["picked"], "gpt-6.2-sol")  # newest, then the shorter id

    def test_effort_bound_picks_the_cheapest_that_meets_it(self):
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*", "effort": ">=high"}]})
        self.assertEqual(route.pick("r", fake=self.state(), rules=rules)["picked"], "gemini-4-argon-high")
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*"}]})
        self.assertEqual(route.pick("r", fake=self.state(), rules=rules)["picked"], "gemini-4-argon-xhigh")
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*", "effort": "<=medium"}]})
        self.assertEqual(route.pick("r", fake=self.state(), rules=rules)["picked"], "gemini-4-argon-low")
        rules = self.rules({"prefer": [{"family": "gpt-6.1*", "effort": ">=high"}]})
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertEqual((res["picked"], res["effort"]), ("gpt-6.1-sol", "high"))
        rules = self.rules({"prefer": [{"family": "gpt-6.1*", "effort": ">=max"}, {"family": "gemini-3.8-flash*"}]})
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertEqual(res["picked"], "gemini-3.8-flash-high")
        self.assertIn("gpt-6.1*: no model on this machine matches at effort >=max", res["passed_over"])

    def test_a_provider_not_on_this_machine_is_skipped_with_a_note(self):
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*", "provider": "agy", "effort": ">=high"},
                                       {"family": "gpt-6.1*", "effort": ">=high"}]})
        res = route.pick("r", fake=self.state(machine={"paths": {"agy": None}}), rules=rules)
        self.assertEqual((res["picked"], res["provider"]), ("gpt-6.1-sol", "codex"))
        self.assertTrue(any(r.startswith("gemini-4-argon* (agy): agy is not on this machine") for r in res["passed_over"]))
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*"}, {"family": "gpt-6.1*"}]})
        res = route.pick("r", fake=self.state(machine={"paths": {"agy": None}}), rules=rules)
        self.assertIn("gemini-4-argon*: no model on this machine matches; agy is not on this machine", res["passed_over"])

    def test_local_only_wishes_without_a_local_provider_end_at_once(self):
        rules = self.rules({"local_only": True, "prefer": [{"family": "qwen3*"}, {"family": "gpt-oss*"}]})
        res = route.pick("r", fake=self.state(machine={"paths": {"ollama": None}}), rules=rules)
        self.assertEqual(res["exit_code"], route.EXIT_UNSUPPORTED)
        self.assertFalse(res["wait"])
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertEqual((res["picked"], res["provider"]), ("qwen3:14b", "ollama"))

    def test_any_group_of_wishes_is_one_level_and_keeps_effort_per_member(self):
        inv, absent = route.live_inventory(fake=self.state())
        rows, notes = route.expand_wishes(
            [{"any": [{"family": "gemini-4-argon*"}, {"family": "gpt-6.1*"}], "effort": ">=high"},
             {"family": "gemini-4-argon*", "effort": ">=high"},  # already given by the group: not again
             {"provider": "agy", "model": "gemini-3.8-flash-high"}], inv, absent)
        self.assertEqual(notes, [])
        self.assertEqual(rows[0]["any"], [
            {"provider": "agy", "model": "gemini-4-argon-high", "wish": "gemini-4-argon*"},
            {"provider": "codex", "model": "gpt-6.1-sol", "wish": "gpt-6.1*", "effort": "high"}])
        self.assertEqual(rows[1:], [{"provider": "agy", "model": "gemini-3.8-flash-high"}])
        models, groups = route.role_candidates(rows)
        self.assertEqual(groups, [0, 0, 1])

    def test_never_traits_and_exclude_apply_to_the_matched_ids(self):
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*", "effort": ">=high"}, {"family": "gpt-6.1*"}]},
                           never=["gemini-4-argon*"])
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertEqual(res["picked"], "gpt-6.1-sol")
        self.assertTrue(any("gemini-4-argon-high refused" in r for r in res["passed_over"]))
        rules = self.rules({"needs": ["big-files"], "prefer": [{"family": "qwen3*"}, {"family": "gpt-6.1*"}]})
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertEqual(res["picked"], "gpt-6.1-sol")
        self.assertTrue(any("qwen3:14b cannot big-files" in r for r in res["passed_over"]))
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*", "effort": ">=high"}, {"family": "gpt-6.1*"}]})
        res = route.pick("r", fake=self.state(), rules=rules, exclude=["gemini-4-argon-high"])
        self.assertEqual(res["picked"], "gpt-6.1-sol")

    def test_a_bad_effort_is_said_and_not_guessed(self):
        rules = self.rules({"prefer": [{"family": "gpt-6.1*", "effort": ">=hihg"}]})
        res = route.pick("r", fake=self.state(), rules=rules)
        self.assertIsNone(res["picked"])
        self.assertEqual(res["exit_code"], 2)
        self.assertIn("effort '>=hihg'", res["message"])

    def test_the_owners_id_rows_are_left_as_they_are(self):
        rules = route.load_roles()
        self.assertIs(route.with_wishes(rules, "review")[0], rules)
        self.assertEqual(route.pick("review", fake=self.state(), rules=rules)["picked"], "gemini-4-argon-high")

    def test_list_marks_the_roles_a_wish_would_take(self):
        rules = self.rules({"prefer": [{"family": "gemini-4-argon*", "effort": ">=high"}, {"family": "gpt-6.1*"}]})
        self.assertEqual(route.wish_roles("agy", "gemini-4-argon-high", rules), ["r"])
        self.assertEqual(route.wish_roles("agy", "gemini-4-argon-low", rules), [])
        self.assertEqual(route.wish_roles("codex", "gpt-6.1-sol", rules), ["r"])
        self.assertEqual(route.wish_roles("ollama", "qwen3:14b", rules), [])


class AccountFixture:
    """Two Codex accounts, a personal active one and a work one, and a role that names Codex then agy."""

    DOMAIN = "corp.example"
    WORK, OWN = "codex:~/.codex-work", "codex:~/.codex"

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def usage(used):
        # A week that resets in three days and is a tenth used: calm, so only the policy and the floor decide.
        return {"result": {"rateLimits": {"limitId": "codex", "planType": "plus", "rateLimitReachedType": None,
                                          "primary": {"usedPercent": used, "windowDurationMins": 10080,
                                                      "resetsAt": int(time.time()) + 3 * 86400}}}}

    def state(self, work_used=10, own_used=10, work_ok=False, work_models=None):
        st = CodexTests().state()
        st["codex_accounts"] = [
            {"id": self.OWN, "where": "~/.codex", "email": "someone@example.org"},
            {"id": self.WORK, "where": "~/.codex-work", "email": "someone@" + self.DOMAIN},
        ]
        st["codex_usage"] = self.usage(own_used)
        st["codex_usage_by_account"] = {self.WORK: self.usage(work_used)}
        if work_models is not None:
            st["codex_models_by_account"] = {self.WORK: {"result": {"data": [{"id": m} for m in work_models]}}}
        st["machine"] = {"work_domains": [self.DOMAIN]}
        if work_ok:
            st["machine"]["accounts"] = {self.OWN: {"label": "personal", "work_ok": True}}
        return st

    def rules(self):
        rules = route.load_roles()
        rules["roles"] = {"r": {"models": [{"provider": "codex", "model": "gpt-6.1-sol"},
                                           {"provider": "agy", "model": "gemini-3.8-flash-high"}]}}
        return rules


class AccountChoiceTests(AccountFixture, unittest.TestCase):
    """Account choice by policy among several accounts of one provider (sections 2 and 3, stage 3): a call
    goes to the account the policy ranks first, not blindly to CODEX_HOME."""

    def test_work_goes_to_the_work_account_and_personal_to_the_own_one(self):
        res = route.pick("r", fake=self.state(), rules=self.rules(), context="work")
        self.assertEqual((res["picked"], res["account"], res["account_label"]), ("gpt-6.1-sol", self.WORK, "work"))
        self.assertFalse(res["crossed"])
        self.assertEqual(res["label"], f"gpt-6.1-sol ({self.WORK})")
        res = route.pick("r", fake=self.state(), rules=self.rules(), context="personal")
        self.assertEqual((res["picked"], res["account"], res["account_label"]), ("gpt-6.1-sol", self.OWN, "personal"))
        chk = route.check_name({"provider": "codex", "model": "gpt-6.1-sol"}, fake=self.state(), context="work")
        self.assertEqual((chk["ok"], chk["account"]), (True, self.WORK))

    def test_the_work_accounts_own_pool_and_list_are_checked(self):
        res = route.pick("r", fake=self.state(work_used=99.9), rules=self.rules(), context="work")
        self.assertIsNone(res["picked"])  # the personal codex and agy are refused at work without work_ok
        self.assertTrue(any(r.startswith(f"gpt-6.1-sol ({self.WORK}):") and "floor" in r for r in res["passed_over"]))
        res = route.pick("r", fake=self.state(work_used=99.9, work_ok=True), rules=self.rules(), context="work")
        self.assertEqual((res["picked"], res["account"]), ("gpt-6.1-sol", self.OWN))
        self.assertTrue(res["crossed"])
        res = route.pick("r", fake=self.state(own_used=99.9), rules=self.rules(), context="work")
        self.assertEqual(res["account"], self.WORK)  # the active account's spent pool is not the work one's
        res = route.pick("r", fake=self.state(work_models=["gpt-5.6-luna"]), rules=self.rules(), context="work")
        self.assertIsNone(res["picked"])
        self.assertIn(f"gpt-6.1-sol ({self.WORK}): not offered by codex", res["passed_over"])

    def test_account_home_and_the_adapter_run_there(self):
        self.assertIsNone(route.account_home(self.OWN))
        self.assertIsNone(route.account_home("agy"))
        self.assertEqual(route.account_home(self.WORK), os.path.join(route.ACCOUNTS_HOME, ".codex-work"))
        with mock.patch.dict(os.environ, {"CODEX_HOME": "~/.codex-work"}):
            self.assertIsNone(route.account_home(self.WORK))
            self.assertEqual(route.account_home(self.OWN), os.path.join(route.ACCOUNTS_HOME, ".codex"))
        with mock.patch.object(ask, "ask_broker", return_value=(0,) * 8) as broker_call:
            providers.get("codex").run(self.WORK, "gpt-6.1-sol", "high", "Q", carry_on=False)
        self.assertEqual(broker_call.call_args.kwargs["home"], os.path.join(route.ACCOUNTS_HOME, ".codex-work"))

    def test_ask_runs_the_role_on_the_chosen_account_and_journals_it(self):
        st = self.state()
        env = {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1", "ASK_CONTEXT": "work"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(route, "load_roles", return_value=self.rules()), \
             mock.patch.object(sys, "argv", ["ask.py", "--role", "r", "Питання"]), \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()) as err, \
             mock.patch.object(ask, "_run_cloud", return_value=(0, "A", {"total": 10}, None, "", "ok", None, None)) as cloud:
            self.assertEqual(ask.main(), 0)
        self.assertEqual(cloud.call_args.kwargs["account"], self.WORK)
        self.assertIn(f"account {self.WORK} (work)", err.getvalue())
        with open(route.JOURNAL_PATH, encoding="utf-8") as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual((row["account"], row["crossed"]), (self.WORK, False))

    def test_a_quota_error_on_one_account_tries_the_next_allowed_one(self):
        st = self.state(work_ok=True)
        env = {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1", "ASK_CONTEXT": "work"}
        calls = [(2, "usage limit", {}, None, "", "quota", None, None), (0, "A", {"total": 5}, None, "", "ok", None, None)]
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(route, "load_roles", return_value=self.rules()), \
             mock.patch.object(sys, "argv", ["ask.py", "--role", "r", "Питання"]), \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()), \
             mock.patch.object(ask, "_run_cloud", side_effect=calls) as cloud:
            self.assertEqual(ask.main(), 0)
        self.assertEqual([c.kwargs["account"] for c in cloud.call_args_list], [self.WORK, self.OWN])
        self.assertEqual([c.args[2] for c in cloud.call_args_list], ["gpt-6.1-sol", "gpt-6.1-sol"])

    def test_writer_runs_codex_with_the_accounts_codex_home(self):
        st = self.state()
        with tempfile.TemporaryDirectory() as tmp:
            brief = os.path.join(tmp, "brief.md")
            with open(brief, "w", encoding="utf-8") as f:
                f.write("Do it")
            proc = mock.Mock(stdout=io.StringIO(json.dumps({"type": "thread.started", "thread_id": "t"})), returncode=0)

            def launch(command, **kwargs):
                with open(command[command.index("-o") + 1], "w", encoding="utf-8") as handle:
                    handle.write("done")
                return proc

            rules = self.rules()
            rules["roles"]["r"]["exit_code"] = 6
            with mock.patch.dict(os.environ, {"ASK_ROUTE_FAKE": json.dumps(st), "NO_COLOR": "1", "ASK_CONTEXT": "work"}), \
                 mock.patch.object(route, "load_roles", return_value=rules), \
                 mock.patch.object(route, "codex_path", return_value="fake-codex"), \
                 mock.patch.object(writer.subprocess, "Popen", side_effect=launch) as popen, \
                 mock.patch.object(sys, "stdout", io.StringIO()), \
                 mock.patch.object(sys, "stderr", io.StringIO()) as err:
                writer.main(["--legacy-cli", "--role", "r", "--cwd", tmp, "--brief", brief, "--log", os.path.join(tmp, "w.json")])
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], os.path.join(route.ACCOUNTS_HOME, ".codex-work"))
        self.assertIn(f"[account {self.WORK}: CODEX_HOME=", err.getvalue())


class AccountWishReviewTests(AccountFixture, unittest.TestCase):
    """The review of round 1 (gpt-6.1-sol): wishes are matched on each account's own list and efforts, the
    expiry rule and the score read the account's own pool, and the legacy writer keeps the chosen effort."""

    def lists(self, own, work):
        def data(models):
            return {"result": {"data": [{"id": m, "supportedReasoningEfforts": [{"reasoningEffort": e} for e in effs]}
                                        for m, effs in models.items()]}}
        return data(own), {self.WORK: data(work)}

    def test_a_wish_matches_on_the_work_accounts_own_list_and_efforts(self):
        st = self.state()
        st["codex_models"], st["codex_models_by_account"] = self.lists({"gpt-6.1-sol": ["low", "high"]},
                                                                       {"gpt-6.2-sol": ["low", "high"]})
        rules = self.rules()
        rules["roles"]["r"] = {"prefer": [{"family": "gpt-6.2*", "provider": "codex"}]}
        res = route.pick("r", fake=st, rules=rules, context="work")
        self.assertEqual((res["picked"], res["account"]), ("gpt-6.2-sol", self.WORK))
        st["codex_models"], st["codex_models_by_account"] = self.lists({"gpt-6.1-sol": ["low", "high"]},
                                                                       {"gpt-6.1-sol": ["low", "medium"]})
        rules["roles"]["r"] = {"prefer": [{"family": "gpt-6.1*", "effort": ">=high"}]}
        res = route.pick("r", fake=st, rules=rules, context="work")
        self.assertIsNone(res["picked"])  # the work account has no high; the personal one is refused at work
        self.assertTrue(any("refused by policy" in r for r in res["passed_over"]))
        st["machine"]["accounts"] = {self.OWN: {"label": "personal", "work_ok": True}}
        res = route.pick("r", fake=st, rules=rules, context="work")
        self.assertEqual((res["picked"], res["account"], res["effort"]), ("gpt-6.1-sol", self.OWN, "high"))

    def test_expiry_reads_the_work_accounts_own_pool(self):
        st = self.state()
        now = int(time.time())

        def usage(used, days):
            return {"result": {"rateLimits": {"limitId": "codex", "planType": "plus", "rateLimitReachedType": None,
                                              "primary": {"usedPercent": used, "windowDurationMins": 10080,
                                                          "resetsAt": now + int(days * 86400)}}}}
        st["codex_usage"] = usage(55, 1)           # the active personal pool burns tomorrow
        st["codex_usage_by_account"] = {self.WORK: usage(10, 6)}   # the work pool, calm: six days left
        from datetime import datetime, timezone
        st["cloud"] = {"gemini": {"weekly": 0.6, "5h": 0.9, "pace": 0.8,
                                  "weekly_reset": datetime.fromtimestamp(now + 3 * 86400, timezone.utc).isoformat()}}
        st["machine"]["accounts"] = {"agy": {"label": "work"}}
        rules = self.rules()
        rules["roles"]["r"] = {"models": [{"any": [{"provider": "codex", "model": "gpt-6.1-sol"},
                                                   {"provider": "agy", "model": "gemini-3.8-flash-high"}]}]}
        res = route.pick("r", fake=st, rules=rules, context="work")
        self.assertEqual(res["picked"], "gemini-3.8-flash-high")
        self.assertEqual(res["why"], "expires_first")

    def test_legacy_writer_keeps_the_wishs_effort(self):
        args = writer.build_codex_command("gpt-6.1-sol", "p", HERE, "a", executable="codex", effort="low")
        self.assertIn("model_reasoning_effort=low", args)
        self.assertIn("model_reasoning_effort=high", writer.build_codex_command("gpt-6.1-sol", "p", HERE, "a", executable="codex"))


class GpuMemoryTests(unittest.TestCase):
    """GPU memory beyond NVIDIA, an unread GPU blocking, and Ollama's memory counted for every local call."""

    def real_local_state(self, gpu, provider="ollama", smi=None, rocm=None, loaded=(), ollama_here=True, servers=None,
                         available=None):
        with mock.patch.object(route, "list_processes", return_value=[]), \
             mock.patch.object(route, "machine_gpu", return_value=gpu), \
             mock.patch.object(route, "query_ollama", return_value=(True, {}, list(loaded))) as ollama, \
             mock.patch.object(route, "query_nvidia_smi", return_value=smi or (0, None, 0, [])), \
             mock.patch.object(route, "query_rocm_smi", return_value=rocm or (0, None, 0, [])), \
             mock.patch.object(route, "provider_present", return_value={"present": ollama_here}), \
             mock.patch.object(route, "load_vram_cache", return_value={}), \
             mock.patch.object(route, "_available_ram_mb", return_value=available), \
             mock.patch.object(route, "inspect_local_lock", return_value=(None, None)), \
             mock.patch.object(route, "load_machine", return_value={"local_servers": servers or {}}), \
             mock.patch.object(providers, "local_servers", return_value=servers or {}):
            return route.local_state(model="m:7b", provider=provider), ollama

    def test_an_unread_gpu_blocks_and_amd_is_read(self):
        loc, _ = self.real_local_state({"kind": "nvidia"})
        self.assertFalse(loc["free"])
        self.assertIn("could not be read (nvidia-smi)", loc["reason_str"])
        loc, _ = self.real_local_state({"kind": "amd"}, rocm=(1024, 24576, 0, []))
        self.assertTrue(loc["free"], loc["reason_str"])
        self.assertEqual(loc["total_mb"], 24576)

    def test_ollamas_memory_counts_for_a_local_server_on_shared_memory(self):
        servers = {"studio": {"url": "http://127.0.0.1:1234/v1", "size_gb": 7}}
        held = [{"name": "big:30b", "size_vram": 10_000_000_000}]
        loc, ollama = self.real_local_state({"kind": "unified", "model_budget_gb": 16}, provider="studio",
                                            loaded=held, servers=servers)
        ollama.assert_called_once()
        self.assertFalse(loc["free"])  # 16 GB budget, Ollama holds 10, the server's model needs 7
        self.assertIn("low VRAM", loc["reason_str"])
        servers["studio"]["static"] = True  # its one model was loaded at its start: nothing more is needed
        loc, _ = self.real_local_state({"kind": "unified", "model_budget_gb": 16}, provider="studio", loaded=held,
                                       servers=servers)
        self.assertTrue(loc["free"], loc["reason_str"])

    def test_a_guessed_kind_whose_tool_reads_nothing_tries_the_other_then_the_budget(self):
        with mock.patch.object(route.shutil, "which", side_effect=lambda n: n):
            loc, _ = self.real_local_state({"kind": "nvidia", "_guessed": True}, rocm=(0, 24576, 0, []))
        self.assertTrue(loc["free"], loc["reason_str"])
        self.assertEqual(loc["total_mb"], 24576)
        with mock.patch.object(route.shutil, "which", side_effect=lambda n: n), \
             mock.patch.object(route, "_ram_bytes", return_value=32 * 1024 ** 3):
            loc, _ = self.real_local_state({"kind": "nvidia", "_guessed": True})
        self.assertEqual(loc["total_mb"], 16 * 1024)  # neither tool reads it: half the RAM
        loc, _ = self.real_local_state({"kind": "nvidia"})  # said in machine.json: it must be read
        self.assertIn("could not be read", loc["reason_str"])

    def test_shared_memory_is_never_more_than_is_available_now(self):
        servers = {"studio": {"url": "http://127.0.0.1:1234/v1", "size_gb": 7}}
        loc, _ = self.real_local_state({"kind": "unified", "model_budget_gb": 16}, provider="studio", servers=servers,
                                       available=2048)
        self.assertFalse(loc["free"])
        self.assertAlmostEqual(loc["free_gb"], 2.0)
        loc, _ = self.real_local_state({"kind": "unified", "model_budget_gb": 16}, provider="studio", servers=servers,
                                       available=30000)
        self.assertTrue(loc["free"], loc["reason_str"])

    def test_the_card_with_the_most_free_memory_or_the_one_named(self):
        cards = [(8000, 8192, 90), (0, 24576, 0)]
        self.assertEqual(route.pick_card(cards), (0, 24576, 0))
        self.assertEqual(route.pick_card(cards, device=0), (8000, 8192, 90))
        self.assertIsNone(route.pick_card(cards, device=5))
        out = "8000, 8192, 90\n0, 24576, 0\n"
        with mock.patch.object(route.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=out)):
            self.assertEqual(route.query_nvidia_smi()[:3], (0, 24576, 0))
            self.assertEqual(route.query_nvidia_smi(device=0)[:3], (8000, 8192, 90))

    def test_rocm_smi_reading(self):
        out = json.dumps({"card0": {"VRAM Total Memory (B)": str(24 * 1024 ** 3), "VRAM Total Used Memory (B)": str(1024 ** 3)}})
        with mock.patch.object(route.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=out)):
            self.assertEqual(route.query_rocm_smi(), (1024, 24576, 0, []))
        two = json.dumps({"card0": {"VRAM Total Memory (B)": str(8 * 1024 ** 3), "VRAM Total Used Memory (B)": str(8 * 1024 ** 3)},
                          "card1": {"VRAM Total Memory (B)": str(24 * 1024 ** 3), "VRAM Total Used Memory (B)": "0"}})
        with mock.patch.object(route.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=two)):
            self.assertEqual(route.query_rocm_smi()[:2], (0, 24576))
            self.assertEqual(route.query_rocm_smi(device=0)[:2], (8192, 8192))
        with mock.patch.object(route.subprocess, "run", side_effect=OSError("no rocm-smi")):
            self.assertEqual(route.query_rocm_smi()[1], None)


class AbsentProviderTests(unittest.TestCase):
    def test_an_agy_not_on_this_machine_is_never_run_for_its_usage(self):
        # Not signed in, agy opens a sign-in window per run: absent must mean not asked at all.
        absent = {"present": False, "path": None, "source": "machine.json", "note": "null"}
        with mock.patch.object(route, "provider_present", side_effect=lambda p, fake=None: absent if p == "agy"
                               else {"present": False, "path": None, "source": "x", "note": "x"}), \
             mock.patch.object(route.subprocess, "run", side_effect=AssertionError("agy was run")) as run, \
             mock.patch.object(route, "fetch_codex_usage", return_value={}), \
             mock.patch.object(route, "USAGE_CACHE", os.path.join(tempfile.mkdtemp(), "agy_usage.json")):
            cs = route.cloud_state(force_refresh=True)
        run.assert_not_called()
        self.assertIsNone(cs["error"])
        self.assertFalse([p for p in cs["pools"] if p.get("provider") == "agy"])

    def test_a_failed_agy_is_not_run_again_for_a_while(self):
        tmp = tempfile.mkdtemp()
        failed = mock.Mock(returncode=1, stdout="", stderr="sign in")
        listed = mock.Mock(returncode=0, stdout="gemini-x-high\tGemini X\n", stderr="")
        with mock.patch.object(route, "CACHE_DIR", tmp), \
             mock.patch.object(route, "AGY_MODELS_CACHE", os.path.join(tmp, "agy_models.json")), \
             mock.patch.object(route, "load_config", return_value=("agy", None)), \
             mock.patch.object(route.subprocess, "run", side_effect=[failed, listed]) as run:
            self.assertEqual(route.fetch_agy_models(), (False, []))
            self.assertIn("exited 1", route.agy_unavailable())
            self.assertEqual(route.fetch_agy_models(), (False, []))  # remembered: no second sign-in window
            self.assertEqual(run.call_count, 1)
            self.assertTrue(route.fetch_agy_models(force_refresh=True)[0])  # --refresh asks at once
            self.assertIsNone(route.agy_unavailable())  # a success clears it
            self.assertEqual(run.call_count, 2)


class LocalKindTests(unittest.TestCase):
    """Local is a kind of provider, not the name ollama (round 5a)."""

    def test_the_kind_comes_from_the_adapter(self):
        self.assertTrue(route.is_local("ollama"))
        for prov in ("agy", "codex", "claude-subscription", "nowhere"):
            self.assertFalse(route.is_local(prov), prov)
        self.assertIn("ollama", route.local_providers())
        self.assertEqual(route.account_info("ollama", {})["label"], "local")

    def test_traits_take_a_local_key_for_every_local_provider(self):
        rules = {"traits": {"cannot": {"local": {"*": ["engine"]}, "ollama": {"*": ["python"]}}}}
        self.assertEqual(route.cannot("ollama", "qwen3:14b", rules=rules), {"engine", "python"})
        self.assertEqual(route.cannot("codex", "gpt-6.1-sol", rules=rules), set())

    def test_the_mac_budget_defaults_to_half_the_ram_and_the_machine_wins(self):
        with mock.patch.object(route, "_ram_bytes", return_value=32 * 1024 ** 3):
            gpu = route.machine_gpu({"hardware": {"gpu": {"kind": "apple"}}})
            self.assertEqual((gpu["model_budget_gb"], gpu["budget_source"]), (16.0, "half of the RAM"))
            self.assertEqual(route.machine_gpu({"hardware": {"gpu": {"kind": "apple", "model_budget_gb": 12}}})
                             ["model_budget_gb"], 12)
            self.assertNotIn("model_budget_gb", route.machine_gpu({"hardware": {"gpu": {"kind": "nvidia"}}}))
            self.assertEqual(route.machine_gpu({"hardware": {"gpu": {"kind": "apple", "model_budget_gb": 0}}})
                             ["model_budget_gb"], 0)  # the machine's own "no memory for local models"
        with mock.patch.object(route, "_ram_bytes", return_value=None):
            self.assertNotIn("model_budget_gb", route.machine_gpu({"hardware": {"gpu": {"kind": "apple"}}}))

    def test_no_model_names_of_one_machine_in_the_core(self):
        self.assertFalse(hasattr(route, "DEFAULT_FILE_SIZES"))
        self.assertEqual(route.needed_vram_gb("some:7b", vram_learned={}, file_sizes={}), 12.0)
        self.assertAlmostEqual(route.needed_vram_gb("gpt-oss:20b", vram_learned={}, file_sizes={}), 19.0)
        self.assertEqual(route.needed_vram_gb("model-without-size", vram_learned={}, file_sizes={}), 12.0)
        self.assertAlmostEqual(route.needed_vram_gb("some:7b", vram_learned={}, file_sizes={"some:7b": 4_000_000_000}),
                               4 * 1.3 + 1.0)

    def test_a_local_call_goes_through_its_own_runner(self):
        a = mock.Mock(think=False, timeout=30, keep=None, effort=None)
        with mock.patch.object(ask, "ask_ollama", return_value=(0, "x", {}, None)) as local:
            self.assertEqual(ask._run_local("ollama", "url", "qwen3:14b", "q", a), (0, "x", {}, None))
        local.assert_called_once()
        server = mock.Mock()
        server.run.return_value = (0, "y", {"total": 3}, None)
        with mock.patch.object(ask, "ask_ollama") as local, mock.patch.object(ask.adapters, "get", return_value=server) as get:
            self.assertEqual(ask._run_local("lmstudio", "url", "m", "q", a), (0, "y", {"total": 3}, None))
        get.assert_called_once_with("lmstudio")
        server.run.assert_called_once_with(None, "m", None, "q", timeout_s=30)
        local.assert_not_called()

    def test_local_only_waits_only_for_its_own_providers(self):
        other = type("OtherLocal", (providers.Adapter,), {"name": "otherlocal", "local": True,
                                                          "detect": lambda self: {"present": True, "path": None,
                                                                                  "source": "fake", "note": None}})
        st = free_state()
        st["machine"] = {"paths": {"ollama": None}}
        rules = route.load_roles()
        rules["roles"] = {"r": {"local_only": True, "models": [{"provider": "ollama", "model": "qwen3:14b"}]}}
        with mock.patch.object(providers, "ADAPTERS", providers.ADAPTERS + (other,)):
            res = route.pick("r", fake=st, rules=rules)
        self.assertEqual((res["exit_code"], res["wait"]), (route.EXIT_UNSUPPORTED, False))
