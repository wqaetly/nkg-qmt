import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmt.config import AppError, load_config
from qmt.forecast_diagnostic import METHODS, ORIGINS, run_diagnostic
from qmt.report import experiment_history_panel, exit_policy_note, forecast_diagnostic_panel, historical_chart_data, research_panel
from test_capital_policy_experiment import scaled
import test_forecast_ui
import test_rank_ui


NEW = 'known_failure_exit'


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class ExitPolicyUiTests(unittest.TestCase):
    def test_note_distinguishes_known_failures_from_unknown_data_and_industry_freeze(self):
        self.assertEqual(exit_policy_note({}), '')
        self.assertEqual(exit_policy_note({'exit_policy': 'review_freeze'}), '')
        protocol = {'version': 'historical-exit-test', 'exit_policy': NEW}
        page = research_panel({}, {'protocol': protocol, 'status': 'diagnostic'}, registered_protocol=protocol)
        self.assertGreater(page.index('id="exit-policy"'), page.index('<summary>策略规则、来源与完整指标</summary>'))
        for text in ('月末评估', '三年累计 FCF 覆盖不足', '短债覆盖不足', '资料完整校验通过',
                     '仍拒绝买入', '资料缺失或冲突仍冻结', '行业变更或区间未知仍冻结', '允许退出不等于已成交',
                     '不改变年度选股、预测公式或日常模拟'):
            self.assertIn(text, page)
        for registered in (None, {}, {**protocol, 'exit_policy': 'review_freeze'}, {**protocol, 'version': 'other'}):
            self.assertIn('退出规则待核实', exit_policy_note(protocol, registered))
        for value in (None, [], {}, 'unknown', '<img src=x>'):
            page = exit_policy_note({'exit_policy': value})
            self.assertIn('待核实', page)
            self.assertNotIn('<img', page)

    def test_history_and_operations_require_matching_registration_but_no_new_result_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            folder, summary, result = test_rank_ui.RankUiTests().fixture(workspace)
            legacy = historical_chart_data(workspace)
            summary['protocol']['exit_policy'] = NEW
            registered = copy.deepcopy(summary['protocol'])
            paths = {workspace/'backtests/latest.json': summary, folder/'summary.json': summary,
                     folder/'protocol.json': registered}
            for path, value in paths.items(): write(path, value)
            before = {p: p.read_bytes() for p in workspace.rglob('*.json')}
            self.assertEqual(historical_chart_data(workspace), legacy)
            self.assertEqual(historical_chart_data(workspace)['runs'][0]['fills'], result['trades'])
            self.assertIn('已知财务失败月度退出，行业变更仍冻结', experiment_history_panel(workspace))
            self.assertEqual(before, {p: p.read_bytes() for p in workspace.rglob('*.json')})
            for path, original in paths.items():
                for policy in ('review_freeze', 'unknown', None, []):
                    with self.subTest(path=path.name, policy=policy):
                        changed = copy.deepcopy(original)
                        (changed['protocol'] if 'protocol' in changed else changed)['exit_policy'] = policy
                        write(path, changed)
                        self.assertEqual(historical_chart_data(workspace), {})
                        if path.name != 'latest.json':
                            page = experiment_history_panel(workspace)
                            self.assertIn('退出规则待核实', page)
                            self.assertNotIn('6.00%', page)
                        write(path, original)
            write(folder/'protocol.json', {**registered, 'version': 'different'})
            self.assertEqual(historical_chart_data(workspace), {})
            self.assertNotIn('6.00%', experiment_history_panel(workspace))
            (folder/'protocol.json').unlink()
            self.assertEqual(historical_chart_data(workspace), {})
            self.assertIn('退出规则待核实', experiment_history_panel(workspace))
            summary['protocol'].pop('exit_policy')
            for path in (folder/'summary.json', workspace/'backtests/latest.json'): write(path, summary)
            self.assertEqual(historical_chart_data(workspace), legacy)
            summary['protocol']['exit_policy'] = 'review_freeze'
            for path in (folder/'summary.json', workspace/'backtests/latest.json'): write(path, summary)
            self.assertEqual(historical_chart_data(workspace), legacy)

    def test_forecast_matching_checks_all_three_exit_policies(self):
        data = {**test_forecast_ui.ForecastUiTests().data(), 'source_engine_sha256': 'a'*64}
        current = {'source_engine_sha256': 'a'*64, 'protocol': {'version': 'historical-exit-test'}}
        self.assertIn('<summary>独立预测诊断</summary>', forecast_diagnostic_panel(data, current))
        self.assertIn('<summary>独立预测诊断</summary>', forecast_diagnostic_panel({**data, 'exit_policy': 'review_freeze'}, current))
        data['exit_policy'] = current['protocol']['exit_policy'] = NEW
        registered = copy.deepcopy(current['protocol'])
        self.assertIn('<summary>独立预测诊断</summary>', forecast_diagnostic_panel(data, current, registered))
        for value in ('review_freeze', 'unknown', None, []):
            for target in ('forecast', 'summary', 'registered'):
                with self.subTest(target=target, value=value):
                    d, c, r = copy.deepcopy((data, current, registered))
                    {'forecast': d, 'summary': c['protocol'], 'registered': r}[target]['exit_policy'] = value
                    self.assertIn('<summary>其他版本的预测诊断</summary>', forecast_diagnostic_panel(d, c, r))
        for registered in (None, {}, {'version': 'other', 'exit_policy': NEW}):
            self.assertIn('<summary>其他版本的预测诊断</summary>', forecast_diagnostic_panel(data, current, registered))

    def diagnostic_fixture(self, workspace):
        saved = {**scaled(), 'version': 'historical-exit-test', 'account_config': load_config(),
                 'rules': {'max_holdings': 20, 'cash_reserve': '.10'}, 'engine_sha256': 'a'*64, 'replay_end': '20261106'}
        base = workspace/'backtests'/saved['version']
        base.mkdir(parents=True)
        write(base/'protocol.json', saved)
        write(base/'summary.json', {'protocol': saved})
        write(base/'cohort.json', {'rebalances': [{'day': day, 'symbols': ['600001.SH']} for day in ORIGINS]})
        for day in ORIGINS:
            folder = base/'cohorts'/day
            folder.mkdir(parents=True)
            write(folder/'cohort.json', {'selected_on': day, 'stocks': [{'symbol': '600001.SH'}]})
            write(folder/'financials.json', {'600001.SH': {}})
        write(base/'financials.json', {'600001.SH': {'data': {'dividend': []}}})
        return base, saved

    def test_diagnostic_carries_source_policy_without_changing_predictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            base, saved = self.diagnostic_fixture(workspace)
            def predict(stock, dossier, origin, rules, **kwargs):
                self.assertEqual(kwargs, {'dividend_policy': 'approved_annual_cash', 'cashflow_policy': 'three_year_coverage'})
                return {'symbol': stock['symbol'], 'origin': origin, 'target_year': int(origin[:4])+1,
                        'price': '10', 'estimates_dps': {method: '.6' for method in METHODS}}
            outputs = []
            for policy in (None, 'review_freeze', NEW):
                registered = {**saved, **({'exit_policy': policy} if policy else {})}
                write(base/'protocol.json', registered)
                write(base/'summary.json', {'protocol': registered})
                with patch('qmt.forecast_diagnostic.protocol', return_value={'engine_sha256': 'a'*64}), \
                     patch('qmt.forecast_diagnostic.forecast', side_effect=predict):
                    outputs.append(run_diagnostic(workspace, saved['version']))
            self.assertNotIn('exit_policy', outputs[0])
            self.assertNotIn('exit_policy', outputs[1])
            self.assertEqual(outputs[2]['exit_policy'], NEW)
            for result in outputs[1:]:
                self.assertEqual(result['rows'], outputs[0]['rows'])
                self.assertEqual(result['forecast_sha256'], outputs[0]['forecast_sha256'])
            self.assertIn('summary.json', outputs[2]['input_sha256'])
            self.assertTrue(any('行业变更仍冻结' in text for text in outputs[2]['limitations']))
            self.assertFalse((workspace/'ledger.sqlite3').exists())

    def test_invalid_or_mismatched_diagnostic_source_does_not_overwrite_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            base, saved = self.diagnostic_fixture(workspace)
            saved['exit_policy'] = NEW
            dest = workspace/'forecast-diagnostic.json'
            dest.write_text('preserve', encoding='utf-8')
            cases = [(dict(saved, exit_policy=value), {'protocol': saved}) for value in (None, [], 'unknown')]
            cases += [(saved, summary) for summary in ({}, {'protocol': []}, {'protocol': {**saved, 'exit_policy': 'review_freeze'}},
                      {'protocol': {**saved, 'version': 'other'}})]
            cases += [(dict(saved, valuation_policy='historical_context'), {'protocol': saved}),
                      (dict(saved, exit_policy='review_freeze'), {'protocol': saved})]
            for registered, summary in cases:
                write(base/'protocol.json', registered)
                write(base/'summary.json', summary)
                with patch('qmt.forecast_diagnostic.forecast') as predict, self.assertRaises(AppError):
                    run_diagnostic(workspace, saved['version'])
                predict.assert_not_called()
                self.assertEqual(dest.read_text(encoding='utf-8'), 'preserve')
            write(base/'protocol.json', saved)
            (base/'summary.json').unlink()
            with self.assertRaises(AppError): run_diagnostic(workspace, saved['version'])
            self.assertEqual(dest.read_text(encoding='utf-8'), 'preserve')


if __name__ == '__main__':
    unittest.main()
