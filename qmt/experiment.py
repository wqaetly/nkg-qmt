"""Historical cohort and reproducible diagnostics; never optimize against a return target."""
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from .automatic import assess
from .config import AppError, atomic_json, date_key, dec, now_cn, load_config
from .market import Tushare
from .research import implemented_dividends, summarize
from .selection import load_rules, mainboard, positive, liquidity
from .service import read_json


EXPERIMENT = {'version':'historical-dividend-v2','selection_day':'20221230','development_end':'20241231',
              'validation_start':'20250101','pool_size':20,'entry_yields':['0.04','0.05','0.06'],
              'primary_entry_yield':'0.05','target_cagr':'0.10'}


def historical_name(rows, day):
    available=[r for r in rows if r['start_date']<=day and (not r.get('end_date') or day<=r['end_date'])
               and (not r.get('ann_date') or r['ann_date']<=day)]
    if len(available)!=1:
        return None
    return available[0]['name']


def historical_basics(client, day):
    basics={}
    for status in ('L','D','P'):
        rows=client.call('stock_basic',{'list_status':status},'ts_code,name,industry,market,list_date,delist_date')
        for row in rows:
            if (mainboard(row['ts_code']) and row.get('list_date') and row['list_date']<=day
                    and (not row.get('delist_date') or row['delist_date']>day)):
                basics[row['ts_code']]=row
    if len(basics)<1000:
        raise AppError('历史上市/退市证券集合明显不足，停止建池。')
    return basics


def protocol(cfg):
    code_hash=hashlib.sha256()
    for name in ('experiment.py','backtest.py','research.py','automatic.py','ledger.py','selection.py'):
        code_hash.update((Path(__file__).parent/name).read_bytes())
    return {**EXPERIMENT,'rules':load_rules(),'account_config':cfg,'engine_sha256':code_hash.hexdigest()}


