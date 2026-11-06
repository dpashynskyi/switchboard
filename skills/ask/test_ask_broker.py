"""Offline /ask broker integration. Only the repository's JSON-driven fake agents run."""
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
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'tools'))
import ask
import broker
import route
from test_route import free_state
from test_broker import acp, codex


REAL_POPEN = subprocess.Popen


class AskBrokerTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory(dir=ROOT))
        for key, name in {'CACHE_DIR': '', 'LOG_DIR': 'logs', 'ASK_ROOM': 'agy-room',
                          'CODEX_ROOM': 'codex-room', 'JOURNAL_PATH': 'usage.jsonl',
                          'AGY_CONVERSATIONS_PATH': 'conversations.json',
                          'RESERVATIONS_PATH': 'reservations.json', 'RESERVATIONS_LOCK': 'reservations.lock'}.items():
            self.stack.enter_context(mock.patch.object(route, key, os.path.join(self.tmp, name)))
        self.stack.enter_context(mock.patch.dict(os.environ, {'ASK_ROUTE_FAKE': json.dumps(free_state()),
                                                             'NO_COLOR': '1', 'ASK_CONTEXT': 'personal'}))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.out))
        self.stack.enter_context(contextlib.redirect_stderr(self.err))
        real_popen = REAL_POPEN
        def only_fake(cmd, *args, **kw):
            if os.name == 'nt' and list(cmd[:4]) == ['taskkill', '/T', '/F', '/PID'] and len(cmd) == 5:
                return real_popen(cmd, *args, **kw)  # stop_tree on Windows, stopping a fake server
            self.assertEqual(cmd[:3], [sys.executable, '-B', str(ROOT / 'tools' / 'fake_agents.py')])
            return real_popen(cmd, *args, **kw)
        self.stack.enter_context(mock.patch.object(subprocess, 'Popen', side_effect=only_fake))
        self.stack.enter_context(mock.patch('urllib.request.urlopen', side_effect=AssertionError('no network')))
        original = ask.ask_broker
        def fake_broker(provider, *args, **kw):
            kw['command'] = [sys.executable, '-B', str(ROOT / 'tools' / 'fake_agents.py'),
                             'acp' if provider == 'agy' else 'codex']
            return original(provider, *args, **kw)
        self.stack.enter_context(mock.patch.object(ask, 'ask_broker', side_effect=fake_broker))

    def run_main(self, args, script=None):
        with mock.patch.object(sys, 'argv', ['ask.py'] + args), \
             mock.patch.dict(os.environ, {'BROKER_FAKE_SCRIPT': json.dumps(script or {})}):
            return ask.main()

    def run_broker(self, provider='agy', conversation=None, script=None, **kw):
        with mock.patch.dict(os.environ, {'BROKER_FAKE_SCRIPT': json.dumps(script or {})}):
            return ask.ask_broker(provider, 'model', 'question', conversation, **kw)

    def events(self, result):
        return [json.loads(line) for line in result[4].splitlines()]

    def thought_script(self, text="Private thought"):
        return {"turns": [{"answer": "Exact answer", "steps": [{"notification": "session/update", "params": {
            "update": {"sessionUpdate": "agent_thought_chunk", "content": {"text": text}}}}]}]}

    def test_reasoning_modes_and_continuation(self):
        for mode in ("file", "show", "live"):
            self.out.seek(0); self.out.truncate()
            self.err.seek(0); self.err.truncate()
            args = ["--agy", "gemini-3.8-flash-high", "Question"]
            if mode != "file":
                args += ["--reasoning", mode]
            self.assertEqual(self.run_main(args, self.thought_script()), 0)
            self.assertIn("[reasoning: ", self.err.getvalue())
            if mode == "show":
                self.assertIn("--- reasoning (summary, agy gemini-3.8-flash-high, turn 2) ---\nPrivate thought\n--- answer ---\nExact answer", self.out.getvalue())
            else:
                self.assertNotIn("Private thought", self.out.getvalue())
            if mode == "live":
                self.assertLess(self.err.getvalue().index("[reasoning live: tail -f "), self.err.getvalue().index("[reasoning: "))
        self.assertEqual(self.run_main(["--agy", "gemini-3.8-flash-high", "--continue", "Next"], self.thought_script("Next thought")), 0)
        text = (Path(ask.reasoning_dir()) / "fake-session.md").read_text()
        self.assertIn("## Turn 1,", text)
        self.assertIn("## Turn 2,", text)
        self.assertIn("## Turn 4,", text)

    def test_reasoning_read_prefix_unknown_ambiguous_and_open(self):
        self.assertEqual(self.run_main(["--agy", "gemini-3.8-flash-high", "Question"], self.thought_script()), 0)
        path = Path(ask.reasoning_dir()) / "fake-session.md"
        for args in (["--reasoning-read"], ["--reasoning-read", "fake-s"]):
            self.out.seek(0); self.out.truncate()
            self.assertEqual(self.run_main(args), 0)
            self.assertEqual(self.out.getvalue(), path.read_text())
        for conversation in ("unknown", "fake"):
            (path.parent / "fake-other.md").write_text("other")
            self.out.seek(0); self.out.truncate()
            self.assertEqual(self.run_main(["--reasoning-read", conversation]), 2)
            self.assertEqual(self.out.getvalue(), "")
        self.assertIn("fake-other", self.err.getvalue())
        with mock.patch.object(ask, "_open_for_human") as opener:
            self.assertEqual(self.run_main(["--reasoning-open"]), 0)
            opener.assert_called_once_with(str(path))
            self.assertEqual(self.out.getvalue(), "")

    def test_reasoning_show_empty_and_redacted(self):
        args = ["--agy", "gemini-3.8-flash-high", "--reasoning", "show", "Question"]
        self.assertEqual(self.run_main(args), 0)
        self.assertIn("(no reasoning from the provider this turn)", self.out.getvalue())
        self.out.seek(0); self.out.truncate()
        self.assertEqual(self.run_main(args, self.thought_script("a@example.invalid sk-" + "a" * 20)), 0)
        self.assertNotIn("a@example.invalid", self.out.getvalue())
        self.assertNotIn("sk-", self.out.getvalue())

    def test_main_answer_and_live_log(self):
        self.assertEqual(self.run_main(['--agy', 'gemini-3.8-flash-high', 'Question'],
                                       {'turns': [{'answer': 'Exact answer\nsecond line'}]}), 0)
        self.assertTrue(self.out.getvalue().endswith('Exact answer\nsecond line\n'))
        self.assertNotIn('live log', self.out.getvalue())
        self.assertNotIn('start,', self.out.getvalue())
        self.assertIn('[live log:', self.err.getvalue())
        self.assertEqual(len(list(Path(route.LOG_DIR).glob('*.progress.txt'))), 1)

    def test_codex_tokens_effort_sandbox_and_redaction(self):
        result = self.run_broker('codex', effort='high', script={'turns': [{'answer': 'Answer', 'usage': {
            'inputTokens': 100, 'outputTokens': 20, 'totalTokens': 120, 'reasoningOutputTokens': 3},
            'steps': [{'notification': 'sample', 'params': {'token': 'fake-secret', 'text': 'a@example.invalid'}}]}]})
        self.assertEqual(result[:2], (0, 'Answer'))
        self.assertEqual(result[2]['total'], 120)
        events = self.events(result)
        params = {e['msg'].get('method'): e['msg'].get('params') for e in events if e['dir'] == 'out'}
        self.assertEqual(params['thread/start']['sandbox'], 'read-only')
        self.assertEqual(params['turn/start']['effort'], 'high')
        for path in Path(route.LOG_DIR).iterdir():
            text = path.read_text()
            self.assertNotIn('fake-secret', text)
            self.assertNotIn('a@example.invalid', text)
        self.assertIn('reasoningOutputTokens=3', next(Path(route.LOG_DIR).glob('*.progress.txt')).read_text())

    def test_agy_resumed_journal_and_running_total(self):
        broker.atomic_json(route.AGY_CONVERSATIONS_PATH, {'old': {'in': 500, 'out': 100, 'total': 600, 'cached': 30}})
        self.assertEqual(self.run_main(['--agy', 'gemini-3.8-flash-high', '--thread', 'old', 'Question']), 0)
        row = json.loads(Path(route.JOURNAL_PATH).read_text().splitlines()[-1])
        self.assertEqual(row['tokens']['total'], 120)
        self.assertEqual(row['conversation_tokens']['total'], 720)
        self.assertEqual(route.load_agy_conversations()['old']['total'], 720)

    def test_continue_requires_own_stored_id_and_resumes_both_protocols(self):
        self.assertEqual(self.run_main(['--agy', 'gemini-3.8-flash-high', '--continue', 'Question']), 2)
        self.assertIn('--thread <id>', self.err.getvalue())
        for provider, method, key in [('agy', 'session/resume', 'sessionId'), ('codex', 'thread/resume', 'threadId')]:
            broker.atomic_json(os.path.join(route.CACHE_DIR, 'ask_last_conversation.json'), {provider: 'stored-id'})
            result = self.run_broker(provider, True, script={'resumeUsage': {'inputTokens': 1}})
            event = next(e for e in self.events(result) if e['msg'].get('method') == method)
            self.assertEqual(event['msg']['params'][key], 'stored-id')
            self.assertEqual(result[6], 'stored-id')

    def test_default_never_denies_write_and_command_allows_read(self):
        for kind in ('edit', 'delete', 'move', 'read'):
            step = acp(path='note.txt', kind=kind)
            step['expect'] = {'outcome': {'outcome': 'selected', 'optionId': 'yes-once' if kind == 'read' else 'no-once'}}
            result = self.run_broker(script={'turns': [{'steps': [step]}, {'answer': 'Done'}]})
            self.assertEqual(result[0], 0)
        step = acp(command='python -I agy_run.py')
        step['expect'] = {'outcome': {'outcome': 'selected', 'optionId': 'no-once'}}
        self.assertEqual(self.run_broker(script={'turns': [{'steps': [step]}, {}]})[0], 0)
        step = codex(changes=[{'path': 'note.txt'}])
        step['expect'] = {'decision': 'decline'}
        self.assertEqual(self.run_broker('codex', script={'turns': [{'steps': [step]}]})[0], 0)

    def test_deadline_does_not_fall_over(self):
        decision = {'picked': 'model', 'model': 'model', 'provider': 'agy'}
        with mock.patch.object(route, 'pick', return_value=decision) as pick:
            self.assertEqual(self.run_main(['--role', 'review', '--deadline', '1', 'Question'],
                {'honourCancel': True, 'turns': [{'steps': [{'hang': True}]}]}), 9)
        self.assertEqual(pick.call_count, 1)
        row = json.loads(Path(route.JOURNAL_PATH).read_text().splitlines()[-1])
        self.assertEqual(row['status'], 'deadline')
        self.assertIn('--thread fake-session', self.err.getvalue())

    def test_legacy_adapters_call_original_functions(self):
        with mock.patch.object(ask, '_run_agy_with_retry_and_accounting', return_value='agy') as agy, \
             mock.patch.object(ask, 'ask_codex', return_value='codex') as codex_call:
            self.assertEqual(ask._run_cloud('agy', 'fake', 'model', 'text', False, 20, legacy=True), 'agy')
            self.assertEqual(ask._run_cloud('codex', 'fake', 'model', 'text', False, 20, legacy=True), 'codex')
            agy.assert_called_once()
            codex_call.assert_called_once()

    def test_resume_error_falls_back_once_and_closes(self):
        expected = (0, 'legacy', {}, None, '', 'ok', 'old', None)
        with mock.patch.object(ask, 'ask_codex', return_value=expected) as legacy:
            result = self.run_broker('codex', 'old', script={'resumeError': True})
        self.assertEqual(result, expected)
        legacy.assert_called_once()
        self.assertIn('falling back', self.err.getvalue())

    def test_cap_keeps_partial_usage(self):
        result = self.run_broker('codex', max_tokens=10, script={'turns': [{'steps': [
            {'notification': 'thread/tokenUsage/updated', 'params': {'tokenUsage': {'total': {'inputTokens': 100}}}},
            {'sleep': 10}]}]})
        self.assertEqual(result[0], 5)
        self.assertEqual(result[5], 'capped')
        self.assertEqual(result[2]['total'], 100)


if __name__ == '__main__':
    unittest.main()
