# sky-tvguide

Exporter jadwal **Sports** dari [Sky NZ TV Guide](https://tvguide.sky.co.nz/),
disimpan sebagai arsip CSV harian (`sports_YYYY-MM-DD.csv`).

Data dibaca langsung dari endpoint GraphQL publik yang digunakan aplikasi TV
Guide. Tidak memakai Playwright, Chromium, login, token, atau API secret.

**Status:** terverifikasi 7 Agustus 2026 — 15 Sports channels dan 318 programme
slots berhasil diambil untuk satu hari. Jumlah channel/program dapat berubah
sesuai jadwal Sky.

## Requirements

- Python 3.9 atau lebih baru
- Network access ke `https://api.skyone.co.nz/exp/graph`
- Tidak ada third-party Python package

## Penggunaan

```bash
# Hari ini menurut timezone Pacific/Auckland
python scrape_sky_sports_guide.py

# Log detail
python scrape_sky_sports_guide.py --debug

# Ambil beberapa hari, maksimum 28 hari
python scrape_sky_sports_guide.py --days 7

# Rentang tanggal eksplisit
python scrape_sky_sports_guide.py --start-date 2026-08-07 --until 2026-08-13

# Override nama/path output
python scrape_sky_sports_guide.py --output custom_name.csv

# Run manual lalu commit dan push CSV
python scrape_sky_sports_guide.py --git-push
```

Flag lama `--headful` masih diterima untuk compatibility, tetapi hanya
menampilkan warning karena exporter tidak lagi menjalankan browser.

## GitHub Actions

Workflow `.github/workflows/daily-scrape.yml` berjalan setiap hari pukul
08:00 WITA (`00:00 UTC`) dan dapat dijalankan manual lewat **Run workflow**.

Workflow akan:

1. Menjalankan exporter menggunakan Python 3.12.
2. Menulis `sports_YYYY-MM-DD.csv`.
3. Commit dan push CSV bila ada perubahan.
4. Upload `debug_artifacts/failure.json` selama tujuh hari jika gagal.

## Output CSV

Setiap channel pada source ini berasal dari market/feed Sky New Zealand, sehingga
menggunakan kode negara ISO 3166-1 alpha-2 `NZ`. Nama asli tetap dipertahankan
untuk matching/dedup, sedangkan `channel_display_name` menyediakan format siap
tampil seperti `[NZ] Sky Sport 1`.

| Kolom | Keterangan |
|---|---|
| `country_code` | Kode negara/market feed, selalu `NZ` untuk source Sky NZ |
| `channel_id` | ID channel dari GraphQL |
| `channel_number` | Nomor channel Sky |
| `channel_name` | Nama channel asli tanpa prefix negara |
| `channel_display_name` | Nama channel siap tampil, contoh `[NZ] Sky Sport 1` |
| `date` | Tanggal guide dalam timezone `Pacific/Auckland` |
| `program_title` | Judul program |
| `start_time` / `end_time` | Jam lokal `Pacific/Auckland` |
| `scraped_at` | Timestamp scraping UTC (ISO 8601) |

Contoh:

```csv
country_code,channel_id,channel_number,channel_name,channel_display_name,date,program_title,start_time,end_time,scraped_at
NZ,SPT1,51,Sky Sport 1,[NZ] Sky Sport 1,2026-08-15,DP World Tour: Round 2,11:00PM,4:10AM,2026-08-15T01:11:40Z
NZ,0026,60,ESPN,[NZ] ESPN,2026-08-15,SportsCenter,6:00AM,7:00AM,2026-08-15T01:11:40Z
```

## Reliability controls

- Sports group ID ditemukan secara dinamis; tidak hard-coded.
- Retry dengan exponential backoff untuk timeout, HTTP `429`, dan HTTP `5xx`.
- Response size limit 16 MiB dan basic schema validation.
- Zero-row response dianggap failure agar CSV kosong tidak ter-commit.
- Diagnostic JSON otomatis dibuat saat gagal.

Endpoint ini merupakan implementation detail aplikasi Sky dan dapat berubah.
Jika schema berubah, workflow akan gagal dengan diagnostic yang eksplisit,
bukan menghasilkan CSV kosong.