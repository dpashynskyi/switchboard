"""Tests of the context marker (switchboard-context) and of ask.py --init / --context-here."""
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
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import init_config
import route


class MarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.deep = os.path.join(self.root, "clients", "acme", "repo", "src")
        os.makedirs(self.deep)

    def mark(self, folder, word):
        with open(os.path.join(folder, route.CONTEXT_MARKER), "w", encoding="utf-8") as f:
            f.write(word + "\n")

    def test_the_nearest_marker_up_the_tree_wins_over_context_paths(self):
        self.mark(self.root, "personal")
        self.mark(os.path.join(self.root, "clients", "acme"), "work")
        machine = {"context_paths": {self.root: "personal"}}
        ctx = route.resolve_context(None, env={}, cwd=self.deep, machine=machine)
        self.assertEqual((ctx["context"], ctx["source"], ctx["strict_default"]), ("work", "marker", False))
        ctx = route.resolve_context(None, env={}, cwd=os.path.join(self.root, "clients"), machine=machine)
        self.assertEqual((ctx["context"], ctx["source"]), ("personal", "marker"))
        self.assertEqual(route.resolve_context(None, env={"ASK_CONTEXT": "personal"}, cwd=self.deep)["source"], "ASK_CONTEXT")
        self.assertEqual(route.resolve_context("personal", env={}, cwd=self.deep)["source"], "--context")

    def test_a_wrong_word_is_work_and_says_so(self):
        self.mark(self.deep, "wrok")
        ctx = route.resolve_context(None, env={}, cwd=self.deep, machine={"context_paths": {self.root: "personal"}})
        self.assertEqual((ctx["context"], ctx["strict_default"]), ("work", True))
        self.assertIn("'wrok'", ctx["note"])

    def test_no_marker_falls_to_context_paths_and_tests_never_read_one_without_a_cwd(self):
        machine = {"context_paths": {self.root: "personal"}}
        self.assertEqual(route.resolve_context(None, env={}, cwd=self.deep, machine=machine)["source"], "context_paths")
        self.mark(self.deep, "work")
        with mock.patch.object(os, "getcwd", return_value=self.deep):
            self.assertEqual(route.resolve_context(None, env={})["context"], "work")  # strict default, not the marker
            self.assertTrue(route.resolve_context(None, env={})["strict_default"])

    def test_a_marker_that_cannot_be_read_is_work_not_the_personal_one_above(self):
        self.mark(self.root, "personal")
        try:
            os.symlink(os.path.join(self.root, "nowhere", "marker"), os.path.join(self.deep, route.CONTEXT_MARKER))
        except (OSError, NotImplementedError):  # Windows without the right to make links
            self.skipTest("this account may not create symbolic links")
        ctx = route.resolve_context(None, env={}, cwd=self.deep, machine={})
        self.assertEqual((ctx["context"], ctx["strict_default"]), ("work", True))

    def test_context_here_writes_the_marker_and_warns_inside_a_repo(self):
        lines = []
        # This test also works when the temporary folder is inside a checkout.
        with mock.patch.object(init_config, "git_root", return_value=None):
            self.assertEqual(init_config.context_here("Work", cwd=self.deep, out=lines.append), 0)
        with open(os.path.join(self.deep, route.CONTEXT_MARKER), encoding="utf-8") as f:
            self.assertEqual(f.read(), "work\n")
        self.assertEqual(len(lines), 1)
        os.makedirs(os.path.join(self.root, "clients", "acme", "repo", ".git"))
        lines = []
        init_config.context_here("personal", cwd=self.deep, out=lines.append)
        self.assertIn("inside the git repository", lines[1])
        self.assertEqual(init_config.context_here("home", cwd=self.deep, out=lines.append), 2)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "machine.json")
        self.fake = {"codex_accounts": [{"id": "codex:~/.codex", "where": "~/.codex", "email": "me@example.org"},
                                        {"id": "codex:~/.codex-work", "where": "~/.codex-work", "email": "me@corp.example"},
                                        {"id": "codex:~/.codex-mail", "where": "~/.codex-mail", "email": "me@public.test"}],
                     "machine": {}}

    def run_init(self, answers=(), **kw):
        lines, replies = [], list(answers)
        asked = []

        def ask(q):
            asked.append(q)
            return replies.pop(0)
        code = init_config.init(path=self.path, fake=self.fake, out=lines.append, ask=ask if answers else None, **kw)
        return code, "\n".join(lines), asked

    def test_without_a_terminal_or_a_kind_it_only_shows(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, out, _ = self.run_init()
        self.assertEqual(code, 0)
        self.assertIn("codex:~/.codex-work  @corp.example", out)
        self.assertIn("Nothing was written", out)
        self.assertFalse(os.path.exists(self.path))

    def test_both_with_a_work_domain_keeps_old_keys_and_backs_up(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"paths": {"ollama": None}, "context_paths": {"~/work": "work"}}, f)
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, out, _ = self.run_init(kind="both", work_domains=["@Corp.Example"], yes=True)
        self.assertEqual(code, 0)
        with open(self.path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written, {"paths": {"ollama": None}, "context_paths": {"~/work": "work", "~": "personal"},
                                   "work_domains": ["corp.example"]})
        self.assertTrue(os.path.exists(self.path + ".bak"))
        self.assertIn("--context-here work", out)

    def test_interactive_asks_only_of_non_public_domains_and_a_no_writes_nothing(self):
        with mock.patch.object(init_config, "PUBLIC_MAIL", {"public.test"}):  # a mail service anyone signs up to
            code, out, asked = self.run_init(answers=["b", "y", "n", "n"])  # both; corp.example yes; example.org no; write no
        self.assertEqual(code, 0)
        self.assertEqual([q.split("@")[1].split(" ")[0] for q in asked if "@" in q], ["corp.example", "example.org"])
        self.assertFalse(any("public.test" in q for q in asked))  # a public mail service is never offered as work
        self.assertIn('"corp.example"', out)
        self.assertFalse(os.path.exists(self.path))

    def test_public_mail_services_of_every_country_are_never_offered(self):
        for domain in ("gmail.com", "yahoo.co.uk", "hotmail.co.uk", "gmx.at", "outlook.fr", "ukr.net", "proton.me"):
            self.assertTrue(init_config.is_public_mail(domain), domain)
        for domain in ("corp.example", "acme.com", "uni-kyiv.edu.ua"):
            self.assertFalse(init_config.is_public_mail(domain), domain)

    def test_a_file_that_cannot_be_read_is_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        code, out, _ = self.run_init(kind="personal", yes=True)
        self.assertEqual(code, 2)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{broken")




class RolesTests(unittest.TestCase):
    """Roles from this machine's models (ask.py --roles, --init's last step) and the user's roles.json over the
    shipped roles."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = os.path.join(self.tmp.name, "roles.json")
        # The shipped default roles live beside this file.
        with open(os.path.join(HERE, "roles.json"), encoding="utf-8") as f:
            self.defaults = json.load(f)
        efforts = [{"reasoningEffort": e} for e in ("low", "medium", "high")]
        # Another machine: none of the shipped families, other models of each provider.
        self.fake = {"machine": {}, "agy_models": ["nova-2-pro-low", "nova-2-pro-high", "nova-2-lite-low"],
                     "codex_models": {"result": {"data": [{"id": "gpt-7-pro", "supportedReasoningEfforts": efforts},
                                                          {"id": "gpt-7-mini", "supportedReasoningEfforts": efforts}]}},
                     "ollama_models": ["llama4:8b", "mistral:7b"], "journal": []}
        self.bench = {"sources": {m: {"top": 100} for m in ("aa-frontiercode", "aa-terminal-bench", "benchlm-coding",
                                                            "aa-intelligence", "arena-text", "aa-lcr", "aa-coding-agent")},
                      "models": {"gpt-7-pro": {"benchlm-coding": 90, "aa-intelligence": 90, "aa-coding-agent": 90},
                                 "nova-2-pro": {"benchlm-coding": 70, "aa-intelligence": 70, "aa-coding-agent": 70},
                                 "gpt-7-mini": {"benchlm-coding": 40, "aa-intelligence": 40, "arena-text": 40}}}

    def propose(self, real=init_config.propose_roles):
        return real(self.defaults, fake=self.fake, journal=[], bench=self.bench)

    def test_the_users_file_goes_over_the_shipped_roles(self):
        base = {"floors": {"weekly": 0.1, "5h": 0.1}, "pace_skip": 2.0, "roles": {"a": {"prefer": [1]}, "b": {"prefer": [2]}}}
        over = {"_comment": "mine", "floors": {"weekly": 0.2}, "pace_skip": 3.0, "roles": {"a": {"prefer": [9]}, "b": None, "c": {}}}
        self.assertEqual(route.merge_roles(base, over), {"floors": {"weekly": 0.2, "5h": 0.1}, "pace_skip": 3.0,
                                                         "roles": {"a": {"prefer": [9]}, "c": {}}})
        with open(self.user, "w", encoding="utf-8") as f:
            json.dump({"pace_skip": 9.0}, f)
        with mock.patch.object(route, "USER_ROLES_PATH", self.user):
            with mock.patch.dict(os.environ, {"ASK_ROLES": ""}):
                self.assertEqual(route.load_roles()["pace_skip"], 9.0)
            self.assertNotEqual(route.load_roles()["pace_skip"], 9.0)  # ASK_ROLES: that file exactly, nothing over it

    def test_a_machine_with_other_models_gets_its_roles_filled_and_keeps_the_authors_rows(self):
        p = self.propose()
        review = p["review"]["prefer"]
        self.assertEqual(review[:len(self.defaults["roles"]["review"]["prefer"])], self.defaults["roles"]["review"]["prefer"])
        added = review[len(self.defaults["roles"]["review"]["prefer"]):]
        self.assertEqual(added, [{"provider": "codex", "family": "gpt-7-pro", "effort": ">=high"},
                                 {"provider": "agy", "family": "nova-2-pro", "effort": ">=high"}])  # best two, by metrics
        draft_added = p["draft"]["prefer"][len(self.defaults["roles"]["draft"]["prefer"]):]
        self.assertEqual(sum(not route.is_local(r["provider"]) for r in draft_added), 1)  # a thrifty role: one cloud
        self.assertEqual(sum(route.is_local(r["provider"]) for r in draft_added), 2)
        index_added = p["index"]["prefer"][len(self.defaults["roles"]["index"]["prefer"]):]
        self.assertEqual({r["provider"] for r in index_added}, {"ollama"})
        writer = route.role_candidates(p["writer"]["prefer"])[0]
        self.assertEqual(writer[-1]["provider"], "claude-subscription")  # the subagent stays the last resort
        self.assertFalse(any(route.is_local(r.get("provider")) for r in writer))

    def test_a_role_with_only_a_subagent_is_never_filled_and_a_fitting_machine_changes_nothing(self):
        self.defaults["roles"]["sub"] = {"prefer": [{"provider": "claude-subscription", "family": "claude-sonnet*"}]}
        self.assertNotIn("sub", self.propose())
        self.fake["agy_models"] = ["gemini-4-argon-high", "gemini-3.8-flash-high", "gemini-3.8-flash-low"]
        self.fake["codex_models"] = {"result": {"data": [{"id": "gpt-6.1-sol", "supportedReasoningEfforts":
                                                          [{"reasoningEffort": "high"}]}]}}
        self.fake["ollama_models"] = ["qwen3:14b", "gemma4:12b", "gpt-oss:20b"]
        self.assertNotIn("review", self.propose())

    def test_refresh_keeps_a_removed_role_removed_and_a_users_role_settings(self):
        with open(self.user, "w", encoding="utf-8") as f:
            json.dump({"roles": {"review": None, "draft": dict(self.defaults["roles"]["draft"], local_only=True)}}, f)
        lines = []
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            init_config.refresh_roles(yes=True, fake=self.fake, out=lines.append, path=self.user, defaults=self.defaults)
        with open(self.user, encoding="utf-8") as f:
            written = json.load(f)
        self.assertIsNone(written["roles"]["review"])
        self.assertTrue(written["roles"]["draft"]["local_only"])
        self.assertFalse(any(not route.is_local(r.get("provider")) for r in
                             route.role_candidates(written["roles"]["draft"]["prefer"])[0][len(self.defaults["roles"]["draft"]["prefer"]):]))

    def test_an_agy_id_without_an_effort_gets_no_bound(self):
        self.fake["agy_models"] = ["plain-model"]
        self.fake["codex_models"] = {"result": {"data": []}}
        added = self.propose()["review"]["prefer"][len(self.defaults["roles"]["review"]["prefer"]):]
        self.assertIn({"provider": "agy", "family": "plain-model"}, added)

    def test_refresh_shows_writes_on_yes_keeps_the_users_other_keys_and_refuses_a_broken_file(self):
        with open(self.user, "w", encoding="utf-8") as f:
            json.dump({"pace_skip": 3.0, "roles": {"mine": {"prefer": []}}}, f)
        lines = []
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
             mock.patch.object(init_config, "propose_roles", side_effect=lambda d, fake=None: self.propose()):
            self.assertEqual(init_config.refresh_roles(fake=self.fake, out=lines.append, path=self.user,
                                                       defaults=self.defaults), 0)
            self.assertIn("Nothing was written", lines[-1])
            self.assertIn("-> ", "\n".join(lines))
            self.assertEqual(init_config.refresh_roles(yes=True, fake=self.fake, out=lines.append, path=self.user,
                                                       defaults=self.defaults), 0)
        with open(self.user, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["pace_skip"], 3.0)
        self.assertIn("mine", written["roles"])
        self.assertIn("review", written["roles"])
        self.assertTrue(os.path.exists(self.user + ".bak"))
        with open(self.user, "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(init_config.refresh_roles(yes=True, fake=self.fake, out=lines.append, path=self.user,
                                                   defaults=self.defaults), 2)

if __name__ == "__main__":
    unittest.main()
