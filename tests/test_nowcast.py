import copy
import math
import tempfile
import unittest
from pathlib import Path

from app.contracts import visible
from app.domain import TABLES, occurrence_nowcast, seed_dataset
from app.repository import Conflict
from app.service import Service

AS_OF = '2026-09-30T23:59:59Z'


def run_nowcast(dataset=None, as_of=AS_OF, options=None):
    dataset = dataset if dataset is not None else seed_dataset()
    return occurrence_nowcast(visible(dataset, as_of), options or {}, as_of)


def channel(result, name):
    return next(entry for entry in result['channels'] if entry['channel'] == name)


class NowcastDecompositionTests(unittest.TestCase):
    def test_buckets_reconcile_per_week_and_total(self):
        result = run_nowcast()
        for entry in result['channels']:
            if entry['status'] != 'estimable':
                continue
            for week in entry['weeks']:
                self.assertEqual(week['reported'], week['known_total'] + week['unresolved'])
                self.assertEqual(week['known_total'], sum(week['known'].values()))
                self.assertAlmostEqual(week['total']['point'],
                                       week['reported'] + week['unreported']['point'], places=2)
                self.assertAlmostEqual(week['total']['point'],
                                       sum(cause['point'] for cause in week['final_causes'].values()), places=2)
                self.assertLessEqual(week['total']['lower'], week['total']['point'])
                self.assertLessEqual(week['total']['point'], week['total']['upper'])
            totals = entry['totals']
            self.assertEqual(totals['reported'], sum(week['reported'] for week in entry['weeks']))
            self.assertAlmostEqual(totals['total']['point'],
                                   totals['reported'] + totals['unreported']['point'], places=2)
            self.assertAlmostEqual(totals['total']['point'],
                                   sum(cause['point'] for cause in totals['final_causes'].values()), places=2)
            self.assertTrue(all(entry['reconciliation']['checks'].values()),
                            entry['reconciliation'])

    def test_known_and_unresolved_match_visible_records(self):
        dataset = visible(seed_dataset(), AS_OF)
        latest_cause = {}
        for row in dataset['cause_versions']:
            key = row['case_id']
            if key not in latest_cause or (row['revision'], row['received_at']) > (
                    latest_cause[key]['revision'], latest_cause[key]['received_at']):
                latest_cause[key] = row
        for entry in run_nowcast()['channels']:
            cases = [row for row in dataset['cases'] if row['channel'] == entry['channel']]
            unresolved = sum(latest_cause.get(row['case_id'], {}).get('cause', 'unresolved') == 'unresolved'
                             for row in cases)
            self.assertEqual(entry['totals']['reported'], len(cases))
            self.assertEqual(entry['totals']['unresolved'], unresolved)
            self.assertEqual(entry['totals']['known_total'], len(cases) - unresolved)

    def test_mature_weeks_collapse_to_actuals(self):
        entry = channel(run_nowcast(), 'direct')
        mature = [week for week in entry['weeks'] if week['age'] >= 4]
        self.assertTrue(mature)
        for week in mature:
            self.assertEqual(week['coverage'], 1.0)
            self.assertEqual(week['unreported']['point'], 0.0)
            self.assertEqual(week['unreported']['upper'], 0.0)
            self.assertEqual(week['total']['point'], week['reported'])

    def test_zero_reported_week_gets_tail_upper_bound(self):
        entry = channel(run_nowcast(), 'direct')
        week35 = next(week for week in entry['weeks'] if week['occurred_week'] == 35)
        self.assertEqual(week35['reported'], 0)
        self.assertEqual(week35['basis'], 'delay_scaling')
        self.assertEqual(week35['unreported']['point'], 0.0)
        expected_upper = math.log(0.025) / math.log(1 - 95 / 121)
        self.assertAlmostEqual(week35['unreported']['upper'], round(expected_upper, 3), places=3)

    def test_youngest_weeks_use_channel_level_not_pooled(self):
        entry = channel(run_nowcast(), 'direct')
        fallback = [week for week in entry['weeks'] if week['basis'] == 'channel_mature_week_level']
        self.assertEqual([week['occurred_week'] for week in fallback], [37, 38])
        mature_totals = [week['reported'] for week in entry['weeks'] if week['age'] >= 4]
        for week in fallback:
            self.assertEqual(week['reported'], 0)
            self.assertEqual(week['unreported']['lower'], min(mature_totals))
            self.assertEqual(week['unreported']['upper'], max(mature_totals))
            self.assertAlmostEqual(week['unreported']['point'],
                                   round(sum(mature_totals) / len(mature_totals), 3), places=3)


