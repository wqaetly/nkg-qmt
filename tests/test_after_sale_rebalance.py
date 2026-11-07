import copy
import hashlib
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from qmt.backtest import simulate
from qmt.config import AppError, load_config
from qmt.research import DISCLOSURES
from test_backtest import bar
from test_suspension_replay import proof


DAYS = ['20200129','20200130','20200131','20200203','20200204','20200205','20200206']
A, B, C, D = '600001.SH', '600002.SH', '600003.SH', '600004.SH'


def snapshot(day, codes, frozen=(), blocking=()):
    return {'candidates':[{'symbol':c,'reference_price':'9','known_on':day} for c in codes],
            'freeze':list(frozen),'portfolio_freeze':list(blocking)}


def fixture():
    data = {'sessions':DAYS,'universe_as_of':DAYS[0],
            'bars':{c:{d:bar() for d in DAYS} for c in (A,B,C)},
            'industries':{c:c for c in (A,B,C)},'dividends':{}}
    signals = {DAYS[0]:snapshot(DAYS[0],[A,B]), DAYS[2]:snapshot(DAYS[2],[C])}
    cfg = {**load_config(),'slippage_bps':0,'paper_cash':'10000',
           'paper_max_position':'2000','paper_max_sector':'4000'}
    return data,signals,cfg


def run(data, signals, cfg, **options):
    return simulate(data,signals,cfg,**{'max_holdings':2,'freeze_policy':'position',
                    'capital_policy':'equity_ratio','rebalance_policy':'after_confirmed_sale',**options})


def supplement(result):
    return [p for p in result['plans'] if '一次补买' in p['note']]


def twenty_fixture(replacements=1):
    old = [f'600{i:03d}.SH' for i in range(1,21)]
    new = [f'600{i:03d}.SH' for i in range(101,101+replacements)]
    data,_,cfg = fixture()
    data['bars'] = {c:{d:bar() for d in DAYS} for c in old+new}
    data['industries'] = {c:c for c in old+new}
    cfg.update(paper_cash='100000',paper_max_position='5000',paper_max_sector='20000')
    signals = {DAYS[0]:snapshot(DAYS[0],old),
               DAYS[2]:snapshot(DAYS[2],new,frozen=old[replacements:])}
    return data,signals,cfg,old,new


def stock_event(ex_day, record_day):
    return {'ann_date':'20200101','imp_ann_date':'20200120','record_date':record_day,
            'ex_date':ex_day,'pay_date':ex_day,'cash_div_tax':'0','stk_div':'.3'}


