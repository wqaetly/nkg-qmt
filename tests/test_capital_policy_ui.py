import copy
import json
import tempfile
import unittest
from pathlib import Path

from qmt.config import dec, load_config
from qmt.demo import create_demo
from qmt.ledger import Ledger
from qmt.report import capital_policy_note, capital_ratios, forecast_diagnostic_panel, historical_chart_data, render, research_panel
import test_forecast_ui
import test_rank_ui


class CapitalPolicyUiTests(unittest.TestCase):
    def registered(self, version='historical-test', policy='fixed_amount'):
        return {'version':version,'capital_policy':policy,'account_config':{
            'paper_cash':'100000','paper_max_position':'5000','paper_max_sector':'20000'},
            'rules':{'max_holdings':20,'cash_reserve':'.10'}}

    def test_rule_details_distinguish_fixed_and_ratio_without_guessing_current_amounts(self):
        protocol={'version':'historical-test'};registered=self.registered();before=copy.deepcopy(registered)
        legacy=capital_policy_note(protocol,registered)
        self.assertIn('固定金额上限，单股 ¥5,000.00、行业 ¥20,000.00',legacy)
        self.assertEqual(legacy,capital_policy_note({**protocol,'capital_policy':'fixed_amount'},registered))
        protocol['capital_policy']='equity_ratio';ratio=self.registered(policy='equity_ratio')
        page=research_panel({}, {'protocol':protocol,'status':'diagnostic'},registered_protocol=ratio)
        self.assertGreater(page.index('id="capital-policy"'),page.index('<summary>策略规则、来源与完整指标</summary>'))
        for text in ('单股上限 5.00%、行业上限 20.00%','20 个等权槽位','目标预算 4.50% 净值',
                     '保留 10% 现金','金额随净值涨跌','不要求买满','冻结股保留原数量','不自动触发日内卖出'):
            self.assertIn(text,page)
        self.assertNotIn('¥5,000.00',page);self.assertNotIn('¥20,000.00',page)
        ratio['account_config']['paper_cash']='200000'
        changed=capital_policy_note(protocol,ratio)
        self.assertIn('单股上限 2.50%、行业上限 10.00%',changed)
        self.assertIn('目标预算 2.50%',changed)
        self.assertEqual(registered,before)

    def test_unknown_missing_and_conflicting_rules_are_not_silently_fixed(self):
        for policy in ('unknown','',None,[],{'x':1},'<img src=x>'):
            page=capital_policy_note({'capital_policy':policy})
            self.assertIn('待核实',page)
            self.assertNotIn('历史资金规则：固定金额',page);self.assertNotIn('<img',page)
        self.assertIn('单股 ¥—、行业 ¥—',capital_policy_note({}))
        self.assertIn('未核实',capital_policy_note({'capital_policy':'equity_ratio'}))
        self.assertIn('不一致',capital_policy_note({'version':'other','capital_policy':'equity_ratio'},self.registered(policy='equity_ratio')))
        for cash in ('0','NaN',None):
            registered=self.registered(policy='equity_ratio');registered['account_config']['paper_cash']=cash
            self.assertIn('未核实',capital_policy_note({'version':'historical-test','capital_policy':'equity_ratio'},registered))

    def test_ratio_candles_and_actual_operations_require_matching_registered_capital_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,result=test_rank_ui.RankUiTests().fixture(workspace)
            legacy=historical_chart_data(workspace)
            write=lambda p,v:p.write_text(json.dumps(v),encoding='utf-8')
            summary['protocol']['capital_policy']='equity_ratio'
            registered=self.registered(folder.name,'equity_ratio')
            result.update(initial_cash='100000.00',assumptions={'capital_policy':'equity_ratio',
                'position_limit_ratio':'0.05','sector_limit_ratio':'0.20'})
            paths={workspace/'backtests/latest.json':summary,folder/'summary.json':summary,
                   folder/'protocol.json':registered,folder/'yield-rank-result.json':result}
            for p,v in paths.items():write(p,v)
            before={p:p.read_bytes() for p in workspace.rglob('*.json')}
            self.assertEqual(historical_chart_data(workspace),legacy)
            self.assertEqual(before,{p:p.read_bytes() for p in workspace.rglob('*.json')})
            changes=[(workspace/'backtests/latest.json',{**summary,'protocol':{**summary['protocol'],'capital_policy':'fixed_amount'}}),
                     (folder/'summary.json',{**summary,'protocol':{**summary['protocol'],'capital_policy':'unknown'}}),
                     (folder/'protocol.json',{**registered,'capital_policy':'fixed_amount'}),
                     (folder/'protocol.json',{**registered,'version':'other'}),
                     (folder/'protocol.json',{}),
                     (folder/'yield-rank-result.json',{**result,'initial_cash':'99999'})]
            for assumption in ({}, {'capital_policy':'fixed_amount'},
                               {**result['assumptions'],'position_limit_ratio':'0.06'},
                               {**result['assumptions'],'sector_limit_ratio':.2},
                               {**result['assumptions'],'capital_policy':None}):
                changes.append((folder/'yield-rank-result.json',{**result,'assumptions':assumption}))
            for path,value in changes:
                with self.subTest(path=path.name,value=value):
                    write(path,value);self.assertEqual(historical_chart_data(workspace),{});write(path,paths[path])
            (folder/'protocol.json').unlink();self.assertEqual(historical_chart_data(workspace),{})
            # Legacy fixed archives have no new assumptions fields and keep their original operations.
            summary['protocol'].pop('capital_policy');result.pop('assumptions');result.pop('initial_cash')
            for p,v in ((workspace/'backtests/latest.json',summary),(folder/'summary.json',summary),(folder/'yield-rank-result.json',result)):write(p,v)
            self.assertEqual(historical_chart_data(workspace),legacy)
            result['assumptions']={'capital_policy':'equity_ratio','position_limit_ratio':'.05','sector_limit_ratio':'.2'}
            write(folder/'yield-rank-result.json',result);self.assertEqual(historical_chart_data(workspace),{})

    def test_forecast_source_match_includes_capital_policy_with_legacy_default(self):
        data={**test_forecast_ui.ForecastUiTests().data(),'source_engine_sha256':'a'*64}
        current={'source_engine_sha256':'a'*64,'protocol':{}}
        self.assertIn('<summary>独立预测诊断</summary>',forecast_diagnostic_panel(data,current))
        data['capital_policy']='fixed_amount'
        self.assertIn('<summary>独立预测诊断</summary>',forecast_diagnostic_panel(data,current))
        for value in ('equity_ratio','future',None,[]):
            self.assertIn('<summary>其他版本的预测诊断</summary>',forecast_diagnostic_panel({**data,'capital_policy':value},current))
        current['protocol']['capital_policy']='equity_ratio';data['capital_policy']='equity_ratio'
        self.assertIn('<summary>独立预测诊断</summary>',forecast_diagnostic_panel(data,current))
        for value in ('future',None,[]):
            current['protocol']['capital_policy']=value;data['capital_policy']=value
            self.assertIn('<summary>其他版本的预测诊断</summary>',forecast_diagnostic_panel(data,current))

    def test_subcent_config_uses_real_ledger_amounts_without_rounding_away_result_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder,summary,result=test_rank_ui.RankUiTests().fixture(workspace)
            cfg={**load_config(),'paper_cash':'100000.005','paper_max_position':'5000.005','paper_max_sector':'20000.005'}
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:
                ledger.initialize('paper',cfg['paper_cash'],cfg['paper_max_position'],cfg['paper_max_sector'],'20250731')
                account=ledger.account('paper')
            finally:
                ledger.close()
            self.assertEqual([account[k] for k in ('initial_cash','max_position','max_sector')],['100000.01','5000.01','20000.01'])
            registered={**self.registered(folder.name,'equity_ratio'),'account_config':cfg}
            ratios=tuple(dec(account[k])/dec(account['initial_cash']) for k in ('max_position','max_sector'))
            self.assertEqual(capital_ratios(registered),ratios)
            fixed=capital_policy_note({'version':folder.name},{**registered,'capital_policy':'fixed_amount'})
            self.assertIn('单股 ¥5,000.01、行业 ¥20,000.01',fixed)
            summary['protocol']['capital_policy']='equity_ratio'
            result.update(initial_cash=account['initial_cash'],assumptions={'capital_policy':'equity_ratio',
                'position_limit_ratio':str(ratios[0]),'sector_limit_ratio':str(ratios[1])})
            write=lambda p,v:p.write_text(json.dumps(v),encoding='utf-8')
            for p,v in ((folder/'protocol.json',registered),(folder/'summary.json',summary),
                        (workspace/'backtests/latest.json',summary),(folder/'yield-rank-result.json',result)):
                write(p,v)
            self.assertEqual(historical_chart_data(workspace)['runs'][0]['fills'],result['trades'])
            # This altered value rounds to the same cent, but was not the actual initial balance.
            write(folder/'yield-rank-result.json',{**result,'initial_cash':'100000.006'})
            self.assertEqual(historical_chart_data(workspace),{})
            raw_ratio=str(dec(cfg['paper_max_position'])/dec(cfg['paper_cash']))
            write(folder/'yield-rank-result.json',{**result,'assumptions':{**result['assumptions'],'position_limit_ratio':raw_ratio}})
            self.assertEqual(historical_chart_data(workspace),{})
            write(folder/'yield-rank-result.json',result)
            for key in ('paper_cash','paper_max_position','paper_max_sector'):
                zero={**registered,'account_config':{**cfg,key:'.004'}}
                self.assertIsNone(capital_ratios(zero))
                write(folder/'protocol.json',zero);self.assertEqual(historical_chart_data(workspace),{})

    def test_render_reads_frozen_account_config_instead_of_current_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            folder,summary,_=test_rank_ui.RankUiTests().fixture(workspace)
            registered=self.registered(folder.name);registered['account_config']['paper_max_position']='4321'
            (folder/'protocol.json').write_text(json.dumps(registered),encoding='utf-8')
            screen=json.loads((workspace/'screen.json').read_text(encoding='utf-8'));screen['source']='Tushare'
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:page=render(workspace,ledger,screen).read_text(encoding='utf-8')
            finally:ledger.close()
            self.assertIn('固定金额上限，单股 ¥4,321.00、行业 ¥20,000.00',page)


if __name__=='__main__':
    unittest.main()
