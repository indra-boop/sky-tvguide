# sky-tvguide

Scraper jadwal **Sports** dari [tvguide.sky.co.nz](https://tvguide.sky.co.nz/) (Sky NZ TV Guide) menjadi CSV.

## Kenapa browser automation, bukan hit API langsung

`tvguide.sky.co.nz` adalah React SPA (client-side rendered). Endpoint internal
`web-graphql.sky.co.nz/prod/graphql` kemungkinan jadi sumber data asli, tapi
query/payload persisnya belum di-reverse-engineer di sini. Jadi script ini
memakai Playwright (browser automation) untuk render halaman lalu membaca
DOM yang sudah jadi.

Legal check (per 30 Jul 2026): `robots.txt` situs ini terbuka penuh
(`User-agent: *` tanpa `Disallow`), dan data yang diambil adalah jadwal
publik tanpa login. Tetap pakai rate-limit wajar saat scraping.

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

## Pakai

```bash
python scrape_sky_sports_guide.py --output sports_today.csv
```

Debug (browser kelihatan + log detail):

```bash
python scrape_sky_sports_guide.py --output sports_today.csv --headful --debug
```

> Catatan: di dalam script ada `executable_path="/opt/pw-browsers/chromium"`
> yang khusus untuk sandbox tempat script ini ditulis. Hapus/kosongkan
> parameter itu supaya Playwright pakai browser hasil `playwright install`
> di komputer Anda sendiri.

## Output

CSV dengan kolom:

| Kolom | Keterangan |
|---|---|
| `channel_id` | ID kanal dari URL `/channel/{id}` |
| `channel_number` | Nomor kanal yang tampil di guide (mis. `050`) |
| `channel_name` | Nama kanal, diekstrak dari nama file logo (mis. `Sky Sports 1`) |
| `program_title` | Judul acara |
| `start_time` / `end_time` | Jam tayang |
| `scraped_at` | Timestamp scraping |

## Keterbatasan saat ini

- Hanya mengambil hari yang sedang tampil di guide (default: hari ini).
  Situs punya tab navigasi hari sampai ~28 hari ke depan, tapi logic klik
  tab hari lain belum diimplementasikan.
- Struktur DOM sudah divalidasi manual per 30 Jul 2026. Kalau Sky mengubah
  halaman, titik paling rawan: nama class Tailwind, threshold
  `offsetWidth > 700` untuk deteksi baris grid, dan pola nama file logo
  kanal. Jalankan `--headful --debug` untuk diagnosa cepat kalau hasil
  tiba-tiba kosong.
- Script belum di-run end-to-end oleh yang menulisnya (dibuat di sandbox
  tanpa akses network ke domain ini) — tiap bagian logic sudah divalidasi
  terpisah terhadap DOM live situs, tapi tetap test manual sebelum dipakai
  rutin/terjadwal (mis. cron).
