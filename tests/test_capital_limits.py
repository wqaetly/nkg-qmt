import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.automatic import allocate
from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES


DAY, NEXT = '20261105', '20261106'
HELD, BUY, THIRD = '600001.SH', '600002.SH', '600003.SH'


class CapitalLimitsTests(unittest.TestCase):
    def ledger(self, policy=None, mode='paper'):
        cfg=load_config()
        if policy is not None:cfg['paper_capital_policy']=policy
        ledger=Ledger(':memory:',cfg)
        self.addCleanup(ledger.close)
        ledger.initialize(mode,'100000','5000','20000','20261102')
        return ledger

    def plan(self, ledger, symbol, price, *, quantity=100, side='buy', mode='paper', industry='测试行业', prices=None, industries=None, valuation_only=None, day=DAY, next_day=NEXT):
        row={'symbol':symbol,'industry':industry,'gate':True,'scenarios':[1],'evidence_hash':'fixture'}
        return ledger.create_plan(mode,row,day,next_day,quantity,price,side,'test',prices or {},industries or {symbol:industry},valuation_only=valuation_only)

    def receivable(self, ledger):
        ledger.opening('paper',HELD,100,'20261102')
        event={'record_date':'20261103','ex_date':'20261104','pay_date':'20261109','cash_div_tax':'200','stk_div':'0'}
        ledger.dividends({HELD:[event]},DAY)
        return {HELD:{'trade_date':DAY,'close':'10'}}

    def test_policy_validated_before_db_and_config_change_cannot_reuse_book(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'book.sqlite'
            for value in ('unknown',None,True,[]):
                with self.subTest(value=value),self.assertRaises(AppError):
                    Ledger(path,{**load_config(),'paper_capital_policy':value})
                self.assertFalse(path.exists())
            cfg=load_config();ledger=Ledger(path,cfg);ledger.initialize('paper','100000','5000','20000',DAY);ledger.close()
            original=path.read_bytes()
            with self.assertRaisesRegex(AppError,'配置与旧账本不一致'):
                Ledger(path,{**cfg,'paper_capital_policy':'equity_ratio'})
            self.assertEqual(path.read_bytes(),original)

    def test_limits_follow_paper_nav_only_and_use_frozen_account_amounts(self):
        ratio=self.ledger('equity_ratio');fixed=self.ledger();explicit=self.ledger('fixed_amount')
        for nav,position,sector in [('50000','2500','10000'),('100000','5000','20000'),('200000','10000','40000'),('0','0','0')]:
            with self.subTest(nav=nav):
                self.assertEqual(ratio.position_limits('paper',nav),{'max_position':Decimal(position),'max_sector':Decimal(sector)})
                self.assertEqual(fixed.position_limits('paper',nav),explicit.position_limits('paper',nav))
                self.assertEqual(fixed.position_limits('paper',nav),{'max_position':Decimal('5000'),'max_sector':Decimal('20000')})
        ratio.initialize('manual','10000','2000','4000',DAY)
        self.assertEqual(ratio.position_limits('manual','200000'),{'max_position':Decimal('2000'),'max_sector':Decimal('4000')})
        # Account initialization, not unrelated config defaults, defines the ratios.
        other=Ledger(':memory:',{**load_config(),'paper_capital_policy':'equity_ratio'})
        self.addCleanup(other.close);other.initialize('paper','20000','2000','4000',DAY)
        self.assertEqual(other.position_limits('paper','40000'),{'max_position':Decimal('4000'),'max_sector':Decimal('8000')})

    def test_initial_nav_preserves_complete_default_plan_and_mark(self):
        books=[self.ledger(),self.ledger('fixed_amount'),self.ledger('equity_ratio')]
        for ledger in books:
            ledger.mark('paper',DAY,{})
            self.plan(ledger,BUY,'10',quantity=500)
        self.assertEqual(books[0].summary('paper'),books[1].summary('paper'))
        self.assertEqual(books[0].summary('paper'),books[2].summary('paper'))

    def test_allocation_keeps_20_slots_and_four_point_five_percent_nav_per_slot(self):
        codes=[f'60000{i}.SH' for i in range(1,7)]
        report={'as_of':DAY,'rows':[{'symbol':s,'price':'10'} for s in codes]}
        assessments=[{'symbol':s,'industry':'同一行业','state':'满足价格条件','estimated_yield':'.06','reference_price':'20'} for s in codes]
        rules={'cash_reserve':'0.10','max_holdings':20,'order_price_buffer':'0'}
        results={}
        for policy in (None,'fixed_amount','equity_ratio'):
            ledger=self.ledger(policy)
            for nav in ('100000.00','200000.00','50000.00'):
                ledger.db.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?)',('paper',DAY,nav,'0',nav))
                ledger.db.commit()
                results[policy,nav]=allocate(ledger,report,assessments,rules)
        for nav in ('100000.00','200000.00','50000.00'):
            self.assertEqual(results[None,nav],results['fixed_amount',nav])
        initial={'month':'202611','created_on':DAY,'targets':{s:400 for s in codes[:5]},
                 'allocation_notes':{codes[5]:'单个目标预算 0.00 元不足一手（限价估算 1000.00 元）'},'per_stock_budget':'4500.0000'}
        self.assertEqual(results[None,'100000.00'],initial)
        self.assertEqual(results['equity_ratio','100000.00'],initial)
        up=results['equity_ratio','200000.00'];down=results['equity_ratio','50000.00']
        self.assertEqual(up['per_stock_budget'],'9000.0000')
        self.assertEqual(up['targets'],{**{s:900 for s in codes[:4]},codes[4]:400})
        self.assertEqual(down['per_stock_budget'],'2250.0000')
        self.assertEqual(down['targets'],{s:200 for s in codes[:5]})

    def test_create_plan_scales_with_real_holdings_and_manual_stays_fixed(self):
        for policy,price,allowed in [('equity_ratio','100',True),(None,'100',False),('equity_ratio','1',False),(None,'1',True)]:
            with self.subTest(policy=policy,held_price=price):
                ledger=self.ledger(policy);ledger.opening('paper',HELD,500,'20261102')
                # Opening assets cost 5,000, leaving 95,000 cash before appreciation/depreciation.
                ledger.db.execute("INSERT INTO cash_events VALUES('paper','opening-cost','20261102',?,'-5000','fixture')",(HELD,));ledger.db.commit()
                prices={HELD:{'trade_date':DAY,'close':price}};industries={HELD:'旧行业',BUY:'测试行业'}
                limit='70' if price=='100' else '48'
                if allowed:self.plan(ledger,BUY,limit,prices=prices,industries=industries)
                else:
                    with self.assertRaisesRegex(AppError,'单股金额'):self.plan(ledger,BUY,limit,prices=prices,industries=industries)
        manual=self.ledger('equity_ratio','manual');manual.opening('manual',HELD,500,DAY)
        with self.assertRaisesRegex(AppError,'单股金额'):
            self.plan(manual,BUY,'70',mode='manual',prices={HELD:{'trade_date':DAY,'close':'100'}},industries={HELD:'旧行业',BUY:'测试行业'})

    def test_pending_buys_are_capacity_reservations_not_nav_assets(self):
        ledger=self.ledger('equity_ratio')
        industries={f'60000{i}.SH':'测试行业' for i in range(1,6)}
        self.plan(ledger,HELD,'50',industries=industries)
        with self.assertRaisesRegex(AppError,'单股金额'):self.plan(ledger,BUY,'51',industries=industries)
        with self.assertRaisesRegex(AppError,'未核对计划'):self.plan(ledger,HELD,'1',industries=industries)
        for symbol in (BUY,THIRD,'600004.SH'):self.plan(ledger,symbol,'50',industries=industries)
        with self.assertRaisesRegex(AppError,'行业金额'):self.plan(ledger,'600005.SH','1',industries=industries)
        self.assertEqual(len(ledger.open_plans('paper')),4)
        self.assertEqual(ledger.cash('paper'),Decimal('100000'))

    def test_receivables_in_nav_never_spendable_nor_pending_sale_cash(self):
        ledger=self.ledger('equity_ratio');prices=self.receivable(ledger);industries={HELD:'旧行业',BUY:'测试行业'}
        ledger.mark('paper',DAY,prices)
        self.assertEqual(ledger.summary('paper')['marks'][-1]['equity'],'121000.00')
        self.plan(ledger,BUY,'60.50',prices=prices,industries=industries)
        poor=self.ledger('equity_ratio');prices=self.receivable(poor)
        poor.db.execute("INSERT INTO cash_events VALUES('paper','cash-used','20261102',?,'-99500','fixture')",(HELD,));poor.db.commit()
        self.plan(poor,HELD,'10',side='sell',prices=prices,industries=industries)
        with self.assertRaisesRegex(AppError,'现金扣除'):
            self.plan(poor,BUY,'10',prices=prices,industries=industries)
        self.assertEqual(poor.cash('paper'),Decimal('500'))
        self.assertEqual(len(poor.open_plans('paper')),1)

    def test_verified_suspension_counts_toward_nav_and_sector_not_trading_quotes(self):
        ledger=self.ledger('equity_ratio');ledger.opening('paper',HELD,2000,'20261102')
        ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules','historical-replay')")
        ledger.db.execute("INSERT INTO metadata VALUES('historical-sessions',?)",(json.dumps(['20261104',DAY,NEXT]),));ledger.db.commit()
        proof={'symbol':HELD,'day':DAY,'full_day':True,'available_on':DAY,
               'source':'https://example.com/fixture.pdf','source_sha256':'a'*64,'pages':'1'}
        carry={HELD:{'day':DAY,'symbol':HELD,'quantity':2000,'price':'10','price_date':'20261104',
                     'market_value':'20000','method':'verified_suspension_prior_close','tradable':False,'stale_sessions':1,'proofs':[proof]}}
        prices={BUY:{'trade_date':DAY,'close':'40'},THIRD:{'trade_date':DAY,'close':'1'}}
        industries={s:'测试行业' for s in (HELD,BUY,THIRD)}
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof]}):
            ledger.mark('paper',DAY,prices,valuation_only=carry)
            self.assertEqual(ledger.summary('paper')['marks'][-1]['equity'],'120000.00')
            self.plan(ledger,BUY,'40',prices=prices,industries=industries,valuation_only=carry)
            with self.assertRaisesRegex(AppError,'行业金额'):
                self.plan(ledger,THIRD,'1',prices=prices,industries=industries,valuation_only=carry)
            with self.assertRaisesRegex(AppError,'不可用于'):
                self.plan(ledger,HELD,'10',side='sell',prices=prices,industries=industries,valuation_only=carry)
        self.assertEqual(ledger.positions('paper'),{HELD:2000})

    def test_future_paid_dividend_cannot_inflate_past_nav(self):
        ledger=Ledger(':memory:',{**load_config(),'paper_capital_policy':'equity_ratio'})
        self.addCleanup(ledger.close);ledger.initialize('paper','10000','2000','4000','20261101')
        ledger.opening('paper',HELD,100,'20261101')
        event={'record_date':'20261103','ex_date':'20261104','pay_date':NEXT,'cash_div_tax':'.5','stk_div':'0'}
        ledger.dividends({HELD:[event]},NEXT)
        self.assertEqual(ledger.cash('paper','20261103'),Decimal('10000'))
        self.assertEqual(ledger.cash('paper'),Decimal('10050'))
        with self.assertRaisesRegex(AppError,'晚于信号日'):
            self.plan(ledger,BUY,'22.10',day='20261103',next_day='20261104',
                      prices={HELD:{'trade_date':'20261103','close':'10'}},industries={HELD:'旧行业',BUY:'测试行业'})
        self.assertEqual(ledger.open_plans('paper'),[])

    def test_future_opening_fill_stock_event_or_plan_rejects_past_plans(self):
        for kind in ('opening','fill','stock','plan'):
            with self.subTest(kind=kind):
                ledger=self.ledger('equity_ratio')
                if kind=='opening':ledger.opening('paper',HELD,100,NEXT)
                elif kind=='fill':
                    self.plan(ledger,HELD,'11')
                    bar={key:Decimal(value) for key,value in {'open':'10','close':'10','high':'11','low':'9','up':'11','down':'9','vol':'100000'}.items()}
                    ledger.settle_paper(NEXT,{HELD:{NEXT:bar}})
                    self.assertEqual(len(ledger.summary('paper')['fills']),1)
                elif kind=='stock':
                    ledger.opening('paper',HELD,100,'20261102')
                    event={'event_kind':'stock_only','record_date':DAY,'ex_date':NEXT,'pay_date':None,'cash_div_tax':'0','stk_div':'.1'}
                    terms={'stock_list_date':'20261109','available_on':DAY,'source':'https://example.com/fixture.pdf'}
                    with patch('qmt.ledger.stock_distribution_terms',return_value=terms):ledger.dividends({HELD:[event]},NEXT)
                    self.assertEqual(ledger.positions('paper'),{HELD:110})
                    self.assertEqual(ledger.summary('paper')['cash_events'],[])
                else:self.plan(ledger,HELD,'10',day=NEXT,next_day='20261109')
                before=ledger.summary('paper')
                with self.assertRaisesRegex(AppError,'晚于信号日'):
                    self.plan(ledger,BUY,'5',prices={HELD:{'trade_date':DAY,'close':'10'}},industries={HELD:'旧行业',BUY:'测试行业'})
                self.assertEqual(ledger.summary('paper'),before)

    def test_new_policy_rejects_before_start_without_changing_fixed_or_manual_history(self):
        ledger=self.ledger('equity_ratio')
        with self.assertRaisesRegex(AppError,'账户起点前'):
            self.plan(ledger,BUY,'5',day='20261101',next_day='20261102')
        for policy,mode in ((None,'paper'),('fixed_amount','paper'),('equity_ratio','manual')):
            with self.subTest(policy=policy,mode=mode):
                old=self.ledger(policy,mode)
                old.db.execute('INSERT INTO cash_events VALUES(?,?,?,?,?,?)',(mode,'future',NEXT,HELD,'50','fixture'));old.db.commit()
                self.plan(old,BUY,'5',mode=mode,day='20261101',next_day='20261102')
                self.assertEqual(len(old.open_plans(mode)),1)

    def test_future_payment_and_stock_listing_remain_valid_current_assets(self):
        ledger=self.ledger('equity_ratio');ledger.opening('paper',HELD,100,'20261102')
        event={'record_date':'20261103','ex_date':'20261104','pay_date':'20261109','cash_div_tax':'200','stk_div':'.1'}
        terms={'stock_list_date':'20261109','available_on':'20261102','source':'https://example.com/fixture.pdf'}
        with patch('qmt.ledger.stock_distribution_terms',return_value=terms):ledger.dividends({HELD:[event]},DAY)
        prices={HELD:{'trade_date':DAY,'close':'10'}}
        ledger.mark('paper',DAY,prices)
        mark=ledger.summary('paper')['marks'][-1]
        self.assertEqual((mark['cash'],mark['receivable'],mark['equity']),('100000.00','20000.00','121100.00'))
        self.assertEqual(ledger.positions('paper'),{HELD:110})
        self.assertEqual(ledger.available_to_sell('paper',HELD,DAY),100)
        self.plan(ledger,BUY,'60.55',prices=prices,industries={HELD:'旧行业',BUY:'测试行业'})
        self.assertEqual(len(ledger.open_plans('paper')),1)

    def test_ratio_allocation_requires_exact_signal_day_mark_only(self):
        report={'as_of':DAY,'rows':[]};rules={'cash_reserve':'0.10','max_holdings':20,'order_price_buffer':'0'}
        for mark_day in (None,'20261104',NEXT):
            with self.subTest(mark_day=mark_day):
                ledger=self.ledger('equity_ratio')
                if mark_day:ledger.mark('paper',mark_day,{})
                before=ledger.summary('paper')
                with self.assertRaisesRegex(AppError,'信号当日'):allocate(ledger,report,[],rules)
                self.assertEqual(ledger.summary('paper'),before)
                if mark_day:
                    fixed=self.ledger();fixed.mark('paper',mark_day,{})
                    self.assertEqual(allocate(fixed,report,[],rules)['per_stock_budget'],'4500.0000')


if __name__=='__main__':unittest.main()
