import copy
import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import Mock

from qmt.comparison import build_comparisons
from qmt.config import AppError


DAYS=['20221230','20230103','20240102']


def client(prices=('10','9','12'),factors=('2','2.222222222222222222222222222','2.5')):
    data={api:[{'ts_code':'515180.SH','trade_date':day,field:value} for day,value in zip(DAYS,values)]
          for api,field,values in [('fund_daily','close',prices),('fund_adj','adj_factor',factors)]}
    instance=Mock()
    instance.call.side_effect=lambda api,params,fields:copy.deepcopy(data[api])
    return instance,data


class ComparisonTests(unittest.TestCase):
    def test_cash_stays_zero_and_etf_counts_factor_once(self):
        source,_=client()
        cash,etf=build_comparisons(source,DAYS)
        self.assertEqual(cash['metrics']['cagr'],0)
        self.assertEqual(cash['metrics']['max_drawdown'],0)
        self.assertTrue(all(p['value']=='1' for p in cash['series']))
        self.assertEqual(etf['status'],'diagnostic')
        self.assertEqual(Decimal(etf['series'][0]['value']),Decimal(1))
        self.assertAlmostEqual(float(etf['series'][1]['value']),1)
        self.assertEqual(etf['metrics']['total_return'],.5)
        self.assertIsNone(etf['metrics']['costs'])
        self.assertEqual({c.args[0] for c in source.call.call_args_list},{'fund_daily','fund_adj'})

    def test_factor_base_scaling_and_initial_cash_do_not_change_return(self):
        first,_=client(factors=('2','2.2','2.5'))
        second,_=client(factors=('20','22','25'))
        a=build_comparisons(first,DAYS,'10000')[1]
        b=build_comparisons(second,DAYS,'100000')[1]
        self.assertEqual(a['series'],b['series'])
        self.assertEqual(a['metrics']['cagr'],b['metrics']['cagr'])

    def test_interior_missing_day_does_not_silently_shorten_window(self):
        source,data=client()
        data['fund_daily'].pop(1);data['fund_adj'].pop(1)
        cash,etf=build_comparisons(source,DAYS)
        self.assertEqual(cash['metrics']['total_return'],0)
        self.assertEqual(etf['status'],'blocked')
        self.assertEqual(etf['metrics'],{})
        self.assertEqual(etf['series'],[])

    def test_duplicate_wrong_symbol_zero_factor_and_outside_dates_block(self):
        for mutate in (
            lambda d:d['fund_adj'].append(d['fund_adj'][0]),
            lambda d:d['fund_daily'][0].update(ts_code='510300.SH'),
            lambda d:d['fund_adj'][1].update(adj_factor=0),
            lambda d:d['fund_adj'][1].update(trade_date='20250101'),
            lambda d:d['fund_daily'][1].update(trade_date='20230102'),
        ):
            source,data=client();mutate(data)
            self.assertEqual(build_comparisons(source,DAYS)[1]['status'],'blocked')

    def test_invalid_calendar_and_cash_fail_before_data_request(self):
        for sessions in ([],DAYS[:1],DAYS[::-1],DAYS+DAYS[:1],['2022011','20220112']):
            source,_=client()
            with self.assertRaises(AppError):build_comparisons(source,sessions)
            source.call.assert_not_called()
        source,_=client()
        with self.assertRaises(AppError):build_comparisons(source,DAYS,'0')

    def test_unavailable_etf_does_not_hide_cash_comparison(self):
        source=Mock();source.call.side_effect=AppError('接口不可用')
        cash,etf=build_comparisons(source,DAYS)
        self.assertEqual(cash['status'],'diagnostic')
        self.assertEqual(etf['status'],'blocked')
        self.assertEqual(etf['message'],'接口不可用')

    def test_explicit_long_benchmark_uses_its_own_identity(self):
        source,data=client()
        for rows in data.values():
            for row in rows:row['ts_code']='510880.SH'
        etf=build_comparisons(source,DAYS,symbol='510880.SH')[1]
        self.assertEqual(etf['status'],'diagnostic')
        self.assertEqual(etf['symbol'],'510880.SH')
        self.assertIn('华泰柏瑞上证红利',etf['label'])
        self.assertNotIn('易方达',etf['label'])
        self.assertTrue(all(c.args[1]['ts_code']=='510880.SH' for c in source.call.call_args_list))

    def test_long_series_batches_with_overlap_and_preserves_all_days(self):
        days=[(datetime(2014,12,31)+timedelta(days=i)).strftime('%Y%m%d') for i in range(3005)]
        positions={day:i for i,day in enumerate(days)}
        source=Mock()
        def response(api,params,fields):
            selected=[d for d in days if params['start_date']<=d<=params['end_date']]
            self.assertLessEqual(len(selected),1500)
            key='close' if api=='fund_daily' else 'adj_factor'
            return [{'ts_code':'510880.SH','trade_date':day,key:str(Decimal(10)+Decimal(positions[day])/100) if key=='close' else '3'} for day in selected]
        source.call.side_effect=response
        etf=build_comparisons(source,days,symbol='510880.SH')[1]
        self.assertEqual(etf['status'],'diagnostic')
        self.assertEqual([p['day'] for p in etf['series']],days)
        self.assertEqual(len(source.call.call_args_list),6)
        self.assertAlmostEqual(float(etf['series'][-1]['value']),4.004)
        self.assertAlmostEqual(etf['metrics']['total_return'],3.004)
        calls=source.call.call_args_list
        self.assertEqual(calls[0].args[1]['end_date'],calls[2].args[1]['start_date'])

    def test_pre_listing_window_is_not_shortened(self):
        source,_=client()
        etf=build_comparisons(source,['20070117','20070118'],symbol='510880.SH')[1]
        self.assertEqual(etf['status'],'blocked')
        source.call.assert_not_called()


if __name__=='__main__':unittest.main()
