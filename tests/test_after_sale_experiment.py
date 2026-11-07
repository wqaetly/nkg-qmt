import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main, parser
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import protocol, register_experiment, run_experiment, validate_experiment
from test_backtest import inputs
from test_capital_policy_experiment import scaled


NEW = 'after_confirmed_sale'


def previous():
    old = scaled()
    return {**old, 'version': old['version'].replace('v33', 'v36')+'-known-failure-exit',
            'exit_policy': 'known_failure_exit'}


def following():
    old = previous()
    return {**old, 'version': old['version'].replace('v36', 'v38')+'-after-sale-rebalance',
            'rebalance_policy': NEW}


class AfterSaleExperimentTests(unittest.TestCase):
    def test_cli_only_changes_version_and_execution_policy_with_complete_flags(self):
        with tempfile.TemporaryDirectory() as tmp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            root = Path(tmp); atomic_json(root/'screen.json', {'as_of': '20261106'})
            prefix = ['--workspace', str(root), 'backtest']
            flags = ['--annual-pool', '--all-candidates', '--yield-rank', '--approved-annual-cash',
                     '--three-year-cashflow', '--equity-limits', '--known-failure-exit']
            for bad in ([], flags[:-1], flags+['--pe-context'], flags+['--earnings-basis', 'ttm'],
                        [flag for flag in flags if flag != '--all-candidates']):
                self.assertEqual(main(prefix+bad+['--after-sale-rebalance']), 1)
                run.assert_not_called()
            for extra in ([], ['--long-history', '--position-freeze']):
                self.assertEqual(main(prefix+flags+extra), 0)
                old = run.call_args.kwargs['experiment']
                self.assertNotIn('rebalance_policy', old)
                self.assertEqual(main(prefix+flags+extra+['--after-sale-rebalance']), 0)
                self.assertEqual(run.call_args.kwargs['experiment'], {
                    **old, 'version': old['version'].replace('historical-dividend-v36', 'historical-dividend-v38', 1)+'-after-sale-rebalance',
                    'rebalance_policy': NEW})
        self.assertFalse(hasattr(parser().parse_args(['daily']), 'after_sale_rebalance'))
        self.assertEqual(parser().parse_args(['forecast-check']).version, scaled()['version'])

    def test_registration_keeps_legacy_shape_and_rejects_unknown_or_incompatible_policy(self):
        cfg = load_config(); old = previous()
        self.assertNotIn('rebalance_policy', protocol(cfg, old))
        explicit = protocol(cfg, {**old, 'rebalance_policy': 'monthly_only'})
        self.assertEqual(explicit, {**protocol(cfg, old), 'rebalance_policy': 'monthly_only'})
        bad = [{**following(), 'rebalance_policy': value} for value in (None, '', [], 'unknown')]
        bad += [{**following(), key: value} for key, value in (
            ('exit_policy', 'review_freeze'), ('valuation_policy', 'historical_context'),
            ('capital_policy', 'fixed_amount'), ('cashflow_policy', 'latest_and_three_year'),
            ('dividend_policy', 'implemented_only'), ('earnings_basis', 'ttm'),
            ('cohort_limit', 20), ('pool_refresh', None), ('entry_mode', 'yield_threshold'))]
        for experiment in bad:
            with self.subTest(experiment=experiment), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(AppError): register_experiment(root, '20261106', cfg, experiment)
                self.assertFalse((root/'backtests').exists())
        for value in (None, [], 'unknown'):
            with self.assertRaises(AppError): protocol(cfg, {**old, 'rebalance_policy': value})
        with self.assertRaises(AppError): protocol(cfg, {**following(), 'exit_policy': 'review_freeze'})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = register_experiment(root, '20261106', cfg, following())
            saved = (folder/'protocol.json').read_bytes()
            self.assertEqual(json.loads(saved)['rebalance_policy'], NEW)
            with self.assertRaises(AppError):
                register_experiment(root, '20261106', cfg, {**following(), 'rebalance_policy': 'monthly_only'})
            self.assertEqual((folder/'protocol.json').read_bytes(), saved)

    def test_simulate_receives_only_new_explicit_policy_and_unchanged_inputs(self):
        data, signals, _ = inputs(); cfg = load_config()
        signals = {day: {'candidates': candidates, 'freeze': []} for day, candidates in signals.items()}
        for policy in (None, 'monthly_only', NEW):
            experiment = following() if policy == NEW else previous()
            if policy == 'monthly_only': experiment['rebalance_policy'] = policy
            validate_experiment(experiment)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); folder = register_experiment(root, data['sessions'][-1], cfg, experiment)
                prepared = (folder, None, {'stocks': []}, data, {}, {}, {}, {}, {}, {})
                with patch('qmt.experiment.prepare_annual_experiment', return_value=prepared), \
                     patch('qmt.experiment.experiment_signals', return_value=(signals, [])), \
                     patch('qmt.comparison.build_comparisons', return_value=[]), \
                     patch('qmt.backtest.simulate', side_effect=AppError('fixture: stop before ledger')) as simulate:
                    summary = run_experiment(root, data['sessions'][-1], cfg, lambda *a, **k: None, experiment)
                self.assertEqual(simulate.call_args.args, (data, signals, cfg))
                self.assertEqual(simulate.call_args.kwargs, {
                    'initial_cash': cfg['paper_cash'], 'max_holdings': 20, 'freeze_policy': 'portfolio',
                    'capital_policy': 'equity_ratio', **({'rebalance_policy': NEW} if policy == NEW else {})})
                self.assertEqual(summary['status'], 'blocked')
                self.assertEqual(summary['protocol'], experiment)
                self.assertEqual(any('原目标买单' in note for note in summary['limitations']), policy == NEW)
                self.assertFalse((root/'ledger.sqlite3').exists())


if __name__ == '__main__': unittest.main()
