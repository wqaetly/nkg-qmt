import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main, parser
from qmt.backtest import simulate
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import ANNUAL_EXPERIMENT, prepare_annual_experiment, protocol, register_experiment, run_experiment
from test_backtest import CODE, DAYS, bar, inputs
from test_broad_experiment import DAY, END, broad_client, quiet, signals as prepare_signals
from test_cashflow_policy_experiment import cumulative


def scaled(base=ANNUAL_EXPERIMENT):
    previous = cumulative({**base, 'version': base['version']+'-all', 'cohort_limit': None})
    return {**previous, 'version': previous['version']+'-equity-limits', 'capital_policy': 'equity_ratio'}


class CapitalPolicyExperimentTests(unittest.TestCase):
    def test_default_results_and_explicit_fixed_policy_are_identical(self):
        data, signals, cfg = inputs()
        original_cfg = copy.deepcopy(cfg)
        implicit = simulate(data, signals, cfg)
        self.assertEqual(implicit, simulate(data, signals, cfg, capital_policy='fixed_amount'))
        self.assertNotIn('capital_policy', implicit['assumptions'])
        self.assertEqual(cfg, original_cfg)
        for policy in (None, '', 'unknown', []):
            with self.subTest(policy=policy), self.assertRaises(AppError):
                simulate(data, signals, cfg, capital_policy=policy)
        with self.assertRaisesRegex(AppError, '配置与回放政策不一致'):
            simulate(data, signals, {**cfg, 'paper_capital_policy': 'equity_ratio'})

    def test_growing_NAV_changes_real_orders_while_frozen_shares_stay_fixed(self):
        data, signals, cfg = inputs()
        cfg.update(paper_cash='100000', paper_max_position='5000', paper_max_sector='20000',
                   minimum_commission='0', commission_rate='0')
        other = '600002.SH'
        data['bars'][CODE] = {day: bar('10' if i<2 else '60') for i, day in enumerate(DAYS)}
        data['bars'][other] = {day: bar('17') for day in DAYS}
        data['industries'][other] = '另一虚构行业'
        signals = {
            DAYS[0]: {'candidates': [{'symbol': CODE, 'reference_price': '11', 'known_on': DAYS[0]}],
                      'freeze': [], 'portfolio_freeze': []},
            DAYS[2]: {'candidates': [{'symbol': other, 'reference_price': '18', 'known_on': DAYS[2]}],
                      'freeze': [CODE], 'portfolio_freeze': []}}
        fixed = simulate(data, signals, cfg, max_holdings=20, freeze_policy='position')
        dynamic = simulate(data, signals, cfg, max_holdings=20, freeze_policy='position', capital_policy='equity_ratio')
        self.assertEqual(fixed['trades'][0], dynamic['trades'][0])
        self.assertEqual(dynamic['equity_curve'][2]['equity'], '120000.00')
        self.assertEqual(fixed['equity_curve'][-1]['positions'], {CODE: 400, other: 200})
        self.assertEqual(dynamic['equity_curve'][-1]['positions'], {CODE: 400, other: 300})
        self.assertEqual([r['side'] for r in dynamic['trades']], ['buy', 'buy'])
        self.assertGreater(Decimal(dynamic['trades'][1]['price'])*dynamic['trades'][1]['quantity'], 5000)
        self.assertEqual(Decimal(dynamic['assumptions']['position_limit_ratio']), Decimal('.05'))
        self.assertEqual(Decimal(dynamic['assumptions']['sector_limit_ratio']), Decimal('.2'))

    def test_fixed_replay_keeps_legacy_subcent_budget_boundaries(self):
        for field in ('paper_max_position', 'paper_max_sector'):
            with self.subTest(field=field):
                data, signals, cfg = inputs()
                cfg[field] = '1615.996'
                if field == 'paper_max_sector':
                    cfg['paper_max_position'] = '1000'
                    other = '600002.SH'
                    data['bars'][other] = copy.deepcopy(data['bars'][CODE])
                    data['industries'][other] = data['industries'][CODE]
                    signals[DAYS[0]].append({**signals[DAYS[0]][0], 'symbol': other})
                fixed = simulate(data, signals, cfg)
                self.assertEqual(fixed, simulate(data, signals, cfg, capital_policy='fixed_amount'))
                self.assertEqual([t['quantity'] for t in fixed['trades']], [100])
                dynamic = simulate(data, signals, cfg, capital_policy='equity_ratio')
                expected = [200] if field == 'paper_max_position' else [100, 100]
                self.assertEqual([t['quantity'] for t in dynamic['trades']], expected)

    def test_falling_NAV_reduces_sector_capacity_without_selling_frozen_shares(self):
        data, _, cfg = inputs()
        cfg.update(minimum_commission='0', commission_rate='0')
        data['bars'][CODE] = {day: bar('8' if i<2 else '2') for i, day in enumerate(DAYS)}
        others = [f'60000{i}.SH' for i in range(2, 7)]
        for code in others:
            data['bars'][code] = {day: bar('7.9') for day in DAYS}
            data['industries'][code] = '同一虚构行业'
        signals = {
            DAYS[0]: {'candidates': [{'symbol': CODE, 'reference_price': '9', 'known_on': DAYS[0]}],
                      'freeze': [], 'portfolio_freeze': []},
            DAYS[2]: {'candidates': [{'symbol': code, 'reference_price': '9', 'known_on': DAYS[2]} for code in others],
                      'freeze': [CODE], 'portfolio_freeze': []}}
        fixed = simulate(data, signals, cfg, max_holdings=6, freeze_policy='position')
        dynamic = simulate(data, signals, cfg, max_holdings=6, freeze_policy='position', capital_policy='equity_ratio')
        self.assertEqual(dynamic['equity_curve'][2]['equity'], '9400.00')
        self.assertEqual(len(fixed['equity_curve'][-1]['positions']), 6)
        self.assertEqual(len(dynamic['equity_curve'][-1]['positions']), 5)
        self.assertEqual(dynamic['equity_curve'][-1]['positions'][CODE], 100)
        self.assertTrue(all(t['side']=='buy' for t in dynamic['trades']))

    def test_initial_cash_override_discloses_the_actual_frozen_ratios(self):
        data, signals, cfg = inputs()
        result = simulate(data, signals, cfg, initial_cash='20000', capital_policy='equity_ratio')
        self.assertEqual(result['initial_cash'], '20000.00')
        self.assertEqual(Decimal(result['assumptions']['position_limit_ratio']), Decimal('.1'))
        self.assertEqual(Decimal(result['assumptions']['sector_limit_ratio']), Decimal('.2'))
        self.assertNotIn('paper_capital_policy', cfg)

    def test_experiment_passes_frozen_policy_to_real_simulation(self):
        client, _ = broad_client(expensive_prefix=False)
        client.bars.return_value = {day: bar() for day in (DAY, END)}
        experiment = scaled({**ANNUAL_EXPERIMENT, 'selection_day': DAY,
                             'development_end': DAY, 'validation_start': DAY})
        cfg = load_config()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch('qmt.experiment.Tushare', return_value=client), \
                 patch('qmt.experiment.now_cn', return_value=datetime(2026, 11, 7)):
                prepared = prepare_annual_experiment(root, END, quiet, cfg, experiment)
            snapshots, audits = prepare_signals(prepared, '0.05', experiment)
            with patch('qmt.experiment.prepare_annual_experiment', return_value=prepared), \
                 patch('qmt.experiment.experiment_signals', return_value=(snapshots, audits)), \
                 patch('qmt.comparison.build_comparisons', return_value=[]):
                summary = run_experiment(root, END, cfg, quiet, experiment)
            result = json.loads((prepared[0]/'yield-rank-result.json').read_text(encoding='utf8'))
            registered = json.loads((prepared[0]/'protocol.json').read_text(encoding='utf8'))
            self.assertEqual(result['assumptions']['capital_policy'], registered['capital_policy'])
            self.assertEqual(registered['capital_policy'], 'equity_ratio')
            self.assertEqual(Decimal(result['assumptions']['position_limit_ratio']), Decimal('.05'))
            self.assertEqual(Decimal(result['assumptions']['sector_limit_ratio']), Decimal('.2'))
            self.assertTrue(result['trades'])
            self.assertEqual(summary['primary_metrics'], result['metrics'])

    def test_registration_rejects_unknown_incompatible_or_changed_policy(self):
        cfg = load_config()
        self.assertEqual(protocol(cfg)['capital_policy'], 'fixed_amount')
        for policy in (None, '', 'unknown', []):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with self.assertRaises(AppError):
                    register_experiment(root, '20261106', cfg, {**scaled(), 'capital_policy': policy})
                self.assertFalse((root/'backtests').exists())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(AppError):
                register_experiment(root, '20261106', cfg, {**ANNUAL_EXPERIMENT, 'capital_policy': 'equity_ratio'})
            with self.assertRaisesRegex(AppError, '配置与登记政策不一致'):
                protocol({**cfg, 'paper_capital_policy': 'fixed_amount'}, scaled())
            folder = register_experiment(root, '20261106', cfg, scaled())
            frozen = (folder/'protocol.json').read_bytes()
            with self.assertRaises(AppError):
                register_experiment(root, '20261106', cfg, {**scaled(), 'capital_policy': 'fixed_amount'})
            self.assertEqual((folder/'protocol.json').read_bytes(), frozen)

    def test_cli_changes_only_the_registered_capital_policy(self):
        with tempfile.TemporaryDirectory() as temp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            root = Path(temp)
            atomic_json(root/'screen.json', {'as_of': '20261106'})
            prefix = ['--workspace', str(root), 'backtest']
            self.assertEqual(main(prefix+['--equity-limits']), 1)
            run.assert_not_called()
            for long in (False, True):
                flags = ['--annual-pool', '--all-candidates', '--yield-rank', '--approved-annual-cash', '--three-year-cashflow']
                if long:
                    flags += ['--long-history', '--position-freeze']
                self.assertEqual(main(prefix+flags), 0)
                original = run.call_args.kwargs['experiment']
                self.assertEqual(main(prefix+flags+['--equity-limits']), 0)
                self.assertEqual(run.call_args.kwargs['experiment'],
                                 {**original, 'version': original['version']+'-equity-limits', 'capital_policy': 'equity_ratio'})
        self.assertFalse(hasattr(parser().parse_args(['daily']), 'equity_limits'))
        self.assertEqual(parser().parse_args(['forecast-check']).version, scaled()['version'])


if __name__ == '__main__':
    unittest.main()
