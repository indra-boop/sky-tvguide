"""A multi-day Sky scrape must never replace a WITA day with a partial NZ slice."""

import contextlib
import copy
import csv
import io
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import ingest_window_to_dashboard as window
import scrape_sky_sports_guide as scrape


def fixtures(start_date=date(2026, 11, 27), days=8):
    first = datetime.combine(start_date, time.min, window.NZ).astimezone(timezone.utc)
    last = datetime.combine(start_date + timedelta(days=days + 1), time.min, window.NZ).astimezone(timezone.utc)
    slots = []
    cursor = first
    while cursor <= last:
        end = cursor + timedelta(hours=1)
        local = cursor.astimezone(window.NZ)
        slots.append({
            'country_code': 'NZ', 'channel_id': 'SPT1', 'channel_name': 'Sky Sport 1',
            'channel_display_name': '[NZ] Sky Sport 1', 'program_title': f'Sports slot {cursor.isoformat()}',
            'start_time': local.strftime('%I:%M%p').lstrip('0'),
            'end_time': end.astimezone(window.NZ).strftime('%I:%M%p').lstrip('0'),
            'start_utc': cursor.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'end_utc': end.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'scraped_at': '2026-11-26T19:00:00Z',
        })
        cursor += timedelta(hours=1)
    rows = []
    for offset in range(days + 1):
        guide = start_date + timedelta(days=offset)
        begin = datetime.combine(guide, time.min, window.NZ).astimezone(timezone.utc)
        finish = datetime.combine(guide + timedelta(days=1), time.min, window.NZ).astimezone(timezone.utc)
        for slot in slots:
            instant = datetime.fromisoformat(slot['start_utc'].replace('Z', '+00:00'))
            if begin <= instant <= finish:  # Sky may repeat next-midnight slot in adjacent guides.
                rows.append({**slot, 'date': guide.isoformat()})
    return rows


class WindowIngestTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.start = date(2026, 11, 27)
        self.days = 8
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def write(self, rows=None, start=None, days=None):
        start = start or self.start
        days = days or self.days
        path = Path(self.folder.name) / f'sports_{start}_to_{start + timedelta(days=days)}.csv'
        rows = fixtures(start, days) if rows is None else rows
        fields = list(dict.fromkeys(k for row in rows for k in row))
        with path.open('w', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return str(path)

    def test_nine_nz_dates_produce_eight_complete_non_overlapping_wita_days(self):
        for start in [self.start, date(2026, 7, 15), date(2026, 9, 25), date(2026, 4, 3)]:
            with self.subTest(start=start):
                batches = window.plan_batches(self.write(start=start), start, 8, min_rows_per_guide=1)
                self.assertEqual(8, len(batches))
                self.assertEqual([24] * 8, [len(rows) for _, rows in batches])
                all_starts = []
                for day, rows in batches:
                    starts = [datetime.fromisoformat(row['start_utc'].replace('Z', '+00:00')).astimezone(window.WITA) for row in rows]
                    self.assertEqual({day}, {instant.date().isoformat() for instant in starts})
                    self.assertEqual(list(range(24)), [instant.hour for instant in starts])
                    all_starts.extend(starts)
                self.assertEqual(192, len(set(all_starts)))
                self.assertEqual(start, all_starts[0].date())
                self.assertEqual(start + timedelta(days=7), all_starts[-1].date())

    def test_all_batches_validated_before_first_post_and_exact_day_confirmed(self):
        path = self.write()
        calls = []
        def post(url, token, rows, *, retries):
            day = datetime.fromisoformat(rows[0]['start_utc'].replace('Z', '+00:00')).astimezone(window.WITA).date().isoformat()
            calls.append(day)
            self.assertEqual(24, len(rows))
            return {'ok': True, 'received': len(rows), 'normalized': len(rows),
                    'window': {'start': day, 'end': day}}
        with patch.object(window, 'assert_compatible_aggregator') as probe, patch.object(window, '_post', side_effect=post):
            window.ingest_window(path, self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
        probe.assert_called_once()
        self.assertEqual(8, len(calls))
        self.assertEqual(8, len(set(calls)))

    def test_bad_last_guide_never_writes_even_when_earlier_days_are_valid(self):
        rows = fixtures()
        rows[-1]['start_utc'] = 'invalid'
        with patch.object(window, '_post') as post, patch.object(window, 'assert_compatible_aggregator') as probe:
            with self.assertRaises(window.IngestError):
                window.ingest_window(self.write(rows), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
        post.assert_not_called()
        probe.assert_not_called()

    def test_missing_guide_minimum_rows_and_wrong_filename_fail_closed(self):
        rows = [r for r in fixtures() if r['date'] != '2026-11-30']
        with self.assertRaisesRegex(window.IngestError, 'NZ guide 2026-11-30'):
            window.plan_batches(self.write(rows), self.start, 8, min_rows_per_guide=1)
        with self.assertRaisesRegex(window.IngestError, 'minimum'):
            window.plan_batches(self.write(), self.start, 8, min_rows_per_guide=100)
        with self.assertRaisesRegex(window.IngestError, 'CSV date guard'):
            window.plan_batches(self.write(), self.start - timedelta(days=1), 8, min_rows_per_guide=1)

    def test_exact_duplicate_midnight_slots_removed_but_conflicts_rejected(self):
        rows = fixtures()
        rows.append(dict(rows[0]))
        self.assertEqual(192, sum(len(batch) for _, batch in window.plan_batches(self.write(rows), self.start, 8, min_rows_per_guide=1)))
        conflict = dict(rows[20], program_title='Different programme at the same instant')
        with self.assertRaisesRegex(window.IngestError, 'conflicting duplicate'):
            window.plan_batches(self.write(rows + [conflict]), self.start, 8, min_rows_per_guide=1)

    def test_duplicate_rows_cannot_satisfy_minimum_or_trigger_ingest(self):
        # Repeating a truncated guide must not make it safe to replace a full day.
        rows = fixtures() * 7  # 175 rows per guide, but only 25 distinct slots.
        with patch.object(window, '_post') as post, patch.object(window, 'assert_compatible_aggregator') as probe:
            with self.assertRaisesRegex(window.IngestError, 'minimum 150'):
                window.ingest_window(self.write(rows), self.start, 8,
                                     url='https://example.invalid/api/ingest/v2',
                                     token='synthetic-test', min_rows_per_guide=150)
        probe.assert_not_called()
        post.assert_not_called()

    def test_minimum_counts_shared_midnight_slot_once_for_each_guide(self):
        # Adjacent guides each legitimately contain the same midnight slot.
        batches = window.plan_batches(self.write(fixtures() * 2), self.start, 8,
                                      min_rows_per_guide=25)
        self.assertEqual(8, len(batches))
        self.assertEqual([24] * 8, [len(rows) for _, rows in batches])

    def test_repeated_nz_clock_keeps_distinct_utc_source_keys(self):
        start = date(2026, 4, 3)
        rows = [{**row, 'program_title': 'Tennis'} for row in fixtures(start)]
        batches = window.plan_batches(self.write(rows, start=start), start, 8,
                                      min_rows_per_guide=1)
        repeated_clock = []
        for _, batch in batches:
            for row in batch:
                instant = datetime.fromisoformat(row['start_utc'].replace('Z', '+00:00'))
                nz = instant.astimezone(window.NZ)
                if nz.date() == date(2026, 4, 5) and nz.hour == 2:
                    repeated_clock.append(row)
        self.assertEqual(2, len(repeated_clock))
        self.assertEqual(repeated_clock[0]['start_time'], repeated_clock[1]['start_time'])
        self.assertNotEqual(repeated_clock[0]['source_key'], repeated_clock[1]['source_key'])

    def test_source_keys_are_stable_across_titles_and_distinct_between_channels(self):
        original = fixtures()
        rows = original + [{**row, 'channel_id': 'SPT2'} for row in original]
        before = window.plan_batches(self.write(rows), self.start, 8, min_rows_per_guide=1)
        changed = [{**row, 'program_title': 'Corrected programme title'} for row in rows]
        after = window.plan_batches(self.write(changed * 2), self.start, 8,
                                   min_rows_per_guide=1)
        keys_before = {row['source_key'] for _, batch in before for row in batch}
        keys_after = {row['source_key'] for _, batch in after for row in batch}
        self.assertEqual(384, len(keys_before))
        self.assertEqual(keys_before, keys_after)

    def test_invalid_utc_bounds_and_wrong_guide_date_are_rejected(self):
        for change in [
            {'start_utc': '2026-02-30T00:00:00Z'},
            {'start_utc': '2026-11-26T11:00:00'},
            {'end_utc': '2026-11-26T10:00:00Z'},
            {'start_utc': '2027-01-01T00:00:00Z', 'end_utc': '2027-01-01T01:00:00Z'},
        ]:
            rows = fixtures()
            rows[0].update(change)
            with self.subTest(change=change), self.assertRaises(window.IngestError):
                window.plan_batches(self.write(rows), self.start, 8, min_rows_per_guide=1)

    def test_row_and_byte_caps_are_not_bypassed(self):
        path = self.write()
        for name, cap in [('MAX_EVENTS_PER_SYNC', 23), ('MAX_BODY_BYTES', 20)]:
            with patch.object(window, name, cap), patch.object(window, '_post') as post:
                with self.assertRaises(window.IngestError):
                    window.ingest_window(path, self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
                post.assert_not_called()

    def test_dry_run_makes_no_network_calls(self):
        with patch.object(window, '_post') as post, patch.object(window, 'assert_compatible_aggregator') as probe:
            window.ingest_window(self.write(), self.start, 8, min_rows_per_guide=1, dry_run=True)
        post.assert_not_called()
        probe.assert_not_called()

    def test_missing_capabilities_and_failed_probe_never_post(self):
        for body in [{}, {'capabilities': {'skyAbsoluteUtc': True, 'ingestWindowByWitaDate': False}}, {'capabilities': []}]:
            with patch.object(window, 'urlopen', return_value=io.BytesIO(json.dumps(body).encode())), patch.object(window, '_post') as post:
                with self.assertRaisesRegex(window.IngestError, 'capabilities'):
                    window.ingest_window(self.write(), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
                post.assert_not_called()
        with patch.object(window, 'urlopen', side_effect=TimeoutError), patch.object(window, '_post') as post:
            with self.assertRaisesRegex(window.IngestError, 'compatibility check failed'):
                window.ingest_window(self.write(), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
            post.assert_not_called()

    def test_capability_probe_is_read_only_and_never_sends_bearer_token(self):
        body = {'capabilities': {'skyAbsoluteUtc': True, 'ingestWindowByWitaDate': True}}
        with patch.object(window, 'urlopen', return_value=io.BytesIO(json.dumps(body).encode())) as request:
            window.assert_compatible_aggregator('https://example.invalid/api/ingest/v2')
        req = request.call_args.args[0]
        self.assertEqual('GET', req.get_method())
        self.assertEqual('https://example.invalid/api/schedule/v2?days=1', req.full_url)
        self.assertFalse(req.has_header('Authorization'))

    def test_failed_post_stops_next_days_and_reports_completed_dates(self):
        ok = {'ok': True, 'received': 24, 'normalized': 24,
              'window': {'start': '2026-11-27', 'end': '2026-11-27'}}
        with patch.object(window, 'assert_compatible_aggregator'), patch.object(window, '_post', side_effect=[ok, window.IngestError('synthetic failure')]) as post:
            with self.assertRaisesRegex(window.IngestError, 'WITA 2026-11-28.*completed dates=2026-11-27'):
                window.ingest_window(self.write(), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
        self.assertEqual(2, post.call_count)

    def test_wrong_server_window_stops_subsequent_batches(self):
        wrong = {'ok': True, 'received': 24, 'normalized': 24,
                 'window': {'start': '2026-11-26', 'end': '2026-11-27'}}
        with patch.object(window, 'assert_compatible_aggregator'), patch.object(window, '_post', return_value=wrong) as post:
            with self.assertRaisesRegex(window.IngestError, 'exact WITA replacement date'):
                window.ingest_window(self.write(), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
        post.assert_called_once()

    def test_incomplete_or_invalid_receipt_counts_stop_subsequent_dates(self):
        first = {'ok': True, 'received': 24, 'normalized': 24,
                 'window': {'start': '2026-11-27', 'end': '2026-11-27'}}
        second = {**first, 'window': {'start': '2026-11-28', 'end': '2026-11-28'}}
        for field in ('received', 'normalized'):
            for value in (23, 25, None, True, 24.0, '24'):
                bad = {**second, field: value}
                with self.subTest(field=field, value=value), patch.object(window, 'assert_compatible_aggregator'), patch.object(window, '_post', side_effect=[first, bad]) as post:
                    with self.assertRaisesRegex(window.IngestError, 'WITA 2026-11-28:.*completed dates=2026-11-27;.*received and normalized'):
                        window.ingest_window(self.write(), self.start, 8,
                                             url='https://example.invalid/api/ingest/v2',
                                             token='synthetic-test', min_rows_per_guide=1)
                self.assertEqual(2, post.call_count)

    def test_scraper_failure_does_not_write_partial_csv(self):
        path = Path(self.folder.name) / 'partial.csv'
        with patch.object(scrape, '_find_sports_group_id', return_value='sports'), patch.object(scrape, '_scrape_day', side_effect=[[{'synthetic': 'row'}], scrape.SkyGuideError('missing day')]), patch.object(scrape, '_write_csv') as write:
            with self.assertRaises(scrape.SkyGuideError):
                scrape.scrape(str(path), days=9, start_date=self.start)
        write.assert_not_called()
        self.assertFalse(path.exists())

    @staticmethod
    def source_response():
        slot = {'id': 'slot', 'startMs': 1795752000000, 'endMs': 1795755600000,
                'programme': {'title': 'Sports programme'}}
        return {'experience': {'channelGroup': {'channels': [
            {'id': 'SPT1', 'title': 'Sky Sport 1', 'slotsForDay': {'slots': [slot]}},
            {'id': 'SPT2', 'title': 'Sky Sport 2', 'slotsForDay': {'slots': [copy.deepcopy(slot)]}},
        ]}}}

    def test_malformed_channel_or_slot_aborts_collection_before_csv(self):
        valid = self.source_response()
        bad_channels = [None, {'id': 'SPT2'}, {'id': 'SPT2', 'title': 'Sky Sport 2'},
                        {'id': 'SPT2', 'title': 'Sky Sport 2', 'slotsForDay': {'slots': None}}]
        valid_slot = valid['experience']['channelGroup']['channels'][1]['slotsForDay']['slots'][0]
        for bad_slot in [None, {}, {'programme': {}},
                         dict(valid_slot, startMs=None), dict(valid_slot, endMs=True),
                         dict(valid_slot, endMs=valid_slot['startMs'])]:
            bad_channels.append({'id': 'SPT2', 'title': 'Sky Sport 2', 'slotsForDay': {'slots': [bad_slot]}})
        for bad_channel in bad_channels:
            broken = copy.deepcopy(valid)
            broken['experience']['channelGroup']['channels'][1] = bad_channel
            with self.subTest(channel=bad_channel), patch.object(scrape, '_find_sports_group_id', return_value='sports'), patch.object(scrape, '_post_graphql', side_effect=[valid, broken]), patch.object(scrape, '_write_csv') as write:
                with self.assertRaises(scrape.SkyGuideError):
                    scrape.scrape(str(Path(self.folder.name) / 'partial.csv'), days=2, start_date=self.start)
                write.assert_not_called()

    def test_missing_channel_on_later_guide_aborts_collection(self):
        first = self.source_response()
        second = copy.deepcopy(first)
        second['experience']['channelGroup']['channels'].pop()
        with patch.object(scrape, '_find_sports_group_id', return_value='sports'), patch.object(scrape, '_post_graphql', side_effect=[first, second]), patch.object(scrape, '_write_csv') as write:
            with self.assertRaisesRegex(scrape.SkyGuideError, 'channel list changed'):
                scrape.scrape(str(Path(self.folder.name) / 'partial.csv'), days=2, start_date=self.start)
        write.assert_not_called()

    def test_explicit_empty_source_channel_is_preserved_as_known_not_missing(self):
        response = self.source_response()
        response['experience']['channelGroup']['channels'][1]['slotsForDay']['slots'] = []
        expected = set()
        with patch.object(scrape, '_post_graphql', return_value=response):
            rows = scrape._scrape_day('sports', self.start, expected_channel_ids=expected)
        self.assertEqual({'SPT1', 'SPT2'}, expected)
        self.assertEqual(['SPT1'], [row['channel_id'] for row in rows])

    def test_explicit_source_gap_is_logged_without_inventing_programmes(self):
        response = self.source_response()
        gap = {'id': 'SPT2-gap-2026-12-05', 'startMs': 1796428800000,
               'endMs': 1796515200000, 'live': False, 'programme': None}
        for gap_id in ['SPT2-gap-2026-12-05', 'SPT2-gap-2026-12-05T00:00:00Z', 'SPT2-2026-12-05T00:00:00Z']:
            response['experience']['channelGroup']['channels'][1]['slotsForDay']['slots'] = [{**gap, 'id': gap_id}]
            with self.subTest(gap_id=gap_id), patch.object(scrape, '_post_graphql', return_value=response), contextlib.redirect_stderr(io.StringIO()) as log:
                rows = scrape._scrape_day('sports', date(2026, 12, 5))
            self.assertEqual(['SPT1'], [row['channel_id'] for row in rows])
            self.assertIn('channel SPT2: 1 explicit source gap', log.getvalue())
        for change in [{'id': 'SPT2-unknown'}, {'id': 'SPT2-gap-2026-12-06'},
                       {'live': True}, {'endMs': gap['startMs']}]:
            response['experience']['channelGroup']['channels'][1]['slotsForDay']['slots'] = [{**gap, **change}]
            with self.subTest(change=change), patch.object(scrape, '_post_graphql', return_value=response):
                with self.assertRaises(scrape.SkyGuideError):
                    scrape._scrape_day('sports', date(2026, 12, 5))


if __name__ == '__main__':
    unittest.main()
