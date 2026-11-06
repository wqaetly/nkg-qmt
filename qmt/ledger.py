import json
import sqlite3
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from .config import AppError, dec, money, date_key, symbol_key, now_cn


class Ledger:
    def __init__(self, path, cfg):
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
        for r in self.db.execute("SELECT p.symbol,p.side,f.quantity FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? AND f.trade_date<=? ORDER BY f.id",(mode,day)):
            held[r['symbol']] = held.get(r['symbol'],0)+r['quantity']*(1 if r['side']=='buy' else -1)
        return {s:q for s,q in held.items() if q}

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

    def create_plan(self, mode, row, day, next_day, quantity, limit_price, side, note, all_prices, industries):
        account = self.account(mode)
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
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            plans = self.open_plans(mode)
            if any(p['symbol']==symbol for p in plans):
                raise AppError("该股票还有未核对计划，先处理实际成交或撤单。")
            held = self.positions(mode)
            if side=='sell':
                if not note.strip() or quantity>held.get(symbol,0):
                    raise AppError("卖出需说明退出依据，且不得超过持仓。")
                if quantity%100 and quantity!=held.get(symbol,0):
                    raise AppError("不足整手的卖出须一次卖清该股票剩余持仓。")
            else:
                if not row.get('industry') or row['industry']=='待核实':
                    raise AppError("缺少行业归类，不能校验集中度。")
                values = {}
                for code,qty in held.items():
                    quote = all_prices.get(code)
                    if not quote or quote.get('trade_date')!=day or not industries.get(code):
                        raise AppError("已有持仓缺少当日估值/行业，暂停新增。")
                    values[code] = dec(quote['close'],positive=True)*qty
                reserve = Decimal(0)
                for plan in plans:
                    if plan['side']=='buy':
                        remaining = plan['quantity']-plan['filled']
                        amount = dec(plan['limit_price'])*remaining
                        values[plan['symbol']] = values.get(plan['symbol'],Decimal(0))+amount
                        reserve += amount+self.estimated_fee(plan['limit_price'],remaining,'buy')
                amount = price*quantity
                if amount+self.estimated_fee(price,quantity,side)+reserve>self.cash(mode):
                    raise AppError("现金扣除未完成买单预留后不足。")
                if values.get(symbol,Decimal(0))+amount>dec(account['max_position']):
                    raise AppError("超过单股金额上限。")
                sector = sum(value for code,value in values.items() if industries.get(code)==row['industry'])
                if sector+amount>dec(account['max_sector']):
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
            bought_today=self.db.execute("SELECT COALESCE(SUM(f.quantity),0) FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? AND p.symbol=? AND p.side='buy' AND f.trade_date=?",(plan['mode'],plan['symbol'],day)).fetchone()[0]
            opened_today=self.db.execute("SELECT COALESCE(SUM(quantity),0) FROM openings WHERE mode=? AND symbol=? AND holding_date>=?",(plan['mode'],plan['symbol'],day)).fetchone()[0]
            available=self.positions(plan['mode'],day).get(plan['symbol'],0)-bought_today-opened_today
            if quantity>available:
                raise AppError("可卖数量不足或违反 T+1。")
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

    def settle_paper(self, day, market):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for plan in sorted(self.open_plans('paper'),key=lambda p:(p['execute_date'],p['id'])):
                if plan['execute_date']>day:
                    continue
                bar = market.get(plan['symbol'],{}).get(plan['execute_date'])
                if not bar:
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
                elif (direction==1 and price>dec(plan['limit_price'])) or (direction==-1 and price<dec(plan['limit_price'])):
                    reason = '开盘超出计划限价，不追价'
                if reason:
                    self.db.execute("UPDATE plans SET status='expired',note=note||? WHERE id=?",('；'+reason,plan['id']))
                else:
                    self._fill(plan,plan['execute_date'],plan['quantity'],price,self.estimated_fee(price,plan['quantity'],plan['side']),f"paper-{plan['id']}")

    def dividends(self, events_by_symbol, day):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for symbol,events in events_by_symbol.items():
                for event in events:
                    stock_only=event.get('event_kind')=='stock_only'
                    if stock_only and (dec(event['cash_div_tax'])!=0 or event.get('pay_date') is not None or dec(event['stk_div'])<=0):
                        raise AppError('纯送转事件的现金或股份字段矛盾，暂停入账。')
                    effective_day=event['ex_date'] if stock_only else event['pay_date']
                    if effective_day <= self.account('paper')['start_date']:
                        continue
                    if event['ex_date']>day:
                        continue
                    qty = self.positions('paper',event['record_date']).get(symbol,0)
                    if not qty:
                        continue
                    if dec(event['stk_div'])!=0:
                        raise AppError("持仓涉及送转股，首版需人工处理股本变化；停止后续模拟。")
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

    def mark(self, mode, day, prices):
        cash = self.cash(mode,day)
        receivable = sum((dec(r[0]) for r in self.db.execute("SELECT amount FROM entitlements WHERE mode=? AND ex_date<=? AND pay_date>?",(mode,day,day))),Decimal(0))
        equity = cash+receivable
        for symbol,qty in self.positions(mode,day).items():
            quote = prices.get(symbol)
            if not quote or quote.get('trade_date')!=day:
                raise AppError(f"{symbol} 缺少当日估值，未用旧价伪造净值。")
            equity += dec(quote['close'],positive=True)*qty
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?)',(mode,day,str(cash),str(receivable),str(money(equity))))

    def summary(self, mode):
        return {'account':self.account(mode),'cash':str(self.cash(mode)),'positions':self.positions(mode),
            'plans':[dict(r) for r in self.db.execute('SELECT * FROM plans WHERE mode=? ORDER BY id DESC LIMIT 100',(mode,))],
            'fills':[dict(r) for r in self.db.execute('SELECT p.symbol,p.side,f.* FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode=? ORDER BY f.id DESC LIMIT 100',(mode,))],
            'cash_events':[dict(r) for r in self.db.execute('SELECT * FROM cash_events WHERE mode=? ORDER BY day DESC',(mode,))],
            'marks':[dict(r) for r in self.db.execute('SELECT * FROM marks WHERE mode=? ORDER BY day',(mode,))]}
