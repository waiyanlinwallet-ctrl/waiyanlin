import os
import re
import sys
import time
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import requests
import fitz
from bs4 import BeautifulSoup
from telegram import Bot, InputMediaPhoto

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("yadanabon-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
RENDER_DPI = int(os.getenv("RENDER_DPI", "160"))
MYAWADY_INDEX_URL = os.getenv(
    "MYAWADY_INDEX_URL",
    "https://myawady.net.mm/pdf-file-amiuacaarern/rttnaapunsttngcaa"
)
REQUEST_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; YadanabonDailyNewsBot/1.0)"}
STATE_FILE = os.getenv("STATE_FILE", "state/posted_dates.txt")
TEMP_DIR = "/tmp/yadanabon_bot"
os.makedirs(TEMP_DIR, exist_ok=True)


def myanmar_today():
    return datetime.now(ZoneInfo("Asia/Yangon")).date()


def today_candidates(target):
    d, m, y = target.day, target.month, target.year
    return [
        f"https://myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://www.myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://www.myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa",
    ]


def fetch_html(url):
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def page_date_matches(text, target):
    d, m, y = target.day, target.month, target.year
    patterns = [
        rf"(?<!\d)0?{d}\s*[-/\.]\s*0?{m}\s*[-/\.]\s*{y}(?!\d)",
        rf"(?<!\d){y}\s*[-/\.]\s*0?{m}\s*[-/\.]\s*0?{d}(?!\d)",
    ]
    return any(re.search(p, text) for p in patterns)


def find_pdf_link(html, page_url, target):
    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    date_evidence = page_date_matches(title + " " + visible_text, target)
    url_evidence = f"/{target.day}-{target.month}-{target.year}-" in page_url

    candidates = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        label = (a.get_text(" ", strip=True) + " " + href).lower()
        if ".pdf" in href.lower():
            if any(word in label for word in ["rttna", "ydnp", "yad", "newspaper", "သတင်းစာ"]):
                candidates.insert(0, href)
            else:
                candidates.append(href)

    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = tag.get("src") or tag.get("data")
        if src:
            href = urljoin(page_url, src)
            if ".pdf" in href.lower():
                candidates.append(href)

    if date_evidence or url_evidence:
        return candidates[0] if candidates else None
    return None


def discover_today_pdf():
    target = myanmar_today()
    session = requests.Session()
    session.headers.update(HEADERS)

    for page_url in today_candidates(target):
        try:
            response = session.get(page_url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            pdf_url = find_pdf_link(response.text, page_url, target)
            if pdf_url:
                log.info("Found today's PDF: %s", pdf_url)
                return pdf_url
        except Exception as exc:
            log.warning("Could not check %s: %s", page_url, exc)

    try:
        response = session.get(MYAWADY_INDEX_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        seen = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(MYAWADY_INDEX_URL, a["href"])
            label = a.get_text(" ", strip=True)
            if href in seen:
                continue
            seen.add(href)
            if "rttnaapunsttngcaa" not in href.lower() and "ရတနာပုံ" not in label:
                continue
            if not page_date_matches(label + " " + href, target):
                continue
            html = fetch_html(href)
            if html:
                pdf_url = find_pdf_link(html, href, target)
                if pdf_url:
                    log.info("Found today's PDF: %s", pdf_url)
                    return pdf_url
    except Exception as exc:
        log.warning("Index scan failed: %s", exc)

    return None


def load_posted_dates():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()


def save_posted_date(day_key):
    parent = os.path.dirname(STATE_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(day_key + "\n")


def download_pdf(url, day_key):
    path = os.path.join(TEMP_DIR, f"{day_key}.pdf")
    with requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").lower()
        first_chunk = True
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                if first_chunk:
                    first_chunk = False
                    if not (chunk.startswith(b"%PDF") or "pdf" in content_type):
                        raise ValueError("Downloaded file does not appear to be a PDF.")
                f.write(chunk)
    if os.path.getsize(path) < 1000:
        raise ValueError("Downloaded PDF is unexpectedly small.")
    return path


def convert_pdf_to_jpg(pdf_path, day_key):
    doc = fitz.open(pdf_path)
    output_paths = []
    matrix = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
    for index, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = os.path.join(TEMP_DIR, f"{day_key}_page_{index + 1:03d}.jpg")
        pix.save(out_path, jpg_quality=90)
        output_paths.append(out_path)
    doc.close()
    return output_paths


def post_images(bot, image_paths, day_key):
    total = len(image_paths)
    display_date = datetime.strptime(day_key, "%Y-%m-%d").strftime("%d-%m-%Y")
    for start in range(0, total, 10):
        batch = image_paths[start:start + 10]
        media = []
        opened = []
        try:
            for offset, path in enumerate(batch):
                f = open(path, "rb")
                opened.append(f)
                page_number = start + offset + 1
                caption = f"ရတနာပုံသတင်းစာ — {display_date}\nစာမျက်နှာ {page_number}/{total}" if offset == 0 else None
                media.append(InputMediaPhoto(media=f, caption=caption))
            bot.send_media_group(
                chat_id=CHANNEL_ID, media=media,
                read_timeout=120, write_timeout=120
            )
            time.sleep(2)
        finally:
            for f in opened:
                f.close()


def run_once():
    target = myanmar_today()
    day_key = target.strftime("%Y-%m-%d")
    if day_key in load_posted_dates():
        log.info("Already posted for Myanmar date %s; skipping.", day_key)
        return

    pdf_url = discover_today_pdf()
    if not pdf_url:
        log.info("Today's PDF not found yet (%s). No post made; next scheduled run will retry.", day_key)
        return

    pdf_path = download_pdf(pdf_url, day_key)
    image_paths = convert_pdf_to_jpg(pdf_path, day_key)
    if not image_paths:
        raise RuntimeError("PDF conversion returned no images.")

    bot = Bot(token=BOT_TOKEN)
    post_images(bot, image_paths, day_key)
    save_posted_date(day_key)
    log.info("Posted %s (%d pages).", day_key, len(image_paths))


def main():
    if "--once" in sys.argv:
        run_once()
        return

    log.info("This project is designed for scheduled GitHub Actions runs. Use: python main.py --once")
    run_once()


if __name__ == "__main__":
    main()
