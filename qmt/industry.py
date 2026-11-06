"""Date-bounded SW industry membership; unknown intervals never use today's label."""
from collections import defaultdict
from .config import AppError, date_key

FIELDS='l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new'


def industry_at(rows, day):
    found={}
    for row in rows:
        start=row.get('in_date'); end=row.get('out_date')
        if not start:
            continue
        date_key(start)
        if end:
            date_key(end)
            if end<start:raise AppError('行业纳入/剔除日期顺序异常。')
        if start<=day and (not end or day<end):
            if not row.get('l1_code') or not row.get('l1_name'):raise AppError('一级行业代码或名称缺失。')
            found[row['l1_code']]=row['l1_name']
    if len(found)!=1:
        return None
    code,name=next(iter(found.items()))
    return {'code':code,'name':name,'label':code+' '+name}


def load_industry_history(client, codes, progress=print):
    selected={}; sectors=set()
    for i,code in enumerate(codes,1):
        rows=[]
        for flag in ('Y','N'):
            part=client.call('index_member_all',{'ts_code':code,'is_new':flag},FIELDS)
            if len(part)>=2000 or any(r['ts_code']!=code for r in part):raise AppError('行业历史记录截断或代码不符。')
            rows.extend(part)
        selected[code]=rows
        sectors.update(r['l1_code'] for r in rows)
        if i%5==0:progress(f'历史行业记录 {i}/{len(codes)}',flush=True)
    peers=defaultdict(list)
    for sector in sorted(sectors):
        for flag in ('Y','N'):
            rows=client.call('index_member_all',{'l1_code':sector,'is_new':flag},FIELDS)
            if len(rows)>=2000 or any(r['l1_code']!=sector for r in rows):
                raise AppError('同行历史集合可能截断，暂停行业相对估值。')
            for row in rows:peers[row['ts_code']].append(row)
    return selected,dict(peers)