def prepare_experiment(workspace, end, progress=print, cfg=None):
    path=workspace/'backtests'/EXPERIMENT['version']
    path.mkdir(parents=True,exist_ok=True)
    manifest=path/'protocol.json'
    registered=protocol(cfg or load_config())
    if manifest.exists() and read_json(manifest)!=registered:
        raise AppError('预先登记的回测协议已变化；请建立新实验版本。')
    if not manifest.exists():
        atomic_json(manifest,registered)
        atomic_json(path/'registration.json',{'registered_at':now_cn().isoformat(),'note':'本次历史实验运行前登记，不是声称2022年已实盘制定此策略。'})
    client=Tushare(workspace/'backtests'/'source-cache')
    day=EXPERIMENT['selection_day']
    if end<=EXPERIMENT['validation_start'] or end>now_cn().strftime('%Y%m%d'):
        raise AppError('回放截止日期须晚于验证开始且不晚于今天。')
    basics=historical_basics(client,day)
    quotes=client.prices(day)
    start=(datetime.strptime(day,'%Y%m%d')-timedelta(days=60)).strftime('%Y%m%d')
    sessions=client.calendar(start,day)[-20:]
    amounts={}
    for stamp in sessions:
        rows=client.call('daily',{'trade_date':stamp},'ts_code,trade_date,amount')
        if len(rows)<len(quotes)*.8 or len({r['ts_code'] for r in rows})!=len(rows):
            raise AppError('历史成交额快照缺失或重复。')
        for row in rows:
            if row['trade_date']!=stamp:raise AppError('历史成交日期不符。')
            amounts.setdefault(row['ts_code'],{})[stamp]=dec(row['amount'])*1000
    rules=load_rules()
    candidates=[]
    for code,basic in basics.items():
        q=quotes.get(code,{})
        pe=positive(q.get('pe_ttm')); dy=positive(q.get('dv_ttm'))
        if pe and pe<=25 and dy and dy>=3 and liquidity(amounts.get(code,{}),sessions,rules)['state']=='通过':
            candidates.append(code)
    candidates.sort(key=lambda code:(-dec(quotes[code]['dv_ttm']),dec(quotes[code]['pe_ttm']),code))
    chosen=[]; rejected=[]; dossiers={}; names={}
    for code in candidates:
        name_rows=client.call('namechange',{'ts_code':code},'ts_code,name,start_date,end_date,ann_date')
        if any(r['ts_code']!=code for r in name_rows):raise AppError('历史名称代码不符。')
        name=historical_name(name_rows,day)
        if not name or 'ST' in name or '退' in name:
            rejected.append({'symbol':code,'reason':'历史名称缺失或当时有风险警示'});continue
        dossier=client.dossier(code,end)
        row=summarize({**dossier,'as_of':day},{**basics[code],'name':name},quotes[code])
        if row.get('financial_company') or row['status']=='排除' or len(row['annual'])<5:
            rejected.append({'symbol':code,'reason':'当时金融模型不适用、亏损或历史不足'});continue
        chosen.append({'symbol':code,'name':name,'industry':basics[code].get('industry') or '未知行业',
                       'selection_quote':quotes[code],'selection_financial_period':row.get('latest_period')})
        dossiers[code]=dossier; names[code]=name_rows
        progress(f'历史研究池 {len(chosen)}/20：{code} {name}（按 {day} 可用指标）',flush=True)
        if len(chosen)==20:break
    if len(chosen)!=20:raise AppError('历史研究池不足20只，未用当前股票补齐。')
    cohort={'selected_on':day,'stocks':chosen,'rejected':rejected,
            'method':'当时上市（含后来退市），20日流动性，正PE≤25、历史股息率≥3%；按当时股息率降序、PE升序选20只非金融且至少五年财报的公司。'}
    old_cohort=path/'cohort.json'
    if old_cohort.exists() and read_json(old_cohort)!=cohort:
        raise AppError('历史名单发生修订，不能覆盖旧实验。')
    atomic_json(old_cohort,cohort)
    calendar=client.calendar(day,end)
    if calendar[0]!=day or calendar[-1]!=end:raise AppError('回放起止日必须是交易日。')
    bars={}; history={}; dividends={}
    for i,stock in enumerate(chosen,1):
        code=stock['symbol']
        bars[code]=client.bars(code,day,end)
        history[code]=client.call('daily_basic',{'ts_code':code,'start_date':'20200101','end_date':end},
                                 'ts_code,trade_date,close,total_share,pe_ttm,pb,dv_ttm')
        rows=client.call('daily',{'ts_code':code,'start_date':'20221101','end_date':end},'ts_code,trade_date,amount')
        amounts[code]={}
        for r in rows:
            stamp=date_key(r['trade_date'])
            if r['ts_code']!=code or not '20221101'<=stamp<=end or stamp in amounts[code]:raise AppError('回放成交额代码、日期或重复记录异常。')
            amounts[code][stamp]=dec(r['amount'])*1000
        # The account starts empty: known pre-start record dates cannot confer an entitlement.
        relevant=[r for r in dossiers[code]['data']['dividend'] if not r.get('record_date') or r['record_date']>=day]
        dividends[code]=implemented_dividends(relevant,end)
        progress(f'历史行情与分红 {i}/20',flush=True)
    data={'universe_as_of':day,'sessions':calendar,'bars':bars,'industries':{s['symbol']:s['industry'] for s in chosen},'dividends':dividends}
    # Raw cache + protocol preserve source parameters; derived file uses strings for exact monetary values.
    serial=json.loads(json.dumps(data,default=str))
    atomic_json(path/'market.json',serial)
    atomic_json(path/'financials.json',dossiers)
    atomic_json(path/'names.json',names)
    atomic_json(path/'valuation-history.json',history)
    atomic_json(path/'amounts.json',json.loads(json.dumps(amounts,default=str)))
    progress('历史输入已缓存，准备按各月可见公告生成信号',flush=True)
    return path,client,cohort,serial,dossiers,names,history,amounts


