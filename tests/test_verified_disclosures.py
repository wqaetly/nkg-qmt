import copy
import unittest
from unittest.mock import patch

from qmt.config import AppError
from qmt.research import DISCLOSURES, implemented_dividends, statements, summarize, verified_zero_cash_years
from test_automatic import fixture


def source(day):
    return {'available_on': day, 'source': 'https://example.com/disclosure.pdf',
            'source_sha256': 'a' * 64, 'pages': '1—2', 'note': '测试原文登记'}


def zero_item():
    def row(period, day, stage, cash):
        return {'ts_code': '600001.SH', 'end_date': period, 'ann_date': day, 'div_proc': stage,
                'cash_div_tax': cash, 'stk_div': 0.0, 'record_date': None, 'ex_date': None,
                'pay_date': None, 'imp_ann_date': None}
    return {**source('20260628'), 'symbol': '600001.SH', 'period': '20251231', 'cash_dps': '0',
            'expected': [row('20251231', '20260429', '预案', 0.0),
                         row('20251231', '20260628', '股东大会通过', None),
                         row('20250630', '20250824', '预案', 0.0),
                         row('20250630', '20250824', '股东大会通过', None)]}


def financial_item():
    return {**source('20230831'), 'field': 'st_borr', 'value': '0',
            'expected': {'ts_code': '601225.SH', 'end_date': '20230630', 'ann_date': '20230831',
                         'f_ann_date': '20230831', 'report_type': '1', 'comp_type': '1',
                         'total_assets': 10000.0, 'total_liab': 1000.0, 'money_cap': 3000.0,
                         'st_borr': None, 'lt_borr': 100.0}}


class VerifiedFinancialZeroTests(unittest.TestCase):
    def setUp(self):
        self.item = financial_item()
        self.rows = [copy.deepcopy(self.item['expected'])]
        self.registry = patch.dict(DISCLOSURES, {'financial_zero_fields': [self.item], 'zero_cash_years': []})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_exact_row_duplicate_raw_and_available_date(self):
        self.rows *= 2
        before = copy.deepcopy(self.rows)
        self.assertEqual(statements(self.rows, '20230830'), {})
        corrected = statements(self.rows, '20230831')['20230630']
        self.assertEqual(corrected['st_borr'], '0')
        self.assertEqual(corrected['verified_zero_fields'][0]['original'], before[0])
        self.assertEqual(corrected['verified_zero_fields'][0]['source_sha256'], 'a' * 64)
        self.assertEqual(self.rows, before)
        corrected['verified_zero_fields'][0]['original']['st_borr'] = 123
        self.assertIsNone(self.item['expected']['st_borr'])

    def test_unregistered_none_other_period_symbol_and_later_version_stay_missing(self):
        for changes in ({'ts_code': '600002.SH'}, {'end_date': '20230331'},
                        {'ann_date': '20230901', 'f_ann_date': '20230901'}):
            with self.subTest(changes=changes):
                row = {**self.rows[0], **changes}
                result = statements([row], '20230902')[row['end_date']]
                self.assertIsNone(result['st_borr'])
                self.assertNotIn('verified_zero_fields', result)
        later = {**self.rows[0], 'ann_date': '20230901', 'f_ann_date': '20230901'}
        self.assertIsNone(statements(self.rows + [later], '20230901')['20230630']['st_borr'])
        # An income row with the same identity is not a balance-sheet correction.
        income = {k: v for k, v in self.rows[0].items()
                  if k in ('ts_code', 'end_date', 'ann_date', 'f_ann_date', 'report_type')}
        self.assertEqual(statements([income], '20230831')['20230630'], income)

    def test_correction_is_not_effective_before_its_source(self):
        self.item['available_on'] = '20230901'
        earlier = statements(self.rows, '20230831')['20230630']
        self.assertIsNone(earlier['st_borr'])
        self.assertNotIn('verified_zero_fields', earlier)

    def test_changed_original_including_extra_field_or_type_fails(self):
        for changes in ({'money_cap': 3001.0}, {'st_borr': 0.0}, {'extra': None}, {'lt_borr': 100}):
            with self.subTest(changes=changes), self.assertRaises(AppError):
                statements([{**self.rows[0], **changes}], '20230831')

    def test_duplicate_registration_fails_but_future_registration_is_inert(self):
        duplicate = copy.deepcopy(self.item)
        DISCLOSURES['financial_zero_fields'].append(duplicate)
        with self.assertRaises(AppError):
            statements(self.rows, '20230831')
        duplicate['available_on'] = '20230901'
        self.assertEqual(statements(self.rows, '20230831')['20230630']['st_borr'], '0')

    def test_invalid_registration_source_and_fields(self):
        for changes in ({'source': 'http://example.com/a'}, {'source': 'https://['}, {'source_sha256': 'broken'}, {'pages': ''},
                        {'available_on': '20230830'}, {'field': 'lt_borr'}, {'value': 0}):
            with self.subTest(changes=changes), patch.dict(self.item, changes), self.assertRaises(AppError):
                statements(self.rows, '20230901')
        with patch.dict(self.item['expected'], {'st_borr': 0}), self.assertRaises(AppError):
            statements(self.rows, '20230901')

    def test_reserved_proof_in_raw_is_rejected_only_after_disclosure(self):
        future = {**self.rows[0], 'ann_date': '20230901', 'f_ann_date': '20230901',
                  'verified_zero_fields': [{'value': '0'}]}
        self.assertEqual(statements(self.rows + [future], '20230831')['20230630']['st_borr'], '0')
        with self.assertRaises(AppError):
            statements(self.rows + [future], '20230901')

    def test_financial_provenance_changes_the_evidence_hash(self):
        _, dossier = fixture(day='20261105')
        latest = dossier['data']['balancesheet'][-1]
        latest['st_borr'] = None
        self.item.update({**source('20261030'), 'expected': copy.deepcopy(latest)})
        def summary():
            return summarize(dossier, {'name': '测试公司'},
                             {'trade_date': '20261105', 'close': '8', 'total_share': '.1'})
        first = summary()
        self.assertEqual(first['balance']['st_borr'], '0')
        self.item['source_sha256'] = 'b' * 64
        self.assertNotEqual(first['evidence_hash'], summary()['evidence_hash'])
        self.assertIsNone(latest['st_borr'])


