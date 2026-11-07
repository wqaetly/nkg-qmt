import json
import sqlite3
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from .config import AppError, dec, money, date_key, symbol_key, now_cn
from .research import stock_distribution_terms
from . import research


def rebalance_quantity(actual, target):
    """Round buys down; sell lots or the entire odd remainder without splitting it."""
    if type(actual) is not int or type(target) is not int or actual < 0 or target < 0:
        raise AppError('调仓股数必须为非负整数。')
    difference = target - actual
    if difference >= 0:
        return difference // 100 * 100
    needed, remainder = -difference, actual % 100
    lots = ((needed + 99) // 100) * 100
    with_remainder = ((needed - remainder + 99) // 100) * 100 + remainder
    return -min(actual,lots,with_remainder)


class Ledger:
    def __init__(self, path, cfg):
        if cfg.get('paper_capital_policy','fixed_amount') not in ('fixed_amount','equity_ratio'):
            raise AppError('模拟资金限额规则必须为 fixed_amount 或 equity_ratio。')
        self.cfg = cfg
        self.db = sqlite3.connect(str(path), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts(mode TEXT PRIMARY KEY CHECK(mode IN ('paper','manual')),
            initial_cash TEXT NOT NULL,max_position TEXT NOT NULL,max_sector TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS openings(mode TEXT REFERENCES accounts(mode),symbol TEXT,quantity INTEGER,
            holding_date TEXT,PRIMARY KEY(mode,symbol));
        CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY,mode TEXT NOT NULL REFERENCES accounts(mode),
            symbol TEXT NOT NULL,industry TEXT NOT NULL,signal_date TEXT NOT NULL,execute_date TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),quantity INTEGER NOT NULL,filled INTEGER NOT NULL DEFAULT 0,
            limit_price TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',note TEXT NOT NULL,review_hash TEXT NOT NULL,
            UNIQUE(mode,symbol,signal_date,side));
        CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,mode TEXT REFERENCES accounts(mode),
            plan_id INTEGER REFERENCES plans(id),reference TEXT NOT NULL,trade_date TEXT NOT NULL,
            quantity INTEGER NOT NULL,price TEXT NOT NULL,fee TEXT NOT NULL,UNIQUE(mode,reference));
        CREATE TABLE IF NOT EXISTS cash_events(mode TEXT REFERENCES accounts(mode),reference TEXT,day TEXT,
            symbol TEXT,amount TEXT NOT NULL,note TEXT NOT NULL,PRIMARY KEY(mode,reference));
        CREATE TABLE IF NOT EXISTS entitlements(mode TEXT REFERENCES accounts(mode),reference TEXT,symbol TEXT,
            record_date TEXT,ex_date TEXT,pay_date TEXT,quantity INTEGER,dps TEXT,amount TEXT,PRIMARY KEY(mode,reference));
        CREATE TABLE IF NOT EXISTS stock_events(mode TEXT REFERENCES accounts(mode),reference TEXT,symbol TEXT,
            record_date TEXT,ex_date TEXT,stock_list_date TEXT,record_quantity INTEGER,ratio TEXT,
            quantity INTEGER,source TEXT NOT NULL,available_on TEXT NOT NULL,PRIMARY KEY(mode,reference));
        CREATE TABLE IF NOT EXISTS marks(mode TEXT REFERENCES accounts(mode),day TEXT,cash TEXT,
            receivable TEXT,equity TEXT,PRIMARY KEY(mode,day));
        """)
        if 'start_date' not in {r[1] for r in self.db.execute('PRAGMA table_info(accounts)')}:
            self.db.execute("ALTER TABLE accounts ADD COLUMN start_date TEXT NOT NULL DEFAULT ''")
            self.db.execute("UPDATE accounts SET start_date=? WHERE start_date=''",(now_cn().strftime('%Y%m%d'),))
        encoded = json.dumps(cfg, sort_keys=True)
        row = self.db.execute("SELECT value FROM metadata WHERE key='config'").fetchone()
        if row and row[0] != encoded:
            self.db.close()
            raise AppError("配置与旧账本不一致。请使用新的 --workspace 开展独立实验，不能混改旧账。")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('config',?)", (encoded,))

    def close(self):
        self.db.close()

    def account(self, mode):
        row = self.db.execute("SELECT * FROM accounts WHERE mode=?", (mode,)).fetchone()
        if not row:
            raise AppError("账本尚未初始化；手动账本需明确策略资金及单股、行业上限。")
        return dict(row)

    def position_limits(self, mode, equity):
        account = self.account(mode)
        limits = {key:dec(account[key]) for key in ('max_position','max_sector')}
        if mode=='paper' and self.cfg.get('paper_capital_policy','fixed_amount')=='equity_ratio':
            ratio = dec(equity)/dec(account['initial_cash'],positive=True)
            limits = {key:value*ratio for key,value in limits.items()}
        return limits

    def initialize(self, mode, cash, max_position, max_sector, start_date=None):
        start_date=date_key(start_date or now_cn().strftime('%Y%m%d'))
        cash, position, sector = money(cash), money(max_position), money(max_sector)
        if mode not in {'paper','manual'} or not 0 < position <= sector <= cash:
            raise AppError("需满足 0 < 单股上限 <= 行业上限 <= 初始现金，模式须为 paper/manual。")
        with self.db:
            if self.db.execute("SELECT 1 FROM accounts WHERE mode=?", (mode,)).fetchone():
                raise AppError("账本已经初始化，不覆盖已有资产。")
            self.db.execute("INSERT INTO accounts VALUES(?,?,?,?,?)", (mode,str(cash),str(position),str(sector),start_date))

    def opening(self, mode, symbol, quantity, day):
        self.account(mode)
        symbol_key(symbol)
        date_key(day)
        if type(quantity) is not int or quantity <= 0:
            raise AppError("期初持仓数量必须为正整数。")
        with self.db:
            if self.db.execute("SELECT 1 FROM plans WHERE mode=?", (mode,)).fetchone():
                raise AppError("已生成计划后不得添加期初持仓。")
            if self.db.execute("SELECT 1 FROM openings WHERE mode=? AND symbol=?", (mode,symbol)).fetchone():
                raise AppError("该期初持仓已存在。")
            self.db.execute("INSERT INTO openings VALUES(?,?,?,?)", (mode,symbol,quantity,day))

    def positions(self, mode, day='99999999'):
        held = {r['symbol']:r['quantity'] for r in self.db.execute("SELECT * FROM openings WHERE mode=? AND holding_date<=?",(mode,day))}
        for r in self.db.execute('SELECT symbol,quantity FROM stock_events WHERE mode=? AND ex_date<=?',(mode,day)):
            held[r['symbol']] = held.get(r['symbol'],0)+r['quantity']
        for r in self.db.execute("SELECT p.symbol,p.side,f.quantity FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? AND f.trade_date<=? ORDER BY f.id",(mode,day)):
            held[r['symbol']] = held.get(r['symbol'],0)+r['quantity']*(1 if r['side']=='buy' else -1)
        return {s:q for s,q in held.items() if q}

    def available_to_sell(self, mode, symbol, day):
        self.account(mode); symbol_key(symbol); date_key(day)
        bought = self.db.execute("SELECT COALESCE(SUM(f.quantity),0) FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? AND p.symbol=? AND p.side='buy' AND f.trade_date=?",(mode,symbol,day)).fetchone()[0]
        opened = self.db.execute('SELECT COALESCE(SUM(quantity),0) FROM openings WHERE mode=? AND symbol=? AND holding_date=?',(mode,symbol,day)).fetchone()[0]
        locked = self.db.execute('SELECT COALESCE(SUM(quantity),0) FROM stock_events WHERE mode=? AND symbol=? AND ex_date<=? AND stock_list_date>?',(mode,symbol,day,day)).fetchone()[0]
        return max(0,self.positions(mode,day).get(symbol,0)-bought-opened-locked)

    def cash(self, mode, day='99999999'):
        value = dec(self.account(mode)['initial_cash'])
        for r in self.db.execute("SELECT p.side,f.* FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? AND f.trade_date<=?",(mode,day)):
            value += money(dec(r['price'])*r['quantity'])*(1 if r['side']=='sell' else -1)-dec(r['fee'])
        for r in self.db.execute("SELECT amount FROM cash_events WHERE mode=? AND day<=?",(mode,day)):
            value += Decimal(r[0])
        return value

    def open_plans(self, mode):
        return [dict(r) for r in self.db.execute("SELECT * FROM plans WHERE mode=? AND status IN ('pending','partial') ORDER BY id",(mode,))]

    def estimated_fee(self, price, quantity, side):
        value = dec(price)*quantity
        fee = max(value*dec(self.cfg['commission_rate']),dec(self.cfg['minimum_commission']))
        return money(fee+(value*dec(self.cfg['sell_stamp_rate']) if side=='sell' else 0))

    def _historical_sessions(self, mode):
        marker=self.db.execute("SELECT value FROM metadata WHERE key='automatic-rules'").fetchone()
        calendar=self.db.execute("SELECT value FROM metadata WHERE key='historical-sessions'").fetchone()
        if mode!='paper' or not marker or marker[0]!='historical-replay' or not calendar:
            raise AppError('停牌估值和停牌到期仅供明确登记交易日历的历史模拟。')
        try:
            sessions=json.loads(calendar[0])
            if not isinstance(sessions,list) or not sessions or sessions!=sorted(set(sessions)):
                raise ValueError()
            for day in sessions:date_key(day)
        except (TypeError,ValueError):
            raise AppError('历史模拟交易日历无效。') from None
        return sessions

    def _held_values(self, mode, day, prices, held, valuation_only=None):
        """One risk/mark resolver; carried values never become tradable quotes."""
        carried={} if valuation_only is None else valuation_only
        if valuation_only is not None:
            sessions=self._historical_sessions(mode)
            if not isinstance(carried,dict) or not set(carried)<=set(held) or day not in sessions:
                raise AppError('停牌估值须明确对应当日实际持仓。')
            for symbol,item in carried.items():
                fields={'day','symbol','quantity','price','price_date','market_value','method','tradable','stale_sessions','proofs'}
                if (not isinstance(item,dict) or set(item)!=fields or symbol in prices
                        or item['day']!=day or item['symbol']!=symbol or type(item['quantity']) is not int
                        or item['quantity']!=held[symbol] or item['method']!='verified_suspension_prior_close'
                        or item['tradable'] is not False or type(item['stale_sessions']) is not int):
                    raise AppError('停牌估值字段、持股数或原始行情发生冲突。')
                price_date=date_key(item['price_date'])
                if price_date not in sessions or price_date>=day:
                    raise AppError('停牌估值必须保留更早的真实价格日期。')
                chain=sessions[sessions.index(price_date)+1:sessions.index(day)+1]
                proofs=[research.full_day_suspension(symbol,d) for d in chain]
                if None in proofs or item['proofs']!=proofs or item['stale_sessions']!=len(chain):
                    raise AppError('停牌估值缺少逐交易日已可用的完整原文证明。')
                price=dec(item['price'],positive=True)
                if dec(item['market_value'],positive=True)!=price*held[symbol]:
                    raise AppError('停牌估值金额与实际股数不一致。')
        values={}
        for symbol,qty in held.items():
            if symbol in carried:
                values[symbol]=dec(carried[symbol]['market_value'],positive=True)
                continue
            quote=prices.get(symbol)
            if not quote or quote.get('trade_date')!=day:
                raise AppError(f"{symbol} 在 {day} 缺少当日估值，未用旧价伪造净值。")
            values[symbol]=dec(quote['close'],positive=True)*qty
        return values

    def create_plan(self, mode, row, day, next_day, quantity, limit_price, side, note, all_prices, industries, *, valuation_only=None):
        self.account(mode)
        symbol = symbol_key(row['symbol'])
        date_key(day)
        date_key(next_day)
        if next_day<=day or side not in {'buy','sell'} or type(quantity) is not int or quantity<=0:
            raise AppError("交易计划参数无效。")
        if not symbol.startswith(('600','601','603','605','000','001','002','003')):
            raise AppError("首版执行仅支持沪深主板普通股；其他板块可研究，交易规则需另核实。")
        automatic = (mode=='paper' and row.get('automatic_signal') is True and
                     self.db.execute("SELECT 1 FROM metadata WHERE key='automatic-rules'").fetchone() is not None)
        if row.get('automatic_signal') and not automatic:
            raise AppError('机械信号只能进入独立自动模拟账本，不能用于手动交易。')
        if side=='buy' and (quantity%100 or (not automatic and (not row.get('gate') or not row.get('scenarios')))):
            raise AppError("买入须为整手且研究已确认、数据完整。")
        price = dec(limit_price,positive=True)
        if price != price.quantize(Decimal('0.01')):
            raise AppError("主板股价请按 0.01 元精度输入。")
        if valuation_only is not None:
            self._held_values(mode,day,all_prices,self.positions(mode),valuation_only)
            if symbol in valuation_only:
                raise AppError('停牌账面估值不可用于创建买卖计划或限价。')
            quote=all_prices.get(symbol)
            if not quote or quote.get('trade_date')!=day or research.full_day_suspension(symbol,day):
                raise AppError('创建交易计划仍须该股票当日真实行情，不能使用停牌估值。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if mode=='paper' and self.cfg.get('paper_capital_policy','fixed_amount')=='equity_ratio':
                future = self.db.execute("""SELECT 1 FROM (
                    SELECT mode,trade_date AS day FROM fills
                    UNION ALL SELECT mode,day FROM cash_events
                    UNION ALL SELECT mode,holding_date FROM openings
                    UNION ALL SELECT mode,ex_date FROM stock_events
                    UNION ALL SELECT mode,signal_date FROM plans
                    ) WHERE mode=? AND day>? LIMIT 1""",(mode,day)).fetchone()
                if day<self.account(mode)['start_date'] or future:
                    raise AppError('净值比例模拟不能在账户起点前或已有晚于信号日的账务时补建计划。')
            plans = self.open_plans(mode)
            if any(p['symbol']==symbol for p in plans):
                raise AppError("该股票还有未核对计划，先处理实际成交或撤单。")
            held = self.positions(mode)
            if side=='sell':
                if not note.strip() or quantity>held.get(symbol,0):
                    raise AppError("卖出需说明退出依据，且不得超过持仓。")
                if quantity%100 and quantity%100!=held.get(symbol,0)%100:
                    raise AppError("卖出须为整手，或一次包含该股票全部不足100股的余股；不得拆分零股。")
            else:
                if not row.get('industry') or row['industry']=='待核实':
                    raise AppError("缺少行业归类，不能校验集中度。")
                if any(not industries.get(code) for code in held):
                    raise AppError("已有持仓缺少当日估值/行业，暂停新增。")
                if valuation_only is None and any(not all_prices.get(code) or all_prices[code].get('trade_date')!=day for code in held):
                    raise AppError("已有持仓缺少当日估值/行业，暂停新增。")
                values=self._held_values(mode,day,all_prices,held,valuation_only)
                cash = self.cash(mode)
                receivable = sum((dec(r[0]) for r in self.db.execute("SELECT amount FROM entitlements WHERE mode=? AND ex_date<=? AND pay_date>?",(mode,day,day))),Decimal(0))
                # Pending buys reserve capacity below, but are not assets in NAV.
                limits = self.position_limits(mode,money(cash+sum(values.values(),Decimal(0))+receivable))
                reserve = Decimal(0)
                for plan in plans:
                    if plan['side']=='buy':
                        remaining = plan['quantity']-plan['filled']
                        amount = dec(plan['limit_price'])*remaining
                        values[plan['symbol']] = values.get(plan['symbol'],Decimal(0))+amount
                        reserve += amount+self.estimated_fee(plan['limit_price'],remaining,'buy')
                amount = price*quantity
                if amount+self.estimated_fee(price,quantity,side)+reserve>cash:
                    raise AppError("现金扣除未完成买单预留后不足。")
                if values.get(symbol,Decimal(0))+amount>limits['max_position']:
                    raise AppError("超过单股金额上限。")
                sector = sum(value for code,value in values.items() if industries.get(code)==row['industry'])
                if sector+amount>limits['max_sector']:
                    raise AppError("超过行业金额上限。")
            try:
                cursor = self.db.execute("INSERT INTO plans(mode,symbol,industry,signal_date,execute_date,side,quantity,limit_price,note,review_hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (mode,symbol,row['industry'],day,next_day,side,quantity,str(price),note,row.get('review_hash',row.get('evidence_hash',''))))
            except sqlite3.IntegrityError:
                raise AppError("当日已创建相同方向计划，不重复生成。") from None
            return cursor.lastrowid

    def _fill(self, plan, day, quantity, price, fee, reference):
        date_key(day)
        price,fee = dec(price,positive=True),money(fee)
        if type(quantity) is not int or quantity<=0 or not reference.strip() or len(reference)>100:
            raise AppError("成交数量/唯一成交编号无效。")
        old = self.db.execute("SELECT * FROM fills WHERE mode=? AND reference=?",(plan['mode'],reference)).fetchone()
        if old:
            if (old['plan_id'],old['trade_date'],old['quantity'],dec(old['price']),dec(old['fee']))==(plan['id'],day,quantity,price,fee):
                return False
            raise AppError("成交编号已存在且内容不同，未覆盖。")
        if plan['status'] not in {'pending','partial'} or day!=plan['execute_date'] or quantity>plan['quantity']-plan['filled']:
            raise AppError("计划状态、成交日期或剩余数量不符。")
        latest = self.db.execute("SELECT MAX(day) FROM (SELECT trade_date AS day FROM fills WHERE mode=? UNION ALL SELECT day FROM cash_events WHERE mode=?)",(plan['mode'],plan['mode'])).fetchone()[0]
        if latest and day<latest:
            raise AppError("不允许倒序补录跨日成交。")
        cash,value = self.cash(plan['mode'],day),money(price*quantity)
        if plan['side']=='buy' and value+fee>cash:
            raise AppError("账本现金不足。")
        if plan['side']=='sell':
            available=self.available_to_sell(plan['mode'],plan['symbol'],day)
            if quantity>available:
                raise AppError("可卖数量不足、违反 T+1 或新增股份尚未上市。")
            if cash+value<fee:
                raise AppError("费用超过可用资金。")
        self.db.execute("INSERT INTO fills(mode,plan_id,reference,trade_date,quantity,price,fee) VALUES(?,?,?,?,?,?,?)",
            (plan['mode'],plan['id'],reference,day,quantity,str(price),str(fee)))
        filled = plan['filled']+quantity
        self.db.execute("UPDATE plans SET filled=?,status=? WHERE id=?",(filled,'filled' if filled==plan['quantity'] else 'partial',plan['id']))
        self.db.execute("DELETE FROM marks WHERE mode=? AND day>=?",(plan['mode'],day))
        return True

    def record_manual(self, plan_id, day, quantity, price, fee, reference):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            plan = self.db.execute("SELECT * FROM plans WHERE id=? AND mode='manual'",(plan_id,)).fetchone()
            if not plan:
                raise AppError("手动账本计划不存在。")
            return self._fill(dict(plan),day,quantity,price,fee,reference)

    def close_plan(self, plan_id, reason):
        if not reason.strip():
            raise AppError("请说明取消原因，并确认已核对券商成交及剩余委托。")
        with self.db:
            changed = self.db.execute("UPDATE plans SET status='closed',note=note||? WHERE id=? AND status IN ('pending','partial')",('；关闭：'+reason[:500],plan_id)).rowcount
            if not changed:
                raise AppError("计划不存在或已结束。")

    def settle_paper(self, day, market, *, suspensions=None):
        if suspensions is not None:
            sessions=self._historical_sessions('paper')
            if not isinstance(suspensions,dict) or day not in sessions:
                raise AppError('历史停牌到期日期或证明映射无效。')
            for symbol,proof in suspensions.items():
                expected=research.full_day_suspension(symbol,day)
                if not expected or proof!=expected or day in market.get(symbol,{}):
                    raise AppError('停牌到期证明未核实或与原始行情冲突。')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for plan in sorted(self.open_plans('paper'),key=lambda p:(p['execute_date'],p['id'])):
                if plan['execute_date']>day:
                    continue
                bar = market.get(plan['symbol'],{}).get(plan['execute_date'])
                if not bar:
                    if plan['execute_date']==day and suspensions is not None and plan['symbol'] in suspensions:
                        self.db.execute("UPDATE plans SET status='expired',note=note||? WHERE id=?",
                            ('；已核实执行日全天停牌，订单到期且不延至复牌追单',plan['id']))
                        continue
                    raise AppError("计划执行日日线缺失，保留计划，不能假定停牌或成交。")
                reason = ''
                direction = 1 if plan['side']=='buy' else -1
                price = (bar['open']*(1+direction*Decimal(self.cfg['slippage_bps'])/10000)).quantize(Decimal('0.01'),rounding=ROUND_CEILING if direction==1 else ROUND_FLOOR)
                if bar['up'] is None or bar['down'] is None:
                    reason = '缺少涨跌停价格，未假定成交'
                elif bar['vol']<=0 or bar['open']>=bar['up'] or bar['open']<=bar['down']:
                    reason = '无成交或开盘触及涨跌停，未假定成交'
                elif not bar['low']<=price<=bar['high']:
                    reason = '模拟价格超出日内范围，未假定成交'
                elif plan['quantity']>bar['vol']*100*Decimal('0.01'):
                    reason = '订单超过当日成交量的 1%，未假定有足够流动性'
                elif plan['side']=='sell' and plan['quantity']>self.available_to_sell('paper',plan['symbol'],plan['execute_date']):
                    reason = '可卖数量不足、违反 T+1 或新增股份尚未上市，未假定成交'
                elif (direction==1 and price>dec(plan['limit_price'])) or (direction==-1 and price<dec(plan['limit_price'])):
                    reason = '开盘超出计划限价，不追价'
                if reason:
                    self.db.execute("UPDATE plans SET status='expired',note=note||? WHERE id=?",('；'+reason,plan['id']))
                else:
                    self._fill(plan,plan['execute_date'],plan['quantity'],price,self.estimated_fee(price,plan['quantity'],plan['side']),f"paper-{plan['id']}")

    def dividends(self, events_by_symbol, day):
        date_key(day)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for symbol,events in events_by_symbol.items():
                for event in sorted(events,key=lambda e:(e['ex_date'],e['record_date'])):
                    stock_only=event.get('event_kind')=='stock_only'
                    if stock_only and (dec(event['cash_div_tax'])!=0 or event.get('pay_date') is not None or dec(event['stk_div'])<=0):
                        raise AppError('纯送转事件的现金或股份字段矛盾，暂停入账。')
                    effective_day=event['ex_date'] if stock_only else event['pay_date']
                    if effective_day <= self.account('paper')['start_date']:
                        continue
                    if event['ex_date']>day:
                        continue
                    qty = self.positions('paper',event['record_date']).get(symbol,0)
                    ratio = dec(event['stk_div'])
                    prior = self.db.execute("SELECT * FROM stock_events WHERE mode='paper' AND symbol=? AND (record_date=? OR ex_date=?)",(symbol,event['record_date'],event['ex_date'])).fetchall()
                    if len(prior)>1:
                        raise AppError('已入账送转事件标识冲突，保留旧账待核对。')
                    old_stock = prior[0] if prior else None
                    if old_stock and (old_stock['record_quantity']!=qty or dec(old_stock['ratio'])!=ratio):
                        raise AppError('已入账送转条款或登记日持仓变化，保留旧账待核对。')
                    if ratio:
                        self.db.execute("UPDATE plans SET status='expired',note=note||? WHERE mode='paper' AND symbol=? AND signal_date<? AND execute_date>=? AND status IN ('pending','partial')",('；送转除权使旧价格/股数失效，须按新行情重新生成',symbol,event['ex_date'],event['ex_date']))
                        if not qty:
                            continue
                        terms = stock_distribution_terms(event,day)
                        if terms is None:
                            raise AppError("持仓涉及未核实上市条款的送转股；停止后续模拟。")
                        listed,available = date_key(terms['stock_list_date']),date_key(terms['available_on'])
                        if listed<event['ex_date'] or available>day or not terms['source']:
                            raise AppError('送转股上市日或公告依据无效，暂停入账。')
                        added = qty*ratio
                        if added!=added.to_integral_value():
                            raise AppError('送转权益含不足一股的分配尾差，分配规则未核实；停止后续模拟。')
                        reference = f"stock-{symbol}-{event['record_date']}-{event['ex_date']}"
                        values = ('paper',reference,symbol,event['record_date'],event['ex_date'],listed,qty,str(ratio),int(added),terms['source'],available)
                        if old_stock:
                            expected = dict(zip(('mode','reference','symbol','record_date','ex_date','stock_list_date','record_quantity','ratio','quantity','source','available_on'),values))
                            if any(old_stock[k]!=v for k,v in expected.items() if k!='ratio'):
                                raise AppError('已入账送转条款或登记日持仓变化，保留旧账待核对。')
                        else:
                            self.db.execute('INSERT INTO stock_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',values)
                            self.db.execute("DELETE FROM marks WHERE mode='paper' AND day>=?",(event['ex_date'],))
                    if not qty or stock_only:
                        continue
                    dps = dec(event['cash_div_tax'])
                    ref = f"div-{symbol}-{event['record_date']}-{event['ex_date']}"
                    amount = str(money(qty*dps))
                    old = self.db.execute("SELECT * FROM entitlements WHERE mode='paper' AND reference=?",(ref,)).fetchone()
                    if old and (old['quantity']!=qty or dec(old['dps'])!=dps or old['pay_date']!=event['pay_date']):
                        raise AppError("已入账分红事件/登记日持仓变化，保留旧账待核对。")
                    self.db.execute("INSERT OR IGNORE INTO entitlements VALUES('paper',?,?,?,?,?,?,?,?)",
                        (ref,symbol,event['record_date'],event['ex_date'],event['pay_date'],qty,str(dps),amount))
                    if event['pay_date']<=day:
                        self.db.execute("INSERT OR IGNORE INTO cash_events VALUES('paper',?,?,?,?,?)",(ref,event['pay_date'],symbol,amount,'已实施现金分红；税前模拟，未扣个人股息税'))

    def cash_event(self, reference, day, amount, symbol, note):
        account=self.account('manual')
        date_key(day)
        if day < account['start_date']:
            raise AppError('初始化之前的现金流水已包含在期初现金中，不得重复入账。')
        value = Decimal(str(amount))
        if not value.is_finite() or not reference.strip() or not note.strip():
            raise AppError("现金事件需有效金额、唯一凭证号及说明。")
        value = value.quantize(Decimal('0.01'))
        with self.db:
            old = self.db.execute("SELECT * FROM cash_events WHERE mode='manual' AND reference=?",(reference,)).fetchone()
            if old:
                if (old['day'],Decimal(old['amount']),old['symbol'],old['note'])==(day,value,symbol,note):
                    return
                raise AppError("现金事件凭证号冲突。")
            latest=self.db.execute("SELECT MAX(day) FROM (SELECT trade_date AS day FROM fills WHERE mode='manual' UNION ALL SELECT day FROM cash_events WHERE mode='manual')").fetchone()[0]
            if latest and day<latest:
                raise AppError("请按日期顺序录入成交及现金流水，不允许倒序改变历史余额。")
            if self.cash('manual',day)+value<0:
                raise AppError("现金事件会使账本余额为负。")
            self.db.execute("INSERT INTO cash_events VALUES('manual',?,?,?,?,?)",(reference,day,symbol,str(value),note))
            self.db.execute("DELETE FROM marks WHERE mode='manual' AND day>=?",(day,))

    def mark(self, mode, day, prices, *, valuation_only=None):
        cash = self.cash(mode,day)
        receivable = sum((dec(r[0]) for r in self.db.execute("SELECT amount FROM entitlements WHERE mode=? AND ex_date<=? AND pay_date>?",(mode,day,day))),Decimal(0))
        equity = cash+receivable
        equity += sum(self._held_values(mode,day,prices,self.positions(mode,day),valuation_only).values(),Decimal(0))
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?)',(mode,day,str(cash),str(receivable),str(money(equity))))

    def summary(self, mode):
        return {'account':self.account(mode),'cash':str(self.cash(mode)),'positions':self.positions(mode),
            'plans':[dict(r) for r in self.db.execute('SELECT * FROM plans WHERE mode=? ORDER BY id DESC LIMIT 100',(mode,))],
            'fills':[dict(r) for r in self.db.execute('SELECT p.symbol,p.side,f.* FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? ORDER BY f.id DESC LIMIT 100',(mode,))],
            'cash_events':[dict(r) for r in self.db.execute('SELECT * FROM cash_events WHERE mode=? ORDER BY day DESC',(mode,))],
            'stock_events':[dict(r) for r in self.db.execute('SELECT * FROM stock_events WHERE mode=? ORDER BY ex_date,reference',(mode,))],
            'marks':[dict(r) for r in self.db.execute('SELECT * FROM marks WHERE mode=? ORDER BY day',(mode,))]}
