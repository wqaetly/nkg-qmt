import copy
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.config import AppError, load_config
from qmt.ledger import Ledger, rebalance_quantity
from qmt.research import DISCLOSURES


SYMBOL = '600001.SH'


def event(symbol=SYMBOL, **changes):
    return {'ts_code': symbol, 'end_date': '20251231', 'ann_date': '20261015',
            'imp_ann_date': '20261030', 'div_proc': '实施', 'record_date': '20261103',
            'ex_date': '20261104', 'pay_date': '20261106', 'cash_div_tax': '1', 'stk_div': '.3', **changes}


def terms(listed='20261106', **changes):
    return {'stock_list_date': listed, 'source': 'https://example.test/verified.pdf',
            'available_on': '20261030', **changes}


def prices(day, close='10', symbol=SYMBOL):
    return {symbol: {'trade_date': day, 'close': close}}


def bar():
    return {key: Decimal(value) for key, value in {
        'open': '10', 'high': '11', 'low': '9', 'close': '10', 'vol': '10000', 'up': '12', 'down': '8'}.items()}


class StockLedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(':memory:', {**load_config(), 'slippage_bps': 0})
        self.ledger.initialize('paper', '100000', '5000', '20000', '20261101')
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules','test')")

    def tearDown(self):
        self.ledger.close()

    def plan(self, side, qty, day='20261103', execute='20261104', limit='10', symbol=SYMBOL):
        return self.ledger.create_plan('paper', {'symbol': symbol, 'industry': '测试行业', 'automatic_signal': True},
            day, execute, qty, limit, side, '测试订单', prices(day, symbol=symbol), {symbol: '测试行业'})

    def test_cash_and_stock_ex_date_neutrality_and_idempotency(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        self.ledger.mark('paper', '20261103', prices('20261103', '14'))
        initial_equity = self.ledger.summary('paper')['marks'][-1]['equity']
        events = {SYMBOL: [event()]}
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()):
            self.ledger.dividends(events, '20261104')
            self.ledger.dividends(events, '20261104')
            self.assertEqual(self.ledger.positions('paper', '20261103'), {SYMBOL: 300})
            self.assertEqual(self.ledger.positions('paper', '20261104'), {SYMBOL: 390})
            self.ledger.mark('paper', '20261104', prices('20261104'))
            mark = self.ledger.summary('paper')['marks'][-1]
            self.assertEqual(mark['receivable'], '300.00')
            self.assertEqual(mark['equity'], initial_equity)
            self.assertEqual(self.ledger.cash('paper'), Decimal('100000'))
            self.ledger.dividends(events, '20261106')
            self.ledger.dividends(events, '20261106')
        self.ledger.mark('paper', '20261106', prices('20261106'))
        account = self.ledger.summary('paper')
        self.assertEqual(account['marks'][-1]['equity'], initial_equity)
        self.assertEqual(account['marks'][-1]['receivable'], '0')
        self.assertEqual(account['cash'], '100300.00')
        self.assertEqual(len(account['cash_events']), 1)
        self.assertEqual(len(account['stock_events']), 1)
        stock = account['stock_events'][0]
        self.assertEqual((stock['record_quantity'], stock['quantity'], stock['ratio']), (300, 90, '0.3'))
        self.assertEqual((stock['stock_list_date'], stock['available_on']), ('20261106', '20261030'))

    def test_listing_lock_and_t_plus_one_share_one_availability_rule(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()):
            self.ledger.dividends({SYMBOL: [event()]}, '20261104')
        self.assertEqual(self.ledger.available_to_sell('paper', SYMBOL, '20261104'), 300)
        self.plan('buy', 100, '20261104', '20261105')
        self.ledger.settle_paper('20261105', {SYMBOL: {'20261105': bar()}})
        self.assertEqual(self.ledger.positions('paper'), {SYMBOL: 490})
        self.assertEqual(self.ledger.available_to_sell('paper', SYMBOL, '20261105'), 300)
        self.assertEqual(self.ledger.available_to_sell('paper', SYMBOL, '20261106'), 490)

    def test_locked_sell_fails_direct_fill_but_expires_paper_plan_then_lists(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()):
            self.ledger.dividends({SYMBOL: [event()]}, '20261104')
        plan_id = self.plan('sell', 390, '20261104', '20261105', '9.9')
        plan = dict(self.ledger.db.execute('SELECT * FROM plans WHERE id=?', (plan_id,)).fetchone())
        with self.assertRaisesRegex(AppError, '尚未上市'):
            self.ledger._fill(plan, '20261105', 390, '10', '5', 'too-early')
        self.ledger.settle_paper('20261105', {SYMBOL: {'20261105': bar()}})
        self.assertEqual(self.ledger.summary('paper')['plans'][0]['status'], 'expired')
        self.assertFalse(self.ledger.summary('paper')['fills'])
        self.plan('sell', 390, '20261105', '20261106', '9.9')
        self.ledger.settle_paper('20261106', {SYMBOL: {'20261106': bar()}})
        self.assertEqual(self.ledger.positions('paper'), {})

    def test_subsequent_record_date_includes_previous_stock_entitlement(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        first = event()
        second = event(record_date='20261107', ex_date='20261108', pay_date='20261108', stk_div='.1')
        with patch('qmt.ledger.stock_distribution_terms', side_effect=lambda e, day: terms(e['ex_date'])):
            self.ledger.dividends({SYMBOL: [first]}, '20261106')
            self.ledger.dividends({SYMBOL: [second]}, '20261108')
            self.ledger.dividends({SYMBOL: [first, second]}, '20261108')
        self.assertEqual(self.ledger.positions('paper', '20261107'), {SYMBOL: 390})
        self.assertEqual(self.ledger.positions('paper', '20261108'), {SYMBOL: 429})
        self.assertEqual(self.ledger.summary('paper')['stock_events'][1]['record_quantity'], 390)
        self.assertEqual(self.ledger.cash('paper'), Decimal('100690'))

    def test_unheld_unverified_event_cancels_old_buy_without_blocking(self):
        self.plan('buy', 100)
        with patch('qmt.ledger.stock_distribution_terms', return_value=None) as lookup:
            self.ledger.dividends({SYMBOL: [event()]}, '20261104')
        lookup.assert_not_called()
        self.ledger.settle_paper('20261104', {SYMBOL: {'20261104': bar()}})
        account = self.ledger.summary('paper')
        self.assertEqual(account['plans'][0]['status'], 'expired')
        self.assertIn('除权', account['plans'][0]['note'])
        self.assertEqual(account['positions'], {})
        self.assertFalse(account['fills'] or account['stock_events'] or account['cash_events'])

    def test_catchup_does_not_cancel_an_order_scheduled_before_ex_date(self):
        self.plan('buy', 100, '20261102', '20261103')
        self.ledger.dividends({SYMBOL: [event()]}, '20261104')
        plan = self.ledger.summary('paper')['plans'][0]
        self.assertEqual(plan['status'], 'pending')
        self.assertNotIn('除权', plan['note'])

    def test_same_call_orders_successive_stock_events_by_ex_date(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        first = event()
        second = event(record_date='20261107', ex_date='20261108', pay_date='20261108', stk_div='.1')
        with patch('qmt.ledger.stock_distribution_terms', side_effect=lambda e, day: terms(e['ex_date'])):
            self.ledger.dividends({SYMBOL: [second, first]}, '20261108')
            self.ledger.dividends({SYMBOL: [first, second]}, '20261108')
        account = self.ledger.summary('paper')
        self.assertEqual(account['positions'], {SYMBOL: 429})
        self.assertEqual(account['cash'], '100690.00')
        self.assertEqual([e['record_quantity'] for e in account['stock_events']], [300, 390])

    def test_held_event_without_registered_terms_still_blocks(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        self.plan('sell', 300)
        with self.assertRaisesRegex(AppError, '未核实'):
            self.ledger.dividends({SYMBOL: [event()]}, '20261104')
        account = self.ledger.summary('paper')
        self.assertEqual(account['plans'][0]['status'], 'pending')
        self.assertEqual(account['positions'], {SYMBOL: 300})
        self.assertFalse(account['stock_events'])

    def test_fractional_share_rolls_back_all_events_cash_orders_and_marks(self):
        other = '600002.SH'
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        self.ledger.opening('paper', other, 101, '20261102')
        self.plan('sell', 300)
        self.ledger.mark('paper', '20261103', {**prices('20261103'), **prices('20261103', symbol=other)})
        before = self.ledger.summary('paper')
        events = {SYMBOL: [event(pay_date='20261104')], other: [event(other, pay_date='20261104')]}
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()), self.assertRaisesRegex(AppError, '不足一股'):
            self.ledger.dividends(events, '20261104')
        self.assertEqual(self.ledger.summary('paper'), before)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM entitlements').fetchone()[0], 0)

    def test_existing_terms_or_record_shares_may_not_be_silently_rewritten(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        events = {SYMBOL: [event()]}
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()):
            self.ledger.dividends(events, '20261104')
        before = self.ledger.summary('paper')
        for changed in (terms('20261107'), terms(source='https://example.test/revised.pdf'), terms(available_on='20261031')):
            with self.subTest(terms=changed), patch('qmt.ledger.stock_distribution_terms', return_value=changed), self.assertRaises(AppError):
                self.ledger.dividends(events, '20261106')
            self.assertEqual(self.ledger.summary('paper'), before)
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()), self.assertRaises(AppError):
            self.ledger.dividends({SYMBOL: [event(stk_div='.4')]}, '20261106')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE openings SET quantity=400 WHERE mode='paper'")
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()), self.assertRaisesRegex(AppError, '登记日持仓'):
            self.ledger.dividends(events, '20261106')
        self.assertEqual(self.ledger.summary('paper')['stock_events'], before['stock_events'])

    def test_pure_stock_event_does_not_create_cash_and_manual_holdings_are_untouched(self):
        self.ledger.opening('paper', SYMBOL, 300, '20261102')
        self.ledger.initialize('manual', '100000', '5000', '20000', '20261101')
        self.ledger.opening('manual', SYMBOL, 300, '20261102')
        pure = event(event_kind='stock_only', cash_div_tax=0, pay_date=None, stock_list_date='20261106')
        with patch('qmt.ledger.stock_distribution_terms', return_value=terms()):
            self.ledger.dividends({SYMBOL: [pure]}, '20261104')
            self.ledger.dividends({SYMBOL: [pure]}, '20261106')
        self.assertEqual(self.ledger.positions('paper'), {SYMBOL: 390})
        self.assertFalse(self.ledger.summary('paper')['cash_events'])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM entitlements').fetchone()[0], 0)
        self.assertEqual(self.ledger.positions('manual'), {SYMBOL: 300})
        self.assertFalse(self.ledger.summary('manual')['stock_events'])

    def test_real_registered_event_credits_integer_rights_and_invalidates_old_order(self):
        source = next(s for s in DISCLOSURES['stock_distributions'] if s['expected']['ts_code'] == '600123.SH')
        raw = copy.deepcopy(source['expected'])
        self.ledger.close()
        self.ledger = Ledger(':memory:', load_config())
        self.ledger.initialize('paper', '100000', '5000', '20000', '20230101')
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules','test')")
        self.ledger.opening('paper', raw['ts_code'], 300, '20230615')
        self.plan('sell', 300, '20230616', '20230619', symbol=raw['ts_code'])
        self.ledger.dividends({raw['ts_code']: [raw]}, raw['ex_date'])
        account = self.ledger.summary('paper')
        self.assertEqual(account['positions'], {raw['ts_code']: 390})
        self.assertEqual(self.ledger.available_to_sell('paper', raw['ts_code'], raw['ex_date']), 390)
        self.assertEqual(account['cash'], '100300.00')
        self.assertEqual(account['plans'][0]['status'], 'expired')
        self.assertEqual(account['stock_events'][0]['source'], source['source'])

    def test_odd_remainder_may_sell_once_but_may_not_be_split(self):
        self.ledger.opening('paper', SYMBOL, 390, '20261102')
        for quantity in (90, 100, 190, 200, 290, 300, 390):
            with self.subTest(quantity=quantity):
                plan = self.plan('sell', quantity)
                self.ledger.db.execute('DELETE FROM plans WHERE id=?', (plan,))
                self.ledger.db.commit()
        for quantity in (1, 40, 89, 110, 180, 389, 400):
            with self.subTest(quantity=quantity), self.assertRaises(AppError):
                self.plan('sell', quantity)

    def test_rebalance_rounding_never_buys_a_partial_lot_or_splits_sell_remainder(self):
        for actual, target, expected in ((390, 300, -90), (390, 250, -190), (260, 200, -60),
                                         (90, 0, -90), (390, 400, 0), (390, 500, 100),
                                         (390, 390, 0), (390, 350, -90), (390, 290, -100), (0, 90, 0)):
            with self.subTest(actual=actual, target=target):
                self.assertEqual(rebalance_quantity(actual, target), expected)
        for actual, target in ((True, 100), (100, False), (1.0, 100), (100, '200'), (-1, 0), (0, -1)):
            with self.subTest(actual=actual, target=target), self.assertRaises(AppError):
                rebalance_quantity(actual, target)
        for actual in (0, 1, 90, 100, 260, 390):
            for target in range(0, 501, 37):
                change = rebalance_quantity(actual, target)
                if change > 0:
                    self.assertEqual(change % 100, 0)
                    self.assertLessEqual(actual + change, target)
                elif change < 0:
                    sold = -change
                    self.assertLessEqual(sold, actual)
                    self.assertIn(sold % 100, (0, actual % 100))
                    self.assertLessEqual(actual - sold, target)
                    self.assertFalse(any(q >= actual-target and (q % 100 == 0 or q % 100 == actual % 100)
                                         for q in range(1, sold)))


if __name__ == '__main__':
    unittest.main()
