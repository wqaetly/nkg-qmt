import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from qmt.config import ROOT, load_config
from qmt.demo import create_demo
from qmt.report import chart_panel, optional_report, percent, research_panel, comparison_chart, period_diagnostics_panel, coverage_panel, automatic_panel, experiment_history_panel


class ResearchUiTests(unittest.TestCase):
    def research(self):
        return {'as_of':'20261106','version':'research-20-v1','target_count':20,'method':'三项独立筛选',
                'stocks':[{'symbol':f'600{i:03d}.SH','name':f'公司{i}','industry':'制造','state':'待核实',
                           'reason':'分红政策待查','price':'20','estimated_yield':'.04',
                           'valuation':{'state':'通过'},'liquidity':{'state':'通过'},'source_urls':[]} for i in range(20)],
                'limitations':['尚未完成独立验证']}

    def test_twenty_research_names_and_historical_returns_do_not_claim_success(self):
        research=self.research()
        backtest={'status':'完成历史实验','runs':[{'label':'训练区间','metrics':{'cagr':'.2','total_return':'.7','max_drawdown':'-.3'}}]}
        page=research_panel(research,backtest)
        self.assertEqual(page.count('class="research-stock"'),20)
        self.assertIn('20 / 20',page)
        self.assertIn('年化 ≥ 10%',page)
        self.assertIn('待验证，不是收益承诺',page)
        self.assertIn('20.00%',page)
        self.assertIn('-30.00%',page)
        self.assertIn('单个区间超过 10% 不代表目标已验证',page)
        self.assertIn('仅诊断，待验证',research_panel(research,{'status':'diagnostic'}))

    def test_automatic_budget_holdings_and_benchmark_use_each_ledgers_actual_settings(self):
        data={'day':'20261106','account':{'account':{'initial_cash':'100000','start_date':'20261106'},'marks':[{'equity':'100000'}]},
              'rules':{'max_holdings':20,'cash_reserve':'.10','benchmark':'510880.SH'},'assessments':[]}
        page=automatic_panel(data)
        self.assertIn('初始虚拟本金 ¥100,000.00',page)
        self.assertIn('持股上限 20 只',page)
        self.assertIn('现金底仓 10.00%',page)
        self.assertIn('基准 510880.SH',page)
        self.assertNotIn('515180.SH',page)
        data['account']['account']['initial_cash']='10000'
        data['rules']['max_holdings']=5
        old=automatic_panel(data)
        self.assertIn('初始虚拟本金 ¥10,000.00',old)
        self.assertIn('持股上限 5 只',old)

    def test_history_preserves_blocked_versions_and_uses_primary_metrics_in_this_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)/'capital-100k'
            self.assertEqual(experiment_history_panel(workspace),'')
            old=workspace/'backtests'/'v7-long'
            old.mkdir(parents=True)
            (old/'protocol.json').write_text(json.dumps({'version':'v7-long','selection_day':'20141231',
                'account_config':{'paper_cash':'100000'}}),encoding='utf-8')
            (old/'summary.json').write_text(json.dumps({'status':'blocked','coverage':{'start':'20141231','end':'20261106','stocks':20},
                'primary_metrics':{'cagr':.99,'average_exposure':1,'costs':999},
                'runs':[{'label':'现金参考','metrics':{'cagr':0}}]}),encoding='utf-8')
            new=workspace/'backtests'/'v9-quality-long'
            new.mkdir()
            (new/'summary.json').write_text(json.dumps({'status':'diagnostic','initial_cash':'100000',
                'protocol':{'version':'v9-quality-long','selection_day':'20141231'},
                'coverage':{'start':'20141231','end':'20261106','stocks':7},
                'primary_metrics':{'cagr':.08,'average_exposure':.35,'costs':1234.56},
                'runs':[{'label':'5%股息门槛 · 全期','metrics':{'cagr':.5}}]}),encoding='utf-8')
            outside=Path(tmp)/'backtests'/'old-10k'
            outside.mkdir(parents=True)
            (outside/'summary.json').write_text(json.dumps({'protocol':{'version':'不得混入旧1万'}}),encoding='utf-8')
            before={path:path.read_bytes() for path in workspace.rglob('*.json')}
            page=experiment_history_panel(workspace)
            self.assertIn('id="experiment-history"',page)
            self.assertNotIn(' open',page)
            self.assertIn('2 个版本',page)
            self.assertIn('v7-long · 首次建池 20141231 · 累计 20 只',page)
            self.assertIn('输入待核实</td><td>—</td><td>—</td><td>—',page)
            self.assertIn('v9-quality-long · 首次建池 20141231 · 累计 7 只',page)
            self.assertIn('¥ 100,000.00 · 20141231—20261106',page)
            self.assertIn('8.00%',page)
            self.assertIn('35.00%',page)
            self.assertIn('1,234.56',page)
            for wrong in ('99.00%','50.00%','不得混入旧1万'):
                self.assertNotIn(wrong,page)
            self.assertEqual(before,{path:path.read_bytes() for path in workspace.rglob('*.json')})

    def test_history_legacy_selects_only_five_percent_full_period_and_escapes_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            old=workspace/'backtests'/'legacy'
            old.mkdir(parents=True)
            (old/'protocol.json').write_text(json.dumps({'version':'<script>old</script>','selection_day':'20221230',
                'replay_end':'20261106','account_config':{'paper_cash':'10000'}}),encoding='utf-8')
            summary={'status':'diagnostic','runs':[
                {'label':'5%股息门槛 · 开发段','metrics':{'cagr':.5}},
                {'label':'15%股息门槛 · 全期','metrics':{'cagr':.9}},
                {'label':'5%股息门槛 · 全期','status':'diagnostic','metrics':{'cagr':.03,'average_exposure':.04,'costs':80}},
                {'label':'510880参考','metrics':{'cagr':.2}}]}
            (old/'summary.json').write_text(json.dumps(summary),encoding='utf-8')
            page=experiment_history_panel(workspace)
            self.assertIn('3.00%',page)
            self.assertIn('4.00%',page)
            self.assertIn('80.00',page)
            self.assertIn('¥ 10,000.00 · 20221230—20261106',page)
            self.assertIn('&lt;script&gt;old&lt;/script&gt;',page)
            for wrong in ('50.00%','90.00%','20.00%','<script>old</script>'):
                self.assertNotIn(wrong,page)
            summary['runs'][2]={'label':'税前股息门槛 5%','status':'blocked','metrics':{'cagr':.4}}
            (old/'summary.json').write_text(json.dumps(summary),encoding='utf-8')
            blocked=experiment_history_panel(workspace)
            self.assertIn('输入待核实</td><td>—</td><td>—</td><td>—',blocked)
            self.assertNotIn('40.00%',blocked)

    def test_selection_rejections_and_execution_skips_are_counted_in_separate_units(self):
        backtest={'status':'diagnostic','coverage':{'stocks':7},
                  'selection_audit':{'candidate_count':30,'evaluated_count':12,'target_count':20,
                      'stocks':[{} for _ in range(7)],'rejected':[{'reason':'现金不足 <script>x</script>'} for _ in range(3)]+[{'reason':'审计待核实'} for _ in range(2)]},
                  'diagnostics':{'evaluations':35,'reasons':[{'reason':'价格偏高','count':20}],
                      'skips':[{'reason':'一手超出资金上限','count':6}]}}
        page=research_panel(self.research(),backtest)
        self.assertIn('历史涉及 7 只',page)
        self.assertIn('建池 7 只 / 目标 20 只；候选 30 家，已评估 12 家',page)
        self.assertEqual(page.count('现金不足 &lt;script&gt;x&lt;/script&gt;'),1)
        self.assertIn('现金不足 &lt;script&gt;x&lt;/script&gt;</td><td>3</td>',page)
        self.assertIn('执行跳过（事件）</td><td>一手超出资金上限</td><td>6',page)
        self.assertIn('筛选未通过（公司 / 时点）</td><td>价格偏高</td><td>20',page)
        self.assertIn('三者不能相加',page)
        self.assertNotIn('<script>x</script>',page)
        backtest['coverage']['stocks']=0
        self.assertIn('历史涉及 0 只',research_panel(self.research(),backtest))

    def test_optional_records_and_untrusted_text_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'latest.json'
            self.assertEqual(optional_report(path),{})
            path.write_text('{invalid',encoding='utf-8')
            self.assertEqual(optional_report(path)['status'],'数据不可用')
        research=self.research()
        research['stocks'][0].update(name='<script>alert(1)</script>',reason='" onclick="alert(1)',
                                     source_urls=['javascript:alert(1)','https://example.com/report?a=1&b=2'])
        page=research_panel(research,{})
        self.assertNotIn('<script>alert(1)</script>',page)
        self.assertNotIn('href="javascript:',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn('https://example.com/report?a=1&amp;b=2',page)
        self.assertEqual(percent('not a number'),'—')
        self.assertEqual(percent('nan'),'—')

    def test_comparison_payload_and_diagnostics_are_not_raw_html(self):
        backtest={'comparison_series':[{'label':'</script><img src=x onerror=alert(1)>','kind':'strategy','points':[{'day':'20230102','value':1}]}],
                  'diagnostics':{'evaluations':940,'states':{'待数据核实':600},'reasons':[{'reason':'<script>缺失</script>','count':400}]},
                  'runs':[{'label':'实验','metrics':{'cagr':'.1','total_return':'.2','max_drawdown':'-.3','average_exposure':'.4','trade_count':4,'costs':'20'}}]}
        page=research_panel(self.research(),backtest)
        self.assertNotIn('</script><img',page)
        self.assertNotIn('<script>缺失</script>',page)
        self.assertIn('\\u003c/script\\u003e',page)
        self.assertIn('940',page)
        self.assertIn('平均仓位',page)
        self.assertIn('40.00%',page)
        self.assertIn('成交笔数',page)
        self.assertIn('费用 / 元',page)
        self.assertNotIn('comparison-data',comparison_chart({}))

    def test_annual_and_rolling_metrics_label_actual_period_without_claiming_success(self):
        data={'label':'5%主方案 <script>不可执行</script>',
              'annual':[{'year':2026,'start':'20251231','end':'20261106',
                         'metrics':{'total_return':'.12','max_drawdown':'-.08','average_exposure':'.25','trade_count':4,'costs':'22.25'}}],
              'rolling_three_year':{'windows':5,'min_cagr':-.1,'median_cagr':.08,'max_cagr':.2,'positive_fraction':.8}}
        page=period_diagnostics_panel(data)
        self.assertIn('2026 · 20251231—20261106',page)
        self.assertIn('12.00%',page)
        self.assertIn('25.00%',page)
        self.assertIn('22.25',page)
        self.assertIn('最低年化',page)
        self.assertIn('80.00%',page)
        self.assertIn('最后一年可能尚未结束',page)
        self.assertIn('重叠窗口不是独立样本',page)
        self.assertNotIn('<script>不可执行</script>',page)
        self.assertIn('&lt;script&gt;',page)
        full=research_panel(self.research(),{'period_diagnostics':data})
        self.assertIn('待验证，不是收益承诺',full)
        self.assertIn('id="period-diagnostics"',full)

    def test_missing_annual_or_rolling_metrics_are_not_invented(self):
        self.assertEqual(period_diagnostics_panel({}),'')
        self.assertEqual(period_diagnostics_panel({'rolling_three_year':{'windows':0}}),'')
        self.assertEqual(period_diagnostics_panel({'rolling_three_year':{'windows':2,'min_cagr':.1}}),'')
        only_rolling={'rolling_three_year':{'windows':3,'min_cagr':0,'median_cagr':.01,'max_cagr':.02,'positive_fraction':1}}
        self.assertIn('完整三年窗口数',period_diagnostics_panel(only_rolling))
        self.assertNotIn('<th>年度 / 实际区间</th>',period_diagnostics_panel(only_rolling))
        only_rolling['rolling_three_year']['positive_fraction']=float('nan')
        self.assertEqual(period_diagnostics_panel(only_rolling),'')

    def test_blocked_history_reports_coverage_without_strategy_or_annual_returns(self):
        coverage={'start':'20141231','end':'20261106','sessions':2885,'stocks':20,'price_rows':56000,'valuation_rows':55000,
                  'input_errors':{'600882.SH':'历史行业存在冲突 <script>不可执行</script>'}}
        backtest={'status':'blocked','coverage':coverage,
                  'comparison_series':[{'label':'现金参考','kind':'cash','points':[{'day':'20141231','value':1},{'day':'20261106','value':1}]}],
                  'period_diagnostics':{'label':'不应显示的旧统计','annual':[{'year':2015,'start':'20141231','end':'20151231','metrics':{'total_return':'.5'}}]}}
        page=research_panel(self.research(),backtest)
        for value in ('20141231','20261106','2885','56000','55000','600882.SH'):
            self.assertIn(value,page)
        self.assertIn('输入待核实',page)
        self.assertIn('输入疑点 1 只',page)
        self.assertIn('不是股票亏损',page)
        self.assertIn('不能把现金线当作策略收益或已通过验证',page)
        self.assertNotIn('<script>不可执行</script>',page)
        self.assertNotIn('id="period-diagnostics"',page)
        self.assertNotIn('不应显示的旧统计',page)
        self.assertEqual(coverage_panel({}),'')
        self.assertIn('本次未记录输入错误',coverage_panel({**coverage,'input_errors':{}}))

    def test_chart_can_select_twenty_matching_research_stocks_but_not_stale_pool(self):
        research=self.research()
        report={'as_of':'20261106','rows':[]}
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            data={'as_of':'20261106','stocks':{s['symbol']:{'candles':[{'day':'20261106'}],'error':None} for s in research['stocks']}}
            (workspace/'charts.json').write_text(json.dumps(data),encoding='utf-8')
            page=chart_panel(workspace,report,{'focus_symbols':[]},research)
            self.assertEqual(page.count('<option value="600'),20)
            self.assertIn('600019.SH',page)
            data['stocks']['600019.SH']={'candles':[],'error':'数据不可用'}
            (workspace/'charts.json').write_text(json.dumps(data),encoding='utf-8')
            self.assertEqual(chart_panel(workspace,report,{'focus_symbols':[]},research).count('<option value="600'),19)
            research['as_of']='20261105'
            self.assertEqual(chart_panel(workspace,report,{'focus_symbols':[]},research),'')

    def test_demo_remains_isolated_from_research_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            (workspace/'research').mkdir()
            (workspace/'research/latest.json').write_text(json.dumps(self.research()),encoding='utf-8')
            page=create_demo(workspace,load_config()).read_text(encoding='utf-8')
            self.assertNotIn('id="research-lab"',page)
            self.assertIn('虚构演示数据',page)

    @unittest.skipUnless(shutil.which('node'),'Node is needed for the browser interaction self-check')
    def test_search_and_pagination_cover_all_twenty(self):
        script=r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const elements = {};
for (const id of ['research-search','research-prev','research-next','research-count','research-page','research-empty']) {
  elements[id] = {value:'', handlers:{}, addEventListener(event, handler) { this.handlers[event] = handler; }};
}
const rows = Array.from({length:20}, (_,i) => ({dataset:{search:`company ${i} code-${i}`},hidden:false}));
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document:{getElementById:id=>elements[id],querySelectorAll:()=>rows}});
assert.equal(rows.filter(r=>!r.hidden).length,5);
assert.equal(elements['research-page'].textContent,'1 / 4 页 · 每页 5 只');
elements['research-next'].handlers.click();
assert.equal(rows[5].hidden,false);assert.equal(rows[0].hidden,true);
elements['research-search'].value='code-19';elements['research-search'].handlers.input();
assert.equal(rows.filter(r=>!r.hidden).length,1);assert.equal(rows[19].hidden,false);
assert.equal(elements['research-count'].textContent,'匹配 1 / 20 只');
elements['research-search'].value='missing';elements['research-search'].handlers.input();
assert.equal(elements['research-empty'].hidden,false);
assert.equal(elements['research-next'].disabled,true);
'''
        subprocess.run(['node','-e',script,str(ROOT/'qmt/research-ui.js')],check=True,capture_output=True,text=True)

    @unittest.skipUnless(shutil.which('node'),'Node is needed for the chart interaction self-check')
    def test_comparison_toggles_keyboard_and_missing_dates(self):
        script=r'''
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Node {
  constructor(tag) { this.tag=tag;this.children=[];this.attrs={};this.style={};this.handlers={};this.textContent=''; }
  setAttribute(key,value) { this.attrs[key]=value; }
  appendChild(node) { this.children.push(node); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children=[]; }
  addEventListener(event,handler) { this.handlers[event]=handler; }
}
const code=fs.readFileSync(process.argv[1],'utf8');
const series=[
  {label:'策略',kind:'strategy',points:[{day:'20230102',value:1},{day:'20230103',value:.9},{day:'20230104',value:1.1}]},
  {label:'ETF',kind:'benchmark',points:[{day:'20230102',value:1},{day:'20230104',value:1.2}]},
  {label:'现金',kind:'cash',points:[{day:'20230102',value:1},{day:'20230103',value:1},{day:'20230104',value:1}]}
];
function run(data) {
  const nodes=Object.fromEntries(['comparison-data','comparison-chart','comparison-tip','comparison-legend'].map(id=>[id,new Node(id)]));
  nodes['comparison-data'].textContent=JSON.stringify(data);
  vm.runInNewContext(code,{window:{addEventListener(){}},document:{getElementById:id=>nodes[id],createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag)}});
  return nodes;
}
const nodes=run(series), host=nodes['comparison-chart'], legend=nodes['comparison-legend'];
assert.equal(legend.children.length,3);
assert.equal(host.children[0].children.filter(n=>n.tag==='path').length,3);
const etf=host.children[0].children.find(n=>n.tag==='path'&&n.attrs['data-series']==='ETF');
assert.equal((etf.attrs.d.match(/M/g)||[]).length,2);
host.onkeydown({key:'ArrowLeft',preventDefault(){}});
assert.match(nodes['comparison-tip'].textContent,/2023-01-03/);
assert.match(nodes['comparison-tip'].textContent,/ETF：当日数据缺失/);
for(const label of legend.children){const input=label.children[0];input.checked=false;input.handlers.change();}
assert.equal(host.children.length,0);assert.equal(host.onkeydown,null);assert.match(host.textContent,/选择至少一条/);
legend.children[0].children[0].checked=true;legend.children[0].children[0].handlers.change();
assert.equal(host.children[0].children.filter(n=>n.tag==='path').length,1);
for (const bad of [
  [{...series[0],points:[{day:'20230230',value:1}]}],
  [{...series[0],points:[{day:'20230102',value:2}]}],
  [{...series[0],points:[{day:'20230102',value:1},{day:'20230102',value:1.1}]}],
  [series[0],{...series[1],points:[{day:'20230103',value:1},{day:'20230104',value:1.2}]}]
]) assert.match(run(bad)['comparison-chart'].textContent,/无效/);
'''
        subprocess.run(['node','-e',script,str(ROOT/'qmt/benchmark-ui.js')],check=True,capture_output=True,text=True)


if __name__=='__main__':unittest.main()
