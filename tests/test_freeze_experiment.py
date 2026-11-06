import copy
import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import (ANNUAL_EXPERIMENT, ANNUAL_LONG_EXPERIMENT,
                            holding_freeze_diagnostics, register_experiment, run_experiment)
from test_backtest import bar
from test_broad_experiment import DAY, END, prepare, quiet, signals
from test_yield_rank import ranking


def position(experiment):
    return {**experiment, 'version': experiment['version'] + '-position-freeze', 'freeze_policy': 'position'}


SKIP = '既有持仓资料待核实，保留冻结股数并暂停新增买入'


def diagnostic_fixture(policy='position'):
    a, b, unheld, zero_quantity = '600001.SH', '600002.SH', '600003.SH', '600004.SH'
    days = ['20260130', '20260227', '20260331']
    snapshots = {days[0]: {'candidates': [], 'freeze': [a, b, unheld, zero_quantity], 'portfolio_freeze': [b, unheld]},
                 days[1]: {'candidates': [], 'freeze': [a, unheld], 'portfolio_freeze': [unheld]},
                 days[2]: {'candidates': [], 'freeze': [unheld], 'portfolio_freeze': [unheld]}}
    result = {'assumptions': {'freeze_policy': policy},
              'equity_curve': [{'day': days[0], 'positions': {a: 100, b: 200, zero_quantity: 0}, 'equity': '10000'},
                               {'day': days[1], 'positions': {a: 100}, 'equity': '12000'},
                               {'day': days[2], 'positions': {a: 100}, 'equity': '12000'}],
              'skipped': [{'day': days[0], 'symbol': c, 'reason': SKIP} for c in ('600005.SH', '600006.SH')]
                         + [{'day': days[1], 'symbol': '600005.SH', 'reason': '目标预算不足一手或行业额度不足'}]}
    audits = [{'day': day, 'assessments': [{'symbol': c, 'state': '待数据核实', 'reason': '单股财务缺口' if c == a else '身份待核实',
                                          'active': c != b} for c in snapshot['freeze']]}
              for day, snapshot in snapshots.items()]
    data = {'bars': {a: {days[0]: {'close': '10'}, days[1]: {'close': '12'}}, b: {days[0]: {'close': '20'}}}}
    return result, snapshots, audits, data


