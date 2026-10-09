#!/usr/bin/env python3
"""Kirim CSV hasil scrape Sky NZ ke aggregator dashboard.

Baris CSV dikirim apa adanya. Konversi Pacific/Auckland -> WITA, inferensi
sport/competition/teams, dan klasifikasi broadcast_kind seluruhnya dikerjakan
oleh `adaptSky()` di lib/sports-aggregator/core.mjs pada sisi aggregator, jadi
skrip ini sengaja TIDAK mentransformasi apa pun. Menambah transformasi di sini
akan membuat dua sumber kebenaran yang bisa berbeda diam-diam.

Kegagalan ingest selalu menghasilkan exit code non-zero supaya workflow merah
dan terlihat, meniru pola sendToDashboard() di ausport-scraper.
"""

from __future__ import annotations

import argparse
import re
import csv
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKY_TIMEZONE = ZoneInfo("Pacific/Auckland")
SOURCE_NAME = "sky-tvguide"

# Batas server: MAX_EVENTS_PER_SYNC = 2000 di services/aggregator-api/server.mjs.
# Payload TIDAK dipecah jadi beberapa batch: ingestSnapshot() memperlakukan satu
# POST sebagai satu snapshot penuh per source, jadi batch kedua berpotensi
# menghapus batch pertama. Lebih baik gagal keras daripada kehilangan data.
MAX_EVENTS_PER_SYNC = 2000

DEFAULT_TIMEOUT_SECONDS = 40
DEFAULT_RETRIES = 3
DEFAULT_MIN_ROWS = 150  # ambang QC harian; di bawah ini dianggap scrape rusak

REQUIRED_COLUMNS = {
    "date",
    "start_time",
    "program_title",
    "channel_display_name",
}


class IngestError(RuntimeError):
    """Ingest tidak dapat diselesaikan."""


