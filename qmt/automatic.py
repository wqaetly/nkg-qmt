"""Deterministic paper experiment; never creates a human research approval."""
import hashlib
import json
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
from statistics import median

from .config import AppError, atomic_json, dec, date_key, now_cn
from .ledger import Ledger, rebalance_quantity
from .market import Tushare
from .research import statements, signed, implemented_dividends, annual_dividend_cash, annual_cash_decision, annual_cashflow_warning, verified_zero_cash_years, disclosed_date
from .service import read_json, update_ledger
from .selection import load_rules, automatic_path
from vendor.dividend_stock_research.dividend_price import calculate


def dividend_assessment(row, dossier, rules, *, earnings_basis='conservative_minimum', dividend_policy='implemented_only', cashflow_policy='latest_and_three_year', exit_policy='review_freeze'):
    if earnings_basis not in ('conservative_minimum','ttm'):
        raise AppError('未知实验盈利口径，未回退为默认规则。')
    if dividend_policy not in ('implemented_only','approved_annual_cash'):
        raise AppError('未知现金分红实验口径。')
    if cashflow_policy not in ('latest_and_three_year','three_year_coverage'):
        raise AppError('未知自由现金流实验口径。')
    if exit_policy not in ('review_freeze','known_failure_exit'):
        raise AppError('未知持仓退出实验规则。')
    if exit_policy=='known_failure_exit' and (earnings_basis!='conservative_minimum'
            or dividend_policy!='approved_annual_cash' or cashflow_policy!='three_year_coverage'):
        raise AppError('已知筛选失败退出须使用保守盈利、已批准年度现金及三年累计现金覆盖口径。')
    result = {'symbol':row['symbol'],'name':row['name'],'industry':row['industry'],
              'state':'待数据核实','reason':'','scenarios':[], 'evidence_hash':row.get('evidence_hash','')}
    decision=None
    if dividend_policy=='approved_annual_cash':
        result['dividend_policy']=dividend_policy
    if cashflow_policy=='three_year_coverage':
        result['cashflow_policy']=cashflow_policy
    failures=[]
    if exit_policy=='known_failure_exit':
        result['exit_decision']={'policy':exit_policy,'failure_codes':failures,
                                 'validation_complete':False,'blockers':[],'action':'existing_rules'}
    def stop(state, reason):
        result.update(state=state,reason=reason)
        if exit_policy=='known_failure_exit' and state=='待数据核实':
            result['exit_decision'].update(validation_complete=False,blockers=['financial_evidence_unresolved'],action='freeze')
        return result
    if row.get('cashflow_policy','latest_and_three_year')!=cashflow_policy:
        return stop('待数据核实','自由现金流摘要与评估口径不一致，须重新生成摘要')
    if row['status']=='排除':
        return stop('排除','风险警示、退市标记或最新归母利润非正')
    if row.get('financial_company'):
        return stop('不适用','金融企业需要监管资本模型，本实验不买入')
    if not row['symbol'].startswith(('600','601','603','605','000','001','002','003')):
        return stop('不适用','本实验仅支持沪深主板普通股')
    recent=row.get('annual',[])[:1]
    if cashflow_policy=='three_year_coverage':
        try:
            cashflows=statements(dossier['data']['cashflow'],row['as_of'])
            for item in row.get('annual',[])[:5]:
                raw=cashflows.get(item['period'],{})
                operating,capex=raw.get('n_cashflow_act'),raw.get('c_pay_acq_const_fiolta')
                free=None if operating is None or capex is None else signed(operating)-signed(capex)
                for key,value in (('operating_cash',operating),('capex',capex),('simple_fcf',free)):
                    saved=item.get(key)
                    if (saved is None)!=(value is None) or (value is not None and signed(saved)!=signed(value)):
                        return stop('待数据核实','自由现金流摘要与当时原始现金流量表不一致')
            warning=annual_cashflow_warning(row.get('annual',[]))
            if row.get('cashflow_warning')!=warning:
                return stop('待数据核实','最新年度自由现金流警告与摘要不一致')
            if warning:result['cashflow_warning']=warning
            if recent and recent[0].get('simple_fcf') is not None:
                result['latest_fcf']=str(signed(recent[0]['simple_fcf']))
            first_three=row.get('annual',[])[:3]
            if len(first_three)==3 and all(a.get('simple_fcf') is not None for a in first_three):
                result['three_year_fcf']=str(sum(signed(a['simple_fcf']) for a in first_three))
        except (AppError,KeyError,TypeError,ValueError,ArithmeticError):
            return stop('待数据核实','当时可用现金流量表缺失或版本冲突；不补零')
    if cashflow_policy=='latest_and_three_year' and recent and recent[0].get('simple_fcf') is not None and signed(recent[0]['simple_fcf'])<=0:
        return stop('排除','最近完整年度自由现金流非正')
    try:
        confirmed=verified_zero_cash_years(dossier['data'].get('dividend',[]),row['symbol'],row['as_of'])
        annual_periods=[p for p in statements(dossier['data']['income'],row['as_of']) if p.endswith('1231')] if confirmed else []
        latest_year=int(max(annual_periods)[:4]) if annual_periods else None
        zero_periods=[f'{latest_year-i}1231' for i in range(5) if f'{latest_year-i}1231' in confirmed] if latest_year else []
    except (AppError,KeyError,TypeError,ValueError):
        return stop('待数据核实','年度零现金分红登记或当时可用原始财报不一致，须重新核实')
    if zero_periods:
        result['zero_cash_periods']=zero_periods
        result['zero_cash_decisions']=[confirmed[period] for period in zero_periods]
        return stop('排除','、'.join(p[:4] for p in zero_periods)+'年度已核实全年零现金分红，不满足本策略连续现金分红规则；不代表公司无支付能力')
    try:
        balance=statements(dossier['data']['balancesheet'],row['as_of'],quarterly_share_conflicts=[])
        latest=row.get('latest_period')
        b=balance.get(latest,{})
        fields={'total_liab':'总负债','total_assets':'总资产','st_borr':'短期借款',
                'non_cur_liab_due_1y':'一年内到期非流动负债','money_cap':'货币资金'}
        missing=[{'api':'balancesheet','period':latest,'field':key,'label':label,'available_on':disclosed_date(b)}
                 for key,label in fields.items() if b and b.get(key) is None]
        missing_reason='、'.join(item['label'] for item in missing)+f'（{latest}）缺失，不能按零计算' if missing else ''
        if missing:
            result['financial_gaps']=missing
        if dividend_policy=='approved_annual_cash':
            periods=[p for p in statements(dossier['data']['income'],row['as_of']) if p.endswith('1231')]
            decision=annual_cash_decision(dossier['data']['dividend'],row['symbol'],max(periods),row['as_of'],balance) if periods else None
            if (row.get('dividend_policy')!=dividend_policy
                    or json.dumps(row.get('cash_decision'),sort_keys=True)!=json.dumps(decision,sort_keys=True)):
                return stop('待数据核实','年度现金决策摘要与当时原始记录、来源或实验口径不一致')
            result['cash_decision']=decision
    except (AppError,KeyError,TypeError,ValueError):
        return stop('待数据核实','；'.join(row.get('issues',[])[:-1]) or '当时可用资产负债表缺失或版本冲突')
    if not row.get('gate'):
        reason=missing_reason or '；'.join(row.get('issues',[])[:-1]) or '结构化数据未通过'
        return stop('待数据核实',reason)
    try:
        data, day = dossier['data'], row['as_of']
        inc, indicators = [statements(data[key],day,indicator_roe_conflicts=[] if key=='fina_indicator' else None)
                           for key in ('income','fina_indicator')]
        annual = row['annual'][:5]
        year = int(annual[0]['period'][:4])
        if [x['period'] for x in annual] != [f'{year-i}1231' for i in range(5)]:
            return stop('待数据核实','近五个连续完整年度尚不齐全')
        profits = [min(signed(a['profit']),signed(a['deducted_profit'])) for a in annual]
        if min(profits) <= 0:
            return stop('排除','近五年存在非正归母或扣非利润')
        if any(a['audit'] != '标准无保留意见' for a in annual):
            return stop('待数据核实','近五年审计意见未全部取得标准无保留意见')
        estimates=annual_dividend_cash(implemented_dividends(data['dividend'],day),balance,f'{int(day[:4])-8}0101')
        bridge=bool(decision and decision['stage']=='approved')
        cash_periods=[f'{year-i}1231' for i in range(1,6)] if bridge else [a['period'] for a in annual]
        if any(estimates.get(p,{}).get('implemented_dps') is None
               or dec(estimates[p]['implemented_dps'])<=0 for p in cash_periods):
            return stop('待数据核实','批准年度之前五年已实施正现金分红不齐' if bridge else '近五年已实施正现金分红数据不齐或非正，须重新研究')
        for item in annual:
            fresh=estimates.get(item['period'],{})
            for key in ('implemented_dps','ordinary_dps','estimated_dividend_cash','estimated_ordinary_cash'):
                if key not in item:
                    continue  # Old summaries may omit derived fields; calculations still use fresh evidence.
                expected=fresh.get(key)
                if (item[key] is None)!=(expected is None) or (expected is not None and dec(item[key])!=dec(expected)):
                    return stop('待数据核实','分红摘要与当时可用原始事件或股数依据不一致，须重新研究')
        cash_estimates=estimates
        if bridge:
            cash_estimates={**estimates,annual[0]['period']:{'estimated_dividend_cash':decision['total_cash'],
                                                          'estimated_ordinary_cash':decision['ordinary_cash']}}
        dividends = [dec(cash_estimates[a['period']]['estimated_dividend_cash'],positive=True) for a in annual[:3]]
        if cashflow_policy=='three_year_coverage':
            result['three_year_dividend_cash']=str(sum(dividends))
        payout = [dec(cash_estimates[a['period']]['estimated_ordinary_cash'],positive=True)/dec(a['profit'],positive=True) for a in annual[:3]]
        rate=min(median(payout),payout[0],Decimal(1)) if dividend_policy=='approved_annual_cash' else min(median(payout),Decimal(1))
        if dividend_policy=='approved_annual_cash':
            result.update(payout_inputs=list(map(str,payout)),payout_uncapped_median=str(median(payout)),
                          payout_latest_cap=str(payout[0]),cash_coverage_basis='commitment' if bridge else 'implemented_announcements')
            if not decision:
                result['cash_decision']={'period':annual[0]['period'],'stage':'implemented','cash_scope':'implemented_announcements',
                    'total_cash':str(dividends[0]),'ordinary_cash':cash_estimates[annual[0]['period']]['estimated_ordinary_cash'],
                    'classification':'ordinary_proxy_with_known_special_excluded',
                    'components':cash_estimates[annual[0]['period']]['dividend_cash_basis']}
        if max(payout)>1:
            return stop('待数据核实','近三年估算派息超过对应归母利润；须核查特别分红、留存收益和持续政策，不能直接视为不合格公司')
        fcf = sum(signed(a['simple_fcf']) for a in annual[:3])
        if dividend_policy=='approved_annual_cash':
            result['fcf_coverage']=str(fcf/sum(dividends))
        if fcf < sum(dividends) or (cashflow_policy=='latest_and_three_year' and signed(annual[0]['simple_fcf']) <= 0):
            if exit_policy=='review_freeze':
                return stop('待数据核实','三年自由现金流合计不足覆盖估算派息，须核查存量现金、资本开支与分红资金来源')
            failures.append('three_year_fcf_shortfall')
        latest = row['latest_period']
        if not latest.endswith('1231'):
            previous = str(int(latest[:4])-1)
            annual_period, previous_period = previous+'1231',previous+latest[4:]
            profit_ttm = signed(inc[latest]['n_income_attr_p'])+signed(inc[annual_period]['n_income_attr_p'])-signed(inc[previous_period]['n_income_attr_p'])
            deducted_ttm = signed(indicators[latest]['profit_dedt'])+signed(indicators[annual_period]['profit_dedt'])-signed(indicators[previous_period]['profit_dedt'])
        else:
            profit_ttm, deducted_ttm = signed(inc[latest]['n_income_attr_p']), signed(indicators[latest]['profit_dedt'])
        earnings = (min(profit_ttm,deducted_ttm) if earnings_basis=='ttm'
                    else min(median(profits[:3]),profits[0],profit_ttm,deducted_ttm))
        if earnings <= 0 and not failures:
            return stop('排除','滚动归母或扣非利润非正')
        b = balance[latest]
        if any(item['field'] in ('total_liab','total_assets') for item in missing):
            return stop('待数据核实',missing_reason)
        leverage = dec(b['total_liab'])/dec(b['total_assets'],positive=True)
        if leverage > dec(rules['max_debt_ratio']) and not failures:
            return stop('排除','资产负债率超过实验上限')
        if missing:
            return stop('待数据核实',missing_reason)
        debt_due = dec(b['st_borr'])+dec(b['non_cur_liab_due_1y'])
        if debt_due > dec(b['money_cap'])+max(Decimal(0),signed(annual[0]['operating_cash'])):
            if exit_policy=='review_freeze':
                return stop('待数据核实','短期到期债务超过货币资金加上年经营现金流；须核查定存、金融资产与借款安排，不能据此断言偿债不足')
            failures.append('short_debt_coverage_shortfall')
        shares = dec(row['share_count'],positive=True)
        if exit_policy=='known_failure_exit':
            dec(row['price'],positive=True)
            result['exit_decision']['validation_complete']=True
            if failures:
                result['exit_decision'].update(action='exit',debt_due=str(debt_due),
                    cash_plus_operating_cash=str(dec(b['money_cap'])+max(Decimal(0),signed(annual[0]['operating_cash']))))
                return stop('排除','已知现金覆盖筛选条件未满足；资料校验完成，按既有月度规则退出，不代表公司无支付能力')
        eps = earnings/shares
        for name, earnings_factor, payout_factor in [('悲观','0.8','0.9'),('基准','1','1'),('较好','1.1','1')]:
            result['scenarios'].append({'scenario':name,**calculate(str(eps*Decimal(earnings_factor)),str(rate*Decimal(payout_factor)),'1',rules['entry_yield'],row['price'])})
        base = result['scenarios'][1]
        result.update(estimated_yield=base['scenario_yield_at_price'],reference_price=base['reference_price_2dp_floor'],
                      estimated_eps=str(eps),payout=str(rate),fcf_coverage=str(fcf/sum(dividends)),debt_ratio=str(leverage))
        meets = dec(row['price']) <= dec(result['reference_price'])
        return stop('满足价格条件' if meets else '等待价格','机械盈利及派息假设满足门槛；未核查业务、政策及特别分红' if meets else '基准情景税前股息率未达到买入门槛')
    except (AppError,KeyError,ValueError,TypeError,ArithmeticError):
        return stop('待数据核实','历史股数、TTM、短期债务或财务字段缺失/无效；不补零')


