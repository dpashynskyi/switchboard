"""Tests of the model catalog (catalog.py): the live lists against the roles, new and gone ids, the band's lines."""
import os as _os
import tempfile as _tempfile
# Isolated before route is imported: a machine's own config and caches are never read or written by a test.
_os.environ["ASK_CONFIG_DIR"] = _tempfile.mkdtemp(prefix="ask-test-config-")
_os.environ["ASK_CACHE_DIR"] = _tempfile.mkdtemp(prefix="ask-test-cache-")
_os.environ["ASK_LANG"] = "en"
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import route  # noqa: E402
import catalog  # noqa: E402


def state(agy=None, codex=None, ollama=("qwen3:14b",), anthropic=None):
    st = {"local": {}, "agy_models": list(agy) if agy is not None else None,
          "codex_models": {"result": {"data": [{"id": m, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
                                               for m in (codex or ["gpt-6.1-sol"])]}},
          "ollama_models": list(ollama)}
    if anthropic is not None:
        st["anthropic_models"] = [{"id": m, "display_name": m} for m in anthropic]
    return st


ARGON = ["gemini-4-argon-low", "gemini-4-argon-high", "gemini-3.8-flash-low", "gemini-3.8-flash-high"]
NEON = ["gemini-4-neon-low", "gemini-4-neon-high", "gemini-4-neon-xhigh"]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="ask-catalog-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        for p in (mock.patch.object(route, "CACHE_DIR", self.cache),
                  mock.patch("subprocess.run", side_effect=AssertionError("tests use fake providers")),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("tests use fake providers"))):
            p.start()
            self.addCleanup(p.stop)
        self.rules = route.load_roles(route.ROLES_PATH)

    def wishes(self, report):
        return {(m["role"], m["what"]) for m in report["missing"] if m["kind"] == "wish"}

    def test_a_wish_agy_does_not_list_is_missing_for_every_role_that_has_it(self):
        report = catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000)
        neon = {role for role, what in self.wishes(report) if what.startswith("gemini-4-neon*")}
        self.assertEqual(neon, {"review", "second-opinion", "writer", "code-writer", "analysis", "hard"})
        line = next(m for m in report["missing"] if m["what"].startswith("gemini-4-neon*"))
        self.assertIn("agy", line["why"])
        # Once agy lists Neon, the wish matches and is no longer said.
        report = catalog.check(fake=state(agy=ARGON + NEON), rules=self.rules, now=1001, max_age=0)
        self.assertFalse([w for w in self.wishes(report) if w[1].startswith("gemini-4-neon*")])

    def test_an_effort_the_list_lacks_says_which_efforts_it_has(self):
        report = catalog.check(fake=state(agy=["gemini-4-neon-low"]), rules=self.rules, now=1000)
        line = next(m for m in report["missing"] if m["what"] == "gemini-4-neon* at effort >=high")
        self.assertIn("listed only at other efforts (gemini-4-neon-low)", line["why"])

    def test_a_list_not_read_decides_nothing(self):
        # agy not signed in: its list is not read, so an agy-only wish is not said missing, and a wish open to every
        # provider says which list it could not read.
        rules = {"roles": {"r": {"prefer": [{"family": "gemini-4-neon*", "provider": "agy"},
                                            {"family": "nothing-like-this*"}]}}}
        report = catalog.check(fake=state(agy=None), rules=rules, now=1000)
        self.assertEqual([m["what"] for m in report["missing"]], ["nothing-like-this*"])
        self.assertIn("(not read: agy)", report["missing"][0]["why"])
        self.assertFalse(report["providers"]["agy"]["live"])

    def test_an_id_a_role_names_is_checked_against_its_providers_list(self):
        rules = {"subscription_models": ["claude-opus-5-5"],
                 "roles": {"r": {"models": [{"provider": "codex", "model": "gpt-6.1-sol"},
                                            {"provider": "codex", "model": "gpt-9-typo"},
                                            {"provider": "claude-subscription", "model": "claude-opus-5-5"},
                                            {"provider": "claude-subscription", "model": "claude-opus-9"}]}}}
        report = catalog.check(fake=state(agy=ARGON), rules=rules, now=1000)
        self.assertEqual(sorted((m["provider"], m["what"]) for m in report["missing"]),
                         [("claude-subscription", "claude-opus-9"), ("codex", "gpt-9-typo")])

    def test_new_and_gone_after_the_first_look_and_seen_by_the_user(self):
        first = catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000)
        self.assertEqual(first["new"], [])  # the first look is the baseline, not a flood of new ids
        later = catalog.check(fake=state(agy=ARGON[1:] + NEON), rules=self.rules, now=5000)  # past CHECK_TTL_S
        self.assertEqual({n["id"] for n in later["new"]}, set(NEON))
        high = next(n for n in later["new"] if n["id"] == "gemini-4-neon-high")
        self.assertIn("review", high["roles"])  # taken by a wish, without an edit
        self.assertEqual([g["id"] for g in later["gone"]], ["gemini-4-argon-low"])
        self.assertEqual({n["id"] for n in later["unseen"]}, set(NEON))
        self.assertTrue(any("new models: " in line for line in catalog.band_notes(later)))
        # The user looks: the band stops saying it; --models still lists it.
        catalog.mark_seen(later, now=5001)
        again = catalog.check(fake=state(agy=ARGON[1:] + NEON), rules=self.rules, now=5002)
        self.assertEqual(again["unseen"], [])
        self.assertEqual(again["unseen_missing"], [])
        self.assertEqual(catalog.band_notes(again), [])
        self.assertEqual({n["id"] for n in again["new"]}, set(NEON))

    def test_a_list_that_fails_once_does_not_make_its_ids_gone_and_new(self):
        catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000)
        down = catalog.check(fake=state(agy=None), rules=self.rules, now=5000)
        self.assertEqual(down["gone"], [])
        back = catalog.check(fake=state(agy=ARGON), rules=self.rules, now=9000)
        self.assertEqual((back["new"], back["gone"]), ([], []))

    def test_the_catalog_is_read_again_only_when_old(self):
        catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000)
        with mock.patch.object(catalog, "read_lists", side_effect=AssertionError("read again too soon")):
            catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000 + catalog.CHECK_TTL_S - 1)
        with mock.patch.object(catalog, "read_lists", wraps=catalog.read_lists) as read:
            catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000 + catalog.CHECK_TTL_S + 1)
            self.assertEqual(read.call_count, 1)

    def test_anthropic_list_checks_the_subscription_ids_and_says_new_claude_models(self):
        rules = {"subscription_models": ["claude-opus-5-5", "claude-opus-5-9-typo"], "roles": {}}
        catalog.check(fake=state(agy=ARGON, anthropic=["claude-opus-5-5"]), rules=rules, now=1000)
        report = catalog.check(fake=state(agy=ARGON, anthropic=["claude-opus-5-5", "claude-opus-6"]), rules=rules, now=5000)
        self.assertEqual([(m["role"], m["what"]) for m in report["missing"]], [("subscription_models", "claude-opus-5-9-typo")])
        self.assertEqual([(n["provider"], n["id"], n["roles"]) for n in report["new"]], [("anthropic-api", "claude-opus-6", [])])

    def test_without_a_key_anthropic_is_not_asked(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(catalog.fetch_anthropic_models(), (False, []))

    def test_anthropic_list_is_read_with_the_key_and_cached(self):
        pages = [{"data": [{"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"}], "has_more": True, "last_id": "a"},
                 {"data": [{"id": "claude-haiku-5-5"}], "has_more": False, "last_id": "b"}]
        seen = []

        def fake_open(req, timeout=None):
            seen.append((req.full_url, req.get_header("X-api-key")))
            return contextlib.closing(io.BytesIO(json.dumps(pages[len(seen) - 1]).encode()))

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch("urllib.request.urlopen", side_effect=fake_open):
            ok, items = catalog.fetch_anthropic_models()
            self.assertEqual((ok, [i["id"] for i in items]), (True, ["claude-opus-5-5", "claude-haiku-5-5"]))
            self.assertEqual(seen[1], (catalog.ANTHROPIC_URL + "?limit=1000&after_id=a", "k"))
            self.assertEqual(catalog.fetch_anthropic_models()[1], items)  # from the cache
            self.assertEqual(len(seen), 2)

    def test_models_command_prints_marks_seen_and_exits_1_on_a_missing_row(self):
        catalog.check(fake=state(agy=ARGON), rules=self.rules, now=1000)
        with mock.patch.object(route, "load_roles", return_value=self.rules), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = catalog.main(["--refresh"], fake=state(agy=ARGON))
        self.assertEqual(code, 1)
        self.assertIn("gemini-4-neon* at effort >=high: no id in the live lists of", out.getvalue())
        self.assertIn("review", out.getvalue())
        again = catalog.check(fake=state(agy=ARGON), rules=self.rules)
        self.assertEqual(again["unseen_missing"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
