import copy
import json
import tempfile
import unittest
from pathlib import Path

from qmt.report import comparison_chart, historical_chart_data, research_panel, suspension_valuations_panel


class SuspensionUiTests(unittest.TestCase):
    def event(self, day='20250718'):
        return {'day':day,'symbol':'600566.SH','quantity':100,'price':'27.60','price_date':'20250717',
                'market_value':'2760.01','weight':'.031234','method':'verified_suspension_prior_close',
                'tradable':False,'stale_sessions':1,'proofs':[
                    {'symbol':'600566.SH','day':day,'full_day':True,'available_on':'20250718',
                     'source':'https://example.com/suspension.pdf?a=1&b=2','source_sha256':'a'*64,'pages':[1,2]}]}

    def fixture(self):
        return {'days':1,'company_days':1,'rows':[self.event()]}

    def test_actual_valuation_is_inside_existing_fold_without_recomputing_amounts(self):
        data=self.fixture();before=copy.deepcopy(data)
        summary={'diagnostics':{'suspension_valuations':data}}
        page=research_panel({},summary)
        self.assertIn('<details class="research-results" id="research-diagnostics">',page)
        self.assertGreater(page.index('id="suspension-valuations"'),page.index('id="research-diagnostics"'))
        for text in ('涉及 1 个交易日、1 公司·日','公司·日不等于冻结表的公司·月','不是订单或成交次数',
                     '不代表当日成交价或公允价值','禁止以此价格模拟当日成交','证明可获知日是证据当时可用的日期',
                     '<td>20250718</td><td>600566.SH</td><td>100</td><td>27.60',
                     '<td>20250717</td><td>2,760.01</td><td>3.12%</td><td>1</td>',
                     '沿用前收盘；禁止成交','可获知日 20250718','页码 1、2'):
            self.assertIn(text,page)
        self.assertEqual(data,before)

    def test_reverse_dates_and_multiple_daily_proofs_are_preserved(self):
        first=self.event();second=self.event('20250721')
        second['stale_sessions']=2
        second['proofs'].insert(0,copy.deepcopy(first['proofs'][0]))
        page=suspension_valuations_panel({'days':2,'company_days':2,'rows':[first,second]})
        self.assertLess(page.index('<td>20250721</td>'),page.index('<td>20250718</td>'))
        self.assertIn('<details><summary>逐日证据 2 项</summary>',page)
        self.assertNotIn('<details open',page)
        self.assertIn('证明日期 20250718',page)
        self.assertIn('证明日期 20250721',page)
        self.assertEqual(page.count('停牌原文</a>'),3)

    def test_legacy_absence_is_not_explicit_zero_or_missing_details(self):
        for value in (None,{}):
            self.assertEqual(suspension_valuations_panel(value),'')
            self.assertNotIn('id="suspension-valuations"',research_panel({}, {'diagnostics':{'suspension_valuations':value}}))
        page=suspension_valuations_panel({'days':0,'company_days':0,'rows':[]})
        self.assertIn('本版停牌沿用前收盘的持仓估值为 0 次',page)
        self.assertNotIn('<table',page)
        page=suspension_valuations_panel({'days':None,'company_days':None})
        self.assertIn('涉及 — 个交易日、— 公司·日',page)
        self.assertIn('不能当作 0 次',page)
        self.assertNotIn('估值为 0 次',page)

    def test_method_and_no_trade_flag_must_both_be_explicit(self):
        for change in ({'method':None},{'method':'unverified'},{'tradable':None},{'tradable':True},{'tradable':0}):
            with self.subTest(change=change):
                data=self.fixture();data['rows'][0].update(change)
                page=suspension_valuations_panel(data)
                self.assertIn('<small>估值方法或禁止成交标记未核实</small>',page)
                self.assertNotIn('<small>沿用前收盘；禁止成交</small>',page)
                self.assertNotIn('允许成交',page)
        data=self.fixture();data['rows'][0]['proofs'][0]['full_day']=None
        self.assertIn('全日停牌未核实',suspension_valuations_panel(data))

    def test_all_text_is_escaped_and_unsafe_evidence_is_not_linked(self):
        bad='<img src=x onerror=alert(1)>'
        data=self.fixture();data.update(days=bad,company_days=bad)
        data['rows'][0].update(day=bad,symbol=bad,quantity=bad,price=bad,price_date=bad,stale_sessions=bad)
        data['rows'][0]['proofs'][0].update(symbol=bad,day=bad,available_on=bad,pages=[bad],source='javascript:alert(1)')
        before=copy.deepcopy(data);page=suspension_valuations_panel(data)
        self.assertNotIn('<img',page);self.assertNotIn('javascript:',page);self.assertNotIn('<a ',page)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;',page)
        self.assertIn('原文链接不可用',page)
        self.assertEqual(data,before)
        self.assertIn('href="https://example.com/suspension.pdf?a=1&amp;b=2"',suspension_valuations_panel(self.fixture()))

    def test_comparison_note_is_static_and_does_not_change_series(self):
        series=[{'kind':'strategy','label':'主方案','points':[{'day':'20250717','value':1},{'day':'20250718','value':1.01}]}]
        summary={'comparison_series':series}
        before=comparison_chart(summary)
        self.assertNotIn('href="#suspension-valuations"',before)
        summary['diagnostics']={'suspension_valuations':self.fixture()}
        original=copy.deepcopy(summary);after=comparison_chart(summary)
        self.assertIn('href="#suspension-valuations"',after)
        self.assertIn('不代表当日成交价或公允价值',after)
        script=lambda page:page.split('id="comparison-data">',1)[1].split('</script>',1)[0]
        self.assertEqual(script(before),script(after));self.assertEqual(summary,original)

    def test_valuation_records_do_not_become_candles_or_trade_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp);folder=workspace/'backtests'/'historical-fixture';folder.mkdir(parents=True)
            summary={'status':'diagnostic','protocol':{'version':'historical-fixture','entry_mode':'yield_rank'},
                     'coverage':{'start':'20250717','end':'20250721'},
                     'runs':[{'label':'股息排序 · 全期','status':'diagnostic'}]}
            bars={day:{'open':'27.6','high':'28','low':'27','close':'27.6','vol':'100'} for day in ('20250717','20250721')}
            result={'start':'20250717','end':'20250721','plans':[],
                    'trades':[{'symbol':'600566.SH','trade_date':'20250717','side':'buy','quantity':100,'price':'27.60'}]}
            def write(name,data):
                (folder/name).write_text(json.dumps(data),encoding='utf-8')
            write('summary.json',summary);write('market.json',{'bars':{'600566.SH':bars}})
            write('yield-rank-result.json',result)
            latest=workspace/'backtests/latest.json';latest.write_text(json.dumps(summary),encoding='utf-8')
            before=historical_chart_data(workspace)
            result['valuation_events']=[self.event()]
            summary['diagnostics']={'suspension_valuations':self.fixture()}
            write('yield-rank-result.json',result);write('summary.json',summary)
            latest.write_text(json.dumps(summary),encoding='utf-8')
            files={path:path.read_bytes() for path in workspace.rglob('*.json')}
            after=historical_chart_data(workspace)
            self.assertEqual(before,after)
            self.assertEqual([bar['day'] for bar in after['stocks']['600566.SH']['candles']],['20250717','20250721'])
            self.assertEqual(after['runs'][0]['fills'],result['trades'])
            self.assertEqual(files,{path:path.read_bytes() for path in workspace.rglob('*.json')})


if __name__=='__main__':
    unittest.main()
