import copy
import hashlib
import json
import unittest
from decimal import Decimal

from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, annual_dividend_cash, implemented_dividends, stock_distribution_terms


CODE = '600188.SH'


def correction(year):
    key = 'cash_dividend_overlaps' if year == 2021 else 'cash_dividend_components'
    return next(c for c in DISCLOSURES[key] if c['expected'][0]['ts_code'] == CODE)


class DividendComponentTests(unittest.TestCase):
    def test_verified_ordinary_and_special_components_make_one_fiscal_event(self):
        for year, total, ordinary, special, ratio in ((2021, '2.00', '1.60', '.40', '0'),
                                                    (2022, '4.30', '3.07', '1.23', '.5')):
            source = correction(year)
            rows = copy.deepcopy(source['expected'])
            events = implemented_dividends(rows, source['available_on'])
            with self.subTest(year=year):
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event['end_date'], f'{year}1231')
                self.assertEqual(Decimal(str(event['cash_div_tax'])), Decimal(total))
                self.assertEqual(Decimal(str(event['stk_div'])), Decimal(ratio))
                component = event['fiscal_components'][0]
                self.assertEqual(len(event['fiscal_components']), 1)
                self.assertEqual(component['end_date'], f'{year}1231')
                self.assertEqual(Decimal(component['ordinary_cash_div_tax']), Decimal(ordinary))
                self.assertEqual(Decimal(component['special_cash_div_tax']), Decimal(special))
                self.assertEqual(Decimal(component['ordinary_cash_div_tax']) + Decimal(component['special_cash_div_tax']), Decimal(total))
                self.assertEqual(event['correction_originals'], rows)
                self.assertEqual(event['correction_fingerprint'], hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
                self.assertEqual(event['correction_source_sha256'], source['source_sha256'])
                self.assertEqual(event['correction_source'], source['source'])
                self.assertEqual(implemented_dividends(rows[::-1], source['available_on']), events)
                self.assertEqual(implemented_dividends(events, source['available_on']), events)
                self.assertEqual(rows, source['expected'])

    def test_implementation_is_unavailable_before_its_announcement(self):
        for year, before in ((2021, '20220707'), (2022, '20230709')):
            source = correction(year)
            event = implemented_dividends(source['expected'], source['available_on'])
            for rows in (source['expected'], event):
                with self.subTest(year=year, canonical=rows is event):
                    self.assertEqual(implemented_dividends(rows, before), [])

    def test_additive_event_needs_both_components_and_rejects_duplicates(self):
        source = correction(2022)
        rows = source['expected']
        cases = [rows[:1], rows[1:], rows + [rows[0]], rows + [rows[1]]]
        for change in ({'cash_div_tax': 1.24}, {'stk_div': .5}, {'pay_date': '20230718'},
                       {'ann_date': '20230326'}, {'record_date': '20230713'},
                       {'end_date': '20231231'}, {'extra_source_field': 'new schema'}):
            cases.append([rows[0], {**rows[1], **change}])
        cases.append([{**r, 'ts_code': '600099.SH'} for r in rows])
        for changed in cases:
            with self.subTest(rows=changed), self.assertRaises(AppError):
                implemented_dividends(changed, source['available_on'])

    def test_inclusive_event_keeps_total_only_and_never_invents_total_from_special(self):
        source = correction(2021)
        total_only = implemented_dividends(source['expected'][:1], source['available_on'])
        self.assertEqual(Decimal(str(total_only[0]['cash_div_tax'])), Decimal('2'))
        self.assertEqual(annual_dividend_cash(total_only, {})['20211231']['ordinary_dps'], '1.60')
        with self.assertRaises(AppError):
            implemented_dividends(source['expected'][1:], source['available_on'])

    def test_future_unknown_versions_only_block_once_visible(self):
        for year, future_day in ((2021, '20220801'), (2022, '20230801')):
            source = correction(year)
            rows = source['expected']
            events = implemented_dividends(rows, source['available_on'])
            revised = {**rows[1], 'cash_div_tax': '9.99', 'ann_date': future_day, 'imp_ann_date': future_day}
            for known in (rows, events):
                with self.subTest(year=year, canonical=known is events):
                    self.assertEqual(implemented_dividends(known + [revised], source['available_on']), events)
                    with self.assertRaises(AppError):implemented_dividends(known + [revised], future_day)

    def test_changed_record_or_ex_date_cannot_split_a_known_cash_event(self):
        sources = [correction(2021), correction(2022), DISCLOSURES['cash_dividend_overlaps'][0]]
        for source in sources:
            rows = source['expected']
            events = implemented_dividends(rows, source['available_on'])
            for field in ('record_date', 'ex_date'):
                changed = {**rows[1], field: str(int(rows[1][field]) - 1)}
                with self.subTest(symbol=rows[0]['ts_code'], field=field):
                    with self.assertRaises(AppError):implemented_dividends([rows[0], changed], source['available_on'])
                    future = {**changed, 'ann_date': '20240801', 'imp_ann_date': '20240801'}
                    for known in (rows, events):
                        self.assertEqual(implemented_dividends(known + [future], source['available_on']), events)
                        with self.assertRaises(AppError):implemented_dividends(known + [future], '20240801')

    def test_canonical_metadata_components_and_stock_ratio_cannot_be_forged(self):
        for year in (2021, 2022):
            source = correction(year)
            event = implemented_dividends(source['expected'], source['available_on'])[0]
            changes = [{'cash_div_tax': '99'}, {'stk_div': '.6'}, {'end_date': '20231231'},
                       {'correction_source': 'https://example.test/claim'}, {'correction_source_sha256': 'f' * 64},
                       {'correction_available_on': '20200101'}, {'correction_fingerprint': 'fake'},
                       {'correction_originals': []}, {'ts_code': '600099.SH'}]
            variants = [{**event, **change} for change in changes]
            variants.append({k: v for k, v in event.items() if k != 'event_kind'})
            for change in ({'end_date': '20231231'}, {'distribution_shares': '100'},
                           {'cash_div_tax': '99'}, {'ordinary_cash_div_tax': '0'}, {'special_cash_div_tax': '0'}):
                modified = copy.deepcopy(event)
                modified['fiscal_components'][0].update(change)
                variants.append(modified)
            for changed in variants:
                with self.subTest(year=year, event=changed), self.assertRaises(AppError):
                    implemented_dividends([changed], source['available_on'])
            for extra in (event, *source['expected']):
                with self.subTest(year=year, extra=extra), self.assertRaises(AppError):
                    implemented_dividends([event, extra], source['available_on'])

    def test_cash_uses_disclosed_distribution_shares_and_correct_fiscal_year(self):
        events = implemented_dividends(correction(2021)['expected'] + correction(2022)['expected'], '20230710')
        balances = {'20211231': {'total_share': 10}, '20221231': {'total_share': 20}}
        annual = annual_dividend_cash(events, balances)
        self.assertEqual(set(annual), {'20211231', '20221231'})
        for period, shares, total, ordinary in (('20211231', '4948703640', '2.00', '1.60'),
                                                ('20221231', '4961360480', '4.30', '3.07')):
            row = annual[period]
            self.assertEqual(Decimal(row['estimated_dividend_cash']), Decimal(shares) * Decimal(total))
            self.assertEqual(Decimal(row['estimated_ordinary_cash']), Decimal(shares) * Decimal(ordinary))
            self.assertEqual(len(row['dividend_cash_basis']), 1)
            self.assertEqual(row['dividend_cash_basis'][0]['basis'], 'verified_distribution_shares')
        self.assertEqual(set(annual_dividend_cash(events, balances, earliest_period='20220101')), {'20221231'})
        self.assertEqual(annual_dividend_cash(events, balances, earliest_period='20230101'), {})
        self.assertEqual(implemented_dividends(correction(2022)['expected'], '20341231', earliest_period='20230101'), [])
        damaged = copy.deepcopy(events)
        damaged[1]['fiscal_components'][0]['distribution_shares'] = '100'
        with self.assertRaises(AppError):annual_dividend_cash(damaged, balances)

    def test_real_ledger_pays_860_and_adds_100_shares_exactly_once(self):
        events = implemented_dividends(correction(2022)['expected'], '20230710')
        ledger = Ledger(':memory:', load_config())
        try:
            ledger.initialize('paper', '100000', '5000', '20000', '20230710')
            ledger.opening('paper', CODE, 200, '20230712')
            ledger.dividends({CODE: events}, '20230714')
            self.assertEqual(ledger.cash('paper'), Decimal('100000'))
            self.assertEqual(ledger.positions('paper'), {CODE: 200})
            ledger.dividends({CODE: events}, '20230717')
            ledger.dividends({CODE: events}, '20230717')
            ledger.dividends({CODE: events}, '20230718')
            account = ledger.summary('paper')
            self.assertEqual(ledger.cash('paper'), Decimal('100860'))
            self.assertEqual(ledger.positions('paper', '20230714'), {CODE: 200})
            self.assertEqual(ledger.positions('paper'), {CODE: 300})
            self.assertEqual(ledger.available_to_sell('paper', CODE, '20230717'), 300)
            self.assertEqual(len(account['cash_events']), 1)
            self.assertEqual(account['cash_events'][0]['amount'], '860.00')
            self.assertEqual(len(account['stock_events']), 1)
            self.assertEqual((account['stock_events'][0]['record_quantity'], account['stock_events'][0]['quantity']), (200, 100))
            self.assertEqual(account['stock_events'][0]['stock_list_date'], '20230717')
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM entitlements').fetchone()[0], 1)
        finally:
            ledger.close()

    def test_stock_terms_still_require_the_complete_exact_canonical_event(self):
        source = correction(2022)
        event = implemented_dividends(source['expected'], source['available_on'])[0]
        self.assertEqual(stock_distribution_terms(event, '20230717')['stock_list_date'], '20230717')
        self.assertIsNone(stock_distribution_terms(source['expected'][0], '20230717'))
        self.assertIsNone(stock_distribution_terms(event, '20230709'))
        self.assertIsNone(stock_distribution_terms({**event, 'cash_div_tax': '4.31'}, '20230717'))
        ledger = Ledger(':memory:', load_config())
        try:
            ledger.initialize('paper', '100000', '5000', '20000', '20230710')
            ledger.opening('paper', CODE, 200, '20230712')
            with self.assertRaises(AppError):
                ledger.dividends({CODE: [{**event, 'stk_div': '.6'}]}, '20230717')
            self.assertEqual(ledger.cash('paper'), Decimal('100000'))
            self.assertEqual(ledger.positions('paper'), {CODE: 200})
            self.assertFalse(ledger.summary('paper')['stock_events'])
        finally:
            ledger.close()


if __name__ == '__main__':
    unittest.main()
