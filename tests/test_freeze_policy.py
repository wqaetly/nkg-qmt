import copy
import hashlib
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.backtest import simulate
from qmt.config import AppError
from qmt.research import DISCLOSURES
from test_backtest import CODE, DAYS, bar, dividend


OTHER = '600002.SH'
THIRD = '600003.SH'


def candidate(code, day):
    return {'symbol': code, 'reference_price': '9', 'known_on': day}


def fixture():
    data = {'sessions': DAYS, 'universe_as_of': DAYS[0],
            'bars': {code: {day: bar() for day in DAYS} for code in (CODE, OTHER)},
            'industries': {CODE: '甲行业', OTHER: '乙行业'}, 'dividends': {}}
    signals = {DAYS[0]: {'candidates': [candidate(CODE, DAYS[0])], 'freeze': [], 'portfolio_freeze': []},
               DAYS[2]: {'candidates': [candidate(OTHER, DAYS[2])], 'freeze': [CODE], 'portfolio_freeze': []}}
    cfg = {'paper_cash': '10000', 'paper_max_position': '2000', 'paper_max_sector': '4000',
           'slippage_bps': 0, 'commission_rate': '0.0003', 'minimum_commission': '5', 'sell_stamp_rate': '0.001'}
    return data, signals, cfg


def add_stock(data, code, industry='丙行业'):
    data['bars'][code] = {day: bar() for day in DAYS}
    data['industries'][code] = industry


