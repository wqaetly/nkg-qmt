import copy
import unittest
from decimal import Decimal

from qmt.backtest import simulate
from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, full_day_suspension, implemented_dividends, stock_distribution_terms


STOCK_EVENT = {'ann_date': '20200523', 'cash_div_tax': .1, 'div_proc': '实施',
               'end_date': '20191231', 'ex_date': '20200624', 'imp_ann_date': '20200617',
               'pay_date': '20200624', 'record_date': '20200623', 'stk_div': .3, 'ts_code': '000983.SZ'}
HALTED_DAYS = ['20220415', '20220418', '20220419', '20220420', '20220421',
               '20220422', '20220425', '20220426', '20220427', '20220428']


class VerifiedLongEventTests(unittest.TestCase):
    def test_stock_terms_require_the_exact_original_and_public_announcement_date(self):
        event = copy.deepcopy(STOCK_EVENT)
        self.assertIsNone(stock_distribution_terms(event, '20200616'))
        self.assertEqual(implemented_dividends([event], '20200616'), [])
        self.assertEqual(implemented_dividends([event], '20200617'), [event])
        self.assertEqual(stock_distribution_terms(event, '20200617'), {
            'available_on': '20200617', 'stock_list_date': '20200624',
            'source': 'https://static.cninfo.com.cn/finalpage/2020-06-17/1207931254.PDF'})
        for changes in ({'stk_div': .4}, {'cash_div_tax': .2}, {'ann_date': '20200524'},
                        {'end_date': '20181231'}, {'record_date': '20200622'},
                        {'ts_code': '000984.SZ'}, {'stock_list_date': '20200624'}):
            with self.subTest(changes=changes):
                self.assertIsNone(stock_distribution_terms({**event, **changes}, '20261106'))
        self.assertEqual(event, STOCK_EVENT)

    def test_900_record_date_shares_receive_270_bonus_shares_and_90_gross_cash_once(self):
        ledger = Ledger(':memory:', load_config())
        try:
            ledger.initialize('paper', '100000', '5000', '20000', '20200617')
            ledger.opening('paper', '000983.SZ', 900, '20200618')
            events = {'000983.SZ': implemented_dividends([STOCK_EVENT], '20200617')}
            ledger.dividends(events, '20200623')
            self.assertEqual(ledger.positions('paper'), {'000983.SZ': 900})
            self.assertEqual(ledger.cash('paper'), Decimal('100000'))
            ledger.dividends(events, '20200624')
            ledger.dividends(events, '20200624')
            ledger.dividends(events, '20200629')
            account = ledger.summary('paper')
            self.assertEqual(ledger.positions('paper', '20200623'), {'000983.SZ': 900})
            self.assertEqual(account['positions'], {'000983.SZ': 1170})
            self.assertEqual(ledger.available_to_sell('paper', '000983.SZ', '20200624'), 1170)
            self.assertEqual(ledger.cash('paper'), Decimal('100090'))
            self.assertEqual(len(account['cash_events']), 1)
            self.assertEqual(account['cash_events'][0]['amount'], '90.00')
            self.assertEqual(len(account['stock_events']), 1)
            event = account['stock_events'][0]
            self.assertEqual((event['record_quantity'], event['quantity'], event['stock_list_date']), (900, 270, '20200624'))
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM entitlements').fetchone()[0], 1)
        finally:
            ledger.close()

    def test_the_verified_stock_event_does_not_authorize_fractional_rounding(self):
        ledger = Ledger(':memory:', load_config())
        try:
            ledger.initialize('paper', '100000', '5000', '20000', '20200617')
            ledger.opening('paper', '000983.SZ', 901, '20200618')
            with self.assertRaisesRegex(AppError, '不足一股'):
                ledger.dividends({'000983.SZ': [STOCK_EVENT]}, '20200624')
            self.assertEqual(ledger.cash('paper'), Decimal('100000'))
            self.assertEqual(ledger.positions('paper'), {'000983.SZ': 901})
            self.assertFalse(ledger.summary('paper')['stock_events'])
            self.assertFalse(ledger.summary('paper')['cash_events'])
        finally:
            ledger.close()

    def test_erdos_2021_conversion_is_locked_until_the_day_after_ex_date(self):
        event = {'ts_code': '600295.SH', 'end_date': '20211231', 'ann_date': '20220510',
                 'div_proc': '实施', 'cash_div_tax': 2.0, 'stk_div': .4,
                 'record_date': '20220610', 'ex_date': '20220613',
                 'pay_date': '20220613', 'imp_ann_date': '20220607'}
        original = copy.deepcopy(event)
        self.assertIsNone(stock_distribution_terms(event, '20220606'))
        self.assertEqual(implemented_dividends([event], '20220606'), [])
        self.assertEqual(stock_distribution_terms(event, '20220607'), {
            'available_on': '20220607', 'stock_list_date': '20220614',
            'source': 'https://static.cninfo.com.cn/finalpage/2022-06-07/1213619323.PDF'})
        for changes in ({'cash_div_tax': 1.0}, {'stk_div': .3}, {'end_date': '20221231'},
                        {'ann_date': '20220511'}, {'imp_ann_date': '20220606'},
                        {'record_date': '20220615'}, {'ex_date': '20220614'},
                        {'pay_date': '20220623'}, {'ts_code': '900936.SH'},
                        {'stock_list_date': '20220613'}):
            with self.subTest(changes=changes):
                self.assertIsNone(stock_distribution_terms({**event, **changes}, '20261106'))
        ledger = Ledger(':memory:', load_config())
        try:
            ledger.initialize('paper', '100000', '5000', '20000', '20220607')
            ledger.opening('paper', '600295.SH', 100, '20220608')
            events = {'600295.SH': implemented_dividends([event], '20220607')}
            ledger.dividends(events, '20220610')
            self.assertEqual(ledger.positions('paper'), {'600295.SH': 100})
            self.assertEqual(ledger.cash('paper'), Decimal('100000'))
            ledger.dividends(events, '20220613')
            ledger.dividends(events, '20220613')
            self.assertEqual(ledger.positions('paper'), {'600295.SH': 140})
            self.assertEqual(ledger.available_to_sell('paper', '600295.SH', '20220613'), 100)
            self.assertEqual(ledger.cash('paper'), Decimal('100200'))
            ledger.dividends(events, '20220614')
            ledger.dividends(events, '20220623')
            self.assertEqual(ledger.available_to_sell('paper', '600295.SH', '20220614'), 140)
            self.assertEqual(ledger.positions('paper', '20220610'), {'600295.SH': 100})
            self.assertEqual(ledger.cash('paper'), Decimal('100200'))
            account = ledger.summary('paper')
            self.assertEqual(len(account['cash_events']), 1)
            self.assertEqual(account['cash_events'][0]['amount'], '200.00')
            self.assertEqual(len(account['stock_events']), 1)
            stock = account['stock_events'][0]
            self.assertEqual((stock['record_quantity'], stock['quantity'], stock['stock_list_date']),
                             (100, 40, '20220614'))
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM entitlements').fetchone()[0], 1)
            self.assertEqual(event, original)
        finally:
            ledger.close()

    def test_each_suspension_day_has_contemporaneous_evidence_and_stops_before_resumption(self):
        items = [r for r in DISCLOSURES['full_day_suspensions'] if r['symbol'] == '600449.SH']
        self.assertEqual([r['day'] for r in items], HALTED_DAYS)
        for day in HALTED_DAYS:
            proof = full_day_suspension('600449.SH', day)
            available = '20220415' if day < '20220422' else '20220422'
            document = '1212924744' if day < '20220422' else '1213024054'
            with self.subTest(day=day):
                self.assertTrue(proof['full_day'])
                self.assertEqual(proof['available_on'], available)
                self.assertLessEqual(proof['available_on'], day)
                self.assertIn(document, proof['source'])
                self.assertNotIn('1213223402', proof['source'])
        for day in ('20220414', '20220416', '20220429', '20220505'):
            self.assertIsNone(full_day_suspension('600449.SH', day))
        self.assertIsNone(full_day_suspension('600450.SH', '20220415'))

    def test_ten_halted_days_keep_the_last_real_close_then_use_the_resumption_bar(self):
        code = '600449.SH'
        days = ['20220413', '20220414', *HALTED_DAYS, '20220429']
        # Three original daily rows bracket the verified halt; no synthetic suspended bars.
        rows = {
            '20220413': {'open': '14.17', 'high': '14.17', 'low': '13.65', 'close': '13.75',
                         'vol': '93263.37', 'up': '15.53', 'down': '12.71'},
            '20220414': {'open': '13.87', 'high': '14.33', 'low': '13.7', 'close': '14.23',
                         'vol': '89727.79', 'up': '15.13', 'down': '12.38'},
            '20220429': {'open': '14.96', 'high': '14.96', 'low': '12.81', 'close': '13.04',
                         'vol': '338143.6', 'up': '15.65', 'down': '12.81'}}
        data = {'sessions': days, 'universe_as_of': days[0], 'bars': {code: rows},
                'industries': {code: '建筑材料'}, 'dividends': {code: []}}
        signals = {days[0]: [{'symbol': code, 'reference_price': '14.23', 'known_on': days[0]}]}
        original = copy.deepcopy((data, signals))
        cfg = {**load_config(), 'slippage_bps': 0}
        result = simulate(data, signals, cfg, max_holdings=20)
        values = result['valuation_events']
        self.assertEqual([e['day'] for e in values], HALTED_DAYS)
        self.assertEqual([e['stale_sessions'] for e in values], list(range(1, 11)))
        for index, event in enumerate(values, 1):
            self.assertEqual((event['quantity'], event['price_date']), (300, '20220414'))
            self.assertEqual(Decimal(event['price']), Decimal('14.23'))
            self.assertEqual(Decimal(event['market_value']), Decimal('4269'))
            self.assertFalse(event['tradable'])
            self.assertEqual([p['day'] for p in event['proofs']], HALTED_DAYS[:index])
            self.assertTrue(all(p['available_on'] <= p['day'] for p in event['proofs']))
        curve = {r['day']: r for r in result['equity_curve']}
        for day in HALTED_DAYS:
            self.assertEqual(curve[day]['positions'], {code: 300})
            self.assertEqual(Decimal(curve[day]['equity']) - Decimal(curve[day]['cash']), Decimal('4269'))
        resumed = curve['20220429']
        self.assertEqual(Decimal(resumed['equity']) - Decimal(resumed['cash']), Decimal('3912'))
        self.assertEqual([(t['trade_date'], t['quantity']) for t in result['trades']], [('20220414', 300)])
        self.assertEqual(len(result['plans']), 1)
        self.assertEqual(result['cash_events'], [])
        self.assertEqual(result['stock_events'], [])
        self.assertEqual((data, signals), original)
        self.assertEqual(set(rows), {'20220413', '20220414', '20220429'})


if __name__ == '__main__':
    unittest.main()
