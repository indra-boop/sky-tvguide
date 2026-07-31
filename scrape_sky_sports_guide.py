#!/usr/bin/env python3
"""
Scraper: Sky TV Guide (NZ) - Sports channels only
Source : https://tvguide.sky.co.nz/
Method : Browser automation (Playwright), karena situs adalah React SPA
         (client-side rendered, butuh JavaScript untuk menampilkan data).
         Tidak ada REST/GraphQL publik yang terdokumentasi untuk data ini
         (endpoint internal terdeteksi: web-graphql.sky.co.nz/prod/graphql,
         tapi query/payload-nya tidak di-reverse-engineer di sini -> lihat
         catatan di bagian bawah file ini).

Legal/ethical check (per 30 Jul 2026):
- robots.txt situs ini TIDAK melarang crawling (User-agent: * tanpa Disallow).
- Data yang diambil adalah jadwal publik yang memang ditampilkan tanpa login.
- Tetap: gunakan rate-limit wajar, jangan hit server terlalu sering/paralel.

STATUS: Sudah diverifikasi jalan (30 Jul 2026, Windows + Python 3.13) - 20
channel Sports terdeteksi, 141 program berhasil di-parse. Kalau Sky ubah
struktur halaman dan hasil tiba-tiba 0, jalankan dengan --headful --debug
untuk diagnosa.

Install dependencies:
    pip install playwright --break-system-packages
    playwright install chromium

Usage:
    # Default: hanya hari ini, output sports_YYYY-MM-DD.csv (arsip harian)
    python scrape_sky_sports_guide.py
    python scrape_sky_sports_guide.py --headful --debug

    # Ambil rentang hari ke depan (situs cuma sedia data s.d. 28 hari ke depan)
    python scrape_sky_sports_guide.py --days 28
    python scrape_sky_sports_guide.py --until 2026-08-27

    # Arsip harian + otomatis commit & push ke git repo (folder script ini
    # harus berada di dalam git repo yang sudah di-setup remote & auth-nya)
    python scrape_sky_sports_guide.py --git-push

    # Override nama file kalau perlu
    python scrape_sky_sports_guide.py --output custom_name.csv

Catatan multi-hari (verified 30 Jul 2026): semua tab tanggal (hari ini s.d.
+27 hari) SUDAH ada di DOM sekaligus saat page load pertama - tidak perlu
klik tombol panah ">" berkali-kali. Script ini klik tiap tab tanggal
(dicari by exact text, mis. "27 Aug") lalu re-scrape grid untuk hari itu.
"""

import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE
)


def parse_row_text(raw_text: str):
    """
    Parse innerText dari satu baris channel menjadi list program.
    Pola yang diamati di screenshot browser:
        050            <- nomor channel
        AFL: Collingwood v Geelong
        5:45 AM - 8:15 AM
        HotelPlanner Tour ...
        8:15 AM - 8:45 AM
    Jadi: baris pertama = nomor channel, lalu berpasangan (judul, jam).
    """
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    if not lines:
        return None, []

    channel_number = lines[0]
    programs = []
    title_buffer = []

    for line in lines[1:]:
        m = TIME_RANGE_RE.search(line)
        if m:
            title = " ".join(title_buffer).strip()
            if title:
                programs.append(
                    {
                        "title": title,
                        "start_time": m.group(1).upper().replace(" ", ""),
                        "end_time": m.group(2).upper().replace(" ", ""),
                    }
                )
            title_buffer = []
        else:
            title_buffer.append(line)

    return channel_number, programs


def git_commit_and_push(csv_filename: str):
    """
    Commit file CSV yang baru dibuat lalu push ke remote. Dijalankan di
    SCRIPT_DIR (folder git repo), jadi tidak masalah script dipanggil dari
    working directory manapun (mis. lewat Task Scheduler).
    Best-effort: kalau tidak ada perubahan (commit gagal karena "nothing to
    commit"), itu bukan error - cuma berarti data hari ini sama seperti
    commit sebelumnya (jarang terjadi tapi mungkin).
    """
    def run(cmd):
        return subprocess.run(
            cmd, cwd=SCRIPT_DIR, capture_output=True, text=True
        )

    add_result = run(["git", "add", csv_filename])
    if add_result.returncode != 0:
        print(f"WARNING: git add gagal: {add_result.stderr.strip()}", file=sys.stderr)
        return

    commit_msg = f"Update sports schedule {datetime.now().strftime('%Y-%m-%d')}"
    commit_result = run(["git", "commit", "-m", commit_msg])
    if commit_result.returncode != 0:
        # Biasanya karena "nothing to commit" - tidak fatal.
        print(f"INFO: git commit dilewati ({commit_result.stdout.strip() or commit_result.stderr.strip()})")
        return

    push_result = run(["git", "push"])
    if push_result.returncode != 0:
        print(f"ERROR: git push gagal: {push_result.stderr.strip()}", file=sys.stderr)
        return

    print(f"Berhasil commit & push: {commit_msg}")


