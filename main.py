import os
import re
import time
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, unquote

import requests
import fitz  # PyMuPDF
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

# Render's persistent disk is not guaranteed on every plan. A state file helps
# prevent duplicates when the service has a persistent disk mounted at /data.
STATE_FILE = os.getenv("STATE_FILE", "/data/posted_dates.txt")
TEMP_DIR = "/tmp/yadanabon_bot"
os.makedirs(TEMP_DIR, exist_ok=True)


def myanmar_now():
    return datetime.now(ZoneInfo("Asia/Yangon"))


def today_candidates():
    """Possible URL slugs for today's page, in Myanmar time."""
    today = myanmar_now().date()
    d, m, y = today.day, today.month, today.year
    return [
        f"https://myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://www.myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://www.myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa",
    ]


def fetch_html(url):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def page_date_matches(text, target):
    """Check for a date written as d-m-yyyy, dd-mm-yyyy, or similar."""
    d, m, y = target.day, target.month, target.year
    patterns = [
        rf"(?<!\d)0?{d}\s*[-/\.]\s*0?{m}\s*[-/\.]\s*{y}(?!\d)",
        rf"(?<!\d){y}\s*[-/\.]\s*0?{m}\s*[-/\.]\s*0?{d}(?!\d)",
    ]
    return any(re.search(p, text) for p in patterns)


def find_pdf_link(html, page_url, target):
    soup = BeautifulSoup(html, "html.parser")
    # Prefer PDF links on a page whose title/body refers to today's date.
    visible_text = soup.get_text(" ", strip=True)
    if not page_date_matches(visible_text, target):
        # A date-specific URL can still be useful if the page uses a different
        # localized date format. Check title and PDF filename as well below.
        pass

    candidates = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        label = (a.get_text(" ", strip=True) + " " + href).lower()
        if ".pdf" not in href.lower():
            continue
        if any(word in label for word in ["rttna", "ydnp", "yad", "newspaper", "သတင်းစာ"]):
            candidates.insert(0, href)
        else:
            candidates.append(href)

    # Some pages use a linked file or embed rather than a normal anchor.
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = tag.get("src") or tag.get("data")
        if src:
            href = urljoin(page_url, src)
            if ".pdf" in href.lower():
                candidates.append(href)

    # Return only if the page appears to be today's newspaper page.
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    date_evidence = page_date_matches(page_title + " " + visible_text, target)
    if date_evidence or re.search(rf"/{target.day}-{target.month}-{target.year}-", page_url):
        return candidates[0] if candidates else None
    return None


def discover_today_pdf():
    target = myanmar_now().date()
    session = requests.Session()
    session.headers.update(HEADERS)

    # First try likely date-specific page URLs.
    for page_url in today_candidates():
        try:
            response = session.get(page_url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            pdf_url = find_pdf_link(response.text, page_url, target)
            if pdf_url:
                log.info("Found today's PDF at %s", pdf_url)
                return pdf_url
        except Exception as exc:
            log.warning("Could not check %s: %s", page_url, exc)

    # Then inspect the category/index page and select a matching dated article.
    try:
        response = session.get(MYAWADY_INDEX_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        possible_pages = []
        for a in soup.find_all("a", href=True):
            href = urljoin(MYAWADY_INDEX_URL, a["href"])
            label = a.get_text(" ", strip=True)
            if "rttnaapunsttngcaa" in href.lower() or "ရတနာပုံ" in label:
                possible_pages.append((href, label))
        seen = set()
        for page_url, label in possible_pages:
            if page_url in seen:
                continue
            seen.add(page_url)
            if not page_date_matches(label + " " + page_url, target):
                continue
            html = fetch_html(page_url)
            if html:
                pdf_url = find_pdf_link(html, page_url, target)
                if pdf_url:
                    log.info("Found today's PDF at %s", pdf_url)
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
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "a", encoding="utf-8") as f:
        f.write(day_key + "\n")


def download_pdf(url, day_key):
    path = os.path.join(TEMP_DIR, f"{day_key}.pdf")
    with requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").lower()
        first = True
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                if first:
                    first = False
                    if not (chunk.startswith(b"%PDF") or "pdf" in content_type):
                        raise ValueError("The downloaded file does not appear to be a PDF.")
                f.write(chunk)
    if os.path.getsize(path) < 1000:
        raise ValueError("Downloaded PDF is unexpectedly small.")
    return path


def convert_pdf_to_jpg(pdf_path, day_key):
    doc = fitz.open(pdf_path)
    output_paths = []
    zoom = RENDER_DPI / 72
    matrix = fitz.Matrix(zoom, zoom)
    for index, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        out_path = os.path.join(TEMP_DIR, f"{day_key}_page_{index + 1:03d}.jpg")
        pix.save(out_path, jpg_quality=90)
        output_paths.append(out_path)
    doc.close()
    return output_paths


def caption_for(day_key, page_number, total):
    d = datetime.strptime(day_key, "%Y-%m-%d").strftime("%d-%m-%Y")
    return f"ရတနာပုံသတင်းစာ — {d}\nစာမျက်နှာ {page_number}/{total}"


def post_images(bot, image_paths, day_key):
    # Telegram media groups allow up to 10 media items per group.
    total = len(image_paths)
    for start in range(0, total, 10):
        batch = image_paths[start:start + 10]
        media = []
        opened_files = []
        try:
            for offset, path in enumerate(batch):
                f = open(path, "rb")
                opened_files.append(f)
                page_no = start + offset + 1
                media.append(InputMediaPhoto(
                    media=f,
                    caption=caption_for(day_key, page_no, total) if offset == 0 else None
                ))
            bot.send_media_group(chat_id=CHANNEL_ID, media=media, read_timeout=120, write_timeout=120)
            log.info("Posted pages %s-%s of %s", start + 1, start + len(batch), total)
            time.sleep(2)
        finally:
            for f in opened_files:
                f.close()


def run_once(bot):
    now = myanmar_now()
    day_key = now.strftime("%Y-%m-%d")
    posted = load_posted_dates()
    if day_key in posted:
        log.info("Already posted for %s; skipping.", day_key)
        return

    pdf_url = discover_today_pdf()
    if not pdf_url:
        log.info("Today's PDF not found yet (%s). Will retry later.", day_key)
        return

    pdf_path = download_pdf(pdf_url, day_key)
    images = convert_pdf_to_jpg(pdf_path, day_key)
    if not images:
        raise RuntimeError("PDF conversion returned no images.")

    post_images(bot, images, day_key)
    save_posted_date(day_key)
    log.info("Finished posting %s (%d pages).", day_key, len(images))


def main():
    bot = Bot(token=BOT_TOKEN)
    log.info("Yadanabon Daily News bot started. Checking every %s minutes.", CHECK_INTERVAL_MINUTES)
    while True:
        try:
            run_once(bot)
        except Exception:
            log.exception("Run failed; will retry on next cycle.")
        time.sleep(max(5, CHECK_INTERVAL_MINUTES) * 60)


if __name__ == "__main__":
    main()
