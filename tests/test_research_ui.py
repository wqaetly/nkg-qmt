import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from qmt.config import ROOT, load_config
from qmt.demo import create_demo
from qmt.report import chart_panel, optional_report, percent, research_panel


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


if __name__=='__main__':unittest.main()
