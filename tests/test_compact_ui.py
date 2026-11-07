import copy
import json
import tempfile
import unittest
from pathlib import Path

from qmt.config import load_config
from qmt.demo import create_demo
from qmt.ledger import Ledger
from qmt.report import cashflow_policy_note, historical_chart_data, historical_overview, render
import test_rank_ui


class CompactUiTests(unittest.TestCase):
    def summary(self):
        return {'status':'diagnostic','initial_cash':'100000',
                'coverage':{'start':'20141230','end':'20261106'},
                'protocol':{'version':'main-v32','cashflow_policy':'three_year_coverage'},
                'primary_metrics':{'cagr':'-.0291','total_return':'-.4063','max_drawdown':'-.1495',
                                   'costs':'2865.02','average_exposure':'.2966'},
                'diagnostics':{'candidate_months':{'months':143,'empty':36},
                               'financial_gaps':[{'symbol':'A'},{'symbol':'A'}],
                               'holding_freezes':{'latest':{'day':'20261030','held_frozen':6}},
                               'cashflow_policy':{'policy':'three_year_coverage','evaluations':100,
                                   'nonpositive_latest_observations':[{
                                       'day':'20261030','symbol':'A','state':'排序候选','active':True,
                                       'cashflow_warning':{'period':'20251231','simple_fcf':'-100',
                                                           'reason':'<script>不应执行</script>'},
                                       'three_year_fcf':'200','three_year_dividend_cash':'150'}]}}}

    def test_actual_losses_costs_and_risks_remain_visible_without_counting_cash_causes(self):
        summary=self.summary();before=copy.deepcopy(summary)
        page=historical_overview(summary)
        for text in ('20141230—20261106','100,000.00','-2.91%','-40.63%','-14.95%',
                     '2,865.02','29.66%','月末评估 143 次，其中 36 次无候选','历史字段缺口 2 条',
                     '最近评估 20261030，冻结持仓 6 只','自由现金流非正：1 条历史评估记录',
                     '次数不等于闲置现金归因','不是现金闲置金额或原因分摊'):
            self.assertIn(text,page)
        self.assertNotIn('<details',page)
        self.assertNotIn('70.34%',page)
        self.assertNotIn('年化 ≥ 10%',page)
        detail=cashflow_policy_note(summary['protocol'],summary['diagnostics'])
        self.assertIn('三年累计自由现金流覆盖三年总分红',detail)
        self.assertIn('20251231</td><td>-100.00</td><td>排序候选</td><td>是</td><td>200.00</td><td>150.00',detail)
        self.assertIn('&lt;script&gt;不应执行&lt;/script&gt;',detail)
        self.assertNotIn('<script>',detail)
        self.assertEqual(summary,before)

    def test_blocked_missing_and_nonfinite_are_unknown_while_actual_zero_is_preserved(self):
        summary=self.summary();summary['status']='blocked';summary['message']='<待核实>'
        blocked=historical_overview(summary)
        self.assertNotIn('-2.91%',blocked)
        self.assertNotIn('0.00%',blocked)
        self.assertIn('&lt;待核实&gt;',blocked)
        summary['status']='diagnostic'
        summary['primary_metrics']={'cagr':None,'total_return':'NaN','max_drawdown':'Infinity',
                                    'costs':'NaN','average_exposure':0}
        summary['diagnostics']={}
        missing=historical_overview(summary)
        self.assertIn('<small>年化收益</small><strong>—</strong>',missing)
        self.assertIn('累计费用 ¥ —',missing)
        self.assertIn('<small>平均仓位</small><strong>0.00%</strong>',missing)
        self.assertIn('历史字段缺口 未记录 条',missing)
        self.assertIn('冻结持仓 — 只',missing)
        self.assertNotIn('NaN',missing);self.assertNotIn('Infinity',missing)
        row={'cashflow_warning':{'simple_fcf':0}}
        detail=cashflow_policy_note({}, {'cashflow_policy':{'nonpositive_latest_observations':[row]}})
        self.assertIn('<td>0.00</td>',detail)
        self.assertNotIn('<td>0.00</td><td>0.00</td>',detail)

    def test_render_prioritizes_overview_and_candles_with_rules_still_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'))
            report['source']='Tushare'
            for folder,data in (('backtests',self.summary()),('research',{'stocks':[]})):
                (workspace/folder).mkdir(exist_ok=True)
                (workspace/folder/'latest.json').write_text(json.dumps(data),encoding='utf-8')
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:
                page=render(workspace,ledger,report).read_text(encoding='utf-8')
            finally:
                ledger.close()
            self.assertLess(page.index('id="strategy-overview"'),page.index('id="chart-panel"'))
            self.assertLess(page.index('id="chart-panel"'),page.index('id="research-lab"'))
            self.assertIn('策略规则、来源与完整指标',page)
            self.assertIn('id="cashflow-policy"',page)
            self.assertIn('id="chart-provenance"',page)
            self.assertIn('年化 ≥ 10%',page)

    def test_candle_source_requires_the_same_cashflow_policy_and_retains_real_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,result=test_rank_ui.RankUiTests().fixture(workspace)
            summary['protocol']['cashflow_policy']='three_year_coverage'
            write=lambda path,data:path.write_text(json.dumps(data),encoding='utf-8')
            write(folder/'summary.json',summary);write(workspace/'backtests/latest.json',summary)
            self.assertEqual(historical_chart_data(workspace)['runs'][0]['fills'],result['trades'])
            for policy in ('latest_and_three_year','unknown',None,[]):
                write(workspace/'backtests/latest.json',{**summary,'protocol':{**summary['protocol'],'cashflow_policy':policy}})
                self.assertEqual(historical_chart_data(workspace),{})


if __name__=='__main__':
    unittest.main()
