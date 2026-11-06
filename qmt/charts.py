from datetime import datetime, timedelta
from pathlib import Path
from .config import AppError, atomic_json
from .market import Tushare


def update_charts(workspace, report, automatic, progress=print):
    client=Tushare(Path(report['cache_path']))
    day=report['as_of']
    start=(datetime.strptime(day,'%Y%m%d')-timedelta(days=270)).strftime('%Y%m%d')
    codes=set(automatic.get('focus_symbols',[]))
    result={}
    for index,code in enumerate(sorted(codes),1):
        try:
            result[code]={'candles':client.candles(code,start,day),'error':None}
        except AppError as exc:
            result[code]={'candles':[],'error':str(exc)}
        if index==1 or index%20==0 or index==len(codes):
            progress(f'已更新 K 线 {index}/{len(codes)}',flush=True)
    atomic_json(workspace/'charts.json',{'as_of':day,'stocks':result})
    return result
