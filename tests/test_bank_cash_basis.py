import copy
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, annual_cash_decision, annual_dividend_cash, implemented_dividends, stock_distribution_terms


class CashBasisStockTermsTests(unittest.TestCase):
    def test_verified_cash_base_keeps_the_original_stock_listing_terms(self):
        raw = {'ts_code': '600001.SH', 'end_date': '20091231', 'ann_date': '20100429',
               'div_proc': '实施', 'cash_div_tax': .15, 'stk_div': .3,
               'record_date': '20100609', 'ex_date': '20100610',
               'pay_date': '20100618', 'imp_ann_date': '20100604'}
        proof = {'expected': raw, 'available_on': '20100604', 'source': 'https://example.com/fictional.pdf',
                 'source_sha256': 'a' * 64, 'pages': '1—3'}
        registry = {'cash_basis': [{**proof, 'distribution_shares': '1000', 'total_cash': '150'}],
                    'stock_distributions': [{**proof, 'stock_list_date': '20100611'}]}
        with patch.dict(DISCLOSURES, registry):
            event = implemented_dividends([raw], '20100604')[0]
            self.assertEqual(stock_distribution_terms(event, '20100604'),
                             stock_distribution_terms(raw, '20100604'))
            for change in ({'stk_div': .4}, {'cash_div_tax': .16}, {'correction_originals': []},
                           {'correction_source_sha256': 'b' * 64}):
                with self.subTest(change=change), self.assertRaises(AppError):
                    stock_distribution_terms({**event, **change}, '20100604')
            with self.assertRaises(AppError):
                stock_distribution_terms(event, '20100603')


class PudongCashSourceTests(unittest.TestCase):
    def setUp(self):
        self.proofs = [p for p in DISCLOSURES['cash_basis'] if p['expected']['ts_code'] == '600000.SH']

    def test_five_exact_bases_are_available_only_with_the_implementation_notice(self):
        totals = {'20091231': '1324506846.00', '20101231': '2295811866.40',
                  '20111231': '5596041424.50', '20121231': '10259409278.25',
                  '20131231': '12311291133.90'}
        self.assertEqual({p['expected']['end_date'] for p in self.proofs}, set(totals))
        for proof in self.proofs:
            raw = copy.deepcopy(proof['expected'])
            period, day = raw['end_date'], proof['available_on']
            before = (datetime.strptime(day, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
            with self.subTest(period=period):
                self.assertEqual(implemented_dividends([raw], before), [])
                events = implemented_dividends([raw], day)
                cash = annual_dividend_cash(events, {})[period]
                self.assertEqual(Decimal(cash['estimated_dividend_cash']), Decimal(totals[period]))
                self.assertEqual(cash['dividend_cash_basis'][0]['basis'], 'verified_distribution_shares')
                self.assertEqual(annual_dividend_cash(events, {period: {'total_share': '999999'}})[period], cash)
                self.assertNotIn('ordinary_cash_div_tax', events[0]['fiscal_components'][0])
                self.assertNotIn('special_cash_div_tax', events[0]['fiscal_components'][0])
                self.assertEqual(raw, proof['expected'])
                with self.assertRaises(AppError):
                    implemented_dividends([{**raw, 'cash_div_tax': str(raw['cash_div_tax'])}], day)

    def test_exact_implementation_does_not_resolve_an_unknown_interim_or_whole_year(self):
        announcements = ['20090829', '20100830', '20110816', '20120815', '20130814']
        for proof, day in zip(self.proofs, announcements):
            raw = copy.deepcopy(proof['expected'])
            interim = {'ts_code': '600000.SH', 'end_date': raw['end_date'][:4] + '0630',
                       'ann_date': day, 'div_proc': '股东大会通过', 'cash_div_tax': None,
                       'stk_div': 0.0, 'record_date': None, 'ex_date': None,
                       'pay_date': None, 'imp_ann_date': None}
            with self.subTest(period=raw['end_date']):
                decision = annual_cash_decision([raw, interim], '600000.SH', raw['end_date'], '20141231', {})
                self.assertIsNone(decision)
                self.assertNotIn('annual_composition', proof)
                self.assertIsNone(interim['cash_div_tax'])

    def test_bonus_shares_wait_for_listing_and_personal_cash_is_not_company_total(self):
        for proof in self.proofs[:2]:
            raw = proof['expected']
            events = implemented_dividends([raw], raw['ex_date'])
            terms = stock_distribution_terms(events[0], raw['ex_date'])
            for quantity in (100, 101):
                ledger = Ledger(':memory:', load_config())
                try:
                    ledger.initialize('paper', '100000', '5000', '20000', raw['end_date'][:4] + '0101')
                    ledger.opening('paper', '600000.SH', quantity, raw['record_date'])
                    if quantity == 101:
                        with self.assertRaisesRegex(AppError, '不足一股'):
                            ledger.dividends({'600000.SH': events}, raw['ex_date'])
                        self.assertEqual(ledger.positions('paper'), {'600000.SH': 101})
                        self.assertEqual(ledger.summary('paper')['stock_events'], [])
                        continue
                    ledger.dividends({'600000.SH': events}, raw['ex_date'])
                    self.assertEqual(ledger.positions('paper'), {'600000.SH': 130})
                    self.assertEqual(ledger.available_to_sell('paper', '600000.SH', raw['ex_date']), 100)
                    self.assertEqual(ledger.available_to_sell('paper', '600000.SH', terms['stock_list_date']), 130)
                    self.assertEqual(ledger.cash('paper'), Decimal('100000'))
                    ledger.dividends({'600000.SH': events}, raw['pay_date'])
                    ledger.dividends({'600000.SH': events}, raw['pay_date'])
                    self.assertEqual(ledger.cash('paper'), Decimal('100000') + Decimal(str(raw['cash_div_tax'])) * 100)
                    summary = ledger.summary('paper')
                    self.assertEqual(len(summary['stock_events']), 1)
                    self.assertEqual(len(summary['cash_events']), 1)
                finally:
                    ledger.close()
