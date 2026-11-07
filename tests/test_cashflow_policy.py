import copy
import hashlib
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.automatic import assess, dividend_assessment
from qmt.config import AppError
from qmt.research import DISCLOSURES, summarize
from qmt.selection import load_rules
from test_automatic import fixture


POLICY = 'three_year_coverage'
DEFAULT = 'latest_and_three_year'


class CashflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.original_row, self.dossier = fixture()
        self.rules = load_rules()

    def cashflow(self, value, period='20251231'):
        raw = next(r for r in self.dossier['data']['cashflow'] if r['end_date'] == period)
        raw['n_cashflow_act'] = None if value is None else 300 + value

    def row(self, policy=POLICY, dividend_policy='implemented_only'):
        row = summarize(self.dossier, {'name': '虚构测试公司', 'industry': '测试行业'},
            {'trade_date': self.dossier['as_of'], 'close': '8', 'total_share': '.1', 'dv_ttm': 7.5},
            cashflow_policy=policy, dividend_policy=dividend_policy)
        row['selection'] = copy.deepcopy(self.original_row['selection'])
        return row

    def assessment(self, policy=POLICY, dividend_policy='implemented_only'):
        return assess(self.row(policy, dividend_policy), self.dossier, self.rules,
                      cashflow_policy=policy, dividend_policy=dividend_policy)

    def test_negative_and_zero_latest_fcf_can_use_signed_three_year_coverage(self):
        for latest in (-100, 0):
            with self.subTest(latest=latest):
                self.cashflow(latest)
                before = copy.deepcopy(self.dossier)
                new, old = self.row(), self.row(DEFAULT)
                self.assertTrue(new['gate'])
                self.assertEqual(new['annual'], old['annual'])
                self.assertEqual(new['cashflow_warning']['simple_fcf'], str(latest))
                self.assertNotEqual(new['evidence_hash'], old['evidence_hash'])
                result = self.assessment()
                self.assertEqual(result['state'], '满足价格条件')
                self.assertEqual(result['latest_fcf'], str(latest))
                self.assertEqual(result['three_year_fcf'], str(3400 + latest))
                self.assertEqual(Decimal(result['three_year_dividend_cash']), Decimal(1800))
                self.assertEqual(Decimal(result['fcf_coverage']), Decimal(3400 + latest) / 1800)
                self.assertEqual(result['cashflow_warning'], new['cashflow_warning'])
                self.assertEqual(self.assessment(DEFAULT)['state'], '排除')
                self.assertEqual(self.dossier, before)

    def test_signed_three_year_shortfall_still_blocks(self):
        self.cashflow(-1700)
        result = self.assessment()
        self.assertEqual(result['state'], '待数据核实')
        self.assertIn('三年自由现金流合计不足', result['reason'])
        self.assertEqual(result['three_year_fcf'], '1700')
        self.assertEqual(Decimal(result['three_year_dividend_cash']), Decimal(1800))
        self.assertEqual(result['scenarios'], [])

    def test_missing_latest_or_old_cash_fields_never_become_zero(self):
        saved = copy.deepcopy(self.dossier)
        for period in ('20251231', '20241231', '20211231'):
            with self.subTest(period=period):
                self.dossier = copy.deepcopy(saved)
                self.cashflow(-100)
                self.cashflow(None, period)
                self.assertFalse(self.row()['gate'])
                result = self.assessment()
                self.assertEqual(result['state'], '待数据核实')
                self.assertEqual(result['scenarios'], [])
                if period == '20251231':
                    self.assertNotIn('latest_fcf', result)
                    self.assertNotIn('cashflow_warning', result)
                if period != '20211231':
                    self.assertNotIn('three_year_fcf', result)

    def test_latest_debt_checks_remain_in_force(self):
        self.cashflow(-100)
        latest = self.dossier['data']['balancesheet'][-1]
        latest['st_borr'] = 100000
        self.assertIn('短期到期债务超过', self.assessment()['reason'])
        latest['total_liab'] = 8000
        self.assertEqual(self.assessment()['state'], '排除')
        latest['total_liab'] = 1000
        latest['st_borr'] = None
        self.assertEqual(self.assessment()['state'], '待数据核实')

    def test_nonstandard_audit_and_nonpositive_profit_still_block(self):
        self.cashflow(-100)
        audit = self.dossier['data']['fina_audit'][0]
        audit['audit_result'] = '保留意见'
        self.assertIn('审计意见', self.assessment()['reason'])
        audit['audit_result'] = '标准无保留意见'
        self.dossier['data']['income'][-1]['n_income_attr_p'] = -1
        self.assertEqual(self.assessment()['state'], '排除')

    def test_approved_total_including_interim_and_special_remains_denominator(self):
        data = self.dossier['data']
        for row in data['dividend']:
            row['ts_code'] = self.dossier['symbol']
        data['balancesheet'].append({**data['balancesheet'][0], 'end_date': '20201231', 'ann_date': '20210401'})
        data['dividend'].append({**data['dividend'][0], 'end_date': '20201231', 'ann_date': '20210401',
            'imp_ann_date': '20210501', 'record_date': '20210510', 'ex_date': '20210511', 'pay_date': '20210512'})
        latest = next(r for r in data['dividend'] if r['end_date'] == '20251231')
        latest.update(cash_div_tax='.7', imp_ann_date='20261120', record_date='20261121', ex_date='20261122', pay_date='20261123')
        interim = {**latest, 'end_date': '20250630', 'ann_date': '20250801', 'imp_ann_date': '20250802',
            'record_date': '20250803', 'ex_date': '20250804', 'pay_date': '20250805', 'cash_div_tax': '.1',
            'ordinary_cash_div_tax': '0', 'special_cash_div_tax': '.1'}
        data['dividend'].append(interim)
        proof = {'symbol': self.dossier['symbol'], 'period': '20251231', 'available_on': '20261105',
            'source': 'https://example.com/approved.pdf', 'source_sha256': 'a' * 64, 'pages': '2',
            'cash_dps': '.7', 'total_cash': '700', 'share_count': '1000', 'share_basis': 'source_exact_base',
            'classification': 'ordinary_proxy_unverified', 'known_interim_rows': [copy.deepcopy(interim)],
            'share_witnesses': [], 'annual_scope': 'reported_annual_total', 'annual_total_cash': '800',
            'supporting_sources': [{'source': 'https://example.com/annual.pdf', 'source_sha256': 'b' * 64,
                                    'pages': '20', 'available_on': '20260401'}], 'remaining_gaps': []}
        with patch.dict(DISCLOSURES, {'annual_cash_decisions': [proof]}):
            self.cashflow(-100)
            result = self.assessment(dividend_policy='approved_annual_cash')
            self.assertEqual(result['state'], '满足价格条件')
            self.assertEqual(result['cash_decision']['stage'], 'approved')
            self.assertEqual(Decimal(result['three_year_dividend_cash']), Decimal(2000))
            self.assertEqual(Decimal(result['fcf_coverage']), Decimal(3300) / 2000)
            self.cashflow(-1500)
            result = self.assessment(dividend_policy='approved_annual_cash')
            self.assertEqual(result['state'], '待数据核实')
            self.assertEqual(result['three_year_fcf'], '1900')
            self.assertIn('三年自由现金流合计不足', result['reason'])

    def test_policy_tags_warning_and_raw_cashflow_cannot_be_bypassed(self):
        self.cashflow(-100)
        for row, policy in ((self.row(DEFAULT), POLICY), (self.row(), DEFAULT)):
            result = assess(row, self.dossier, self.rules, cashflow_policy=policy)
            self.assertEqual(result['state'], '待数据核实')
            self.assertIn('口径不一致', result['reason'])
        row = self.row()
        row['cashflow_warning']['simple_fcf'] = '0'
        self.assertIn('警告与摘要不一致', assess(row, self.dossier, self.rules, cashflow_policy=POLICY)['reason'])
        row = self.row()
        row['annual'][0]['simple_fcf'] = '1700'
        row.pop('cashflow_warning')
        self.assertIn('原始现金流量表不一致', assess(row, self.dossier, self.rules, cashflow_policy=POLICY)['reason'])
        for policy in (None, '', 'unknown'):
            with self.assertRaises(AppError): self.row(policy)
            with self.assertRaises(AppError): assess(self.original_row, self.dossier, self.rules, cashflow_policy=policy)
            with self.assertRaises(AppError): dividend_assessment(self.original_row, self.dossier, self.rules, cashflow_policy=policy)

    def test_default_complete_objects_match_prechange_fixtures(self):
        digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':')).encode()).hexdigest()
        for latest, expected in ((1700, (
                '51a23d654f5aa5c3e1c112d9e98ad82beb650e2629f52f3c2e56ec10a05a9ac2',
                'f4138e9a287127406fb17b21e2948e1f490ce37ec19951baba6990102fee623d')),
                (-100, ('540705e7359d5f74679a36115b0c807d5ca5b96432705eaf5ead86f531dd23aa',
                        'b86aeb055305d70f82d5addd0795dc84c32293733f9511a317b302fe665cc03a'))):
            self.cashflow(latest)
            row = self.row(DEFAULT)
            result = assess(row, self.dossier, self.rules)
            self.assertEqual((digest(row), digest(result)), expected)
            self.assertEqual(result, self.assessment(DEFAULT))
            self.assertNotIn('cashflow_policy', row)
            self.assertNotIn('cashflow_policy', result)


if __name__ == '__main__':
    unittest.main()
