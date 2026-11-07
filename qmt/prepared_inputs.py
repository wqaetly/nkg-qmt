"""Reuse sealed annual inputs for one registered policy change in a fresh account."""
import hashlib
import json
from pathlib import Path

from qmt.config import dec
from qmt.experiment import protocol, register_experiment, validate_experiment
from qmt.market import Tushare

RAW = ('cohort', 'market', 'financials', 'names', 'valuation-history', 'amounts', 'industry-history')


def checked_bytes(path, root, pinned):
    name = path.resolve().relative_to(root.resolve()).as_posix()
    payload = path.read_bytes()
    if name not in pinned or hashlib.sha256(payload).hexdigest() != pinned[name]:
        raise RuntimeError('Prepared source is changed or unregistered: ' + name)
    return payload


def comparable(value, changed_policy):
    normalized = {**value, 'valuation_policy': value.get('valuation_policy', 'historical_gate'),
                  'exit_policy': value.get('exit_policy', 'review_freeze'),
                  'rebalance_policy': value.get('rebalance_policy', 'monthly_only')}
    return {k: v for k, v in normalized.items() if k not in ('version', 'engine_sha256', changed_policy)}


def prepare_frozen(workspace, end, progress, cfg, experiment, *, baseline, root, pinned):
    validate_experiment(experiment)
    policies = (experiment.get('valuation_policy', 'historical_gate'), experiment.get('exit_policy', 'review_freeze'))
    if experiment.get('rebalance_policy', 'monthly_only') == 'after_confirmed_sale' and policies == ('historical_gate', 'known_failure_exit'):
        changed_policy = 'rebalance_policy'
    elif policies == ('historical_context', 'review_freeze'):
        changed_policy = 'valuation_policy'
    elif policies == ('historical_gate', 'known_failure_exit'):
        changed_policy = 'exit_policy'
    else:
        raise RuntimeError('Prepared replay requires one registered valuation, exit or rebalance policy change.')
    baseline_exit = 'known_failure_exit' if changed_policy == 'rebalance_policy' else 'review_freeze'
    source = workspace / 'backtests' / baseline
    target = workspace / 'backtests' / experiment['version']
    boundary = (workspace / 'backtests').resolve()
    if not source.resolve().is_relative_to(boundary) or not target.resolve().is_relative_to(boundary):
        raise RuntimeError('Prepared source or target leaves the backtest directory.')
    if source.resolve() == target.resolve():
        raise RuntimeError('Prepared inputs must be replayed into a new account.')
    old = json.loads(checked_bytes(source / 'protocol.json', root, pinned))
    if (old.get('version') != baseline or old.get('valuation_policy', 'historical_gate') != 'historical_gate'
            or old.get('exit_policy', 'review_freeze') != baseline_exit
            or old.get('rebalance_policy', 'monthly_only') != 'monthly_only'
            or comparable(old, changed_policy) != comparable(protocol(cfg, experiment, end), changed_policy)):
        raise RuntimeError('Prepared source differs beyond version, engine and the registered policy.')
    cohort = json.loads(checked_bytes(source / 'cohort.json', root, pinned))
    children = [{**experiment, 'version': experiment['version'] + '/cohorts/' + row['day'],
                 'selection_day': row['day']} for row in cohort['rebalances']]
    pairs = [(source, target, experiment), *[(source / 'cohorts' / item['selection_day'],
             target / 'cohorts' / item['selection_day'], item) for item in children]]
    payloads = []
    for previous, destination, item in pairs:
        expected = protocol(cfg, item, end)
        if destination.exists() and {p.name for p in destination.iterdir()} - {'protocol.json', 'registration.json'}:
            raise RuntimeError('Prepared target has previous products; retry refused.')
        if (destination / 'protocol.json').exists() and json.loads((destination / 'protocol.json').read_bytes()) != expected:
            raise RuntimeError('Prepared target has a conflicting registration.')
        previous_protocol = json.loads(checked_bytes(previous / 'protocol.json', root, pinned))
        if (previous_protocol.get('valuation_policy', 'historical_gate') != 'historical_gate'
                or previous_protocol.get('exit_policy', 'review_freeze') != baseline_exit
                or previous_protocol.get('rebalance_policy', 'monthly_only') != 'monthly_only'
                or comparable(previous_protocol, changed_policy) != comparable(expected, changed_policy)):
            raise RuntimeError('An annual prepared protocol differs from the registered source.')
        for name in RAW:
            path = destination / (name + '.json')
            if path.exists():
                raise RuntimeError('Prepared output already exists; retry refused: ' + str(path))
            payloads.append((path, checked_bytes(previous / path.name, root, pinned)))
    # Check every source before creating outputs; never copy prior signals, orders or results.
    for _, _, item in pairs:
        register_experiment(workspace, end, cfg, item)
    for path, payload in payloads:
        with path.open('xb') as stream:
            stream.write(payload)
    values = {name: json.loads((target / (name + '.json')).read_bytes()) for name in RAW}
    amounts = {code: {day: dec(value) for day, value in rows.items()} for code, rows in values['amounts'].items()}
    sectors = values['industry-history']
    progress(f'复用封存年度输入：{len(children)} 期、{len(cohort["stocks"])} 只历史股票；重新生成信号和成交', flush=True)
    return (target, Tushare(workspace / 'backtests' / 'source-cache'), values['cohort'], values['market'],
            values['financials'], values['names'], values['valuation-history'], amounts, sectors['stocks'], sectors['peers'])