def assess(row, dossier, rules, *, earnings_basis='conservative_minimum', dividend_policy='implemented_only', cashflow_policy='latest_and_three_year', exit_policy='review_freeze'):
    result=dividend_assessment(row,dossier,rules,earnings_basis=earnings_basis,dividend_policy=dividend_policy,cashflow_policy=cashflow_policy,exit_policy=exit_policy)
    financial_state=result['state']
    financial_reason=result['reason']
    selection=row.get('selection',{})
    valuation=dict(selection.get('valuation',{'state':'待核实','reason':'估值数据缺失'}))
    liquidity=selection.get('liquidity',{'state':'待核实','reason':'流动性数据缺失'})
    selection_unknown=exit_policy=='known_failure_exit' and any(d.get('state') not in ('通过','未通过') for d in (valuation,liquidity))
    if result.get('estimated_eps'):
        normalized_pe=dec(row['price'],positive=True)/dec(result['estimated_eps'],positive=True)
        valuation['normalized_pe']=str(normalized_pe)
        valuation['reason']+=f'；机械盈利 PE {normalized_pe:.2f}'
        if normalized_pe>dec(rules['max_normalized_pe']):
            valuation['state']='未通过'
    result['dimensions']={'valuation':valuation,'liquidity':liquidity,
                          'dividend':{'state':financial_state,'reason':financial_reason}}
    if financial_state in ('满足价格条件','等待价格'):
        if any(d['state']=='待核实' for d in (valuation,liquidity)):
            result.update(state='待数据核实',reason='估值/流动性数据不足；'+valuation['reason']+'；'+liquidity['reason'])
        elif any(d['state']!='通过' for d in (valuation,liquidity)):
            result.update(state='等待筛选条件',reason=valuation['reason']+'；'+liquidity['reason'])
        elif not selection.get('coarse_pass'):
            result.update(state='等待筛选条件',reason='关注/既有计划保留研究资格，但未通过基础候选门槛；'+selection.get('reason','初筛数据缺失'))
    if exit_policy=='known_failure_exit' and result['exit_decision']['failure_codes'] and selection_unknown:
        result.update(state='待数据核实',reason=result['reason']+'；估值/流动性资料仍待核实，不能以已知筛选失败替代资料校验')
        result['exit_decision'].update(validation_complete=False,action='freeze')
        result['exit_decision']['blockers'].append('valuation_or_liquidity_unresolved')
        result['scenarios']=[]
        for key in ('estimated_yield','estimated_eps','reference_price'):
            result.pop(key,None)
    elif exit_policy=='known_failure_exit' and result['state']=='待数据核实':
        result['exit_decision'].update(validation_complete=False,action='freeze')
        if not result['exit_decision']['blockers']:
            result['exit_decision']['blockers'].append('valuation_or_liquidity_unresolved')
    return result


