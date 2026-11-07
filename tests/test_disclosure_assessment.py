import copy
import unittest
from unittest.mock import patch

from qmt.automatic import assess, load_rules
from qmt.experiment import entry_diagnostics
from qmt.research import DISCLOSURES, summarize, verified_zero_cash_years
from test_automatic import fixture
from test_verified_disclosures import source, zero_item


class DisclosureAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.item = zero_item()
        self.rules = load_rules()
        self.registry = patch.dict(DISCLOSURES, {'financial_zero_fields': [], 'zero_cash_years': [self.item]})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def summarize(self, dossier):
        row = summarize(dossier, {'name': '测试公司', 'industry': '测试行业'},
                        {'trade_date': dossier['as_of'], 'close': '8', 'total_share': '.1', 'dv_ttm': 7.5})
        row['selection'] = {'coarse_pass': True, 'valuation': {'state': '通过', 'reason': '测试估值'},
                            'liquidity': {'state': '通过', 'reason': '测试成交额'}}
        return row

    def zero_dossier(self, day='20260628'):
        _, dossier = fixture(day=day)
        dossier['data']['dividend'] = [r for r in dossier['data']['dividend']
                                       if r['end_date'] != '20251231'] + copy.deepcopy(self.item['expected'])
        return dossier

    def test_approved_date_changes_unknown_to_known_rule_failure_with_fresh_proof(self):
        dossier = self.zero_dossier('20260627')
        before = assess(self.summarize(dossier), dossier, self.rules)
        self.assertEqual(before['state'], '待数据核实')
        self.assertNotIn('zero_cash_decisions', before)
        dossier['as_of'] = '20260628'
        row = self.summarize(dossier)
        for basis in ('conservative_minimum', 'ttm'):
            with self.subTest(basis=basis):
                result = assess(row, dossier, self.rules, earnings_basis=basis)
                self.assertEqual(result['state'], '排除')
                self.assertEqual(result['zero_cash_periods'], ['20251231'])
                self.assertEqual(result['zero_cash_decisions'],
                                 [verified_zero_cash_years(dossier['data']['dividend'], row['symbol'], row['as_of'])['20251231']])
                self.assertIn('不满足本策略连续现金分红规则', result['reason'])
                self.assertIn('不代表公司无支付能力', result['reason'])
                self.assertEqual(result['dimensions']['dividend']['state'], '排除')

    def test_stale_summary_cannot_bypass_mutated_raw_or_use_future_approval(self):
        dossier = self.zero_dossier()
        row = self.summarize(dossier)
        dossier['data']['dividend'][-1]['cash_div_tax'] = 0.1
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '待数据核实')
        self.assertNotIn('zero_cash_decisions', result)
        dossier = self.zero_dossier()
        row = self.summarize(dossier)
        row['as_of'] = dossier['as_of'] = '20260627'
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '待数据核实')
        self.assertNotIn('zero_cash_periods', result)

    def test_forged_summary_proof_does_not_create_registered_zero_decision(self):
        with patch.dict(DISCLOSURES, {'zero_cash_years': []}):
            row, dossier = fixture()
            row['annual'][0]['zero_cash_proof'] = {**self.item, 'originals': self.item['expected']}
            result = assess(row, dossier, self.rules)
            self.assertEqual(result['state'], '满足价格条件')
            self.assertNotIn('zero_cash_decisions', result)
            row['annual'][0]['implemented_dps'] = '0'
            result = assess(row, dossier, self.rules)
            self.assertEqual(result['state'], '待数据核实')
            self.assertNotIn('zero_cash_decisions', result)

    def test_verified_zero_outside_latest_five_years_does_not_trigger_exclusion(self):
        self.item.update(period='20211231', available_on='20220628')
        for raw in self.item['expected']:
            for key in ('end_date', 'ann_date'):
                raw[key] = str(int(raw[key][:4]) - 4) + raw[key][4:]
        _, dossier = fixture()
        dossier['data']['dividend'] = [r for r in dossier['data']['dividend']
                                       if r['end_date'] != '20211231'] + copy.deepcopy(self.item['expected'])
        initial = assess(self.summarize(dossier), dossier, self.rules)
        self.assertEqual(initial['state'], '排除')
        self.assertEqual(initial['zero_cash_periods'], ['20211231'])
        # The old zero year leaves the window only after a newer raw annual report is disclosed.
        for api in ('income', 'cashflow', 'balancesheet', 'fina_indicator', 'fina_audit'):
            template = next(r for r in dossier['data'][api] if r['end_date'] == '20251231')
            dossier['data'][api].append({**template, 'end_date': '20261231', 'ann_date': '20270401'})
        payment = copy.deepcopy(next(r for r in dossier['data']['dividend'] if r['end_date'] == '20251231'))
        for key in ('end_date', 'ann_date', 'imp_ann_date', 'record_date', 'ex_date', 'pay_date'):
            payment[key] = str(int(payment[key][:4]) + 1) + payment[key][4:]
        dossier['data']['dividend'].append(payment)
        self.assertEqual(assess(self.summarize(dossier), dossier, self.rules)['zero_cash_periods'], ['20211231'])
        dossier['as_of'] = '20270512'
        row = self.summarize(dossier)
        self.assertEqual([a['period'] for a in row['annual'][:5]],
                         ['20261231', '20251231', '20241231', '20231231', '20221231'])
        self.assertIn('zero_cash_proof', row['annual'][5])
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '满足价格条件')
        self.assertNotIn('zero_cash_decisions', result)

    def test_forged_older_five_year_summary_cannot_hide_current_raw_zero_year(self):
        dossier = self.zero_dossier('20261105')
        for api in ('income', 'cashflow', 'balancesheet', 'fina_indicator', 'fina_audit'):
            template = next(r for r in dossier['data'][api] if r['end_date'] == '20211231')
            dossier['data'][api].append({**template, 'end_date': '20201231', 'ann_date': '20210401'})
        payment = copy.deepcopy(next(r for r in dossier['data']['dividend'] if r['end_date'] == '20211231'))
        for key in ('end_date', 'ann_date', 'imp_ann_date', 'record_date', 'ex_date', 'pay_date'):
            payment[key] = str(int(payment[key][:4]) - 1) + payment[key][4:]
        dossier['data']['dividend'].append(payment)
        row = self.summarize(dossier)
        self.assertEqual(assess(row, dossier, self.rules)['state'], '排除')
        row['annual'] = row['annual'][1:]
        row.update(gate=True, issues=[])
        self.assertEqual([a['period'] for a in row['annual']],
                         ['20241231', '20231231', '20221231', '20211231', '20201231'])
        original = copy.deepcopy(dossier)
        for basis in ('conservative_minimum', 'ttm'):
            with self.subTest(basis=basis):
                result = assess(row, dossier, self.rules, earnings_basis=basis)
                self.assertEqual(result['state'], '排除')
                self.assertEqual(result['zero_cash_periods'], ['20251231'])
                self.assertEqual(result['zero_cash_decisions'][0]['period'], '20251231')
        self.assertEqual(dossier, original)

    def test_missing_short_term_borrowing_identifies_raw_field_and_period(self):
        _, dossier = fixture(symbol='600002.SH')
        dossier['data']['balancesheet'][-1]['st_borr'] = None
        row = self.summarize(dossier)
        self.assertTrue(row['gate'])
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '待数据核实')
        self.assertEqual(result['financial_gaps'], [{'api': 'balancesheet', 'period': '20260930',
                         'field': 'st_borr', 'label': '短期借款', 'available_on': '20261030'}])
        self.assertIn('短期借款（20260930）缺失，不能按零计算', result['reason'])
        self.assertIsNone(dossier['data']['balancesheet'][-1]['st_borr'])

    def test_registered_debt_fields_are_rechecked_even_with_a_complete_summary(self):
        _, dossier = fixture(symbol='600002.SH')
        latest = dossier['data']['balancesheet'][-1]
        latest.update(st_borr=None, non_cur_liab_due_1y=None)
        entries = [{**source('20261030'), 'expected': copy.deepcopy(latest), 'field': field, 'value': '0'}
                   for field in ('st_borr', 'non_cur_liab_due_1y')]
        DISCLOSURES['financial_zero_fields'] = entries
        original = copy.deepcopy(dossier)
        row = self.summarize(dossier)
        self.assertEqual(assess(row, dossier, self.rules)['state'], '满足价格条件')
        self.assertEqual(dossier, original)
        DISCLOSURES['financial_zero_fields'] = entries[:1]
        missing = assess(row, dossier, self.rules)
        self.assertEqual(missing['state'], '待数据核实')
        self.assertEqual([gap['field'] for gap in missing['financial_gaps']], ['non_cur_liab_due_1y'])
        DISCLOSURES['financial_zero_fields'] = entries
        latest['money_cap'] += 1
        self.assertEqual(assess(row, dossier, self.rules)['state'], '待数据核实')

    def test_verified_debt_zero_does_not_clear_an_unregistered_cash_gap(self):
        _, dossier = fixture(symbol='600002.SH')
        latest = dossier['data']['balancesheet'][-1]
        latest.update(st_borr=None, non_cur_liab_due_1y=None, money_cap=None)
        DISCLOSURES['financial_zero_fields'] = [
            {**source('20261030'), 'expected': copy.deepcopy(latest), 'field': field, 'value': '0'}
            for field in ('st_borr', 'non_cur_liab_due_1y')]
        row = self.summarize(dossier)
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '待数据核实')
        self.assertEqual([gap['field'] for gap in result['financial_gaps']], ['money_cap'])
        self.assertTrue(all(latest[field] is None for field in ('st_borr', 'non_cur_liab_due_1y', 'money_cap')))

    def test_balance_gaps_remain_visible_when_fresh_summary_gate_is_false(self):
        labels = {'total_assets': '总资产', 'total_liab': '总负债', 'money_cap': '货币资金',
                  'st_borr': '短期借款', 'non_cur_liab_due_1y': '一年内到期非流动负债'}
        for fields in (['total_assets'], ['total_liab'], ['money_cap'], list(labels)):
            with self.subTest(fields=fields):
                _, dossier = fixture(symbol='600002.SH')
                for field in fields:
                    dossier['data']['balancesheet'][-1][field] = None
                row = self.summarize(dossier)
                self.assertFalse(row['gate'])
                result = assess(row, dossier, self.rules)
                self.assertEqual(result['state'], '待数据核实')
                self.assertEqual({r['field'] for r in result['financial_gaps']}, set(fields))
                for gap in result['financial_gaps']:
                    self.assertEqual(gap, {'api': 'balancesheet', 'period': '20260930', 'field': gap['field'],
                                         'label': labels[gap['field']], 'available_on': '20261030'})
                self.assertTrue(all(dossier['data']['balancesheet'][-1][field] is None for field in fields))

    def test_known_high_leverage_still_excludes_when_short_term_borrowing_is_missing(self):
        _, dossier = fixture(symbol='600002.SH')
        dossier['data']['balancesheet'][-1].update(total_assets=10000, total_liab=9000, st_borr=None)
        row = self.summarize(dossier)
        self.assertTrue(row['gate'])
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '排除')
        self.assertEqual(result['reason'], '资产负债率超过实验上限')

    def test_unknown_assets_cannot_be_used_to_infer_high_leverage_exclusion(self):
        _, dossier = fixture(symbol='600002.SH')
        dossier['data']['balancesheet'][-1].update(total_assets=None, total_liab=9000)
        row = self.summarize(dossier)
        self.assertFalse(row['gate'])
        result = assess(row, dossier, self.rules)
        self.assertEqual(result['state'], '待数据核实')
        self.assertEqual(result['financial_gaps'], [{'api': 'balancesheet', 'period': '20260930',
                         'field': 'total_assets', 'label': '总资产', 'available_on': '20261030'}])

    def test_diagnostics_groups_repeated_months_by_company_period_and_field(self):
        dossier = self.zero_dossier()
        zero = assess(self.summarize(dossier), dossier, self.rules)
        _, gap_dossier = fixture(symbol='600002.SH')
        gap_dossier['data']['balancesheet'][-1]['st_borr'] = None
        missing = assess(self.summarize(gap_dossier), gap_dossier, self.rules)
        other_company = {**missing, 'symbol': '600003.SH'}
        other_period = {**missing, 'financial_gaps': [{**missing['financial_gaps'][0],
                                                     'period': '20260630', 'available_on': '20260830'}]}
        audits = [{'day': '20261030', 'assessments': [zero, missing]},
                  {'day': '20261105', 'assessments': [zero, missing, other_company, other_period]}]
        original = copy.deepcopy(audits)
        result = entry_diagnostics(audits, {})
        self.assertEqual(result['verified_zero_cash'], [{'symbol': '600001.SH', 'period': '20251231',
                         'available_on': '20260628', 'source': self.item['source'], 'note': self.item['note'], 'count': 2}])
        self.assertEqual([(r['symbol'], r['period'], r['field'], r['count']) for r in result['financial_gaps']],
                         [('600002.SH', '20260630', 'st_borr', 1), ('600002.SH', '20260930', 'st_borr', 2),
                          ('600003.SH', '20260930', 'st_borr', 1)])
        self.assertEqual(audits, original)

    def test_legacy_diagnostics_omits_optional_disclosure_sections(self):
        legacy = {'state': '待数据核实', 'dimensions': {}}
        result = entry_diagnostics([{'assessments': [legacy]}], {'20261105': {'candidates': []}})
        self.assertEqual(set(result), {'entry_funnel', 'candidate_months'})
        self.assertEqual(result['candidate_months'], {'months': 1, 'empty': 1, 'total_candidates': 0})
        self.assertEqual([r['count'] for r in result['entry_funnel']], [1, 0, 0, 0, 0, 0])


if __name__ == '__main__':
    unittest.main()