def experiment_signals(client, cohort, data, dossiers, names, history, amounts, entry_yield):
    sessions=data['sessions']; end=sessions[-1]
    extended=client.calendar('20221101',end)
    days=[d for i,d in enumerate(sessions[:-1]) if i==0 or d[:6]!=sessions[i+1][:6]]
    history_maps={}
    for code,rows in history.items():
        seen={}
        for r in rows:
            stamp=date_key(r['trade_date'])
            if r['ts_code']!=code or stamp in seen or not '20200101'<=stamp<=end:raise AppError('回放估值代码或日期异常。')
            seen[stamp]=r
        history_maps[code]=seen
    rules={**load_rules(),'entry_yield':entry_yield}
    signals={}; audits=[]
    for day in days:
        candidates=[]; freeze=[]; assessed=[]
        for stock in cohort['stocks']:
            code=stock['symbol']; q=history_maps[code].get(day,{})
            name=historical_name(names[code],day)
            row=summarize({**dossiers[code],'as_of':day},{'name':name or '身份待核实','industry':stock['industry']},q)
            past_start=(datetime.strptime(day,'%Y%m%d')-timedelta(days=365*3)).strftime('%Y%m%d')
            pe=positive(q.get('pe_ttm'))
            values=[positive(r.get('pe_ttm')) for stamp,r in history_maps[code].items() if past_start<=stamp<day]
            values=[v for v in values if v is not None]
            percentile=(sum(v<pe for v in values)+Decimal('.5')*sum(v==pe for v in values))/len(values) if pe and len(values)>=250 else None
            # Initial diagnostic uses absolute PE≤25 + own-history percentile; no pretend historical industry data.
            valuation={'state':'待核实' if percentile is None else '通过' if pe<=25 and percentile<=Decimal('.5') else '未通过',
                       'reason':'历史诊断：正PE≤25且自身三年正PE分位≤50%，同行业历史分类未取得','percentile':str(percentile) if percentile is not None else None}
            liq=liquidity(amounts[code],[s for s in extended if s<=day][-20:],rules)
            row['selection']={'valuation':valuation,'liquidity':liq,'coarse_pass':bool(pe and positive(q.get('dv_ttm')) and dec(q['dv_ttm'])>=3)}
            a=assess(row,{**dossiers[code],'as_of':day},rules)
            if not name:a.update(state='待数据核实',reason='当时名称不可核实')
            assessed.append({'symbol':code,'state':a['state'],'reason':a['reason'],'evidence_hash':a.get('evidence_hash')})
            if a['state']=='待数据核实':freeze.append(code)
            if a['state']=='满足价格条件':candidates.append({'symbol':code,'reference_price':a['reference_price'],'known_on':day,
                                                          'reason':a['reason'],'estimated_yield':a['estimated_yield']})
        candidates.sort(key=lambda a:(-dec(a['estimated_yield']),a['symbol']))
        signals[day]={'candidates':candidates,'freeze':freeze}
        audits.append({'day':day,'assessments':assessed})
    return signals,audits


def run_experiment(workspace, end, cfg, progress=print):
    from .backtest import simulate, metrics
    path,client,cohort,data,dossiers,names,history,amounts=prepare_experiment(workspace,end,progress,cfg)
    runs=[]
    for rate in EXPERIMENT['entry_yields']:
        signals,audits=experiment_signals(client,cohort,data,dossiers,names,history,amounts,rate)
        name='yield-'+rate.replace('.','')
        atomic_json(path/(name+'-signals.json'),{'signals':signals,'audit':audits})
        try:
            result=simulate(data,signals,cfg)
            curve=result['equity_curve']
            development=[p for p in curve if p['day']<=EXPERIMENT['development_end']]
            validation=[development[-1]]+[p for p in curve if p['day']>=EXPERIMENT['validation_start']]
            result['development_metrics']=metrics(development,[t for t in result['trades'] if t['trade_date']<=EXPERIMENT['development_end']],result['initial_cash'])
            result['validation_metrics']=metrics(validation,[t for t in result['trades'] if t['trade_date']>=EXPERIMENT['validation_start']],validation[0]['equity'])
            atomic_json(path/(name+'-result.json'),result)
            for key,label in (('metrics','全期'),('development_metrics','2023—2024开发段'),('validation_metrics','2025起留出段')):
                runs.append({'label':f'{float(rate):.0%}股息门槛 · {label}','metrics':result[key],
                             'status':'diagnostic','artifact':str(path/(name+'-result.json'))})
        except AppError as exc:
            runs.append({'label':f'税前股息门槛 {float(rate):.0%}','status':'blocked','message':str(exc),'metrics':{}})
        progress(f'历史回放 {rate}：{runs[-1]["status"]}',flush=True)
    output={'status':'diagnostic','title':'独立历史20股池 · 原始价格成交回放','as_of':end,'protocol':EXPERIMENT,'runs':runs,
        'limitations':['当前仅为历史诊断，不能据此宣布年化10%达成。',
                       '名单按2022年末指标重建，未使用今天研究池；但原始历史财报版本未逐份公告存档核验，仍存在修订数据风险。',
                       '未取得历史行业分类；历史诊断用绝对PE与自身历史分位，行业仓位标签暂用当前元数据。',
                       '本次运行前登记2023—2024开发段、2025起留出段；全部4%/5%/6%结果公开，不按留出段结果宣称挑中了有效参数。',
                       '当前为税前现金分红与保守费用；送转股或持仓估值缺失会阻断，不删除异常股票来美化收益。']}
    atomic_json(workspace/'backtests'/'latest.json',output)
    return output
