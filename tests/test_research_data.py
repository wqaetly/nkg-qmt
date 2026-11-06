import copy
import hashlib
import json
import unittest
from decimal import Decimal

from qmt.backtest import simulate
from qmt.config import AppError, load_config
from qmt.research import DISCLOSURES, implemented_dividends, normalize_cash_joint_periods, normalize_cash_overlaps, normalize_stock_only, statements, summarize
from test_automatic import fixture


class DisclosedDataTests(unittest.TestCase):
    def test_joint_distribution_is_one_payment_with_exact_fiscal_components(self):
        correction=DISCLOSURES['cash_joint_periods'][0]
        originals=copy.deepcopy(correction['expected'])
        events=implemented_dividends(originals,'20260526')
        self.assertEqual(len(events),1)
        event=events[0]
        self.assertEqual(event['event_kind'],'cash_joint_periods')
        self.assertEqual(event['cash_div_tax'],'0.312')
        self.assertEqual(event['end_date'],'20260331')
        self.assertEqual(event['fiscal_components'],[
            {'end_date':'20251231','cash_div_tax':'0.132'},
            {'end_date':'20260331','cash_div_tax':'0.18'}])
        self.assertNotIn('special_cash_div_tax',event)
        self.assertNotIn('ordinary_cash_div_tax',event)
        self.assertEqual(event['correction_originals'],originals)
        self.assertEqual(event['correction_fingerprint'],'097140cf8af3e877dc845d2b872326d2ff348a93518fdfa4ece1498e6081e35d')
        self.assertEqual(event['correction_source'],correction['source'])
        self.assertEqual(implemented_dividends(events,'20260526'),events)
        self.assertEqual(implemented_dividends(originals[::-1],'20260526'),events)
        self.assertEqual(originals,correction['expected'])

    def test_joint_distribution_is_not_known_before_implementation_notice(self):
        originals=DISCLOSURES['cash_joint_periods'][0]['expected']
        self.assertEqual(normalize_cash_joint_periods(originals,'20260525'),originals)
        self.assertEqual(implemented_dividends(originals,'20260525'),[])
        self.assertEqual(implemented_dividends(implemented_dividends(originals,'20260526'),'20260525'),[])

    def test_future_joint_revision_does_not_freeze_an_earlier_signal(self):
        originals=DISCLOSURES['cash_joint_periods'][0]['expected']
        future={**originals[1],'ann_date':'20260610','imp_ann_date':'20260610','cash_div_tax':.181}
        expected=implemented_dividends(originals,'20260526')
        self.assertEqual(implemented_dividends(originals+[future],'20260526'),expected)
        self.assertEqual(implemented_dividends(expected+[future],'20260526'),expected)
        with self.assertRaises(AppError):implemented_dividends(originals+[future],'20260610')

    def test_joint_distribution_requires_both_exact_rows_and_no_extras(self):
        originals=DISCLOSURES['cash_joint_periods'][0]['expected']
        cases=[originals[:1],originals[1:],originals+[originals[0]]]
        for changes in ({'cash_div_tax':.181},{'pay_date':'20260602'},{'ann_date':'20260523'},
                        {'record_date':'20260528'},{'end_date':None},{'extra_source_field':'new schema'}):
            cases.append([originals[0],{**originals[1],**changes}])
        cases.append([{**r,'ts_code':'600099.SH'} for r in originals])
        for rows in cases:
            with self.subTest(rows=rows),self.assertRaises(AppError):
                implemented_dividends(rows,'20260526')

    def test_joint_distribution_metadata_and_fiscal_attribution_cannot_be_forged(self):
        event=implemented_dividends(DISCLOSURES['cash_joint_periods'][0]['expected'],'20260526')[0]
        changes=[{'cash_div_tax':'0.18'},{'end_date':'20251231'},{'correction_fingerprint':'fake'},
                 {'correction_source':'https://example.com/claim'},{'correction_available_on':'20260429'},
                 {'correction_originals':event['correction_originals'][:1]},
                 {'fiscal_components':[{'end_date':'20251231','cash_div_tax':'0.312'}]},
                 {'event_kind':'cash_overlap'},{'special_cash_div_tax':'.18'}]
        variants=[{**event,**c} for c in changes]
        variants.append({k:v for k,v in event.items() if k!='event_kind'})
        for row in variants:
            with self.subTest(row=row),self.assertRaises(AppError):
                implemented_dividends([row],'20260526')
        unknown={**event,'ts_code':'600099.SH','event_kind':'cash','correction_originals':[]}
        with self.assertRaises(AppError):implemented_dividends([unknown],'20260526')

    def test_joint_fiscal_years_and_window_boundary_preserve_cash_total(self):
        originals=DISCLOSURES['cash_joint_periods'][0]['expected']
        _,dossier=fixture(day='20270430',symbol='002043.SZ')
        dossier['data']['dividend']=copy.deepcopy(originals)
        for api in ('income','cashflow','balancesheet','fina_indicator','fina_audit'):
            source=next(r for r in dossier['data'][api] if r['end_date']=='20251231')
            dossier['data'][api].append({**source,'end_date':'20261231','ann_date':'20270401'})
        quote={'trade_date':dossier['as_of'],'close':'8','total_share':'.1','dv_ttm':3}
        result=summarize(dossier,{'name':'兔宝宝','industry':'装饰材料'},quote)
        annual={r['period']:r for r in result['annual']}
        for period,dps in (('20251231','0.132'),('20261231','0.18')):
            self.assertEqual(annual[period]['implemented_dps'],dps)
            self.assertEqual(annual[period]['ordinary_dps'],dps)
        # Cash stays whole if either fiscal component is in scope; old research periods do not.
        events=implemented_dividends(originals,'20341231',earliest_period='20260101')
        self.assertEqual(events[0]['cash_div_tax'],'0.312')
        self.assertEqual(len(events[0]['fiscal_components']),2)
        self.assertEqual(implemented_dividends(originals,'20341231',earliest_period='20260401'),[])
        dossier['as_of']='20341231';quote['trade_date']='20341231'
        annual={r['period']:r for r in summarize(dossier,{'name':'兔宝宝'},quote)['annual']}
        self.assertIsNone(annual['20251231']['implemented_dps'])
        self.assertIsNone(annual['20251231']['ordinary_dps'])
        self.assertEqual(annual['20261231']['implemented_dps'],'0.18')

    def test_joint_cash_event_is_settled_once_by_existing_replay(self):
        code='002043.SZ';days=['20260526','20260527','20260528','20260529','20260601','20260602']
        events=implemented_dividends(DISCLOSURES['cash_joint_periods'][0]['expected'],days[-1])
        data={'sessions':days,'universe_as_of':days[0],'industries':{code:'测试行业'},'dividends':{code:events},
              'bars':{code:{d:{'open':'8','high':'8.2','low':'7.8','close':'8','vol':'10000','up':'8.8','down':'7.2'} for d in days}}}
        signals={days[0]:[{'symbol':code,'reference_price':'8','known_on':days[0]}]}
        cfg={**load_config(),'slippage_bps':0,'paper_cash':'10000','paper_max_position':'2000','paper_max_sector':'4000'}
        result=simulate(data,signals,cfg,max_holdings=5)
        self.assertEqual(len(result['cash_events']),1)
        self.assertEqual(Decimal(result['cash_events'][0]['amount']),Decimal('62.40'))
        self.assertEqual(result['equity_curve'][-1]['positions'],{code:200})
        self.assertEqual(Decimal(result['equity_curve'][-1]['cash']),Decimal('8457.40'))

    def test_shenhua_total_includes_special_and_keeps_exact_evidence(self):
        correction=DISCLOSURES['cash_dividend_overlaps'][0]
        originals=copy.deepcopy(correction['expected'])
        events=implemented_dividends(originals,'20170703')
        self.assertEqual(len(events),1)
        event=events[0]
        self.assertEqual(Decimal(str(event['cash_div_tax'])),Decimal('2.970'))
        self.assertEqual(Decimal(event['ordinary_cash_div_tax']),Decimal('.460'))
        self.assertEqual(Decimal(event['special_cash_div_tax']),Decimal('2.510'))
        self.assertEqual(Decimal(event['ordinary_cash_div_tax'])+Decimal(event['special_cash_div_tax']),
                         Decimal(str(event['cash_div_tax'])))
        self.assertEqual([event[k] for k in ('end_date','record_date','ex_date','pay_date')],
                         ['20161231','20170707','20170710','20170710'])
        self.assertEqual(event['correction_originals'],originals)
        self.assertEqual(event['correction_fingerprint'],hashlib.sha256(json.dumps(originals,sort_keys=True).encode()).hexdigest())
        for key in ('source','available_on'):
            self.assertEqual(event['correction_'+key],correction[key])
        self.assertEqual(event['correction_source_pages'],'1—2')
        self.assertEqual(originals,correction['expected'])
        self.assertEqual(implemented_dividends(events,'20170703'),events)
        self.assertEqual(implemented_dividends(originals[::-1],'20170703'),events)
        self.assertEqual(implemented_dividends(originals,'20261106',earliest_period='19000101'),events)

    def test_shenhua_classification_is_not_known_before_implementation_announcement(self):
        originals=DISCLOSURES['cash_dividend_overlaps'][0]['expected']
        self.assertEqual(normalize_cash_overlaps(originals,'20170702'),originals)
        self.assertEqual(implemented_dividends(originals,'20170702'),[])
        # An exact total alone still contains the disclosed special component.
        self.assertEqual(implemented_dividends(originals[:1],'20170703')[0]['ordinary_cash_div_tax'],'0.460')
        # A special component alone is not enough to invent a total row.
        self.assertEqual(normalize_cash_overlaps(originals[1:],'20170703'),originals[1:])

    def test_shenhua_unverified_overlap_and_extra_duplicates_still_fail(self):
        originals=DISCLOSURES['cash_dividend_overlaps'][0]['expected']
        for changes in ({'cash_div_tax':2.6},{'pay_date':'20170711'},{'ann_date':'20170319'},
                        {'extra_source_field':'new schema'}):
            rows=[originals[0],{**originals[1],**changes}]
            self.assertEqual(normalize_cash_overlaps(rows,'20170703'),rows)
            with self.subTest(changes=changes),self.assertRaises(AppError):
                implemented_dividends(rows,'20170703')
        rows=originals+[originals[1]]
        self.assertEqual(normalize_cash_overlaps(rows,'20170703'),rows)
        with self.assertRaises(AppError):implemented_dividends(rows,'20170703')
        unknown=[{**r,'ts_code':'600099.SH'} for r in originals]
        self.assertEqual(normalize_cash_overlaps(unknown,'20170703'),unknown)
        with self.assertRaises(AppError):implemented_dividends(unknown,'20170703')

    def test_shenhua_correction_metadata_cannot_be_forged(self):
        original=implemented_dividends(DISCLOSURES['cash_dividend_overlaps'][0]['expected'],'20170703')[0]
        for changes in ({'correction_fingerprint':'fake'},{'cash_div_tax':5.48},
                        {'ordinary_cash_div_tax':'2.97'},{'special_cash_div_tax':'0'},
                        {'correction_source':'https://example.com/claim'},
                        {'correction_available_on':'20170318'},{'correction_originals':[]}):
            with self.subTest(changes=changes),self.assertRaises(AppError):
                implemented_dividends([{**original,**changes}],'20170703')

    def test_long_replay_keeps_old_cash_events_but_still_filters_future_announcements(self):
        row={'ts_code':'600001.SH','div_proc':'实施','end_date':'20141231','ann_date':'20150331',
             'imp_ann_date':'20150504','record_date':'20150508','ex_date':'20150511','pay_date':'20150511',
             'cash_div_tax':'.5','stk_div':0}
        self.assertEqual(implemented_dividends([row],'20261106'),[])
        self.assertEqual(implemented_dividends([row],'20261106',earliest_period='19000101'),[row])
        self.assertEqual(implemented_dividends([row],'20150430',earliest_period='19000101'),[])

    def test_verified_stock_only_events_keep_no_cash_payment_date(self):
        for correction in DISCLOSURES['stock_only']:
            original=copy.deepcopy(correction['expected'])
            events=implemented_dividends([original],correction['available_on'])
            self.assertEqual(len(events),1)
            event=events[0]
            self.assertEqual(event['event_kind'],'stock_only')
            self.assertEqual(Decimal(event['cash_div_tax']),0)
            self.assertIsNone(event['pay_date'])
            self.assertEqual(event['stock_list_date'],correction['stock_list_date'])
            self.assertEqual(event['correction_fingerprint'],hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest())
            self.assertEqual(event['correction_originals'],[original])
            self.assertEqual(original,correction['expected'])
            self.assertEqual(implemented_dividends(events,correction['available_on']),events)

    def test_later_annual_evidence_is_not_backfilled_into_earlier_signal(self):
        fangda=DISCLOSURES['stock_only'][1]['expected']
        self.assertEqual(normalize_stock_only([fangda],'20210316'),[fangda])
        with self.assertRaises(AppError):implemented_dividends([fangda],'20210316')
        self.assertEqual(implemented_dividends([fangda],'20210317')[0]['cash_div_tax'],'0')
        cosco=DISCLOSURES['stock_only'][0]['expected']
        self.assertEqual(implemented_dividends([cosco],'20210707'),[])

    def test_raw_mismatch_never_gets_zero_or_invented_pay_date(self):
        original=DISCLOSURES['stock_only'][0]['expected']
        for changes in ({'stk_div':.4},{'record_date':'20210712'},{'ann_date':'20210528'},
                        {'ts_code':'601918.SH'},{'extra_source_field':'new schema'}):
            row={**original,**changes}
            self.assertEqual(normalize_stock_only([row],'20261106'),[row])
            with self.subTest(changes=changes),self.assertRaises(AppError):
                implemented_dividends([row],'20261106')

    def test_unverified_marker_or_tampered_fingerprint_is_not_trusted(self):
        original=DISCLOSURES['stock_only'][0]['expected']
        event=implemented_dividends([original],'20261106')[0]
        for changes in ({'correction_fingerprint':'fake'},{'cash_div_tax':'1'},{'pay_date':'20210714'},
                        {'stock_list_date':'20210714'},{'correction_source':'https://example.com/claim'}):
            with self.subTest(changes=changes),self.assertRaises(AppError):
                implemented_dividends([{**event,**changes}],'20261106')

    def test_unknown_missing_cash_with_payment_date_stays_missing(self):
        row={**DISCLOSURES['stock_only'][0]['expected'],'ts_code':'600099.SH','pay_date':'20210714'}
        with self.assertRaises(AppError):implemented_dividends([row],'20261106')

    def test_confirmed_zero_cash_is_visible_and_not_sustainable_cash_dividend(self):
        row,dossier=fixture(day='20221230',symbol='601919.SH')
        dossier['data']['dividend'].append(copy.deepcopy(DISCLOSURES['stock_only'][0]['expected']))
        # Reuse the disclosed 2021 fiscal row as an explicit older full-year fixture.
        for api in ('income','cashflow','balancesheet','fina_indicator','fina_audit'):
            source=next(r for r in dossier['data'][api] if r['end_date']=='20211231')
            dossier['data'][api].append({**source,'end_date':'20201231','ann_date':'20210331'})
        result=summarize(dossier,{'name':'中远海控','industry':'航运'},
                         {'trade_date':'20221230','close':'8','total_share':'.1','dv_ttm':3})
        year=next(a for a in result['annual'] if a['period']=='20201231')
        self.assertEqual(year['implemented_dps'],'0')
        self.assertTrue(any('已核实零现金分红' in s for s in result['issues']))
        self.assertFalse(result['gate'])

    def test_material_share_conflict_is_preserved_and_names_field(self):
        base={'end_date':'20220930','ann_date':'20221029','total_share':4319607279.0,'total_assets':31225405794.27}
        with self.assertRaisesRegex(AppError,'total_share'):
            statements([base,{**base,'total_share':994965038.0}],'20221230')
        with self.assertRaisesRegex(AppError,'total_share'):
            statements([{**base,'total_share':19868519955.0},{**base,'total_share':19869000000.0}],'20221230')
        self.assertEqual(statements([base,{**base,'total_share':None}],'20221230')['20220930']['total_share'],base['total_share'])


if __name__=='__main__':unittest.main()
