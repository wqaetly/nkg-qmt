import copy
import unittest
from decimal import Decimal

from qmt.backtest import simulate
from qmt.config import AppError, load_config
from qmt.research import DISCLOSURES, implemented_dividends, summarize
from test_automatic import fixture


class CorporateShareBasisTests(unittest.TestCase):
    def correction(self):
        return next(c for c in DISCLOSURES['cash_joint_periods'] if c['expected'][0]['ts_code']=='600720.SH')

    def test_duplicate_fiscal_rows_keep_exact_evidence_and_one_payment(self):
        correction=self.correction()
        original=copy.deepcopy(correction['expected'])
        expected=implemented_dividends(original,'20240604')
        self.assertEqual(len(expected),1)
        event=expected[0]
        self.assertEqual(event['cash_div_tax'],'0.3674')
        self.assertEqual(event['fiscal_components'],correction['fiscal_components'])
        self.assertEqual(len(event['correction_originals']),3)
        self.assertEqual(event['correction_fingerprint'],'f548885ccfab4ad8585cde0c317eebf2c1c6a90b66cd1a2e05e0f951c9774d23')
        self.assertEqual(event['correction_source_sha256'],correction['source_sha256'])
        self.assertEqual(event['correction_component_sources'],correction['component_sources'])
        self.assertEqual(original,correction['expected'])
        self.assertEqual(implemented_dividends(expected,'20240604'),expected)

    def test_proposals_do_not_make_implementation_known_early(self):
        original=self.correction()['expected']
        for day in ('20240327','20240417','20240603'):
            self.assertEqual(implemented_dividends(original,day),[])
            normalized=implemented_dividends(original,'20240604')
            self.assertEqual(implemented_dividends(normalized,day),[])

    def test_missing_duplicate_evidence_or_new_revision_is_not_silently_merged(self):
        original=self.correction()['expected']
        for index in range(3):
            with self.subTest(index=index),self.assertRaises(AppError):
                implemented_dividends(original[:index]+original[index+1:],'20240604')
        future={**original[-1],'cash_div_tax':.258,'ann_date':'20240610','imp_ann_date':'20240610'}
        self.assertEqual(implemented_dividends(original+[future],'20240604'),implemented_dividends(original,'20240604'))
        with self.assertRaises(AppError):implemented_dividends(original+[future],'20240610')

    def test_verified_shares_and_sources_cannot_be_forged(self):
        event=implemented_dividends(self.correction()['expected'],'20240604')[0]
        variants=[]
        for shares in ('776290282','0','-1','2.5','NaN'):
            changed=copy.deepcopy(event)
            changed['fiscal_components'][0]['distribution_shares']=shares
            variants.append(changed)
        for key,value in (('correction_source','https://example.com/claim'),('correction_source_sha256','fake'),
                          ('correction_component_sources',[]),('correction_available_on','20240327')):
            variants.append({**event,key:value})
        variants.append({k:v for k,v in event.items() if k!='event_kind'})
        for row in variants:
            with self.subTest(row=row),self.assertRaises(AppError):
                implemented_dividends([row],'20240604')

    def test_company_cash_uses_distribution_shares_for_each_fiscal_year(self):
        _,dossier=fixture(symbol='600720.SH')
        dossier['data']['dividend']=[r for r in dossier['data']['dividend'] if r['end_date'][:4] not in ('2022','2023')]+copy.deepcopy(self.correction()['expected'])
        for row in dossier['data']['balancesheet']:
            if row['end_date']=='20221231':row['total_share']=776290282
            if row['end_date']=='20231231':row['total_share']=2061708481
        summary=summarize(dossier,{'name':'中交设计','industry':'工程咨询'},
                          {'trade_date':dossier['as_of'],'close':'8','total_share':'206170.8481','dv_ttm':4})
        annual={r['period']:r for r in summary['annual']}
        for year,dps in (('2022','0.1104'),('2023','0.257')):
            row=annual[year+'1231']
            self.assertEqual(Decimal(row['implemented_dps']),Decimal(dps))
            self.assertEqual(Decimal(row['estimated_dividend_cash']),Decimal(dps)*Decimal('2061708481'))
            self.assertEqual(row['estimated_ordinary_cash'],row['estimated_dividend_cash'])
            self.assertEqual(len(row['dividend_cash_basis']),1)
        self.assertNotEqual(Decimal(annual['20221231']['estimated_dividend_cash']),Decimal('0.1104')*Decimal('776290282'))

    def test_personal_cash_uses_own_record_date_shares_only(self):
        code='600720.SH';days=['20240604','20240605','20240606','20240612','20240613','20240614']
        events=implemented_dividends(self.correction()['expected'],days[-1])
        market={'sessions':days,'universe_as_of':days[0],'industries':{code:'测试行业'},'dividends':{code:events},
                'bars':{code:{d:{'open':'8','high':'8.2','low':'7.8','close':'8','vol':'10000','up':'8.8','down':'7.2'} for d in days}}}
        signals={days[0]:[{'symbol':code,'reference_price':'8','known_on':days[0]}]}
        cfg={**load_config(),'slippage_bps':0,'paper_cash':'10000','paper_max_position':'2000','paper_max_sector':'4000'}
        result=simulate(market,signals,cfg,max_holdings=5)
        self.assertEqual(len(result['cash_events']),1)
        self.assertEqual(Decimal(result['cash_events'][0]['amount']),Decimal('73.48'))
        self.assertEqual(result['equity_curve'][-1]['positions'],{code:200})
        self.assertEqual(Decimal(result['equity_curve'][-1]['cash']),Decimal('8468.48'))



if __name__=='__main__':unittest.main()
