from collections import Counter
from pathlib import Path

from app.contracts import latest, load
from app.repository import digest

TABLES = ['cases', 'cause_versions', 'shipment_counts', 'channel_rules']

def development_triangle(snapshot, options):
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

def action(repository, request):
    raise ValueError('尚未登记该业务的可写操作')

OPERATIONS = {'development_triangle': development_triangle}


def seed_dataset():
    return load(Path(__file__).resolve().parents[1] / 'data/source.json')
