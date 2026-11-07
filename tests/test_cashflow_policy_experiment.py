import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main, parser
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import ANNUAL_EXPERIMENT, prepare_annual_experiment, protocol, register_experiment, run_experiment
from qmt.forecast_diagnostic import forecast
from qmt.selection import load_rules
from test_backtest import bar
from test_broad_experiment import DAY, END, broad_client, quiet, signals
from test_dividend_policy_experiment import approved


POLICY = 'three_year_coverage'


def cumulative(base=ANNUAL_EXPERIMENT):
    old = approved(base)
    return {**old, 'version': old['version']+'-three-year-fcf', 'cashflow_policy': POLICY}


class CashflowPolicyExperimentTests(unittest.TestCase):
    def test_unknown_or_incompatible_policy_cannot_register_or_overwrite(self):
        cfg = load_config()
        self.assertEqual(protocol(cfg)['cashflow_policy'], 'latest_and_three_year')
        for value in ('unknown', None, [], ''):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with self.assertRaises(AppError):
                    register_experiment(root, END, cfg, {**cumulative(), 'cashflow_policy': value})
                self.assertFalse((root/'backtests').exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(AppError):
                register_experiment(root, END, cfg, {**ANNUAL_EXPERIMENT, 'cashflow_policy': POLICY})
            chosen = approved()
            folder = register_experiment(root, END, cfg, chosen)
            original = (folder/'protocol.json').read_bytes()
            with self.assertRaises(AppError):
                register_experiment(root, END, cfg, {**chosen, 'cashflow_policy': POLICY})
            self.assertEqual((folder/'protocol.json').read_bytes(), original)

    def test_cli_requires_approved_cash_and_preserves_the_other_parameters(self):
        with tempfile.TemporaryDirectory() as tmp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            root = Path(tmp)
            atomic_json(root/'screen.json', {'as_of': END})
            prefix = ['--workspace', str(root), 'backtest']
            for flags in ([], ['--annual-pool', '--all-candidates', '--yield-rank']):
                self.assertEqual(main(prefix+flags+['--three-year-cashflow']), 1)
            run.assert_not_called()
            for long in (False, True):
                flags = ['--annual-pool', '--all-candidates', '--yield-rank', '--approved-annual-cash']
                if long:
                    flags += ['--long-history', '--position-freeze']
                self.assertEqual(main(prefix+flags), 0)
                old = run.call_args.kwargs['experiment']
                self.assertEqual(main(prefix+flags+['--three-year-cashflow']), 0)
                self.assertEqual(run.call_args.kwargs['experiment'],
                                 {**old, 'version': old['version']+'-three-year-fcf', 'cashflow_policy': POLICY})
        self.assertFalse(hasattr(parser().parse_args(['daily']), 'three_year_cashflow'))
        self.assertEqual(parser().parse_args(['forecast-check']).version,
                         'historical-dividend-v33-annual-all-rank-approved-cash-three-year-fcf-equity-limits')

    def test_actual_negative_year_survives_annual_and_monthly_paths_without_creating_cash(self):
        code = '600003.SH'
        client, _ = broad_client(expensive_prefix=False)
        original_dossier = client.dossier.side_effect

        def dossier(*args, **kwargs):
            result = original_dossier(*args, **kwargs)
            if result['symbol'] == code:
                next(r for r in result['data']['cashflow'] if r['end_date'] == '20251231')['c_pay_acq_const_fiolta'] = 2100
            return result

        client.dossier.side_effect = dossier
        client.bars.return_value = {day: bar() for day in (DAY, END)}
        base = {**ANNUAL_EXPERIMENT, 'selection_day': DAY, 'validation_start': DAY, 'development_end': DAY}
        old, chosen = approved(base), cumulative(base)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('qmt.experiment.Tushare', return_value=client), \
                 patch('qmt.experiment.now_cn', return_value=datetime(2026, 11, 7)):
                control = prepare_annual_experiment(root, END, quiet, load_config(), old)
                prepared = prepare_annual_experiment(root, END, quiet, load_config(), chosen)
            self.assertNotIn(code, [s['symbol'] for s in control[2]['stocks']])
            stock = next(s for s in prepared[2]['stocks'] if s['symbol'] == code)
            self.assertEqual(stock['selection_quality']['cashflow_policy'], POLICY)
            self.assertEqual(stock['selection_quality']['cashflow_warning']['simple_fcf'], '-100')
            self.assertEqual(json.loads((prepared[0]/'cohorts'/DAY/'protocol.json').read_text())['cashflow_policy'], POLICY)
            before = copy.deepcopy(prepared[3]['dividends'])
            snapshots, audits = signals(prepared, '0.05', chosen)
            self.assertIn(code, [c['symbol'] for c in snapshots[DAY]['candidates']])
            assessment = next(a for a in audits[0]['assessments'] if a['symbol'] == code)
            self.assertEqual(assessment['cashflow_policy'], POLICY)
            self.assertEqual(assessment['latest_fcf'], '-100')
            self.assertEqual(assessment['three_year_fcf'], '3300')
            self.assertEqual(prepared[3]['dividends'], before)
            with patch('qmt.experiment.prepare_annual_experiment', return_value=prepared), \
                 patch('qmt.experiment.experiment_signals', return_value=(snapshots, audits)), \
                 patch('qmt.comparison.build_comparisons', return_value=[]):
                result = run_experiment(root, END, load_config(), quiet, chosen)
            self.assertEqual(result['status'], 'diagnostic')
            observations = result['diagnostics']['cashflow_policy']['nonpositive_latest_observations']
            self.assertEqual([(o['day'], o['symbol']) for o in observations], [(DAY, code)])
            account = json.loads((prepared[0]/'yield-rank-result.json').read_text())
            self.assertEqual(account['cash_events'], [])
            self.assertTrue(all(p['receivable'] == '0' for p in account['equity_curve']))
            prediction = forecast(stock, prepared[4][code], DAY, load_rules(),
                                  dividend_policy='approved_annual_cash', cashflow_policy=POLICY)
            self.assertEqual(prediction['cashflow_policy'], POLICY)
            with self.assertRaisesRegex(AppError, '证据与冻结候选不一致'):
                forecast(stock, prepared[4][code], DAY, load_rules(), dividend_policy='approved_annual_cash')


if __name__ == '__main__':
    unittest.main()