def _read_rows(csv_path: str) -> list[dict[str, str]]:
    if not os.path.isfile(csv_path):
        raise IngestError(f"CSV tidak ditemukan: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - fieldnames
        if missing:
            raise IngestError(
                f"Kolom wajib hilang di {os.path.basename(csv_path)}: "
                f"{sorted(missing)}"
            )
        rows = [
            {key: (value or "") for key, value in row.items() if key}
            for row in reader
        ]

    if not rows:
        raise IngestError(f"CSV kosong: {csv_path}")
    return rows


def _csv_date_from_name(path: str):
    """Parse YYYY-MM-DD from sports_YYYY-MM-DD.csv; return None if not dated."""
    name = os.path.basename(path)
    match = re.fullmatch(r"sports_(\d{4}-\d{2}-\d{2})\.csv", name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _assert_csv_matches_target(path: str, target_date) -> None:
    """Reject silent stale ingest when filename date ≠ target date."""
    file_date = _csv_date_from_name(path)
    if file_date is None:
        raise IngestError(
            f"CSV path must be sports_YYYY-MM-DD.csv so date can be verified "
            f"(got {os.path.basename(path)!r}). Target date: {target_date.isoformat()}."
        )
    if file_date != target_date:
        raise IngestError(
            f"CSV date guard: resolved file date {file_date.isoformat()} ≠ "
            f"target date {target_date.isoformat()} "
            f"({os.path.basename(path)}). Refusing stale/wrong-day ingest."
        )


def _resolve_csv_path(
    explicit: str | None, *, target_date=None
) -> str:
    """Resolve CSV for ingest; require filename date == target (NZ today).

    Previous behaviour fell back to H-1 then glob(sports_20*.csv)[-1] with no
    age check, which silently ingested stale files. Prefer failing loudly.
    Note: legacy sports_today.csv (stale since 2026-07-31) was removed 2026-10-09.
    """
    if target_date is None:
        target_date = datetime.now(SKY_TIMEZONE).date()

    if explicit:
        path = (
            explicit
            if os.path.isabs(explicit)
            else os.path.join(SCRIPT_DIR, explicit)
        )
        if not os.path.isfile(path):
            raise IngestError(f"CSV tidak ditemukan: {path}")
        _assert_csv_matches_target(path, target_date)
        return path

    candidate = os.path.join(
        SCRIPT_DIR, f"sports_{target_date.isoformat()}.csv"
    )
    if os.path.isfile(candidate):
        _assert_csv_matches_target(candidate, target_date)
        return candidate

    # Loud failure: do not fall back to H-1 or newest glob match.
    h1 = (target_date + timedelta(days=-1)).isoformat()
    matches = sorted(glob.glob(os.path.join(SCRIPT_DIR, "sports_20*.csv")))
    newest = os.path.basename(matches[-1]) if matches else "(none)"
    raise IngestError(
        f"CSV for target date {target_date.isoformat()} not found "
        f"(expected sports_{target_date.isoformat()}.csv). "
        f"Stale fallback disabled (would have considered H-1 "
        f"sports_{h1}.csv or newest {newest})."
    )


def _post(url: str, token: str, rows: list[dict[str, str]], *, retries: int) -> dict[str, Any]:
    payload = json.dumps(
        {"source": SOURCE_NAME, "events": rows}, separators=(",", ":")
    ).encode("utf-8")
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        started_at = time.monotonic()
        request = Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "jerco-sky-tvguide-ingest/1.0",
            },
        )

        try:
            with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                body = response.read(1024 * 1024).decode("utf-8", "replace")
                document = json.loads(body) if body else {}
            print(
                "Dashboard ingest success: "
                + json.dumps(
                    {
                        "status": response.status,
                        "durationMs": int((time.monotonic() - started_at) * 1000),
                        "rows": len(rows),
                        "response": document,
                    },
                    ensure_ascii=False,
                )
            )
            return document

        except HTTPError as error:
            snippet = error.read(2000).decode("utf-8", "replace")
            last_error = IngestError(f"HTTP {error.code}: {snippet or error.reason}")
            # 401 = token salah, 400/422 = payload ditolak. Mengulang tidak menolong.
            retryable = error.code == 429 or 500 <= error.code < 600
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            retryable = True

        print(
            f"[warning] Ingest attempt {attempt}/{retries} gagal: {last_error}",
            file=sys.stderr,
        )
        if not retryable or attempt == retries:
            break
        time.sleep(2 ** (attempt - 1))

    raise IngestError(f"Ingest gagal setelah {retries} percobaan: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kirim CSV Sky NZ ke aggregator dashboard"
    )
    parser.add_argument("--csv", help="Path CSV; default: sports_<hari ini NZ>.csv")
    parser.add_argument(
        "--target-date",
        help="Target YYYY-MM-DD (Pacific/Auckland). Default: today NZ. "
        "CSV filename date must match this value.",
    )
    parser.add_argument(
        "--min-rows",
        type=int,
        default=int(os.environ.get("MINIMUM_INGEST_ROWS", DEFAULT_MIN_ROWS)),
        help=f"Ambang minimum baris (default {DEFAULT_MIN_ROWS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validasi CSV dan tampilkan ringkasan tanpa mengirim apa pun",
    )
    args = parser.parse_args()

    url = os.environ.get("DASHBOARD_INGEST_URL", "").strip()
    token = os.environ.get("DASHBOARD_INGEST_TOKEN", "").strip()

    try:
        if args.target_date:
            try:
                target_date = datetime.strptime(args.target_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise IngestError(
                    f"--target-date must be YYYY-MM-DD, got {args.target_date!r}"
                ) from exc
        else:
            target_date = datetime.now(SKY_TIMEZONE).date()

        csv_path = _resolve_csv_path(args.csv, target_date=target_date)
        rows = _read_rows(csv_path)

        print(
            f"[info] {os.path.basename(csv_path)}: {len(rows)} baris, "
            f"{len({row.get('channel_display_name', '') for row in rows})} channel"
        )

        if len(rows) < args.min_rows:
            raise IngestError(
                f"Row guard: {len(rows)} baris < ambang {args.min_rows}. "
                f"Scrape kemungkinan rusak; ingest dibatalkan."
            )
        if len(rows) > MAX_EVENTS_PER_SYNC:
            raise IngestError(
                f"{len(rows)} baris melebihi batas {MAX_EVENTS_PER_SYNC} event per "
                f"sync. Payload tidak dipecah karena satu POST = satu snapshot "
                f"penuh per source. Persempit rentang tanggal CSV."
            )

        if args.dry_run:
            sample = rows[0]
            print(
                "[dry-run] Contoh baris: "
                + json.dumps(
                    {
                        key: sample.get(key, "")
                        for key in sorted(REQUIRED_COLUMNS)
                    },
                    ensure_ascii=False,
                )
            )
            print("[dry-run] Tidak ada yang dikirim.")
            return 0

        if not url:
            raise IngestError("DASHBOARD_INGEST_URL belum di-set")
        if not token:
            raise IngestError("DASHBOARD_INGEST_TOKEN belum di-set")

        _post(url, token, rows, retries=DEFAULT_RETRIES)

    except Exception as error:  # noqa: BLE001 - dilaporkan lalu keluar non-zero
        print(
            "Dashboard ingest failure: "
            + json.dumps(
                {
                    "source": SOURCE_NAME,
                    "error": str(error),
                    "errorType": type(error).__name__,
                    "at": datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                    "nonFatal": False,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
