import copy
import io
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main
from qmt.backtest import simulate
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import (ANNUAL_EXPERIMENT, ANNUAL_LONG_EXPERIMENT, HOLD_EXPERIMENT,
                            HOLD_LONG_EXPERIMENT, experiment_signals, prepare_annual_experiment,
                            prepare_experiment, protocol)
from qmt.selection import load_rules
from test_backtest import bar
from test_lab import quality_client


DAY, END = '20261105', '20261106'


def quiet(*args, **kwargs):
    pass


def broad_client(expensive_prefix=True):
    codes = [f'600{i:03d}.SH' for i in range(1, 26)]
    client = quality_client(codes)
    quotes = client.prices.return_value
    for index, code in enumerate(codes, 1):
        quotes[code].update(dv_ttm=str(Decimal('20') - Decimal(index) / 10),
                            close='20' if expensive_prefix and 3 <= index <= 22 else '8')
    old_yearly = client.yearly.side_effect
    sessions = client.calendar('20260901', DAY)
    today = datetime.strptime(DAY, '%Y%m%d')
    history = [(today - timedelta(days=i)).strftime('%Y%m%d') for i in range(1, 400)
               if (today - timedelta(days=i)).weekday() < 5][:260]

    def yearly(api, code, start, end, fields):
        if api == 'daily_basic':
            return [{**quotes[code], 'trade_date': day, 'pe_ttm': '20'} for day in history] + [quotes[code]]
        if api == 'daily':
            return [{'ts_code': code, 'trade_date': day, 'amount': 100000} for day in sessions]
        return old_yearly(api, code, start, end, fields)

    client.yearly.side_effect = yearly
    return client, codes


def experiment_protocol(broad):
    return {**ANNUAL_EXPERIMENT, 'selection_day': DAY, 'validation_start': DAY,
            'version': ANNUAL_EXPERIMENT['version'] + ('-all-test' if broad else '-cap-test'),
            **({'cohort_limit': None} if broad else {})}


def prepare(root, broad, expensive_prefix=True, annual=False):
    client, codes = broad_client(expensive_prefix)
    experiment = experiment_protocol(broad)
    function = prepare_annual_experiment if annual else prepare_experiment
    with patch('qmt.experiment.Tushare', return_value=client), \
         patch('qmt.experiment.now_cn', return_value=datetime(2026, 11, 7)):
        result = function(root, END, quiet, load_config(), experiment)
    return result, client, codes, experiment


def signals(prepared, rate, experiment):
    _, client, cohort, data, dossiers, names, history, amounts, industries, peers = prepared
    return experiment_signals(client, cohort, data, dossiers, names, history, amounts,
                              rate, industries, peers, quiet, experiment)