def prepare_source_corrected(workspace, end, progress, cfg, experiment, *, baseline, root, pinned,
                             cohort_probe, coverage):
    """Apply the pinned source-only cohort correction without fetching or reselecting."""
    from qmt.experiment import history_bounds
    from qmt.industry import industry_at
    from qmt.market import ROW_LIMITS
    from qmt.research import implemented_dividends

    def require(ok, message):
        if not ok:
            raise RuntimeError(message)

    def read(path):
        return json.loads(checked_bytes(Path(path), root, pinned))

    def canonical(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))

    def encode(value):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()

    validate_experiment(experiment)
    source = workspace / 'backtests' / baseline
    target = workspace / 'backtests' / experiment['version']
    boundary = (workspace / 'backtests').resolve()
    require(source.resolve().is_relative_to(boundary) and target.resolve().is_relative_to(boundary)
            and source.resolve() != target.resolve(), 'Source correction requires a new in-workspace account.')
    design = read(root / coverage)
    probe = read(root / cohort_probe)
    require(design['status'] == 'source_inputs_covered_no_replay'
            and design['probe']['result_path'] == str(cohort_probe)
            and design['probe']['result_sha256'] == pinned[str(cohort_probe)]
            and probe['status'] == 'cohort_financial_source_comparison_complete'
            and probe['returns_computed'] is False and probe['orders_computed'] is False,
            'Source correction lacks a completed, pinned non-trading probe.')
    plan_name = probe['plan']['path']
    require(probe['plan']['sha256'] == pinned.get(plan_name)
            and design['probe']['plan_path'] == plan_name
            and design['probe']['plan_sha256'] == pinned.get(plan_name), 'Source probe preparation differs.')
    plan = read(root / plan_name)
    require({'qmt/research.py', 'qmt/automatic.py', 'qmt/experiment.py', 'qmt/disclosures.json'}
            <= plan['current_code_sha256'].keys(), 'Source probe is missing financial code or registry pins.')
    for name, digest in plan['current_code_sha256'].items():
        require(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest,
                'Source probe code or registry changed: ' + name)
    for name, digest in design['files_sha256'].items():
        require(pinned.get(name) == digest, 'Coverage evidence is not registered: ' + name)
    changes = [c for c in probe['cases'] if c['quality_changed']]
    require(len(changes) == len(design['groups'])
            and len({g['baseline'] for g in design['groups']}) == len(design['groups'])
            and all(c['old_cohort_exact_match'] and (c['quality_changed'] or
                c['old_registry']['quality'] == c['new_registry']['quality']) for c in probe['cases'])
            and {(c['version'].split('/cohorts/')[0], c['day'], c['symbol']) for c in changes}
                == {(g['baseline'], g['target_day'], g['symbol']) for g in design['groups']}
            and all(c['day'] == '20241231' and c['symbol'] == '600123.SH'
                    and c['eligible_changed'] and not c['old_registry']['quality']['eligible']
                    and c['new_registry']['quality']['eligible'] for c in changes),
            'Probe changes exceed the registered source correction.')
    groups = [g for g in design['groups'] if g['baseline'] == baseline]
    require(len(groups) == 1, 'Missing or duplicated source coverage group.')
    group = groups[0]; day = group['target_day']; code = group['symbol']
    require((root / group['donor_path']).resolve() == source.resolve()
            and (root / group['target_child_path']).resolve() == (source / 'cohorts' / day).resolve(),
            'Source coverage points at another account.')
    changed = next(c for c in changes if c['version'] == baseline + '/cohorts/' + day)
    prepared_case = next(c for c in plan['cases'] if c['version'] == changed['version'] and c['symbol'] == code)
    original_order = next(c['original_candidate_order'] for c in probe['cohorts'] if c['version'] == changed['version'])
    parent = read(source / 'cohort.json')
    children = [{**experiment, 'version': experiment['version'] + '/cohorts/' + row['day'],
                 'selection_day': row['day']} for row in parent['rebalances']]
    pairs = [(Path('.'), experiment), *[(Path('cohorts') / c['selection_day'], c) for c in children]]
    payloads = {}
    for suffix, item in pairs:
        destination = target / suffix
        expected = protocol(cfg, item, end)
        previous = read(source / suffix / 'protocol.json')
        require(previous['version'] == baseline + ('' if suffix == Path('.') else '/' + suffix.as_posix())
                and previous.get('exit_policy') == expected.get('exit_policy') == 'known_failure_exit'
                and previous.get('valuation_policy', 'historical_gate') == 'historical_gate'
                and comparable(previous, None) == comparable(expected, None),
                'Source correction changes policy, dates or account configuration.')
        require(not destination.exists() or not ({p.name for p in destination.iterdir()}
                    - {'protocol.json', 'registration.json'}), 'Prepared target has prior products; retry refused.')
        require(not (destination / 'protocol.json').exists()
                or json.loads((destination / 'protocol.json').read_bytes()) == expected,
                'Prepared target has conflicting registration.')
        for name in RAW:
            payloads[suffix, name] = checked_bytes(source / suffix / (name + '.json'), root, pinned)

    def value(suffix, name):
        return json.loads(payloads[suffix, name])

    def put(suffix, name, data):
        payloads[suffix, name] = encode(data)

    child_suffix = Path('cohorts') / day
    child = value(child_suffix, 'cohort')
    chosen = {s['symbol']: s for s in child['stocks']}
    rejected = {s['symbol']: s for s in child['rejected']}
    require(len(chosen) == len(child['stocks']) and len(rejected) == len(child['rejected'])
            and not chosen.keys() & rejected.keys() and set(original_order) == chosen.keys() | rejected.keys()
            and len(original_order) == child['candidate_count'] == child['evaluated_count']
            and [s for s in original_order if s in chosen] == list(chosen)
            and [s for s in original_order if s in rejected] == list(rejected)
            and rejected.get(code) == {'symbol': code, **changed['old_registry']['quality']},
            'Original cohort or candidate order differs from the source probe.')
    require((len(chosen), len(rejected), child['candidate_count'], child['evaluated_count'])
            == (group['selected_before'], group['rejected_before'], group['target_candidate_count'], group['target_evaluated_count']),
            'Original cohort counts differ from verified coverage.')
    donor = {name: value(Path('.'), name) for name in RAW if name != 'cohort'}
    sector = industry_at(donor['industry-history']['stocks'][code], day)
    require(sector is not None and sector['label'] == group['industry_label']
            and any(s['symbol'] == code for s in parent['stocks'])
            and len(parent['stocks']) == group['parent_union_count_before'] == group['parent_union_count_after'],
            'New annual member lacks the existing parent identity or industry.')
    chosen[code] = {'symbol': code, 'name': prepared_case['basic']['name'], 'industry': sector['label'],
                    'selection_quote': prepared_case['quote'],
                    'selection_financial_period': changed['new_registry']['summary']['latest_period'],
                    'selection_quality': changed['new_registry']['quality']}
    del rejected[code]
    child['stocks'] = [chosen[s] for s in original_order if s in chosen]
    child['rejected'] = [rejected[s] for s in original_order if s in rejected]
    require((len(child['stocks']), len(child['rejected'])) == (group['selected_after'], group['rejected_after']),
            'Corrected cohort counts differ from verified coverage.')
    insertion = group['insertion']; index = child['stocks'].index(chosen[code])
    require(original_order.index(code) + 1 == insertion['original_candidate_index']
            and index == insertion['selected_index_0based']
            and (child['stocks'][index - 1]['symbol'] if index else None) == insertion['previous_symbol']
            and (child['stocks'][index + 1]['symbol'] if index + 1 < len(child['stocks']) else None) == insertion['next_symbol'],
            'Source-corrected candidate ranking differs from verified coverage.')
    put(child_suffix, 'cohort', child)

    windows = group['windows']
    start_year, valuation_start, amounts_start = history_bounds({**experiment, 'selection_day': day})
    require(windows == {'dossier_start_year': start_year, 'dossier_as_of': end, 'bars_start': day,
                        'valuation_start': valuation_start, 'amounts_start': amounts_start},
            'Source coverage has different historical input windows.')
    requests = {r['api']: r for r in group['dossier_requests']}
    require(len(requests) == len(group['dossier_requests']) == 6, 'Incomplete exact dossier request set.')
    def frozen_call(api, params, fields):
        item = requests.get(api)
        require(item is not None and [item['params'], item['fields']] == [params, fields],
                'Missing exact frozen dossier request.')
        path = root / item['path']
        key = hashlib.sha256(json.dumps([api, params, fields], sort_keys=True).encode()).hexdigest()
        require(path.name == api + '-' + key + '.json' and pinned.get(item['path']) == item['sha256'],
                'Dossier query identity differs.')
        cached = read(path); rows = cached.get('rows')
        require([cached.get('api'), cached.get('params'), cached.get('fields')] == [api, params, fields]
                and isinstance(rows, list) and len(rows) == item['row_count']
                and len(rows) < (ROW_LIMITS['dividend'] if api == 'dividend' else 100)
                and all(isinstance(r, dict) and set(fields.split(',')).issubset(r) for r in rows),
                'Incomplete or truncated frozen dossier cache.')
        return rows
    frozen_client = Tushare(workspace / 'backtests' / 'source-cache')
    frozen_client.call = frozen_call
    dossier = frozen_client.dossier(code, end, start_year=start_year)
    require(not dossier['errors'] and hashlib.sha256(canonical(dossier).encode()).hexdigest() == group['dossier_sha256'],
            'Frozen end-dossier differs or has unresolved data errors.')
    for name, item in (
            ('financials', dossier), ('names', donor['names'][code]),
            ('valuation-history', [r for r in donor['valuation-history'][code] if valuation_start <= r['trade_date'] <= end]),
            ('amounts', {d: v for d, v in donor['amounts'][code].items() if amounts_start <= d <= end})):
        data = value(child_suffix, name)
        require(code not in data or name == 'amounts', 'New member already has an unexpected child payload.')
        if name == 'amounts':
            require(len(data.get(code, {})) == group['child_existing_amount_count']
                    and all(item.get(d) == v for d, v in data.get(code, {}).items()),
                    'Original entry liquidity observations differ from the donor.')
        data[code] = item; put(child_suffix, name, data)
    sectors = value(child_suffix, 'industry-history')
    require(code not in sectors['stocks'] and sectors['peers'].get(code) == donor['industry-history']['peers'].get(code)
            and code in sectors['peers'], 'New member requires unverified peer or industry coverage.')
    sectors['stocks'][code] = donor['industry-history']['stocks'][code]
    put(child_suffix, 'industry-history', sectors)
    market = value(child_suffix, 'market')
    bars = {d: v for d, v in donor['market']['bars'][code].items() if day <= d <= end}
    require(set(bars) == set(market['sessions']) and code not in market['bars']
            and code not in donor['market']['errors'] and code not in market['errors'],
            'New member has missing sessions or unresolved market data.')
    market['bars'][code] = bars; market['industries'][code] = sector['label']
    market['dividends'][code] = implemented_dividends(
        [r for r in dossier['data']['dividend'] if not r.get('record_date') or r['record_date'] >= day],
        end, earliest_period='19000101')
    require(market['dividends'][code] == group['child_new_dividends'],
            'New child includes an unverified payment or a pre-start entitlement.')
    put(child_suffix, 'market', market)
    counts = group['coverage_counts']
    require(counts == {'bars': len(bars), 'valuation': len(value(child_suffix, 'valuation-history')[code]),
                      'amounts': len(value(child_suffix, 'amounts')[code]), 'names': len(donor['names'][code]),
                      'industry_intervals': len(sectors['stocks'][code])}, 'Donor coverage counts differ.')

    rebalance = next(r for r in parent['rebalances'] if r['day'] == day)
    original_child = json.loads(checked_bytes(source / child_suffix / 'cohort.json', root, pinned))
    require(rebalance['until'] == group['until'] and rebalance['symbols'] == [s['symbol'] for s in original_child['stocks']]
            and rebalance['rejected'] == original_child['rejected'], 'Changed annual membership differs.')
    rebalance['symbols'] = [s['symbol'] for s in child['stocks']]
    rebalance['rejected'] = child['rejected']
    put(Path('.'), 'cohort', parent)
    parent_market = donor['market']
    membership = [{'start': r['day'], 'end': r['until']} for r in parent['rebalances'] if code in r['symbols']]
    require([m for m in membership if m['start'] != day] == parent_market['universe_membership'][code]
            == group['parent_membership_before'] and group['parent_membership_add'] == {'start': day, 'end': group['until']},
            'Source correction changes another membership interval.')
    parent_market['universe_membership'][code] = membership
    put(Path('.'), 'market', parent_market)
    expected_paths = [source / 'market.json', *[source / 'cohorts' / d / 'market.json' for d in ('20221230', '20231229')]]
    require(set(group['cash_event_metadata_paths']) == {p.relative_to(root).as_posix() for p in expected_paths},
            'Cash metadata scope differs from the verified original event.')
    metadata_keys = {'event_kind', 'fiscal_components', 'correction_source', 'correction_source_sha256',
                     'correction_source_pages', 'correction_available_on', 'correction_note',
                     'correction_originals', 'correction_fingerprint'}
    for path in expected_paths:
        suffix = path.parent.relative_to(source)
        current = value(suffix, 'market'); old_events = current['dividends'][code]
        first = membership[0]['start'] if suffix == Path('.') else value(suffix, 'cohort')['selected_on']
        raw = value(suffix, 'financials')[code]['data']['dividend']
        normalized = implemented_dividends([r for r in raw if not r.get('record_date') or r['record_date'] >= first],
                                           end, earliest_period='19000101')
        require(len(normalized) == len(old_events), 'Source correction changes the payment event count.')
        changed_events = 0
        for old_event, new_event in zip(old_events, normalized):
            if canonical(old_event) != canonical(new_event):
                changed_events += 1
                require(new_event.get('end_date') == '20230930' and new_event.get('record_date') == '20240207'
                        and new_event.get('event_kind') == 'cash_basis'
                        and canonical({k: v for k, v in new_event.items() if k not in metadata_keys}) == canonical(old_event),
                        'Source correction changes an actual entitlement or another event.')
        require(changed_events == 1, 'Expected exactly one source-only cash event correction.')
        current['dividends'][code] = normalized; put(suffix, 'market', current)

    # All sources and derived payloads are validated before registering any destination.
    for _, item in pairs:
        register_experiment(workspace, end, cfg, item)
    for (suffix, name), payload in payloads.items():
        with (target / suffix / (name + '.json')).open('xb') as stream:
            stream.write(payload)
    values = {name: value(Path('.'), name) for name in RAW}
    amounts = {s: {d: dec(v) for d, v in rows.items()} for s, rows in values['amounts'].items()}
    sectors = values['industry-history']
    progress('复用封存原始数据并应用已核实年度现金来源：仅重建对应年度候选及分红依据', flush=True)
    return (target, Tushare(workspace / 'backtests' / 'source-cache'), values['cohort'], values['market'],
            values['financials'], values['names'], values['valuation-history'], amounts, sectors['stocks'], sectors['peers'])
