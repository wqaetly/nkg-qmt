"""Historical cohort and reproducible diagnostics; never optimize against a return target."""
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from statistics import median
from collections import Counter

from .automatic import assess, dividend_assessment
from .config import AppError, atomic_json, date_key, dec, now_cn, load_config
from .market import Tushare
from .research import implemented_dividends, summarize, statements, DISCLOSURES
from .selection import load_rules, mainboard, positive, liquidity
from .service import read_json
from .industry import industry_at, load_industry_history


EXPERIMENT = {'version':'historical-dividend-v24','selection_day':'20221230','development_end':'20241231',
              'validation_start':'20250101','pool_size':20,'entry_yields':['0.04','0.05','0.06'],
              'primary_entry_yield':'0.05','target_cagr':'0.10','benchmark':'515180.SH','earnings_basis':'conservative_minimum',
              'freeze_policy':'portfolio'}
LONG_EXPERIMENT = {**EXPERIMENT,'version':'historical-dividend-v24-long','selection_day':'20141231',
                   'development_end':'20191231','validation_start':'20200101','benchmark':'510880.SH'}
QUALITY_EXPERIMENT = {**EXPERIMENT,'version':'historical-dividend-v24-quality','quality_first':True}
QUALITY_LONG_EXPERIMENT = {**LONG_EXPERIMENT,'version':'historical-dividend-v24-quality-long','quality_first':True}
ANNUAL_EXPERIMENT = {**QUALITY_EXPERIMENT,'version':'historical-dividend-v24-annual','pool_refresh':'year_end',
    'pool_exit':'退出名单的资料待核实持仓继续冻结跟踪；其他退出名单股票按下一交易日限价退出。行业沿用首次归属，变更冻结。',
    'monthly_targets':'沿用原回放月末重配：除冻结持仓外，仅当月仍满足全部买入条件者保留目标；不宣称只在3%股息率以下退出。'}
ANNUAL_LONG_EXPERIMENT = {**ANNUAL_EXPERIMENT,'version':'historical-dividend-v24-annual-long',
    'selection_day':LONG_EXPERIMENT['selection_day'],'development_end':LONG_EXPERIMENT['development_end'],
    'validation_start':LONG_EXPERIMENT['validation_start'],'benchmark':LONG_EXPERIMENT['benchmark']}


HOLD_EXPERIMENT = {**ANNUAL_EXPERIMENT,'version':'historical-dividend-v24-annual-hold','retention_yield':'0.03',
    'monthly_targets':'月末检查：当期成员财务已核实、流动性通过且当日情景股息率至少3%可保留；估值及历史股息入场条件不单独触发卖出。仅持有资格不允许加仓；仍满足完整入场条件者沿用原重配。保留股先占额度，按原月度单股预算与行业额度整手修剪，资料待核实仍冻结；不是每日风险退出。'}
HOLD_LONG_EXPERIMENT = {**ANNUAL_LONG_EXPERIMENT,'version':'historical-dividend-v24-annual-long-hold',
                       'retention_yield':HOLD_EXPERIMENT['retention_yield'],'monthly_targets':HOLD_EXPERIMENT['monthly_targets']}


def cohort_assessment(row, dossier, rules, *, earnings_basis='conservative_minimum'):
    """Reuse financial gates before ranking; price is evaluated later at each signal."""
    result=dividend_assessment(row,dossier,rules,earnings_basis=earnings_basis)
    return {'eligible':result['state'] in ('满足价格条件','等待价格'),
            'state':result['state'],'reason':result['reason'],'evidence_hash':result['evidence_hash']}


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
    for name in ('experiment.py','backtest.py','research.py','automatic.py','ledger.py','selection.py','industry.py','comparison.py','market.py','config.py','service.py','disclosures.json'):
        code_hash.update((Path(__file__).parent/name).read_bytes())
    code_hash.update((Path(__file__).parent.parent/'vendor/dividend_stock_research/dividend_price.py').read_bytes())
    return {**experiment,'replay_end':end,'rules':load_rules(),'account_config':cfg,'engine_sha256':code_hash.hexdigest()}