class BroadExperimentTests(unittest.TestCase):
    def test_complete_scan_preserves_legacy_prefix_and_exposes_later_entry_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, old_client, codes, old_protocol = prepare(root, False)
            broad, client, _, experiment = prepare(root, True)
            old_cohort, cohort = old[2], broad[2]
            self.assertEqual(len(old_cohort['stocks']), 20)
            self.assertEqual(len(cohort['stocks']), 23)
            self.assertEqual(old_cohort['stocks'], cohort['stocks'][:20])
            self.assertEqual((old_cohort['candidate_count'], old_cohort['evaluated_count']), (25, 22))
            self.assertEqual((cohort['candidate_count'], cohort['evaluated_count']), (25, 25))
            self.assertIsNone(cohort['target_count'])
            self.assertEqual(cohort['holding_limit'], 20)
            self.assertEqual([row['state'] for row in cohort['rejected']], ['待数据核实', '排除'])
            self.assertEqual([call.args[0] for call in client.dossier.call_args_list if call.args[1] == DAY], codes)
            self.assertEqual([call.args[0] for call in old_client.dossier.call_args_list if call.args[1] == DAY], codes[:22])
            self.assertEqual(set(broad[3]['bars']), set(codes[2:]))
            self.assertFalse(broad[3]['errors'])
            # All three thresholds share the financial cohort; complete entry checks happen later.
            for rate in experiment['entry_yields']:
                with self.subTest(rate=rate):
                    old_signals, _ = signals(old, rate, old_protocol)
                    new_signals, _ = signals(broad, rate, experiment)
                    self.assertEqual(old_signals[DAY]['candidates'], [])
                    self.assertEqual([r['symbol'] for r in new_signals[DAY]['candidates']], codes[22:])
            self.assertEqual(protocol(load_config(), old_protocol, END)['rules'],
                             protocol(load_config(), experiment, END)['rules'])
            self.assertEqual(protocol(load_config(), old_protocol, END)['account_config'],
                             protocol(load_config(), experiment, END)['account_config'])
            with patch('qmt.experiment.Tushare', return_value=client), self.assertRaisesRegex(AppError, '协议已变化'):
                prepare_experiment(root, END, quiet, load_config(), {**old_protocol, 'cohort_limit': None})

    def test_parent_cohort_reports_complete_coverage_without_a_twenty_stock_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _, codes, _ = prepare(Path(tmp), True, annual=True)
            cohort, data = prepared[2:4]
            self.assertIsNone(cohort['target_count'])
            self.assertEqual(cohort['holding_limit'], load_rules()['max_holdings'])
            self.assertEqual(len(cohort['stocks']), 23)
            self.assertEqual(cohort['rebalances'][0]['evaluated_count'], 25)
            self.assertEqual(cohort['rebalances'][0]['candidate_count'], 25)
            self.assertEqual(cohort['rebalances'][0]['symbols'], codes[2:])
            self.assertEqual(set(data['universe_membership']), set(codes[2:]))

    def test_all_entry_candidates_reach_allocator_and_portfolio_constraints_still_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _, _, experiment = prepare(Path(tmp), True, expensive_prefix=False)
            snapshots, _ = signals(prepared, '0.05', experiment)
        candidates = snapshots[DAY]['candidates']
        self.assertEqual(len(candidates), 23)
        codes = [row['symbol'] for row in candidates]
        cfg = {**load_config(), 'slippage_bps': 0}
        self.assertEqual(tuple(Decimal(cfg[key]) for key in ('paper_cash', 'paper_max_position', 'paper_max_sector')),
                         (Decimal('100000'), Decimal('5000'), Decimal('20000')))
        for scenario in ('diverse', 'sector_limit', 'expensive_lots'):
            with self.subTest(scenario=scenario):
                prices = {code: '100' if scenario == 'expensive_lots' and index < 20 else '8'
                          for index, code in enumerate(codes)}
                industries = {code: 'same' if scenario == 'sector_limit' and index < 20 else code
                              for index, code in enumerate(codes)}
                data = {'sessions': [DAY, END], 'universe_as_of': DAY,
                        'bars': {code: {day: bar(prices[code]) for day in (DAY, END)} for code in codes},
                        'industries': industries, 'dividends': {}}
                snapshot = copy.deepcopy(snapshots[DAY])
                if scenario == 'expensive_lots':
                    for candidate in snapshot['candidates'][:20]:
                        candidate['reference_price'] = '150'
                result = simulate(data, {DAY: snapshot}, cfg, max_holdings=20)
                last = result['equity_curve'][-1]
                positions = last['positions']
                self.assertEqual(result['initial_cash'], '100000.00')
                self.assertLessEqual(len(positions), 20)
                self.assertGreaterEqual(Decimal(last['cash']), Decimal('10000'))
                sector_values = {}
                for code, quantity in positions.items():
                    self.assertEqual(quantity % 100, 0)
                    value = Decimal(prices[code]) * quantity
                    self.assertLessEqual(value, Decimal('5000'))
                    sector_values[industries[code]] = sector_values.get(industries[code], Decimal(0)) + value
                self.assertTrue(all(value <= Decimal('20000') for value in sector_values.values()))
                if scenario == 'diverse':
                    self.assertEqual(len(positions), 20)
                    self.assertEqual(set(positions), set(codes[:20]))
                else:
                    self.assertIn(codes[20], positions)
                    self.assertTrue(any('一手或行业额度不足' in skip['reason'] for skip in result['skipped']))
                self.assertTrue(all(trade['trade_date'] == END for trade in result['trades']))

    def test_cli_requires_annual_pool_and_preserves_hold_long_and_legacy_protocols(self):
        cases = [([], ANNUAL_EXPERIMENT), (['--long-history'], ANNUAL_LONG_EXPERIMENT),
                 (['--hold-band'], HOLD_EXPERIMENT),
                 (['--hold-band', '--long-history'], HOLD_LONG_EXPERIMENT)]
        with tempfile.TemporaryDirectory() as tmp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, patch('sys.stdout', new_callable=io.StringIO), \
             patch('sys.stderr', new_callable=io.StringIO) as stderr:
            root = Path(tmp)
            atomic_json(root / 'screen.json', {'as_of': END})
            base = ['--workspace', str(root), 'backtest']
            for extra in ([], ['--quality-first'], ['--long-history']):
                self.assertEqual(main(base + ['--all-candidates'] + extra), 1)
            self.assertIn('--all-candidates 须与 --annual-pool 同用', stderr.getvalue())
            run.assert_not_called()
            for flags, expected in cases:
                with self.subTest(flags=flags):
                    self.assertEqual(main(base + ['--annual-pool', '--all-candidates'] + flags), 0)
                    chosen = run.call_args.kwargs['experiment']
                    self.assertEqual(chosen, {**expected, 'version': expected['version'] + '-all', 'cohort_limit': None})
                    self.assertEqual(run.call_args.args[1], END)
                    self.assertEqual(Decimal(run.call_args.args[2]['paper_cash']), Decimal('100000'))
                    self.assertEqual(main(base + ['--annual-pool'] + flags), 0)
                    self.assertEqual(run.call_args.kwargs['experiment'], expected)
                    self.assertNotIn('cohort_limit', expected)


if __name__ == '__main__':
    unittest.main()
