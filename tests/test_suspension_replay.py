import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.backtest import simulate, suspension_values
from qmt.config import AppError
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, full_day_suspension
from test_backtest import CODE, DAYS, bar, dividend, inputs
from test_freeze_policy import OTHER, THIRD, add_stock, candidate, fixture
from test_stock_replay import registered


def proof(day, code=CODE, **changes):
    return {'symbol':code,'day':day,'full_day':True,'available_on':day,
            'source':'https://example.com/fictional-suspension.pdf','source_sha256':'a'*64,'pages':'1',**changes}


def valuation(day=DAYS[2], price_date=DAYS[1], quantity=200):
    chain=DAYS[DAYS.index(price_date)+1:DAYS.index(day)+1]
    return {'day':day,'symbol':CODE,'quantity':quantity,'price':'8','price_date':price_date,
            'market_value':str(8*quantity),'method':'verified_suspension_prior_close','tradable':False,
            'stale_sessions':len(chain),'proofs':[proof(d) for d in chain]}


class SuspensionReplayTests(unittest.TestCase):
    def ledger(self, mode='paper', marker='historical-replay', calendar=True):
        _,_,cfg=fixture()
        ledger=Ledger(':memory:',cfg)
        self.addCleanup(ledger.close)
        ledger.initialize(mode,'10000','2000','4000',DAYS[0])
        ledger.opening(mode,CODE,200,DAYS[1])
        if marker is not None:
            ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules',?)",(marker,))
        if calendar:
            ledger.db.execute("INSERT INTO metadata VALUES('historical-sessions',?)",(json.dumps(DAYS),))
        ledger.db.commit()
        return ledger

    def test_consecutive_proven_days_keep_original_price_date_and_real_resumption_quote(self):
        data,signals,cfg=inputs()
        for day in DAYS[2:4]:del data['bars'][CODE][day]
        data['bars'][CODE][DAYS[4]]=bar('9')
        original=copy.deepcopy((data,signals,cfg))
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(d) for d in DAYS[2:4]]}):
            result=simulate(data,signals,cfg)
        events=result['valuation_events']
        self.assertEqual([e['day'] for e in events],DAYS[2:4])
        self.assertEqual([e['price_date'] for e in events],[DAYS[1]]*2)
        self.assertEqual([e['stale_sessions'] for e in events],[1,2])
        self.assertEqual(events[1]['proofs'],[proof(d) for d in DAYS[2:4]])
        for event in events:
            self.assertEqual(event['quantity'],200)
            self.assertEqual(Decimal(event['market_value']),Decimal('1600'))
            self.assertFalse(event['tradable'])
            self.assertEqual(event['method'],'verified_suspension_prior_close')
            self.assertEqual(Decimal(event['weight']),Decimal(1600)/Decimal('9995'))
        self.assertEqual(result['equity_curve'][-1]['equity'],'10195.00')
        self.assertEqual(len(result['trades']),1)
        self.assertEqual((data,signals,cfg),original)

    def test_unknown_or_future_missing_day_and_corrupt_proof_never_become_a_carry(self):
        for records in ([],[proof(DAYS[2],available_on=DAYS[3])],
                        [proof(DAYS[2],source_sha256='bad')],[proof(DAYS[2],full_day=False)]):
            data,signals,cfg=inputs();del data['bars'][CODE][DAYS[2]]
            with self.subTest(records=records),patch.dict(DISCLOSURES,{'full_day_suspensions':records}),self.assertRaises(AppError):
                simulate(data,signals,cfg)
        data,_,_=inputs()
        for day in DAYS[2:4]:del data['bars'][CODE][day]
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[3])]}),self.assertRaisesRegex(AppError,'未经核实'):
            suspension_values({CODE:200},DAYS[3],DAYS,data['bars'],{}, {CODE:DAYS[1]})

    def test_no_prior_actual_close_or_conflicting_real_bar_is_rejected(self):
        data,signals,cfg=inputs()
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}):
            with self.assertRaisesRegex(AppError,'日线冲突'):simulate(data,signals,cfg)
            del data['bars'][CODE][DAYS[2]]
            with self.assertRaisesRegex(AppError,'更早的真实'):
                suspension_values({CODE:200},DAYS[2],DAYS,data['bars'],{}, {})

    def test_dividend_or_stock_event_anywhere_after_anchor_blocks_carry(self):
        for ratio in ('0','.3'):
            data,signals,cfg=inputs();event={**dividend(),'stk_div':ratio}
            data['dividends']={CODE:[event]};del data['bars'][CODE][DAYS[2]]
            registry={'full_day_suspensions':[proof(DAYS[2])],'stock_distributions':[registered(event)]}
            with self.subTest(ratio=ratio),patch.dict(DISCLOSURES,registry),self.assertRaisesRegex(AppError,'跨越除权除息'):
                simulate(data,signals,cfg)
        data,_,_=inputs();data['bars'][CODE].pop(DAYS[2]);data['bars'][CODE].pop(DAYS[3])
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(d) for d in DAYS[2:4]]}),self.assertRaisesRegex(AppError,'跨越除权除息'):
            suspension_values({CODE:200},DAYS[3],DAYS,data['bars'],{CODE:[dividend()]},{CODE:DAYS[1]})

    def test_cash_payment_during_suspension_is_not_paid_twice_or_added_to_price(self):
        data,signals,cfg=inputs();data['dividends']={CODE:[dividend()]}
        for day in DAYS[2:]:data['bars'][CODE][day]=bar('7.5')
        del data['bars'][CODE][DAYS[4]]
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[4])]}):result=simulate(data,signals,cfg)
        self.assertEqual(result['equity_curve'][-1]['equity'],'9995.00')
        self.assertEqual(result['equity_curve'][-1]['receivable'],'0')
        self.assertEqual([Decimal(e['amount']) for e in result['cash_events']],[Decimal(100)])
        self.assertEqual(result['valuation_events'][0]['price'],'7.5')

    def test_buy_and_sell_execution_suspensions_expire_without_fill_fee_or_later_chase(self):
        for side in ('buy','sell'):
            data,signals,cfg=inputs();day=DAYS[1] if side=='buy' else DAYS[3]
            if side=='sell':signals[DAYS[2]]=[]
            del data['bars'][CODE][day]
            with self.subTest(side=side),patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(day)]}):
                result=simulate(data,signals,cfg)
                self.assertEqual(result['plans'][-1]['side'],side)
                self.assertEqual(result['plans'][-1]['status'],'expired')
                self.assertIn('不延至复牌',result['plans'][-1]['note'])
                self.assertEqual(len(result['trades']),0 if side=='buy' else 1)
                self.assertEqual(Decimal(result['metrics']['costs']),Decimal(0 if side=='buy' else 5))
                self.assertEqual(result['equity_curve'][-1]['positions'],{} if side=='buy' else {CODE:200})
                self.assertEqual(len(result['valuation_events']),0 if side=='buy' else 1)

    def test_monthly_suspension_preserves_actual_shares_and_allows_healthy_buy(self):
        data,signals,cfg=fixture();signals[DAYS[2]]['freeze']=[]
        del data['bars'][CODE][DAYS[2]]
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}):
            for policy in ('portfolio','position'):
                with self.subTest(policy=policy):
                    result=simulate(data,signals,cfg,freeze_policy=policy)
                    self.assertEqual(result['equity_curve'][-1]['positions'],{CODE:200,OTHER:200})
                    self.assertFalse(any(p['symbol']==CODE and p['signal_date']==DAYS[2] for p in result['plans']))

    def test_twenty_suspended_holding_slots_cannot_be_reused(self):
        data,signals,cfg=fixture();codes=[f'600{i:03d}.SH' for i in range(1,22)]
        for code in codes:add_stock(data,code,industry=code)
        cfg.update(paper_cash='100000',paper_max_position='5000',paper_max_sector='20000')
        signals[DAYS[0]]['candidates']=[candidate(c,DAYS[0]) for c in codes[:20]]
        signals[DAYS[2]].update(candidates=[candidate(codes[20],DAYS[2])],freeze=[])
        for code in codes[:20]:del data['bars'][code][DAYS[2]]
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2],c) for c in codes[:20]]}):
            result=simulate(data,signals,cfg,max_holdings=20,freeze_policy='position')
        self.assertEqual(len(result['valuation_events']),20)
        self.assertEqual(result['equity_curve'][-1]['positions'],result['equity_curve'][1]['positions'])
        self.assertFalse(any(p['symbol']==codes[20] for p in result['plans']))

    def test_suspended_candidates_and_retained_holding_keep_shares_without_using_buy_slots(self):
        for kind in ('held_candidate','held_retain','unheld_candidate'):
            data,signals,cfg=fixture();signals[DAYS[2]]['freeze']=[]
            code=OTHER if kind=='unheld_candidate' else CODE
            healthy=THIRD if kind=='unheld_candidate' else OTHER
            if kind=='held_candidate':
                signals[DAYS[2]]['candidates'].insert(0,candidate(CODE,DAYS[2]))
            else:
                signals[DAYS[2]]['retain']=[CODE]
            if kind=='unheld_candidate':
                add_stock(data,THIRD)
                signals[DAYS[2]]['candidates'].append(candidate(THIRD,DAYS[2]))
            del data['bars'][code][DAYS[2]]
            original=copy.deepcopy((data,signals,cfg))
            with self.subTest(kind=kind),patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2],code)]}):
                result=simulate(data,signals,cfg,max_holdings=2,freeze_policy='position')
                self.assertEqual(result['equity_curve'][-1]['positions'],{CODE:200,healthy:200})
                self.assertFalse(any(p['symbol']==code and p['signal_date']==DAYS[2] for p in result['plans']))
                self.assertEqual(len(result['valuation_events']),0 if kind=='unheld_candidate' else 1)
                if kind!='held_retain':
                    self.assertTrue(any(s['symbol']==code and '信号日全天停牌' in s['reason'] for s in result['skipped']))
            self.assertEqual((data,signals,cfg),original)
            with self.subTest(kind=kind,proof='unknown'),patch.dict(DISCLOSURES,{'full_day_suspensions':[]}),self.assertRaises(AppError):
                simulate(data,signals,cfg,max_holdings=2,freeze_policy='position')
            self.assertEqual((data,signals,cfg),original)
        data,signals,cfg=fixture();del data['bars'][CODE][DAYS[2]]
        signals[DAYS[2]]['candidates'].insert(0,candidate(CODE,DAYS[2]))
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}),self.assertRaisesRegex(AppError,'同时是买入候选'):
            simulate(data,signals,cfg,freeze_policy='position')

    def test_carried_market_value_occupies_sector_and_pending_buys_reserve_cash(self):
        data,signals,cfg=fixture();data['industries'][OTHER]=data['industries'][CODE]
        add_stock(data,THIRD);signals[DAYS[2]].update(candidates=[candidate(OTHER,DAYS[2]),candidate(THIRD,DAYS[2])],freeze=[])
        data['bars'][CODE][DAYS[1]]=bar('15');signals[DAYS[0]]['candidates'][0]['reference_price']='16'
        # At the signal the price must allow the 15 execution, while the target remains 100 shares.
        data['bars'][CODE][DAYS[0]]=bar('15');del data['bars'][CODE][DAYS[2]]
        cfg['paper_max_sector']='2000'
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}):
            result=simulate(data,signals,cfg,freeze_policy='position')
        self.assertEqual(result['equity_curve'][-1]['positions'],{CODE:100,THIRD:200})
        self.assertFalse(any(p['symbol']==OTHER for p in result['plans']))
        data,signals,cfg=fixture();cfg.update(paper_max_position='9000',paper_max_sector='10000')
        add_stock(data,THIRD);signals[DAYS[2]].update(candidates=[candidate(OTHER,DAYS[2]),candidate(THIRD,DAYS[2])],freeze=[])
        # Appreciation is known from the previous true session, before the suspension signal.
        data['bars'][CODE][DAYS[1]]['close']=Decimal('30');data['bars'][CODE][DAYS[1]]['high']=Decimal('30')
        del data['bars'][CODE][DAYS[2]]
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}):
            result=simulate(data,signals,cfg,max_holdings=3,freeze_policy='position')
        self.assertEqual(result['equity_curve'][-1]['positions'],{CODE:300,OTHER:600,THIRD:100})
        self.assertEqual(result['metrics']['costs'],'15.00')

    def test_ledger_resolver_rejects_forgery_missing_chain_and_raw_quote_conflict(self):
        ledger=self.ledger();valid=valuation(DAYS[3])
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(d) for d in DAYS[2:4]]}):
            ledger.mark('paper',DAYS[3],{},valuation_only={CODE:valid})
            changes=[{'price_date':DAYS[3]},{'quantity':100},{'market_value':'1500'},{'tradable':True},
                     {'method':'raw_close'},{'stale_sessions':1},{'proofs':[proof(DAYS[3])]},
                     {'proofs':[proof(DAYS[2],source_sha256='b'*64),proof(DAYS[3])]}]
            for change in changes:
                with self.subTest(change=change),self.assertRaises(AppError):
                    ledger.mark('paper',DAYS[3],{},valuation_only={CODE:{**valid,**change}})
            with self.assertRaises(AppError):
                ledger.mark('paper',DAYS[3],{CODE:{'trade_date':DAYS[3],'close':'8'}},valuation_only={CODE:valid})
            self.assertEqual(len(ledger.summary('paper')['marks']),1)

    def test_daily_manual_or_missing_calendar_cannot_accept_carry_or_suspension_expiry(self):
        for mode,marker,calendar in [('manual','historical-replay',True),('paper',None,True),
                                     ('paper','daily-automatic',True),('paper','historical-replay',False)]:
            ledger=self.ledger(mode,marker,calendar)
            with self.subTest(mode=mode,marker=marker,calendar=calendar),patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}):
                with self.assertRaises(AppError):ledger.mark(mode,DAYS[2],{},valuation_only={CODE:valuation()})
                with self.assertRaises(AppError):ledger.mark(mode,DAYS[2],{})
                if mode=='paper':
                    with self.assertRaises(AppError):ledger.settle_paper(DAYS[2],{},suspensions={CODE:proof(DAYS[2])})

    def test_create_plan_shares_resolver_but_carry_is_never_own_order_quote(self):
        ledger=self.ledger();quotes={OTHER:{'trade_date':DAYS[2],'close':'8'}}
        industries={CODE:'同行业',OTHER:'同行业'}
        row={'symbol':OTHER,'industry':'同行业','automatic_signal':True}
        with patch.dict(DISCLOSURES,{'full_day_suspensions':[proof(DAYS[2])]}):
            ledger.create_plan('paper',row,DAYS[2],DAYS[3],200,'8','buy','test',quotes,industries,valuation_only={CODE:valuation()})
            with self.assertRaisesRegex(AppError,'行业金额'):
                ledger.create_plan('paper',{**row,'symbol':THIRD},DAYS[2],DAYS[3],200,'8','buy','test',
                    {**quotes,THIRD:{'trade_date':DAYS[2],'close':'8'}},{**industries,THIRD:'同行业'},valuation_only={CODE:valuation()})
            with self.assertRaisesRegex(AppError,'不可用于'):
                ledger.create_plan('paper',{**row,'symbol':CODE},DAYS[2],DAYS[3],100,'8','sell','test',quotes,industries,valuation_only={CODE:valuation()})
            with self.assertRaisesRegex(AppError,'真实行情'):
                ledger.create_plan('paper',{**row,'symbol':THIRD},DAYS[2],DAYS[3],100,'8','buy','test',quotes,
                    {**industries,THIRD:'其他行业'},valuation_only={CODE:valuation()})


if __name__=='__main__':unittest.main()
