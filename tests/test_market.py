import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from qmt.config import AppError
from qmt.market import PageLimitError, Tushare, year_ranges


CODE = '600001.SH'


class MarketHistoryTests(unittest.TestCase):
    def test_year_ranges_have_no_gaps_or_overlap(self):
        self.assertEqual(year_ranges('20141231','20160102'),
                         [('20141231','20141231'),('20150101','20151231'),('20160101','20160102')])
        for start,end in [('20260102','20260101'),('20260230','20260301')]:
            with self.assertRaises(AppError):
                year_ranges(start,end)

    def test_long_bars_and_candles_keep_both_ends_and_limits(self):
        calls=[]
        def response(api,params,fields):
            calls.append((api,params))
            rows=[]
            for day in sorted({params['start_date'],params['end_date']}):
                row={'ts_code':CODE,'trade_date':day}
                row.update({'up_limit':11,'down_limit':9} if api=='stk_limit'
                           else {'open':10,'high':10.5,'low':9.5,'close':10,'vol':123})
                rows.append(row)
            return rows
        with patch.object(Tushare,'call',side_effect=response):
            client=Tushare()
            bars=client.bars(CODE,'19941230','20261106')
            candles=client.candles(CODE,'19941230','20261106')
        self.assertEqual(min(bars),'19941230')
        self.assertEqual(max(bars),'20261106')
        self.assertEqual([c['day'] for c in candles],sorted(bars))
        self.assertTrue(all(b['up']==Decimal(11) and b['down']==Decimal(9) for b in bars.values()))
        self.assertTrue(all(p['start_date'][:4]==p['end_date'][:4] for _,p in calls))

    def test_out_of_slice_rows_do_not_silently_overlap(self):
        row={'ts_code':CODE,'trade_date':'20260102','open':10,'high':10,'low':10,'close':10,'vol':1}
        with patch.object(Tushare,'call',return_value=[row]),self.assertRaises(AppError):
            Tushare().candles(CODE,'20251231','20260102')

    def test_financial_warmup_uses_endpoint_date_basis_and_keeps_conflicts(self):
        calls=[]
        def response(api,params,fields):
            calls.append((api,params))
            if api=='dividend':
                return []
            year=int(params['start_date'][:4])
            # Indicator range means report period; the other financial ranges mean announcement.
            row={'ts_code':CODE,'ann_date':f'{year}0401','end_date':f'{year-1}1231'}
            if api=='fina_indicator':
                row.update(end_date=f'{year}0331',ann_date=f'{year+1}0401')
            return [{**row,'total_share':n} for n in range(6)]
        with patch.object(Tushare,'call',side_effect=response):
            result=Tushare().dossier(CODE,'20261106',start_year=2009)
        self.assertEqual(result['errors'],{})
        self.assertEqual(result['start_year'],2009)
        self.assertEqual(len(result['data']['balancesheet']),108)
        self.assertEqual({r['total_share'] for r in result['data']['balancesheet']},set(range(6)))
        self.assertEqual(min(p['start_date'] for api,p in calls if api!='dividend'),'20090101')
        self.assertEqual([p for api,p in calls if api=='dividend'],[{'ts_code':CODE}])
        self.assertEqual(result['data']['income'][0]['end_date'],'20081231')
        self.assertEqual(result['data']['fina_indicator'][-1]['ann_date'],'20270401')

    def test_default_financial_start_and_invalid_year(self):
        with patch.object(Tushare,'call',return_value=[]) as call:
            result=Tushare().dossier(CODE,'20261106')
        self.assertEqual(result['start_year'],2017)
        self.assertEqual(call.call_count,6)
        self.assertEqual(min(c.args[1]['start_date'] for c in call.call_args_list if 'start_date' in c.args[1]),'20170101')
        self.assertTrue(all(c.args[1]['end_date']=='20261106' for c in call.call_args_list if 'end_date' in c.args[1]))
        with patch.object(Tushare,'call',return_value=[]):
            self.assertEqual(Tushare().dossier(CODE,'20270106')['start_year'],2018)
        for start in (2027,'2009',True,99):
            with self.assertRaises(AppError):
                Tushare().dossier(CODE,'20261106',start_year=start)

    def test_one_failed_slice_discards_partial_financial_api(self):
        def response(api,params,fields):
            if api=='income':
                if params['start_date']=='20100101':
                    raise AppError('test source unavailable')
                return [{'ts_code':CODE,'ann_date':'20090401','end_date':'20081231'}]
            return []
        with patch.object(Tushare,'call',side_effect=response):
            result=Tushare().dossier(CODE,'20101231',start_year=2009)
        self.assertEqual(result['data']['income'],[])
        self.assertIn('income',result['errors'])
        self.assertNotIn('balancesheet',result['errors'])

    def test_conservative_statement_slice_limit_stays_closed(self):
        def response(api,params,fields):
            return [{'ts_code':CODE,'ann_date':'20260401','end_date':'20251231'}]*100 if api=='balancesheet' else []
        with patch.object(Tushare,'call',side_effect=response):
            result=Tushare().dossier(CODE,'20261106',start_year=2026)
        self.assertIn('balancesheet',result['errors'])
        self.assertEqual(result['data']['balancesheet'],[])

    def test_documented_full_pages_fail_before_caching(self):
        for api,limit in [('daily',6000),('daily_basic',6000),('stk_limit',5800),('fina_indicator',100),('dividend',2000)]:
            with self.subTest(api=api), tempfile.TemporaryDirectory() as tmp:
                response={'code':0,'data':{'fields':['ts_code'],'items':[[CODE]]*limit}}
                with patch.dict('os.environ',{'TUSHARE_TOKEN':'test'}),patch('qmt.market.request_json',return_value=response):
                    with self.assertRaises(PageLimitError):
                        Tushare(Path(tmp)).call(api,{'ts_code':CODE},'ts_code')
                self.assertEqual(list(Path(tmp).glob('*.json')),[])

    def test_cached_full_page_is_not_treated_as_complete(self):
        api,params,fields='fina_indicator',{'ts_code':CODE},'ts_code'
        key=hashlib.sha256(json.dumps([api,params,fields],sort_keys=True).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/f'{api}-{key}.json').write_text(json.dumps({'rows':[{'ts_code':CODE}]*100}),encoding='utf-8')
            with patch('qmt.market.request_json') as request,self.assertRaises(PageLimitError):
                Tushare(Path(tmp)).call(api,params,fields)
            request.assert_not_called()

    def test_default_batch_reuses_cache_without_network(self):
        def response(url,payload):
            return {'code':0,'data':{'fields':payload['fields'].split(','),'items':[]}}
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'TUSHARE_TOKEN':'test'}),patch('qmt.market.time.sleep'):
            client=Tushare(Path(tmp))
            with patch('qmt.market.request_json',side_effect=response) as request:
                first=client.dossier(CODE,'20261106')
            self.assertEqual(request.call_count,6)
            with patch('qmt.market.request_json') as request:
                second=client.dossier(CODE,'20261106')
            request.assert_not_called()
            self.assertEqual(first,second)

    def test_full_batch_retries_only_that_api_by_year_and_discards_full_page(self):
        def response(api,params,fields):
            if api!='income':
                return []
            if params['start_date']=='20170101' and params['end_date']=='20261106':
                return [{'ts_code':CODE,'ann_date':'20260401','end_date':'20251231','n_income_attr_p':999}]*100
            return [{'ts_code':CODE,'ann_date':params['start_date'],'end_date':params['start_date'],'n_income_attr_p':1}]
        with patch.object(Tushare,'call',side_effect=response) as call:
            result=Tushare().dossier(CODE,'20261106')
        self.assertEqual(result['errors'],{})
        self.assertEqual(len(result['data']['income']),10)
        self.assertEqual({r['n_income_attr_p'] for r in result['data']['income']},{1})
        self.assertEqual(call.call_count,16)

    def test_explicit_page_limit_retries_and_failed_year_discards_everything(self):
        def response(api,params,fields):
            if api!='fina_indicator':
                return []
            if params['end_date']=='20261106' and params['start_date']=='20170101':
                raise PageLimitError('full page')
            if params['start_date']=='20180101':
                raise AppError('source unavailable')
            return [{'ts_code':CODE,'ann_date':'20170401','end_date':'20170331'}]
        with patch.object(Tushare,'call',side_effect=response):
            result=Tushare().dossier(CODE,'20261106')
        self.assertEqual(result['data']['fina_indicator'],[])
        self.assertEqual(result['errors'],{'fina_indicator':'source unavailable'})

    def test_other_errors_and_dividend_full_page_never_trigger_yearly_retry(self):
        with patch.object(Tushare,'call',side_effect=AppError('bad response')),patch.object(Tushare,'yearly') as yearly:
            result=Tushare().dossier(CODE,'20261106')
        yearly.assert_not_called()
        self.assertEqual(len(result['errors']),6)
        with patch.object(Tushare,'call',side_effect=lambda api,params,fields: [{'ts_code':CODE}]*2000 if api=='dividend' else []),patch.object(Tushare,'yearly') as yearly:
            result=Tushare().dossier(CODE,'20261106')
        yearly.assert_not_called()
        self.assertEqual(list(result['errors']),['dividend'])

    def test_malformed_full_page_is_not_a_page_limit_error(self):
        response={'code':0,'data':{'fields':['ts_code'],'items':[[CODE,'unexpected']]*100}}
        with patch.dict('os.environ',{'TUSHARE_TOKEN':'test'}),patch('qmt.market.request_json',return_value=response):
            with self.assertRaises(AppError) as caught:
                Tushare().call('fina_indicator',{'ts_code':CODE},'ts_code')
        self.assertNotIsInstance(caught.exception,PageLimitError)
        api,params,fields='fina_indicator',{'ts_code':CODE},'ts_code,ann_date'
        key=hashlib.sha256(json.dumps([api,params,fields],sort_keys=True).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/f'{api}-{key}.json').write_text(json.dumps({'rows':[{'ts_code':CODE}]*100}),encoding='utf-8')
            with self.assertRaises(AppError) as caught:
                Tushare(Path(tmp)).call(api,params,fields)
        self.assertNotIsInstance(caught.exception,PageLimitError)

    def test_default_batch_checks_report_period_for_indicator(self):
        row={'ts_code':CODE,'ann_date':'20270401','end_date':'20260331'}
        with patch.object(Tushare,'call',side_effect=lambda api,params,fields: [row] if api=='fina_indicator' else []):
            self.assertEqual(Tushare().dossier(CODE,'20261106')['errors'],{})
        with patch.object(Tushare,'call',side_effect=lambda api,params,fields: [row] if api=='income' else []):
            result=Tushare().dossier(CODE,'20261106')
        self.assertIn('income',result['errors'])
        self.assertEqual(result['data']['income'],[])


if __name__=='__main__':
    unittest.main()
