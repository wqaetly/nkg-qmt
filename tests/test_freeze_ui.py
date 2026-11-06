import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from qmt.config import load_config
from qmt.demo import create_demo
from qmt.ledger import Ledger
from qmt.report import experiment_history_panel, freeze_policy_label, holding_freezes_panel, render, research_panel


class FreezeUiTests(unittest.TestCase):
    def fixture(self):
        return {'policy':'portfolio','months':3,'frozen_months':2,'portfolio_blocked_months':2,
                'buy_skip_months':1,'buy_skips':4,'company_months':3,
                'latest':{'day':'20230531','held_frozen':0,'portfolio_blocked':False,'buy_skips':0},
                'rows':[
                    {'day':'20230331','symbol':'600001.SH','quantity':100,'market_value':'1234.50',
                     'weight':'.012345','active':True,'scope':'portfolio','reason':'等待实施公告'},
                    {'day':'20230428','symbol':'600001.SH','quantity':100,'market_value':'1200',
                     'weight':'.012','active':False,'scope':'portfolio','reason':'派息持续性待核实'},
                    {'day':'20230428','symbol':'600002.SH','quantity':200,'market_value':'2400',
                     'weight':'.024','active':True,'scope':'portfolio','reason':'短期借款字段缺失'}]}

    def test_actual_holdings_are_inside_existing_fold_with_distinct_count_units(self):
        data=self.fixture();before=copy.deepcopy(data)
        page=research_panel({}, {'protocol':{'freeze_policy':'portfolio'},'diagnostics':{'holding_freezes':data}})
        fold=page.split('<details class="research-results" id="research-diagnostics">',1)[1].split('</details>',1)[0]
        self.assertIn('id="holding-freezes"',fold)
        self.assertNotIn('id="research-diagnostics" open',page)
        for text in ('当期冻结清单与当日实际持仓的交集','月度评估 3 次','实际冻结持仓 2 个组合月',
                     '组合暂停买入 2 个组合月','买入受阻 1 个组合月、4 次买入尝试','3 公司·月',
                     '同月多只冻结持仓仍只算一个组合月','不是订单、成交或亏损','净值、持仓席位及行业占额'):
            self.assertIn(text,fold)
        self.assertIn('最近评估日 20230531：实际冻结持仓 0 只；组合暂停买入：否；买入尝试跳过 0 次。本期无实际冻结持仓。',fold)
        self.assertLess(fold.index('<td>20230428</td>'),fold.index('<td>20230331</td>'))
        self.assertIn('<td>600001.SH</td><td>100</td><td>1,200.00</td><td>1.20%</td><td>是</td><td>组合停买</td>',fold)
        self.assertIn('<td>1,234.50</td><td>1.23%</td><td>否</td>',fold)
        self.assertEqual(data,before)

    def test_position_policy_keeps_global_exceptions_and_records_each_rows_scope(self):
        data=self.fixture();data['policy']='position';data['portfolio_blocked_months']=1
        data['rows'][0]['scope']='position'
        data['rows'][1].update(scope='portfolio',reason='身份未知')
        page=research_panel({}, {'protocol':{'freeze_policy':'position'},'diagnostics':{'holding_freezes':data}})
        for text in ('冻结政策：按股冻结','保持冻股原数量及资金、行业占额','其他通过全部资格者可买入',
                     '身份或行业未知仍暂停组合新增买入','缺少持仓估值或未核实权益仍阻断回放',
                     '日常模拟规则未随此实验改变','<td>按股冻结</td>','<td>组合停买</td><td>身份未知</td>'):
            self.assertIn(text,page)
        portfolio=research_panel({}, {'protocol':{'freeze_policy':'portfolio'}})
        self.assertIn('暂停组合全部新增买入（含加仓）',portfolio)
        self.assertIn('其他持仓仍可按规则卖出',portfolio)

    def test_missing_diagnostics_do_not_add_empty_section_and_unknown_is_not_zero(self):
        for value in (None,{}):
            self.assertEqual(holding_freezes_panel(value),'')
            page=research_panel({}, {'protocol':{},'diagnostics':{'holding_freezes':value}})
            self.assertNotIn('id="holding-freezes"',page)
            self.assertIn('冻结政策：未记录',page)
            self.assertIn('不能据此推断已按股冻结',page)
        data={'policy':'position','rows':[{'day':'20230428','symbol':'600001.SH','quantity':100,
                                          'market_value':None,'weight':None,'active':None,'scope':None,'reason':'未记录'}]}
        page=holding_freezes_panel(data)
        self.assertIn('实际冻结持仓 未记录；组合暂停买入：未记录',page)
        self.assertNotIn('本期无实际冻结持仓',page)
        self.assertIn('<td>100</td><td>—</td><td>—</td><td>未记录</td><td>未记录</td>',page)
        self.assertNotIn('0.00%',page)

    def test_history_shows_explicit_policy_and_does_not_infer_legacy_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            for version,policy in [('legacy',None),('portfolio','portfolio'),('position','position'),('invalid','<script>bad</script>')]:
                folder=workspace/'backtests'/version;folder.mkdir(parents=True)
                protocol={'version':version,**({'freeze_policy':policy} if policy is not None else {})}
                (folder/'summary.json').write_text(json.dumps({'protocol':protocol,'status':'diagnostic'}),encoding='utf-8')
            before={p:p.read_bytes() for p in workspace.rglob('*.json')}
            page=experiment_history_panel(workspace)
            self.assertIn('<th>冻结政策</th>',page)
            self.assertEqual(page.count('<td>未记录</td>'),2)
            self.assertIn('<td>组合停买</td>',page);self.assertIn('<td>按股冻结</td>',page)
            self.assertIn('未声明冻结政策的旧归档显示“未记录”',page)
            self.assertNotIn('<script>bad</script>',page)
            self.assertEqual(before,{p:p.read_bytes() for p in workspace.rglob('*.json')})
        self.assertEqual(freeze_policy_label({}),'未记录')

    def test_untrusted_diagnostic_text_is_escaped_without_mutating_input(self):
        data=self.fixture();bad='<img src=x onerror=alert(1)>'
        data.update(policy=bad,months=bad)
        data['latest'].update(day=bad)
        data['rows'][0].update(day=bad,symbol=bad,quantity=bad,reason=bad,scope=bad)
        before=copy.deepcopy(data)
        page=holding_freezes_panel(data)
        self.assertNotIn('<img',page)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;',page)
        self.assertIn('诊断政策：未记录',page)
        self.assertEqual(data,before)

    def test_fixture_render_preserves_chart_operations_account_and_five_per_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'));report['source']='Tushare'
            research={'as_of':report['as_of'],'target_count':20,
                      'stocks':[{'symbol':f'600{i:03d}.SH','name':f'公司{i}'} for i in range(20)]}
            (workspace/'research').mkdir(exist_ok=True)
            (workspace/'research/latest.json').write_text(json.dumps(research),encoding='utf-8')
            (workspace/'backtests').mkdir(exist_ok=True)
            summary={'status':'diagnostic','protocol':{'version':'historical-fixture','freeze_policy':'portfolio'}}
            path=workspace/'backtests/latest.json';path.write_text(json.dumps(summary),encoding='utf-8')
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:
                account=copy.deepcopy(ledger.summary('paper'));original=copy.deepcopy(report)
                before=render(workspace,ledger,report).read_text(encoding='utf-8')
                summary['diagnostics']={'holding_freezes':self.fixture()}
                path.write_text(json.dumps(summary),encoding='utf-8')
                after=render(workspace,ledger,report).read_text(encoding='utf-8')
                self.assertEqual(ledger.summary('paper'),account);self.assertEqual(report,original)
            finally:
                ledger.close()
            chart=lambda page:re.search(r'id="market-chart-data">(.*?)</script>',page,re.S).group(1)
            self.assertEqual(chart(before),chart(after))
            self.assertIn('id="holding-freezes"',after)
            self.assertIn('每页 5 只',after)
            self.assertEqual(after.count('class="research-stock"'),20)


if __name__=='__main__':
    unittest.main()
