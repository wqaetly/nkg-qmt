"""A dated research cohort, separate from orders and verified investment conclusions."""
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import AppError, atomic_json, dec, now_cn
from .context import research_context
from .selection import automatic_path, load_rules
from .service import read_json


def research_pool(report, automatic, count=20):
    if (report.get('source')!='Tushare' or not report.get('complete') or automatic.get('day')!=report.get('as_of')
            or automatic.get('rules')!=report.get('selection_rules')):
        raise AppError('研究池需要同一天、同一规则的完整真实初筛与评估。')
    rows={r['symbol']:r for r in report['rows']}
    assessed={a['symbol']:a for a in automatic['assessments']}
    watch=load_rules()['research_watchlist']
    eligible=[a for a in assessed.values() if a['state'] not in ('排除','不适用')]
    ranked=sorted(eligible,key=lambda a:(a['state']!='满足价格条件',a['state']!='等待价格',
                  a.get('dimensions',{}).get('valuation',{}).get('state')!='通过',
                  -dec(a.get('estimated_yield') or '0'),a['symbol']))
    order=list(dict.fromkeys([c for c in watch if c in assessed]+[a['symbol'] for a in ranked]))
    chosen=[]; sectors=Counter()
    for code in order:
        a=assessed[code]; row=rows[code]
        if a['state'] in ('排除','不适用') or sectors[row['industry']]>=3:
            continue
        dimensions=a.get('dimensions',{})
        chosen.append({'symbol':code,'name':row['name'],'industry':row['industry'],'price':row['price'],
            'state':a['state'],'reason':a['reason'],'estimated_yield':a.get('estimated_yield'),
            'valuation':dimensions.get('valuation',{}),'liquidity':dimensions.get('liquidity',{}),
            'dividend':dimensions.get('dividend',{}),'evidence_hash':row.get('evidence_hash'),
            'source_urls':[e['correction_source'] for e in row.get('events',[]) if e.get('correction_source')],
            'pinned_research':code in watch})
        sectors[row['industry']]+=1
        if len(chosen)==count:break
    return {'version':'research-cohort-v2','as_of':report['as_of'],'target_count':count,'stocks':chosen,
        'method':'保留指定关注股；其余按全部门槛满足、财务可评估但等待价格、待核实依次研究。同组按机械情景股息率排序；每行业最多三只。排名不抵消风险门禁。',
        'target_cagr':'0.10','target_status':'待验证','source_universe_count':report.get('universe_count'),
        'limitations':['研究名单不是买入清单；不足二十只时不补不合格公司。','今日名单不得用于声称历史选股收益；历史回放需独立重建当时名单。',
                       '研究数量不代表实际持仓数量；资金和持仓上限以独立自动模拟账本所示配置为准。',
                       '年化10%为研究目标，尚未达到可验证的策略结论；不保证未来收益。']}


def run_lab(workspace, report, enrich=False, progress=print):
    auto=read_json(automatic_path(workspace)/'latest.json')
    result=research_pool(report,auto)
    fingerprint=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    result.update(fingerprint=fingerprint,generated_at=now_cn().isoformat())
    frozen=workspace/'research'/'cohorts'/f"{result['version']}-{result['as_of']}.json"
    if frozen.exists():
        if read_json(frozen)['fingerprint']!=fingerprint:
            raise AppError('当日研究名单已冻结且输入发生变化；使用新 workspace 保留独立实验。')
        result=read_json(frozen)
    else:
        atomic_json(frozen,result)
    atomic_json(workspace/'research'/'latest.json',result)
    progress(f"研究池已冻结 {len(result['stocks'])}/{result['target_count']} 只，年化目标仍待验证",flush=True)
    if enrich:
        rows={r['symbol']:r for r in report['rows']}
        def enrich_one(stock):
            code=stock['symbol']; path=workspace/'context'/f'{code}.json'
            previous=read_json(path) if path.exists() else {}
            if (previous.get('evidence_hash')==stock['evidence_hash'] and previous.get('as_of')==report['as_of']
                    and previous.get('sources') and previous.get('analysis') and not previous.get('warnings')):
                return code,previous
            context=research_context(rows[code])
            context.update(evidence_hash=stock['evidence_hash'],as_of=report['as_of'])
            atomic_json(path,context)
            return code,context
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures=[executor.submit(enrich_one,stock) for stock in result['stocks']]
            for i,future in enumerate(as_completed(futures),1):
                code,context=future.result()
                progress(f"资料草稿 {i}/{len(futures)} {code}：{len(context['sources'])} 条线索；{len(context['warnings'])} 项接口提示；原文尚待核查",flush=True)
    for stock in result['stocks']:
        context_path=workspace/'context'/f"{stock['symbol']}.json"
        context=read_json(context_path) if context_path.exists() else {}
        if context.get('as_of')==report['as_of'] and context.get('evidence_hash')==stock['evidence_hash']:
            stock['source_urls']=list(dict.fromkeys(stock['source_urls']+[s['url'] for s in context['sources']]))
            stock['research_note']='已生成草稿，原文尚待核查' if context['analysis'] else '资料不足，继续核查'
    atomic_json(workspace/'research'/'latest.json',result)
    from .charts import update_charts
    update_charts(workspace,report,{'focus_symbols':list(dict.fromkeys(auto['focus_symbols']+[s['symbol'] for s in result['stocks']]))},progress)
    return result
