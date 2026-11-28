#!/usr/bin/env python3
"""Ingest eight complete WITA days from nine adjacent Sky NZ guide dates.

Every input row and every date batch is validated before any write. The
aggregator must advertise UTC-aware Sky normalization and replacement limited
to the WITA date range present in one snapshot. Never split by arbitrary row
counts or by NZ guide date: those batches would overlap WITA replacement dates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ingest_to_dashboard import (
    DEFAULT_MIN_ROWS,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_EVENTS_PER_SYNC,
    SOURCE_NAME,
    IngestError,
    _post,
    _read_rows,
)

WITA = ZoneInfo("Asia/Makassar")
NZ = ZoneInfo("Pacific/Auckland")
MAX_BODY_BYTES = 6 * 1024 * 1024
REQUIRED_CAPABILITIES = ("skyAbsoluteUtc", "ingestWindowByWitaDate")
UTC_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z")


def _utc(value: str, field: str, index: int) -> datetime:
    if not UTC_PATTERN.fullmatch(value):
        raise IngestError(f"Row {index}: {field} must be an explicit UTC instant")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IngestError(f"Row {index}: invalid {field}") from error


def plan_batches(
    csv_path: str, start_date: date, days: int, *, min_rows_per_guide: int = DEFAULT_MIN_ROWS
) -> list[tuple[str, list[dict[str, str]]]]:
    """Return complete, disjoint WITA-day snapshots; invalid input never writes."""
    if not 1 <= days <= 27:
        raise IngestError("Window must contain 1..27 WITA days (plus one adjacent NZ guide day)")
    if min_rows_per_guide < 1:
        raise IngestError("Minimum rows per NZ guide must be positive")
    last_guide = start_date + timedelta(days=days)
    expected_name = f"sports_{start_date.isoformat()}_to_{last_guide.isoformat()}.csv"
    if os.path.basename(csv_path) != expected_name:
        raise IngestError(f"CSV date guard: expected {expected_name}")
    expected_guides = {start_date + timedelta(days=offset) for offset in range(days + 1)}
    expected_wita = [(start_date + timedelta(days=offset)).isoformat() for offset in range(days)]
    batches: dict[str, list[dict[str, str]]] = {day: [] for day in expected_wita}
    guide_slots: dict[date, set[tuple[str, datetime]]] = {
        guide: set() for guide in expected_guides
    }
    seen: dict[tuple[str, datetime], dict[str, str]] = {}

    for index, row in enumerate(_read_rows(csv_path), 1):
        try:
            guide_date = date.fromisoformat(row.get("date", ""))
        except ValueError as error:
            raise IngestError(f"Row {index}: invalid NZ guide date") from error
        if guide_date not in expected_guides:
            raise IngestError(f"Row {index}: guide date {guide_date} outside requested range")
        channel = row.get("channel_id", "").strip()
        if not channel or not row.get("program_title", "").strip():
            raise IngestError(f"Row {index}: channel_id and programme title are required")
        start = _utc(row.get("start_utc", ""), "start_utc", index)
        end = _utc(row.get("end_utc", ""), "end_utc", index)
        if end <= start:
            raise IngestError(f"Row {index}: end_utc must be later than start_utc")
        guide_start = datetime.combine(guide_date, time.min, NZ)
        guide_end = datetime.combine(guide_date + timedelta(days=1), time.min, NZ)
        # The API can include the programme crossing NZ midnight and the next day's 00:00 slot.
        if start > guide_end or end <= guide_start:
            raise IngestError(f"Row {index}: UTC slot does not belong to its NZ guide date")
        key = (channel, start)
        # Repeated rows must not disguise a truncated guide. A midnight slot
        # may legitimately count once in each adjacent NZ guide.
        guide_slots[guide_date].add(key)
        # Adjacent guide dates repeat a slot crossing NZ midnight. Drop only exact duplicates;
        # conflicting versions require a new consistent collection, not an arbitrary winner.
        comparable = {k: v for k, v in row.items() if k not in {"date", "scraped_at"}}
        if key in seen:
            if seen[key] != comparable:
                raise IngestError(f"Row {index}: conflicting duplicate slot for channel {channel}")
            continue
        seen[key] = comparable
        wita_day = start.astimezone(WITA).date().isoformat()
        if wita_day in batches:
            # NZ clocks repeat when DST ends. The supported explicit source key
            # keeps those distinct UTC slots separate in aggregator normalization.
            source_key = f"sky:{channel}:utc:{start.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}"
            batches[wita_day].append({**row, "source_key": source_key})

    for guide in sorted(expected_guides):
        count = len(guide_slots[guide])
        if count < min_rows_per_guide:
            raise IngestError(
                f"NZ guide {guide}: {count} unique slots < minimum {min_rows_per_guide}; no ingest"
            )
    planned = []
    for day, rows in batches.items():
        if not rows or len(rows) > MAX_EVENTS_PER_SYNC:
            raise IngestError(f"WITA {day}: {len(rows)} rows must be in 1..{MAX_EVENTS_PER_SYNC}")
        rows.sort(key=lambda row: (row["start_utc"], row["channel_id"]))
        payload = json.dumps({"source": SOURCE_NAME, "events": rows}, separators=(",", ":")).encode()
        if len(payload) > MAX_BODY_BYTES:
            raise IngestError(f"WITA {day}: payload exceeds aggregator byte limit")
        planned.append((day, rows))
    return planned


def assert_compatible_aggregator(ingest_url: str) -> None:
    """Read-only compatibility check; no bearer token is sent to the public schedule API."""
    parsed = urlsplit(ingest_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path != "/api/ingest/v2":
        raise IngestError("DASHBOARD_INGEST_URL must identify the aggregator /api/ingest/v2 endpoint")
    url = urlunsplit((parsed.scheme, parsed.netloc, "/api/schedule/v2", "days=1", ""))
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "jerco-sky-window-ingest/1.0"})
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise IngestError("Compatibility response exceeds byte limit")
        payload = json.loads(body)
    except Exception as error:
        raise IngestError(f"Aggregator compatibility check failed ({type(error).__name__}); no ingest") from error
    capabilities = payload.get("capabilities", {}) if isinstance(payload, dict) else {}
    if not isinstance(capabilities, dict) or any(capabilities.get(name) is not True for name in REQUIRED_CAPABILITIES):
        raise IngestError("Aggregator needs skyAbsoluteUtc + ingestWindowByWitaDate capabilities; deploy/verify dependency first")


def ingest_window(
    csv_path: str, start_date: date, days: int, *, url: str = "", token: str = "",
    min_rows_per_guide: int = DEFAULT_MIN_ROWS, dry_run: bool = False,
) -> list[dict[str, str | int]]:
    batches = plan_batches(csv_path, start_date, days, min_rows_per_guide=min_rows_per_guide)
    summary = [{"dateWita": day, "rows": len(rows)} for day, rows in batches]
    print("Validated WITA window: " + json.dumps(summary))
    if dry_run:
        print("[dry-run] All dates validated; no requests sent")
        return summary
    if not token or not url:
        raise IngestError("DASHBOARD_INGEST_URL and DASHBOARD_INGEST_TOKEN are required")
    assert_compatible_aggregator(url)
    completed: list[str] = []
    for day, rows in batches:
        try:
            result = _post(url, token, rows, retries=DEFAULT_RETRIES)
            window = result.get("window", {})
            if result.get("ok") is not True or window.get("start") != day or window.get("end") != day:
                raise IngestError("Aggregator response did not confirm the exact WITA replacement date")
            counts = (result.get("received"), result.get("normalized"))
            if any(type(count) is not int or count != len(rows) for count in counts):
                raise IngestError("Aggregator receipt must confirm received and normalized counts equal sent rows")
            completed.append(day)
            print(f"WITA {day}: ingest confirmed ({len(rows)} rows)")
        except Exception as error:
            raise IngestError(
                f"WITA {day}: ingest failed; completed dates={','.join(completed) or 'none'}; "
                f"subsequent dates were not sent: {error}"
            ) from error
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--start-date", default=datetime.now(WITA).date().isoformat(), help="First complete WITA day")
    parser.add_argument("--days", type=int, default=8, help="Complete WITA days; input needs one extra NZ guide day")
    parser.add_argument("--min-rows-per-guide", type=int, default=int(os.environ.get("MINIMUM_INGEST_ROWS", DEFAULT_MIN_ROWS)))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        ingest_window(args.csv, date.fromisoformat(args.start_date), args.days,
                      url=os.environ.get("DASHBOARD_INGEST_URL", "").strip(),
                      token=os.environ.get("DASHBOARD_INGEST_TOKEN", "").strip(),
                      min_rows_per_guide=args.min_rows_per_guide, dry_run=args.dry_run)
    except Exception as error:
        print(json.dumps({"source": SOURCE_NAME, "errorType": type(error).__name__, "error": str(error),
                          "at": datetime.now(timezone.utc).isoformat()}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