def validate_experiment(experiment):
    if experiment.get('earnings_basis','conservative_minimum') not in ('conservative_minimum','ttm'):
        raise AppError('未知实验盈利口径，未登记或回退为默认规则。')
    mode=experiment.get('entry_mode','yield_threshold')
    freeze_policy=experiment.get('freeze_policy','portfolio')
    if freeze_policy not in ('portfolio','position'):
        raise AppError('未知冻结范围，未回退为默认规则。')
    if freeze_policy=='position' and mode!='yield_rank':
        raise AppError('按股票冻结对照仅与完整年度候选的股息排序实验同用。')
    if mode not in ('yield_threshold','yield_rank'):
        raise AppError('未知实验入场方式，未登记或回退为股息门槛。')
    if mode=='yield_rank' and (experiment.get('pool_refresh')!='year_end' or not experiment.get('quality_first')
            or experiment.get('cohort_limit',experiment['pool_size']) is not None
            or experiment.get('retention_yield') is not None
            or experiment['entry_yields']!=['0.05'] or experiment['primary_entry_yield']!='0.05'):
        raise AppError('股息排序实验须使用完整年度财务候选且不叠加持有缓冲；只计算单个5%参考情景，不作为入场门槛。')
    if experiment.get('retention_yield') is not None and (experiment.get('pool_refresh')!='year_end'
            or not 0<dec(experiment['retention_yield'])<min(dec(rate) for rate in experiment['entry_yields'])):
        raise AppError('持有缓冲实验须使用年度池，且持有门槛为正并低于全部入场门槛。')


def register_experiment(workspace, end, cfg, experiment):
    validate_experiment(experiment)
    path=workspace/'backtests'/experiment['version']
    path.mkdir(parents=True,exist_ok=True)
    manifest=path/'protocol.json'
    registered=protocol(cfg or load_config(),experiment,end)
    if manifest.exists() and read_json(manifest)!=registered:
        raise AppError('预先登记的回测协议已变化；请建立新实验版本。')
    if not manifest.exists():
        atomic_json(manifest,registered)
        atomic_json(path/'registration.json',{'registered_at':now_cn().isoformat(),'note':'本次历史实验运行前登记，不是声称过去已实盘制定此策略。'})
    return path


