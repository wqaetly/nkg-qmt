"""Historical cohort and reproducible diagnostics; never optimize against a return target."""
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from collections import Counter

from .automatic import assess
from .config import AppError, atomic_json, date_key, dec, now_cn, load_config
from .market import Tushare
from .research import implemented_dividends, summarize, statements, DISCLOSURES
from .selection import load_rules, mainboard, positive, liquidity
from .service import read_json
from .industry import industry_at, load_industry_history


EXPERIMENT = {'version':'historical-dividend-v7','selection_day':'20221230','development_end':'20241231',
              'validation_start':'20250101','pool_size':20,'entry_yields':['0.04','0.05','0.06'],
              'primary_entry_yield':'0.05','target_cagr':'0.10','benchmark':'515180.SH'}
LONG_EXPERIMENT = {**EXPERIMENT,'version':'historical-dividend-v7-long','selection_day':'20141231',
                   'development_end':'20191231','validation_start':'20200101','benchmark':'510880.SH'}


def history_bounds(experiment):
    day=experiment['selection_day']
    year=int(day[:4])
    return year-5, f'{year-2}0101', (datetime.strptime(day,'%Y%m%d')-timedelta(days=60)).strftime('%Y%m%d')


def historical_input_issues(codes, start, end):
    return {item['symbol']:'历史行业：'+item['reason'] for item in DISCLOSURES.get('industry_conflicts',[])
            if item['symbol'] in codes and start<=item['date']<=end}


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


def protocol(cfg, experiment=EXPERIMENT, end=None):
    code_hash=hashlib.sha256()
    for name in ('experiment.py','backtest.py','research.py','automatic.py','ledger.py','selection.py','industry.py','comparison.py','market.py','disclosures.json'):
        code_hash.update((Path(__file__).parent/name).read_bytes())
    return {**experiment,'replay_end':end,'rules':load_rules(),'account_config':cfg,'engine_sha256':code_hash.hexdigest()}


