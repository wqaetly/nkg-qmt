import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main
from qmt.backtest import simulate
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import (ANNUAL_EXPERIMENT, ANNUAL_LONG_EXPERIMENT, entry_diagnostics,
                            register_experiment, run_experiment)
from test_backtest import bar
from test_broad_experiment import DAY, END, prepare, signals


def ranking(experiment):
    return {**experiment, 'version': experiment['version']+'-rank', 'entry_mode': 'yield_rank',
            'cohort_limit': None, 'entry_yields': ['0.05'], 'primary_entry_yield': '0.05'}


class YieldRankTests(unittest.TestCase):
    def test_rank_keeps_full_eligible_list_and_removes_only_projected_yield_price_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _, codes, experiment = prepare(Path(tmp), True)
            old, old_audits = signals(prepared, '0.05', experiment)
            ranked, audits = signals(prepared, '0.05', ranking(experiment))
        self.assertEqual([c['symbol'] for c in old[DAY]['candidates']], codes[22:])
        candidates = ranked[DAY]['candidates']
        self.assertEqual(len(candidates), 23)
        self.assertEqual([c['symbol'] for c in candidates], codes[22:]+codes[2:22])
        self.assertTrue(all(a['state'] == '排序候选' for a in audits[0]['assessments']))
        self.assertEqual([a['evidence_hash'] for a in audits[0]['assessments']],
                         [a['evidence_hash'] for a in old_audits[0]['assessments']])
        self.assertEqual([a['dimensions'] for a in audits[0]['assessments']],
                         [a['dimensions'] for a in old_audits[0]['assessments']])
        self.assertEqual(ranked[DAY]['freeze'], old[DAY]['freeze'])
        high = next(c for c in candidates if c['symbol'] == codes[2])
        self.assertLess(Decimal(high['estimated_yield']), Decimal('.05'))
        self.assertEqual(high['reference_price'], '20.20')
        audit = audits[0]['assessments'][0]
        self.assertLess(Decimal(audit['valuation_reference_price']), Decimal('20'))
        self.assertEqual(audit['order_limit'], '20.20')
        diagnostics = entry_diagnostics(audits, ranked, 'yield_rank')
        self.assertEqual(diagnostics['entry_funnel'][-1], {'stage': '排序候选', 'count': 23})

    def test_rank_reaches_next_day_execution_without_exceeding_budget_or_twenty_holdings(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _, _, experiment = prepare(Path(tmp), True)
            ranked, _ = signals(prepared, '0.05', ranking(experiment))
        candidates = ranked[DAY]['candidates']
        prices = {c['symbol']: '20' if Decimal(c['estimated_yield']) < Decimal('.05') else '8' for c in candidates}
        data = {'sessions': [DAY, END], 'universe_as_of': DAY, 'dividends': {},
                'industries': {code: code for code in prices},
                'bars': {code: {day: bar(price) for day in (DAY, END)} for code, price in prices.items()}}
        result = simulate(data, ranked, load_config(), max_holdings=20)
        last = result['equity_curve'][-1]
        self.assertEqual(len(last['positions']), 20)
        self.assertGreaterEqual(Decimal(last['cash']), Decimal('10000'))
        self.assertTrue(all(t['trade_date'] == END for t in result['trades']))
        for code, quantity in last['positions'].items():
            self.assertEqual(quantity % 100, 0)
            self.assertLessEqual(Decimal(prices[code])*quantity, Decimal('5000'))
        # Industry and lot failures must still allow candidates after the first twenty.
        for code in list(prices)[:20]:
            data['industries'][code] = 'same industry'
        constrained = simulate(data, ranked, load_config(), max_holdings=20)
        self.assertIn(candidates[20]['symbol'], constrained['equity_curve'][-1]['positions'])
        self.assertLessEqual(sum(Decimal(prices[c])*q for c, q in constrained['equity_curve'][-1]['positions'].items()
                                 if data['industries'][c] == 'same industry'), Decimal('20000'))

    def test_rank_does_not_promote_rejections_missing_data_or_inactive_members(self):
        from qmt.automatic import assess
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _, codes, experiment = prepare(Path(tmp), True)
            for state in ('待数据核实', '等待筛选条件', '排除'):
                def assessment(*args, **kwargs):
                    return {**assess(*args, **kwargs), 'state': state}
                with self.subTest(state=state), patch('qmt.experiment.assess', side_effect=assessment):
                    ranked, audits = signals(prepared, '0.05', ranking(experiment))
                self.assertEqual(ranked[DAY]['candidates'], [])
                self.assertEqual(len(ranked[DAY]['freeze']), 23 if state == '待数据核实' else 0)
                self.assertTrue(all(a['state'] == state for a in audits[0]['assessments']))
            prepared[3]['universe_membership'] = {codes[2]: [{'start': '20261104', 'end': DAY}]}
            ranked, audits = signals(prepared, '0.05', ranking(experiment))
            self.assertNotIn(codes[2], [c['symbol'] for c in ranked[DAY]['candidates']])
            self.assertEqual(audits[0]['assessments'][0]['state'], '退出研究池')

    def test_invalid_rank_protocol_stops_before_registration_or_data_fetch(self):
        experiment = ranking(ANNUAL_EXPERIMENT)
        changes = ({'entry_mode': 'unknown'}, {'entry_mode': None}, {'pool_refresh': 'none'},
                   {'quality_first': False}, {'cohort_limit': 20}, {'retention_yield': '.03'},
                   {'entry_yields': ['0.04', '0.05', '0.06']}, {'primary_entry_yield': '0.04'})
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp, \
                 patch('qmt.experiment.prepare_annual_experiment') as fetch:
                root = Path(tmp)
                for function in (register_experiment, run_experiment):
                    with self.assertRaises(AppError):
                        function(root, END, load_config(), experiment={**experiment, **change})
                self.assertFalse((root/'backtests').exists())
                fetch.assert_not_called()

    def test_cli_requires_full_annual_pool_and_isolates_rank_and_ttm(self):
        with tempfile.TemporaryDirectory() as tmp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, patch('sys.stdout', new_callable=io.StringIO), \
             patch('sys.stderr', new_callable=io.StringIO):
            root = Path(tmp)
            atomic_json(root/'screen.json', {'as_of': END})
            base = ['--workspace', str(root), 'backtest']
            for flags in ([], ['--annual-pool'], ['--all-candidates'], ['--annual-pool', '--all-candidates', '--hold-band']):
                self.assertEqual(main(base+flags+['--yield-rank']), 1)
            run.assert_not_called()
            for flags, expected in (([], ANNUAL_EXPERIMENT), (['--long-history'], ANNUAL_LONG_EXPERIMENT)):
                for basis in ('conservative_minimum', 'ttm'):
                    self.assertEqual(main(base+flags+['--annual-pool', '--all-candidates', '--yield-rank', '--earnings-basis', basis]), 0)
                    chosen = run.call_args.kwargs['experiment']
                    self.assertEqual(chosen['version'], expected['version']+'-all-rank'+('-ttm' if basis == 'ttm' else ''))
                    self.assertEqual(chosen['entry_yields'], ['0.05'])
                    self.assertEqual(chosen['entry_mode'], 'yield_rank')
                    self.assertEqual(chosen['earnings_basis'], basis)

    def test_single_rank_artifact_labels_and_blocked_result_are_separate_from_threshold(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prepared, _, _, experiment = prepare(root, True)
                experiment = {**ranking(experiment), 'development_end': DAY, 'validation_start': END}
                path = root/'backtests'/experiment['version']
                path.mkdir()
                prepared = (path, *prepared[1:])
                prepared[3]['bars'] = {code: {day: bar(str(rows[-1]['close'])) for day in (DAY, END)}
                                       for code, rows in prepared[6].items()}
                if blocked:
                    prepared[3]['errors'] = {'source': 'fixture unresolved'}
                with patch('qmt.experiment.prepare_annual_experiment', return_value=prepared), \
                     patch('qmt.comparison.build_comparisons', return_value=[]):
                    summary = run_experiment(root, END, load_config(), lambda *a, **k: None, experiment)
                self.assertEqual(summary['status'], 'blocked' if blocked else 'diagnostic')
                self.assertEqual(summary['runs'][0]['label'], '股息排序' if blocked else '股息排序 · 全期')
                self.assertTrue((path/'yield-rank-signals.json').exists())
                self.assertEqual((path/'yield-rank-result.json').exists(), not blocked)
                self.assertFalse(list(path.glob('yield-00*-*.json')))
                self.assertFalse(any('全部4%/5%/6%' in line for line in summary['limitations']))
                self.assertFalse(any(r['reason'].startswith('财务、估值和流动性通过') for r in summary['diagnostics']['reasons']))
                if not blocked:
                    self.assertEqual(summary['period_diagnostics']['label'], '股息排序主方案（含估计成本）')
                    self.assertEqual(summary['comparison_series'][0]['label'], '股息排序主方案（含估计成本）')
                    self.assertEqual(json.loads((path/'summary.json').read_text(encoding='utf8')), summary)


if __name__ == '__main__':
    unittest.main()