class FreezePolicyTests(unittest.TestCase):
    def test_actual_frozen_holding_stays_fixed_and_position_policy_can_buy_other_stock(self):
        data, signals, cfg = fixture()
        original = copy.deepcopy((data, signals, cfg))
        old = simulate(data, signals, cfg)
        new = simulate(data, signals, cfg, freeze_policy='position')
        self.assertEqual(old['equity_curve'][-1]['positions'], {CODE: 200})
        self.assertEqual(new['equity_curve'][-1]['positions'], {CODE: 200, OTHER: 200})
        self.assertEqual([(t['symbol'], t['side'], t['trade_date']) for t in new['trades']],
                         [(CODE, 'buy', DAYS[1]), (OTHER, 'buy', DAYS[3])])
        self.assertEqual(new['assumptions']['freeze_policy'], 'position')
        self.assertIn('暂停新增买入', old['skipped'][-1]['reason'])
        self.assertEqual((data, signals, cfg), original)

    def test_default_matches_captured_legacy_result_bytes_except_new_assumption(self):
        data, signals, cfg = fixture()
        result = simulate(data, signals, cfg)
        explicit = simulate(data, signals, cfg, freeze_policy='portfolio')
        self.assertEqual(result, explicit)
        self.assertEqual(result['assumptions'].pop('freeze_policy'), 'portfolio')
        self.assertEqual(result['assumptions'].pop('suspension_valuation'), 'verified_full_day_prior_close')
        self.assertEqual(result.pop('valuation_events'), [])
        # Captured from the unchanged engine with this exact deterministic toy fixture.
        self.assertEqual(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
                         'e3bf1fdc1e91dfe203a2191e88765776e4798e1eebfadfc22144401c4ee18305')

    def test_global_frozen_held_stock_still_blocks_all_new_buys(self):
        data, signals, cfg = fixture()
        signals[DAYS[2]]['portfolio_freeze'] = [CODE]
        result = simulate(data, signals, cfg, freeze_policy='position')
        self.assertEqual(result['equity_curve'][-1]['positions'], {CODE: 200})
        self.assertEqual(result['skipped'][-1]['reason'], '既有持仓资料待核实，保留冻结股数并暂停新增买入')

    def test_unheld_global_frozen_name_does_not_block_other_valid_candidates(self):
        data, signals, cfg = fixture()
        add_stock(data, THIRD)
        signals[DAYS[2]].update(freeze=[CODE, THIRD], portfolio_freeze=[THIRD])
        result = simulate(data, signals, cfg, freeze_policy='position')
        self.assertEqual(result['equity_curve'][-1]['positions'], {CODE: 200, OTHER: 200})
        self.assertFalse(any(p['symbol'] == THIRD for p in result['plans']))

    def test_position_policy_requires_valid_explicit_classification_on_every_snapshot(self):
        for value in (None, CODE, {}, [CODE, CODE], [OTHER], [123], [{}], ['bad-code']):
            data, signals, cfg = fixture()
            signals[DAYS[2]]['portfolio_freeze'] = value
            with self.subTest(value=value), self.assertRaises(AppError):
                simulate(data, signals, cfg, freeze_policy='position')
        for day in (DAYS[0], DAYS[2]):
            data, signals, cfg = fixture()
            del signals[day]['portfolio_freeze']
            with self.subTest(missing_day=day), self.assertRaisesRegex(AppError, '明确给出'):
                simulate(data, signals, cfg, freeze_policy='position')
        data, signals, cfg = fixture()
        signals[DAYS[0]] = signals[DAYS[0]]['candidates']
        with self.assertRaisesRegex(AppError, '明确给出'):
            simulate(data, signals, cfg, freeze_policy='position')
        for policy in ('unknown', '', None, [], True):
            with self.subTest(policy=policy), self.assertRaisesRegex(AppError, '未知冻结策略'):
                simulate(data, {}, cfg, freeze_policy=policy)

    def test_twenty_frozen_positions_reserve_all_twenty_slots(self):
        data, signals, cfg = fixture()
        codes = [f'600{i:03d}.SH' for i in range(1, 22)]
        for code in codes:
            add_stock(data, code, industry=code)
        cfg.update(paper_cash='100000', paper_max_position='5000', paper_max_sector='20000')
        signals[DAYS[0]]['candidates'] = [candidate(c, DAYS[0]) for c in codes[:20]]
        signals[DAYS[2]].update(candidates=[candidate(codes[20], DAYS[2])], freeze=codes[:20])
        result = simulate(data, signals, cfg, max_holdings=20, freeze_policy='position')
        first = result['equity_curve'][1]['positions']
        self.assertEqual(len(first), 20)
        self.assertEqual(result['equity_curve'][-1]['positions'], first)
        self.assertFalse(any(p['symbol'] == codes[20] for p in result['plans']))

    def test_frozen_market_value_not_cost_reserves_industry_capacity_without_trimming(self):
        for price, expected_other in (('15', 100), ('20', 0)):
            data, signals, cfg = fixture()
            data['industries'][OTHER] = data['industries'][CODE]
            add_stock(data, THIRD)
            signals[DAYS[2]]['candidates'].append(candidate(THIRD, DAYS[2]))
            for day in DAYS[2:]:
                data['bars'][CODE][day] = bar(price)
            result = simulate(data, signals, cfg, freeze_policy='position')
            positions = result['equity_curve'][-1]['positions']
            with self.subTest(price=price):
                self.assertEqual(positions[CODE], 200)
                self.assertEqual(positions.get(OTHER, 0), expected_other)
                self.assertEqual(positions[THIRD], 200)
                self.assertLessEqual(Decimal(price) * positions[CODE] + 8 * positions.get(OTHER, 0), Decimal('4000'))
                self.assertFalse(any(t['symbol'] == CODE and t['signal_date'] == DAYS[2] for t in result['trades']))

    def test_cash_cannot_include_pending_sell_proceeds_even_if_sale_later_fills(self):
        for execution_price, sale_status in (('20', 'filled'), ('12', 'expired')):
            data, signals, cfg = fixture()
            cfg.update(paper_max_position='9000', paper_max_sector='10000')
            signals[DAYS[2]]['freeze'] = []
            for day in DAYS[2:]:
                data['bars'][CODE][day] = bar('20')
            data['bars'][CODE][DAYS[3]] = bar(execution_price)
            result = simulate(data, signals, cfg, max_holdings=2, freeze_policy='position')
            with self.subTest(sale_status=sale_status):
                self.assertEqual(result['equity_curve'][1]['positions'], {CODE: 500})
                buy = next(p for p in result['plans'] if p['symbol'] == OTHER)
                sell = next(p for p in result['plans'] if p['side'] == 'sell')
                self.assertEqual(buy['quantity'], 500)
                self.assertEqual(sell['status'], sale_status)
                self.assertGreaterEqual(Decimal(result['equity_curve'][-1]['cash']), Decimal(result['equity_curve'][-1]['equity']) * Decimal('.1'))

    def test_pending_buy_reserve_and_fees_reduce_the_next_order(self):
        data, signals, cfg = fixture()
        cfg.update(paper_max_position='9000', paper_max_sector='10000')
        add_stock(data, THIRD)
        for day in DAYS[2:]:
            data['bars'][CODE][day] = bar('30')
        signals[DAYS[2]]['candidates'].append(candidate(THIRD, DAYS[2]))
        result = simulate(data, signals, cfg, max_holdings=3, freeze_policy='position')
        positions = result['equity_curve'][-1]['positions']
        self.assertEqual(positions, {CODE: 300, OTHER: 600, THIRD: 100})
        self.assertGreaterEqual(Decimal(result['equity_curve'][-1]['cash']), Decimal(result['equity_curve'][-1]['equity']) * Decimal('.1'))
        self.assertEqual(result['metrics']['costs'], '15.00')

    def test_signal_or_execution_price_and_industry_gaps_still_halt(self):
        for code, day in ((CODE, DAYS[2]), (OTHER, DAYS[2]), (OTHER, DAYS[3])):
            data, signals, cfg = fixture()
            del data['bars'][code][day]
            with self.subTest(code=code, day=day), self.assertRaises(AppError):
                simulate(data, signals, cfg, freeze_policy='position')
        data, signals, cfg = fixture()
        data['industries'][CODE] = '待核实'
        with self.assertRaises(AppError):
            simulate(data, signals, cfg, freeze_policy='position')

    def test_unfilled_buy_expires_without_inventing_positions_or_later_chasing(self):
        for changed in (bar(volume='0'), bar('12'), {**bar(), 'up': None}, bar(volume='1')):
            data, signals, cfg = fixture()
            data['bars'][OTHER][DAYS[3]] = changed
            result = simulate(data, signals, cfg, freeze_policy='position')
            with self.subTest(changed=changed):
                self.assertEqual(result['equity_curve'][-1]['positions'], {CODE: 200})
                self.assertEqual(next(p for p in result['plans'] if p['symbol'] == OTHER)['status'], 'expired')
                self.assertEqual(len(result['trades']), 1)

    def test_unverified_or_fractional_held_stock_entitlement_still_halts(self):
        data, signals, cfg = fixture()
        event = {**dividend(), 'stk_div': '.4019063'}
        data['dividends'][CODE] = [event]
        with self.assertRaisesRegex(AppError, '未核实上市条款'):
            simulate(data, signals, cfg, freeze_policy='position')
        registration = {'expected': event, 'available_on': '20200120', 'stock_list_date': DAYS[3],
                        'source': 'https://example.com/verified-stock-event.pdf', 'source_sha256': 'a' * 64}
        with patch.dict(DISCLOSURES, {'stock_distributions': [registration]}), self.assertRaisesRegex(AppError, '不足一股'):
            simulate(data, signals, cfg, freeze_policy='position')


if __name__ == '__main__':
    unittest.main()
