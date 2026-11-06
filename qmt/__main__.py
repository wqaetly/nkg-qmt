import argparse
import json
import hashlib
import sys
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path

from .config import ROOT, AppError, load_env, load_config, atomic_json, now_cn, date_key, symbol_key, dec, workspace_lock
from .context import research_context
from .ledger import Ledger
from .report import render
from .research import review_template, evaluate_review
from .service import screen, read_json, update_ledger


def parser():
    p = argparse.ArgumentParser(description="NKG 红利研究工作台：Tushare 初筛 → 人工研究确认 → 模拟/手动记账。不会发单。")
    p.add_argument('--workspace',type=Path,default=ROOT/'data/capital-100k',help='独立数据目录，默认仓库 data/capital-100k/；旧 data/ 账本保留')
    sub = p.add_subparsers(dest='command',required=True)
    daily = sub.add_parser('daily',help='更新主板估值、流动性、财报与模拟账本和报告')
    daily.add_argument('--limit',type=int,help='仅用于调试的覆盖数量；默认完整筛选后的研究批次')
    daily.add_argument('--refresh',action='store_true',help='忽略当天缓存重新取数')
    sub.add_parser('report',help='根据已有数据重新生成本地报告')
    lab=sub.add_parser('lab',help='冻结二十只研究名单并更新K线，不运行定时任务或新增订单')
    lab.add_argument('--enrich',action='store_true',help='使用已配置搜索和AI补充公开资料草稿')
    backtest=sub.add_parser('backtest',help='独立历史二十股研究池与含费用回放，不更改模拟账本')
    backtest.add_argument('--long-history',action='store_true',help='从2014年末独立建池，回放2015年至今；另取更早财报与估值预热')
    backtest.add_argument('--quality-first',action='store_true',help='独立实验：财务门槛前置后选最多20只，保留旧股息率排序实验')
    backtest.add_argument('--annual-pool',action='store_true',help='财务门槛前置，每年末重新建池；同一账本连续回放，不重置现金')
    backtest.add_argument('--all-candidates',action='store_true',help='与 --annual-pool 同用：检查全部年末初筛候选，完整入场筛选后分配最多20只持仓')
    backtest.add_argument('--hold-band',action='store_true',help='与 --annual-pool 同用：月末对已有健康持仓使用3%保留门槛，新买仍按原条件')
    research = sub.add_parser('research',help='搜索公开资料并由已配置的 AI 生成研究草稿')
    research.add_argument('symbol',type=symbol_key)
    review = sub.add_parser('review-template',help='生成待人工核查的三情景模板，不覆盖已有模板')
    review.add_argument('symbol',type=symbol_key)
    approve = sub.add_parser('review-import',help='校验并保存已经由使用者核对原文的研究文件')
    approve.add_argument('file',type=Path)
    init = sub.add_parser('init-manual',help='初始化手动交易账本；现金指分配给本策略的可用现金')
    for name in ('cash','max-position','max-sector'):
        init.add_argument('--'+name,required=True)
    opening = sub.add_parser('opening',help='仅在生成计划前登记已有股票，不扣减已填写的可用现金')
    opening.add_argument('--mode',choices=('paper','manual'),required=True)
    opening.add_argument('--symbol',type=symbol_key,required=True)
    opening.add_argument('--quantity',type=int,required=True)
    opening.add_argument('--date',type=date_key,required=True)
    plan = sub.add_parser('plan',help='明确指定数量，生成下一交易日计划；不连接券商')
    plan.add_argument('--mode',choices=('paper','manual'),default='paper')
    plan.add_argument('--symbol',type=symbol_key,required=True)
    plan.add_argument('--side',choices=('buy','sell'),default='buy')
    plan.add_argument('--quantity',type=int,required=True)
    plan.add_argument('--scenario',choices=('悲观','基准','较好'),default='悲观')
    plan.add_argument('--required-yield',default='0.05')
    plan.add_argument('--price',help='可选更严格限价；卖出必须填写')
    plan.add_argument('--reason',default='',help='卖出必须写明依据，价格下跌不自动触发退出')
    fill = sub.add_parser('fill',help='回填中信已成交记录；未成交委托不能录入')
    fill.add_argument('--plan',type=int,required=True)
    for name in ('date','price','fee','reference'):
        fill.add_argument('--'+name,required=True)
    fill.add_argument('--quantity',type=int,required=True)
    cancel = sub.add_parser('close-plan',help='确认已核对所有成交、撤销剩余委托后关闭计划')
    cancel.add_argument('--plan',type=int,required=True)
    cancel.add_argument('--reason',required=True)
    cancel.add_argument('--broker-reconciled',action='store_true',required=True)
    cash = sub.add_parser('cash-event',help='手动记录券商实际到账分红或补扣税；不用于入金')
    cash.add_argument('--kind',choices=('dividend','tax'),required=True)
    for name in ('date','amount','reference','note'):
        cash.add_argument('--'+name,required=True)
    cash.add_argument('--symbol',type=symbol_key,required=True)
    sub.add_parser('demo',help='创建隔离的虚构示例，离线验证完整流程')
    return p


