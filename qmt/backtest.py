"""Historical replay of supplied dated signals; never selects using future returns."""
import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from bisect import bisect_right
from statistics import median

from .config import AppError, date_key, dec, money, symbol_key
from .ledger import Ledger, rebalance_quantity
from . import research


def metrics(curve, trades, initial_cash):
    """Calendar-time CAGR and drawdown include uninvested cash and paid costs."""
    initial = dec(initial_cash, positive=True)
    peak = initial
    drawdown = Decimal(0)
    years = {}
    previous = initial
    for point in curve:
        equity = dec(point['equity'], positive=True)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity/peak-1)
        year = point['day'][:4]
        years.setdefault(year, {'start_equity':previous, 'end_equity':equity, 'sessions':0})
        years[year]['end_equity'] = equity
        years[year]['sessions'] += 1
        previous = equity
    days = (datetime.strptime(curve[-1]['day'],'%Y%m%d')-datetime.strptime(curve[0]['day'],'%Y%m%d')).days
    total_return = previous/initial-1
    average_equity = sum(dec(p['equity']) for p in curve)/len(curve)
    traded = sum((dec(t['price'])*t['quantity'] for t in trades),Decimal(0))
    return {'total_return':float(total_return),
            'cagr':float((previous/initial)**(Decimal('365.2425')/days)-1) if days else None,
            'calendar_days':days,'max_drawdown':float(drawdown),
            'costs':str(sum((dec(t['fee']) for t in trades),Decimal(0))),
            'gross_traded_value':str(traded),'two_way_turnover':float(traded/average_equity),
            'average_exposure':float(sum(Decimal(str(p['exposure'])) for p in curve)/len(curve)),
            'invested_session_fraction':sum(p['exposure']>0 for p in curve)/len(curve),
            'trade_count':len(trades),
            'annual_returns':{year:{'return':float(v['end_equity']/v['start_equity']-1),'sessions':v['sessions'],
                                    'partial_period':year in (curve[0]['day'][:4],curve[-1]['day'][:4])}
                              for year,v in years.items()}}


def period_diagnostics(curve, trades):
    """Fixed-path calendar years and overlapping three-year windows, not retraining."""
    days=[date_key(p['day']) for p in curve]
    if len(days)<2 or days!=sorted(set(days)):
        raise AppError('稳定性诊断需要至少两个不重复的递增日期。')
    annual=[]
    for year in sorted({day[:4] for day in days[1:]}):
        indices=[i for i,day in enumerate(days) if i>0 and day[:4]==year]
        first,last=indices[0],indices[-1]
        section=curve[first-1:last+1]
        fills=[t for t in trades if days[first]<=t['trade_date']<=days[last]]
        annual.append({'year':int(year),'start':days[first-1],'end':days[last],
                       'metrics':metrics(section,fills,section[0]['equity'])})
    windows=[]
    for last,day in enumerate(days):
        if last+1<len(days) and day[:6]==days[last+1][:6]:continue
        end=datetime.strptime(day,'%Y%m%d')
        # February 29 maps to February 28 when the earlier year is not leap.
        try:target=end.replace(year=end.year-3)
        except ValueError:target=end.replace(year=end.year-3,day=28)
        first=bisect_right(days,target.strftime('%Y%m%d'))-1
        if first<0:continue
        value=metrics(curve[first:last+1],[],curve[first]['equity'])['cagr']
        windows.append({'start':days[first],'end':day,'cagr':value})
    values=[w['cagr'] for w in windows]
    return {'annual':annual,'rolling_three_year':{'windows':len(values),
        'min_cagr':min(values) if values else None,'median_cagr':median(values) if values else None,
        'max_cagr':max(values) if values else None,
        'positive_fraction':sum(v>0 for v in values)/len(values) if values else None},
        'rolling_windows':windows}


