"""Slot timestamps, live flag and show metadata must survive into the CSV."""

import csv
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from scrape_sky_sports_guide import _scrape_day, _write_csv

LEGACY_COLUMNS = [
    "country_code", "channel_id", "channel_number", "channel_name",
    "channel_display_name", "date", "program_title", "start_time", "end_time",
    "scraped_at", "sport_category",
]
NEW_COLUMNS = ["start_utc", "end_utc", "live", "show_title", "show_type"]


def _ms(iso_utc: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _response(slots):
    return {"experience": {"channelGroup": {"channels": [{
        "id": "SPT1", "title": "Sky Sport 1", "number": 51,
        "slotsForDay": {"slots": slots},
    }]}}}


class AbsoluteTimesTests(unittest.TestCase):
    def _rows(self, slots):
        with patch("scrape_sky_sports_guide._post_graphql", return_value=_response(slots)):
            return _scrape_day("sports", date(2026, 10, 5))

    def test_carry_over_slot_keeps_its_real_day(self):
        # Guide date 5 Oct (NZDT, UTC+13). First slot started 4 Oct 20:30 NZ.
        rows = self._rows([
            {"id": "a", "startMs": _ms("2026-10-04T07:30"), "endMs": _ms("2026-10-04T12:00"), "live": True,
             "programme": {"title": "NRL: Roosters v Knights GF",
                           "show": {"id": "s1", "title": "NRL", "type": "SPORT"}}},
            {"id": "b", "startMs": _ms("2026-10-05T11:00"), "endMs": _ms("2026-10-05T11:30"), "live": False,
             "programme": {"title": "The Crowd Goes Wild"}},
        ])
        self.assertEqual(2, len(rows))
        first, last = rows
        # Clock-only columns are unchanged and ambiguous on their own...
        self.assertEqual(("2026-10-05", "8:30PM", "1:00AM"), (first["date"], first["start_time"], first["end_time"]))
        # ...the absolute instant is not.
        self.assertEqual("2026-10-04T07:30:00Z", first["start_utc"])
        self.assertEqual("2026-10-04T12:00:00Z", first["end_utc"])
        self.assertEqual("true", first["live"])
        self.assertEqual(("NRL", "SPORT"), (first["show_title"], first["show_type"]))
        # Trailing 12:00AM slot belongs to 6 Oct NZ = 5 Oct 11:00 UTC.
        self.assertEqual(("12:00AM", "2026-10-05T11:00:00Z"), (last["start_time"], last["start_utc"]))
        self.assertEqual("false", last["live"])
        self.assertEqual(("", ""), (last["show_title"], last["show_type"]))

    def test_missing_live_flag_stays_empty(self):
        rows = self._rows([
            {"id": "a", "startMs": _ms("2026-10-05T01:00"), "endMs": _ms("2026-10-05T02:00"),
             "programme": {"title": "UFC 332", "show": None}},
        ])
        self.assertEqual("", rows[0]["live"])
        self.assertEqual("", rows[0]["show_title"])

    def test_csv_keeps_legacy_columns_first(self):
        rows = self._rows([
            {"id": "a", "startMs": _ms("2026-10-05T01:00"), "endMs": _ms("2026-10-05T02:00"), "live": True,
             "programme": {"title": "UFC 332"}},
        ])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.csv")
            _write_csv(path, rows)
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(LEGACY_COLUMNS + NEW_COLUMNS, reader.fieldnames)
                row = next(reader)
        self.assertEqual("2026-10-05T01:00:00Z", row["start_utc"])
        self.assertEqual("true", row["live"])


if __name__ == "__main__":
    unittest.main()
