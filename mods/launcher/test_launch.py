"""launch.py offline: which id a launch takes, and what each client runs (nothing is started)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launch  # noqa: E402


class LaunchTest(unittest.TestCase):
    def test_agy_takes_the_effort_its_roles_use_highest_first(self):
        row = {"id": "gemini-4-argon", "ids": {"low": "a-low", "medium": "a-medium", "high": "a-high"},
               "roles": ["analysis@medium", "review@high"]}
        self.assertEqual(launch._model_ids(row), ["a-high"])
        self.assertEqual(launch._model_ids({"id": "x", "ids": {"low": "x-low", "medium": "x-medium"}, "roles": []}),
                         ["x-medium"])
        self.assertEqual(launch._model_ids({"id": "gpt-6.1-sol"}), ["gpt-6.1-sol"])

    def test_claude_desktop_opens_a_code_session_on_the_folder(self):
        argv = launch.open_argv("claude-subscription", "claude-opus-5-5", "desktop", "/w/my repo")
        self.assertEqual(argv, ["open", "claude://code/new?folder=%2Fw%2Fmy+repo&source=switchboard"])

    def test_terminal_runs_the_cli_with_the_model_in_the_folder(self):
        with mock.patch.object(launch.shutil, "which", return_value="/bin/claude"):
            line = launch.command_line("claude-subscription", "claude-opus-5-5", "/w/my repo")
        self.assertEqual(line, "cd '/w/my repo' && /bin/claude --model claude-opus-5-5")
        argv = launch.terminal_argv(line)
        self.assertEqual(argv[:2], ["osascript", "-e"])
        self.assertIn("claude-opus-5-5", argv[2])

    def test_a_button_takes_the_providers_title_colour_and_logo(self):
        entry = {"provider": "codex", "title": "Codex", "color": "#10A37F", "icon": "openai.svg",
                 "detect": {"present": True},
                 "accounts": [{"allowed": True, "models": [{"id": "gpt-6", "roles": []}]}]}
        with mock.patch.object(launch.providers, "inventory", return_value=[entry]):
            [row] = launch.listing()
        self.assertEqual((row["label"], row["color"]), ("Codex", "#10A37F"))
        self.assertTrue(row["icon"].startswith('<svg fill="#10A37F" '))
        self.assertNotIn("<title>", row["icon"])

    def test_a_provider_with_no_logo_gets_none(self):
        self.assertEqual(launch.icon_svg("", "#000"), "")
        self.assertEqual(launch.icon_svg("../roles.json", "#000"), "")
        self.assertEqual(launch.icon_svg("missing.svg", "#000"), "")

    def test_an_unknown_client_is_refused(self):
        with self.assertRaises(SystemExit):
            launch.open_argv("agy", "m", "desktop", "/w")


if __name__ == "__main__":
    unittest.main()