def prepare_experiment(workspace, end, progress=print, cfg=None, experiment=EXPERIMENT):
    path=register_experiment(workspace,end,cfg,experiment)
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
    quality_first=experiment.get('quality_first',False)
    target=experiment.get('cohort_limit',experiment['pool_size'])
    if target is None and not quality_first:
        raise AppError('全候选实验必须先检查财务门槛。')
    for candidate_index,code in enumerate(candidates,1):
        if quality_first and candidate_index%10==0:
            progress(f'财务建池检查 {day}：{candidate_index}/{len(candidates)}，已通过 {len(chosen)} 只',flush=True)
        name_rows=client.call('namechange',{'ts_code':code},'ts_code,name,start_date,end_date,ann_date')
        if any(r['ts_code']!=code for r in name_rows):raise AppError('历史名称代码不符。')
        name=historical_name(name_rows,day)
        if not name or 'ST' in name or '退' in name:
            rejected.append({'symbol':code,'reason':'历史名称缺失或当时有风险警示'});continue
        try:
            income=statements(client.yearly('income',code,day[:4]+'0101',day,
                              'ts_code,ann_date,f_ann_date,end_date,report_type,comp_type,revenue,n_income_attr_p,basic_eps'),day)
        except AppError as exc:
            if not quality_first:raise
            rejected.append({'symbol':code,'state':'待数据核实','reason':str(exc)});continue
        if income and str(income[max(income)].get('comp_type')) in {'2','3','4','7'}:
            rejected.append({'symbol':code,'reason':'当时利润表为金融企业，模型不适用'});continue
        dossier=client.dossier(code,day if quality_first else end,start_year=financial_year)
        row=summarize({**dossier,'as_of':day},{**basics[code],'name':name},quotes[code])
        quality={}
        if quality_first:
            quality=cohort_assessment(row,dossier,rules,earnings_basis=experiment.get('earnings_basis','conservative_minimum'))
            if not quality['eligible']:
                rejected.append({'symbol':code,**quality});continue
        elif row.get('financial_company') or row['status']=='排除' or len(row['annual'])<5:
            rejected.append({'symbol':code,'reason':'当时金融模型不适用、亏损或历史不足'});continue
        chosen.append({'symbol':code,'name':name,'industry':basics[code].get('industry') or '未知行业',
                       'selection_quote':quotes[code],'selection_financial_period':row.get('latest_period'),
                       **({'selection_quality':quality} if quality_first else {})})
        dossiers[code]=dossier; names[code]=name_rows
        progress(f'历史研究池 {len(chosen)}/{target if target is not None else "不限候选数"}：{code} {name}（按 {day} 可用指标）',flush=True)
        if target is not None and len(chosen)==target:break
    if len(chosen)!=target and not quality_first:raise AppError('历史研究池不足20只，未用当前股票补齐。')
    industries,peers=load_industry_history(client,[s['symbol'] for s in chosen],progress)
    for stock in chosen:
        initial=industry_at(industries[stock['symbol']],day)
        if initial is None:raise AppError(f"{stock['symbol']} 入池时行业未知或冲突，未以当前标签替代。")
        stock['industry']=initial['label']
    atomic_json(path/'industry-history.json',{'stocks':industries,'peers':peers})
    cohort={'selected_on':day,'stocks':chosen,'rejected':rejected,
            'method':'当时上市（含后来退市），20日流动性，正PE≤25、历史股息率≥3%；按当时股息率降序、PE升序选20只非金融且至少五年财报的公司。'}
    if quality_first:
        cohort.update(candidate_count=len(candidates),evaluated_count=len(chosen)+len(rejected),target_count=target,
            method='先通过既有五年连续盈利/现金分红、审计、三年现金覆盖和债务财务门槛，再按当时股息率降序、PE升序选最多20只；不要求建池时满足5%买价，不足不补齐。')
        if target is None:
            cohort.update(holding_limit=rules['max_holdings'],
                method='检查全部年末初筛候选，保留全部财务合格者；月末再检查完整入场条件并排序，由资金、行业与整手约束选最多20只持仓，不提前截断候选。')
    old_cohort=path/'cohort.json'
    if old_cohort.exists() and read_json(old_cohort)!=cohort:
        raise AppError('历史名单发生修订，不能覆盖旧实验。')
    atomic_json(old_cohort,cohort)
    calendar=client.calendar(day,end)
    if calendar[0]!=day or calendar[-1]!=end:raise AppError('回放起止日必须是交易日。')
    bars={}; history={}; dividends={}
    input_errors=historical_input_issues([s['symbol'] for s in chosen],day,end)
    if not chosen:input_errors['cohort']='建池日无可核实且通过财务门槛的候选，未把空名单当作成功回测。'
    for i,stock in enumerate(chosen,1):
        code=stock['symbol']
        if quality_first:dossiers[code]=client.dossier(code,end,start_year=financial_year)
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
        progress(f'历史行情与分红 {i}/{len(chosen)}',flush=True)
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