def prepare_experiment(workspace, end, progress=print, cfg=None, experiment=EXPERIMENT):
    path=workspace/'backtests'/experiment['version']
    path.mkdir(parents=True,exist_ok=True)
    manifest=path/'protocol.json'
    registered=protocol(cfg or load_config(),experiment,end)
    if manifest.exists() and read_json(manifest)!=registered:
        raise AppError('预先登记的回测协议已变化；请建立新实验版本。')
    if not manifest.exists():
        atomic_json(manifest,registered)
        atomic_json(path/'registration.json',{'registered_at':now_cn().isoformat(),'note':'本次历史实验运行前登记，不是声称过去已实盘制定此策略。'})
    client=Tushare(workspace/'backtests'/'source-cache')
    day=experiment['selection_day']
    financial_year,valuation_start,liquidity_start=history_bounds(experiment)
    if end<=experiment['validation_start'] or end>now_cn().strftime('%Y%m%d'):
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
        income=statements(client.yearly('income',code,day[:4]+'0101',day,
                          'ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,revenue,n_income_attr_p,basic_eps'),day)
        if income and str(income[max(income)].get('comp_type')) in {'2','3','4','7'}:
            rejected.append({'symbol':code,'reason':'当时利润表为金融企业，模型不适用'});continue
        dossier=client.dossier(code,end,start_year=financial_year)
        row=summarize({**dossier,'as_of':day},{**basics[code],'name':name},quotes[code])
        if row.get('financial_company') or row['status']=='排除' or len(row['annual'])<5:
            rejected.append({'symbol':code,'reason':'当时金融模型不适用、亏损或历史不足'});continue
        chosen.append({'symbol':code,'name':name,'industry':basics[code].get('industry') or '未知行业',
                       'selection_quote':quotes[code],'selection_financial_period':row.get('latest_period')})
        dossiers[code]=dossier; names[code]=name_rows
        progress(f'历史研究池 {len(chosen)}/20：{code} {name}（按 {day} 可用指标）',flush=True)
        if len(chosen)==20:break
    if len(chosen)!=20:raise AppError('历史研究池不足20只，未用当前股票补齐。')
    industries,peers=load_industry_history(client,[s['symbol'] for s in chosen],progress)
    for stock in chosen:
        initial=industry_at(industries[stock['symbol']],day)
        if initial is None:raise AppError(f"{stock['symbol']} 入池时行业未知或冲突，未以当前标签替代。")
        stock['industry']=initial['label']
    atomic_json(path/'industry-history.json',{'stocks':industries,'peers':peers})
    cohort={'selected_on':day,'stocks':chosen,'rejected':rejected,
            'method':'当时上市（含后来退市），20日流动性，正PE≤25、历史股息率≥3%；按当时股息率降序、PE升序选20只非金融且至少五年财报的公司。'}
    old_cohort=path/'cohort.json'
    if old_cohort.exists() and read_json(old_cohort)!=cohort:
        raise AppError('历史名单发生修订，不能覆盖旧实验。')
    atomic_json(old_cohort,cohort)
    calendar=client.calendar(day,end)
    if calendar[0]!=day or calendar[-1]!=end:raise AppError('回放起止日必须是交易日。')
    bars={}; history={}; dividends={}
    input_errors=historical_input_issues([s['symbol'] for s in chosen],day,end)
    for i,stock in enumerate(chosen,1):
        code=stock['symbol']
        bars[code]=client.bars(code,day,end)
        history[code]=client.yearly('daily_basic',code,valuation_start,end,'ts_code,trade_date,close,total_share,pe_ttm,pb,dv_ttm')
        rows=client.yearly('daily',code,liquidity_start,end,'ts_code,trade_date,amount')
        amounts[code]={}
        for r in rows:
            stamp=date_key(r['trade_date'])
            if r['ts_code']!=code or not liquidity_start<=stamp<=end or stamp in amounts[code]:raise AppError('回放成交额代码、日期或重复记录异常。')
            amounts[code][stamp]=dec(r['amount'])*1000
        # The account starts empty: known pre-start record dates cannot confer an entitlement.
        relevant=[r for r in dossiers[code]['data']['dividend'] if not r.get('record_date') or r['record_date']>=day]
        try:
            dividends[code]=implemented_dividends(relevant,end,earliest_period='19000101')
        except AppError as exc:
            input_errors[code]='；'.join(filter(None,[input_errors.get(code),str(exc)]))
            dividends[code]=[]
        progress(f'历史行情与分红 {i}/20',flush=True)
    data={'universe_as_of':day,'sessions':calendar,'bars':bars,'industries':{s['symbol']:s['industry'] for s in chosen},'dividends':dividends,'errors':input_errors}
    # Raw cache + protocol preserve source parameters; derived file uses strings for exact monetary values.
    serial=json.loads(json.dumps(data,default=str))
    atomic_json(path/'market.json',serial)
    atomic_json(path/'financials.json',dossiers)
    atomic_json(path/'names.json',names)
    atomic_json(path/'valuation-history.json',history)
    atomic_json(path/'amounts.json',json.loads(json.dumps(amounts,default=str)))
    progress('历史输入已缓存，准备按各月可见公告生成信号',flush=True)
    return path,client,cohort,serial,dossiers,names,history,amounts,industries,peers


