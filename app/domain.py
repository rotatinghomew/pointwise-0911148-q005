import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from app.contracts import instant, latest, load
from app.repository import digest

TABLES = ['cases', 'cause_versions', 'shipment_counts', 'channel_rules']

WEEK_ZERO = date(2026, 1, 5)

NOWCAST_DEFAULTS = {
    'mature_weeks_required': 4,
    'min_resolved_for_shares': 20,
    'interval_z': 1.96,
}

NOWCAST_METHOD = {
    'known': '截止前已受理且最新原因判断非 unresolved 的实际件数',
    'unresolved': '已受理但最新原因判断仍为 unresolved 的实际件数；复核提速只在本栏与已知栏之间移动件数，不改变总量',
    'unreported': '已报数除以该发生周年龄对应的累计报告比例；已报为零的周按二项尾概率给上界；年龄小于已观察最短延迟的周改用本渠道成熟周实际件数的最小到最大',
    'final_causes': '已知原因加上（原因未定与尚待报告）按本渠道成熟周已决原因比例分摊',
    'interval': '报告延迟不确定性按二项模型正态近似，半宽为 interval_z 倍标准差，下界不小于已报数；原因区间经同一分摊规则传导，不是各原因独立置信区间',
    'maturity': '完全成熟发生周数达到 mature_weeks_required 且最老发生周年龄超过已观察最大延迟才给出估计，否则拒绝借用总体分布并给出证据',
    'point_in_time': '只使用 received_at 不晚于 as_of 的记录；历史结果按 request_id 留存，新数据不改写旧周会依据',
}


def development_triangle(snapshot, options, as_of=None):
    cases = latest(snapshot['cases'], ['case_id'])
    causes = {row['case_id']: row['cause'] for row in latest(snapshot['cause_versions'], ['case_id'])}
    cells = Counter((row['channel'], row['occurred_week'], row['reported_week'] - row['occurred_week']) for row in cases)
    ages = [row['reported_week'] - row['occurred_week'] for row in cases]
    return {'cases': len(cases), 'with_cause': sum(causes.get(row['case_id'], 'unresolved') != 'unresolved' for row in cases),
            'channels': sorted({row['channel'] for row in cases}),
            'delay_range': [min(ages), max(ages)] if ages else None,
            'shipment_weeks': len(snapshot['shipment_counts']),
            'reported_triangle': [{'channel': key[0], 'occurred_week': key[1], 'delay': key[2], 'reported': count}
                                  for key, count in sorted(cells.items())]}


def _week_of(as_of):
    return (instant(as_of).date() - WEEK_ZERO).days // 7


