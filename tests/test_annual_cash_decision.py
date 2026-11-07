import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.automatic import assess
from qmt.config import AppError
from qmt.research import DISCLOSURES, implemented_dividends, summarize
from qmt.selection import load_rules
from test_automatic import fixture


POLICY = 'approved_annual_cash'


class AnnualCashDecisionTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()
        _, self.dossier = fixture()
        data = self.dossier['data']
        for row in data['dividend']:
            row['ts_code'] = self.dossier['symbol']
        data['balancesheet'].append({**data['balancesheet'][0], 'end_date': '20201231', 'ann_date': '20210401'})
        data['dividend'].append({**data['dividend'][0], 'end_date': '20201231', 'ann_date': '20210401',
            'imp_ann_date': '20210501', 'record_date': '20210510', 'ex_date': '20210511', 'pay_date': '20210512'})
        self.latest = next(r for r in data['dividend'] if r['end_date'] == '20251231')
        self.latest.update(imp_ann_date='20261120', record_date='20261121', ex_date='20261122', pay_date='20261123')
        self.proof = {'symbol': self.dossier['symbol'], 'period': '20251231', 'available_on': '20261105',
            'source': 'https://example.com/approved.pdf', 'source_sha256': 'a' * 64, 'pages': '2',
            'cash_dps': '.6', 'total_cash': '600', 'share_count': '1000', 'share_basis': 'source_exact_base',
            'classification': 'ordinary_proxy_unverified', 'known_interim_rows': [], 'share_witnesses': [],
            'annual_scope': 'reported_annual_total', 'annual_total_cash': '600',
            'supporting_sources': [{'source': 'https://example.com/annual.pdf', 'source_sha256': 'b' * 64,
                                    'pages': '20', 'available_on': '20260401'}], 'remaining_gaps': []}
        self.patcher = patch.dict(DISCLOSURES, {'annual_cash_decisions': [self.proof]})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def row(self, policy=POLICY):
        row = summarize(self.dossier, {'name': '测试公司', 'industry': '测试行业'},
            {'trade_date': self.dossier['as_of'], 'close': '8', 'total_share': '.1', 'dv_ttm': '7.5'},
            dividend_policy=policy)
        row['selection'] = {'coarse_pass': True, 'valuation': {'state': '通过', 'reason': '测试'},
                            'liquidity': {'state': '通过', 'reason': '测试'}}
        return row

    def assessment(self, policy=POLICY):
        return assess(self.row(policy), self.dossier, self.rules, dividend_policy=policy)

    def test_publication_day_bridge_preserves_raw_events_and_actual_fields(self):
        original = copy.deepcopy(self.dossier)
        self.dossier['as_of'] = '20261104'
        self.assertFalse(self.row()['gate'])
        self.dossier['as_of'] = '20261105'
        row = self.row()
        self.assertTrue(row['gate'])
        self.assertIsNone(row['annual'][0]['implemented_dps'])
        self.assertEqual(row['cash_decision']['stage'], 'approved')
        self.assertEqual(self.assessment()['state'], '满足价格条件')
        self.assertEqual(self.assessment()['cash_coverage_basis'], 'commitment')
        self.assertFalse(self.row('implemented_only')['gate'])
        self.assertFalse(any(e['end_date'] == '20251231' for e in implemented_dividends(self.dossier['data']['dividend'], '20261105')))
        self.assertEqual(self.dossier, original)

    def test_cap_applies_before_and_after_implementation(self):
        self.proof.update(cash_dps='.2', total_cash='200', annual_total_cash='200')
        self.latest['cash_div_tax'] = '.2'
        before = self.assessment()
        self.assertEqual(before['payout'], '0.2')
        self.assertEqual(before['payout_uncapped_median'], '0.6')
        self.dossier['as_of'] = '20261120'
        after = self.assessment()
        self.assertEqual(after['cash_decision']['stage'], 'implemented')
        for field in ('payout', 'estimated_eps', 'fcf_coverage', 'scenarios'):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.assessment('implemented_only')['payout'], '0.6')

    def test_interim_and_annual_total_are_counted_once(self):
        interim = {**self.latest, 'end_date': '20250630', 'ann_date': '20250801', 'imp_ann_date': '20250802',
                   'record_date': '20250803', 'ex_date': '20250804', 'pay_date': '20250805', 'cash_div_tax': '.1'}
        self.dossier['data']['dividend'].append(interim)
        self.proof['known_interim_rows'] = [copy.deepcopy(interim)]
        self.proof.update(cash_dps='.5', total_cash='500')
        self.latest['cash_div_tax'] = '.5'
        before = self.assessment()
        self.assertEqual(self.row()['annual'][0]['implemented_dps'], '0.1')
        self.assertEqual(before['cash_decision']['implemented_cash_included'], '100.0')
        self.assertEqual(before['payout'], '0.6')
        self.assertEqual(Decimal(before['fcf_coverage']), Decimal(5100) / Decimal(1800))
        self.dossier['as_of'] = '20261120'
        self.assertEqual(self.assessment()['scenarios'], before['scenarios'])

    def test_known_special_uses_total_for_coverage_but_not_ordinary_forecast(self):
        interim = {**self.latest, 'end_date': '20250630', 'ann_date': '20250801', 'imp_ann_date': '20250802',
                   'record_date': '20250803', 'ex_date': '20250804', 'pay_date': '20250805', 'cash_div_tax': '.1',
                   'ordinary_cash_div_tax': '0', 'special_cash_div_tax': '.1'}
        self.dossier['data']['dividend'].append(interim)
        self.proof['known_interim_rows'] = [copy.deepcopy(interim)]
        self.proof.update(cash_dps='.5', total_cash='500')
        result = self.assessment()
        self.assertEqual(Decimal(result['cash_decision']['ordinary_cash']), Decimal(500))
        self.assertEqual(Decimal(result['payout']), Decimal('.5'))
        self.assertEqual(Decimal(result['fcf_coverage']), Decimal(5100) / Decimal(1800))

    def test_null_is_not_zero_and_plan_scope_does_not_open_gate(self):
        null = {'ts_code': self.dossier['symbol'], 'end_date': '20250630', 'ann_date': '20250801',
                'div_proc': '股东大会通过', 'cash_div_tax': None, 'stk_div': 0.0,
                'record_date': None, 'ex_date': None, 'pay_date': None, 'imp_ann_date': None}
        self.dossier['data']['dividend'].append(null)
        self.proof['known_interim_rows'] = [copy.deepcopy(null)]
        self.proof.update(annual_scope='plan_only', annual_total_cash=None)
        self.assertEqual(self.row()['cash_decision']['stage'], 'unresolved')
        self.assertFalse(self.row()['gate'])
        self.proof.update(annual_scope='reported_annual_total', annual_total_cash='600')
        self.assertTrue(self.row()['gate'])
        self.assertIsNone(null['cash_div_tax'])
        self.assertEqual(self.row()['cash_decision']['known_interim_rows'], [null])

    def test_missing_fifth_prior_cash_year_still_blocks(self):
        self.dossier['data']['dividend'] = [r for r in self.dossier['data']['dividend'] if r['end_date'] != '20201231']
        self.assertIn('之前五年', self.assessment()['reason'])

    def test_latest_financial_risks_are_not_bypassed(self):
        saved = copy.deepcopy(self.dossier)
        for field, value in (('st_borr', None), ('st_borr', 100000)):
            self.dossier = copy.deepcopy(saved)
            self.dossier['data']['balancesheet'][-2][field] = value
            self.assertEqual(self.assessment()['state'], '待数据核实')
        self.dossier = copy.deepcopy(saved)
        next(r for r in self.dossier['data']['cashflow'] if r['end_date'] == '20251231')['n_cashflow_act'] = 0
        self.assertEqual(self.assessment()['state'], '排除')

    def test_duplicates_cancellations_and_new_interim_facts_block(self):
        with patch.dict(DISCLOSURES, {'annual_cash_decisions': [self.proof, copy.deepcopy(self.proof)]}):
            self.assertFalse(self.row()['gate'])
        self.dossier['data']['dividend'].append({'ts_code': self.dossier['symbol'], 'end_date': '20251231',
                                                'ann_date': '20261106', 'div_proc': '取消', 'cash_div_tax': '0'})
        self.assertTrue(self.row()['gate'])
        self.dossier['as_of'] = '20261106'
        self.assertFalse(self.row()['gate'])
        self.dossier['data']['dividend'].pop()
        self.dossier['data']['dividend'].append({'ts_code': self.dossier['symbol'], 'end_date': '20250630',
                                                'ann_date': '20250801', 'div_proc': '股东大会通过', 'cash_div_tax': None})
        self.assertFalse(self.row()['gate'])

    def test_source_and_policy_fingerprint_and_tamper_reassessment(self):
        row = self.row()
        row['cash_decision']['total_cash'] = '9000'
        self.assertIn('不一致', assess(row, self.dossier, self.rules, dividend_policy=POLICY)['reason'])
        first = self.row()['evidence_hash']
        self.proof['source_sha256'] = 'c' * 64
        self.assertNotEqual(self.row()['evidence_hash'], first)
        self.assertNotEqual(self.row()['evidence_hash'], self.row('implemented_only')['evidence_hash'])
        for policy in (None, '', 'unknown'):
            with self.assertRaises(AppError): self.row(policy)
            with self.assertRaises(AppError): self.assessment(policy)

    def test_new_financial_year_cannot_reuse_previous_approval(self):
        for api in ('income', 'fina_indicator', 'cashflow', 'balancesheet', 'fina_audit'):
            old=next(r for r in self.dossier['data'][api] if r['end_date']=='20251231')
            self.dossier['data'][api].append({**old,'end_date':'20261231','ann_date':'20270401'})
        self.dossier['as_of']='20270430'
        self.assertEqual(self.row()['annual'][0]['period'],'20261231')
        self.assertEqual(self.row()['cash_decision']['stage'],'unresolved')
        self.assertFalse(self.row()['gate'])
        self.assertEqual(self.assessment()['scenarios'],[])

    def test_known_cancellation_is_not_ignored_after_implementation_announcement(self):
        self.dossier['data']['dividend'].append({'ts_code':self.dossier['symbol'],'end_date':'20251231',
            'ann_date':'20261121','div_proc':'取消','cash_div_tax':'0'})
        self.dossier['as_of']='20261121'
        self.assertFalse(self.row()['gate'])
        self.assertEqual(self.assessment()['scenarios'],[])

    def test_matching_approved_raw_row_is_allowed_without_future_implementation_dates(self):
        self.dossier['data']['dividend'].remove(self.latest)
        self.dossier['data']['dividend'].append({'ts_code':self.dossier['symbol'],'end_date':'20251231',
            'ann_date':'20261105','div_proc':'股东大会通过','cash_div_tax':'.6','stk_div':0,
            'record_date':None,'ex_date':None,'pay_date':None,'imp_ann_date':None})
        self.assertTrue(self.row()['gate'])
        self.assertEqual(self.assessment()['cash_decision']['stage'],'approved')

    def test_partial_or_changed_annual_implementation_does_not_discard_approved_commitment(self):
        self.latest['cash_div_tax']='.1'
        self.dossier['as_of']='20261120'
        self.assertFalse(self.row()['gate'])
        self.assertEqual(self.assessment()['scenarios'],[])
        self.assertTrue(any('已批准全年现金' in issue for issue in self.row()['issues']))

    def test_positive_interim_alone_is_not_a_complete_latest_fiscal_year(self):
        self.dossier['data']['dividend'].append({**self.latest,'end_date':'20250630','ann_date':'20250801',
            'imp_ann_date':'20250802','record_date':'20250803','ex_date':'20250804','pay_date':'20250805','cash_div_tax':'.1'})
        with patch.dict(DISCLOSURES,{'annual_cash_decisions':[]}):
            self.assertTrue(self.row('implemented_only')['gate'])
            self.assertFalse(self.row()['gate'])
            self.assertEqual(self.row()['cash_decision']['stage'],'unresolved')
            self.assertEqual(self.assessment()['scenarios'],[])


if __name__ == '__main__':
    unittest.main()