def meta(ledger, key, default=None):
    row = ledger.db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def save_meta(ledger,key,value):
    with ledger.db:
        ledger.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False,sort_keys=True)))


def order_price(row, assessment, side, rules):
    close = dec(row['price'],positive=True)
    buffer = dec(rules['order_price_buffer'])
    if side=='buy':
        return min(dec(assessment['reference_price']),close*(1+buffer)).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
    return (close*(1-buffer)).quantize(Decimal('.01'),rounding=ROUND_CEILING)


def allocate(ledger, report, assessments, rules):
    """Monthly fixed share targets; unused slots remain cash, never force a trade."""
    marks = ledger.summary('paper')['marks']
    if ledger.cfg.get('paper_capital_policy','fixed_amount')=='equity_ratio' and (not marks or marks[-1]['day']!=report['as_of']):
        raise AppError('净值比例分配必须使用信号当日的账户估值。')
    equity = dec(marks[-1]['equity'])
    limits = ledger.position_limits('paper',equity)
    per_stock = min(limits['max_position'],equity*(1-dec(rules['cash_reserve']))/rules['max_holdings'])
    by_code = {r['symbol']:r for r in report['rows']}
    targets, sectors, reasons = {},{},{}
    eligible = sorted((a for a in assessments if a['state']=='满足价格条件'),key=lambda a:(-dec(a['estimated_yield']),a['symbol']))
    for a in eligible:
        symbol,industry = a['symbol'],a['industry']
        if len(targets)>=rules['max_holdings']:
            reasons[symbol]='已达到本月目标持股数量'
            continue
        price = order_price(by_code[symbol],a,'buy',rules)
        budget = min(per_stock,limits['max_sector']-sectors.get(industry,Decimal(0)))
        quantity = max(0,int(budget/(price*100))*100)
        if not quantity:
            reasons[symbol]=f'单个目标预算 {budget:.2f} 元不足一手（限价估算 {price*100:.2f} 元）'
            continue
        targets[symbol]=quantity
        sectors[industry]=sectors.get(industry,Decimal(0))+quantity*price
    return {'month':report['as_of'][:6],'created_on':report['as_of'],'targets':targets,'allocation_notes':reasons,
            'per_stock_budget':str(per_stock)}


