import copy
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from qmt.config import AppError
from qmt.research import (DISCLOSURES, annual_cash_decision, annual_dividend_cash,
                          implemented_dividends, verified_zero_cash_years)


class AnnualSourceCorrectionsTests(unittest.TestCase):
    def test_verified_whole_year_zero_starts_at_public_approval_without_payment(self):
        for code, period, day in [('002081.SZ', '20211231', '20220524'),
                                  ('601515.SH', '20221231', '20230513')]:
            with self.subTest(symbol=code):
                proof = next(p for p in DISCLOSURES['zero_cash_years']
                             if p['symbol'] == code and p['period'] == period)
                rows = copy.deepcopy(proof['expected'])
                original = copy.deepcopy(rows)
                before = (datetime.strptime(day, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
                self.assertEqual(proof['available_on'], day)
                self.assertEqual(verified_zero_cash_years(rows, code, before), {})
                self.assertEqual(verified_zero_cash_years(rows, code, day)[period]['cash_dps'], '0')
                self.assertEqual(implemented_dividends(rows, day), [])
                self.assertTrue(any(r['cash_div_tax'] is None for r in rows))
                self.assertEqual(rows, original)
                with self.assertRaises(AppError):
                    verified_zero_cash_years(rows + [rows[0]], code, day)
                revised = {**rows[0], 'ann_date': '20261106', 'cash_div_tax': .1}
                self.assertIn(period, verified_zero_cash_years(rows + [revised], code, day))
                with self.assertRaises(AppError):
                    verified_zero_cash_years(rows + [revised], code, '20261106')

    def test_rejected_plan_is_not_approved_until_later_public_vote(self):
        code, period = '600846.SH', '20221231'
        proof = next(p for p in DISCLOSURES['annual_cash_decisions']
                     if p['symbol'] == code and p['period'] == period)
        phase = {'ts_code': code, 'end_date': period, 'ann_date': '20230412',
                 'cash_div_tax': .17, 'stk_div': 0.0, 'record_date': None,
                 'ex_date': None, 'pay_date': None, 'imp_ann_date': None}
        rows = [{**phase, 'div_proc': stage} for stage in ('预案', '股东大会通过', '未通过')]
        rows += copy.deepcopy(proof['known_interim_rows'])
        rows.append({**phase, 'ann_date': '20231013', 'div_proc': '实施',
                     'imp_ann_date': '20231021', 'record_date': '20231026',
                     'ex_date': '20231027', 'pay_date': '20231027'})
        original = copy.deepcopy(rows)
        balances = {period: {'total_share': '624761516'}}
        for day in ('20230412', '20230629', '20230929', '20231012'):
            self.assertEqual(annual_cash_decision(rows, code, period, day, balances)['stage'], 'unresolved')
            self.assertEqual(implemented_dividends(rows, day), [])
        approved = annual_cash_decision(rows, code, period, '20231013', balances)
        self.assertEqual(approved['stage'], 'approved')
        self.assertEqual(Decimal(approved['total_cash']), Decimal('106209457.72'))
        self.assertEqual(implemented_dividends(rows, '20231013'), [])
        self.assertIsNone(annual_cash_decision(rows, code, period, '20231021', balances))
        self.assertEqual(len(implemented_dividends(rows, '20231021')), 1)
        self.assertEqual(rows, original)

    def test_interim_cash_and_no_additional_annual_cash_are_one_payment(self):
        code, period = '600123.SH', '20231231'
        proof = next(p for p in DISCLOSURES['cash_basis'] if p['expected']['ts_code'] == code)
        rows = copy.deepcopy([proof['expected'], *proof['annual_composition']['expected']])
        original = copy.deepcopy(rows)
        self.assertEqual(implemented_dividends(rows, '20240201'), [])
        events = implemented_dividends(rows, '20240202')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['cash_div_tax'], .75)
        self.assertEqual(events[0]['pay_date'], '20240208')
        self.assertEqual(annual_cash_decision(rows, code, period, '20240517', {})['stage'], 'unresolved')
        complete = annual_cash_decision(rows, code, period, '20240518', {})
        self.assertEqual(complete['stage'], 'implemented')
        self.assertEqual(complete['cash_scope'], 'reported_annual_total')
        self.assertEqual(Decimal(complete['total_cash']), Decimal('1113840000.00'))
        self.assertEqual(implemented_dividends(rows, '20240518'), events)
        cash = annual_dividend_cash(events, {})[period]
        self.assertEqual(cash['implemented_dps'], '0.75')
        self.assertEqual(Decimal(cash['estimated_dividend_cash']), Decimal('1113840000.00'))
        self.assertEqual(verified_zero_cash_years(rows, code, '20240518'), {})
        self.assertEqual(rows, original)

    def test_deferred_annual_cash_is_not_registered_as_zero(self):
        self.assertFalse(any(p['symbol'] == '600720.SH' and p['period'] == '20221231'
                             for p in DISCLOSURES['zero_cash_years']))


if __name__ == '__main__':
    unittest.main()