def prepare_annual_experiment(workspace, end, progress=print, cfg=None, experiment=ANNUAL_EXPERIMENT):
    path=register_experiment(workspace,end,cfg,experiment)
    if end<=experiment['validation_start'] or end>now_cn().strftime('%Y%m%d'):
        raise AppError('回放截止日期须晚于验证开始且不晚于今天。')
    client=Tushare(workspace/'backtests'/'source-cache')
    sessions=client.calendar(experiment['selection_day'],end)
    if len(sessions)<2 or sessions[0]!=experiment['selection_day'] or sessions[-1]!=end:
        raise AppError('年度回放起止日必须是交易日，且至少两个交易日。')
    # A year-end close can change the next session's targets, never the same close's fills.
    dates=[sessions[0]]+[day for i,day in enumerate(sessions[:-1])
                           if day!=sessions[0] and day[:4]!=sessions[i+1][:4]]
    data={'universe_as_of':sessions[0],'sessions':sessions,'bars':{},'industries':{},'dividends':{},'errors':{},'universe_membership':{}}
    dossiers={};names={};history={};amounts={};industries={};peers={};stocks={};rebalances=[]
    for i,day in enumerate(dates):
        child={**experiment,'version':experiment['version']+'/cohorts/'+day,'selection_day':day}
        prepared=prepare_experiment(workspace,end,progress,cfg,child)
        _,_,cohort,market,fin,aliases,valuation,turnover,sectors,peer_rows=prepared
        until=dates[i+1] if i+1<len(dates) else None
        rebalances.append({'day':day,'until':until,'symbols':[s['symbol'] for s in cohort['stocks']],
                           'candidate_count':cohort['candidate_count'],'rejected':cohort['rejected'],
                           **({'evaluated_count':cohort['evaluated_count']} if 'evaluated_count' in cohort else {})})
        for stock in cohort['stocks']:
            code=stock['symbol']
            data['universe_membership'].setdefault(code,[]).append({'start':day,'end':until})
            if code not in stocks:
                stocks[code]=stock
                for dest,source in ((data['bars'],market['bars']),(data['industries'],market['industries']),
                                    (data['dividends'],market['dividends']),(dossiers,fin),(names,aliases),
                                    (history,valuation),(amounts,turnover),(industries,sectors)):
                    dest[code]=source[code]
        # A zero-candidate year is an empty active set; prior holdings still need tracking.
        data['errors'].update({code:reason for code,reason in market['errors'].items() if code!='cohort'})
        for code,rows in peer_rows.items():
            existing=peers.setdefault(code,[])
            existing.extend(row for row in rows if row not in existing)
        progress(f'年度研究池 {day}：{len(cohort["stocks"])} 只，累计涉及 {len(stocks)} 只；账本不重置',flush=True)
    if not stocks:data['errors']['cohort']='全部建池期均无可核实合格候选，未生成成功策略收益。'
    cohort={'selected_on':sessions[0],'stocks':list(stocks.values()),'rebalances':rebalances,'target_count':experiment.get('cohort_limit',experiment['pool_size']),
            'method':'每年末以当时公告重选最多20只；股票从首次入池到回放结束持续跟踪，使用同一连续模拟账户。'}
    if cohort['target_count'] is None:
        cohort.update(holding_limit=load_rules()['max_holdings'],
            method='每年末检查全部初筛候选并保留财务合格者；月末完成入场筛选后由资金与行业约束分配最多20只持仓，账户不重置。年中不额外扫描全市场。')
    for filename,content in (('cohort',cohort),('market',data),('financials',dossiers),('names',names),
                             ('valuation-history',history),('amounts',amounts),('industry-history',{'stocks':industries,'peers':peers})):
        atomic_json(path/(filename+'.json'),json.loads(json.dumps(content,default=str)))
    return path,client,cohort,data,dossiers,names,history,amounts,industries,peers


