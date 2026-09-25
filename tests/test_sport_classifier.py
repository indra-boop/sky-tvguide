"""Regression tests for the Sky sport-category export."""

import csv
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from sport_classifier import SPORT_PATTERNS, classify_sport
from scrape_sky_sports_guide import _scrape_day, _write_csv


class SportClassifierTests(unittest.TestCase):
    def test_all_23_sports(self):
        cases = {
            "MMA": "UFC 332: Main Card",
            "Canoe Polo": "ICF Polo World Championships Hls",
            "Rugby League": "NRL: Roosters v Sharks SF1",
            "Rugby Union": "WXV: Scotland v Black Ferns Hls",
            "Aussie Rules": "AFLW: Carlton v Richmond",
            "American Football": "College Football",
            "Cricket": "England v Sri Lanka: 2nd ODI",
            "Basketball": "NBL Bullets v Hawks",
            "Baseball": "MLB Yankees v Rays",
            "Golf": "DP World Tour: Round 2",
            "Tennis": "Laver Cup Day 1 - Day Session",
            "Motorsport": "FIA F1: Azerbaijan GP Qualifying",
            "Cycling": "CRO Race 2026: Stage 3",
            "Racing": "Trackside 1 LIVE - AUS/INTL",
            "Netball": "NETBUSTERS",
            "Darts": "World Series Darts: Finals D4 Eve",
            "Squash": "PSA Qatar Classic Hls",
            "Sailing": "Sailing To The Games Ep.4",
            "Surfing": "WSL: Lexus Trestles Pro HLs",
            "Triathlon": "T100 Triathlon: French Riviera Hls",
            "Trail Running": "GTWS: Myoko Trail Hls",
            "Lawn Bowls": "The Bowls Show",
            "Soccer": "UEFA Women's Champions League Highlights",
        }
        self.assertEqual(23, len(cases))
        self.assertEqual(set(cases), {sport for sport, _ in SPORT_PATTERNS})
        for expected, title in cases.items():
            with self.subTest(title=title):
                self.assertEqual(expected, classify_sport(title))

    def test_mma_aliases_and_promo(self):
        for title in ("UFC Fight Night", "PFL Finals", "ONE Championship 180", "Mixed Martial Arts"):
            self.assertEqual("MMA", classify_sport(title))
        self.assertEqual("MMA", classify_sport("October 4th: UFC 332"))

    def test_ambiguous_title_is_other(self):
        for title in ("Get Up", "SportsCenter", "The Crowd Goes Wild",
                      "Coming Up: Something", "WSL: Arsenal v Chelsea", ""):
            self.assertEqual("Other", classify_sport(title))
        self.assertEqual("American Football", classify_sport("College Football"))
        self.assertEqual("Surfing", classify_sport("WSL: Lexus Trestles Pro HLs"))
        self.assertEqual("Motorsport", classify_sport("FIA Formula 2: Sprint Race"))

    def test_scraped_rows_and_csv_expose_category(self):
        response = {"experience": {"channelGroup": {"channels": [{
            "id": "SARA", "title": "Sky Arena", "number": 65,
            "slotsForDay": {"slots": [{
                "id": "one", "startMs": 1780000000000, "endMs": 1780003600000,
                "programme": {"title": "October 4th: UFC 332"}
            }]}
        }]}}}
        with patch("scrape_sky_sports_guide._post_graphql", return_value=response):
            rows = _scrape_day("sports", date(2026, 9, 25))
        self.assertEqual(1, len(rows))
        self.assertEqual("MMA", rows[0]["sport_category"])
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "test.csv")
            _write_csv(path, rows)
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual("sport_category", reader.fieldnames[-1])
                self.assertEqual("MMA", next(reader)["sport_category"])


if __name__ == "__main__":
    unittest.main()