def experiment_signals(client, cohort, data, dossiers, names, history, amounts, entry_yield, industries, peers, progress=print, experiment=EXPERIMENT):
    sessions=data['sessions']; end=sessions[-1]
    _,valuation_start,liquidity_start=history_bounds(experiment)
    extended=client.calendar(liquidity_start,end)
    days=[d for i,d in enumerate(sessions[:-1]) if i==0 or d[:6]!=sessions[i+1][:6]]
    history_maps={}
    for code,rows in history.items():
        seen={}
        for r in rows:
            stamp=date_key(r['trade_date'])
            if r['ts_code']!=code or stamp in seen or not valuation_start<=stamp<=end:raise AppError('回放估值代码或日期异常。')
            seen[stamp]=r
        history_maps[code]=seen
    rules={**load_rules(),'entry_yield':entry_yield}
    signals={}; audits=[]
    for index,day in enumerate(days,1):
        candidates=[]; freeze=[]; assessed=[]
        market_quotes=client.prices(day)
        sector_pe={}
        for code,membership in peers.items():
            sector=industry_at(membership,day)
            pe=positive(market_quotes.get(code,{}).get('pe_ttm'))
            if sector and pe:sector_pe.setdefault(sector['code'],[]).append(pe)
        for stock in cohort['stocks']:
            code=stock['symbol']; q=history_maps[code].get(day,{})
            name=historical_name(names[code],day)
            row=summarize({**dossiers[code],'as_of':day},{'name':name or '身份待核实','industry':stock['industry']},q)
            past_start=(datetime.strptime(day,'%Y%m%d')-timedelta(days=365*3)).strftime('%Y%m%d')
            pe=positive(q.get('pe_ttm'))
            values=[positive(r.get('pe_ttm')) for stamp,r in history_maps[code].items() if past_start<=stamp<day]
            values=[v for v in values if v is not None]
            percentile=(sum(v<pe for v in values)+Decimal('.5')*sum(v==pe for v in values))/len(values) if pe and len(values)>=250 else None
            sector=industry_at(industries[code],day)
            peers_pe=sector_pe.get(sector['code'],[]) if sector else []
            enough=percentile is not None and len(peers_pe)>=5
            valuation={'state':'待核实' if not enough else '通过' if pe<=25 and pe<=median(peers_pe) and percentile<=Decimal('.5') else '未通过',
                       'reason':'正PE≤25、不高于当时申万一级行业正PE中位数，且自身三年正PE分位≤50%；历史成员按纳入/剔除日期匹配',
                       'percentile':str(percentile) if percentile is not None else None,'peer_count':len(peers_pe),
                       'industry_median_pe':str(median(peers_pe)) if peers_pe else None}
            liq=liquidity(amounts[code],[s for s in extended if s<=day][-20:],rules)
            row['selection']={'valuation':valuation,'liquidity':liq,'coarse_pass':bool(pe and positive(q.get('dv_ttm')) and dec(q['dv_ttm'])>=3)}
            a=assess(row,{**dossiers[code],'as_of':day},rules)
            if not name:a.update(state='待数据核实',reason='当时名称不可核实')
            if not sector or sector['label']!=stock['industry']:
                a.update(state='待数据核实',reason='入池后行业变更或区间未知；保留持仓并停止新增，须重核行业风险')
            assessed.append({'symbol':code,'state':a['state'],'reason':a['reason'],'evidence_hash':a.get('evidence_hash')})
            if a['state']=='待数据核实':freeze.append(code)
            if a['state']=='满足价格条件':candidates.append({'symbol':code,'reference_price':a['reference_price'],'known_on':day,
                                                          'reason':a['reason'],'estimated_yield':a['estimated_yield']})
        candidates.sort(key=lambda a:(-dec(a['estimated_yield']),a['symbol']))
        signals[day]={'candidates':candidates,'freeze':freeze}
        audits.append({'day':day,'assessments':assessed})
        if index%10==0 or index==len(days):progress(f'{entry_yield} 规则历史评估 {index}/{len(days)} 月',flush=True)
    return signals,audits