class NowcastBorrowingTests(unittest.TestCase):
    def test_new_channel_refuses_borrowing_with_evidence(self):
        entry = channel(run_nowcast(), 'new')
        self.assertEqual(entry['status'], 'insufficient_history')
        self.assertIsNone(entry['totals']['unreported'])
        self.assertIsNone(entry['totals']['total'])
        self.assertIsNone(entry['totals']['final_causes'])
        self.assertTrue(all(week['unreported'] is None for week in entry['weeks']))
        self.assertGreater(entry['totals']['reported'], 0)

        borrowing = entry['borrowing']
        self.assertFalse(borrowing['used'])
        self.assertEqual(borrowing['own_delay_range'], [8, 9])
        self.assertEqual(borrowing['other_channels_delay_range'], [2, 7])
        self.assertFalse(borrowing['ranges_overlap'])
        self.assertTrue(any('不重叠' in reason for reason in borrowing['refused_reasons']))
        self.assertTrue(any('总体延迟分布' in reason for reason in borrowing['refused_reasons']))

        maturity = entry['maturity']
        self.assertLess(maturity['fully_developed_weeks'],
                        maturity['fully_developed_weeks_required'])
        self.assertIsNone(maturity['cause_shares'])

    def test_maturity_boundary_uses_observed_tail(self):
        dataset = seed_dataset()
        early = run_nowcast(dataset, '2026-02-16T23:59:59Z')
        self.assertEqual(channel(early, 'direct')['status'], 'insufficient_history')
        later = run_nowcast(dataset, '2026-02-23T23:59:59Z')
        direct = channel(later, 'direct')
        self.assertEqual(direct['status'], 'estimable')
        self.assertIsNone(direct['totals']['final_causes'],
                          '成熟已决案件不足时只给三段分解，不分摊最终原因')

    def test_unknown_option_rejected(self):
        with self.assertRaises(ValueError):
            occurrence_nowcast(seed_dataset(), {'bogus': 1}, AS_OF)

    def test_empty_snapshot_does_not_crash(self):
        result = occurrence_nowcast({table: [] for table in TABLES}, {}, AS_OF)
        self.assertEqual(result['channels'], [])
        self.assertEqual(result['as_of_week'], 38)

    def test_source_data_is_not_mutated(self):
        dataset = seed_dataset()
        before = copy.deepcopy(dataset)
        occurrence_nowcast(dataset, {}, AS_OF)
        self.assertEqual(dataset, before)


class NowcastPointInTimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source.json'
        self.source.write_text(Path('data/source.json').read_text())
        self.service = Service(self.source, Path(self.temporary.name) / 'state.sqlite3')

    def request(self, request_id, as_of):
        return {'request_id': request_id, 'operation': 'occurrence_nowcast', 'as_of': as_of}

    def test_retrospective_uses_only_then_visible_records(self):
        early = self.service.run(self.request('meeting-week24', '2026-06-28T23:59:59Z'))
        late = self.service.run(self.request('meeting-week38', '2026-09-30T23:59:59Z'))
        self.assertNotIn('new', [entry['channel'] for entry in early['channels']],
                         '第24周时 new 渠道的渠道规则与案件都不可见')
        self.assertIn('new', [entry['channel'] for entry in late['channels']])
        self.assertLess(channel(early, 'direct')['totals']['reported'],
                        channel(late, 'direct')['totals']['reported'])
        self.assertNotEqual(early['dataset_digest'], late['dataset_digest'])

    def test_old_meeting_basis_is_not_rewritten(self):
        early = self.service.run(self.request('meeting-week24', '2026-06-28T23:59:59Z'))
        self.service.run(self.request('meeting-week38', '2026-09-30T23:59:59Z'))
        self.assertEqual(early, self.service.repository.analysis('meeting-week24'))
        self.source.write_text('{}')
        self.assertEqual(early, self.service.run(self.request('meeting-week24', '2026-06-28T23:59:59Z')))

    def test_conflicting_request_id_is_rejected(self):
        self.service.run(self.request('meeting', '2026-06-28T23:59:59Z'))
        with self.assertRaises(Conflict):
            self.service.run(self.request('meeting', '2026-09-30T23:59:59Z'))


if __name__ == '__main__':
    unittest.main()
