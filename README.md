# sky-tvguide

Scraper jadwal **Sports** dari [tvguide.sky.co.nz](https://tvguide.sky.co.nz/) (Sky NZ TV Guide), disimpan sebagai arsip CSV harian (`sports_YYYY-MM-DD.csv`).

**Status**: Terverifikasi jalan (30 Jul 2026) — 20 channel Sports, 141 program berhasil diambil.

## Kenapa browser automation, bukan hit API langsung

`tvguide.sky.co.nz` adalah React SPA (client-side rendered). Endpoint internal
`web-graphql.sky.co.nz/prod/graphql` kemungkinan jadi sumber data asli, tapi
query/payload persisnya belum di-reverse-engineer di sini. Jadi script ini
memakai Playwright (browser automation) untuk render halaman lalu membaca
DOM yang sudah jadi.

Legal check (per 30 Jul 2026): `robots.txt` situs ini terbuka penuh
(`User-agent: *` tanpa `Disallow`), dan data yang diambil adalah jadwal
publik tanpa login. Tetap pakai rate-limit wajar saat scraping.

## Install (lokal)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Pakai

```bash
# Default: output sports_YYYY-MM-DD.csv (arsip harian, tanggal hari ini)
python scrape_sky_sports_guide.py

# Debug (browser kelihatan + log detail)
python scrape_sky_sports_guide.py --headful --debug

# Override nama file
python scrape_sky_sports_guide.py --output custom_name.csv

# Jalankan + langsung commit & push hasil CSV ke git (untuk run manual)
python scrape_sky_sports_guide.py --git-push
```

## Otomatis harian via GitHub Actions (direkomendasikan)

Repo ini sudah include `.github/workflows/daily-scrape.yml` — jalan otomatis
tiap hari jam 06:00 WITA di server GitHub (laptop Anda **tidak perlu nyala**),
lalu commit CSV baru ke repo pakai identitas `github-actions[bot]` (bukan akun
GitHub pribadi). Tidak perlu setup token/secret apapun karena cuma push ke
repo sendiri, bukan ke API eksternal.

Cara aktifkan:
1. Push repo ini ke GitHub (kalau belum).
2. Buka tab **Actions** di repo → workflow "Daily Sports Schedule Scrape"
   otomatis aktif begitu file workflow ter-push.
3. Mau test langsung tanpa nunggu jadwal? Buka Actions → pilih workflow itu →
   klik **Run workflow** (tombol manual trigger).

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
- Beberapa channel yang belum punya logo resmi (mis. 605-609 "Sky Sport
  Pop-up", 62, 63) menghasilkan `channel_name` yang kurang rapi (nama file
  mentah). Kosmetik saja — data program & jam tetap akurat.
- Struktur DOM sudah divalidasi manual per 30 Jul 2026. Kalau Sky mengubah
  halaman, titik paling rawan: nama class Tailwind, threshold
  `offsetWidth > 700` untuk deteksi baris grid, dan pola nama file logo
  kanal. Jalankan `--headful --debug` untuk diagnosa cepat kalau hasil
  tiba-tiba kosong (baik lokal maupun cek log run di tab Actions).
