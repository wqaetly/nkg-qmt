"""Comparable dated reference curves, deliberately separate from executable ledgers."""
from decimal import Decimal

from .automatic import benchmark
from .backtest import metrics
from .config import AppError, date_key, dec, symbol_key


ETF_NAMES={'515180.SH':'易方达中证红利 ETF','510880.SH':'华泰柏瑞上证红利 ETF'}
LONG_BENCHMARK_SOURCE='https://www.sse.com.cn/disclosure/fund/announcement/c/2020-11-20/510880_20201120_1.pdf'


def reference_series(client, sessions, symbol):
    series=[]; start=0
    while start<len(sessions)-1:
        stop=min(start+1500,len(sessions))
        chunk=sessions[start:stop]
        result=benchmark(client,chunk[0],chunk[-1],{'benchmark':symbol})
        if [point['day'] for point in result['series']]!=chunk:
            raise AppError('ETF 行情和因子未完整覆盖相同交易日历，不插值或沿用旧价。')
        # One shared session joins independently normalized batches below fund_adj's 2000-row cap.
        base=Decimal(series[-1]['value']) if series else Decimal(1)
        points=result['series'][1:] if series else result['series']
        series.extend({'day':p['day'],'value':str(base*Decimal(p['value']))} for p in points)
        if stop==len(sessions):break
        start=stop-1
    return series


def build_comparisons(client, sessions, initial_cash='10000', symbol='515180.SH'):
    if not isinstance(sessions,list) or len(sessions)<2 or sessions!=sorted(set(sessions)):
        raise AppError('比较基准至少需要两个严格递增、不重复的交易日。')
    for day in sessions:
        date_key(day)
    symbol_key(symbol)
    initial=dec(initial_cash,positive=True)
    cash_curve=[{'day':day,'equity':str(initial),'exposure':0} for day in sessions]
    cash={'label':'零收益现金基准','status':'diagnostic','start':sessions[0],'end':sessions[-1],
          'metrics':metrics(cash_curve,[],initial),
          'series':[{'day':day,'value':'1'} for day in sessions],
          'limitations':['现金按名义本金保持不变，不计利息和通胀；不是货币基金收益。']}
    etf={'label':f"{symbol[:6]} {ETF_NAMES.get(symbol,'ETF')} · 复权参考",'symbol':symbol,'status':'diagnostic',
         'start':sessions[0],'end':sessions[-1],
         'source_refs':['https://tushare.pro/document/2?doc_id=127','https://tushare.pro/document/2?doc_id=199'],
         'limitations':['按每个交易日收盘价×复权因子归一至期初1；不另加现金分红，避免重复计算。',
                        '这是连续持有的复权参考曲线，不是整手、限价或次日开盘成交账本。',
                        '未扣投资者佣金、滑点和个人税费；与个股税前、现金分红不再投资的模拟口径有差异。']}
    try:
        if symbol=='510880.SH':
            etf['source_refs'].append(LONG_BENCHMARK_SOURCE)
            if sessions[0]<'20070118':
                raise AppError('510880 于 2007-01-18 上市，不能用上市后数据替代此前窗口。')
        series=reference_series(client,sessions,symbol)
        curve=[{'day':p['day'],'equity':str(initial*Decimal(p['value'])),'exposure':1} for p in series]
        values=metrics(curve,[],initial)
        # A reference series has no actual orders or paid trading costs.
        for key in ('costs','gross_traded_value','two_way_turnover','trade_count'):
            values[key]=None
        etf.update(metrics=values,series=series)
    except (AppError,KeyError,TypeError,ValueError) as exc:
        etf.update(status='blocked',metrics={},series=[],
                   message=str(exc) if isinstance(exc,AppError) else 'ETF 基准数据字段无效，未输出收益比较。')
    return [cash,etf]