def plan_day(ledger, report, assessments, rules):
    day = report['as_of']
    old = meta(ledger,'automatic-run-'+day)
    if old is not None:
        return old
    rows = {r['symbol']:r for r in report['rows']}
    assessed = {a['symbol']:a for a in assessments}
    target = meta(ledger,'automatic-targets')
    if target is None or target['month']!=day[:6]:
        target = allocate(ledger,report,assessments,rules)
        save_meta(ledger,'automatic-targets',target)
    held = ledger.positions('paper')
    notes = dict(target['allocation_notes'])
    blocked = any(code not in assessed or assessed[code]['state']=='待数据核实' for code in held)
    # Data gaps freeze holdings, while observed risk failures cause an exit.
    for code in set(held)|set(target['targets']):
        a = assessed.get(code)
        if a is not None and (a['state'] in ('排除','不适用') or (a['state']!='待数据核实' and a.get('estimated_yield') is not None and dec(a['estimated_yield'])<dec(rules['exit_yield']))):
            target['targets'].pop(code,None)
            notes[code]='退出：财务风险规则失败或情景股息率低于退出门槛'
    save_meta(ledger,'automatic-targets',target)
    plans = []
    for side in ('sell','buy'):
        for code in sorted(set(held)|set(target['targets'])):
            actual, desired = held.get(code,0),target['targets'].get(code,0)
            difference = rebalance_quantity(actual,desired)
            qty = -difference if side=='sell' else difference
            if qty<=0:
                continue
            if ledger.db.execute('SELECT 1 FROM plans WHERE mode=? AND symbol=? AND signal_date=?',('paper',code,day)).fetchone():
                continue
            if any(p['symbol']==code for p in ledger.open_plans('paper')):
                notes[code]='仍有待结算订单，未重复发单'
                continue
            a = assessed.get(code,{})
            if not a or a.get('state')=='待数据核实' or (side=='buy' and (blocked or a.get('state')!='满足价格条件')):
                notes[code]='资料/价格条件变化，暂停新增；数据缺口不作为卖出事实'
                continue
            quote = report['prices'].get(code)
            if not quote or quote.get('trade_date')!=day:
                notes[code]='当日行情缺失，不能生成订单'
                continue
            row = rows.get(code,{'symbol':code,'price':quote['close'],'industry':report['industries'].get(code,'待核实')})
            price = order_price(row,a,side,rules)
            if side=='buy':
                reserved_codes=set(held)|{p['symbol'] for p in ledger.open_plans('paper') if p['side']=='buy'}
                if code not in reserved_codes and len(reserved_codes)>=rules['max_holdings']:
                    notes[code]='实际持仓及待买入股票已达数量上限，等待卖出成交后再买入'
                    continue
                reserve = sum((dec(p['limit_price'])*(p['quantity']-p['filled'])+ledger.estimated_fee(p['limit_price'],p['quantity']-p['filled'],'buy') for p in ledger.open_plans('paper') if p['side']=='buy'),Decimal(0))
                equity = dec(ledger.summary('paper')['marks'][-1]['equity'])
                cash = ledger.cash('paper')-reserve-equity*dec(rules['cash_reserve'])
                while qty>0 and price*qty+ledger.estimated_fee(price,qty,'buy')>cash:
                    qty-=100
                if qty<=0:
                    notes[code]='扣除费用、待成交预留与现金底仓后不足一手；不预支卖出款'
                    continue
            digest = hashlib.sha256(json.dumps({'rules':rules,'assessment':a,'targets':target},sort_keys=True).encode()).hexdigest()
            planned = {**row,'automatic_signal':True,'review_hash':digest}
            reason = f"自动模拟 {rules['version']}；"+('月度目标差额买入' if side=='buy' else notes.get(code,'月度目标差额卖出'))
            try:
                plans.append(ledger.create_plan('paper',planned,day,report['next_session'],qty,price,side,reason,report['prices'],report['industries']))
            except AppError as exc:
                notes[code]=str(exc)
    result={'day':day,'assessments':assessments,'target':target,'plan_ids':plans,'notes':notes}
    save_meta(ledger,'automatic-run-'+day,result)
    return result


