import copy
import json
import tempfile
import unittest
from pathlib import Path

from qmt.report import approved_cash_note, chart_panel, experiment_history_panel, historical_chart_data, research_panel
import test_rank_ui


class DividendPolicyUiTests(unittest.TestCase):
    def test_new_policy_explains_forward_stage_coverage_and_cap_without_changing_legacy_html(self):
        research={'stocks':[]}
        old={'protocol':{'entry_mode':'yield_rank'},'status':'diagnostic'}
        legacy=research_panel(research,old)
        explicit={**old,'protocol':{**old['protocol'],'dividend_policy':'implemented_only'}}
        self.assertEqual(research_panel(research,explicit),legacy)
        new={**old,'protocol':{**old['protocol'],'dividend_policy':'approved_annual_cash'},
             'diagnostics':{'cash_estimates':{'evaluations':10,'stages':{'approved':2,'implemented':7,'not_recorded':1},
                            'coverage_bases':{'commitment':2,'implemented_announcements':7,'not_recorded':1}}}}
        original=copy.deepcopy(new);page=research_panel(research,new)
        for text in ('股息估计：已批准年度现金估计','已批准未实施 2 次','已实施公告 7 次','承诺现金覆盖 2 次',
                     '已实施公告现金覆盖 7 次','未记录 1 次','不是本账户实际到账','最新年度已经实施时也受最新 q 上限约束',
                     '包含退出名单后的跟踪','不是持股、订单、成交或到账次数','日常模拟规则未随此实验改变'):
            self.assertIn(text,page)
        self.assertNotIn('id="dividend-policy"',legacy)
        self.assertEqual(new,original)
        unsafe=approved_cash_note(new['protocol'],{'cash_estimates':{'stages':{'approved':'<img src=x>'}}})
        self.assertIn('&lt;img src=x&gt;',unsafe);self.assertNotIn('<img',unsafe)
        self.assertEqual(approved_cash_note({'dividend_policy':'<script>unknown</script>'},{}),'')

    def test_new_policy_labels_only_its_archive_and_does_not_create_cash_or_stock_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            folder,summary,result=test_rank_ui.RankUiTests().fixture(workspace)
            legacy=historical_chart_data(workspace)
            summary['protocol']['dividend_policy']='approved_annual_cash'
            write=lambda path,data:path.write_text(json.dumps(data),encoding='utf8')
            write(folder/'summary.json',summary);write(workspace/'backtests/latest.json',summary)
            before={p:p.read_bytes() for p in workspace.rglob('*.json')}
            history=historical_chart_data(workspace)
            self.assertEqual(history['stocks'],legacy['stocks'])
            self.assertEqual(history['runs'][0],{**legacy['runs'][0],
                'label':legacy['runs'][0]['label']+' · 已批准年度现金估计'})
            self.assertEqual(history['runs'][0]['fills'],result['trades'])
            self.assertEqual(history['runs'][0]['stock_events'],[])
            self.assertNotIn('cash_events',history['runs'][0])
            page=chart_panel(workspace,{'as_of':'20250804','source':'Tushare','rows':[]},{'focus_symbols':[]})
            self.assertIn('本次历史 股息排序 · 主方案 · 已批准年度现金估计',page)
            self.assertIn('累计 1 只 · 已批准年度现金估计',experiment_history_panel(workspace))
            self.assertEqual(before,{p:p.read_bytes() for p in workspace.rglob('*.json')})
            for policy in ('implemented_only','unknown',None,[]):
                write(workspace/'backtests/latest.json',{**summary,'protocol':{**summary['protocol'],'dividend_policy':policy}})
                self.assertEqual(historical_chart_data(workspace),{})
            write(workspace/'backtests/latest.json',summary)
            summary['protocol']['dividend_policy']='unknown'
            write(folder/'summary.json',summary)
            self.assertEqual(historical_chart_data(workspace),{})


if __name__=='__main__':
    unittest.main()