def experiment_signals(client, cohort, data, dossiers, names, history, amounts, entry_yield, industries, peers, progress=print, experiment=EXPERIMENT):
    validate_experiment(experiment)
    if not cohort['stocks']:return {},[]
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
        candidates=[]; freeze=[]; assessed=[]; retain=[]; portfolio_freeze=[]
        market_quotes=client.prices(day)
        sector_pe={}
        for code,membership in peers.items():
            sector=industry_at(membership,day)
            pe=positive(market_quotes.get(code,{}).get('pe_ttm'))
            if sector and pe:sector_pe.setdefault(sector['code'],[]).append(pe)
        for stock in cohort['stocks']:
            code=stock['symbol']; q=history_maps[code].get(day,{})
            memberships=data.get('universe_membership',{}).get(code)
            if memberships and memberships[0]['start']>day:continue
            active=memberships is None or any(m['start']<=day and (m['end'] is None or day<m['end']) for m in memberships)
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
            a=assess(row,{**dossiers[code],'as_of':day},rules,earnings_basis=experiment.get('earnings_basis','conservative_minimum'))
            if not name:a.update(state='待数据核实',reason='当时名称不可核实')
            if not sector or sector['label']!=stock['industry']:
                a.update(state='待数据核实',reason='入池后行业变更或区间未知；保留持仓并停止新增，须重核行业风险')
            if not active and a['state']!='待数据核实':
                a.update(state='退出研究池',reason='年度研究池已退出，按下一交易日限价退出；未复用下一期名单到此前日期')
            rank=experiment.get('entry_mode')=='yield_rank' and a['state'] in ('满足价格条件','等待价格')
            if rank:
                # The 5% valuation reference is not an execution cap in the ranking experiment.
                a.update(state='排序候选',reason='财务、估值和流动性通过，按预计股息率排序；预计5%股息率不作为入场门槛')
                order_limit=str((dec(q['close'],positive=True)*Decimal('1.01')).quantize(Decimal('.01'),rounding=ROUND_FLOOR))
            holding_eligible=(experiment.get('retention_yield') is not None and active and a['state']!='待数据核实'
                and a['dimensions']['dividend']['state'] in ('满足价格条件','等待价格')
                and valuation['state']!='待核实' and a['dimensions']['liquidity']['state']=='通过' and a.get('estimated_yield') is not None
                and dec(a['estimated_yield'])>=dec(experiment['retention_yield']))
            if holding_eligible:retain.append(code)
            assessed.append({'symbol':code,'state':a['state'],'reason':a['reason'],'evidence_hash':a.get('evidence_hash'),
                             'active':active,'dimensions':a['dimensions'],'coarse_pass':row['selection']['coarse_pass'],
                             'estimated_yield':a.get('estimated_yield'),
                             **({key:a[key] for key in ('financial_gaps','zero_cash_periods','zero_cash_decisions') if key in a}),
                             **({'valuation_reference_price':a['reference_price'],'order_limit':order_limit} if rank else {}),
                             **({'data_warnings':row['data_warnings']} if row.get('data_warnings') else {}),
                             **({'holding_eligible':holding_eligible} if experiment.get('retention_yield') is not None else {})})
            if a['state']=='待数据核实':
                freeze.append(code)
                if not name or not sector or sector['label']!=stock['industry']:
                    portfolio_freeze.append(code)
            if a['state'] in ('满足价格条件','排序候选'):candidates.append({'symbol':code,'reference_price':order_limit if rank else a['reference_price'],'known_on':day,
                                                          'reason':a['reason'],'estimated_yield':a['estimated_yield']})
        candidates.sort(key=lambda a:(-dec(a['estimated_yield']),a['symbol']))
        signals[day]={'candidates':candidates,'freeze':freeze}
        if experiment.get('freeze_policy','portfolio')=='position':
            signals[day]['portfolio_freeze']=portfolio_freeze
        if experiment.get('retention_yield') is not None:signals[day]['retain']=retain
        audits.append({'day':day,'assessments':assessed})
        if index%10==0 or index==len(days):progress(f'{"股息排序" if experiment.get("entry_mode")=="yield_rank" else entry_yield} 规则历史评估 {index}/{len(days)} 月',flush=True)
    return signals,audits