def focus_symbols(account, assessments, limit=5, watchlist=()):
    codes=list(sorted(account['positions']))
    codes.extend(p['symbol'] for p in account['plans'] if p['status'] in ('pending','partial'))
    available={a['symbol'] for a in assessments}
    codes.extend(code for code in watchlist if code in available)
    ranked=sorted((a for a in assessments if a.get('estimated_yield') is not None),
                  key=lambda a:(a.get('state')!='满足价格条件',-dec(a['estimated_yield']),a['symbol']))
    codes.extend(a['symbol'] for a in ranked)
    codes.extend(a['symbol'] for a in assessments)
    return list(dict.fromkeys(codes))[:limit]


def benchmark(client, start, end, rules):
    symbol = rules['benchmark']
    prices = client.call('fund_daily',{'ts_code':symbol,'start_date':start,'end_date':end},'ts_code,trade_date,close')
    factors = client.call('fund_adj',{'ts_code':symbol,'start_date':start,'end_date':end},'ts_code,trade_date,adj_factor')
    def unique(rows,field):
        result={}
        for row in rows:
            day=date_key(row['trade_date'])
            if row['ts_code']!=symbol or not start<=day<=end or day in result:
                raise AppError('ETF 基准代码、日期或重复记录异常。')
            result[day]=dec(row[field],positive=True)
        return result
    close, factor = unique(prices,'close'),unique(factors,'adj_factor')
    if start not in close or end not in close or set(close)!=set(factor):
        raise AppError('ETF 起止日行情/复权因子不完整，不输出收益比较。')
    base=close[start]*factor[start]
    return {'symbol':symbol,'start':start,'end':end,'return':str(close[end]*factor[end]/base-1),
            'series':[{'day':d,'value':str(close[d]*factor[d]/base)} for d in sorted(close)],
            'note':'ETF 收盘价×复权因子的同期变动，不是可成交账本；未扣投资者交易费，与个股税前模拟口径有差异'}


