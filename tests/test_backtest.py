import unittest
from decimal import Decimal

from qmt.backtest import metrics, simulate
from qmt.config import AppError, load_config


CODE = '600001.SH'
DAYS = ['20200130','20200131','20200203','20200204','20200205']


def bar(price='8', volume='10000'):
    price=Decimal(price)
    return {'open':price,'high':price+Decimal('.2'),'low':price-Decimal('.2'),'close':price,
            'vol':volume,'up':price*Decimal('1.1'),'down':price*Decimal('.9')}


def inputs():
    data={'sessions':DAYS,'universe_as_of':DAYS[0],'bars':{CODE:{day:bar() for day in DAYS}},
          'industries':{CODE:'虚构行业'},'dividends':{}}
    signals={DAYS[0]:[{'symbol':CODE,'reference_price':'9','known_on':DAYS[0],'reason':'虚构测试信号'}]}
    cfg={**load_config(),'slippage_bps':0}
    return data,signals,cfg


def dividend():
    return {'ann_date':'20200101','imp_ann_date':'20200120','record_date':'20200131',
            'ex_date':'20200203','pay_date':'20200205','cash_div_tax':'.5','stk_div':0}


class BacktestTests(unittest.TestCase):
    def test_signal_executes_next_day_and_lot_uses_signal_price(self):
        data,signals,cfg=inputs()
        data['bars'][CODE]['20200131']=bar('4')
        result=simulate(data,signals,cfg)
        self.assertEqual(len(result['trades']),1)
        fill=result['trades'][0]
        self.assertEqual(fill['signal_date'],'20200130')
        self.assertEqual(fill['trade_date'],'20200131')
        self.assertEqual(fill['quantity'],200)
        self.assertEqual(Decimal(fill['price']),Decimal(4))
        self.assertEqual(result['equity_curve'][0]['positions'],{})
        self.assertEqual(result['metrics']['costs'],'5.00')

    def test_future_signal_and_future_universe_are_rejected(self):
        data,signals,cfg=inputs()
        signals[DAYS[0]][0]['known_on']='20200201'
        with self.assertRaises(AppError):simulate(data,signals,cfg)
        data,signals,cfg=inputs();data['universe_as_of']='20200131'
        with self.assertRaises(AppError):simulate(data,signals,cfg)

    def test_no_signal_freezes_but_empty_snapshot_exits(self):
        data,signals,cfg=inputs()
        result=simulate(data,signals,cfg)
        self.assertEqual(len(result['trades']),1)
        self.assertEqual(result['equity_curve'][-1]['positions'],{CODE:200})
        signals['20200203']=[]
        result=simulate(data,signals,cfg)
        self.assertEqual(len(result['trades']),2)
        self.assertEqual(result['trades'][-1]['trade_date'],'20200204')
        self.assertEqual(result['equity_curve'][-1]['positions'],{})
        self.assertEqual(result['metrics']['costs'],'11.60')

    def test_end_of_window_signal_cannot_fake_fill(self):
        data,signals,cfg=inputs();signals={DAYS[-1]:signals[DAYS[0]]}
        result=simulate(data,signals,cfg)
        self.assertEqual(result['plans'],[])
        self.assertEqual(result['metrics']['total_return'],0)
        self.assertEqual(result['metrics']['average_exposure'],0)

    def test_freeze_only_blocks_new_buys_when_stock_is_held(self):
        data,signals,cfg=inputs();other='600002.SH'
        data['bars'][other]={d:bar() for d in DAYS};data['industries'][other]='其他行业'
        original=signals[DAYS[0]]
        signals[DAYS[0]]={'candidates':original,'freeze':[other]}
        signals['20200203']={'candidates':[{'symbol':other,'reference_price':'9','known_on':'20200203'}],'freeze':[CODE]}
        result=simulate(data,signals,cfg)
        self.assertEqual(len(result['trades']),1)
        self.assertEqual(result['equity_curve'][-1]['positions'],{CODE:200})
        self.assertIn('暂停新增买入',result['skipped'][-1]['reason'])

    def test_missing_held_or_execution_bar_halts(self):
        for day in ('20200131','20200203'):
            data,signals,cfg=inputs();data['bars'][CODE].pop(day)
            with self.subTest(day=day),self.assertRaises(AppError):simulate(data,signals,cfg)

    def test_suspension_limits_and_price_gap_do_not_invent_fills(self):
        for changed in (bar(volume='0'),bar('12'),{**bar(),'up':Decimal('8')}, {**bar(),'up':None},bar(volume='100')):
            data,signals,cfg=inputs();data['bars'][CODE]['20200131']=changed
            result=simulate(data,signals,cfg)
            self.assertEqual(result['trades'],[])
            self.assertEqual(result['plans'][0]['status'],'expired')

    def test_dividend_is_receivable_then_cash_not_reinvested_early(self):
        data,signals,cfg=inputs();data['dividends']={CODE:[dividend()]}
        for day in DAYS[2:]:data['bars'][CODE][day]=bar('7.5')
        result=simulate(data,signals,cfg);curve={p['day']:p for p in result['equity_curve']}
        self.assertEqual(Decimal(curve['20200203']['receivable']),Decimal(100))
        self.assertEqual(Decimal(curve['20200203']['cash']),Decimal(8395))
        self.assertEqual(Decimal(curve['20200205']['receivable']),Decimal(0))
        self.assertEqual(Decimal(curve['20200205']['cash']),Decimal(8495))
        self.assertEqual(Decimal(curve['20200205']['equity']),Decimal(9995))
        self.assertEqual(len(result['cash_events']),1)

    def test_cash_dividend_on_non_session_is_paid_before_next_mark(self):
        data,signals,cfg=inputs()
        event={**dividend(),'ex_date':'20200201','pay_date':'20200202'}
        data['dividends']={CODE:[event]}
        result=simulate(data,signals,cfg)
        self.assertEqual(result['cash_events'][0]['day'],'20200202')
        self.assertEqual(Decimal(result['equity_curve'][2]['cash']),Decimal(8495))

    def test_split_and_unknown_duplicate_dividend_halt(self):
        data,signals,cfg=inputs();data['dividends']={CODE:[{**dividend(),'stk_div':'.1'}]}
        with self.assertRaises(AppError):simulate(data,signals,cfg)
        data['dividends']={CODE:[dividend(),dividend()]}
        with self.assertRaises(AppError):simulate(data,signals,cfg)

    def test_future_dividend_is_not_used_for_entry(self):
        data,signals,cfg=inputs()
        baseline=simulate(data,signals,cfg)
        data['dividends']={CODE:[{**dividend(),'ann_date':'20200210','imp_ann_date':'20200211',
                               'record_date':'20200212','ex_date':'20200213','pay_date':'20200214','cash_div_tax':'20'}]}
        with_future=simulate(data,signals,cfg)
        self.assertEqual(baseline['trades'],with_future['trades'])
        self.assertEqual(baseline['equity_curve'],with_future['equity_curve'])

    def test_sector_cap_and_cash_reserve_limit_targets(self):
        data,signals,cfg=inputs()
        for i in range(2,7):
            code=f'60000{i}.SH';data['bars'][code]={d:bar() for d in DAYS};data['industries'][code]='虚构行业'
            signals[DAYS[0]].append({'symbol':code,'reference_price':'9','known_on':DAYS[0]})
        result=simulate(data,signals,cfg)
        holdings=result['equity_curve'][-1]['positions']
        self.assertEqual(sum(holdings.values()),400)
        self.assertGreaterEqual(Decimal(result['equity_curve'][-1]['cash']),Decimal(1000))

    def test_cagr_drawdown_and_returns_use_entire_cash_account(self):
        curve=[{'day':'20200101','equity':'10000','exposure':0},
               {'day':'20200701','equity':'8000','exposure':.4},
               {'day':'20210101','equity':'11000','exposure':.5}]
        result=metrics(curve,[],'10000')
        self.assertAlmostEqual(result['total_return'],.1)
        self.assertAlmostEqual(result['max_drawdown'],-.2)
        self.assertAlmostEqual(result['cagr'],1.1**(365.2425/366)-1)
        self.assertEqual(result['annual_returns']['2020']['return'],-.2)
        self.assertEqual(result['annual_returns']['2021']['return'],.375)


if __name__=='__main__':unittest.main()
