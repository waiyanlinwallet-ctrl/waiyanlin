import os
import re
import sys
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse, unquote

import requests
import certifi
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
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}
STATE_FILE = os.getenv("STATE_FILE", "state/posted_dates.txt")
TEMP_DIR = "/tmp/yadanabon_bot"
os.makedirs(TEMP_DIR, exist_ok=True)

# Verify TLS certificates normally. A TLS bypass is intentionally NOT used:
# disabling certificate checks could allow a fake page/PDF to be downloaded.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = certifi.where()


def myanmar_today():
    return datetime.now(ZoneInfo("Asia/Yangon")).date()


def today_candidates(target):
    d, m, y = target.day, target.month, target.year
    return [
        f"https://myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa-0",
        f"https://www.myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa-0",
        f"https://myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://www.myawady.net.mm/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa-0",
        f"https://www.myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa-0",
        f"https://myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa",
        f"https://www.myawady.net.mm/index.php/{d}-{m}-{y}-rttnaapunsttngcaa",
    ]


def fetch_response(url):
    response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response


def fetch_html(url):
    response = fetch_response(url)
    return response.text if response is not None else None


def page_date_matches(text, target):
    d, m, y = target.day, target.month, target.year
    patterns = [
        rf"(?<!\d)0?{d}\s*[-/\.]\s*0?{m}\s*[-/\.]\s*{y}(?!\d)",
        rf"(?<!\d){y}\s*[-/\.]\s*0?{m}\s*[-/\.]\s*0?{d}(?!\d)",
    ]
    return any(re.search(p, text) for p in patterns)


def _looks_like_pdf_url(url):
    path = unquote(urlparse(url).path).lower()
    return (
        path.endswith(".pdf")
        or ".pdf?" in url.lower()
        or "download" in path
        or "pdf" in path
    )


def _pdf_candidates_from_html(html, page_url):
    """Collect PDF links, embedded viewer URLs and PDF.js-style file parameters."""
    soup = BeautifulSoup(html, "html.parser")
    found = []

    def add(value):
        if not value:
            return
        value = value.strip().replace("&amp;", "&")
        if value.startswith(("javascript:", "data:", "#")):
            return
        full = urljoin(page_url, value)
        if full not in found:
            found.append(full)

    for tag in soup.find_all(["a", "iframe", "embed", "object", "source"]):
        for attr in ("href", "src", "data"):
            add(tag.get(attr))

    # Some PDF viewers keep the actual file URL in a query parameter, e.g. ?file=...
    for tag in soup.find_all(True):
        for attr in ("data-url", "data-file", "data-pdf", "data-document-url"):
            add(tag.get(attr))

    # Inspect script text for common PDF file paths and PDF viewer config.
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=True)
        if not text:
            continue
        patterns = [
            r"""["']([^"'<>\\\s]+\.pdf(?:\?[^"'<>\\\s]*)?)["']""",
            r"""(?:file|url|pdfUrl|pdf_url)\s*[:=]\s*["']([^"']+)["']""",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                add(match.replace("\\/", "/"))

    # Prefer actual PDF-looking paths, then viewer URLs as a fallback.
    pdf_like = [u for u in found if _looks_like_pdf_url(u)]
    other_embeds = [
        u for u in found
        if any(word in u.lower() for word in ("pdf", "viewer", "document"))
    ]
    return pdf_like + [u for u in other_embeds if u not in pdf_like]


def find_pdf_link(html, page_url, target):
    soup = BeautifulSoup(html, "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    date_evidence = page_date_matches(title + " " + visible_text, target)
    url_evidence = (
        f"/{target.day}-{target.month}-{target.year}-" in page_url
        or f"{target.day}-{target.month}-{target.year}-" in page_url
    )
    if not (date_evidence or url_evidence):
        return None

    candidates = _pdf_candidates_from_html(html, page_url)
    if not candidates:
        return None

    # Try candidates in preferred order; only accept URLs that return PDF bytes.
    for candidate in candidates:
        try:
            with SESSION.get(
                candidate, timeout=REQUEST_TIMEOUT, stream=True,
                headers={"Range": "bytes=0-1023"}
            ) as response:
                if response.status_code not in (200, 206):
                    continue
                content_type = response.headers.get("content-type", "").lower()
                first = next(response.iter_content(chunk_size=1024), b"")
                if first.startswith(b"%PDF") or "application/pdf" in content_type:
                    return candidate
        except requests.RequestException as exc:
            log.debug("Candidate is not a directly reachable PDF (%s): %s", candidate, exc)
    return None


def discover_today_pdf():
    target = myanmar_today()
    seen_pages = set()

    for page_url in today_candidates(target):
        if page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        try:
            response = fetch_response(page_url)
            if response is None:
                continue
            pdf_url = find_pdf_link(response.text, response.url, target)
            if pdf_url:
                log.info("Found today's PDF: %s", pdf_url)
                return pdf_url
        except requests.exceptions.SSLError as exc:
            log.error(
                "TLS certificate verification failed for %s. "
                "Will not disable TLS verification automatically: %s",
                page_url, exc
            )
        except Exception as exc:
            log.warning("Could not check %s: %s", page_url, exc)

    try:
        response = fetch_response(MYAWADY_INDEX_URL)
        if response is None:
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        page_candidates = []
        for a in soup.find_all("a", href=True):
            href = urljoin(response.url, a["href"])
            label = a.get_text(" ", strip=True)
            if (
                "rttnaapunsttngcaa" in href.lower()
                or "ရတနာပုံ" in label
            ) and page_date_matches(label + " " + href, target):
                page_candidates.append(href)

        for href in dict.fromkeys(page_candidates):
            try:
                html = fetch_html(href)
                if html:
                    pdf_url = find_pdf_link(html, href, target)
                    if pdf_url:
                        log.info("Found today's PDF: %s", pdf_url)
                        return pdf_url
            except Exception as exc:
                log.warning("Could not inspect index result %s: %s", href, exc)
    except requests.exceptions.SSLError as exc:
        log.error("TLS certificate verification failed for index page: %s", exc)
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
    with SESSION.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
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
                        raise ValueError(
                            f"Downloaded URL did not return a PDF "
                            f"(content-type: {content_type})."
                        )
                f.write(chunk)
    if os.path.getsize(path) < 1000:
        raise ValueError("Downloaded PDF is unexpectedly small.")
    # Validate the file before trying to render it.
    with fitz.open(path) as doc:
        if len(doc) == 0:
            raise ValueError("PDF has no pages.")
    return path


def convert_pdf_to_jpg(pdf_path, day_key):
    doc = fitz.open(pdf_path)
    output_paths = []
    matrix = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
    try:
        for index, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out_path = os.path.join(
                TEMP_DIR, f"{day_key}_page_{index + 1:03d}.jpg"
            )
            pix.save(out_path, jpg_quality=90)
            output_paths.append(out_path)
    finally:
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
                caption = (
                    f"ရတနာပုံသတင်းစာ — {display_date}\n"
                    f"စာမျက်နှာ {page_number}/{total}"
                    if offset == 0 else None
                )
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
        log.error(
            "Today's PDF was not found (%s). Check TLS errors and the "
            "Myawady page's embedded PDF viewer URL in the Actions logs.",
            day_key
        )
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
    log.info("Starting Yadanabon Daily Newspaper job.")
    run_once()


if __name__ == "__main__":
    main()
