import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.contracts import visible
from app.domain import OPERATIONS, seed_dataset
from app.repository import digest
from app.service import Service


class BusinessContractTests(unittest.TestCase):
    def setUp(self):
        self.data = seed_dataset()
        self.operation = next(iter(OPERATIONS))

    def run_check(self):
        return OPERATIONS[self.operation](self.data, {})

    def test_triangle_counts_actual_reported_cases(self):
        result = self.run_check()
        self.assertEqual(sum(row['reported'] for row in result['reported_triangle']), result['cases'])
        self.assertTrue(all(row['delay'] >= 0 for row in result['reported_triangle']))
        self.assertTrue(all(row['occurred_week'] >= 28 for row in self.data['cases'] if row['channel'] == 'new'))

    def test_earlier_cutoff_cannot_see_later_reports(self):
        early = OPERATIONS[self.operation](visible(self.data, '2026-06-01'), {})
        self.assertLess(early['cases'], self.run_check()['cases'])
        self.assertTrue(any(row['cause'] == 'unresolved' for row in self.data['cause_versions']))


    def test_fixture_timestamps_do_not_arrive_before_events(self):
        from app.contracts import instant
        for records in self.data.values():
            for row in records:
                if 'received_at' not in row:
                    continue
                for key in ('event_at', 'assigned_at', 'signed_at', 'paid_at', 'issued_at'):
                    if row.get(key):
                        self.assertLessEqual(instant(row[key]), instant(row['received_at']))

    def test_seed_and_live_source_have_one_contract(self):
        from app.contracts import load
        self.assertEqual(self.data, load('data/source.json'))
