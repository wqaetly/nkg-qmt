import copy
import hashlib
import json
import unittest
from decimal import Decimal

from qmt.config import AppError
from qmt.research import DISCLOSURES, implemented_dividends, normalize_stock_only, statements, summarize
from test_automatic import fixture


class DisclosedDataTests(unittest.TestCase):
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
