import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from qmt.report import chart_panel, entry_mode_label, experiment_history_panel, historical_chart_data, research_panel


class RankUiTests(unittest.TestCase):
    def fixture(self, workspace):
        folder=workspace/'backtests'/'historical-dividend-v21-annual-all-rank'
        folder.mkdir(parents=True)
        protocol={'version':folder.name,'entry_mode':'yield_rank','earnings_basis':'conservative_minimum'}
        summary={'protocol':protocol,'status':'diagnostic',
                 'coverage':{'start':'20250731','end':'20250804','stocks':1},
                 'runs':[{'label':'股息排序 · 全期','status':'diagnostic','metrics':{'cagr':'.06'}},
                         {'label':'股息排序 · 截至2024开发段','status':'diagnostic','metrics':{'cagr':'.3'}},
                         {'label':'5%股息门槛 · 全期','status':'diagnostic','metrics':{'cagr':'.9'}}]}
        result={'start':'20250731','end':'20250804',
                'plans':[{'id':1,'symbol':'600000.SH','side':'buy','signal_date':'20250731','execute_date':'20250801','quantity':100,'limit_price':'8.2','status':'filled','note':'股息排序'}],
                'trades':[{'id':1,'plan_id':1,'symbol':'600000.SH','side':'buy','trade_date':'20250801','quantity':100,'price':'8.18','fee':'5.00'}]}
        write=lambda path,data:path.write_text(json.dumps(data),encoding='utf-8')
        write(folder/'summary.json',summary);write(workspace/'backtests/latest.json',summary)
        write(folder/'market.json',{'bars':{'600000.SH':{day:{'open':8,'high':9,'low':7,'close':8.2,'vol':100} for day in ('20250731','20250801','20250804')}}})
        write(folder/'cohort.json',{'stocks':[{'symbol':'600000.SH','name':'测试公司'}]})
        for key in ('rank','004','005','006'):
            write(folder/f'yield-{key}-result.json',result)
        return folder,summary,result

    def test_only_rank_file_and_source_are_loaded_even_when_old_rate_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,result=self.fixture(workspace)
            before={p:p.read_bytes() for p in workspace.rglob('*.json')}
            history=historical_chart_data(workspace)
            self.assertEqual([run['id'] for run in history['runs']],['rank'])
            self.assertEqual(history['runs'][0]['label'],'本次历史 股息排序 · 主方案')
            self.assertEqual(history['runs'][0]['fills'],result['trades'])
            page=chart_panel(workspace,{'as_of':'20250804','source':'Tushare','rows':[]},{'focus_symbols':[]})
            self.assertIn('<option value="rank">历史主方案</option>',page)
            for wrong in ('<option value="004">','<option value="005">','<option value="006">','本次历史 5%'):
                self.assertNotIn(wrong,page)
            payload=json.loads(re.search(r'id="market-chart-data">(.*?)</script>',page,re.S)[1])
            self.assertEqual(len(payload['historical']['runs']),1)
            self.assertEqual(payload['fills'],[])
            self.assertEqual(before,{p:p.read_bytes() for p in workspace.rglob('*.json')})

    def test_missing_blocked_unknown_or_mismatched_rank_never_falls_back_to_005(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,result=self.fixture(workspace)
            result_path=folder/'yield-rank-result.json'
            result_path.unlink()
            self.assertEqual(historical_chart_data(workspace),{})
            result_path.write_text(json.dumps(result),encoding='utf-8')
            for change in ({'status':'blocked'},
                           {'runs':[{'label':'股息排序','status':'blocked','metrics':{}},summary['runs'][-1]]},
                           {'protocol':{**summary['protocol'],'entry_mode':'unknown'}}):
                altered={**summary,**change}
                (folder/'summary.json').write_text(json.dumps(altered),encoding='utf-8')
                self.assertEqual(historical_chart_data(workspace),{})
            (folder/'summary.json').write_text(json.dumps(summary),encoding='utf-8')
            (workspace/'backtests/latest.json').write_text(json.dumps({**summary,'protocol':{**summary['protocol'],'entry_mode':'yield_threshold'}}),encoding='utf-8')
            self.assertEqual(historical_chart_data(workspace),{})

    def test_history_distinguishes_entry_modes_and_uses_rank_full_period_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,_=self.fixture(workspace)
            old=workspace/'backtests'/'historical-dividend-v20-annual-all';old.mkdir()
            (old/'summary.json').write_text(json.dumps({'protocol':{'version':old.name},'status':'diagnostic',
                'runs':[{'label':'5%股息门槛 · 全期','status':'diagnostic','metrics':{'cagr':'.03'}}]}),encoding='utf-8')
            page=experiment_history_panel(workspace)
            self.assertIn('<th>入场方式</th>',page)
            self.assertIn('<td>固定股息门槛</td>',page)
            self.assertIn('<td>股息排序</td>',page)
            self.assertIn('各版本预设主方案全期结果',page)
            self.assertIn('6.00%',page);self.assertIn('3.00%',page)
            self.assertNotIn('30.00%',page);self.assertNotIn('90.00%',page)
            summary['primary_metrics']={'cagr':'.8'}
            summary['runs'][0]={'label':'股息排序','status':'blocked','metrics':{}}
            (folder/'summary.json').write_text(json.dumps(summary),encoding='utf-8')
            blocked=experiment_history_panel(workspace)
            self.assertIn('输入待核实</td><td>—</td><td>—</td><td>—',blocked)
            self.assertNotIn('80.00%',blocked);self.assertNotIn('90.00%',blocked)
            self.assertEqual(entry_mode_label({}),'固定股息门槛')
            self.assertEqual(entry_mode_label({'entry_mode':'<script>mode</script>'}),'未识别入场方式')

    def test_rank_explains_reference_and_keeps_funnel_counts_and_single_curve(self):
        research={'target_count':20,'stocks':[{'symbol':f'600{i:03d}.SH','name':f'公司{i}'} for i in range(20)]}
        backtest={'protocol':{'entry_mode':'yield_rank','earnings_basis':'ttm','cohort_limit':None},
                  'status':'diagnostic','selection_audit':{'holding_limit':20,'rebalances':[]},
                  'runs':[{'label':'股息排序 · 全期','status':'diagnostic','metrics':{'cagr':'.06'}}],
                  'comparison_series':[{'label':'股息排序主方案（含估计成本）','kind':'strategy','points':[{'day':'20250731','value':1}]}],
                  'diagnostics':{'entry_funnel':[{'stage':'当期候选','count':100},{'stage':'全部买入条件通过','count':42}],
                                 'candidate_months':{'months':4,'empty':1,'total_candidates':42}}}
        before=copy.deepcopy(backtest)
        page=research_panel(research,backtest)
        self.assertIn('入场方式：股息排序',page)
        self.assertIn('历史 TTM 股息率至少 3% 初筛',page)
        self.assertIn('5% 只作情景计算参考，不作为入场条件或买入限价上限',page)
        self.assertIn('最多持仓 20 只',page)
        self.assertIn('排序候选</td><td>42</td>',page)
        self.assertIn('1 次无排序候选',page)
        self.assertNotIn('全部买入条件通过',page)
        self.assertNotIn('5%股息门槛',page)
        self.assertEqual(page.count('class="research-stock"'),20)
        curves=json.loads(re.search(r'id="comparison-data">(.*?)</script>',page,re.S)[1])
        self.assertEqual(curves,backtest['comparison_series'])
        self.assertEqual(backtest,before)
        backtest['diagnostics']['entry_funnel'][-1]['stage']='排序候选'
        self.assertIn('排序候选</td><td>42</td>',research_panel(research,backtest))


if __name__=='__main__':
    unittest.main()