class FreezeExperimentTests(unittest.TestCase):
    def test_invalid_policy_or_incompatible_protocol_is_rejected_before_registration_and_fetch(self):
        experiment = position(ranking(ANNUAL_EXPERIMENT))
        changes = ({'freeze_policy': 'unknown'}, {'freeze_policy': None}, {'freeze_policy': []},
                   {'entry_mode': 'yield_threshold'}, {'pool_refresh': 'none'}, {'cohort_limit': 20},
                   {'retention_yield': '.03'})
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp, \
                 patch('qmt.experiment.prepare_annual_experiment') as fetch:
                root = Path(tmp)
                for function in (register_experiment, run_experiment):
                    with self.assertRaises(AppError):
                        function(root, END, load_config(), experiment={**experiment, **change})
                self.assertFalse((root / 'backtests').exists())
                fetch.assert_not_called()

    def test_cli_dependencies_and_four_separate_versions_with_default_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, patch('sys.stdout', new_callable=io.StringIO), \
             patch('sys.stderr', new_callable=io.StringIO):
            root = Path(tmp)
            atomic_json(root / 'screen.json', {'as_of': END})
            base = ['--workspace', str(root), 'backtest']
            for flags in ([], ['--annual-pool'], ['--annual-pool', '--all-candidates'], ['--yield-rank'],
                          ['--all-candidates', '--yield-rank'], ['--annual-pool', '--yield-rank'],
                          ['--annual-pool', '--all-candidates', '--yield-rank', '--hold-band']):
                self.assertEqual(main(base + flags + ['--position-freeze']), 1)
            run.assert_not_called()
            for extra, expected in (([], ANNUAL_EXPERIMENT), (['--long-history'], ANNUAL_LONG_EXPERIMENT)):
                versions = set()
                for local in (False, True):
                    for basis in ('conservative_minimum', 'ttm'):
                        flags = ['--annual-pool', '--all-candidates', '--yield-rank', '--earnings-basis', basis]
                        if local:
                            flags.append('--position-freeze')
                        self.assertEqual(main(base + extra + flags), 0)
                        chosen = run.call_args.kwargs['experiment']
                        version = expected['version'] + '-all-rank' + ('-position-freeze' if local else '') + ('-ttm' if basis == 'ttm' else '')
                        self.assertEqual(chosen['version'], version)
                        self.assertEqual(chosen['freeze_policy'], 'position' if local else 'portfolio')
                        self.assertEqual(chosen['earnings_basis'], basis)
                        self.assertEqual(chosen['entry_yields'], ['0.05'])
                        versions.add(version)
                self.assertEqual(len(versions), 4)

    def test_real_signal_classification_preserves_all_candidates_audits_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared, _, codes, experiment = prepare(Path(tmp), True)
            finance, identity, industry_missing, industry_changed, retired_finance = codes[2:7]
            dossiers, names, industries = prepared[4], prepared[5], prepared[8]
            for code in (finance, retired_finance):
                dossiers[code]['data']['balancesheet'][-1]['st_borr'] = None
            names[identity] = []
            industries[industry_missing] = []
            industries[industry_changed][0]['l1_name'] = '已发生行业变化'
            prepared[3]['universe_membership'] = {retired_finance: [{'start': '20261104', 'end': DAY}]}
            before = copy.deepcopy(prepared[2:])
            for basis in ('conservative_minimum', 'ttm'):
                with self.subTest(basis=basis):
                    control = {**ranking(experiment), 'earnings_basis': basis}
                    old, old_audits = signals(prepared, '0.05', control)
                    new, new_audits = signals(prepared, '0.05', position(control))
                    self.assertEqual(new_audits, old_audits)
                    self.assertEqual({day: {k: v for k, v in s.items() if k != 'portfolio_freeze'}
                                      for day, s in new.items()}, old)
                    self.assertEqual(set(new[DAY]['freeze']), {finance, identity, industry_missing, industry_changed, retired_finance})
                    self.assertEqual(set(new[DAY]['portfolio_freeze']), {identity, industry_missing, industry_changed})
                    by_symbol = {a['symbol']: a for a in new_audits[0]['assessments']}
                    self.assertEqual(by_symbol[finance]['financial_gaps'][0]['field'], 'st_borr')
                    self.assertFalse(by_symbol[retired_finance]['active'])
                    self.assertEqual(by_symbol[retired_finance]['state'], '待数据核实')
                    self.assertNotIn(retired_finance, [c['symbol'] for c in new[DAY]['candidates']])
            self.assertEqual(prepared[2:], before)

    def test_diagnostics_counts_actual_held_intersections_and_latest_zero(self):
        for policy, blocked_months in (('position', 1), ('portfolio', 2)):
            args = diagnostic_fixture(policy)
            original = copy.deepcopy(args)
            actual = holding_freeze_diagnostics(*args, policy)
            with self.subTest(policy=policy):
                self.assertEqual((actual['months'], actual['frozen_months'], actual['company_months']), (3, 2, 3))
                self.assertEqual(actual['portfolio_blocked_months'], blocked_months)
                self.assertEqual((actual['buy_skip_months'], actual['buy_skips']), (1, 2))
                self.assertEqual(actual['latest'], {'day': '20260331', 'held_frozen': 0, 'portfolio_blocked': False, 'buy_skips': 0})
                self.assertEqual([(r['symbol'], r['quantity'], Decimal(r['market_value']), Decimal(r['weight'])) for r in actual['rows']],
                                 [('600001.SH', 100, Decimal('1000'), Decimal('.1')),
                                  ('600002.SH', 200, Decimal('4000'), Decimal('.4')),
                                  ('600001.SH', 100, Decimal('1200'), Decimal('.1'))])
                self.assertFalse(actual['rows'][1]['active'])
                self.assertEqual([r['scope'] for r in actual['rows']],
                                 ['position', 'portfolio', 'position'] if policy == 'position' else ['portfolio'] * 3)
                self.assertEqual(args, original)

    def test_diagnostics_rejects_policy_mismatch_and_known_research_state(self):
        for policy, stored in (('unknown', 'unknown'), ('position', 'portfolio'), ('portfolio', 'position'), ('portfolio', None)):
            args = diagnostic_fixture()
            args[0]['assumptions'] = {'freeze_policy': stored} if stored else {}
            with self.subTest(policy=policy, stored=stored), self.assertRaisesRegex(AppError, '冻结范围一致'):
                holding_freeze_diagnostics(*args, policy)
        args = diagnostic_fixture()
        args[2][0]['assessments'][0]['state'] = '排除'
        with self.assertRaisesRegex(AppError, '研究状态不一致'):
            holding_freeze_diagnostics(*args, 'position')

    def test_global_buy_skip_must_belong_to_an_actually_blocked_signal(self):
        for day in ('20260227', '20260430'):
            args = diagnostic_fixture()
            args[0]['skipped'].append({'day': day, 'symbol': '600005.SH', 'reason': SKIP})
            with self.subTest(day=day), self.assertRaises(AppError):
                holding_freeze_diagnostics(*args, 'position')

    def test_no_signal_has_no_latest_snapshot(self):
        result = {'assumptions': {'freeze_policy': 'portfolio'}, 'equity_curve': [], 'skipped': []}
        actual = holding_freeze_diagnostics(result, {}, [], {}, 'portfolio')
        self.assertEqual(actual, {'policy': 'portfolio', 'months': 0, 'frozen_months': 0,
                                 'portfolio_blocked_months': 0, 'buy_skip_months': 0, 'buy_skips': 0,
                                 'company_months': 0, 'latest': None, 'rows': []})

    def test_failed_actual_freeze_validation_cannot_publish_success_metrics_curve_or_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared, _, _, experiment = prepare(root, True)
            experiment = {**position(ranking(experiment)), 'development_end': DAY, 'validation_start': END}
            path = root / 'backtests' / experiment['version']
            path.mkdir()
            prepared = (path, *prepared[1:])
            prepared[3]['bars'] = {code: {day: bar(str(rows[-1]['close'])) for day in (DAY, END)}
                                   for code, rows in prepared[6].items()}
            with patch('qmt.experiment.prepare_annual_experiment', return_value=prepared), \
                 patch('qmt.comparison.build_comparisons', return_value=[]), \
                 patch('qmt.experiment.holding_freeze_diagnostics', side_effect=AppError('fixture freeze mismatch')) as diagnosis:
                result = run_experiment(root, END, load_config(), quiet, experiment)
            diagnosis.assert_called_once()
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['primary_metrics'], {})
            self.assertEqual(result['period_diagnostics'], {})
            self.assertEqual(result['comparison_series'], [])
            self.assertTrue(all(r['status'] == 'blocked' and not r['metrics'] for r in result['runs']))
            self.assertFalse(list(path.glob('*-result.json')))
            self.assertTrue((path / 'yield-rank-signals.json').exists())


if __name__ == '__main__':
    unittest.main()