def _inputs(data, signals, max_holdings, *, freeze_policy='portfolio'):
    if freeze_policy not in ('portfolio', 'position'):
        raise AppError('未知冻结策略，不能回退为默认规则。')
    if data.get('errors'):
        raise AppError('历史输入尚未核实，未生成模拟收益：'+'；'.join(f'{code} {reason}' for code,reason in data['errors'].items()))
    sessions = data['sessions']
    if len(sessions)<2 or sessions!=sorted(set(sessions)):
        raise AppError('回测至少需要两个严格递增且不重复的交易日。')
    for day in sessions:
        date_key(day)
    if type(max_holdings) is not int or not 1<=max_holdings<=20:
        raise AppError('回测持仓数量须为 1—20；研究池数量不等于持仓数量。')
    if data.get('universe_as_of',sessions[0])>sessions[0]:
        raise AppError('历史候选池不能来自回测开始之后。')
    bars = {}
    for symbol, rows in data['bars'].items():
        symbol_key(symbol)
        if not data['industries'].get(symbol) or data['industries'][symbol]=='待核实':
            raise AppError('回测缺少股票行业，不能控制集中度。')
        bars[symbol] = {}
        for day,row in rows.items():
            date_key(day)
            if day not in sessions:
                raise AppError('回测日线包含窗口外或非交易日记录。')
            if research.full_day_suspension(symbol,day):
                raise AppError(f'{symbol} {day} 全天停牌证明与原始日线冲突。')
            bar = {k:dec(row[k],positive=k!='vol') for k in ('open','high','low','close','vol')}
            bar.update(up=dec(row['up'],positive=True) if row.get('up') is not None else None,
                       down=dec(row['down'],positive=True) if row.get('down') is not None else None)
            if not bar['low']<=min(bar['open'],bar['close'])<=max(bar['open'],bar['close'])<=bar['high']:
                raise AppError('回测 OHLC 关系异常。')
            if bar['up'] is not None and bar['down'] is not None and bar['down']>=bar['up']:
                raise AppError('回测涨跌停范围异常。')
            bars[symbol][day] = bar
    membership = data.get('universe_membership')
    if 'universe_membership' in data:
        if not isinstance(membership,dict) or set(membership)!=set(bars):
            raise AppError('历史成员区间必须完整对应回测股票集合。')
        universe_start = date_key(data.get('universe_as_of',sessions[0]))
        for code,windows in membership.items():
            if not isinstance(windows,list) or not windows:
                raise AppError('历史成员区间必须为非空列表。')
            previous_end = None
            for index,window in enumerate(windows):
                if not isinstance(window,dict) or set(window)!={'start','end'}:
                    raise AppError('历史成员区间须明确提供 start 和 end。')
                start = date_key(window['start'])
                end = date_key(window['end']) if window['end'] is not None else None
                if (start not in sessions or start<universe_start or
                        (end is not None and (end not in sessions or end<=start)) or
                        (index and (previous_end is None or start<previous_end))):
                    raise AppError('历史成员区间须按交易日递增且不重叠，结束日不属于该区间。')
                previous_end = end
    snapshots = {}
    for day,snapshot in signals.items():
        if freeze_policy=='position' and (not isinstance(snapshot,dict) or 'portfolio_freeze' not in snapshot):
            raise AppError('按持仓冻结模式要求每个信号明确给出 portfolio_freeze。')
        candidates = snapshot.get('candidates') if isinstance(snapshot,dict) else snapshot
        frozen = snapshot.get('freeze',[]) if isinstance(snapshot,dict) else []
        retained = snapshot.get('retain',[]) if isinstance(snapshot,dict) else []
        if day not in sessions or not isinstance(candidates,list) or not isinstance(frozen,list) or not isinstance(retained,list):
            raise AppError('信号日期不在回测交易日内，或候选格式无效。')
        if (any(not isinstance(code,str) for code in retained) or len(set(retained))!=len(retained)
                or any(code not in bars or (day not in bars[code] and not research.full_day_suspension(code,day))
                       for code in retained)):
            raise AppError('保留股票代码格式无效、重复或缺少信号日行情。')
        if set(retained)&set(frozen):
            raise AppError('同一股票不能同时保留与资料缺失冻结。')
        if membership is not None and any(not any(window['start']<=day and (window['end'] is None or day<window['end'])
                                                   for window in membership[code]) for code in retained):
            raise AppError('保留股票不在当日有效的历史成员区间内。')
        if any(not isinstance(code,str) for code in frozen) or len(set(frozen))!=len(frozen) or any(code not in bars for code in frozen):
            raise AppError('冻结股票代码重复或缺少行情。')
        if freeze_policy=='position':
            portfolio_frozen=snapshot['portfolio_freeze']
            if (not isinstance(portfolio_frozen,list) or any(not isinstance(code,str) for code in portfolio_frozen)
                    or len(set(portfolio_frozen))!=len(portfolio_frozen) or not set(portfolio_frozen)<=set(frozen)):
                raise AppError('portfolio_freeze 须为不重复且属于 freeze 的合法股票代码列表。')
            for code in portfolio_frozen:
                symbol_key(code)
        if membership is not None and any(day<membership[code][0]['start'] for code in frozen):
            raise AppError('尚未首次入池的股票不能作为历史冻结持仓。')
        if set(frozen)&{c['symbol'] for c in candidates}:
            raise AppError('同一股票不能同时是买入候选与资料缺失冻结对象。')
        if len({c['symbol'] for c in candidates})!=len(candidates):
            raise AppError('同一天的候选代码重复。')
        for candidate in candidates:
            if candidate['symbol'] not in bars or date_key(candidate['known_on'])>day:
                raise AppError('信号含未来依据或缺少行情。')
            if membership is not None and not any(window['start']<=day and (window['end'] is None or day<window['end'])
                                                  for window in membership[candidate['symbol']]):
                raise AppError('买入候选不在当日有效的历史成员区间内。')
            dec(candidate['reference_price'],positive=True)
        snapshots[day]={'candidates':candidates,'freeze':frozen}
        if freeze_policy=='position':
            snapshots[day]['portfolio_freeze']=list(portfolio_frozen)
        if retained:
            snapshots[day]['retain']=sorted(retained)
    dividends = {}
    for symbol, events in data.get('dividends',{}).items():
        if symbol not in bars:
            raise AppError('分红代码不在回测股票集合中。')
        seen = set()
        dividends[symbol] = []
        for event in events:
            stock_only=event.get('event_kind')=='stock_only'
            for key in ('ann_date','imp_ann_date','record_date','ex_date'):
                date_key(event[key])
            if stock_only:
                if dec(event['cash_div_tax'])!=0 or event.get('pay_date') is not None or dec(event['stk_div'])<=0:
                    raise AppError('纯送转事件不应有现金支付，且须有正股份比例。')
                if date_key(event['stock_list_date'])<event['ex_date']:
                    raise AppError('送转股上市日期早于除权日。')
            else:
                date_key(event['pay_date'])
            published = max(event['ann_date'],event['imp_ann_date'])
            if not published<=event['record_date']<event['ex_date'] or (not stock_only and event['ex_date']>event['pay_date']):
                raise AppError('回测分红公告、登记、除息与付款日期关系异常。')
            identity = (event['record_date'],event['ex_date'])
            if identity in seen:
                raise AppError('回测分红重复或冲突，须在输入时先核对。')
            seen.add(identity)
            dec(event['cash_div_tax']);dec(event['stk_div'])
            dividends[symbol].append(event)
    return sessions,bars,dividends,snapshots


