import copy
import itertools
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.automatic import assess, load_rules
from qmt.config import AppError
from qmt.research import DISCLOSURES, annual_dividend_cash, implemented_dividends, summarize
from test_automatic import fixture


def correction():
    return next(c for c in DISCLOSURES['cash_joint_periods'] if c['expected'][0]['ts_code'] == '600720.SH')


def event(period='20221231', cash='0.2', **changes):
    return {'ts_code': '600720.SH', 'end_date': period, 'ann_date': '20230301',
            'imp_ann_date': '20230310', 'div_proc': '实施', 'cash_div_tax': cash, 'stk_div': 0,
            'record_date': '20230320', 'ex_date': '20230321', 'pay_date': '20230321', **changes}


class DividendCashTests(unittest.TestCase):
    def test_joint_fiscal_cash_uses_verified_basis_once_and_is_order_independent(self):
        source = correction()
        balances = {'20221231': {'total_share': 776290282, 'ann_date': '20230322'},
                    '20231231': {'total_share': 2061708481, 'ann_date': '20240327'}}
        original = copy.deepcopy(source)
        results = []
        for order in itertools.permutations(source['expected']):
            events = implemented_dividends(order, '20240604')
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['cash_div_tax'], '0.3674')
            self.assertEqual(events[0]['correction_source_sha256'], source['source_sha256'])
            self.assertEqual(events[0]['correction_component_sources'], source['component_sources'])
            annual = annual_dividend_cash(events, balances)
            results.append(annual)
            self.assertEqual(annual['20221231']['estimated_dividend_cash'], '227612616.3024')
            self.assertEqual(annual['20231231']['estimated_dividend_cash'], '529859079.617')
            for period in balances:
                self.assertEqual(annual[period]['estimated_ordinary_cash'], annual[period]['estimated_dividend_cash'])
                self.assertEqual(len(annual[period]['dividend_cash_basis']), 1)
                basis = annual[period]['dividend_cash_basis'][0]
                self.assertEqual(basis['basis'], 'verified_distribution_shares')
                self.assertEqual(basis['shares'], '2061708481')
                self.assertEqual(basis['available_on'], '20240604')
                self.assertIsNone(basis['share_period'])
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(source, original)

    def test_mixed_year_keeps_every_proxy_payment_and_missing_basis_invalidates_whole_total(self):
        rows = correction()['expected'] + [event()]
        events = implemented_dividends(rows, '20240604')
        balances = {'20221231': {'total_share': 1000, 'ann_date': '20230322'}}
        year = annual_dividend_cash(events, balances)['20221231']
        self.assertEqual(year['implemented_dps'], '0.3104')
        self.assertEqual(Decimal(year['estimated_dividend_cash']), Decimal('227612816.3024'))
        self.assertEqual([b['basis'] for b in year['dividend_cash_basis']],
                         ['year_end_proxy', 'verified_distribution_shares'])
        incomplete = annual_dividend_cash(events, {})['20221231']
        self.assertIsNone(incomplete['estimated_dividend_cash'])
        self.assertIsNone(incomplete['estimated_ordinary_cash'])
        self.assertEqual(len(incomplete['dividend_cash_basis']), 2)
        self.assertIsNone(incomplete['dividend_cash_basis'][0]['shares'])
        self.assertEqual(incomplete['dividend_cash_basis'][1]['estimated_dividend_cash'], '227612616.3024')

    def test_same_year_components_with_distinct_verified_bases_and_special_cash(self):
        raw = [event('20220630', '1'), event('20221231', '2')]
        registered = {'expected': raw, 'available_on': '20230310', 'source': 'https://example.com/test-only.pdf',
                      'pages': '1', 'note': 'Synthetic unit-test evidence', 'fiscal_components': [
                          {'end_date': '20220630', 'cash_div_tax': '1', 'ordinary_cash_div_tax': '.25',
                           'special_cash_div_tax': '.75', 'distribution_shares': '100'},
                          {'end_date': '20221231', 'cash_div_tax': '2', 'ordinary_cash_div_tax': '1.5',
                           'special_cash_div_tax': '.5', 'distribution_shares': '200'}]}
        with patch.dict(DISCLOSURES, {'cash_joint_periods': [registered]}):
            events = implemented_dividends(raw, '20230321')
            year = annual_dividend_cash(events, {'20221231': {'total_share': 999999}})['20221231']
        self.assertEqual(year['implemented_dps'], '3')
        self.assertEqual(year['ordinary_dps'], '1.75')
        self.assertEqual(Decimal(year['estimated_dividend_cash']), Decimal('500'))
        self.assertEqual(Decimal(year['estimated_ordinary_cash']), Decimal('325'))
        self.assertEqual([b['shares'] for b in year['dividend_cash_basis']], ['100', '200'])

    def test_proxy_only_cash_preserves_original_decimal_formula_and_special_classification(self):
        rows = [event(cash='2.970', ordinary_cash_div_tax='.460', special_cash_div_tax='2.510'),
                event('20220630', '.123456789', record_date='20220920', ex_date='20220921')]
        balances = {'20221231': {'total_share': '1234567890.123', 'ann_date': '20230322'}}
        year = annual_dividend_cash(rows, balances)['20221231']
        self.assertEqual(year['estimated_dividend_cash'], str(Decimal(year['implemented_dps']) * Decimal(balances['20221231']['total_share'])))
        self.assertEqual(year['estimated_ordinary_cash'], str(Decimal(year['ordinary_dps']) * Decimal(balances['20221231']['total_share'])))
        self.assertTrue(all(b['basis'] == 'year_end_proxy' for b in year['dividend_cash_basis']))
        with self.assertRaisesRegex(AppError, '普通、特别'):
            annual_dividend_cash([event(cash='1', ordinary_cash_div_tax='.8', special_cash_div_tax='.8')], balances)

    def test_unverified_special_field_does_not_invent_ordinary_classification(self):
        year = annual_dividend_cash([event(cash='1', special_cash_div_tax='.75')],
                                    {'20221231': {'total_share': 100}})['20221231']
        self.assertEqual(year['ordinary_dps'], '1')
        self.assertEqual(year['estimated_ordinary_cash'], '100')
        changed = copy.deepcopy(correction())
        changed['fiscal_components'][0]['special_cash_div_tax'] = '.01'
        with patch.dict(DISCLOSURES, {'cash_joint_periods': [changed]}), self.assertRaisesRegex(AppError, '明确普通部分'):
            implemented_dividends(changed['expected'], '20240604')

    def test_source_availability_and_fiscal_window_do_not_backfill(self):
        raw = correction()['expected']
        self.assertEqual(implemented_dividends(raw, '20240603'), [])
        events = implemented_dividends(raw, '20240604')
        self.assertEqual(annual_dividend_cash(events, {}, earliest_period='20230101').keys(), {'20231231'})
        self.assertEqual(annual_dividend_cash(events, {}, earliest_period='20240101'), {})
        future = {**raw[0], 'ann_date': '20240701', 'imp_ann_date': '20240701', 'cash_div_tax': .12}
        self.assertEqual(annual_dividend_cash(implemented_dividends(raw + [future], '20240604'), {}),
                         annual_dividend_cash(events, {}))

    def test_invalid_or_forged_distribution_shares_never_fall_back(self):
        source = correction()
        for invalid in ('0', '-1', '100.5', 'NaN', 'Infinity', None):
            changed = copy.deepcopy(source)
            changed['fiscal_components'][0]['distribution_shares'] = invalid
            with self.subTest(shares=invalid), patch.dict(DISCLOSURES, {'cash_joint_periods': [changed]}), self.assertRaises(AppError):
                implemented_dividends(changed['expected'], '20240604')
        raw = event(distribution_shares='1234')
        with self.assertRaises(AppError):
            implemented_dividends([raw], '20240604')
        normalized = implemented_dividends(source['expected'], '20240604')[0]
        variants = []
        changed = copy.deepcopy(normalized)
        changed['fiscal_components'][0]['distribution_shares'] = '1'
        variants.append(changed)
        variants.append({k: v for k, v in normalized.items() if k != 'event_kind'})
        variants.append({**normalized, 'correction_source_sha256': 'forged'})
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(AppError):
                annual_dividend_cash([variant], {})

    def test_automatic_recomputes_legacy_summary_and_rejects_altered_cash_or_dps(self):
        row, dossier = fixture()
        expected = assess(row, dossier, load_rules())
        self.assertEqual(expected['state'], '满足价格条件')
        legacy = copy.deepcopy(row)
        for year in legacy['annual']:
            for key in ('estimated_dividend_cash', 'estimated_ordinary_cash', 'dividend_cash_basis'):
                year.pop(key)
        self.assertEqual(assess(legacy, dossier, load_rules()), expected)
        for field in ('estimated_dividend_cash', 'estimated_ordinary_cash', 'implemented_dps', 'ordinary_dps'):
            changed = copy.deepcopy(row)
            changed['annual'][0][field] = '1'
            with self.subTest(field=field):
                self.assertEqual(assess(changed, dossier, load_rules())['state'], '待数据核实')
        dossier['data']['balancesheet'][4]['total_share'] = 2000
        self.assertEqual(assess(row, dossier, load_rules())['state'], '待数据核实')

    def test_deleting_old_summary_dps_cannot_hide_missing_or_zero_fourth_fifth_year_cash(self):
        for period in ('20221231', '20211231'):
            for missing in (True, False):
                row, dossier = fixture()
                old = next(year for year in row['annual'] if year['period'] == period)
                for key in ('implemented_dps', 'ordinary_dps', 'estimated_dividend_cash', 'estimated_ordinary_cash'):
                    old.pop(key)
                if missing:
                    dossier['data']['dividend'] = [r for r in dossier['data']['dividend'] if r['end_date'] != period]
                else:
                    next(r for r in dossier['data']['dividend'] if r['end_date'] == period)['cash_div_tax'] = '0'
                with self.subTest(period=period, missing=missing):
                    result = assess(row, dossier, load_rules())
                    self.assertEqual(result['state'], '待数据核实')
                    self.assertEqual(result['scenarios'], [])
                    self.assertIn('近五年已实施正现金分红', result['reason'])

    def test_missing_unused_old_proxy_does_not_expand_summary_gate(self):
        original, dossier = fixture()
        for year in (2018, 2019, 2020):
            for api in ('income', 'cashflow', 'balancesheet', 'fina_indicator', 'fina_audit'):
                source = next(r for r in dossier['data'][api] if r['end_date'] == '20211231')
                older = {**source, 'end_date': f'{year}1231', 'ann_date': f'{year + 1}0401'}
                if api == 'balancesheet':
                    older['total_share'] = None
                dossier['data'][api].append(older)
            dossier['data']['dividend'].append(event(f'{year}1231', '.6', ts_code=original['symbol'],
                ann_date=f'{year + 1}0401', imp_ann_date=f'{year + 1}0501',
                record_date=f'{year + 1}0510', ex_date=f'{year + 1}0511', pay_date=f'{year + 1}0512'))
        row = summarize(dossier, {'name': original['name'], 'industry': original['industry']},
                        {'trade_date': original['as_of'], 'close': '8', 'total_share': '.1'})
        row['selection'] = original['selection']
        self.assertTrue(row['gate'])
        self.assertTrue(all(year['estimated_dividend_cash'] is None for year in row['annual'][5:]))
        self.assertEqual(assess(row, dossier, load_rules())['state'], '满足价格条件')


if __name__ == '__main__':
    unittest.main()
