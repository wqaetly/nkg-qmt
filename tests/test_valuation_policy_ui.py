import copy
import json
import tempfile
import unittest
from pathlib import Path

from qmt.report import experiment_history_panel, forecast_diagnostic_panel, historical_chart_data, research_panel, valuation_policy_note
import test_forecast_ui
import test_rank_ui


class ValuationPolicyUiTests(unittest.TestCase):
    def test_context_note_keeps_data_requirements_and_existing_percentile_evidence(self):
        protocol={'version':'historical-test','valuation_policy':'historical_context'}
        research={'stocks':[{'symbol':'600000.SH','valuation':{'state':'通过','reason':'历史 PE 分位 73.00%'}}]}
        page=research_panel(research,{'protocol':protocol,'status':'diagnostic'},registered_protocol=protocol)
        self.assertGreater(page.index('id="valuation-policy"'),page.index('<summary>策略规则、来源与完整指标</summary>'))
        for text in ('分位仅作参考','超过 50% 不单独否决','正 PE≤25','行业正 PE 中位数','保守盈利 PE≤25',
                     '至少 250 个样本','同行至少 5 家','缺失仍待核实','历史 PE 分位 73.00%','日常规则不变'):
            self.assertIn(text,page)
        self.assertEqual(valuation_policy_note({}),'')
        self.assertEqual(valuation_policy_note({'valuation_policy':'historical_gate'}),'')
        self.assertIn('待核实',valuation_policy_note(protocol))
        self.assertIn('不一致',valuation_policy_note(protocol,{**protocol,'valuation_policy':'historical_gate'}))
        for policy in (None,[],{},'<img src=x>','unknown'):
            note=valuation_policy_note({'valuation_policy':policy})
            self.assertIn('待核实',note);self.assertNotIn('<img',note)

    def test_actual_candles_and_operations_need_matching_archive_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,result=test_rank_ui.RankUiTests().fixture(workspace)
            legacy=historical_chart_data(workspace)
            write=lambda p,v:p.write_text(json.dumps(v),encoding='utf-8')
            summary['protocol']['valuation_policy']='historical_context'
            registered=copy.deepcopy(summary['protocol'])
            paths={workspace/'backtests/latest.json':summary,folder/'summary.json':summary,folder/'protocol.json':registered}
            for p,v in paths.items():write(p,v)
            before={p:p.read_bytes() for p in workspace.rglob('*.json')}
            self.assertEqual(historical_chart_data(workspace),legacy)
            self.assertEqual(historical_chart_data(workspace)['runs'][0]['fills'],result['trades'])
            self.assertIn('历史 PE 分位仅作参考',experiment_history_panel(workspace))
            self.assertEqual(before,{p:p.read_bytes() for p in workspace.rglob('*.json')})
            for path,original in paths.items():
                for policy in ('historical_gate','unknown',None):
                    changed=copy.deepcopy(original)
                    (changed['protocol'] if 'protocol' in changed else changed)['valuation_policy']=policy
                    write(path,changed)
                    self.assertEqual(historical_chart_data(workspace),{})
                    if path.name!='latest.json':
                        history=experiment_history_panel(workspace)
                        self.assertIn('估值规则待核实',history);self.assertNotIn('6.00%',history)
                    write(path,original)
            write(folder/'protocol.json',{**registered,'version':'historical-other-context'})
            history=experiment_history_panel(workspace)
            self.assertIn('估值规则待核实',history);self.assertNotIn('6.00%',history)
            self.assertEqual(historical_chart_data(workspace),{})
            (folder/'protocol.json').unlink()
            self.assertEqual(historical_chart_data(workspace),{})
            self.assertIn('估值规则待核实',experiment_history_panel(workspace))
            summary['protocol'].pop('valuation_policy')
            for p in (folder/'summary.json',workspace/'backtests/latest.json'):write(p,summary)
            self.assertEqual(historical_chart_data(workspace),legacy)
            write(folder/'protocol.json',summary['protocol'])
            summary['protocol'].pop('version');write(folder/'summary.json',summary)
            self.assertIn('6.00%',experiment_history_panel(workspace))

    def test_history_context_requires_nonempty_version_but_keeps_legacy_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder=workspace/'backtests'/'fixture-context';folder.mkdir(parents=True)
            context={'valuation_policy':'historical_context','entry_mode':'yield_rank'}
            cases=[
                ('missing',context,context,False),
                ('null',{**context,'version':None},{**context,'version':None},False),
                ('empty',{**context,'version':''},{**context,'version':''},False),
                ('valid',{**context,'version':'fixture-context'},{**context,'version':'fixture-context'},True),
                ('legacy',{'version':'fixture-legacy'},{},True),
            ]
            for label,registered,protocol,visible in cases:
                with self.subTest(label=label):
                    summary={'status':'diagnostic','protocol':protocol,
                             'primary_metrics':{'cagr':'.1234','average_exposure':'.3456','costs':'78.90'}}
                    for name,data in (('protocol.json',registered),('summary.json',summary)):
                        (folder/name).write_text(json.dumps(data),encoding='utf-8')
                    page=experiment_history_panel(workspace)
                    if visible:
                        for value in ('12.34%','34.56%','78.90'):
                            self.assertIn(value,page)
                        self.assertNotIn('估值规则待核实',page)
                    else:
                        self.assertIn('估值规则待核实',page)
                        self.assertIn('输入待核实</td><td>—</td><td>—</td><td>—',page)
                        for value in ('12.34%','34.56%','78.90'):
                            self.assertNotIn(value,page)

    def test_forecast_match_requires_known_policy_and_matching_registered_context(self):
        data={**test_forecast_ui.ForecastUiTests().data(),'source_engine_sha256':'a'*64}
        current={'source_engine_sha256':'a'*64,'protocol':{'version':'historical-main'}}
        self.assertIn('<summary>独立预测诊断</summary>',forecast_diagnostic_panel(data,current))
        current['protocol']['valuation_policy']='historical_gate'
        self.assertIn('<summary>独立预测诊断</summary>',forecast_diagnostic_panel(data,current))
        data['valuation_policy']='historical_context'
        self.assertIn('<summary>其他版本的预测诊断</summary>',forecast_diagnostic_panel(data,current))
        current['protocol']['valuation_policy']='historical_context';registered=copy.deepcopy(current['protocol'])
        self.assertIn('<summary>独立预测诊断</summary>',forecast_diagnostic_panel(data,current,registered))
        for frozen in (None,{}, {**registered,'version':'different'}, {**registered,'valuation_policy':'historical_gate'}):
            self.assertIn('<summary>其他版本的预测诊断</summary>',forecast_diagnostic_panel(data,current,frozen))
        for policy in ('historical_gate','unknown',None,[]):
            page=forecast_diagnostic_panel({**data,'valuation_policy':policy},current,registered)
            self.assertIn('<summary>其他版本的预测诊断</summary>',page)
            self.assertIn('来源 '+data['source_version'],page)
        for policy in ('unknown',None,[]):
            current['protocol']['valuation_policy']=policy;registered['valuation_policy']=policy
            self.assertIn('<summary>其他版本的预测诊断</summary>',forecast_diagnostic_panel({**data,'valuation_policy':policy},current,registered))


if __name__=='__main__':
    unittest.main()
