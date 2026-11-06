"""Offline history and burn pricing tests; all telemetry is synthetic."""
import copy
import os
import tempfile
import unittest
from unittest import mock

import route
import score


class BurnTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000000.
        self.window = dict(window='weekly', remaining_fraction=.48,
                           available_fraction=.48, resets_in_s=3.2 * 86400,
                           length_s=604800, pace=.94)
        self.pool = dict(provider='agy', label='idle', covers=['a'], windows=[self.window])
        self.reset = self.now + self.window['resets_in_s']
        self.history = {'idle': [[self.now - 86400, .48, self.reset]]}
        self.rules = {'score_defaults': {'owner_step': 0, 'burn_floor': .1},
                      'roles': {'r': {'models': [{'provider': 'agy', 'model': m} for m in ('a', 'b')],
                                      'score': {'time_budget_s': 900, 'cost': 1}}}}
        self.cs = dict(pools=[self.pool], now_ts=self.now)

    def rate(self, history=None, **kw):
        return route.recent_rate('idle', self.window, self.now, self.rules,
                                 history=self.history if history is None else history, **kw)

    def cost(self, history=None, tokens=10000, details=None):
        with mock.patch.object(score, 'expected_tokens', return_value=tokens), \
             mock.patch.object(route, 'pool_window_rate', return_value=1.):
            return score.cost_share('r', 'agy', 'a', [], self.rules, {}, self.cs,
                                    history=history, details=details)

    def test_record_gap_change_prune_and_stale(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as tmp:
            path = os.path.join(tmp, 'history.json')
            record = lambda t: route.record_pool_history([self.pool], t, path, self.rules)
            record(self.now)
            record(self.now + 1)
            self.assertEqual(len(route.read_pool_history(path)['idle']), 1)
            self.window['remaining_fraction'] = .47
            record(self.now + 2)
            self.assertEqual(len(route.read_pool_history(path)['idle']), 2)
            record(self.now + 602)
            self.assertEqual(len(route.read_pool_history(path)['idle']), 3)
            self.pool['stale'] = True
            record(self.now + 1202)
            self.assertEqual(len(route.read_pool_history(path)['idle']), 3)
            record(self.now + 9 * 86400)
            self.assertEqual(route.read_pool_history(path)['idle'], [])
            with open(path, 'w', encoding='utf-8') as f:
                f.write('broken')
            self.assertEqual(route.read_pool_history(path), {})
            self.assertIsNone(route.recent_rate('idle', self.window, self.now, self.rules, path=path))
            self.pool['stale'] = False
            record(self.now)
            self.assertEqual(route.read_pool_history(path)['idle'][0][1], .47)
            self.assertEqual(os.listdir(tmp), ['history.json'])

    def test_history_is_advisory(self):
        with mock.patch.object(route, '_acquire_file_lock', side_effect=OSError):
            route.record_pool_history([self.pool], self.now, rules=self.rules)

    def test_fake_never_reads_or_writes_history(self):
        with mock.patch.object(route, 'record_pool_history') as record, \
             mock.patch.object(route, 'read_pool_history') as read:
            state = route.cloud_state(fake={'cloud': {'pools': [], 'pool_history': self.history},
                                            'codex_usage': {}, 'reservations': []},
                                      now=self.now, rules=self.rules)
        record.assert_not_called()
        read.assert_not_called()
        self.assertEqual(state['pool_history'], self.history)

    def test_rates_and_resets(self):
        self.assertEqual(self.rate(), 0)
        self.history['idle'][0][1] = .72
        self.assertAlmostEqual(self.rate(), .24 / 86400)
        self.history['idle'][0][2] -= 604800
        self.assertIsNone(self.rate())
        self.history['idle'].append([self.now - 7200, .5, self.reset])
        self.assertAlmostEqual(self.rate(), .02 / 7200)
        self.history['idle'] = [[self.now - 86400, .3, self.reset],
                                [self.now - 3600, .48, self.reset]]
        self.assertIsNone(self.rate())
        self.history['idle'][-1][0] = self.now - 7200
        self.assertEqual(self.rate(), 0)
        self.assertIsNone(self.rate({'idle': [[self.now - 3600, .48, self.reset]]}))
        self.assertIsNone(self.rate({'idle': [[self.now - 90000, .48, self.reset]]}))
        self.assertIsNone(self.rate({'idle': [[self.now - 86400, .3, self.reset]]}))

    def test_real_state_records_before_reservations(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as tmp:
            path = os.path.join(tmp, 'history.json')
            def reserve(pools, reservations):
                pools[0]['windows'][0]['available_fraction'] = .2
            with mock.patch.object(route, 'POOL_HISTORY', path), \
                 mock.patch.object(route, 'resolve_fake', return_value=None), \
                 mock.patch.object(route, 'provider_present', return_value={'present': False}), \
                 mock.patch.object(route, '_pools_from_agy_groups', return_value=[self.pool]), \
                 mock.patch.object(route, 'fetch_codex_usage', return_value={}), \
                 mock.patch.object(route, 'load_claude_usage', return_value=([], {})), \
                 mock.patch.object(route, 'read_reservations', return_value=[]), \
                 mock.patch.object(route, '_apply_reservations_to_pools', side_effect=reserve):
                state = route.cloud_state(now=self.now, rules=self.rules, ttl=0)
            self.assertEqual(state['pool_history']['idle'][0], [self.now, .48, self.reset])
            self.assertEqual(state['pools'][0]['windows'][0]['available_fraction'], .2)

    def test_exact_legacy_and_prices(self):
        legacy = .01 / .48 * (self.window['resets_in_s'] / 604800) * max(1., .94)
        self.assertEqual(self.cost(), legacy)
        self.assertEqual(self.cost({}), legacy)
        self.assertAlmostEqual(self.cost(self.history), .1 * legacy)
        self.assertAlmostEqual(self.cost(self.history, tokens=1000000),
                               self.cost(tokens=1000000) * (.1 + .9 * (1 - .48)))
        self.history['idle'][0][1] = 1.
        self.assertEqual(self.cost(self.history), legacy)

    def test_projection_uses_available_and_unknown(self):
        self.window['available_fraction'] = .1
        self.assertEqual(route.projected_unused(self.window, 0), .1)
        self.assertIsNone(route.projected_unused(self.window, None))
        self.window['resets_in_s'] = None
        self.assertIsNone(route.projected_unused(self.window, 0))

    def test_rank_reverses_only_with_history_and_reports_burn(self):
        busy = copy.deepcopy(self.pool)
        busy.update(label='busy', covers=['b'])
        busy['windows'][0].update(remaining_fraction=.8, available_fraction=.8,
                                  resets_in_s=86400)
        self.cs['pools'].append(busy)
        self.history['busy'] = [[self.now - 7200, 1., self.now + 86400]]
        evaluated = [dict(m, ok=True) for m in self.rules['roles']['r']['models']]
        with mock.patch.object(score, 'expected_tokens', return_value=10000), \
             mock.patch.object(score, 'expected_seconds', return_value=60), \
             mock.patch.object(score, 'quality', return_value=.8), \
             mock.patch.object(route, 'pool_window_rate', return_value=1.):
            ranked, _, _ = score.rank('r', evaluated, self.rules, [], {}, self.cs)
            self.assertEqual(ranked[0]['model'], 'b')
            self.assertIsNone(ranked[0]['score']['unused'])
            self.cs['pool_history'] = self.history
            ranked, _, notes = score.rank('r', evaluated, self.rules, [], {}, self.cs)
            self.assertEqual(ranked[0]['model'], 'a')
            self.assertEqual(ranked[0]['score']['unused'], .48)
            self.assertIn('48% would burn', notes[0])

    def test_c0_is_the_dearest_window_before_the_discount(self):
        # The model sits in two pools: the dear one is idle (discounted below the cheap one), the cheap one has no
        # history. C is the dearest after the discount; C0 the dearest before it, whichever window that was.
        second = copy.deepcopy(self.pool)
        second.update(label='second')
        second['windows'][0].update(remaining_fraction=.8, available_fraction=.8, resets_in_s=86400)
        self.cs['pools'].append(second)
        details = {}
        cost = self.cost(self.history, details=details)
        self.assertAlmostEqual(cost, .01 / .8 * (86400 / 604800))
        self.assertAlmostEqual(details['c0'], .01 / .48 * (self.window['resets_in_s'] / 604800))

    def test_discount_for_all_still_counts(self):
        # Both pools idle: both prices fall to the floor. The penalty is scaled by the dearest price before the
        # discount, so the better model wins though its pool is dearer; scaled by the discounted maximum it would not.
        other = copy.deepcopy(self.pool)
        other.update(label='other', covers=['b'])
        other['windows'][0].update(remaining_fraction=.8, available_fraction=.8, resets_in_s=86400)
        self.cs['pools'].append(other)
        self.history['other'] = [[self.now - 86400, .8, self.now + 86400]]
        self.cs['pool_history'] = self.history
        evaluated = [dict(m, ok=True) for m in self.rules['roles']['r']['models']]
        with mock.patch.object(score, 'expected_tokens', return_value=10000),              mock.patch.object(score, 'expected_seconds', return_value=60),              mock.patch.object(score, 'quality', side_effect=lambda r, p, m, *a, **k: .9 if m == 'a' else .8),              mock.patch.object(route, 'pool_window_rate', return_value=1.):
            ranked, _, _ = score.rank('r', evaluated, self.rules, [], {}, self.cs)
        self.assertEqual(ranked[0]['model'], 'a')
        self.assertAlmostEqual(ranked[0]['score']['C'], .1 * ranked[0]['score']['C0'])


if __name__ == '__main__':
    unittest.main()
