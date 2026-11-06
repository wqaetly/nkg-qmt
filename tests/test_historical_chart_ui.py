import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from qmt.config import ROOT
from qmt.report import chart_panel, historical_chart_data, research_panel, stock_events_panel


class HistoricalChartUiTests(unittest.TestCase):
    def fixture(self, workspace):
        folder=workspace/'backtests'/'historical-dividend-v9-quality-long'
        folder.mkdir(parents=True)
        summary={'protocol':{'version':folder.name},'status':'diagnostic',
                 'coverage':{'start':'20250731','end':'20250804','stocks':26},
                 'runs':[{'label':'4%股息门槛 · 全期','status':'diagnostic','artifact':'C:/outside/untrusted-result.json'},
                         {'label':'5%股息门槛 · 全期','status':'diagnostic'},
                         {'label':'6%股息门槛 · 全期','status':'blocked'}]}
        write=lambda file,data:file.write_text(json.dumps(data),encoding='utf-8')
        write(folder/'summary.json',summary)
        write(workspace/'backtests/latest.json',summary)
        bars={day:{'date':day,'open':'8','high':'9','low':'7','close':'8.2','vol':'100'} for day in ('20250804','20250731','20250801')}
        write(folder/'market.json',{'bars':{f'600{i:03d}.SH':bars for i in range(26)}})
        write(folder/'cohort.json',{'stocks':[{'symbol':'600000.SH','name':'</script><img src=x onerror=alert(1)>'}]})
        result={'start':'20250731','end':'20250804',
                'plans':[{'id':1,'symbol':'600000.SH','side':'buy','signal_date':'20250731','execute_date':'20250801','quantity':500,'limit_price':'8.2','status':'filled','note':'历史目标'}],
                'trades':[{'id':1,'plan_id':1,'symbol':'600000.SH','side':'buy','trade_date':'20250801','quantity':500,'price':'8.18','fee':'5.00'}]}
        write(folder/'yield-004-result.json',result)
        write(folder/'yield-005-result.json',{**result,'plans':[],'trades':[]})
        write(folder/'yield-006-result.json',result)
        return folder,summary,result

    def test_history_reads_only_successful_current_results_and_keeps_union_above_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            folder,_,result=self.fixture(workspace)
            before={p:p.read_bytes() for p in workspace.rglob('*.json')}
            history=historical_chart_data(workspace)
            self.assertEqual(len(history['stocks']),26)
            self.assertEqual([run['id'] for run in history['runs']],['004','005'])
            self.assertEqual(history['runs'][0]['fills'],result['trades'])
            self.assertEqual(history['runs'][1]['fills'],[])
            candles=history['stocks']['600000.SH']['candles']
            self.assertEqual([bar['day'] for bar in candles],['20250731','20250801','20250804'])
            self.assertIsInstance(candles[0]['open'],float)
            page=chart_panel(workspace,{'as_of':'20250804','source':'Tushare','rows':[]},{'focus_symbols':[]})
            self.assertIn('id="chart-source"',page)
            self.assertIn('<option value="daily">日常模拟</option>',page)
            self.assertIn('本次历史 5% · 主方案',page)
            self.assertNotIn('本次历史 6%',page)
            self.assertIn('id="chart-trade"',page)
            self.assertNotIn('</script><img',page)
            self.assertIn('\\u003c/script\\u003e',page)
            self.assertEqual(before,{p:p.read_bytes() for p in workspace.rglob('*.json')})
            self.assertEqual(chart_panel(workspace,{'source':'DEMO','rows':[]},{'focus_symbols':[]}), '')

    def test_missing_blocked_mismatched_and_unsafe_versions_do_not_add_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            folder,summary,result=self.fixture(workspace)
            latest=workspace/'backtests/latest.json'
            for version in ('../outside','historical-v9/../../outside','C:\\outside','historical-v9\\cohorts','historical-v9/child'):
                latest.write_text(json.dumps({**summary,'protocol':{'version':version}}),encoding='utf-8')
                self.assertEqual(historical_chart_data(workspace),{})
            latest.write_text(json.dumps({**summary,'status':'blocked'}),encoding='utf-8')
            self.assertEqual(historical_chart_data(workspace),{})
            latest.write_text(json.dumps(summary),encoding='utf-8')
            (folder/'yield-004-result.json').unlink()
            (folder/'yield-005-result.json').write_text(json.dumps({**result,'start':'20240101'}),encoding='utf-8')
            self.assertEqual(historical_chart_data(workspace),{})
            (folder/'yield-004-result.json').write_text(json.dumps(result),encoding='utf-8')
            (folder/'market.json').write_text(json.dumps({'bars':{'600000.SH':{'20250731':{'open':'nan'}}}}),encoding='utf-8')
            self.assertEqual(historical_chart_data(workspace),{})

    def test_stock_events_keep_raw_prices_and_source_isolation_with_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)
            folder,_,result=self.fixture(workspace)
            event={'mode':'paper','reference':'stock-1','symbol':'600000.SH','record_date':'20250801',
                   'ex_date':'20250804','stock_list_date':'20250805','record_quantity':500,'ratio':'0.3',
                   'quantity':150,'source':'https://example.com/notice.pdf'}
            (folder/'yield-004-result.json').write_text(json.dumps({**result,'stock_events':[event]}),encoding='utf-8')
            raw_before=(folder/'market.json').read_bytes()
            history=historical_chart_data(workspace)
            self.assertEqual(history['runs'][0]['stock_events'],[event])
            self.assertEqual(history['runs'][1]['stock_events'],[])
            self.assertEqual(history['stocks']['600000.SH']['candles'][-1]['close'],8.2)
            self.assertEqual((folder/'market.json').read_bytes(),raw_before)
            daily={**event,'quantity':30,'source':'javascript:alert(1)','reference':'</script><img src=x onerror=alert(1)>'}
            page=chart_panel(workspace,{'as_of':'20250804','source':'Tushare','rows':[]},
                             {'focus_symbols':[],'account':{'stock_events':[daily]}})
            payload=json.loads(re.search(r'id="market-chart-data">(.*?)</script>',page,re.S)[1])
            self.assertEqual(payload['stock_events'],[{**daily,'source':''}])
            self.assertEqual(payload['historical']['runs'][0]['stock_events'],[event])
            self.assertNotIn('</script><img',page)
            self.assertNotIn('javascript:',page)
            self.assertIn('送转不是买入，不重复调整原始 K 线',page)
            flow=stock_events_panel([event,{**daily,'record_date':None,'stock_list_date':None,'symbol':'<script>x</script>'}])
            self.assertIn('<details class="stock-events">',flow)
            self.assertNotIn(' open',flow)
            self.assertIn('新增 150 股',flow)
            self.assertIn('每登记股新增 0.3 股',flow)
            self.assertIn('https://example.com/notice.pdf',flow)
            self.assertIn('上市日 —',flow)
            self.assertIn('&lt;script&gt;x&lt;/script&gt;',flow)
            self.assertNotIn('javascript:',flow)
            self.assertEqual(stock_events_panel(None),'')
            self.assertEqual(stock_events_panel([]),'')

    def test_annual_cohort_counts_show_union_separately_from_each_year_cap(self):
        audit={'stocks':[{'symbol':str(i)} for i in range(25)],'target_count':20,'rebalances':[
            {'day':'20201231','symbols':list(range(20)),'candidate_count':30,'rejected':[{'reason':'财务待核实'}]},
            {'day':'20211231','symbols':list(range(5,25)),'candidate_count':35,'rejected':[{'reason':'财务待核实'}]}]}
        page=research_panel({}, {'coverage':{'stocks':25},'selection_audit':audit,'diagnostics':{'evaluations':500}})
        self.assertIn('历史涉及 25 只',page)
        self.assertIn('历年涉及 25 只；每期最多 20 只，共 2 次建池',page)
        self.assertNotIn('建池 25 只 / 目标 20 只',page)
        self.assertIn('20201231</td><td>20</td><td>30</td><td>10',page)
        self.assertIn('20211231</td><td>20</td><td>35</td><td>15',page)
        self.assertIn('建池淘汰（公司 / 建池时点）</td><td>财务待核实</td><td>2',page)
        self.assertIn('退出研究池后继续跟踪',page)

    @unittest.skipUnless(shutil.which('node'),'Node is needed for chart interaction self-check')
    def test_source_switch_and_trade_jump_preserve_signal_fill_dates_and_hide_live_reference(self):
        script=r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.handlers={};this.textContent='';this._value='';this.scrollWidth=1080;this.clientWidth=390;}
  get value(){return this.tag==='select'?(this.children.some(n=>n.value===this._value)?this._value:this.children[0]?.value||''):this._value;}
  set value(value){this._value=String(value);}
  get selectedOptions(){return this.children.filter(n=>n.value===this.value);}
  setAttribute(key,value){this.attrs[key]=value;}
  appendChild(node){this.children.push(node);return node;}
  replaceChildren(){this.children=[];this._value='';}
  addEventListener(event,handler){this.handlers[event]=handler;}
  scrollIntoView(){}
  focus(){}
}
const windowHandlers={};
const nodes=Object.fromEntries(['market-chart-data','chart-symbol','chart-source','chart-trade','chart-range','chart-context','chart-reference-legend','stock-chart','chart-tip','chart-explanation','chart-panel'].map(id=>[id,new Node(['chart-symbol','chart-source','chart-trade','chart-range'].includes(id)?'select':id)]));
const option=(select,value)=>{const node=new Node('option');node.value=value;node.textContent=value;select.appendChild(node);};
option(nodes['chart-source'],'daily');option(nodes['chart-source'],'004');option(nodes['chart-source'],'005');option(nodes['chart-range'],'60');option(nodes['chart-symbol'],'600000.SH');
const candles=Array.from({length:160},(_,index)=>{const date=new Date(Date.UTC(2025,0,1+index));return {day:date.toISOString().slice(0,10).replaceAll('-',''),open:8,high:9,low:7,close:8.2,vol:100};});
const fill={symbol:'600000.SH',side:'buy',quantity:500,price:'8.18',fee:'5',trade_date:candles[20].day,plan_id:1};
const plan={symbol:'600000.SH',side:'buy',quantity:500,limit_price:'8.2',id:1,signal_date:candles[19].day,execute_date:candles[20].day,status:'filled',note:'回放'};
const stockEvent={symbol:'600000.SH',record_date:candles[20].day,ex_date:candles[21].day,stock_list_date:candles[22].day,record_quantity:500,ratio:'0.3',quantity:150,source:'https://example.com/notice.pdf'};
const dailyEvent={...stockEvent,ex_date:candles[150].day,record_quantity:100,quantity:30,stock_list_date:null,source:''};
const stocks={'600000.SH':{candles}},names={'600000.SH':'测试公司'};
nodes['market-chart-data'].textContent=JSON.stringify({stocks,names,as_of:'20250609',assessments:[{symbol:'600000.SH',reference_price:8,state:'等待价格',reason:'日常原因'}],plans:[],fills:[],stock_events:[dailyEvent],notes:{},historical:{version:'historical-v9',start:candles[0].day,end:candles.at(-1).day,stocks,names,runs:[{id:'004',label:'历史4%',plans:[plan],fills:[fill],stock_events:[stockEvent]},{id:'005',label:'历史5%',plans:[],fills:[]}]}});
const button=new Node('button');button.dataset={chartSymbol:'600000.SH'};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{requestAnimationFrame:fn=>fn(),window:{addEventListener:(event,handler)=>windowHandlers[event]=handler},document:{getElementById:id=>nodes[id],querySelectorAll:()=>[button],createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag)}});
const elements=()=>nodes['stock-chart'].children[0].children;
assert(elements().some(node=>node.attrs['data-kind']==='reference'));assert.equal(nodes['chart-source'].value,'daily');
assert.equal(elements().find(node=>node.attrs['data-kind']==='stock-event').attrs['data-quantity'],'30');
nodes['chart-trade'].value='stock:0';nodes['chart-trade'].handlers.change();
assert.match(nodes['chart-tip'].textContent,/送转新增 30 股.*上市日 —/);
assert(!elements().some(node=>node.attrs['data-kind']==='fill'),'A stock distribution is not a buy fill');
nodes['chart-source'].value='004';nodes['chart-source'].handlers.change();
assert.equal(nodes['chart-trade'].children.length,3);assert.equal(nodes['chart-trade'].disabled,false);
assert(!elements().some(node=>node.attrs['data-kind']==='reference'));
assert(!elements().some(node=>node.attrs['data-kind']==='fill'),'Old trade should be outside latest 60 bars');
nodes['chart-trade'].value='0';nodes['chart-trade'].handlers.change();
assert.match(nodes['chart-tip'].textContent,/模拟买 500 股 @ 8.18/);
assert(elements().some(node=>node.attrs['data-kind']==='signal'&&node.attrs['data-day']===plan.signal_date));
assert(elements().some(node=>node.attrs['data-kind']==='fill'&&node.attrs['data-day']===fill.trade_date));
nodes['stock-chart'].clientWidth=320;
windowHandlers.resize();
const markerX=Number(elements().find(node=>node.attrs['data-kind']==='fill').attrs.d.split(' ')[1]);
assert(markerX>=nodes['stock-chart'].scrollLeft&&markerX<=nodes['stock-chart'].scrollLeft+nodes['stock-chart'].clientWidth,'Resizing must keep the selected old fill in the viewport');
assert.notEqual(nodes['stock-chart'].scrollLeft,nodes['stock-chart'].scrollWidth-nodes['stock-chart'].clientWidth,'Resizing must not always scroll to the latest candles');
assert.match(nodes['chart-tip'].textContent,/模拟买 500 股 @ 8.18/);
nodes['chart-trade'].value='stock:0';nodes['chart-trade'].handlers.change();
const stockMarker=elements().find(node=>node.attrs['data-kind']==='stock-event');
assert.equal(stockMarker.attrs['data-day'],stockEvent.ex_date);assert.equal(stockMarker.attrs['data-quantity'],'150');
assert.match(stockMarker.children[0].textContent,/登记日 2025-01-21 持股 500 股，每登记股新增 0.3 股；上市日 2025-01-23/);
assert.match(nodes['chart-tip'].textContent,/收 8.20.*送转新增 150 股/,'Stock distributions must not adjust raw candle prices');
const flow=nodes['chart-explanation'].children.find(node=>node.tag==='details');
assert.equal(flow.attrs.open,undefined);assert.equal(flow.open,undefined);
assert.equal(flow.children[2].children[0].children[0].href,stockEvent.source);
nodes['stock-chart'].onkeydown({key:'ArrowLeft',preventDefault(){}});
assert.match(nodes['chart-tip'].textContent,/模拟买 500 股/);assert(!nodes['chart-tip'].textContent.includes('送转新增'));
nodes['stock-chart'].onkeydown({key:'ArrowRight',preventDefault(){}});
assert.match(nodes['chart-tip'].textContent,/送转新增 150 股/);
windowHandlers.resize();
const stockX=Number(elements().find(node=>node.attrs['data-kind']==='stock-event').attrs.x)+5;
assert(stockX>=nodes['stock-chart'].scrollLeft&&stockX<=nodes['stock-chart'].scrollLeft+nodes['stock-chart'].clientWidth);
nodes['chart-source'].value='005';nodes['chart-source'].handlers.change();
assert.equal(nodes['chart-trade'].disabled,true);assert(!elements().some(node=>node.attrs['data-kind']==='fill'));
assert(!elements().some(node=>node.attrs['data-kind']==='stock-event'));assert(!nodes['chart-explanation'].children.some(node=>node.tag==='details'));
button.handlers.click();assert.equal(nodes['chart-source'].value,'daily');assert(elements().some(node=>node.attrs['data-kind']==='reference'));
assert.equal(elements().find(node=>node.attrs['data-kind']==='stock-event').attrs['data-quantity'],'30');
'''
        subprocess.run(['node','-e',script,str(ROOT/'qmt/chart.js')],check=True,capture_output=True,text=True)


if __name__=='__main__':unittest.main()