def selected(report, symbol):
    row = next((r for r in report.get('rows',[]) if r['symbol']==symbol),None)
    if not row:
        raise AppError('该代码不在本次已覆盖候选中，请先运行完整 daily。')
    return row


def main(argv=None):
    args = parser().parse_args(argv)
    with workspace_lock(args.workspace.resolve()):
        return run(args)


def run(args):
    load_env()
    cfg = load_config()
    if args.command=='demo':
        from .demo import create_demo
        print(create_demo(args.workspace/'demo',cfg))
        return 0
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True,exist_ok=True)
    if (workspace/'.demo-mode').is_file():
        raise AppError('演示目录不能用于真实数据或手动账本。')
    if (workspace/'.automatic-paper').is_file():
        raise AppError('自动模拟目录由 daily 管理，不能在其中手动录入或启用实盘账本。')
    ledger = Ledger(workspace/'ledger.sqlite3',cfg)
    report = read_json(workspace/'screen.json') if (workspace/'screen.json').exists() else {}
    status_path = workspace/'status.json'
    status = read_json(status_path) if status_path.exists() else {'ok':False,'message':'尚未完成数据更新'}
    today = now_cn().strftime('%Y%m%d')
    try:
        if not ledger.db.execute("SELECT 1 FROM accounts WHERE mode='paper'").fetchone():
            ledger.initialize('paper',cfg['paper_cash'],cfg['paper_max_position'],cfg['paper_max_sector'])
        if args.command=='daily':
            if now_cn().hour<21:
                raise AppError('每日任务请在北京时间 21:00 后运行，等待分红与财报数据更新。')
            if args.limit is not None and args.limit<1:
                raise AppError('--limit 必须为正整数。')
            status={'ok':False,'message':'正在更新数据；旧报告暂不可据此新增计划','at':now_cn().isoformat()}
            atomic_json(status_path,status)
            render(workspace,ledger,report,status)
            report = screen(workspace,cfg,today,args.limit,args.refresh)
            update_ledger(workspace,ledger,report)
            if report['complete']:
                from .automatic import run_automatic
                from .charts import update_charts
                automatic=run_automatic(workspace,report,cfg)
                update_charts(workspace,report,automatic)
                message=f"完成 {report['universe_count']} 家主板初筛、{report['covered']} 家财报研究与自动模拟检查；本次信号日订单 {len(automatic['plan_ids'])} 笔。机械假设不等于人工研究确认。"
            else:
                message=f"仅完成 {report['covered']}/{report['pool_size']} 家调试研究；未运行自动模拟。"
            status={'ok':True,'message':message,'at':now_cn().isoformat()}
            atomic_json(status_path,status)
        elif args.command=='backtest':
            from .experiment import run_experiment, EXPERIMENT, LONG_EXPERIMENT, QUALITY_EXPERIMENT, QUALITY_LONG_EXPERIMENT, ANNUAL_EXPERIMENT, ANNUAL_LONG_EXPERIMENT, HOLD_EXPERIMENT, HOLD_LONG_EXPERIMENT
            experiment=(QUALITY_LONG_EXPERIMENT if args.long_history else QUALITY_EXPERIMENT) if args.quality_first else (LONG_EXPERIMENT if args.long_history else EXPERIMENT)
            if args.annual_pool:experiment=ANNUAL_LONG_EXPERIMENT if args.long_history else ANNUAL_EXPERIMENT
            if args.hold_band:
                if not args.annual_pool:raise AppError('--hold-band 须与 --annual-pool 同用，另存实验，不修改日常规则。')
                experiment=HOLD_LONG_EXPERIMENT if args.long_history else HOLD_EXPERIMENT
            if args.all_candidates:
                if not args.annual_pool:raise AppError('--all-candidates 须与 --annual-pool 同用，持仓上限仍为20。')
                experiment={**experiment,'version':experiment['version']+'-all','cohort_limit':None}
            run_experiment(workspace,report['as_of'],cfg,experiment=experiment)
        elif args.command=='lab':
            from .lab import run_lab
            run_lab(workspace,report,args.enrich)
        elif args.command=='research':
            row = selected(report,args.symbol)
            result = research_context(row)
            atomic_json(workspace/'context'/f'{args.symbol}.json',result)
            print(f"研究草稿：{len(result['sources'])} 条线索；{len(result['warnings'])} 项提示。原文尚未核实。")
        elif args.command=='review-template':
            row = selected(report,args.symbol)
            path = workspace/'drafts'/f'{args.symbol}.json'
            if path.exists():
                raise AppError('模板已存在，未覆盖你的编辑。')
            atomic_json(path,review_template(row))
            print(path)
        elif args.command=='review-import':
            review = read_json(args.file)
            row = selected(report,symbol_key(review['symbol']))
            matrix,note = evaluate_review(row,review,cfg)
            if not matrix:
                raise AppError('文件仍是未确认草稿；不得导入为已核实。')
            atomic_json(workspace/'reviews'/f"{row['symbol']}.json",review)
            digest=hashlib.sha256(json.dumps(review,sort_keys=True).encode()).hexdigest()
            atomic_json(workspace/'reviews'/'versions'/f'{digest}.json',review)
            row.update(scenarios=matrix,review_note=note,status='可进一步评估',review_hash=digest)
            atomic_json(workspace/'screen.json',report)
            print('已保存研究确认；价格满足假设后仍需明确分配数量。')
        elif args.command=='init-manual':
            ledger.initialize('manual',args.cash,args.max_position,args.max_sector)
            print('手动账本已初始化；未连接券商。')
        elif args.command=='opening':
            if args.date>today:
                raise AppError('不能录入未来持仓。')
            ledger.opening(args.mode,args.symbol,args.quantity,args.date)
        elif args.command=='plan':
            if not status.get('ok') or report.get('requested_date')!=today or report.get('as_of')!=today:
                raise AppError('须先完成今天收盘后的 daily；历史报告、休市日和失败报告不能新增计划。')
            if args.side=='sell' and args.symbol in ledger.positions(args.mode):
                row={'symbol':args.symbol,'industry':report.get('industries',{}).get(args.symbol,'待核实')}
            else:
                row = selected(report,args.symbol)
            price = args.price
            if args.side=='buy':
                review_path=workspace/'reviews'/f'{args.symbol}.json'
                review=read_json(review_path) if review_path.exists() else None
                matrix,note=evaluate_review(row,review,cfg)
                entry=next((s for s in matrix if s['scenario']==args.scenario and dec(s['required_yield'])==dec(args.required_yield)),None)
                if not entry:
                    raise AppError('没有匹配的已确认情景。')
                cap=dec(entry['reference_price_2dp_floor'])
                if dec(row['price'])>cap:
                    raise AppError('收盘价尚未满足该情景股息率门槛，继续观察。')
                price=dec(price,positive=True) if price else min(cap,dec(row['price'])).quantize(Decimal('0.01'),rounding=ROUND_FLOOR)
                if price>cap:
                    raise AppError('限价超过已确认情景参考上限。')
                row['scenarios']=matrix
                row['review_hash']=hashlib.sha256(json.dumps(review,sort_keys=True).encode()).hexdigest()
                atomic_json(workspace/'reviews'/'versions'/f"{row['review_hash']}.json",review)
                reason=f"{args.scenario}情景，税前股息率要求 {args.required_yield}；使用者指定数量"
            else:
                if not price or not args.reason.strip():
                    raise AppError('卖出须填写 --price 和 --reason。')
                reason=args.reason
            plan_id=ledger.create_plan(args.mode,row,today,report['next_session'],args.quantity,price,args.side,reason,report['prices'],report['industries'])
            print(f'已建立 {args.mode} 计划 #{plan_id}，仅 {report["next_session"]} 有效；没有发送任何委托。')
        elif args.command=='fill':
            if date_key(args.date)>today:
                raise AppError('不能记录未来成交。')
            changed=ledger.record_manual(args.plan,args.date,args.quantity,args.price,args.fee,args.reference)
            print('已回填实际成交。' if changed else '同一成交已记录，本次未重复入账。')
        elif args.command=='close-plan':
            ledger.close_plan(args.plan,args.reason)
        elif args.command=='cash-event':
            if date_key(args.date)>today:
                raise AppError('不能记录未来现金流水。')
            amount=Decimal(args.amount)
            if not amount.is_finite() or (args.kind=='tax' and amount>=0) or (args.kind=='dividend' and amount<=0):
                raise AppError('现金分红填正金额，税款补扣填负金额。')
            ledger.cash_event(args.reference,args.date,args.amount,args.symbol,args.note)
        print(render(workspace,ledger,report,status))
        return 0
    except (AppError,ValueError,KeyError,TypeError,InvalidOperation,OSError) as exc:
        message=str(exc) if isinstance(exc,AppError) else '本地输入/文件结构无效，请检查参数或 JSON；未展示敏感原文。'
        if args.command=='daily':
            status={'ok':False,'message':'本次更新暂停：'+message,'at':now_cn().isoformat()}
            atomic_json(status_path,status)
            render(workspace,ledger,report,status)
        elif args.command=='backtest':
            atomic_json(workspace/'backtests'/'latest.json',{'status':'blocked','as_of':report.get('as_of'),
                        'message':'本次历史实验暂停：'+message,'runs':[],'limitations':['未完成的区间不输出收益，旧实验文件保留。']})
            render(workspace,ledger,report,status)
        print('错误：'+message,file=sys.stderr)
        return 1
    finally:
        ledger.close()


if __name__=='__main__':
    try:
        sys.exit(main())
    except (AppError,ValueError,KeyError,OSError) as exc:
        print('错误：'+(str(exc) if isinstance(exc,AppError) else '无法载入配置或数据目录。'),file=sys.stderr)
        sys.exit(1)
