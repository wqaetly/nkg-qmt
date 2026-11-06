import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from .config import AppError, atomic_json, now_cn, dec
from .market import Tushare
from .research import summarize, evaluate_review, implemented_dividends
from .selection import load_rules, select_universe


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def trading_context(client, today):
    value = datetime.strptime(today, "%Y%m%d")
    sessions = client.calendar((value-timedelta(days=35)).strftime("%Y%m%d"),(value+timedelta(days=25)).strftime("%Y%m%d"))
    past = [day for day in sessions if day<=today]
    future = [day for day in sessions if day>today]
    if not past or not future:
        raise AppError("无法确定最近与下一交易日。")
    return past[-1],future[0]


def screen(workspace, cfg, today, limit=None, refresh=False, progress=print):
    cache = workspace / "cache" / today
    if refresh:
        # A new immutable cache namespace avoids deleting or silently rewriting raw evidence.
        cache = cache / now_cn().strftime("%H%M%S%f")
    client = Tushare(cache)
    day,next_day = trading_context(client,today)
    basics,prices = client.basics(),client.prices(day)
    rules=load_rules()
    members,checks,universe_count=select_universe(client,day,basics,prices,rules,workspace,progress)
    codes = members if limit is None else members[:limit]
    result = {"generated_at":now_cn().isoformat(),"requested_date":today,"as_of":day,"next_session":next_day,
              "pool_date":day,"pool_source":"沪深主板 → 估值、历史派息与 20 日流动性初筛 → 财报研究",
              "universe_count":universe_count,"preliminary_count":sum(c['coarse_pass'] for c in checks.values()),
              "selection_checks":checks,"strategy_version":rules['version'],"selection_rules":rules,
              "pool_size":len(members),"covered":len(codes),"rows":[],"prices":prices,
              "industries":{s:b.get("industry") or "待核实" for s,b in basics.items()},
              "source":"Tushare", "cache_path":str(cache), "complete":len(codes)==len(members)}
    for index,code in enumerate(codes,1):
        dossier = client.dossier(code,day)
        atomic_json(workspace / "dossiers" / day / f"{code}.json",dossier)
        row = summarize(dossier,basics.get(code,{}),prices.get(code,{}))
        row['selection']=checks[code]
        review_path = workspace / "reviews" / f"{code}.json"
        try:
            review = read_json(review_path) if review_path.exists() else None
            row["scenarios"],row["review_note"] = evaluate_review(row,review,cfg)
            if row["scenarios"]:
                row["status"] = "可进一步评估"
                row["review_hash"] = hashlib.sha256(json.dumps(review,sort_keys=True).encode()).hexdigest()
        except (AppError,KeyError,TypeError,ValueError) as exc:
            row["review_note"] = str(exc) if isinstance(exc,AppError) else "研究确认文件结构无效"
        result["rows"].append(row)
        if index==1 or index%10==0 or index==len(codes):
            progress(f"已核查结构化数据 {index}/{len(codes)}（财报研究批次 {len(members)}）",flush=True)
    atomic_json(workspace / "screen.json",result)
    atomic_json(workspace / "history" / f"screen-{day}.json",result)
    return result


def update_ledger(workspace, ledger, report):
    day = report["as_of"]
    client = Tushare(Path(report["cache_path"]))
    pending = [p for p in ledger.open_plans("paper") if p["execute_date"]<=day]
    # Include sold holdings: record-date ownership can still create a later cash entitlement.
    codes = set(ledger.positions("paper")) | {p["symbol"] for p in pending}
    codes |= {r[0] for r in ledger.db.execute("SELECT DISTINCT p.symbol FROM fills f JOIN plans p ON p.id=f.plan_id WHERE f.mode='paper'")}
    codes |= {r[0] for r in ledger.db.execute("SELECT symbol FROM openings WHERE mode='paper'")}
    events = {}
    for symbol in codes:
        path = workspace / "dossiers" / day / f"{symbol}.json"
        dossier = read_json(path) if path.exists() else client.dossier(symbol,day)
        if "dividend" in dossier["errors"]:
            raise AppError(f"{symbol} 分红数据不可用，账本更新暂停。")
        events[symbol] = implemented_dividends(dossier["data"]["dividend"],day)
    for symbol,items in events.items():
        for event in items:
            if (event.get('pay_date') or event['ex_date'])>ledger.account('paper')['start_date'] and event["ex_date"]<=day and dec(event["stk_div"])!=0 and (
                    ledger.positions("paper",event["record_date"]).get(symbol,0) or
                    any(p["symbol"]==symbol and p["execute_date"]<=event["record_date"] for p in pending)):
                raise AppError("持仓/计划涉及送转股，需处理股本变动后再继续模拟。")
    bars = {}
    for symbol in {p["symbol"] for p in pending}:
        start = min(p["execute_date"] for p in pending if p["symbol"]==symbol)
        bars[symbol] = client.bars(symbol,start,day)
    # Settle chronologically so a later cash dividend cannot fund an earlier purchase.
    dates = sorted({p["execute_date"] for p in pending} | {e["pay_date"] for v in events.values() for e in v if e.get("pay_date") and e["pay_date"]<=day} | {day})
    for date in dates:
        ledger.dividends(events,date)
        ledger.settle_paper(date,bars)
        ledger.dividends(events,date)
    for mode in ("paper","manual"):
        if ledger.db.execute("SELECT 1 FROM accounts WHERE mode=?",(mode,)).fetchone():
            ledger.mark(mode,day,report["prices"])