class VerifiedZeroCashYearTests(unittest.TestCase):
    def setUp(self):
        self.item = zero_item()
        self.rows = copy.deepcopy(self.item['expected'])
        self.registry = patch.dict(DISCLOSURES, {'financial_zero_fields': [], 'zero_cash_years': [self.item]})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def proof(self, rows=None, day='20260628'):
        return verified_zero_cash_years(self.rows if rows is None else rows, '600001.SH', day)

    def test_explicit_final_source_date_not_proposal_or_stage_label(self):
        original = copy.deepcopy(self.rows)
        self.assertEqual(self.proof(day='20260627'), {})
        result = self.proof()['20251231']
        self.assertEqual(result['cash_dps'], '0')
        self.assertEqual(result['available_on'], '20260628')
        self.assertEqual(len(result['originals']), 4)
        self.assertEqual(implemented_dividends(self.rows, '20260628'), [])
        self.assertEqual(self.rows, original)
        result['originals'][0]['cash_div_tax'] = 1
        self.assertEqual(self.rows, original)

    def test_future_positive_plan_and_implementation_do_not_leak(self):
        original_proof = self.proof()
        for stage, ann, imp in [('预案', '20260701', None), ('实施', '20260501', '20260701')]:
            future = {**self.rows[0], 'div_proc': stage, 'ann_date': ann, 'imp_ann_date': imp,
                      'cash_div_tax': 1.0}
            with self.subTest(stage=stage):
                self.assertEqual(self.proof(self.rows + [future]), original_proof)
                with self.assertRaises(AppError):
                    self.proof(self.rows + [future], '20260701')

    def test_already_announced_implementation_without_imp_date_is_unknown_not_future(self):
        event = {**self.rows[0], 'div_proc': '实施', 'ann_date': '20260620', 'imp_ann_date': None,
                 'record_date': '20260625', 'ex_date': '20260626', 'pay_date': '20260626', 'cash_div_tax': 0.2}
        with self.assertRaisesRegex(AppError, '必要公告日期'):
            self.proof(self.rows + [event])
        event['imp_ann_date'] = '20260701'
        self.assertEqual(self.proof(self.rows + [event]), self.proof())

    def test_missing_duplicate_changed_rows_fail(self):
        for rows in (self.rows[:-1], self.rows + [self.rows[0]],
                     [{**self.rows[0], 'extra': None}] + self.rows[1:],
                     [{**self.rows[0], 'cash_div_tax': 0}] + self.rows[1:]):
            with self.subTest(rows=rows), self.assertRaises(AppError):
                self.proof(rows)

    def test_positive_interim_even_if_registered_cannot_be_zero(self):
        self.item['expected'][2]['cash_div_tax'] = 0.2
        self.rows = copy.deepcopy(self.item['expected'])
        with self.assertRaises(AppError):
            self.proof()

    def test_other_fiscal_year_and_other_stock_do_not_change_proof(self):
        additions = [{**self.rows[0], 'ts_code': '600002.SH', 'cash_div_tax': 1},
                     {**self.rows[0], 'end_date': '20241231', 'cash_div_tax': 1}]
        self.assertEqual(self.proof(), self.proof(self.rows + additions))

    def test_invalid_source_period_cash_and_duplicate_registration(self):
        for changes in ({'source': ''}, {'source_sha256': 'a' * 63}, {'pages': None},
                        {'period': '20250630'}, {'period': '20261231'}, {'cash_dps': '1'},
                        {'expected': []}, {'expected': [None]}, {'available_on': '20261301'},
                        {'supporting_sources': [source('20260629')]}, {'supporting_sources': [None]}):
            with self.subTest(changes=changes), patch.dict(self.item, changes), self.assertRaises(AppError):
                self.proof()
        DISCLOSURES['zero_cash_years'].append(copy.deepcopy(self.item))
        with self.assertRaises(AppError):
            self.proof()

    def test_unknown_zero_or_pure_stock_is_not_a_whole_year_proof(self):
        with patch.dict(DISCLOSURES, {'zero_cash_years': []}):
            self.assertEqual(self.proof(), {})
            stock = {**self.rows[0], 'stk_div': 0.3, 'cash_div_tax': 0}
            self.assertEqual(self.proof([stock]), {})

    def test_summarize_zero_year_has_proof_not_a_payment_or_missing_year(self):
        _, dossier = fixture(day='20260628')
        dossier['data']['dividend'] = [r for r in dossier['data']['dividend'] if r['end_date'] != '20251231'] + self.rows
        def summary(day):
            dossier['as_of'] = day
            return summarize(dossier, {'name': '测试公司', 'industry': '测试'},
                             {'trade_date': day, 'close': '8', 'total_share': '.1', 'dv_ttm': 7.5})
        earlier = summary('20260627')
        result = summary('20260628')
        self.assertIsNone(earlier['annual'][0]['implemented_dps'])
        annual = result['annual'][0]
        for field in ('implemented_dps', 'ordinary_dps', 'estimated_dividend_cash', 'estimated_ordinary_cash'):
            self.assertEqual(annual[field], '0')
        self.assertEqual(annual['zero_cash_proof'], self.proof()['20251231'])
        self.assertFalse(any(r['end_date'] == '20251231' for r in result['events']))
        self.assertTrue(any('原文核实的全年零现金' in s for s in result['issues']))
        self.assertFalse(any('未取得已实施分红' in s for s in result['issues']))
        self.assertFalse(result['gate'])
        self.assertNotEqual(earlier['evidence_hash'], result['evidence_hash'])
        self.item['source_sha256'] = 'b' * 64
        self.assertNotEqual(result['evidence_hash'], summary('20260628')['evidence_hash'])
        self.assertEqual(earlier['evidence_hash'], summary('20260627')['evidence_hash'])
        with patch.dict(DISCLOSURES, {'zero_cash_years': []}):
            self.assertEqual(earlier['evidence_hash'], summary('20260628')['evidence_hash'])

    def test_summarize_conflict_keeps_year_unknown(self):
        _, dossier = fixture(day='20260628')
        dossier['data']['dividend'] = [r for r in dossier['data']['dividend'] if r['end_date'] != '20251231'] + self.rows[:-1]
        result = summarize(dossier, {'name': '测试公司'}, {'trade_date': '20260628', 'close': '8', 'total_share': '.1'})
        self.assertIsNone(result['annual'][0]['implemented_dps'])
        self.assertNotIn('zero_cash_proof', result['annual'][0])
        self.assertTrue(any('全年零现金分红核实' in s for s in result['issues']))


if __name__ == '__main__':
    unittest.main()