def run_experiment(workspace, end, cfg, progress=print, experiment=EXPERIMENT):
    from .backtest import simulate, metrics, period_diagnostics
    from .comparison import build_comparisons
    path,client,cohort,data,dossiers,names,history,amounts,industries,peers=prepare_experiment(workspace,end,progress,cfg,experiment)
    runs=[]; comparison_series=[]; diagnostics={}; periods={}
    for rate in experiment['entry_yields']:
        signals,audits=experiment_signals(client,cohort,data,dossiers,names,history,amounts,rate,industries,peers,progress,experiment)
        name='yield-'+rate.replace('.','')
        atomic_json(path/(name+'-signals.json'),{'signals':signals,'audit':audits})
        try:
            result=simulate(data,signals,cfg,initial_cash=cfg['paper_cash'],max_holdings=load_rules()['max_holdings'])
            if rate==experiment['primary_entry_yield']:
                periods={'label':'5%门槛主方案（含估计成本）',**period_diagnostics(result['equity_curve'],result['trades'])}
                comparison_series.append({'label':'5%门槛主方案（含估计成本）','kind':'strategy','points':[
                    {'day':p['day'],'value':float(dec(p['equity'])/dec(result['initial_cash']))} for p in result['equity_curve']]})
                assessments=[a for m in audits for a in m['assessments']]
                diagnostics={'evaluations':len(assessments),'states':dict(Counter(a['state'] for a in assessments)),
                             'reasons':[{'reason':reason,'count':count} for reason,count in Counter(a['reason'] for a in assessments if a['state']!='满足价格条件').most_common()],
                             'skips':[{'reason':reason,'count':count} for reason,count in Counter(s['reason'] for s in result['skipped']).most_common()]}
            curve=result['equity_curve']
            development=[p for p in curve if p['day']<=experiment['development_end']]
            validation=[development[-1]]+[p for p in curve if p['day']>=experiment['validation_start']]
            result['development_metrics']=metrics(development,[t for t in result['trades'] if t['trade_date']<=experiment['development_end']],result['initial_cash'])
            result['validation_metrics']=metrics(validation,[t for t in result['trades'] if t['trade_date']>=experiment['validation_start']],validation[0]['equity'])
            atomic_json(path/(name+'-result.json'),result)
            for key,label in (('metrics','全期'),('development_metrics',f"截至{experiment['development_end'][:4]}开发段"),('validation_metrics',f"{experiment['validation_start'][:4]}起留出段")):
                runs.append({'label':f'{float(rate):.0%}股息门槛 · {label}','metrics':result[key],
                             'status':'diagnostic','artifact':str(path/(name+'-result.json'))})
        except AppError as exc:
            runs.append({'label':f'税前股息门槛 {float(rate):.0%}','status':'blocked','message':str(exc),'metrics':{}})
        progress(f'历史回放 {rate}：{runs[-1]["status"]}',flush=True)
    comparisons=build_comparisons(client,data['sessions'],cfg['paper_cash'],symbol=experiment['benchmark'])
    for item in comparisons:
        runs.append({'label':item['label'],'status':item['status'],'metrics':item['metrics'],'message':'；'.join(filter(None,[item.get('message'),*item['limitations']]))})
        if item['series']:
            comparison_series.append({'label':item['label'],'kind':'cash' if item.get('symbol') is None and '现金' in item['label'] else 'benchmark','points':item['series']})
    coverage={'start':data['sessions'][0],'end':end,'sessions':len(data['sessions']),'stocks':len(cohort['stocks']),
              'price_rows':sum(len(rows) for rows in data['bars'].values()),
              'valuation_rows':sum(len(rows) for rows in history.values()),'input_errors':data.get('errors',{})}
    output={'status':'diagnostic' if periods else 'blocked','title':'独立历史20股池 · 原始价格成交回放','as_of':end,'protocol':experiment,'runs':runs,
        'coverage':coverage,'message':f"区间 {data['sessions'][0]}—{end}，{len(data['sessions'])} 个交易日、20只历史建池股票。输入疑点 {len(data.get('errors',{}))} 只。",
        'comparison_series':comparison_series,'diagnostics':diagnostics,'period_diagnostics':periods,
        'limitations':['当前仅为历史诊断，不能据此宣布年化10%达成。',
                       f"名单按{experiment['selection_day']}指标重建，未使用今天研究池；但原始历史财报版本未逐份公告存档核验，仍存在修订数据风险。",
                       '行业使用申万一级历史纳入/剔除区间，缺失/冲突不取当前标签；入池后行业变更会冻结交易，供应商历史分类修订仍需原始存档验证。',
                       f"本次运行前登记开发段截至{experiment['development_end']}、留出段自{experiment['validation_start']}；全部4%/5%/6%结果公开。此前已查看过部分历史结果，此留出段不是真正未见数据；年度和滚动结果是稳定性诊断，不是滚动重选训练的样本外验证。",
                       '当前为税前现金分红与保守费用；送转股或持仓估值缺失会阻断，不删除异常股票来美化收益。']}
    atomic_json(path/'summary.json',output)
    atomic_json(workspace/'backtests'/'latest.json',output)
    return output