def entry_diagnostics(audits, signals, entry_mode='yield_threshold'):
    active=[a for month in audits for a in month['assessments'] if a.get('active',True)]
    funnel=[{'stage':'当期候选','count':len(active)}]
    for stage,passed in (
        ('财务通过',lambda a:a.get('dimensions',{}).get('dividend',{}).get('state') in ('满足价格条件','等待价格')),
        ('估值通过',lambda a:a.get('dimensions',{}).get('valuation',{}).get('state')=='通过'),
        ('流动性通过',lambda a:a.get('dimensions',{}).get('liquidity',{}).get('state')=='通过'),
        ('基础候选通过',lambda a:a.get('coarse_pass',False)),
        ('排序候选' if entry_mode=='yield_rank' else '全部买入条件通过',lambda a:a['state'] in ('满足价格条件','排序候选'))):
        active=[a for a in active if passed(a)]
        funnel.append({'stage':stage,'count':len(active)})
    counts=[len(snapshot['candidates']) for snapshot in signals.values()]
    result={'entry_funnel':funnel,'candidate_months':{'months':len(counts),'empty':counts.count(0),'total_candidates':sum(counts)}}
    conflicts,active_conflicts=Counter(),Counter()
    zero_cash,financial_gaps={},{}
    for month in audits:
        for assessment in month['assessments']:
            for field,group in (('zero_cash_decisions',zero_cash),('financial_gaps',financial_gaps)):
                for item in assessment.get(field,[]):
                    key=(assessment['symbol'],item['period'],item.get('field'),item['available_on'])
                    entry=group.setdefault(key,{'symbol':assessment['symbol'],
                        **{k:item[k] for k in ('period','available_on','source','note','field','label') if k in item},'count':0})
                    entry['count']+=1
            for warning in assessment.get('data_warnings',[]):
                key=(assessment['symbol'],warning['period'],warning['field'])
                conflicts[key]+=1
                active_conflicts[key]+=int(assessment.get('active',True))
    if conflicts:
        result['unused_share_conflicts']=[{'symbol':code,'period':period,'field':field,'count':count,
                                          'active_count':active_conflicts[(code,period,field)]}
                                         for (code,period,field),count in sorted(conflicts.items())]
    if zero_cash:result['verified_zero_cash']=[zero_cash[key] for key in sorted(zero_cash)]
    if financial_gaps:result['financial_gaps']=[financial_gaps[key] for key in sorted(financial_gaps)]
    return result


def holding_freeze_diagnostics(result, signals, audits, data, policy):
    """Describe actual held freezes at decisions, not every unverified research name."""
    if policy not in ('portfolio','position') or result.get('assumptions',{}).get('freeze_policy')!=policy:
        raise AppError('实际冻结诊断须与完整回放的冻结范围一致。')
    curve={point['day']:point for point in result['equity_curve']}
    assessments={month['day']:{a['symbol']:a for a in month['assessments']} for month in audits}
    skips=Counter(item['day'] for item in result['skipped']
                  if item['reason']=='既有持仓资料待核实，保留冻结股数并暂停新增买入')
    if set(skips)-set(signals):
        raise AppError('实际买入跳过日期不在评估信号中，未生成冻结诊断。')
    output={'policy':policy,'months':len(signals),'frozen_months':0,'portfolio_blocked_months':0,
            'buy_skip_months':len(skips),'buy_skips':sum(skips.values()),'company_months':0,'latest':None,'rows':[]}
    for day,snapshot in sorted(signals.items()):
        point=curve[day]
        held={code:quantity for code,quantity in point['positions'].items() if quantity>0}
        frozen=sorted(set(snapshot['freeze'])&set(held))
        global_frozen=set(frozen) if policy=='portfolio' else set(frozen)&set(snapshot['portfolio_freeze'])
        if skips[day] and not global_frozen:
            raise AppError('实际买入跳过记录与组合冻结状态不一致。')
        output['frozen_months']+=bool(frozen)
        output['portfolio_blocked_months']+=bool(global_frozen)
        output['latest']={'day':day,'held_frozen':len(frozen),'portfolio_blocked':bool(global_frozen),'buy_skips':skips[day]}
        for code in frozen:
            assessment=assessments[day][code]
            if assessment['state']!='待数据核实':
                raise AppError('实际冻结持仓与当时研究状态不一致，未生成诊断。')
            value=dec(data['bars'][code][day]['close'],positive=True)*held[code]
            output['rows'].append({'day':day,'symbol':code,'quantity':held[code],'market_value':str(value),
                'weight':str(value/dec(point['equity'],positive=True)),'active':assessment['active'],
                'reason':assessment['reason'],'scope':'portfolio' if code in global_frozen else 'position'})
    output['company_months']=len(output['rows'])
    return output