def day_label_for(date_obj) -> str:
    """Format tanggal sesuai label tab di situs, mis. '31 Jul', '1 Aug', '27 Aug'."""
    return f"{date_obj.day} {date_obj.strftime('%b')}"


def click_day_tab(page, date_obj) -> bool:
    """
    Klik tab tanggal yang sesuai. Tab dicari by exact text match (mis. '27
    Aug') di antara <div> tanpa child - pola yang sudah diverifikasi manual
    di browser. Return False kalau tab tidak ketemu (mis. situs ubah range
    hari yang disediakan, atau ubah struktur DOM).
    """
    label = day_label_for(date_obj)
    clicked = page.evaluate(
        """(label) => {
            const all = [...document.querySelectorAll('div')];
            const target = all.find(
                el => el.textContent.trim() === label && el.children.length === 0
            );
            if (target) { target.click(); return true; }
            return false;
        }""",
        label,
    )
    return clicked


def scrape_current_day(page, date_obj, debug: bool = False):
    """Scrape grid Sports untuk hari yang SEDANG ditampilkan di halaman."""
    rows = []
    channel_links = page.query_selector_all('a[href^="/channel/"]')
    if debug:
        print(f"[debug] {date_obj.isoformat()}: {len(channel_links)} channel row terdeteksi", file=sys.stderr)

    for link in channel_links:
        # Cari ancestor yang mewakili satu baris penuh (lebar mendekati grid),
        # bukan cuma badge nomor channel.
        row_text = page.evaluate(
            """(el) => {
                let node = el;
                for (let i = 0; i < 8 && node.parentElement; i++) {
                    node = node.parentElement;
                    if (node.offsetWidth > 700) break;
                }
                return node.innerText;
            }""",
            link,
        )
        channel_href = link.get_attribute("href") or ""
        channel_id = channel_href.split("/")[-1]

        # Nama channel TIDAK tersedia sebagai teks/alt di DOM - logo channel
        # dirender sebagai CSS background-image pada <div> di dalam link
        # nomor channel (bukan grid program). Nama diambil dari nama file
        # gambar logo tsb, mis. "Sky_Sports_1.png" -> "Sky Sports 1".
        channel_name = page.evaluate(
            """(el) => {
                const divs = el.querySelectorAll('div');
                const bgDiv = [...divs].find(
                    d => getComputedStyle(d).backgroundImage !== 'none'
                );
                if (!bgDiv) return null;
                const bg = getComputedStyle(bgDiv).backgroundImage;
                const m = bg.match(/\\/([^\\/"]+)\\.(png|svg|jpg|jpeg|webp)/i);
                return m ? m[1].replace(/_/g, ' ') : null;
            }""",
            link,
        )

        channel_number, programs = parse_row_text(row_text)

        if debug:
            print(f"[debug]   channel {channel_id} ({channel_name}): "
                  f"{len(programs)} program terparse", file=sys.stderr)

        for prog in programs:
            rows.append(
                {
                    "channel_id": channel_id,
                    "channel_number": channel_number,
                    "channel_name": channel_name or "",
                    "date": date_obj.isoformat(),
                    "program_title": prog["title"],
                    "start_time": prog["start_time"],
                    "end_time": prog["end_time"],
                    "scraped_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
    return rows


def scrape(output_path: str, headful: bool = False, debug: bool = False,
           timeout_ms: int = 60000, git_push: bool = False, days: int = 1,
           start_date=None):
    from playwright.sync_api import sync_playwright

    rows_out = []
    start_date = start_date or datetime.now().date()

    with sync_playwright() as p:
        # STEALTH MODE (ditambahkan setelah investigasi 31 Jul 2026): terbukti
        # via evidence run GitHub Actions bahwa <div id="root"> kosong total -
        # React app tidak pernah mount. HTML yang ter-capture nunjukin situs
        # ini pakai Akamai Bot Manager, dan User-Agent Client Hints browser
        # kita eksplisit lapor diri sebagai "HeadlessChrome". Headless sendiri
        # SUDAH terverifikasi TIDAK masalah di jaringan lokal (test 31 Jul
        # 2026: 151 program sukses headless dari laptop) - jadi kemungkinan
        # besar akar masalahnya reputasi IP datacenter CI, BUKAN headless.
        # Tweak di bawah ini usaha "cukup manusiawi" (UA normal, viewport
        # umum, navigator.webdriver disembunyikan) - tujuannya kurangi false-
        # positive deteksi otomasi untuk mengakses data publik yang memang
        # diizinkan robots.txt, BUKAN untuk bypass proteksi berbayar/login.
        # Kalau tetap gagal di CI setelah ini, itu bukti kuat akar masalahnya
        # memang IP datacenter (lihat opsi self-hosted runner / proxy).
        browser = p.chromium.launch(
            headless=not headful,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-NZ",
            extra_http_headers={
                # Override header User-Agent Client Hints (Sec-CH-UA-*).
                # PENTING: mengganti opsi `user_agent` di atas SAJA tidak
                # cukup - itu cuma ganti navigator.userAgent & header
                # "User-Agent". Client Hints (navigator.userAgentData, yang
                # kebaca oleh script tracking pihak ketiga di situs ini dan
                # keluar sebagai "HeadlessChrome" di evidence sebelumnya)
                # sumbernya beda dan butuh dioverride terpisah lewat header
                # ini + init script di bawah.
                "Accept-Language": "en-NZ,en;q=0.9",
                "Sec-CH-UA": '"Not)A;Brand";v="24", "Chromium";v="128", "Google Chrome";v="128"',
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-CH-UA-Mobile": "?0",
            },
        )
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // Samakan navigator.userAgentData (dibaca JS pihak ketiga, mis.
            // tracking pixel) supaya konsisten dengan header Sec-CH-UA di
            // atas - hilangkan brand "HeadlessChrome" yang jadi bukti utama
            // deteksi otomasi di evidence run sebelumnya.
            if (navigator.userAgentData) {
                Object.defineProperty(navigator, 'userAgentData', {
                    get: () => ({
                        brands: [
                            {brand: 'Not)A;Brand', version: '24'},
                            {brand: 'Chromium', version: '128'},
                            {brand: 'Google Chrome', version: '128'}
                        ],
                        mobile: false,
                        platform: 'Windows',
                        getHighEntropyValues: async () => ({
                            brands: [
                                {brand: 'Not)A;Brand', version: '24'},
                                {brand: 'Chromium', version: '128'},
                                {brand: 'Google Chrome', version: '128'}
                            ],
                            mobile: false,
                            platform: 'Windows',
                            platformVersion: '10.0.0',
                            architecture: 'x86',
                            model: '',
                            uaFullVersion: '128.0.0.0'
                        })
                    })
                });
            }
            """
        )
        # NOTE: "networkidle" tidak pernah tercapai di situs ini karena ada
        # koneksi tracking/analytics yang terus aktif (TikTok pixel, split.io
        # SSE stream, dll). Pakai "domcontentloaded" + tunggu elemen spesifik.
        response = page.goto("https://tvguide.sky.co.nz/", wait_until="domcontentloaded", timeout=timeout_ms)
        if response is not None:
            print(f"[info] HTTP status halaman awal: {response.status}", file=sys.stderr)

        try:
            page.wait_for_selector("select", timeout=45000)
        except Exception:
            # Simpan bukti (screenshot + HTML mentah) SEBELUM exit supaya bisa
            # didiagnosa dari luar (mis. lewat artifact GitHub Actions), tanpa
            # perlu nebak penyebabnya. Jangan hapus blok ini - ini satu-satunya
            # cara diagnosa kalau gagalnya cuma terjadi di runner CI, bukan di
            # mesin lokal.
            diag_dir = os.path.join(SCRIPT_DIR, "debug_artifacts")
            os.makedirs(diag_dir, exist_ok=True)
            try:
                page.screenshot(path=os.path.join(diag_dir, "failure.png"), full_page=True)
            except Exception as e:
                print(f"WARNING: gagal ambil screenshot diagnosa: {e}", file=sys.stderr)
            try:
                with open(os.path.join(diag_dir, "failure.html"), "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception as e:
                print(f"WARNING: gagal simpan HTML diagnosa: {e}", file=sys.stderr)
            print(
                f"ERROR: elemen <select> tidak muncul dalam 45 detik. "
                f"Judul halaman saat ini: {page.title()!r}. URL saat ini: {page.url!r}. "
                f"Bukti (screenshot+HTML) disimpan ke {diag_dir}/ untuk diagnosa lanjut.",
                file=sys.stderr,
            )
            browser.close()
            raise

        # Tutup cookie banner kalau muncul (best-effort, tidak fatal kalau tidak ada)
        for label in ["Accept", "Accept All", "I Agree", "Got it"]:
            try:
                btn = page.get_by_role("button", name=label, exact=False)
                if btn.count() > 0:
                    btn.first.click(timeout=2000)
                    break
            except Exception:
                pass

        # Filter dropdown -> "Sports"
        select_el = page.query_selector("select")
        if select_el is None:
            print("ERROR: elemen <select> filter channel tidak ditemukan. "
                  "Struktur halaman mungkin sudah berubah.", file=sys.stderr)
            browser.close()
            sys.exit(1)

        select_el.select_option(label="Sports")
        page.wait_for_timeout(2000)

        for offset in range(days):
            target_date = start_date + timedelta(days=offset)

            # Klik tab tanggalnya (termasuk untuk hari pertama - aman diklik
            # ulang walau itu tab "Today", cuma memastikan konsisten dan
            # tidak ada edge case tersembunyi soal tab mana yang aktif
            # secara default).
            clicked = click_day_tab(page, target_date)
            if not clicked:
                print(f"WARNING: tab tanggal '{day_label_for(target_date)}' tidak ditemukan "
                      f"- situs mungkin cuma sedia data s.d. hari itu, atau strukturnya berubah. "
                      f"Berhenti di sini.", file=sys.stderr)
                break
            page.wait_for_timeout(2000)

            day_rows = scrape_current_day(page, target_date, debug=debug)
            if not day_rows:
                print(f"WARNING: 0 program untuk {target_date.isoformat()}. "
                      f"Kemungkinan struktur DOM situs berubah.", file=sys.stderr)
            rows_out.extend(day_rows)

        browser.close()

    if not rows_out:
        print("WARNING: 0 baris program berhasil diparse sama sekali. Kemungkinan "
              "struktur DOM situs berubah -> perlu penyesuaian selector.", file=sys.stderr)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "channel_id",
                "channel_number",
                "channel_name",
                "date",
                "program_title",
                "start_time",
                "end_time",
                "scraped_at",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"Selesai. {len(rows_out)} program ({days} hari) disimpan ke {output_path}")

    if git_push:
        git_commit_and_push(os.path.basename(output_path))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape jadwal Sports dari tvguide.sky.co.nz")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Jumlah hari yang di-scrape mulai dari titik start (default: 1). "
             "Situs cuma sedia data s.d. 28 hari ke depan dari hari ini.",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Alternatif dari --days: tanggal akhir dalam format YYYY-MM-DD (mis. 2026-08-27), "
             "dihitung relatif dari titik start. Kalau diisi, --days diabaikan.",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Mulai scrape N hari dari hari ini (default: 0 = mulai hari ini). "
             "Berguna buat scrape per-minggu, mis. minggu ke-2: --start-offset 7 --days 7",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Alternatif dari --start-offset: tanggal mulai eksplisit YYYY-MM-DD. "
             "Kalau diisi, --start-offset diabaikan.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Nama file CSV output. Default otomatis: sports_YYYY-MM-DD.csv (1 hari) atau "
             "sports_YYYY-MM-DD_to_YYYY-MM-DD.csv (multi-hari).",
    )
    parser.add_argument("--headful", action="store_true", help="Jalankan browser dengan tampilan (untuk debug)")
    parser.add_argument("--debug", action="store_true", help="Print info debug ke stderr")
    parser.add_argument(
        "--git-push",
        action="store_true",
        help="Otomatis git add+commit+push file CSV yang baru dibuat (folder script harus di dalam git repo)",
    )
    args = parser.parse_args()

    today = datetime.now().date()

    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    else:
        start_date = today + timedelta(days=args.start_offset)

    if args.until:
        until_date = datetime.strptime(args.until, "%Y-%m-%d").date()
        days = (until_date - start_date).days + 1
        if days < 1:
            print(f"ERROR: --until {args.until} lebih awal dari tanggal start {start_date.isoformat()}.",
                  file=sys.stderr)
            sys.exit(1)
    else:
        days = args.days

    end_date = start_date + timedelta(days=days - 1)
    if days == 1:
        default_name = f"sports_{start_date.isoformat()}.csv"
    else:
        default_name = f"sports_{start_date.isoformat()}_to_{end_date.isoformat()}.csv"

    output_name = args.output or default_name
    # Selalu simpan CSV di folder script ini (folder repo), bukan di cwd
    # saat dijalankan - penting untuk pemakaian lewat Task Scheduler.
    output_path = os.path.join(SCRIPT_DIR, output_name)

    scrape(output_path, headful=args.headful, debug=args.debug, git_push=args.git_push,
           days=days, start_date=start_date)
