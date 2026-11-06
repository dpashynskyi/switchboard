"""Offline preset routing and display checks."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from test_route import free_state
import ask
import route
import score
import writer


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.rules = route.load_roles()
        self.fake = free_state()
        self.fake['machine'] = {}
        self.fake['codex_models'] = [{'id': 'gpt-6.1-sol'}]
        patch = mock.patch.dict(os.environ, ASK_PRESET='', ASK_CONTEXT='personal')
        patch.start()
        self.addCleanup(patch.stop)

    def pick(self, role='review', **kw):
        return route.pick(role, fake=self.fake, context='personal', **kw)

    def test_manual_matches_without_presets_every_role(self):
        old = copy.deepcopy(self.rules)
        old.pop('presets')
        for role in self.rules['roles']:
            with self.subTest(role=role):
                before = self.pick(role, rules=old)
                after = self.pick(role, rules=self.rules)
                self.assertEqual(after['preset'], 'manual')
                # The whole decision, live or shadow score included, is the same but for the preset fields.
                strip = lambda d: {k: v for k, v in d.items() if k not in ('preset', 'preset_picks')}
                self.assertEqual(strip(before), strip(after))

    def test_weights_change_shadow_only_and_do_not_mutate(self):
        before = copy.deepcopy(self.rules)
        baseline = self.pick(rules=self.rules)
        with mock.patch.object(score, 'quality', return_value=.5), \
             mock.patch.object(score, 'expected_seconds', side_effect=lambda r,p,m,*a: 10 if p == 'codex' else 500), \
             mock.patch.object(score, 'cost_share', side_effect=lambda r,p,m,*a,**k: 1 if p == 'codex' else .01), \
             mock.patch.object(score, 'rank', wraps=score.rank) as rank:
            self.rules['roles']['review']['score']['mode'] = 'shadow'
            result = self.pick(rules=self.rules, preset='fast')
            manual = copy.deepcopy(self.rules)
            manual.pop('presets')
            plain = self.pick(rules=manual)
            for key in ('model', 'provider', 'passed_over', 'score_pick'):
                self.assertEqual(result[key], plain[key])
            self.assertNotEqual(result['preset_picks']['fast'], result['preset_picks']['thrifty'])
            self.assertIn({'time': 1.5, 'cost': .2}, [c.kwargs.get('weights') for c in rank.call_args_list])
        self.rules['roles']['review']['score']['mode'] = before['roles']['review']['score']['mode']
        self.assertEqual(self.rules, before)
        self.assertEqual(baseline['preset'], 'manual')

    def test_precedence_and_unknown(self):
        def resolve(flag=None, env=None, machine=None):
            return route.resolve_preset(flag, self.rules, env or {}, machine or {})
        self.assertEqual(resolve('fast', {'ASK_PRESET':'strong'}, {'preset':'thrifty'})['source'], 'flag')
        self.assertEqual(resolve(env={'ASK_PRESET':'strong'}, machine={'preset':'thrifty'})['preset'], 'strong')
        self.assertEqual(resolve(env={'ASK_PRESET':'  '}, machine={'preset':'thrifty'})['preset'], 'thrifty')
        self.assertEqual(resolve()['preset'], 'manual')
        with self.assertRaisesRegex(ValueError, 'known presets: manual, thrifty, strong, fast'):
            resolve('absent')
        with mock.patch.object(sys, 'argv', ['ask.py', '--preset', 'absent']), \
             mock.patch.object(ask, 'load_config', return_value=('', '')), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                ask.main()
            self.assertEqual(error.exception.code, 2)

    def test_journal_pass_through(self):
        decision = self.pick(rules=self.rules, preset='thrifty')
        for helper in (ask._policy_kw, writer._score_kw):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'journal.jsonl')
                row = route.journal('test', 'review', 'test', 'agy', 'test', rules=self.rules,
                                    journal_path=path, **helper(decision))
                self.assertEqual(row['preset'], 'thrifty')
                self.assertEqual(row['preset_picks'], decision['preset_picks'])
                with open(path, encoding='utf-8') as f:
                    self.assertEqual(json.load(f), row)

    def test_dry_run_reads_only_and_claude_pool(self):
        rules = copy.deepcopy(self.rules)
        rules['roles'] = {'subagent': {'models': [{'provider':'claude-subscription', 'model':'claude-fable-5-1'}]}}
        fake = copy.deepcopy(self.fake)
        fake['claude_usage'] = {'windows': [{'label':'Weekly · all models', 'window':'weekly',
            'remaining_fraction':.81, 'pace':1.3, 'covers':['claude-*']}]}
        out = io.StringIO()
        with mock.patch.object(route, 'journal', side_effect=AssertionError('journal')), \
             mock.patch.object(route, 'reserve_quota', side_effect=AssertionError('reserve')), \
             mock.patch.object(route, 'wait_until_free', side_effect=AssertionError('wait')), \
             mock.patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
             mock.patch('subprocess.Popen', side_effect=AssertionError('process')), contextlib.redirect_stdout(out):
            ask.print_presets('fast', 'personal', rules=rules, fake=fake)
        self.assertIn('fast *', out.getvalue())
        self.assertIn('claude:claude-fable-5-1', out.getvalue())
        self.assertIn('Claude, all models: 81% left', out.getvalue())

    def test_cli_dry_run_and_status_use_shared_resolver(self):
        for arguments, source in [(['--preset', 'fast'], 'flag'), (['--preset'], 'ASK_PRESET')]:
            with mock.patch.object(sys, 'argv', ['ask.py'] + arguments + ['--context', 'personal']),                  mock.patch.dict(os.environ, ASK_PRESET='thrifty'),                  mock.patch.object(ask, 'load_config', return_value=('', '')),                  mock.patch.object(ask, 'print_presets', return_value=0) as dry:
                self.assertEqual(ask.main(), 0)
                self.assertEqual(dry.call_args.args[0], 'fast' if source == 'flag' else 'thrifty')
                self.assertEqual(dry.call_args.args[1]['context'], 'personal')
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['ask.py', '--status', '--preset', 'strong']),              mock.patch.object(ask, 'load_config', return_value=('', '')),              mock.patch.object(ask, 'print_status', return_value=0), contextlib.redirect_stdout(out):
            self.assertEqual(ask.main(), 0)
        self.assertIn('Preset: strong (flag) (shadow)', out.getvalue())

    def test_preset_failure_is_advisory_and_exempt_roles(self):
        baseline = self.pick(rules=self.rules)
        original = score.rank
        def rank(*args, **kwargs):
            if 'weights' in kwargs:
                raise ValueError('optional scoring unavailable')
            return original(*args, **kwargs)
        with mock.patch.object(score, 'rank', side_effect=rank):
            result = self.pick(rules=self.rules, preset='fast')
        self.assertEqual(result['model'], baseline['model'])
        self.assertEqual(result['passed_over'], baseline['passed_over'])
        with mock.patch.dict(os.environ, ASK_PRESET='absent'):
            self.assertEqual(self.pick(rules=self.rules)['preset'], 'manual')
        self.assertNotIn('preset_picks', result)
        for role in ('index', 'engine-writer'):
            self.assertNotIn('preset_picks', self.pick(role, rules=self.rules))
        self.rules['roles']['review']['pinned'] = True
        self.assertNotIn('preset_picks', self.pick(rules=self.rules))

    def test_missing_weights_and_no_presets(self):
        rules = copy.deepcopy(self.rules)
        rules.pop('presets')
        self.assertEqual(route.resolve_preset(rules=rules, env={}, machine={})['preset'], 'manual')
        with self.assertRaises(ValueError):
            route.resolve_preset('fast', rules, {}, {})
        evaluated = [{'provider':'agy', 'model':'test', 'ok':True}]
        with mock.patch.object(score, 'quality', return_value=.5), \
             mock.patch.object(score, 'expected_seconds', return_value=10), \
             mock.patch.object(score, 'cost_share', return_value=.1):
            cfg = rules['roles']['review']['score']
            a = score.rank('review', evaluated, rules, [], {}, {}, weights={'time':2})
            b = score.rank('review', evaluated, rules, [], {}, {}, weights={'time':2, 'cost':cfg['cost']})
            self.assertEqual(a,b)


if __name__ == '__main__':
    unittest.main()