def run_experiment(workspace, end, cfg, progress=print, experiment=EXPERIMENT):
    from .backtest import simulate, metrics, period_diagnostics
    from .comparison import build_comparisons
    validate_experiment(experiment)
    rank=experiment.get('entry_mode')=='yield_rank'
    primary_label='股息排序主方案（含估计成本）' if rank else '5%门槛主方案（含估计成本）'
    prepare=prepare_annual_experiment if experiment.get('pool_refresh')=='year_end' else prepare_experiment
    path,client,cohort,data,dossiers,names,history,amounts,industries,peers=prepare(workspace,end,progress,cfg,experiment)
    runs=[]; comparison_series=[]; diagnostics={}; periods={}; primary_metrics={}
    for rate in experiment['entry_yields']:
        signals,audits=experiment_signals(client,cohort,data,dossiers,names,history,amounts,rate,industries,peers,progress,experiment)
        name='yield-rank' if rank else 'yield-'+rate.replace('.','')
        label='股息排序' if rank else f'{float(rate):.0%}股息门槛'
        atomic_json(path/(name+'-signals.json'),{'signals':signals,'audit':audits})
        if rate==experiment['primary_entry_yield']:
            assessments=[a for m in audits for a in m['assessments']]
            diagnostics={'evaluations':len(assessments),'states':dict(Counter(a['state'] for a in assessments)),
                         'reasons':[{'reason':reason,'count':count} for reason,count in Counter(a['reason'] for a in assessments if a['state'] not in ('满足价格条件','排序候选')).most_common()],
                         'skips':[],**entry_diagnostics(audits,signals,experiment.get('entry_mode','yield_threshold'))}
        try:
            result=simulate(data,signals,cfg,initial_cash=cfg['paper_cash'],max_holdings=load_rules()['max_holdings'],
                            freeze_policy=experiment.get('freeze_policy','portfolio'))
            if rate==experiment['primary_entry_yield']:
                held_freezes=holding_freeze_diagnostics(result,signals,audits,data,experiment.get('freeze_policy','portfolio'))
                primary_metrics=result['metrics']
                periods={'label':primary_label,**period_diagnostics(result['equity_curve'],result['trades'])}
                comparison_series.append({'label':primary_label,'kind':'strategy','points':[
                    {'day':p['day'],'value':float(dec(p['equity'])/dec(result['initial_cash']))} for p in result['equity_curve']]})
                diagnostics['skips']=[{'reason':reason,'count':count} for reason,count in Counter(s['reason'] for s in result['skipped']).most_common()]
                diagnostics['holding_freezes']=held_freezes
            curve=result['equity_curve']
            development=[p for p in curve if p['day']<=experiment['development_end']]
            validation=[development[-1]]+[p for p in curve if p['day']>=experiment['validation_start']]
            result['development_metrics']=metrics(development,[t for t in result['trades'] if t['trade_date']<=experiment['development_end']],result['initial_cash'])
            result['validation_metrics']=metrics(validation,[t for t in result['trades'] if t['trade_date']>=experiment['validation_start']],validation[0]['equity'])
            atomic_json(path/(name+'-result.json'),result)
            for key,period_label in (('metrics','全期'),('development_metrics',f"截至{experiment['development_end'][:4]}开发段"),('validation_metrics',f"{experiment['validation_start'][:4]}起留出段")):
                runs.append({'label':f'{label} · {period_label}','metrics':result[key],
                             'status':'diagnostic','artifact':str(path/(name+'-result.json'))})
        except AppError as exc:
            runs.append({'label':'股息排序' if rank else f'税前股息门槛 {float(rate):.0%}','status':'blocked','message':str(exc),'metrics':{}})
        progress(f'历史回放 {label}：{runs[-1]["status"]}',flush=True)
    comparisons=build_comparisons(client,data['sessions'],cfg['paper_cash'],symbol=experiment['benchmark'])
    for item in comparisons:
        runs.append({'label':item['label'],'status':item['status'],'metrics':item['metrics'],'message':'；'.join(filter(None,[item.get('message'),*item['limitations']]))})
        if item['series']:
            comparison_series.append({'label':item['label'],'kind':'cash' if item.get('symbol') is None and '现金' in item['label'] else 'benchmark','points':item['series']})
    coverage={'start':data['sessions'][0],'end':end,'sessions':len(data['sessions']),'stocks':len(cohort['stocks']),
              'price_rows':sum(len(rows) for rows in data['bars'].values()),
              'valuation_rows':sum(len(rows) for rows in history.values()),'input_errors':data.get('errors',{}),
              'pool_sizes':[{'day':r['day'],'stocks':len(r['symbols'])} for r in cohort.get('rebalances',[])]}
    scope=('名单按各年末当时指标重新筛选，最多20只在选股票，退出后仍跟踪持仓；' if cohort.get('rebalances')
           else f"名单按{experiment['selection_day']}指标重建，未使用今天研究池；")
    if experiment.get('cohort_limit',experiment['pool_size']) is None:
        scope='每年末检查全部初筛候选，财务合格者不按20截断；月末完整筛选后最多20只持仓，年中未额外扫描全市场；'
    output={'status':'diagnostic' if periods else 'blocked','title':'独立历史股票池 · 原始价格成交回放','as_of':end,'protocol':experiment,'runs':runs,
        'initial_cash':cfg['paper_cash'],'primary_metrics':primary_metrics,'selection_audit':cohort,
        'coverage':coverage,'message':f"区间 {data['sessions'][0]}—{end}，{len(data['sessions'])} 个交易日、累计涉及{len(cohort['stocks'])}只股票。输入疑点 {len(data.get('errors',{}))} 项。",
        'comparison_series':comparison_series,'diagnostics':diagnostics,'period_diagnostics':periods,
        'limitations':['当前仅为历史诊断，不能据此宣布年化10%达成。',
                       scope+'原始历史财报版本未逐份公告存档核验，仍存在修订数据和缺口选样风险。',
                       '行业使用申万一级历史纳入/剔除区间，缺失/冲突不取当前标签；入池后行业变更会冻结交易，供应商历史分类修订仍需原始存档验证。',
                       f"本次运行前登记开发段截至{experiment['development_end']}、留出段自{experiment['validation_start']}；"+('仅运行一个股息排序方案，5%只作估计参考，不是入场门槛。' if rank else '全部4%/5%/6%结果公开。')+'此前已查看过部分历史结果，此留出段不是真正未见数据；年度和滚动统计切分同一账户曲线，没有逐窗训练参数，不是真正独立盲测。',
                       experiment.get('monthly_targets','月末重新分配目标；除资料待核实的冻结持仓外，不再满足买入条件会退出。这不是仅在预测股息率低于3%才卖出的持有策略。'),
                       ('单股研究资料待核实时保留股数并占用额度，其他合格股仍可按原约束买入；持仓身份或行业不明继续暂停组合新增，估值或股份权益不可核实仍阻断回放。' if experiment.get('freeze_policy')=='position'
                        else '任一实际持仓资料待核实即暂停组合全部新增买单，包括加仓；其他持仓仍可能卖出。'),
                       '当前为税前现金及股份分派与保守费用；仅入账公告已核实且新增股数为整数的送转，未核实/零碎股或持仓估值缺失会阻断，不删除异常股票来美化收益。']}
    atomic_json(path/'summary.json',output)
    atomic_json(workspace/'backtests'/'latest.json',output)
    return output
