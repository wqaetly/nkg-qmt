import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from qmt.config import ROOT, load_config
from qmt.demo import create_demo
from qmt.ledger import Ledger
from qmt.report import chart_panel, optional_report, percent, research_panel, comparison_chart, period_diagnostics_panel, coverage_panel, automatic_panel, experiment_history_panel, data_warnings_panel, render


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

    def test_render_includes_real_summary_stock_flow_inside_existing_ledger_fold(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'))
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            original_summary=ledger.summary
            event={'symbol':'600001.SH','record_date':'20261103','ex_date':'20261104',
                   'stock_list_date':'20261105','record_quantity':100,'quantity':30,'ratio':'0.3',
                   'source':'https://example.com/distribution.pdf'}
            try:
                with patch.object(ledger,'summary',side_effect=lambda mode:{**original_summary(mode),'stock_events':[event]}):
                    page=render(workspace,ledger,report).read_text(encoding='utf-8')
                self.assertIn('<summary>成交与现金流水</summary>',page)
                self.assertIn('<details class="stock-events"><summary>送转股数流水 · 1 笔',page)
                self.assertIn('除权日 20261104 · 新增 30 股',page)
                self.assertIn('登记日 20261103 持股 100 股',page)
                self.assertIn('上市日 20261105',page)
                self.assertIn('href="https://example.com/distribution.pdf"',page)
            finally:
                ledger.close()

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

    def test_history_displays_each_protocols_earnings_basis_without_rewriting_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            for version,basis,cost in [('v17',None,1),('v18','ttm',2),('unknown','<script>basis</script>',3)]:
                folder=workspace/'backtests'/version;folder.mkdir(parents=True)
                protocol={'version':version,**({'earnings_basis':basis} if basis is not None else {})}
                (folder/'protocol.json').write_text(json.dumps(protocol),encoding='utf-8')
                (folder/'summary.json').write_text(json.dumps({'status':'diagnostic','primary_metrics':{'costs':cost}}),encoding='utf-8')
            before={path:path.read_bytes() for path in workspace.rglob('*.json')}
            page=experiment_history_panel(workspace)
            self.assertIn('<th>盈利口径</th>',page)
            self.assertIn('1.00</td><td>历史及 TTM 取低</td>',page)
            self.assertIn('2.00</td><td>最近十二个月取低</td>',page)
            self.assertIn('3.00</td><td>未识别口径</td>',page)
            self.assertIn('未声明盈利口径的旧归档按“历史及 TTM 取低”解释',page)
            self.assertIn('口径变化不是收益验证',page)
            self.assertNotIn('<script>basis</script>',page)
            self.assertEqual(before,{path:path.read_bytes() for path in workspace.rglob('*.json')})

    def test_latest_earnings_basis_is_explicit_and_does_not_claim_normalization_or_validation(self):
        for protocol in ({},{'earnings_basis':'conservative_minimum'}):
            page=research_panel(self.research(),{'protocol':protocol})
            self.assertIn('盈利口径：历史及 TTM 取低',page)
            self.assertIn('归母 TTM、扣非 TTM 四者最低值',page)
        backtest={'protocol':{'earnings_basis':'ttm'},'runs':[{'label':'5%主方案','metrics':{'cagr':'.12'}}],
                  'comparison_series':[{'label':'策略','kind':'strategy','points':[{'day':'20230103','value':1}]}]}
        before=json.dumps(backtest,sort_keys=True)
        page=research_panel(self.research(),backtest)
        self.assertIn('盈利口径：最近十二个月取低',page)
        self.assertIn('id="earnings-basis"',page)
        self.assertIn('只取归母 TTM 与扣非 TTM 较低值，不再与历史年度利润取更低值',page)
        self.assertIn('不等同于正常化盈利，也不代表收益已验证',page)
        self.assertIn('12.00%',page)
        self.assertIn('待验证，不是收益承诺',page)
        self.assertEqual(page.count('class="research-stock"'),20)
        payload=page.split('id="comparison-data">',1)[1].split('</script>',1)[0]
        self.assertEqual(json.loads(payload),backtest['comparison_series'])
        self.assertEqual(before,json.dumps(backtest,sort_keys=True))
        unknown=research_panel({}, {'protocol':{'earnings_basis':'<script>basis</script>'}})
        self.assertIn('盈利口径：未识别口径',unknown)
        self.assertNotIn('<script>basis</script>',unknown)
        self.assertNotIn('id="earnings-basis"',research_panel(self.research(),{}))

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

    def test_full_annual_candidate_pool_separates_coverage_from_holdings_and_daily_research(self):
        backtest={'protocol':{'cohort_limit':None},'coverage':{'stocks':35},
                  'selection_audit':{'target_count':None,'holding_limit':20,'stocks':[{} for _ in range(35)],
                      'rebalances':[{'day':'20251231','symbols':list(range(35)),'candidate_count':50,
                                     'evaluated_count':44,'rejected':[{'reason':'财务待核实'}]}]}}
        before=json.dumps(backtest,sort_keys=True)
        page=research_panel(self.research(),backtest)
        self.assertIn('历年候选池覆盖 35 只',page)
        self.assertIn('年末初筛候选全部检查，合格候选池不设数量上限',page)
        self.assertIn('持仓最多 20 只',page)
        self.assertNotIn('每期最多',page)
        self.assertNotIn('None',page)
        self.assertIn('<th>已检查</th>',page)
        self.assertIn('20251231</td><td>35</td><td>50</td><td>15</td><td>44',page)
        self.assertIn('仅在年末重筛候选池，月度评估当期候选；不是每月扫描全市场',page)
        self.assertIn('实际入池是候选覆盖，不是实际持仓',page)
        self.assertEqual(page.count('class="research-stock"'),20)
        self.assertIn('20 / 20',page)
        self.assertEqual(before,json.dumps(backtest,sort_keys=True))

    def test_legacy_annual_missing_evaluated_count_stays_unknown_and_explicit_zero_is_preserved(self):
        backtest={'selection_audit':{'stocks':[{} for _ in range(7)],'rebalances':[
            {'day':'20201231','symbols':list(range(7)),'candidate_count':30,
             'rejected':[{'reason':'财务待核实'} for _ in range(2)]},
            {'day':'20211231','symbols':[],'candidate_count':0,'evaluated_count':0}]}}
        page=research_panel(self.research(),backtest)
        self.assertIn('历年涉及 7 只；每期最多 20 只，共 2 次建池',page)
        self.assertIn('20201231</td><td>7</td><td>30</td><td>23</td><td>—',page)
        self.assertIn('20211231</td><td>0</td><td>0</td><td>0</td><td>0',page)
        self.assertNotIn('持仓最多',page)
        self.assertNotIn('候选全部检查',page)
        self.assertNotIn('不设数量上限',page)
        backtest['selection_audit'].pop('rebalances')
        self.assertIn('已评估 — 家',research_panel(self.research(),backtest))

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

    def test_unused_quarterly_share_warning_survives_detail_render_without_approval_or_value_changes(self):
        warning={'api':'balancesheet','period':'20220930','ann_date':'20221028','field':'total_share',
                 'values':['100000000','100000001','<script>原值</script>'],
                 'originals':[{'total_share':'100000000','private_test_marker':'完整原始行不应直接展开'}]}
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'))
            report['rows'][0]['data_warnings']=[warning]
            before=json.dumps(report,ensure_ascii=False,sort_keys=True)
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:
                page=render(workspace,ledger,report).read_text(encoding='utf-8')
            finally:
                ledger.close()
            self.assertEqual(page.count('class="data-warnings"'),1)
            self.assertIn('报告期 20220930 · 公告日 20221028 · balancesheet.total_share',page)
            self.assertIn('100000000 / 100000001 / &lt;script&gt;原值&lt;/script&gt;',page)
            self.assertIn('未参与本次数值计算',page)
            self.assertIn('尚未核对原文',page)
            self.assertIn('年报股数及其他字段冲突仍按阻断处理',page)
            self.assertNotIn('<script>原值</script>',page)
            self.assertNotIn('完整原始行不应直接展开',page)
            self.assertEqual(before,json.dumps(report,ensure_ascii=False,sort_keys=True))
        self.assertEqual(data_warnings_panel([]),'')

    def test_dividend_cash_html_distinguishes_verified_shares_proxy_unknown_and_zero(self):
        verified={'end_date':'20251231','record_date':'20260615','ex_date':'20260616','cash_div_tax':'0.2',
                  'ordinary_cash_div_tax':'0.15','shares':'1000000000','basis':'verified_distribution_shares',
                  'share_period':None,'source':'https://example.com/dividend?a=1&b=2','available_on':'20260610',
                  'estimated_dividend_cash':'200000000','estimated_ordinary_cash':'150000000'}
        proxy={**verified,'end_date':'20250630','record_date':'20250810','ex_date':'20250811',
               'cash_div_tax':'0.3','ordinary_cash_div_tax':'0.3','shares':'100000000','basis':'year_end_proxy',
               'share_period':'20241231','source':None,'available_on':'20250401',
               'estimated_dividend_cash':'30000000','estimated_ordinary_cash':'30000000'}
        unknown={**proxy,'end_date':'20241231','shares':None,'source':'javascript:alert(1)',
                 'available_on':'<script>日期</script>','estimated_dividend_cash':None,'estimated_ordinary_cash':None}
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config();create_demo(workspace,cfg)
            report=json.loads((workspace/'screen.json').read_text(encoding='utf-8'))
            annual=report['rows'][0]['annual']
            annual[0].update(estimated_dividend_cash='230000000',estimated_ordinary_cash='180000000',dividend_cash_basis=[verified,proxy])
            annual[1].update(estimated_dividend_cash=None,estimated_ordinary_cash=None,dividend_cash_basis=[verified,unknown])
            annual[2].update(estimated_dividend_cash='0',estimated_ordinary_cash='0',dividend_cash_basis=[])
            before=json.dumps(report,ensure_ascii=False,sort_keys=True)
            ledger=Ledger(workspace/'ledger.sqlite3',cfg)
            try:
                page=render(workspace,ledger,report).read_text(encoding='utf-8')
            finally:
                ledger.close()
            self.assertEqual(page.count('class="dividend-cash"'),1)
            self.assertIn('2025</td><td>230,000,000.00</td><td>180,000,000.00',page)
            self.assertIn('2024</td><td>—</td><td>—',page)
            self.assertIn('2023</td><td>0.00</td><td>0.00',page)
            self.assertIn('2022</td><td>—</td><td>—',page)
            self.assertIn('已核实派息股数</strong> · 1,000,000,000 股',page)
            self.assertIn('年报期末股数代理（未核实派息股数）</strong> · 100,000,000 股 · 代理年报期 20241231',page)
            self.assertIn('依据可用日 20260610',page)
            self.assertIn('登记日 20260615 · 除息日 20260616',page)
            self.assertIn('href="https://example.com/dividend?a=1&amp;b=2"',page)
            self.assertIn('本笔公司金额 200,000,000.00 元 / 剔除已知特别后 150,000,000.00 元',page)
            self.assertIn('股数核实不代表普通派息可持续',page)
            self.assertIn('登记日实际持股 × 每股派息',page)
            self.assertIn('缺失时不以已知部分代替全年总额',page)
            self.assertNotIn('href="javascript:',page)
            self.assertNotIn('<script>日期</script>',page)
            self.assertIn('&lt;script&gt;日期&lt;/script&gt;',page)
            self.assertIn('id="market-chart-data"',page)
            self.assertEqual(before,json.dumps(report,ensure_ascii=False,sort_keys=True))

    def test_legacy_summary_html_does_not_invent_company_cash_or_share_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);cfg=load_config()
            page=create_demo(workspace,cfg).read_text(encoding='utf-8')
            self.assertIn('旧摘要未记录公司派息总额与逐事件股本依据，未从 DPS 回推金额',page)
            self.assertNotIn('class="dividend-cash"',page)
            self.assertNotIn('已核实派息股数',page)
            self.assertIn('已公告现金 DPS / 元',page)
            self.assertIn('id="market-chart-data"',page)

    def test_historical_unused_share_conflicts_preserve_counts_scope_and_unverified_status(self):
        backtest={'diagnostics':{'evaluations':80,'unused_share_conflicts':[
            {'symbol':'600001.SH','period':'20220930','field':'total_share','count':12,'active_count':9},
            {'symbol':'600001.SH','period':'20230630','field':'total_share','count':3,'active_count':0},
            {'symbol':'<script>代码</script>','period':'20230930','field':'<img>','count':1,'active_count':1}]}}
        before=json.dumps(backtest,sort_keys=True)
        page=research_panel(self.research(),backtest)
        self.assertIn('id="unused-share-conflicts"',page)
        self.assertIn('600001.SH</td><td>20220930</td><td>total_share</td><td>12</td><td>9',page)
        self.assertIn('600001.SH</td><td>20230630</td><td>total_share</td><td>3</td><td>0',page)
        self.assertIn('同股不同报告期可分别计数',page)
        self.assertIn('不能当作独立公司数或成交笔数',page)
        self.assertIn('原文尚未核实',page)
        self.assertNotIn('<script>代码</script>',page)
        self.assertNotIn('<img>',page)
        self.assertEqual(before,json.dumps(backtest,sort_keys=True))
        for diagnostics in ({'evaluations':80},{'unused_share_conflicts':[]}):
            self.assertNotIn('id="unused-share-conflicts"',research_panel(self.research(),{'diagnostics':diagnostics}))

    def test_entry_funnel_displays_provided_counts_with_distinct_scope_and_units(self):
        stages=[('当期候选',940),('财务通过',589),('估值通过',208),('流动性通过',199),('基础候选通过',198),('全部买入条件通过',41)]
        backtest={'diagnostics':{'evaluations':2000,'entry_funnel':[{'stage':stage,'count':count} for stage,count in stages],
                  'candidate_months':{'months':47,'empty':24,'total_candidates':41}}}
        page=research_panel(self.research(),backtest)
        for stage,count in stages:
            self.assertIn(f'{stage}</td><td>{count}</td>',page)
        self.assertIn('共 2000 次公司 / 时点评估',page)
        self.assertIn('排除退池后继续跟踪',page)
        self.assertIn('按顺序取交集',page)
        self.assertIn('不表示单项条件的独立因果作用',page)
        self.assertIn('合格候选次数不是订单或成交笔数',page)
        self.assertIn('月度评估 47 次，其中 24 次无买入候选；合格候选累计 41 个公司·月',page)
        self.assertEqual(page.count('class="research-stock"'),20)
        self.assertIn('<details class="research-results" id="research-diagnostics">',page)

    def test_missing_funnel_is_not_reconstructed_and_explicit_zero_is_preserved(self):
        old=research_panel(self.research(),{'diagnostics':{'evaluations':5,'states':{'满足价格条件':0}}})
        self.assertNotIn('id="entry-funnel"',old)
        self.assertNotIn('id="candidate-months"',old)
        self.assertNotIn('持有门槛',old)
        page=research_panel(self.research(),{'diagnostics':{
            'entry_funnel':[{'stage':'<script>阶段</script>','count':0}],
            'candidate_months':{'months':0,'empty':0,'total_candidates':0}}})
        self.assertIn('&lt;script&gt;阶段&lt;/script&gt;</td><td>0',page)
        self.assertNotIn('<script>阶段</script>',page)
        self.assertIn('月度评估 0 次，其中 0 次无买入候选；合格候选累计 0 个公司·月',page)

    def test_retention_summary_uses_protocol_and_labels_month_end_checks(self):
        protocol={'retention_yield':'0.03','monthly_targets':'月末按持有条件保留目标 <script>规则</script>'}
        page=research_panel(self.research(),{'protocol':protocol,'limitations':[protocol['monthly_targets']]})
        self.assertIn('持有门槛 3.00% / 仅月末检查',page)
        self.assertIn('回放调仓规则：月末按持有条件保留目标 &lt;script&gt;规则&lt;/script&gt;',page)
        self.assertNotIn('<script>规则</script>',page)
        self.assertEqual(page.count('月末按持有条件保留目标'),1)
        protocol['retention_yield']='0.025'
        self.assertIn('持有门槛 2.50% / 仅月末检查',research_panel({}, {'protocol':protocol}))
        protocol['retention_yield']='nan'
        self.assertNotIn('持有门槛',research_panel({}, {'protocol':protocol}))

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
