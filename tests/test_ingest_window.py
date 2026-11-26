"""A multi-day Sky scrape must never replace a WITA day with a partial NZ slice."""

import contextlib
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
            return {'ok': True, 'window': {'start': day, 'end': day}}
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
        ok = {'ok': True, 'window': {'start': '2026-11-27', 'end': '2026-11-27'}}
        with patch.object(window, 'assert_compatible_aggregator'), patch.object(window, '_post', side_effect=[ok, window.IngestError('synthetic failure')]) as post:
            with self.assertRaisesRegex(window.IngestError, 'WITA 2026-11-28.*completed dates=2026-11-27'):
                window.ingest_window(self.write(), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
        self.assertEqual(2, post.call_count)

    def test_wrong_server_window_stops_subsequent_batches(self):
        wrong = {'ok': True, 'window': {'start': '2026-11-26', 'end': '2026-11-27'}}
        with patch.object(window, 'assert_compatible_aggregator'), patch.object(window, '_post', return_value=wrong) as post:
            with self.assertRaisesRegex(window.IngestError, 'exact WITA replacement date'):
                window.ingest_window(self.write(), self.start, 8, url='https://example.invalid/api/ingest/v2', token='synthetic-test', min_rows_per_guide=1)
        post.assert_called_once()

    def test_scraper_failure_does_not_write_partial_csv(self):
        path = Path(self.folder.name) / 'partial.csv'
        with patch.object(scrape, '_find_sports_group_id', return_value='sports'), patch.object(scrape, '_scrape_day', side_effect=[[{'synthetic': 'row'}], scrape.SkyGuideError('missing day')]), patch.object(scrape, '_write_csv') as write:
            with self.assertRaises(scrape.SkyGuideError):
                scrape.scrape(str(path), days=9, start_date=self.start)
        write.assert_not_called()
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
