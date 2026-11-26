# sky-tvguide

Exporter jadwal **Sports** dari [Sky NZ TV Guide](https://tvguide.sky.co.nz/),
disimpan sebagai arsip CSV harian (`sports_YYYY-MM-DD.csv`).

Data dibaca langsung dari endpoint GraphQL publik yang digunakan aplikasi TV
Guide. Tidak memakai Playwright, Chromium, login, token, atau API secret.

**Status:** terverifikasi 7 Agustus 2026 — 15 Sports channels dan 318 programme
slots berhasil diambil untuk satu hari. Jumlah channel/program dapat berubah
sesuai jadwal Sky.

## Kebijakan publikasi live

CSV `sports_YYYY-MM-DD.csv` adalah **arsip TV guide**, termasuk program studio, highlights, dan tayang ulang. API/kolom saat ini tidak menyediakan flag live per acara yang tervalidasi. Karena itu CSV ini tidak boleh dipakai sebagai feed live atau diberi badge LIVE berdasarkan kategori olahraga atau kata pada judul. Untuk feed live, tambahkan sumber/badge eksplisit dan filter terpisah dengan tes replay sebelum ingest.

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

1. Menentukan tanggal awal berdasarkan **Asia/Makassar (WITA)**.
2. Mengambil **sembilan tanggal guide NZ** dari tanggal awal tersebut. Tanggal
   tambahan diperlukan agar delapan hari WITA mempunyai data malam yang lengkap.
3. Menulis `sports_START_to_END.csv`, lalu memvalidasi seluruh tanggal, UTC slot,
   duplikat, jumlah baris, dan ukuran batch sebelum satu pun ingest dilakukan.
4. Memeriksa kemampuan aggregator, kemudian mengirim delapan snapshot terpisah
   berdasarkan **tanggal mulai WITA**. Setiap snapshot memuat satu hari lengkap,
   maksimum 2.000 baris. Tanggal tepi yang hanya sebagian tidak dikirim.
5. Commit dan push CSV hanya jika semua langkah yang diminta berhasil; kegagalan
   ingest mencantumkan tanggal gagal dan tanggal yang sudah diterima.
6. Upload `debug_artifacts/failure.json` selama tujuh hari jika scraper gagal.

### Dependency dan verifikasi sebelum aktivasi jendela delapan hari

**FIELD VERIFICATION:** perubahan collector ini membutuhkan rilis aggregator
yang memakai `start_utc` / `end_utc` Sky, mempertahankan `liveAuthority=false`,
dan mengganti data hanya untuk rentang tanggal mulai WITA dalam satu snapshot.
`GET /api/schedule/v2?days=1` harus mengembalikan:

```json
{"capabilities":{"skyAbsoluteUtc":true,"ingestWindowByWitaDate":true}}
```

`ingest_window_to_dashboard.py` menolak mengirim data bila salah satu kemampuan
tidak ada, bernilai selain `true`, atau probe gagal. Deploy dan verifikasi
dependency aggregator serta rehearsal database terisolasi harus mendahului
merge/aktivasi workflow ini. Unit tests dan PR tidak membuktikan produksi siap.

Contoh persiapan yang tidak mengirim apa pun:

```bash
python scrape_sky_sports_guide.py --start-date 2026-11-27 --days 9
python ingest_window_to_dashboard.py \
  --csv sports_2026-11-27_to_2026-12-05.csv \
  --start-date 2026-11-27 --days 8 --dry-run
```

CSV menyimpan sembilan tanggal guide NZ. Batch ingest dari contoh tersebut
hanya mengganti delapan tanggal mulai WITA, **27 November–4 Desember**.
Slot yang melewati tengah malam memakai waktu UTC asli sumber. Duplikat slot
yang muncul pada dua guide dibuang hanya jika isinya sama; konflik membatalkan
seluruh rencana sebelum POST. Satu tanggal gagal diunduh atau gagal validasi
juga membatalkan ingest seluruh jendela. Kegagalan POST menghentikan tanggal
berikutnya; tanggal yang sudah berhasil dicatat dan dapat dikirim ulang secara
idempoten. Skrip ingest satu hari tetap tersedia untuk arsip lama.

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
| `sport_category` | Klasifikasi konservatif dari `program_title`: 23 jenis olahraga atau `Other` bila judul ambigu. Bukan penanda LIVE. |

Contoh:

```csv
country_code,channel_id,channel_number,channel_name,channel_display_name,date,program_title,start_time,end_time,scraped_at,sport_category
NZ,SPT1,51,Sky Sport 1,[NZ] Sky Sport 1,2026-08-15,DP World Tour: Round 2,11:00PM,4:10AM,2026-08-15T01:11:40Z,Golf
NZ,0026,60,ESPN,[NZ] ESPN,2026-08-15,SportsCenter,6:00AM,7:00AM,2026-08-15T01:11:40Z,Other
```

Classifier memakai pola judul spesifik (misalnya `UFC`/`PFL` → `MMA`, `NRL` → `Rugby League`, `F1` → `Motorsport`). `Other` menjaga agar studio show dan judul ambigu tidak diberi cabang olahraga palsu. File arsip lama tetap berformat lama sampai dihasilkan ulang; ingest dashboard menerima kedua format.

Validasi lokal: `python -m unittest discover -s tests -v`.

## Reliability controls

- Sports group ID ditemukan secara dinamis; tidak hard-coded.
- Retry dengan exponential backoff untuk timeout, HTTP `429`, dan HTTP `5xx`.
- Response size limit 16 MiB dan basic schema validation.
- Zero-row response dianggap failure agar CSV kosong tidak ter-commit.
- Diagnostic JSON otomatis dibuat saat gagal.

Endpoint ini merupakan implementation detail aplikasi Sky dan dapat berubah.
Jika schema berubah, workflow akan gagal dengan diagnostic yang eksplisit,
bukan menghasilkan CSV kosong.
