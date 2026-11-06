"""Offline multicriteria rollout tests."""
import copy
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
import tempfile
import unittest
from unittest import mock

import ask
import experience
import route
import score
from test_route import free_state


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.rules = route.load_roles()
        self.bench = experience.load_benchmarks()
        self.fake = free_state()
        self.fake['codex_models'] = [{'id': 'gpt-6.1-sol'}]
        self.env = mock.patch.dict(os.environ, {'ASK_CONTEXT': 'personal'})
        self.env.start()
        self.addCleanup(self.env.stop)
        # No provider discovery, real journal or account settings in scoring tests.
        self.machine = mock.patch.object(route, 'load_machine', return_value={})
        self.machine.start()
        self.addCleanup(self.machine.stop)

    def pick(self, role='review', **kw):
        return route.pick(role, fake=self.fake, rules=self.rules, **kw)

    def test_live_filters_slow_review(self):
        result = self.pick()
        self.assertEqual(result['picked'], 'gpt-6.1-sol')
        self.assertEqual(result['why'], 'score')
        self.assertTrue(any('gemini-4-argon-high: would time out (~16 min > 15 min budget)' in s
                            for s in result['passed_over']))
        self.assertTrue(result['score_notes'])
        self.assertEqual(result['experience'], [])

    def test_second_opinion_scales_aa_tasks_to_one_call(self):
        evaluated = [dict(m, ok=True) for m in self.rules['roles']['second-opinion']['models']]
        ranked, filtered, _ = score.rank('second-opinion', evaluated, self.rules, [], self.bench, {'pools':[]})
        self.assertIn('gpt-6.1-sol', [e['model'] for e in ranked])
        self.assertIn('gemini-3.8-flash-high', [e['model'] for e in ranked])
        self.assertAlmostEqual(score.expected_seconds('second-opinion','codex','gpt-6.1-sol',[],self.rules,self.bench),79.2)
        self.assertNotIn('gpt-6.1-sol', [e['model'] for e in filtered])

    def test_three_review_timeouts_rank_sol_before_argon(self):
        rows = [{'role':'review', 'provider':'agy', 'model':'gemini-4-argon-high',
                 'status':'timeout', 'seconds':302, 'ts':experience.time.time()-i} for i in range(3)]
        self.fake['journal'] = rows
        result = self.pick()
        self.assertEqual(result['picked'], 'gpt-6.1-sol')
        evaluated = [dict(m, ok=True) for m in self.rules['roles']['review']['models']]
        ranked, filtered, _ = score.rank('review', evaluated, self.rules, rows, self.bench, {'pools':[]})
        order = [e['model'] for e in ranked + filtered]
        self.assertLess(order.index('gpt-6.1-sol'), order.index('gemini-4-argon-high'))
        self.assertAlmostEqual(score.expected_seconds('review','agy','gemini-4-argon-high',rows,self.rules,self.bench),939.6)

    def test_timeout_samples_are_censored_and_role_model_specific(self):
        row = {'role':'review', 'provider':'codex', 'model':'gpt-6.1-sol', 'status':'timeout', 'seconds':302}
        cold = 6.6 * 60 * .6
        self.assertAlmostEqual(score.expected_seconds('review','codex','gpt-6.1-sol',[row],self.rules,self.bench), (2*cold+604)/3)
        for other in [dict(row,role='analysis'),dict(row,provider='agy'),dict(row,model='other'),dict(row,type='verdict')]:
            self.assertAlmostEqual(score.expected_seconds('review','codex','gpt-6.1-sol',[other],self.rules,self.bench),cold)
        self.assertEqual(score.expected_seconds('review','agy','missing',[dict(row,provider='agy',model='missing')],self.rules,self.bench),604)

    def test_shadow_keeps_today_and_reports_alternative(self):
        self.rules['roles']['review']['score']['mode'] = 'shadow'
        result = self.pick()
        self.assertEqual(result['picked'], 'gemini-4-argon-high')
        self.assertEqual(result['score_pick'], 'gpt-6.1-sol')
        self.assertEqual(result['score_mode'], 'shadow')
        self.assertEqual(result['passed_over'], [])

    def test_exception_restores_legacy_order(self):
        with mock.patch.object(score, 'rank', side_effect=ValueError('broken optional data')):
            result = self.pick()
        self.assertEqual(result['picked'], 'gemini-4-argon-high')
        self.assertEqual(result['score_notes'], [])

    def test_pinned_and_local_roles_are_held(self):
        self.rules['roles']['review']['pinned'] = True
        with mock.patch.object(score, 'rank', side_effect=AssertionError('must not score')):
            self.assertEqual(self.pick()['picked'], 'gemini-4-argon-high')
            self.assertEqual(self.pick('index')['score_notes'], [])

    def test_size_factor_and_custom_clamp(self):
        rows = [{'role':'review','status':'ok','tokens':{'in':100}}]
        self.assertEqual(score.size_factor('review', None, rows), 1)
        self.assertEqual(score.size_factor('review', 50, []), 1)
        self.assertEqual(score.size_factor('review', 100, rows), 1)
        self.assertEqual(score.size_factor('review', 10000, rows), 3)
        self.assertAlmostEqual(score.size_factor('review', 1, rows), .505)
        self.assertEqual(score.size_factor('review', 10000, rows, {'score_defaults':{'size_clamp':[.6,2]}}), 2)

    def test_codex_default_effort_and_explicit_override(self):
        self.assertEqual(score.variant('codex','gpt-6.1-sol',self.bench)['minutes_per_task'], 6.6)
        self.assertEqual(score.variant('codex','gpt-6.1-sol',self.bench,'low')['minutes_per_task'], 1.7)
        self.assertEqual(score.variant('codex','gpt-6.1-sol-medium',self.bench)['minutes_per_task'], 3.9)

    def test_missing_variant_uses_prior_and_unknown_time(self):
        q = score.quality('review','agy','unlisted',[],self.rules,self.bench)
        self.assertEqual(q, experience.prior('unlisted','review',self.rules,self.bench))
        self.assertIsNone(score.expected_seconds('review','agy','unlisted',[],self.rules,self.bench))
        ranked, filtered, _ = score.rank('review',[{'ok':True,'provider':'agy','model':'unlisted'}],
                                         self.rules,[],self.bench,{'pools':[]})
        self.assertEqual(len(ranked),1)
        self.assertEqual(filtered,[])
        self.assertEqual(score.variant('agy','gpt-oss-120b-medium',self.bench), {})

    def test_owner_bonus_near_tie_clear_gap_cap_and_stable_ties(self):
        rules = {'roles': {'r': {'models':[{'provider':'ollama','model':str(i)} for i in range(8)],
                                'score':{'time_budget_s':900,'metrics':['q']}}},
                 'score_defaults':{'owner_cap':.1,'owner_step':.03}}
        bench = {'sources':{'q':{'top':1}},'variants':{str(i):{'q':.7} for i in range(8)}}
        evaluated = [dict(m,ok=True) for m in rules['roles']['r']['models']]
        ranked,_,_=score.rank('r',evaluated,rules,[],bench,{'pools':[]})
        self.assertEqual(ranked[0]['model'],'0')
        self.assertEqual(ranked[0]['score']['bonus'],.1)
        bench['variants']['7']['q'] = .79
        self.assertEqual(score.rank('r',evaluated,rules,[],bench,{'pools':[]})[0][0]['model'],'0')
        bench['variants']['7']['q'] = .9
        self.assertEqual(score.rank('r',evaluated,rules,[],bench,{'pools':[]})[0][0]['model'],'7')

    def test_times_and_billed_tokens_blend_with_prior_runs(self):
        rows=[{'role':'review','provider':'codex','model':'gpt-6.1-sol','status':'ok',
               'seconds':60,'tokens':{'billed':1000}}]*2
        prior = 60 * 6.6 / 14.1  # Role journal calibration takes precedence over cold AA scale.
        expected = (2 * prior + 2 * 60) / 4
        self.assertAlmostEqual(score.expected_seconds('review','codex','gpt-6.1-sol',rows,self.rules,self.bench),expected)
        self.assertAlmostEqual(score.expected_seconds('review','codex','gpt-6.1-sol',rows,self.rules,self.bench,2),2*expected)
        self.assertGreater(score.expected_tokens('review','codex','gpt-6.1-sol',rows,self.rules,self.bench),500)

    def test_cost_prefers_expiring_quota_and_penalizes_pace(self):
        window={'window':'weekly','available_fraction':.8,'length_s':604800,'resets_in_s':604800,'pace':.8}
        pool={'provider':'codex','label':'codex','covers':['*'],'windows':[window]}
        cs={'pools':[pool]}
        cost=score.cost_share('review','codex','gpt-6.1-sol',[],self.rules,self.bench,cs)
        window['resets_in_s']=60480
        self.assertAlmostEqual(score.cost_share('review','codex','gpt-6.1-sol',[],self.rules,self.bench,cs),cost/10)
        window['pace']=2
        self.assertAlmostEqual(score.cost_share('review','codex','gpt-6.1-sol',[],self.rules,self.bench,cs),cost/5)
        self.assertEqual(score.cost_share('review','ollama','x',[],self.rules,self.bench,cs),0)
        self.assertIsNone(score.cost_share('review','claude-subscription','x',[],self.rules,self.bench,cs))

    def test_policy_rank_precedes_score(self):
        real = route.policy_for
        def policy(provider, *args, **kw):
            result=real(provider,*args,**kw)
            result['rank']=0 if provider=='agy' else 2
            return result
        with mock.patch.object(route,'policy_for',side_effect=policy):
            self.assertEqual(self.pick()['picked'],'gemini-3.8-flash-high')

    def test_role_timeout_and_prompt_size(self):
        for options, expected in [([],900),(['--timeout','77'],77)]:
            decision={'picked':'gpt-6.1-sol','model':'gpt-6.1-sol','provider':'codex',
                      'passed_over':[], 'exit_code':0,'message':'ok'}
            with mock.patch('sys.argv',['ask.py','--role','review','a full question']+options), \
                 mock.patch.object(ask,'load_config',return_value=('fake','fake')), \
                 mock.patch.object(route,'pick',return_value=decision) as pick, \
                 mock.patch.object(route,'load_roles',return_value=self.rules), \
                 mock.patch.object(route,'resolve_context',return_value={'context':'personal'}), \
                 mock.patch.object(route,'cloud_state',return_value={'pools':[]}), \
                 mock.patch.object(route,'reserve_quota',return_value={}), \
                 mock.patch.object(route,'release_reservation'), \
                 mock.patch.object(route,'journal'), \
                 mock.patch.object(ask,'_run_cloud',return_value=(0,'answer',{},None,'','ok',None,None)) as run, \
                 mock.patch('sys.stdout',new_callable=io.StringIO), \
                 mock.patch('sys.stderr',new_callable=io.StringIO):
                self.assertEqual(ask.main(),0)
                self.assertEqual(pick.call_args.kwargs['prompt_tokens'],len('a full question')//4)
                self.assertEqual(run.call_args.args[5],expected)

    def test_role_budget_is_the_deadline_and_its_overrun_a_timeout(self):
        # No --deadline: the role's budget is the broker deadline and a run past it is the model's timeout;
        # a caller's own --deadline stays "deadline".
        for options, deadline, status in [([], 900, 'timeout'), (['--deadline', '60'], 60, 'deadline')]:
            decision={'picked':'gpt-6.1-sol','model':'gpt-6.1-sol','provider':'codex',
                      'passed_over':[], 'exit_code':0,'message':'ok'}
            with mock.patch('sys.argv',['ask.py','--role','review','q']+options), \
                 mock.patch.object(ask,'load_config',return_value=('fake','fake')), \
                 mock.patch.object(route,'pick',return_value=decision), \
                 mock.patch.object(route,'load_roles',return_value=self.rules), \
                 mock.patch.object(route,'resolve_context',return_value={'context':'personal'}), \
                 mock.patch.object(route,'cloud_state',return_value={'pools':[]}), \
                 mock.patch.object(route,'reserve_quota',return_value={}), \
                 mock.patch.object(route,'release_reservation'), \
                 mock.patch.object(route,'journal') as journal, \
                 mock.patch.object(ask,'_run_cloud',return_value=(9,'part',{},None,'','deadline',None,None)) as run, \
                 mock.patch('sys.stdout',new_callable=io.StringIO), \
                 mock.patch('sys.stderr',new_callable=io.StringIO):
                ask.main()
                self.assertEqual(run.call_args.kwargs['deadline'], deadline)
                self.assertEqual(journal.call_args.kwargs['status'], status)

    def test_journal_score_fields_and_printed_shadow(self):
        decision=self.pick()
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as tmp:
            row=route.journal('test','review','x','codex','x',journal_path=os.path.join(tmp,'j'),rules=self.rules,
                              **ask._policy_kw(decision))
            self.assertEqual(row['score_pick'],'gpt-6.1-sol')
            self.assertTrue(row['score_table'])
        output=io.StringIO()
        with mock.patch('sys.stderr',output):
            ask._say_score(dict(decision,score_mode='shadow'),False)
        self.assertIn('[score would pick: gpt-6.1-sol]',output.getvalue())



    def test_explicit_effort_keeps_journal_matching(self):
        rows = [{'role':'review', 'provider':'codex', 'model':'gpt-6.1-sol', 'status':'timeout', 'seconds':900,
                 'ts':experience.time.time()-i} for i in range(3)]
        plain = score.expected_seconds('review','codex','gpt-6.1-sol',rows,self.rules,self.bench)
        evaluated = [{'provider':'codex', 'model':'gpt-6.1-sol', 'ok':True, 'effort':'high'}]
        ranked, filtered, _ = score.rank('review', evaluated, self.rules, rows, self.bench, {'pools':[]})
        self.assertAlmostEqual((ranked + filtered)[0]['score']['seconds'], plain)
        self.assertGreater(plain, 900)
        self.assertEqual(filtered[0]['model'], 'gpt-6.1-sol')

if __name__=='__main__':
    unittest.main()