def _rounded(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


def _interval(point, lower, upper):
    return {'point': point, 'lower': lower, 'upper': upper}


def _reconciliation(totals):
    checks = {'known_total_equals_cause_sum':
              abs(sum(totals['known'].values()) - totals['known_total']) <= 0.01,
              'reported_equals_known_plus_unresolved':
              abs(totals['reported'] - totals['known_total'] - totals['unresolved']) <= 0.01}
    residuals = {}
    if totals['total'] is not None:
        residual = totals['total']['point'] - totals['reported'] - totals['unreported']['point']
        checks['total_equals_reported_plus_unreported'] = abs(residual) <= 0.01
        checks['intervals_ordered'] = (totals['total']['lower'] <= totals['total']['point']
                                       <= totals['total']['upper'])
        residuals['total_minus_reported_unreported'] = round(residual, 6)
    if totals['final_causes'] is not None:
        residual = totals['total']['point'] - sum(item['point'] for item in totals['final_causes'].values())
        checks['final_causes_sum_to_total'] = abs(residual) <= 0.01
        residuals['total_minus_final_cause_sum'] = round(residual, 6)
    return {'checks': checks, 'residuals': residuals}


def _nowcast_channel(channel, rows, opened_week, as_of_week, causes, shipments, cause_names,
                     other_delays, required, min_resolved, z_value):
    weekly = defaultdict(list)
    for row in rows:
        weekly[row['occurred_week']].append(row)
    first_week = opened_week if opened_week is not None else min(weekly, default=as_of_week)
    last_week = max([as_of_week, *weekly])
    delays = [row['reported_week'] - row['occurred_week'] for row in rows]
    max_delay = max(delays) if delays else None
    own_range = [min(delays), max(delays)] if delays else None

    if max_delay is None:
        developed = []
    else:
        developed = [week for week in range(first_week, as_of_week + 1) if week + max_delay <= as_of_week]
    developed_set = set(developed)
    weeks_beyond = max(0, as_of_week - max_delay - first_week) if max_delay is not None else 0
    tail_closed = weeks_beyond >= 1
    mature_delay_counts = Counter(row['reported_week'] - row['occurred_week'] for row in rows
                                  if row['occurred_week'] in developed_set)
    mature_cases = sum(mature_delay_counts.values())
    delay_share = {delay: count / mature_cases for delay, count in mature_delay_counts.items()} if mature_cases else None
    mature_resolved = Counter(causes.get(row['case_id'], 'unresolved') for row in rows
                              if row['occurred_week'] in developed_set)
    mature_resolved.pop('unresolved', None)
    resolved_total = sum(mature_resolved.values())
    shares = None
    if resolved_total >= min_resolved:
        shares = {name: mature_resolved.get(name, 0) / resolved_total for name in cause_names}

    estimable = bool(delay_share) and len(developed) >= required and tail_closed
    level_totals = [len(weekly.get(week, [])) for week in developed]

    week_entries = []
    sum_known = Counter()
    sum_reported = 0
    sum_unresolved = 0
    sum_unreported = [0.0, 0.0, 0.0]
    for week in range(first_week, last_week + 1):
        rows_in_week = weekly.get(week, [])
        known_counter = Counter(causes.get(row['case_id'], 'unresolved') for row in rows_in_week)
        unresolved = known_counter.pop('unresolved', 0)
        known = {name: known_counter.get(name, 0) for name in cause_names}
        reported = len(rows_in_week)
        age = as_of_week - week
        shipment = shipments.get((channel, week))
        exposure = ({'status': shipment['status'], 'count': shipment['count']}
                    if shipment else {'status': 'absent', 'count': None})
        coverage = None
        basis = None
        total = None
        unreported = None
        if estimable:
            coverage = sum(share for delay, share in delay_share.items() if delay <= age)
            if coverage > 0:
                basis = 'delay_scaling'
                expected = reported / coverage
                if reported:
                    spread = z_value * math.sqrt(expected * (1 - coverage) / coverage)
                    total = _interval(expected, max(reported, expected - spread), expected + spread)
                else:
                    upper = 0.0 if coverage >= 1 else math.log(0.025) / math.log(1 - coverage)
                    total = _interval(0.0, 0.0, upper)
            else:
                basis = 'channel_mature_week_level'
                total = _interval(sum(level_totals) / len(level_totals),
                                  float(min(level_totals)), float(max(level_totals)))
            unreported = _interval(total['point'] - reported,
                                   total['lower'] - reported, total['upper'] - reported)
        final_causes = None
        if shares is not None and unreported is not None:
            final_causes = {name: _interval(known[name] + (unresolved + unreported['point']) * shares[name],
                                            known[name] + (unresolved + unreported['lower']) * shares[name],
                                            known[name] + (unresolved + unreported['upper']) * shares[name])
                            for name in cause_names}
        sum_known.update(known)
        sum_reported += reported
        sum_unresolved += unresolved
        if unreported is not None:
            sum_unreported[0] += unreported['point']
            sum_unreported[1] += unreported['lower']
            sum_unreported[2] += unreported['upper']
        week_entries.append({'occurred_week': week, 'age': age, 'reported': reported,
                             'known': known, 'known_total': sum(known.values()), 'unresolved': unresolved,
                             'coverage': coverage, 'basis': basis, 'exposure': exposure,
                             'unreported': unreported, 'total': total, 'final_causes': final_causes})

    totals = {'reported': sum_reported,
              'known': {name: sum_known.get(name, 0) for name in cause_names},
              'known_total': sum(sum_known.values()),
              'unresolved': sum_unresolved,
              'unreported': _interval(*sum_unreported) if estimable else None,
              'total': None,
              'final_causes': None}
    if estimable:
        totals['total'] = _interval(sum_reported + sum_unreported[0],
                                    sum_reported + sum_unreported[1],
                                    sum_reported + sum_unreported[2])
        if shares is not None:
            totals['final_causes'] = {
                name: _interval(totals['known'][name] + (sum_unresolved + sum_unreported[0]) * shares[name],
                                totals['known'][name] + (sum_unresolved + sum_unreported[1]) * shares[name],
                                totals['known'][name] + (sum_unresolved + sum_unreported[2]) * shares[name])
                for name in cause_names}

    maturity = {'opened_week': first_week, 'as_of_week': as_of_week,
                'reported_cases': len(rows), 'occurred_weeks_with_cases': len(weekly),
                'fully_developed_weeks': len(developed),
                'fully_developed_weeks_required': required,
                'weeks_beyond_max_delay': weeks_beyond,
                'observed_delay_range': own_range,
                'oldest_occurred_age': as_of_week - first_week,
                'tail_closed': tail_closed,
                'mature_cases': mature_cases,
                'delay_distribution': ([{'delay': delay, 'share': share}
                                        for delay, share in sorted(delay_share.items())]
                                       if delay_share else None),
                'mature_resolved_cases': resolved_total,
                'cause_shares': shares}

    borrowing = None
    if not estimable:
        other_range = [min(other_delays), max(other_delays)] if other_delays else None
        overlap = bool(own_range and other_range
                       and own_range[0] <= other_range[1] and other_range[0] <= own_range[1])
        reasons = []
        if not rows:
            reasons.append('本渠道尚无已受理案件，没有任何延迟证据')
        else:
            if len(developed) < required:
                reasons.append(f'完全成熟发生周 {len(developed)} 个，低于所需 {required} 个')
            if not tail_closed:
                reasons.append(f'最老发生周仅观察 {as_of_week - first_week} 周，未超过已观察最大延迟 '
                               f'{max_delay} 周，不能排除更长延迟')
            if not mature_cases:
                reasons.append('成熟发生周内没有已受理案件，无法建立延迟分布')
        if own_range and other_range and not overlap:
            reasons.append(f'其他渠道延迟 {other_range[0]}..{other_range[1]} 周与本渠道 '
                           f'{own_range[0]}..{own_range[1]} 周不重叠，总体分布不代表本渠道节律')
        reasons.append('不以总体延迟分布填充尚待报告量，待本渠道历史成熟后再估计')
        borrowing = {'used': False, 'own_delay_range': own_range,
                     'other_channels_delay_range': other_range, 'ranges_overlap': overlap,
                     'refused_reasons': reasons}

    entry = {'channel': channel,
             'status': 'estimable' if estimable else 'insufficient_history',
             'maturity': maturity, 'borrowing': borrowing,
             'totals': totals, 'weeks': week_entries}
    entry = _rounded(entry)
    entry['reconciliation'] = _reconciliation(entry['totals'])
    return entry


def occurrence_nowcast(snapshot, options, as_of=None):
    options = options or {}
    unknown = set(options) - set(NOWCAST_DEFAULTS)
    if unknown:
        raise ValueError('未知估计参数：' + ','.join(sorted(unknown)))
    required = options.get('mature_weeks_required', NOWCAST_DEFAULTS['mature_weeks_required'])
    min_resolved = options.get('min_resolved_for_shares', NOWCAST_DEFAULTS['min_resolved_for_shares'])
    z_value = options.get('interval_z', NOWCAST_DEFAULTS['interval_z'])
    if not (required >= 1 and min_resolved >= 1 and z_value > 0):
        raise ValueError('估计参数必须为正数')
    if as_of is None:
        stamps = [row['received_at'] for records in snapshot.values() for row in records if 'received_at' in row]
        as_of = max(stamps) if stamps else '1970-01-01T00:00:00Z'
    as_of_week = _week_of(as_of)

    cases = latest(snapshot['cases'], ['case_id'])
    causes = {row['case_id']: row['cause'] for row in latest(snapshot['cause_versions'], ['case_id'])}
    opened = {row['channel']: row['opened_week'] for row in latest(snapshot['channel_rules'], ['channel'])}
    shipments = {(row['channel'], row['week']): row
                 for row in latest(snapshot['shipment_counts'], ['channel', 'week'])}

    by_channel = defaultdict(list)
    for row in cases:
        by_channel[row['channel']].append(row)
    channels = sorted(set(by_channel) | set(opened))
    delays_by_channel = {name: [row['reported_week'] - row['occurred_week'] for row in by_channel.get(name, [])]
                         for name in channels}
    cause_names = sorted({cause for cause in causes.values() if cause != 'unresolved'})

    return {'as_of_week': as_of_week,
            'week_zero': WEEK_ZERO.isoformat(),
            'options': {'mature_weeks_required': required,
                        'min_resolved_for_shares': min_resolved,
                        'interval_z': z_value},
            'method': NOWCAST_METHOD,
            'causes': cause_names,
            'channels': [_nowcast_channel(name, by_channel.get(name, []), opened.get(name), as_of_week,
                                          causes, shipments, cause_names,
                                          [delay for other in channels if other != name
                                           for delay in delays_by_channel[other]],
                                          required, min_resolved, z_value)
                         for name in channels]}


def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')

OPERATIONS = {'development_triangle': development_triangle,
              'occurrence_nowcast': occurrence_nowcast}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
