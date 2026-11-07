import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmt.config import AppError
from qmt.forecast_diagnostic import METHODS, run_diagnostic
from qmt.report import (experiment_history_panel, forecast_diagnostic_panel, historical_chart_data,
                        rebalance_policy_note, research_panel)
from test_exit_policy_ui import write
import test_exit_policy_ui
import test_forecast_ui
import test_rank_ui


NEW = 'after_confirmed_sale'


class AfterSaleUiTests(unittest.TestCase):
    def test_rule_is_new_only_and_requires_matching_registration(self):
        self.assertEqual(rebalance_policy_note({}), '')
        self.assertEqual(rebalance_policy_note({'rebalance_policy': 'monthly_only'}), '')
        protocol = {'version': 'historical-after-sale-test', 'rebalance_policy': NEW}
        page = research_panel({}, {'protocol': protocol, 'status': 'diagnostic'}, registered_protocol=protocol)
        self.assertGreater(page.index('id="rebalance-policy"'), page.index('<summary>策略规则、来源与完整指标</summary>'))
        for text in ('持仓数量上限未创建', '卖单真实成交后', '收盘', '最早再下一交易日',
                     '限价和股数不超过原目标', '不重试未成交买单', '允许补单不等于已经成交'):
            self.assertIn(text, page)
        for registered in (None, {}, {**protocol, 'rebalance_policy': 'monthly_only'}, {**protocol, 'version': 'other'}):
            self.assertIn('补单规则待核实', rebalance_policy_note(protocol, registered))
        for value in (None, '', [], '<img src=x>'):
            note = rebalance_policy_note({'rebalance_policy': value})
            self.assertIn('待核实', note); self.assertNotIn('<img', note)
        for version in (None, ''):
            bad = {**protocol, 'version': version}
            self.assertIn('待核实', rebalance_policy_note(bad, bad))

    def test_chart_operations_require_summary_registration_and_result_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder, summary, result = test_rank_ui.RankUiTests().fixture(root)
            legacy = historical_chart_data(root)
            summary['protocol']['rebalance_policy'] = NEW
            registered = copy.deepcopy(summary['protocol'])
            result['assumptions'] = {'rebalance_policy': NEW}
            paths = {root/'backtests/latest.json': summary, folder/'summary.json': summary,
                     folder/'protocol.json': registered, folder/'yield-rank-result.json': result}
            for path, value in paths.items(): write(path, value)
            before = {p: p.read_bytes() for p in paths}
            chart = historical_chart_data(root)
            self.assertEqual(chart['stocks'], legacy['stocks'])
            self.assertEqual(chart['runs'][0]['fills'], result['trades'])
            self.assertEqual(chart['runs'][0]['plans'], result['plans'])
            self.assertIn('卖出成交后一次补单', chart['runs'][0]['label'])
            self.assertIn('卖出成交后一次补单', experiment_history_panel(root))
            self.assertEqual(before, {p: p.read_bytes() for p in paths})
            for path, original in paths.items():
                for value in ('monthly_only', None, [], 'unknown'):
                    with self.subTest(path=path.name, value=value):
                        changed = copy.deepcopy(original)
                        target = changed.get('protocol', changed.get('assumptions', changed))
                        target['rebalance_policy'] = value
                        write(path, changed)
                        self.assertEqual(historical_chart_data(root), {})
                        if path.name in ('summary.json', 'protocol.json'):
                            page = experiment_history_panel(root)
                            self.assertIn('补单规则待核实', page); self.assertNotIn('6.00%', page)
                        write(path, original)
            for version in (None, '', 'other'):
                write(folder/'protocol.json', {**registered, 'version': version})
                self.assertEqual(historical_chart_data(root), {})
                self.assertNotIn('6.00%', experiment_history_panel(root))
            (folder/'protocol.json').unlink()
            self.assertEqual(historical_chart_data(root), {})
            for path in (folder/'summary.json', root/'backtests/latest.json'):
                old = copy.deepcopy(summary); old['protocol'].pop('rebalance_policy'); write(path, old)
            self.assertEqual(historical_chart_data(root), {})  # New result cannot enter old summary.
            result.pop('assumptions'); write(folder/'yield-rank-result.json', result)
            self.assertEqual(historical_chart_data(root), legacy)
            for path in (folder/'summary.json', root/'backtests/latest.json'):
                old['protocol']['rebalance_policy'] = 'monthly_only'; write(path, old)
            self.assertEqual(historical_chart_data(root), legacy)

    def test_forecast_label_requires_same_execution_policy(self):
        data = {**test_forecast_ui.ForecastUiTests().data(), 'source_engine_sha256': 'a'*64}
        current = {'source_engine_sha256': 'a'*64, 'protocol': {'version': 'historical-after-sale-test'}}
        self.assertIn('<summary>独立预测诊断</summary>', forecast_diagnostic_panel(data, current))
        data['rebalance_policy'] = current['protocol']['rebalance_policy'] = NEW
        registered = copy.deepcopy(current['protocol'])
        self.assertIn('<summary>独立预测诊断</summary>', forecast_diagnostic_panel(data, current, registered))
        for value in ('monthly_only', None, [], 'unknown'):
            for which in ('forecast', 'summary', 'registered'):
                d, c, r = copy.deepcopy((data, current, registered))
                {'forecast': d, 'summary': c['protocol'], 'registered': r}[which]['rebalance_policy'] = value
                self.assertIn('<summary>其他版本的预测诊断</summary>', forecast_diagnostic_panel(d, c, r))
        self.assertIn('<summary>其他版本的预测诊断</summary>', forecast_diagnostic_panel(data, current))

    def test_forecast_policy_provenance_does_not_change_rows_or_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); base, saved = test_exit_policy_ui.ExitPolicyUiTests().diagnostic_fixture(root)
            saved['exit_policy'] = 'known_failure_exit'
            def predict(stock, dossier, origin, rules, **kwargs):
                self.assertEqual(kwargs, {'dividend_policy': 'approved_annual_cash', 'cashflow_policy': 'three_year_coverage'})
                return {'symbol': stock['symbol'], 'origin': origin, 'target_year': int(origin[:4])+1,
                        'price': '10', 'estimates_dps': {method: '.6' for method in METHODS}}
            outputs = []
            for policy in (None, 'monthly_only', NEW):
                registered = {**saved, **({'rebalance_policy': policy} if policy else {})}
                write(base/'protocol.json', registered); write(base/'summary.json', {'protocol': registered})
                with patch('qmt.forecast_diagnostic.protocol', return_value={'engine_sha256': 'a'*64}) as frozen, \
                     patch('qmt.forecast_diagnostic.forecast', side_effect=predict):
                    outputs.append(run_diagnostic(root, saved['version']))
                frozen.assert_called_once_with(registered['account_config'], registered)
            self.assertNotIn('rebalance_policy', outputs[0]); self.assertNotIn('rebalance_policy', outputs[1])
            self.assertEqual(outputs[2]['rebalance_policy'], NEW)
            for result in outputs[1:]:
                self.assertEqual(result['rows'], outputs[0]['rows'])
                self.assertEqual(result['forecast_sha256'], outputs[0]['forecast_sha256'])
            comparable = lambda value: {k: v for k, v in value.items() if k not in ('input_sha256', 'rebalance_policy')}
            self.assertEqual(comparable(outputs[0]), comparable(outputs[1]))
            self.assertEqual(comparable(outputs[0]), comparable(outputs[2]))
            good = {**saved, 'rebalance_policy': NEW}
            cases = [({**good, 'rebalance_policy': value}, {'protocol': good}) for value in (None, [], 'unknown', 'monthly_only')]
            cases += [(good, {'protocol': {**good, 'rebalance_policy': 'monthly_only'}}),
                      ({**good, 'exit_policy': 'review_freeze'}, {'protocol': good})]
            dest = root/'forecast-diagnostic.json'; before = dest.read_bytes()
            for registered, summary in cases:
                write(base/'protocol.json', registered); write(base/'summary.json', summary)
                with patch('qmt.forecast_diagnostic.forecast') as forecast, self.assertRaises(AppError):
                    run_diagnostic(root, saved['version'])
                forecast.assert_not_called(); self.assertEqual(dest.read_bytes(), before)
            self.assertFalse((root/'ledger.sqlite3').exists())


if __name__ == '__main__': unittest.main()
