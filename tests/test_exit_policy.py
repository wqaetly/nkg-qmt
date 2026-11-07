import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmt.automatic import assess, dividend_assessment
from qmt.config import AppError, load_config
from qmt.experiment import cohort_assessment, prepare_annual_experiment, protocol, register_experiment
from qmt.research import summarize
from qmt.selection import load_rules
from test_automatic import fixture
from test_broad_experiment import DAY, END, broad_client, prepare, quiet, signals
from test_capital_policy_experiment import scaled


POLICIES = {'dividend_policy': 'approved_annual_cash', 'cashflow_policy': 'three_year_coverage'}
NEW = 'known_failure_exit'


class ExitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.original, self.dossier = fixture()
        self.rules = load_rules()

    def shortfall(self):
        next(r for r in self.dossier['data']['cashflow'] if r['end_date'] == '20251231')['n_cashflow_act'] = -1400

    def row(self):
        row = summarize(self.dossier, {'name': '虚构测试公司', 'industry': '测试行业'},
            {'trade_date': self.dossier['as_of'], 'close': '8', 'total_share': '.1', 'dv_ttm': 7.5}, **POLICIES)
        row['selection'] = copy.deepcopy(self.original['selection'])
        return row

    def evaluate(self, row=None, policy=NEW):
        return assess(self.row() if row is None else row, self.dossier, self.rules, **POLICIES, exit_policy=policy)

    def test_known_failures_reject_buys_without_scenarios_after_complete_validation(self):
        for fcf, debt, expected in ((True, False, ['three_year_fcf_shortfall']),
                (False, True, ['short_debt_coverage_shortfall']),
                (True, True, ['three_year_fcf_shortfall', 'short_debt_coverage_shortfall'])):
            with self.subTest(fcf=fcf, debt=debt):
                self.setUp()
                if fcf: self.shortfall()
                if debt: self.dossier['data']['balancesheet'][-1]['st_borr'] = 4000
                old, new = self.evaluate(policy='review_freeze'), self.evaluate()
                self.assertEqual(old['state'], '待数据核实')
                self.assertEqual(new['state'], '排除')
                self.assertEqual(new['exit_decision']['failure_codes'], expected)
                self.assertTrue(new['exit_decision']['validation_complete'])
                self.assertEqual(new['exit_decision']['action'], 'exit')
                self.assertEqual(new['exit_decision']['blockers'], [])
                self.assertEqual(new['scenarios'], [])
                self.assertNotIn('estimated_yield', new)
                self.assertNotIn('estimated_eps', new)
                self.assertEqual(new['evidence_hash'], old['evidence_hash'])

    def test_early_fcf_failure_cannot_mask_later_TTM_debt_shares_or_price_gaps(self):
        for case in ('ttm', 'debt', 'shares', 'price', 'negative_earnings_and_missing_shares',
                     'high_leverage_and_missing_debt', 'valuation', 'liquidity'):
            with self.subTest(case=case):
                self.setUp(); self.shortfall()
                if case == 'ttm':
                    next(r for r in self.dossier['data']['fina_indicator'] if r['end_date'] == '20250930')['profit_dedt'] = None
                if case in ('debt', 'high_leverage_and_missing_debt'):
                    self.dossier['data']['balancesheet'][-1]['st_borr'] = None
                if case == 'high_leverage_and_missing_debt':
                    self.dossier['data']['balancesheet'][-1]['total_liab'] = 9000
                if case == 'negative_earnings_and_missing_shares':
                    next(r for r in self.dossier['data']['fina_indicator'] if r['end_date'] == '20260930')['profit_dedt'] = -500
                row = self.row()
                if case in ('shares', 'negative_earnings_and_missing_shares'): row['share_count'] = None
                if case == 'price': row['price'] = '0'
                if case in ('valuation', 'liquidity'): row['selection'][case]['state'] = '待核实'
                result = self.evaluate(row)
                self.assertEqual(result['state'], '待数据核实')
                self.assertFalse(result['exit_decision']['validation_complete'])
                self.assertEqual(result['exit_decision']['action'], 'freeze')
                self.assertIn('three_year_fcf_shortfall', result['exit_decision']['failure_codes'])
                self.assertEqual(result['scenarios'], [])
                self.assertNotIn('estimated_yield', result)

    def test_cash_scope_payout_and_raw_conflicts_remain_unknown(self):
        for case in ('missing_cash', 'unresolved_annual', 'payout', 'statement_conflict', 'stock_rights'):
            with self.subTest(case=case):
                self.setUp(); self.shortfall()
                data = self.dossier['data']
                if case == 'missing_cash': data['dividend'].pop(0)
                elif case == 'unresolved_annual': data['dividend'][-1]['imp_ann_date'] = '20261201'
                elif case == 'payout':
                    next(r for r in data['dividend'] if r['end_date'] == '20251231')['cash_div_tax'] = '2'
                elif case == 'statement_conflict':
                    data['balancesheet'].append({**data['balancesheet'][-1], 'money_cap': 999})
                else: data['dividend'][-1]['stk_div'] = None
                result = self.evaluate()
                self.assertEqual(result['state'], '待数据核实')
                self.assertFalse(result['exit_decision']['validation_complete'])
                self.assertEqual(result['exit_decision']['action'], 'freeze')
                self.assertEqual(result['scenarios'], [])
                self.assertNotIn('estimated_yield', result)

    def test_short_debt_failure_still_validates_shares_and_price(self):
        self.dossier['data']['balancesheet'][-1]['st_borr'] = 4000
        for field in ('share_count', 'price'):
            with self.subTest(field=field):
                row = self.row(); row[field] = None
                result = self.evaluate(row)
                self.assertEqual(result['exit_decision']['failure_codes'], ['short_debt_coverage_shortfall'])
                self.assertEqual(result['state'], '待数据核实')
                self.assertFalse(result['exit_decision']['validation_complete'])
                self.assertEqual(result['scenarios'], [])
                self.assertNotIn('estimated_yield', result)

    def test_no_new_failure_preserves_legacy_exclusions_and_market_unknowns(self):
        for case in ('earnings', 'leverage', 'valuation', 'liquidity', 'normalized_override'):
            with self.subTest(case=case):
                self.setUp()
                if case == 'earnings':
                    next(r for r in self.dossier['data']['fina_indicator'] if r['end_date'] == '20260930')['profit_dedt'] = -500
                if case == 'leverage': self.dossier['data']['balancesheet'][-1]['total_liab'] = 9000
                row = self.row(); row['selection']['liquidity' if case == 'liquidity' else 'valuation']['state'] = '待核实'
                if case == 'normalized_override': row['price'] = '30'
                old, new = self.evaluate(row, 'review_freeze'), self.evaluate(row)
                decision = new.pop('exit_decision')
                self.assertEqual(decision['failure_codes'], [])
                self.assertEqual(new, old)

    def test_known_failure_with_missing_market_state_remains_unknown(self):
        self.shortfall()
        row = self.row(); row['selection']['valuation'].pop('state')
        self.assertEqual(self.evaluate(row, 'review_freeze')['state'], '待数据核实')
        result = self.evaluate(row)
        self.assertEqual(result['state'], '待数据核实')
        self.assertIn('valuation_or_liquidity_unresolved', result['exit_decision']['blockers'])

    def test_default_and_explicit_legacy_are_identical_and_cohort_stays_legacy(self):
        # Captured from the sealed v35 engine, not from the opt-in implementation.
        hashes = {'pass': '272b25e3404b1fa73a8c916c8915cb639a7df8a3d9a470d41fb60c94e3fe0b40',
                  'fcf': '6dae0e2a420ca9236a943e4916ce303ee30719bdd28e31313d781347c9e19d03',
                  'debt': '3fb757b409b9e04041b9e5272ec1d341687a45e88be7cbbd960cf89fca942adc',
                  'cash': 'fad0d91802282b0a0d6422b1cb08141acd5b8c9d703bfe3c66dd3f05d646aa8d'}
        for case in ('pass', 'fcf', 'debt', 'cash'):
            with self.subTest(case=case):
                self.setUp()
                if case == 'fcf': self.shortfall()
                if case == 'debt': self.dossier['data']['balancesheet'][-1]['st_borr'] = 4000
                if case == 'cash': self.dossier['data']['dividend'].pop(0)
                row = self.row()
                implicit = assess(row, self.dossier, self.rules, **POLICIES)
                self.assertEqual(hashlib.sha256(json.dumps(implicit, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest(), hashes[case])
                self.assertEqual(implicit, self.evaluate(row, 'review_freeze'))
                self.assertNotIn('exit_decision', implicit)
                quality = cohort_assessment(row, self.dossier, self.rules, **POLICIES)
                self.assertEqual(quality['state'], implicit['dimensions']['dividend']['state'])
                self.assertNotIn('exit_decision', quality)

    def test_unchanged_eligible_math_and_equality_boundaries(self):
        for case in ('pass', 'fcf_equal', 'debt_equal'):
            with self.subTest(case=case):
                self.setUp()
                if case == 'fcf_equal':
                    next(r for r in self.dossier['data']['cashflow'] if r['end_date'] == '20251231')['n_cashflow_act'] = -1300
                if case == 'debt_equal': self.dossier['data']['balancesheet'][-1]['st_borr'] = 2900
                old, new = self.evaluate(policy='review_freeze'), self.evaluate()
                decision = new.pop('exit_decision')
                self.assertTrue(decision['validation_complete'])
                self.assertEqual(decision['failure_codes'], [])
                self.assertEqual(new, old)

    def test_invalid_exit_policy_and_incompatible_financial_mode_rejected(self):
        for policy in (None, '', [], 'unknown'):
            with self.subTest(policy=policy):
                for function in (assess, dividend_assessment):
                    with self.assertRaises(AppError): function(self.row(), self.dossier, self.rules, **POLICIES, exit_policy=policy)
        with self.assertRaises(AppError): assess(self.original, self.dossier, self.rules, exit_policy=NEW)


class MonthlyExitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.prepared, _, codes, experiment = prepare(Path(self.temp.name), True, expensive_prefix=False)
        self.code = codes[2]
        self.experiment = scaled(experiment)
        self.new = {**self.experiment, 'version': 'historical-dividend-v36-test', 'exit_policy': NEW}

    def evaluate(self, experiment=None):
        snapshots, audits = signals(self.prepared, '0.05', experiment or self.new)
        return snapshots[DAY], next(a for a in audits[0]['assessments'] if a['symbol'] == self.code)

    def change_industry(self):
        self.prepared[8][self.code] = [{'l1_code': 'different', 'l1_name': '新行业', 'in_date': '20000101', 'out_date': None}]
        for records in self.prepared[9].values():
            for record in records: record.update(l1_code='different', l1_name='新行业')

    def test_default_monthly_output_and_candidates_unchanged(self):
        old = signals(self.prepared, '0.05', self.experiment)
        self.assertEqual(old, signals(self.prepared, '0.05', {**self.experiment, 'exit_policy': 'review_freeze'}))
        new_signals, new_audits = signals(self.prepared, '0.05', self.new)
        self.assertEqual(new_signals, old[0])
        for old_month, new_month in zip(old[1], new_audits):
            for old_row, new_row in zip(old_month['assessments'], new_month['assessments']):
                new_row.pop('exit_decision')
                self.assertEqual(old_row, new_row)

    def test_known_failure_inactive_position_leaves_freeze_and_keeps_original_pool(self):
        cohort_before = copy.deepcopy(self.prepared[2])
        next(r for r in self.prepared[4][self.code]['data']['cashflow'] if r['end_date'] == '20251231')['n_cashflow_act'] = -1400
        self.prepared[3]['universe_membership'] = {self.code: [{'start': '20200101', 'end': DAY}]}
        old, old_row = self.evaluate(self.experiment)
        new, row = self.evaluate()
        self.assertIn(self.code, old['freeze'])
        self.assertEqual(old_row['state'], '待数据核实')
        self.assertNotIn(self.code, new['freeze'])
        self.assertNotIn(self.code, [r['symbol'] for r in new['candidates']])
        self.assertEqual(row['state'], '退出研究池')
        self.assertEqual(row['exit_decision']['failure_codes'], ['three_year_fcf_shortfall'])
        self.assertIsNone(row['estimated_yield'])
        self.assertEqual(self.prepared[2], cohort_before)

    def test_industry_changes_and_unknown_identity_or_interval_still_freeze(self):
        for case in ('changed', 'renamed', 'missing', 'ambiguous', 'name', 'cash', 'hidden_valuation_gap'):
            with self.subTest(case=case):
                prepared = copy.deepcopy(self.prepared)
                previous_code=self.prepared[8][self.code][0]['l1_code']
                self.change_industry()
                if case == 'renamed': self.prepared[8][self.code][0]['l1_code'] = previous_code
                if case == 'missing': self.prepared[8][self.code] = []
                if case == 'ambiguous': self.prepared[8][self.code].append({'l1_code': 'other', 'l1_name': '冲突', 'in_date': '20000101'})
                if case == 'name': self.prepared[5][self.code] = []
                if case == 'cash': self.prepared[4][self.code]['data']['dividend'].pop(0)
                if case == 'hidden_valuation_gap':
                    quotes = self.prepared[6][self.code]
                    current = next(q for q in quotes if q['trade_date'] == DAY)
                    current['close'] = '30'
                    self.prepared[6][self.code] = [q for q in quotes if q['trade_date'] < DAY][:249]+[current]
                old_snapshot, old_row = self.evaluate(self.experiment)
                snapshot, row = self.evaluate()
                self.assertEqual(row['state'], '待数据核实')
                self.assertFalse(row['exit_decision']['validation_complete'])
                self.assertTrue(row['exit_decision']['blockers'])
                self.assertIn(self.code, snapshot['freeze'])
                self.assertNotIn(self.code, [r['symbol'] for r in snapshot['candidates']])
                row.pop('exit_decision')
                self.assertEqual(row, old_row)
                self.assertEqual(snapshot, old_snapshot)
                self.prepared = prepared

    def test_invalid_protocol_chain_rejected_before_any_registration(self):
        for change in ({'exit_policy': None}, {'exit_policy': []}, {'exit_policy': 'unknown'},
                {'valuation_policy': 'historical_context'}, {'earnings_basis': 'ttm'},
                {'capital_policy': 'fixed_amount'}, {'cashflow_policy': 'latest_and_three_year'},
                {'dividend_policy': 'implemented_only'}, {'entry_mode': 'yield_threshold'},
                {'cohort_limit': 20}, {'pool_refresh': 'never'}, {'quality_first': False}, {'retention_yield': '.03'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with self.assertRaises(AppError): register_experiment(root, END, load_config(), {**self.new, **change})
                self.assertFalse((root/'backtests').exists())
        self.assertNotIn('exit_policy', protocol(load_config(), self.experiment))
        self.assertEqual(protocol(load_config(), self.new)['exit_policy'], NEW)

    def test_real_fixture_annual_preparation_keeps_all_seven_raw_inputs(self):
        paths=[]
        for experiment in (self.experiment, self.new):
            client, _ = broad_client(False)
            with patch('qmt.experiment.Tushare', return_value=client), \
                 patch('qmt.experiment.dividend_assessment', wraps=dividend_assessment) as quality:
                prepared = prepare_annual_experiment(Path(self.temp.name), END, quiet, load_config(), experiment)
            self.assertTrue(quality.call_args_list)
            self.assertTrue(all('exit_policy' not in call.kwargs for call in quality.call_args_list))
            paths.append(prepared[0])
        for name in ('cohort.json', 'market.json', 'financials.json', 'names.json',
                     'valuation-history.json', 'amounts.json', 'industry-history.json'):
            with self.subTest(name=name):
                self.assertEqual((paths[0]/name).read_bytes(), (paths[1]/name).read_bytes())


if __name__ == '__main__':
    unittest.main()