class AfterSaleRebalanceTests(unittest.TestCase):
    def test_default_complete_result_matches_prechange_capture_and_rejects_unknown_policy(self):
        data,signals,cfg = fixture()
        old = run(data,signals,cfg,rebalance_policy='monthly_only')
        self.assertEqual(old,simulate(data,signals,cfg,max_holdings=2,freeze_policy='position',capital_policy='equity_ratio'))
        self.assertEqual(hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest(),
                         'b6879a746b2f0776c6e0c6d1c4a57032524a2f0c6c5604b70013df6fcecbb07c')
        self.assertNotIn('rebalance_policy',old['assumptions'])
        self.assertNotIn('rebalance_timing',old['assumptions'])
        for value in ('',None,'retry',[],True):
            with self.subTest(value=value),self.assertRaisesRegex(AppError,'补单调仓'):
                run(data,signals,cfg,rebalance_policy=value)

    def test_confirmed_sale_close_creates_only_next_session_buy_and_preserves_inputs(self):
        data,signals,cfg = fixture(); original = copy.deepcopy((data,signals,cfg))
        result = run(data,signals,cfg); plans = supplement(result)
        self.assertEqual(len(plans),1)
        self.assertEqual((plans[0]['symbol'],plans[0]['signal_date'],plans[0]['execute_date'],plans[0]['quantity']),
                         (C,DAYS[3],DAYS[4],200))
        self.assertEqual(Decimal(plans[0]['limit_price']),Decimal('8.08'))
        self.assertIn(DAYS[2],plans[0]['note'])
        fills = [t for t in result['trades'] if t['symbol']==C]
        self.assertEqual([t['trade_date'] for t in fills],[DAYS[4]])
        self.assertNotIn(C,result['equity_curve'][3]['positions'])
        self.assertEqual(result['assumptions']['rebalance_policy'],'after_confirmed_sale')
        self.assertEqual((data,signals,cfg),original)

    def test_no_sale_fill_even_with_paid_dividend_does_not_trigger_or_retry_later(self):
        data,signals,cfg = fixture()
        for c in (A,B):data['bars'][c][DAYS[3]]={**bar(),'open':Decimal('7.8'),'low':Decimal('7.6')}
        data['dividends'][A]=[{**stock_event(DAYS[3],DAYS[2]),'cash_div_tax':'.5','stk_div':0}]
        result = run(data,signals,cfg)
        self.assertTrue(result['cash_events'])
        self.assertFalse(any(t['side']=='sell' for t in result['trades']))
        self.assertEqual(supplement(result),[])
        self.assertTrue(any('没有真实成交' in x['reason'] for x in result['skipped']))

    def test_partial_reduction_is_not_a_free_slot(self):
        data,signals,cfg = fixture()
        for day in DAYS[2:]:data['bars'][A][day]=bar('16')
        signals[DAYS[2]]=snapshot(DAYS[2],[A,C]);signals[DAYS[2]]['candidates'][0]['reference_price']='20'
        data['bars'][B][DAYS[3]]={**bar(),'open':Decimal('7.8'),'low':Decimal('7.6')}
        result = run(data,signals,cfg)
        self.assertTrue(any(t['symbol']==A and t['side']=='sell' for t in result['trades']))
        self.assertEqual(len(result['equity_curve'][3]['positions']),2)
        self.assertEqual(supplement(result),[])
        self.assertTrue(any('仍占满' in x['reason'] for x in result['skipped']))

    def test_original_created_but_expired_buy_never_joins_deferred_queue(self):
        data,signals,cfg = fixture();signals[DAYS[0]]=snapshot(DAYS[0],[A])
        data['bars'][C][DAYS[3]]=bar('8.2')
        result = run(data,signals,cfg)
        buys=[p for p in result['plans'] if p['symbol']==C]
        self.assertEqual(len(buys),1)
        self.assertEqual(buys[0]['status'],'expired')
        self.assertEqual(supplement(result),[])

    def test_original_attempt_order_is_preserved_with_only_one_released_slot(self):
        data,signals,cfg = fixture();data['bars'][D]={d:bar() for d in DAYS};data['industries'][D]=D
        signals[DAYS[2]]=snapshot(DAYS[2],[D,C])  # Rank order differs from original plan-creation code order.
        data['bars'][B][DAYS[3]]={**bar(),'open':Decimal('7.8'),'low':Decimal('7.6')}
        result=run(data,signals,cfg)
        self.assertEqual([p['symbol'] for p in supplement(result)],[C])
        self.assertEqual(result['equity_curve'][-1]['positions'],{B:200,C:200})

    def test_old_quantity_and_limit_cannot_expand_when_price_falls_or_recover_after_rise(self):
        for price in ('4','8.09'):
            data,signals,cfg=fixture()
            data['bars'][C][DAYS[3]]=bar(price)
            data['bars'][C][DAYS[4]]=bar('4' if price=='4' else '8')
            result=run(data,signals,cfg)
            with self.subTest(price=price):
                if price=='4':
                    p=supplement(result)[0]
                    self.assertEqual(p['quantity'],200)
                    self.assertEqual(Decimal(p['limit_price']),Decimal('4.04'))
                else:
                    self.assertEqual(supplement(result),[])
                    self.assertTrue(any('超过原买入限价' in x['reason'] for x in result['skipped']))

    def test_current_four_point_five_percent_target_shrinks_below_five_percent_guard(self):
        data,signals,cfg,old,new=twenty_fixture();code=new[0]
        data['bars'][code]={d:bar('14.8') for d in DAYS}
        signals[DAYS[2]]['candidates'][0]['reference_price']='16'
        for d in DAYS[3:]:data['bars'][old[1]][d]=bar('2')
        result=run(data,signals,cfg,max_holdings=20);p=supplement(result)[0]
        nav=Decimal(result['equity_curve'][3]['equity']);old_lot_value=Decimal('14.94')*300
        self.assertLess(nav*Decimal('.045'),old_lot_value)
        self.assertGreater(nav*Decimal('.05'),old_lot_value)
        self.assertEqual(p['quantity'],200)
        self.assertEqual(len(result['equity_curve'][-1]['positions']),20)

    def test_current_sector_values_and_earlier_supplement_pending_reserve_reduce_second_buy(self):
        data,signals,cfg,old,new=twenty_fixture(replacements=2)
        for c in old[2:4]+new:data['industries'][c]='同一行业'
        for d in DAYS[3:]:data['bars'][old[2]][d]=bar('24')
        result=run(data,signals,cfg,max_holdings=20);pp=supplement(result)
        self.assertEqual([p['symbol'] for p in pp],new)
        self.assertEqual([p['quantity'] for p in pp],[500,100])
        nav=Decimal(result['equity_curve'][3]['equity'])
        planned=sum(Decimal(p['limit_price'])*p['quantity'] for p in pp)
        self.assertLessEqual(Decimal(500*24+500*8)+planned,nav*Decimal('.20'))
        self.assertEqual(result['equity_curve'][3]['positions'][old[2]],500)

    def test_receivable_cannot_finance_supplement_and_later_payment_does_not_retry(self):
        data,signals,cfg,old,new=twenty_fixture()
        data['dividends'][old[1]]=[{**stock_event(DAYS[3],DAYS[2]),'pay_date':DAYS[6],
                                   'cash_div_tax':'300','stk_div':0}]
        result=run(data,signals,cfg,max_holdings=20)
        point=result['equity_curve'][3]
        self.assertEqual(Decimal(point['receivable']),Decimal('150000'))
        self.assertLess(Decimal(point['cash']),Decimal(point['equity'])*Decimal('.1'))
        self.assertEqual(supplement(result),[])
        self.assertTrue(result['cash_events'])
        self.assertFalse(any(t['symbol']==new[0] for t in result['trades']))

    def test_cash_reserve_includes_minimum_fee_before_allowing_one_lot(self):
        data,signals,cfg,old,new=twenty_fixture()
        for day in DAYS[3:]:data['bars'][old[1]][day]=bar('269.838')
        result=run(data,signals,cfg,max_holdings=20);point=result['equity_curve'][3]
        available=Decimal(point['cash'])-Decimal(point['equity'])*Decimal('.1')
        self.assertEqual(available,Decimal('810'))
        self.assertLess(Decimal('8.08')*100,available)
        self.assertGreater(Decimal('8.08')*100+Decimal('5'),available)
        self.assertEqual(supplement(result),[])

    def test_created_supplement_expires_once_without_a_later_price_recovery_retry(self):
        data,signals,cfg=fixture();data['bars'][C][DAYS[4]]=bar('8.2')
        result=run(data,signals,cfg);pp=supplement(result)
        self.assertEqual(len(pp),1)
        self.assertEqual((pp[0]['execute_date'],pp[0]['status']),(DAYS[4],'expired'))
        self.assertFalse(any(t['symbol']==C for t in result['trades']))

    def test_original_cash_rejection_does_not_join_queue_even_if_sale_later_frees_cash(self):
        data,signals,cfg,old,new=twenty_fixture()
        signals[DAYS[0]]=snapshot(DAYS[0],old[:19])
        signals[DAYS[2]]=snapshot(DAYS[2],new,frozen=old[1:19])
        for d in DAYS[2:]:data['bars'][old[1]][d]=bar('300')
        result=run(data,signals,cfg,max_holdings=20)
        self.assertTrue(any('不预支卖出款' in x['reason'] for x in result['skipped']))
        self.assertTrue(any(t['side']=='sell' and t['trade_date']==DAYS[3] for t in result['trades']))
        self.assertEqual(supplement(result),[])

    def test_new_snapshot_or_no_next_session_cancels_old_intent(self):
        for change in ('snapshot','last_day'):
            data,signals,cfg=fixture()
            if change=='snapshot':signals[DAYS[3]]=snapshot(DAYS[3],[])
            else:
                data['sessions']=DAYS[:4]
                data['bars']={c:{d:v for d,v in rows.items() if d in data['sessions']} for c,rows in data['bars'].items()}
            with self.subTest(change=change):
                result=run(data,signals,cfg)
                self.assertEqual(supplement(result),[])
                self.assertTrue(any('更新信号' in x['reason'] or '没有下一交易日' in x['reason'] for x in result['skipped']))

    def test_freeze_policy_and_unheld_unknown_preserve_existing_semantics(self):
        data,signals,cfg=fixture();signals[DAYS[2]]=snapshot(DAYS[2],[C],frozen=[B])
        held_global=run(data,signals,cfg,freeze_policy='portfolio')
        self.assertEqual(supplement(held_global),[])
        self.assertTrue(any('暂停新增买入' in x['reason'] for x in held_global['skipped']))
        position=run(data,signals,cfg)
        self.assertEqual([p['symbol'] for p in supplement(position)],[C])
        self.assertEqual(position['equity_curve'][-1]['positions'][B],200)
        signals[DAYS[2]]['portfolio_freeze']=[B]
        self.assertEqual(supplement(run(data,signals,cfg)),[])
        data['bars'][D]={d:bar() for d in DAYS};data['industries'][D]=D
        signals[DAYS[2]]=snapshot(DAYS[2],[C],frozen=[D],blocking=[D])
        self.assertEqual(len(supplement(run(data,signals,cfg,freeze_policy='portfolio'))),1)

    def test_unheld_target_stock_action_cancels_without_a_ledger_stock_event(self):
        for ex_day,record_day in ((DAYS[3],DAYS[2]),(DAYS[4],DAYS[3])):
            data,signals,cfg=fixture();data['dividends'][C]=[stock_event(ex_day,record_day)]
            with self.subTest(ex_day=ex_day):
                result=run(data,signals,cfg)
                self.assertEqual(result['stock_events'],[])
                self.assertFalse(any(t['symbol']==C for t in result['trades']))
                if ex_day==DAYS[3]:
                    self.assertEqual(supplement(result),[])
                    self.assertTrue(any('股数口径' in x['reason'] for x in result['skipped']))
                else:
                    self.assertEqual(supplement(result)[0]['status'],'expired')
                    self.assertIn('送转除权',supplement(result)[0]['note'])

    def test_future_stock_action_cannot_cancel_a_current_valid_supplement(self):
        data,signals,cfg=fixture();data['dividends'][C]=[{**stock_event(DAYS[6],DAYS[5]),
                                                      'ann_date':DAYS[4],'imp_ann_date':DAYS[4]}]
        data['sessions']=DAYS[:5]
        data['bars']={c:{d:v for d,v in rows.items() if d in data['sessions']} for c,rows in data['bars'].items()}
        result=run(data,signals,cfg)
        self.assertEqual(supplement(result)[0]['status'],'filled')

    def test_signal_suspension_is_not_queued_and_decision_missing_quote_is_not_filled(self):
        for missing_day,verified in ((DAYS[2],True),(DAYS[3],True),(DAYS[3],False)):
            data,signals,cfg=fixture();del data['bars'][C][missing_day]
            proofs=[proof(missing_day,C)] if verified else []
            with self.subTest(day=missing_day,verified=verified),patch.dict(DISCLOSURES,{'full_day_suspensions':proofs}):
                result=run(data,signals,cfg)
                self.assertEqual(supplement(result),[])
                self.assertFalse(any(t['symbol']==C for t in result['trades']))
        data,signals,cfg=fixture();del data['bars'][A][DAYS[3]]
        with self.assertRaises(AppError):run(data,signals,cfg)


if __name__=='__main__':
    unittest.main()
