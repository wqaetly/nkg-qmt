import copy
import io
import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main
from qmt.automatic import assess, dividend_assessment
from qmt.config import AppError, atomic_json, load_config
from qmt.experiment import (ANNUAL_EXPERIMENT, HOLD_LONG_EXPERIMENT, cohort_assessment,
                            experiment_signals, prepare_annual_experiment, prepare_experiment,
                            register_experiment)
from qmt.research import summarize
from qmt.selection import load_rules
from test_automatic import fixture
from test_broad_experiment import broad_client


def rebuild(row, dossier):
    fresh = summarize(dossier, {'name': row['name'], 'industry': row['industry']},
                      {'trade_date': row['as_of'], 'close': row['price'], 'total_share': '.1', 'dv_ttm': 7.5})
    fresh['selection'] = copy.deepcopy(row['selection'])
    return fresh


def growth_fixture(price='8'):
    row, dossier = fixture(price=price)
    for api, field, value in (('income', 'n_income_attr_p', 1400), ('fina_indicator', 'profit_dedt', 1250)):
        next(r for r in dossier['data'][api] if r['end_date'] == '20260930')[field] = value
    return rebuild(row, dossier), dossier


class EarningsBasisTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules()

    def test_default_preserves_complete_v17_result_and_explicit_legacy_alias(self):
        row, dossier = fixture()
        reason = '机械盈利及派息假设满足门槛；未核查业务、政策及特别分红'
        expected = {'symbol': '600001.SH', 'name': '虚构测试公司', 'industry': '测试行业',
                    'state': '满足价格条件', 'reason': reason,
                    'evidence_hash': '3eea1e9233865cdcaa7f2f18dba224b52b94c4cb526be93da1ffad774c3d328f',
                    'estimated_yield': '0.0675', 'reference_price': '10.80', 'estimated_eps': '0.9',
                    'payout': '0.6', 'fcf_coverage': '2.833333333333333333333333333', 'debt_ratio': '0.1',
                    'scenarios': []}
        for name, dps, target, floor, yield_ in (
                ('悲观', '0.3888', '7.776', '7.77', '0.0486'),
                ('基准', '0.54', '10.8', '10.80', '0.0675'),
                ('较好', '0.594', '11.88', '11.88', '0.07425')):
            expected['scenarios'].append({'scenario': name, 'scenario_dividend_per_share': dps,
                'reference_price_unrounded': target, 'reference_price_2dp_floor': floor,
                'basis': 'hypothetical, pre-tax; not intrinsic value or a buy instruction',
                'scenario_yield_at_price': yield_})
        self.assertEqual(dividend_assessment(row, dossier, self.rules), expected)
        self.assertEqual(dividend_assessment(row, dossier, self.rules, earnings_basis='conservative_minimum'), expected)
        expected['dimensions'] = {
            'valuation': {'state': '通过', 'reason': '测试估值；机械盈利 PE 8.89',
                          'normalized_pe': '8.888888888888888888888888889'},
            'liquidity': {'state': '通过', 'reason': '测试成交额'},
            'dividend': {'state': '满足价格条件', 'reason': reason}}
        self.assertEqual(assess(row, dossier, self.rules), expected)
        self.assertEqual(assess(row, dossier, self.rules, earnings_basis='conservative_minimum'), expected)

    def test_ttm_changes_only_earnings_while_cash_payout_and_financial_ratios_stay_fixed(self):
        row, dossier = growth_fixture()
        original = copy.deepcopy((row, dossier))
        old = dividend_assessment(row, dossier, self.rules)
        new = dividend_assessment(row, dossier, self.rules, earnings_basis='ttm')
        self.assertEqual(old['estimated_eps'], '0.9')
        self.assertEqual(new['estimated_eps'], '1.475')  # 1250 + 900 - 675, divided by 1000 shares.
        self.assertEqual(new['estimated_yield'], '0.110625')
        for key in ('payout', 'fcf_coverage', 'debt_ratio', 'evidence_hash'):
            self.assertEqual(new[key], old[key])
        self.assertEqual((row, dossier), original)

    def test_ttm_decline_remains_binding_in_both_modes(self):
        for parent, deducted, expected in ((400, 300, '0.525'), (300, 600, '0.55')):
            with self.subTest(parent=parent, deducted=deducted):
                row, dossier = fixture()
                next(r for r in dossier['data']['income'] if r['end_date'] == '20260930')['n_income_attr_p'] = parent
                next(r for r in dossier['data']['fina_indicator'] if r['end_date'] == '20260930')['profit_dedt'] = deducted
                row = rebuild(row, dossier)
                old = dividend_assessment(row, dossier, self.rules)
                new = dividend_assessment(row, dossier, self.rules, earnings_basis='ttm')
                self.assertEqual(new['estimated_eps'], expected)
                self.assertEqual(new, old)

    def test_latest_annual_report_uses_annual_ttm_branch(self):
        row, dossier = fixture(day='20260520')
        next(r for r in dossier['data']['income'] if r['end_date'] == '20251231')['n_income_attr_p'] = 2000
        next(r for r in dossier['data']['fina_indicator'] if r['end_date'] == '20251231')['profit_dedt'] = 1800
        row = rebuild(row, dossier)
        self.assertEqual(row['latest_period'], '20251231')
        self.assertEqual(dividend_assessment(row, dossier, self.rules)['estimated_eps'], '0.9')
        self.assertEqual(dividend_assessment(row, dossier, self.rules, earnings_basis='ttm')['estimated_eps'], '1.8')

    def test_nonpositive_and_missing_ttm_never_create_scenarios(self):
        for api, field, zero in (('income', 'n_income_attr_p', 1750), ('fina_indicator', 'profit_dedt', 1575)):
            for kind in ('negative', 'zero', 'missing'):
                with self.subTest(api=api, kind=kind):
                    row, dossier = fixture()
                    previous = next(r for r in dossier['data'][api] if r['end_date'] == '20250930')
                    previous[field] = {'negative': zero + 1, 'zero': zero, 'missing': None}[kind]
                    row = rebuild(row, dossier)
                    result = dividend_assessment(row, dossier, self.rules, earnings_basis='ttm')
                    self.assertEqual(result['state'], '待数据核实' if kind == 'missing' else '排除')
                    self.assertEqual(result['scenarios'], [])
                    self.assertNotIn('estimated_yield', result)

    def test_future_revision_cannot_change_visible_ttm(self):
        row, dossier = growth_fixture()
        before = dividend_assessment(row, dossier, self.rules, earnings_basis='ttm')
        for api, field in (('income', 'n_income_attr_p'), ('fina_indicator', 'profit_dedt')):
            visible = next(r for r in dossier['data'][api] if r['end_date'] == '20260930')
            dossier['data'][api].append({**visible, 'ann_date': '20261110', field: 999999})
        row = rebuild(row, dossier)
        self.assertEqual(dividend_assessment(row, dossier, self.rules, earnings_basis='ttm'), before)

    def test_normalized_pe_uses_selected_eps_and_keeps_25_cap(self):
        row, dossier = growth_fixture(price='30')
        old = assess(row, dossier, self.rules)
        new = assess(row, dossier, self.rules, earnings_basis='ttm')
        self.assertEqual(old['dimensions']['valuation']['state'], '未通过')
        self.assertEqual(new['dimensions']['valuation']['state'], '通过')
        self.assertEqual(Decimal(new['dimensions']['valuation']['normalized_pe']), Decimal('30') / Decimal('1.475'))
        row, dossier = growth_fixture(price='40')
        self.assertEqual(assess(row, dossier, self.rules, earnings_basis='ttm')['dimensions']['valuation']['state'], '未通过')

    def test_cohort_eligibility_keeps_risk_gates_even_when_price_state_changes(self):
        row, dossier = growth_fixture(price='12')
        old = cohort_assessment(row, dossier, self.rules)
        new = cohort_assessment(row, dossier, self.rules, earnings_basis='ttm')
        self.assertEqual((old['state'], new['state']), ('等待价格', '满足价格条件'))
        self.assertTrue(old['eligible'] and new['eligible'])
        for problem in ('five_year_loss', 'cash_coverage', 'payout_over_profit', 'missing_audit'):
            with self.subTest(problem=problem):
                row, dossier = growth_fixture()
                if problem == 'five_year_loss':
                    dossier['data']['fina_indicator'][0]['profit_dedt'] = -1
                elif problem == 'cash_coverage':
                    for r in dossier['data']['cashflow']:
                        r['n_cashflow_act'] = 400
                elif problem == 'payout_over_profit':
                    for r in dossier['data']['dividend']:
                        r['cash_div_tax'] = '1.1'
                else:
                    dossier['data']['fina_audit'][0]['audit_result'] = '保留意见'
                row = rebuild(row, dossier)
                old = cohort_assessment(row, dossier, self.rules)
                new = cohort_assessment(row, dossier, self.rules, earnings_basis='ttm')
                self.assertFalse(new['eligible'])
                self.assertEqual(new, old)

    def test_unknown_mode_fails_before_early_return_and_before_registration(self):
        row, dossier = fixture()
        row['status'] = '排除'
        for function in (dividend_assessment, assess, cohort_assessment):
            for invalid in ('latest', None, ''):
                with self.subTest(function=function.__name__, invalid=invalid), self.assertRaises(AppError):
                    function(row, dossier, self.rules, earnings_basis=invalid)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(AppError):
                register_experiment(root, '20261106', load_config(), {**ANNUAL_EXPERIMENT, 'earnings_basis': 'bad'})
            self.assertFalse((root / 'backtests').exists())

    def test_annual_child_and_monthly_signals_receive_selected_basis_without_changing_membership(self):
        prepared_by_basis = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for basis in ('conservative_minimum', 'ttm'):
                client, _ = broad_client(expensive_prefix=False)
                original_dossier = client.dossier.side_effect
                def dossier(code, as_of, start_year):
                    result = original_dossier(code, as_of, start_year)
                    for api, field, value in (('income', 'n_income_attr_p', 1400), ('fina_indicator', 'profit_dedt', 1250)):
                        next(r for r in result['data'][api] if r['end_date'] == '20260930')[field] = value
                    return result
                client.dossier.side_effect = dossier
                for quote in client.prices.return_value.values():
                    quote['close'] = '12'
                experiment = {**ANNUAL_EXPERIMENT, 'version': ANNUAL_EXPERIMENT['version'] + '-test-' + basis,
                              'selection_day': '20261105', 'validation_start': '20261105',
                              'cohort_limit': None, 'earnings_basis': basis}
                with patch('qmt.experiment.Tushare', return_value=client), \
                     patch('qmt.experiment.now_cn', return_value=datetime(2026, 11, 7)), \
                     patch('qmt.experiment.prepare_experiment', wraps=prepare_experiment) as child:
                    prepared = prepare_annual_experiment(root, '20261106', lambda *a, **k: None, load_config(), experiment)
                self.assertEqual(child.call_args.args[4]['earnings_basis'], basis)
                path, _, cohort, data, dossiers, names, history, amounts, industries, peers = prepared
                child_protocol = json.loads((path / 'cohorts/20261105/protocol.json').read_text(encoding='utf-8'))
                self.assertEqual(child_protocol['earnings_basis'], basis)
                snapshots, _ = experiment_signals(client, cohort, data, dossiers, names, history, amounts,
                    '0.05', industries, peers, lambda *a, **k: None, experiment)
                prepared_by_basis[basis] = (cohort, snapshots)
            old, old_signals = prepared_by_basis['conservative_minimum']
            new, new_signals = prepared_by_basis['ttm']
            self.assertEqual([s['symbol'] for s in old['stocks']], [s['symbol'] for s in new['stocks']])
            self.assertEqual(old['rebalances'], new['rebalances'])
            self.assertEqual(old_signals['20261105']['candidates'], [])
            self.assertEqual(len(new_signals['20261105']['candidates']), 23)
            self.assertTrue(all(Decimal(c['estimated_yield']) == Decimal('.07375') for c in new_signals['20261105']['candidates']))

    def test_cli_preserves_annual_hold_long_all_and_records_isolated_ttm_version(self):
        with tempfile.TemporaryDirectory() as tmp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, patch('sys.stdout', new_callable=io.StringIO), \
             patch('sys.stderr', new_callable=io.StringIO):
            root = Path(tmp)
            atomic_json(root / 'screen.json', {'as_of': '20261106'})
            base = ['--workspace', str(root), 'backtest', '--annual-pool', '--all-candidates', '--hold-band', '--long-history']
            self.assertEqual(main(base + ['--earnings-basis', 'ttm']), 0)
            chosen = run.call_args.kwargs['experiment']
            self.assertEqual(chosen, {**HOLD_LONG_EXPERIMENT, 'version': HOLD_LONG_EXPERIMENT['version'] + '-all-ttm',
                                      'cohort_limit': None, 'earnings_basis': 'ttm'})
            self.assertEqual(main(base), 0)
            self.assertEqual(run.call_args.kwargs['experiment']['earnings_basis'], 'conservative_minimum')
            self.assertNotEqual(run.call_args.kwargs['experiment']['version'], chosen['version'])
            run.reset_mock()
            with self.assertRaises(SystemExit):
                main(base + ['--earnings-basis', 'unknown'])
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
