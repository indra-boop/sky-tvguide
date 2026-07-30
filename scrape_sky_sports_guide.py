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
    # Default: output otomatis diberi nama sports_YYYY-MM-DD.csv (arsip harian)
    python scrape_sky_sports_guide.py
    python scrape_sky_sports_guide.py --headful --debug

    # Arsip harian + otomatis commit & push ke git repo (folder script ini
    # harus berada di dalam git repo yang sudah di-setup remote & auth-nya)
    python scrape_sky_sports_guide.py --git-push

    # Override nama file kalau perlu
    python scrape_sky_sports_guide.py --output custom_name.csv
"""

import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import datetime

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


def scrape(output_path: str, headful: bool = False, debug: bool = False,
           timeout_ms: int = 60000, git_push: bool = False):
    from playwright.sync_api import sync_playwright

    rows_out = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headful,
        )
        page = browser.new_page()
        # NOTE: "networkidle" tidak pernah tercapai di situs ini karena ada
        # koneksi tracking/analytics yang terus aktif (TikTok pixel, split.io
        # SSE stream, dll). Pakai "domcontentloaded" + tunggu elemen spesifik.
        page.goto("https://tvguide.sky.co.nz/", wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_selector("select", timeout=30000)

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

        channel_links = page.query_selector_all('a[href^="/channel/"]')
        if debug:
            print(f"[debug] jumlah channel row terdeteksi: {len(channel_links)}", file=sys.stderr)

        if not channel_links:
            print("ERROR: tidak ada channel row ditemukan setelah filter Sports. "
                  "Cek screenshot / jalankan --headful --debug.", file=sys.stderr)
            browser.close()
            sys.exit(1)

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
                print(f"[debug] channel {channel_id} ({channel_name}): "
                      f"{len(programs)} program terparse", file=sys.stderr)

            for prog in programs:
                rows_out.append(
                    {
                        "channel_id": channel_id,
                        "channel_number": channel_number,
                        "channel_name": channel_name or "",
                        "program_title": prog["title"],
                        "start_time": prog["start_time"],
                        "end_time": prog["end_time"],
                        "scraped_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )

        browser.close()

    if not rows_out:
        print("WARNING: 0 baris program berhasil diparse. Kemungkinan struktur "
              "DOM situs berubah -> perlu penyesuaian selector.", file=sys.stderr)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "channel_id",
                "channel_number",
                "channel_name",
                "program_title",
                "start_time",
                "end_time",
                "scraped_at",
            ],
        )
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"Selesai. {len(rows_out)} program disimpan ke {output_path}")

    if git_push:
        git_commit_and_push(os.path.basename(output_path))


if __name__ == "__main__":
    default_name = f"sports_{datetime.now().strftime('%Y-%m-%d')}.csv"

    parser = argparse.ArgumentParser(description="Scrape jadwal Sports dari tvguide.sky.co.nz")
    parser.add_argument(
        "--output",
        default=None,
        help=f"Nama file CSV output. Default: {default_name} (arsip harian, otomatis pakai tanggal hari ini)",
    )
    parser.add_argument("--headful", action="store_true", help="Jalankan browser dengan tampilan (untuk debug)")
    parser.add_argument("--debug", action="store_true", help="Print info debug ke stderr")
    parser.add_argument(
        "--git-push",
        action="store_true",
        help="Otomatis git add+commit+push file CSV yang baru dibuat (folder script harus di dalam git repo)",
    )
    args = parser.parse_args()

    output_name = args.output or default_name
    # Selalu simpan CSV di folder script ini (folder repo), bukan di cwd
    # saat dijalankan - penting untuk pemakaian lewat Task Scheduler.
    output_path = os.path.join(SCRIPT_DIR, output_name)

    scrape(output_path, headful=args.headful, debug=args.debug, git_push=args.git_push)
