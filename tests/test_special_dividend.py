import copy
import unittest
from decimal import Decimal

from qmt.config import AppError, load_config
from qmt.forecast_diagnostic import observed_label
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, annual_dividend_cash, implemented_dividends


def source():
    return next(item for item in DISCLOSURES['cash_joint_periods'] if item['expected'][0]['ts_code'] == '600219.SH')


def other_cash(period, cash, record, ex, published):
    return {**source()['expected'][0], 'end_date': period, 'cash_div_tax': cash, 'ann_date': published,
            'imp_ann_date': published, 'record_date': record, 'ex_date': ex, 'pay_date': ex}


class SpecialDividendTests(unittest.TestCase):
    def test_exact_single_payment_classifies_only_after_implementation_and_is_idempotent(self):
        registered = copy.deepcopy(source())
        raw = copy.deepcopy(registered['expected'])
        self.assertEqual(implemented_dividends(raw, '20251204'), [])
        events = implemented_dividends(raw, '20251205')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['cash_div_tax'], '0.2584')
        self.assertEqual(events[0]['fiscal_components'], registered['fiscal_components'])
        self.assertEqual(events[0]['correction_source_sha256'], registered['source_sha256'])
        self.assertEqual(implemented_dividends(events, '20261106'), events)
        self.assertEqual(raw, registered['expected'])
        self.assertEqual(source(), registered)

    def test_annual_total_keeps_special_cash_but_ordinary_proxy_excludes_it(self):
        raw = source()['expected'] + [other_cash('20250630', '.04', '20250924', '20250925', '20250918'),
                                     other_cash('20251231', '.136', '20260519', '20260520', '20260513')]
        events = implemented_dividends(raw, '20261106')
        year = annual_dividend_cash(events, {'20251231': {'total_share': 10000000000, 'ann_date': '20260327'}})['20251231']
        self.assertEqual(Decimal(year['implemented_dps']), Decimal('.4344'))
        self.assertEqual(Decimal(year['ordinary_dps']), Decimal('.176'))
        self.assertEqual(Decimal(year['estimated_ordinary_cash']), Decimal('1760000000'))
        self.assertEqual(Decimal(year['estimated_dividend_cash']), Decimal('4731863350.6856'))
        special = next(b for b in year['dividend_cash_basis'] if b['end_date'] == '20251030')
        self.assertEqual(special['shares'], '11501019159')
        self.assertEqual(special['estimated_dividend_cash'], '2971863350.6856')
        self.assertEqual(special['estimated_ordinary_cash'], '0')
        self.assertEqual(special['basis'], 'verified_distribution_shares')
        incomplete = annual_dividend_cash(events, {})['20251231']
        self.assertIsNone(incomplete['estimated_dividend_cash'])
        self.assertIsNone(incomplete['estimated_ordinary_cash'])
        label = observed_label(raw, '20241231', 2025, '20261106', symbol='600219.SH')
        self.assertEqual(label['state'], 'proxy_total_cash')
        self.assertEqual(Decimal(label['observed_cash_dps']), Decimal('.4344'))
        self.assertFalse(label['ordinary_verified'])

    def test_unknown_component_duplicate_and_diluted_price_cannot_replace_original(self):
        raw = copy.deepcopy(source()['expected'][0])
        for changes in ({'cash_div_tax': .2559}, {'distribution_shares': 1}, {'end_date': '20251231'}):
            with self.assertRaises(AppError):
                implemented_dividends([{**raw, **changes}], '20251205')
        for rows in ([raw, copy.deepcopy(raw)], [raw, {**raw, 'cash_div_tax': .01}]):
            with self.assertRaises(AppError):
                implemented_dividends(rows, '20251205')

    def test_tampered_classification_evidence_or_share_count_is_rejected(self):
        event = implemented_dividends(source()['expected'], '20251205')[0]
        for key, value in (('ordinary_cash_div_tax', '.01'), ('special_cash_div_tax', '.25'), ('distribution_shares', '1')):
            changed = copy.deepcopy(event)
            changed['fiscal_components'][0][key] = value
            with self.assertRaises(AppError):
                implemented_dividends([changed], '20261106')
        for key in ('correction_source', 'correction_fingerprint', 'correction_source_sha256'):
            changed = {**event, key: 'tampered'}
            with self.assertRaises(AppError):
                implemented_dividends([changed], '20261106')

    def test_classification_preserves_investor_cash_once_without_orders(self):
        for events in (source()['expected'], implemented_dividends(source()['expected'], '20251205')):
            ledger = Ledger(':memory:', load_config())
            try:
                ledger.initialize('paper', '10000', '2000', '4000', '20251201')
                ledger.opening('paper', '600219.SH', 100, '20251201')
                ledger.dividends({'600219.SH': events}, '20251211')
                self.assertEqual(ledger.cash('paper', '20251211'), Decimal('10000'))
                ledger.dividends({'600219.SH': events}, '20251212')
                ledger.dividends({'600219.SH': events}, '20251212')
                self.assertEqual(ledger.cash('paper', '20251212'), Decimal('10025.84'))
                for table, expected in (('entitlements', 1), ('cash_events', 1), ('plans', 0), ('fills', 0), ('stock_events', 0)):
                    self.assertEqual(ledger.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], expected)
            finally:
                ledger.close()


if __name__ == '__main__':
    unittest.main()
