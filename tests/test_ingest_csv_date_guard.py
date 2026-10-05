"""Unit tests for sky-tvguide ingest CSV date guard.

Rejects stale fallback (H-1 / newest glob) when the resolved file date
does not match the target date (Pacific/Auckland "today" by default).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import ingest_to_dashboard as ingest


class CsvDateGuardTests(unittest.TestCase):
    def test_csv_date_from_name(self):
        self.assertEqual(
            ingest._csv_date_from_name("/tmp/sports_2026-10-05.csv"),
            date(2026, 10, 5),
        )
        self.assertIsNone(ingest._csv_date_from_name("/tmp/sports_today.csv"))
        self.assertIsNone(ingest._csv_date_from_name("/tmp/results.csv"))

    def test_resolve_today_ok(self):
        target = date(2026, 10, 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"sports_{target.isoformat()}.csv"
            path.write_text("date,start_time,program_title,channel_display_name\n", encoding="utf-8")
            with mock.patch.object(ingest, "SCRIPT_DIR", tmp):
                resolved = ingest._resolve_csv_path(None, target_date=target)
            self.assertEqual(resolved, str(path))

    def test_resolve_missing_target_fails_loudly(self):
        target = date(2026, 10, 5)
        with tempfile.TemporaryDirectory() as tmp:
            # Only a stale H-1 file exists — must NOT silently ingest it.
            stale = Path(tmp) / f"sports_{(target + timedelta(days=-1)).isoformat()}.csv"
            stale.write_text("date,start_time,program_title,channel_display_name\n", encoding="utf-8")
            with mock.patch.object(ingest, "SCRIPT_DIR", tmp):
                with self.assertRaises(ingest.IngestError) as ctx:
                    ingest._resolve_csv_path(None, target_date=target)
            msg = str(ctx.exception)
            self.assertIn(target.isoformat(), msg)
            self.assertIn("Stale fallback disabled", msg)

    def test_explicit_mismatched_date_rejected(self):
        target = date(2026, 10, 5)
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "sports_2026-10-04.csv"
            wrong.write_text("date,start_time,program_title,channel_display_name\n", encoding="utf-8")
            with mock.patch.object(ingest, "SCRIPT_DIR", tmp):
                with self.assertRaises(ingest.IngestError) as ctx:
                    ingest._resolve_csv_path(str(wrong), target_date=target)
            self.assertIn("CSV date guard", str(ctx.exception))

    def test_explicit_matching_date_ok(self):
        target = date(2026, 10, 5)
        with tempfile.TemporaryDirectory() as tmp:
            ok = Path(tmp) / "sports_2026-10-05.csv"
            ok.write_text("date,start_time,program_title,channel_display_name\n", encoding="utf-8")
            with mock.patch.object(ingest, "SCRIPT_DIR", tmp):
                resolved = ingest._resolve_csv_path(str(ok), target_date=target)
            self.assertEqual(resolved, str(ok))


if __name__ == "__main__":
    unittest.main()