def run_automatic(workspace, report, cfg):
    rules=load_rules()
    if (report.get('source')!='Tushare' or not report.get('complete') or report.get('covered')!=report.get('pool_size')
            or report.get('strategy_version')!=rules['version'] or report.get('selection_rules')!=rules):
        raise AppError('自动模拟要求真实数据及完整当前规则研究批次；调试、旧规则报告不能交易。')
    path=automatic_path(workspace)
    path.mkdir(parents=True,exist_ok=True)
    (path/'.automatic-paper').write_text('Managed automatic paper ledger only.',encoding='utf-8')
    ledger=Ledger(path/'ledger.sqlite3',cfg)
    try:
        frozen=meta(ledger,'automatic-rules')
        if frozen is not None and frozen!=rules:
            raise AppError('自动规则与旧实验不一致；修改规则必须换独立 workspace，不覆盖旧收益。')
        save_meta(ledger,'automatic-rules',rules)
        if not ledger.db.execute("SELECT 1 FROM accounts WHERE mode='paper'").fetchone():
            ledger.initialize('paper',cfg['paper_cash'],cfg['paper_max_position'],cfg['paper_max_sector'],report['as_of'])
        if report['as_of']<ledger.account('paper')['start_date']:
            raise AppError('不能使用早于自动账本开始日期的报告。')
        update_ledger(workspace,ledger,report)
        assessments=[assess(row,read_json(workspace/'dossiers'/report['as_of']/f"{row['symbol']}.json"),rules) for row in report['rows']]
        if report['as_of']==report['requested_date']:
            result=plan_day(ledger,report,assessments,rules)
        else:
            result=meta(ledger,'automatic-run-'+report['as_of'],{'day':report['as_of'],'assessments':assessments,'target':{},'plan_ids':[],'notes':{}})
        result={**result,'rules':rules,'account':ledger.summary('paper'),'generated_at':now_cn().isoformat(),
                'closed_market':report['as_of']!=report['requested_date'],'benchmark':None,'benchmark_error':None}
        result['focus_symbols']=focus_symbols(result['account'],assessments,watchlist=rules['research_watchlist'])
        try:
            result['benchmark']=benchmark(Tushare(Path(report['cache_path'])),ledger.account('paper')['start_date'],report['as_of'],rules)
        except AppError as exc:
            result['benchmark_error']=str(exc)
        # Chart data includes all history, rather than the last 100 rows shown in the ledger table.
        result['chart_plans']=[dict(r) for r in ledger.db.execute("SELECT * FROM plans WHERE mode='paper'")]
        result['chart_fills']=[dict(r) for r in ledger.db.execute("SELECT p.symbol,p.side,f.* FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode='paper'")]
        atomic_json(path/'latest.json',result)
        atomic_json(path/'history'/f"{report['as_of']}.json",result)
        return result
    finally:
        ledger.close()