def suspension_values(held, day, sessions, bars, dividends, last_real):
    """Value only verified missing bars, anchored to the last actual raw close."""
    values={}
    for symbol,quantity in held.items():
        if day in bars[symbol] or not research.full_day_suspension(symbol,day):
            continue
        price_date=last_real.get(symbol)
        if price_date is None:
            raise AppError(f'{symbol} 停牌估值没有更早的真实收盘价。')
        chain=sessions[sessions.index(price_date)+1:sessions.index(day)+1]
        proofs=[research.full_day_suspension(symbol,d) for d in chain]
        if None in proofs:
            raise AppError(f'{symbol} 停牌前价之间存在未经核实的行情缺口。')
        if any(price_date<event['ex_date']<=day for event in dividends.get(symbol,[])):
            raise AppError(f'{symbol} 停牌估值跨越除权除息，不能沿用原始前价。')
        price=bars[symbol][price_date]['close']
        values[symbol]={'day':day,'symbol':symbol,'quantity':quantity,'price':str(price),
            'price_date':price_date,'market_value':str(price*quantity),
            'method':'verified_suspension_prior_close','tradable':False,
            'stale_sessions':len(chain),'proofs':proofs}
    return values


def simulate(data, signals, cfg, initial_cash=None, max_holdings=5, *, freeze_policy='portfolio', capital_policy='fixed_amount',
             rebalance_policy='monthly_only'):
    """Replay raw daily bars and supplied ranked close-of-day candidate snapshots.

    data: sessions, bars[symbol][day] (open/high/low/close/vol/up/down),
    industries[symbol], dividends[symbol] (implemented events), universe_as_of.
    Optional universe_membership[symbol]: [{start, end}], with end exclusive.
    signals[day]: [{symbol, reference_price, known_on, reason?}, ...], or
    {candidates: [...], freeze: [symbol,...], retain: [symbol,...]}. Frozen held shares are preserved,
    count toward caps, and block new buys in the default portfolio policy.
    The position policy requires explicit portfolio_freeze subsets in every snapshot;
    only held names in that subset block other buys. All frozen held quantities stay fixed.
    Frozen unheld names never block others.
    Retain-only held shares keep at most their current quantity within the same monthly
    target budgets. Candidates still rebalance normally; no daily price-driven trimming.
    An empty list exits; absent snapshots keep positions. Signals at the last session
    cannot fill. Verified full-day suspensions use an explicit prior-close holding
    valuation, preserve shares and expire execution-day orders without fills.
    Other missing bars and unverified or fractional held distributions halt.
    """
    if capital_policy not in ('fixed_amount','equity_ratio'):
        raise AppError('未知仓位额度规则。')
    if rebalance_policy not in ('monthly_only','after_confirmed_sale'):
        raise AppError('未知补单调仓规则。')
    if 'paper_capital_policy' in cfg and cfg['paper_capital_policy']!=capital_policy:
        raise AppError('账户额度配置与回放政策不一致。')
    initial_cash=cfg['paper_cash'] if initial_cash is None else initial_cash
    sessions,bars,dividends,snapshots = _inputs(data,signals,max_holdings,freeze_policy=freeze_policy)
    fee_cfg = {**cfg,'sell_stamp_rate':str(max(dec(cfg['sell_stamp_rate']),Decimal('.001')))}
    if capital_policy=='equity_ratio':
        fee_cfg['paper_capital_policy']=capital_policy
    ledger = Ledger(':memory:',fee_cfg)
    try:
        ledger.initialize('paper',initial_cash,cfg['paper_max_position'],cfg['paper_max_sector'],sessions[0])
        allocation_account=ledger.account('paper')
        ledger.db.execute("INSERT INTO metadata VALUES('automatic-rules','historical-replay')")
        ledger.db.execute("INSERT INTO metadata VALUES('historical-sessions',?)",(json.dumps(sessions),))
        ledger.db.commit()
        session_index = {day:i for i,day in enumerate(sessions)}
        event_dates = {e[key] for events in dividends.values() for e in events for key in ('ex_date','pay_date')
                       if e.get(key) and sessions[0]<=e[key]<=sessions[-1]}
        curve = []; skipped = []; valuation_events = []; last_real = {}; after_sale = None
        for day in sorted(set(sessions)|event_dates):
            active_events = {s:[e for e in events if day in (e['ex_date'],e['pay_date'])]
                             for s,events in dividends.items()}
            ledger.dividends(active_events,day)
            if day not in session_index:
                continue
            suspended={}
            for symbol,rows in bars.items():
                if day in rows:
                    last_real[symbol]=day
                else:
                    proof=research.full_day_suspension(symbol,day)
                    if proof:suspended[symbol]=proof
            ledger.settle_paper(day,bars,suspensions=suspended or None)
            prices = {s:{'trade_date':day,'close':rows[day]['close']} for s,rows in bars.items() if day in rows}
            valuation_only=suspension_values(ledger.positions('paper',day),day,sessions,bars,dividends,last_real)
            ledger.mark('paper',day,prices,valuation_only=valuation_only or None)
            account = ledger.summary('paper')
            mark = account['marks'][-1]
            equity = dec(mark['equity'],positive=True)
            valuation_events.extend({**value,'weight':str(dec(value['market_value'])/equity)}
                                    for symbol,value in sorted(valuation_only.items()))
            exposure = (equity-dec(mark['cash'])-dec(mark['receivable']))/equity
            curve.append({**mark,'exposure':float(exposure),'positions':dict(account['positions'])})
            if after_sale is not None and day==after_sale['decision_day']:
                batch,after_sale = after_sale,None  # One close-of-day opportunity, never roll an intent forward.
                held = ledger.positions('paper')
                old_snapshot = snapshots[batch['origin']]
                blocked = set(old_snapshot['freeze'] if freeze_policy=='portfolio' else old_snapshot['portfolio_freeze'])&set(held)
                reason = ''
                if day in snapshots:
                    reason = '已有更新信号，取消旧目标'
                elif session_index[day]==len(sessions)-1:
                    reason = '没有下一交易日'
                elif not any(ledger.db.execute('SELECT 1 FROM fills WHERE plan_id=? AND trade_date=? AND quantity>0',
                                              (pid,day)).fetchone() for pid in batch['sell_ids']):
                    reason = '原月末卖单没有真实成交'
                elif blocked:
                    reason = '原信号的实际持仓仍触发全组合资料冻结'
                limits = ledger.position_limits('paper',equity)
                budget = min(limits['max_position'],equity*Decimal('.9')/max_holdings)
                for intent in batch['buys']:
                    code = intent['symbol']; original_price = intent['price']; cancel = reason
                    if not cancel and any(batch['origin']<e['ex_date']<=day and dec(e['stk_div'])>0
                                          for e in dividends.get(code,[])):
                        cancel = '等待期送转除权改变原目标股数口径'
                    if not cancel and code not in prices:
                        cancel = '补单决策日缺少真实可交易收盘价'
                    if not cancel and dec(prices[code]['close'])>original_price:
                        cancel = '补单决策日收盘价超过原买入限价'
                    if cancel:
                        skipped.append({'day':day,'symbol':code,'origin_signal_date':batch['origin'],
                                        'reason':'卖出后补买取消：'+cancel})
                        continue
                    pending_buys = [p for p in ledger.open_plans('paper') if p['side']=='buy']
                    reserved_codes = set(held)|{p['symbol'] for p in pending_buys}
                    values = {s:dec(valuation_only[s]['market_value']) if s in valuation_only else dec(prices[s]['close'])*q
                              for s,q in held.items()}
                    reserved = Decimal(0)
                    for plan in pending_buys:
                        remaining = plan['quantity']-plan['filled']
                        amount = dec(plan['limit_price'])*remaining
                        values[plan['symbol']] = values.get(plan['symbol'],Decimal(0))+amount
                        reserved += amount+ledger.estimated_fee(plan['limit_price'],remaining,'buy')
                    sector_value = sum((value for s,value in values.items() if data['industries'][s]==data['industries'][code]),Decimal(0))
                    available = min(budget-values.get(code,Decimal(0)),limits['max_position']-values.get(code,Decimal(0)),limits['max_sector']-sector_value)
                    quantity = min(rebalance_quantity(held.get(code,0),intent['target']),max(0,int(available/(original_price*100))*100))
                    cash = ledger.cash('paper')-reserved-equity*Decimal('.1')
                    while quantity>0 and original_price*quantity+ledger.estimated_fee(original_price,quantity,'buy')>cash:
                        quantity-=100
                    if code not in reserved_codes and len(reserved_codes)>=max_holdings:
                        cancel = '实际持仓及待买仍占满数量上限'
                    elif quantity<=0:
                        cancel = '当日目标、单股、行业或现金额度不足一手'
                    else:
                        price = min(original_price,dec(prices[code]['close'])*Decimal('1.01')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
                        row = {'symbol':code,'industry':data['industries'][code],'automatic_signal':True,
                               'review_hash':hashlib.sha256(json.dumps(old_snapshot,sort_keys=True).encode()).hexdigest()}
                        try:
                            ledger.create_plan('paper',row,day,sessions[session_index[day]+1],quantity,price,'buy',
                                               f"原月末信号 {batch['origin']} 卖出成交后一次补买",prices,data['industries'],
                                               valuation_only=valuation_only or None)
                        except AppError as exc:
                            cancel = str(exc)
                    if cancel:
                        skipped.append({'day':day,'symbol':code,'origin_signal_date':batch['origin'],
                                        'reason':'卖出后补买取消：'+cancel})
            if day not in snapshots or session_index[day]==len(sessions)-1:
                continue
            next_day = sessions[session_index[day]+1]
            held = ledger.positions('paper')
            research_frozen = set(snapshots[day]['freeze'])&set(held)
            frozen = research_frozen|set(valuation_only)
            blocked = research_frozen if freeze_policy=='portfolio' else set(snapshots[day]['portfolio_freeze'])&set(held)
            targets = {code:held[code] for code in frozen}; targets_price = {}; sector_values = {}
            for code in frozen:
                sector=data['industries'][code]
                value=dec(valuation_only[code]['market_value']) if code in valuation_only else dec(prices[code]['close'])*held[code]
                sector_values[sector]=sector_values.get(sector,Decimal(0))+value
            # Legacy replay budgets used raw config amounts before account rounding.
            limits=(ledger.position_limits('paper',equity) if capital_policy=='equity_ratio' else
                    {'max_position':dec(cfg['paper_max_position']),'max_sector':dec(cfg['paper_max_sector'])})
            budget = min(limits['max_position'],equity*Decimal('.9')/max_holdings)
            candidate_codes = {c['symbol'] for c in snapshots[day]['candidates']}
            for code in sorted((set(snapshots[day].get('retain',[]))&set(held))-candidate_codes-frozen):
                close = dec(prices[code]['close'])
                sector = data['industries'][code]
                available = min(budget,limits['max_sector']-sector_values.get(sector,Decimal(0)))
                quantity = min(held[code],max(0,int(available/(close*100))*100))
                if quantity:
                    targets[code] = quantity
                    sector_values[sector] = sector_values.get(sector,Decimal(0))+close*quantity
            for candidate in snapshots[day]['candidates']:
                code = candidate['symbol']
                if code not in prices and research.full_day_suspension(code,day):
                    skipped.append({'day':day,'symbol':code,'reason':'已核实信号日全天停牌，保留原持股且不创建买卖目标'})
                    continue
                if len(targets)>=max_holdings:
                    break
                if code not in prices:
                    raise AppError(f'{code} 信号日行情缺失，不能用未来价格创建目标。')
                close = dec(prices[code]['close'])
                reference = dec(candidate['reference_price'])
                if close>reference:
                    skipped.append({'day':day,'symbol':code,'reason':'收盘价高于信号参考价'})
                    continue
                price = min(close*Decimal('1.01'),reference).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
                sector = data['industries'][code]
                available = min(budget,limits['max_sector']-sector_values.get(sector,Decimal(0)))
                quantity = max(0,int(available/(price*100))*100)
                if not quantity:
                    skipped.append({'day':day,'symbol':code,'reason':'目标预算不足一手或行业额度不足'})
                    continue
                targets[code] = quantity;targets_price[code] = price
                sector_values[sector] = sector_values.get(sector,Decimal(0))+price*quantity
            deferred_buys = []; sell_ids = []
            for side in ('sell','buy'):
                for code in sorted(set(held)|set(targets)):
                    difference = rebalance_quantity(held.get(code,0),targets.get(code,0))
                    quantity = difference if side=='buy' else -difference
                    if quantity<=0:
                        continue
                    if code not in prices:
                        raise AppError(f'{code} 缺少信号日持仓估值。')
                    price = (dec(prices[code]['close'])*Decimal('.99')).quantize(Decimal('.01'),rounding=ROUND_CEILING) if side=='sell' else targets_price[code]
                    if side=='buy':
                        if blocked:
                            skipped.append({'day':day,'symbol':code,'reason':'既有持仓资料待核实，保留冻结股数并暂停新增买入'})
                            continue
                        reserved_codes = set(held)|{p['symbol'] for p in ledger.open_plans('paper') if p['side']=='buy'}
                        if code not in reserved_codes and len(reserved_codes)>=max_holdings:
                            skipped.append({'day':day,'symbol':code,'reason':'当前持仓及待买股票已达数量上限，不能预支卖出'})
                            if rebalance_policy=='after_confirmed_sale':
                                deferred_buys.append({'symbol':code,'target':targets[code],'price':price})
                            continue
                        reserved = sum((dec(p['limit_price'])*p['quantity']+ledger.estimated_fee(p['limit_price'],p['quantity'],'buy')
                                        for p in ledger.open_plans('paper') if p['side']=='buy'),Decimal(0))
                        available = ledger.cash('paper')-reserved-equity*Decimal('.1')
                        while quantity>0 and price*quantity+ledger.estimated_fee(price,quantity,'buy')>available:
                            quantity-=100
                        if quantity<=0:
                            skipped.append({'day':day,'symbol':code,'reason':'扣除费用与现金底仓后不足一手，不预支卖出款'})
                            continue
                    row = {'symbol':code,'industry':data['industries'][code],'automatic_signal':True,
                           'review_hash':hashlib.sha256(json.dumps(snapshots[day],sort_keys=True).encode()).hexdigest()}
                    try:
                        pid = ledger.create_plan('paper',row,day,next_day,quantity,price,side,'历史月度目标调整',prices,data['industries'],
                                                 valuation_only=valuation_only or None)
                        if rebalance_policy=='after_confirmed_sale' and side=='sell':
                            sell_ids.append(pid)
                    except AppError as exc:
                        skipped.append({'day':day,'symbol':code,'reason':str(exc)})
            if deferred_buys:
                after_sale = {'origin':day,'decision_day':next_day,'sell_ids':sell_ids,'buys':deferred_buys}
        trades = [dict(r) for r in ledger.db.execute("SELECT p.symbol,p.side,p.signal_date,f.* FROM fills f JOIN plans p ON p.id=f.plan_id ORDER BY f.id")]
        plans = [dict(r) for r in ledger.db.execute('SELECT * FROM plans ORDER BY id')]
        return {'start':sessions[0],'end':sessions[-1],'initial_cash':str(money(initial_cash)),
                'equity_curve':curve,'trades':trades,'plans':plans,'skipped':skipped,'valuation_events':valuation_events,
                'cash_events':[dict(r) for r in ledger.db.execute('SELECT * FROM cash_events ORDER BY day')],
                'stock_events':[dict(r) for r in ledger.db.execute('SELECT * FROM stock_events ORDER BY ex_date,reference')],
                'metrics':metrics(curve,trades,initial_cash),
                'assumptions':{'freeze_policy':freeze_policy,'suspension_valuation':'verified_full_day_prior_close',
                               **({'rebalance_policy':rebalance_policy,
                                   'rebalance_timing':'仅原月末因数量上限未创建的目标：原卖单下一交易日真实成交后收盘复核，再下一交易日一次补买；原目标股数和限价封顶，未成交不延期。沿原快照两交易日有效期，不重做每日财务、身份或行业审查；新快照优先取消旧补单。'}
                                  if rebalance_policy=='after_confirmed_sale' else {}),
                               **({'capital_policy':capital_policy,
                                   'position_limit_ratio':str(dec(allocation_account['max_position'])/dec(allocation_account['initial_cash'])),
                                   'sector_limit_ratio':str(dec(allocation_account['max_sector'])/dec(allocation_account['initial_cash']))}
                                  if capital_policy=='equity_ratio' else {}),
                               'max_holdings':max_holdings,'max_position':cfg['paper_max_position'],
                               'max_sector':cfg['paper_max_sector'],'cash_reserve':'0.10','fee_config':fee_cfg,
                               'signal_timing':'收盘后生成目标，仅下一交易日开盘按限价与不利滑点尝试；不成交不事后追单',
                               'tax_note':'固定卖出印花税至少 0.1% 作为成本压力假设，不是逐日历史税率；现金分红税前，未扣个人股息税；佣金为估计综合费用',
                               'data_note':'不使用复权价叠加现金分红；输入信号的财报时点与历史样本来源仍须由调用方核实'}}
    finally:
        ledger.close()
