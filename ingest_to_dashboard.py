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


def _resolve_csv_path(explicit: str | None) -> str:
    if explicit:
        return explicit if os.path.isabs(explicit) else os.path.join(SCRIPT_DIR, explicit)

    today = datetime.now(SKY_TIMEZONE).date()
    for offset in (0, -1):
        candidate = os.path.join(
            SCRIPT_DIR, f"sports_{(today + timedelta(days=offset)).isoformat()}.csv"
        )
        if os.path.isfile(candidate):
            return candidate

    matches = sorted(glob.glob(os.path.join(SCRIPT_DIR, "sports_20*.csv")))
    if matches:
        return matches[-1]
    raise IngestError("Tidak ada file sports_YYYY-MM-DD.csv yang bisa dikirim")


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
        csv_path = _resolve_csv_path(args.csv)
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
