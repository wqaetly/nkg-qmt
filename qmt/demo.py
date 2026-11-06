from decimal import Decimal
from .config import AppError, atomic_json, now_cn
from .ledger import Ledger
from .report import render
from vendor.dividend_stock_research.dividend_price import calculate


def create_demo(workspace,cfg):
    if (workspace/'ledger.sqlite3').exists():
        raise AppError('演示目录已存在，未覆盖账本。可直接打开其中 dashboard.html。')
    workspace.mkdir(parents=True,exist_ok=True)
    (workspace/'.demo-mode').write_text('Synthetic data; never use for real trades.',encoding='utf-8')
    matrix=[{'scenario':name,'required_yield':rate,'meets_yield':True,**calculate(eps,'0.6','1',rate,'10.40')}
        for name,eps in [('悲观','1.2'),('基准','1.5'),('较好','1.8')] for rate in cfg['required_yields']]
    row={'symbol':'600001.SH','name':'示例消费公司（虚构）','industry':'示例消费','price':10.40,
         'as_of':'20261106','historic_yield_pct':5.3,'status':'可进一步评估','gate':True,'evidence_hash':'DEMO',
         'latest_period':'20260930','issues':['全部数字均为虚构，只演示研究与账本界面'],
         'review_note':'演示情景不是对任何公司的估值或买入建议','scenarios':matrix,
         'annual':[{'period':f'{year}1231','profit':'1500000000','deducted_profit':'1400000000',
                    'operating_cash':'1800000000','capex':'500000000','simple_fcf':'1300000000',
                    'implemented_dps':'0.6','audit':'标准无保留意见'} for year in range(2025,2020,-1)]}
    report={'source':'DEMO','as_of':'20261106','requested_date':'20261106','next_session':'20261109',
            'pool_date':'20261030','generated_at':now_cn().isoformat(),'covered':2,'pool_size':2,
            'rows':[row,{**row,'symbol':'600002.SH','name':'示例周期公司（虚构）','status':'观察',
                'gate':False,'scenarios':[],'issues':['现金流与利润背离，需核查资本支出和债务到期压力']}],
            'prices':{'600001.SH':{'trade_date':'20261106','close':10.40}},'industries':{'600001.SH':'示例消费'}}
    ledger=Ledger(workspace/'ledger.sqlite3',cfg)
    try:
        ledger.initialize('paper',cfg['paper_cash'],cfg['paper_max_position'],cfg['paper_max_sector'],'20261101')
        ledger.create_plan('paper',row,'20261102','20261103',100,'10.20','buy','虚构情景演示',{},report['industries'])
        ledger.settle_paper('20261103',{'600001.SH':{'20261103':{'open':Decimal('10'),'low':Decimal('9.9'),
            'high':Decimal('10.5'),'close':Decimal('10.3'),'up':Decimal('11'),'down':Decimal('9'),'vol':Decimal('10000')}}})
        events={'600001.SH':[{'record_date':'20261103','ex_date':'20261104','pay_date':'20261105',
                             'cash_div_tax':'0.1','stk_div':'0'}]}
        for day,price in [('20261103','10.30'),('20261104','10.20'),('20261105','10.25'),('20261106','10.40')]:
            ledger.dividends(events,day)
            ledger.mark('paper',day,{'600001.SH':{'trade_date':day,'close':price}})
        summary=ledger.summary('paper')
        report['demo_operations']={'chart_plans':summary['plans'],'chart_fills':summary['fills'],'notes':{},'assessments':[],
                                   'focus_symbols':['600001.SH','600002.SH']}
        candles=[{'day':day,'open':open_price,'high':max(open_price,close)+.1,'low':min(open_price,close)-.1,'close':close,'vol':10000}
            for day,open_price,close in [('20261102',10.1,10.2),('20261103',10,10.3),('20261104',10.2,10.2),('20261105',10.2,10.25),('20261106',10.25,10.4)]]
        atomic_json(workspace/'charts.json',{'as_of':'20261106','stocks':{'600001.SH':{'candles':candles,'error':None}}})
        atomic_json(workspace/'screen.json',report)
        return render(workspace,ledger,report,{'ok':True,'message':'演示已跑通：信号、次日模拟成交、分红应收与到账；与真实数据完全隔离'})
    finally:
        ledger.close()
