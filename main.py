import os
import json
import logging
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import fitz  # PyMuPDF
import requests
from flask import Flask, request, jsonify

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pdf-jpg-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_DPI = max(100, min(int(os.getenv("RENDER_DPI", "160")), 300))
MAX_PDF_MB = max(1, int(os.getenv("MAX_PDF_MB", "40")))
PORT = int(os.getenv("PORT", "10000"))
# Ignore an identical PDF for 24 hours after successful processing.
DUPLICATE_WINDOW_SECONDS = 24 * 60 * 60
DB_PATH = os.getenv("DEDUP_DB_PATH", "/tmp/pdf_jpg_seen.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable")
if not CHANNEL_ID:
    raise RuntimeError("Missing CHANNEL_ID environment variable")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError(
        "Missing RENDER_EXTERNAL_URL. Deploy this as a Render Web Service so Render sets it."
    )

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/telegram-webhook"

app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "YadanabonPDFtoJPGBot/1.1"})

_seen_updates = set()
_seen_updates_lock = threading.Lock()
_MAX_SEEN_UPDATES = 5000
_db_lock = threading.Lock()


def init_db():
    with _db_lock:
        with sqlite3.connect(DB_PATH, timeout=30) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS processed_docs ("
                "file_key TEXT PRIMARY KEY, processed_at INTEGER NOT NULL, "
                "status TEXT NOT NULL)"
            )
            db.commit()


def claim_document(file_key):
    """Return True only when this PDF has not already been processed recently."""
    now = int(time.time())
    cutoff = now - DUPLICATE_WINDOW_SECONDS
    with _db_lock:
        with sqlite3.connect(DB_PATH, timeout=30) as db:
            db.execute("DELETE FROM processed_docs WHERE processed_at < ?", (cutoff,))
            row = db.execute(
                "SELECT status FROM processed_docs WHERE file_key = ?", (file_key,)
            ).fetchone()
            if row and row[0] in ("processing", "done"):
                db.commit()
                return False
            db.execute(
                "INSERT OR REPLACE INTO processed_docs(file_key, processed_at, status) "
                "VALUES (?, ?, 'processing')",
                (file_key, now),
            )
            db.commit()
            return True


def mark_document(file_key, status):
    with _db_lock:
        with sqlite3.connect(DB_PATH, timeout=30) as db:
            if status == "failed":
                # Allow the user to retry a failed PDF immediately.
                db.execute("DELETE FROM processed_docs WHERE file_key = ?", (file_key,))
            else:
                db.execute(
                    "UPDATE processed_docs SET processed_at = ?, status = ? WHERE file_key = ?",
                    (int(time.time()), status, file_key),
                )
            db.commit()


def telegram(method, data=None, files=None, timeout=60):
    response = session.post(
        f"{API}/{method}", data=data, files=files, timeout=timeout
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {result}")
    return result["result"]


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    telegram("sendMessage", payload)


def set_bot_commands():
    """Add the Telegram menu command so users can tap /start easily."""
    telegram(
        "setMyCommands",
        {
            "commands": json.dumps(
                [
                    {"command": "start", "description": "စတင်ရန်"},
                    {"command": "help", "description": "အသုံးပြုပုံ"},
                ],
                ensure_ascii=False,
            )
        },
    )


def welcome_message(chat_id):
    keyboard = {
        "keyboard": [[{"text": "📄 PDF စတင်ပို့ရန်"}]],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "PDF ဖိုင်ကို ဒီမှာ ပို့ပါ",
    }
    send_message(
        chat_id,
        "မင်္ဂလာပါ။ ယနေ့ထုတ် ရတနာပုံသတင်းစာ PDF ကို ဒီ Bot ဆီ ပို့ပါ။\n"
        "PDF စာမျက်နှာတိုင်းကို JPG ပြောင်းပြီး Channel မှာ တင်ပေးပါမယ်။\n\n"
        "စတင်ရန် အောက်က 📄 PDF စတင်ပို့ရန် ခလုတ်ကို နှိပ်ပြီး PDF ဖိုင်ကို ပို့ပါ။",
        keyboard,
    )


def set_webhook():
    payload = {"url": WEBHOOK_URL, "allowed_updates": ["message"]}
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    result = telegram("setWebhook", payload)
    log.info("Telegram webhook configured: %s; result=%s", WEBHOOK_URL, result)


def get_pdf_file(file_id):
    result = telegram("getFile", {"file_id": file_id})
    file_path = result.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram did not return a file_path")
    url = f"{FILE_API}/{file_path}"
    response = session.get(url, stream=True, timeout=120)
    response.raise_for_status()
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_PDF_MB * 1024 * 1024:
        raise ValueError(f"PDF is larger than {MAX_PDF_MB} MB")
    data = bytearray()
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if chunk:
            data.extend(chunk)
            if len(data) > MAX_PDF_MB * 1024 * 1024:
                raise ValueError(f"PDF is larger than {MAX_PDF_MB} MB")
    if not data.startswith(b"%PDF"):
        raise ValueError("The received file does not look like a PDF")
    return bytes(data)


def pdf_to_jpegs(pdf_bytes, output_dir):
    pdf_path = Path(output_dir) / "input.pdf"
    pdf_path.write_bytes(pdf_bytes)
    output_paths = []
    scale = RENDER_DPI / 72
    matrix = fitz.Matrix(scale, scale)
    with fitz.open(pdf_path) as doc:
        if doc.page_count < 1:
            raise ValueError("PDF has no pages")
        if doc.page_count > 500:
            raise ValueError("PDF has more than 500 pages; refusing to process it")
        for index, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            output_path = Path(output_dir) / f"page_{index + 1:04d}.jpg"
            pix.save(output_path, jpg_quality=88)
            output_paths.append(output_path)
    return output_paths


def post_jpegs(paths, caption):
    # Telegram media groups allow at most 10 items per group.
    for start in range(0, len(paths), 10):
        batch = paths[start:start + 10]
        opened = []
        media = []
        try:
            for i, path in enumerate(batch):
                handle = open(path, "rb")
                opened.append(handle)
                item = {"type": "photo", "media": f"attach://photo{i}"}
                if start == 0 and i == 0 and caption:
                    item["caption"] = caption[:1024]
                media.append(item)
            multipart = {f"photo{i}": handle for i, handle in enumerate(opened)}
            telegram(
                "sendMediaGroup",
                {"chat_id": CHANNEL_ID, "media": json.dumps(media)},
                files=multipart,
                timeout=180,
            )
        finally:
            for handle in opened:
                handle.close()


def process_document(message, file_key):
    chat_id = message["chat"]["id"]
    document = message.get("document") or {}
    filename = document.get("file_name", "document.pdf")
    mime_type = document.get("mime_type", "")
    file_id = document.get("file_id")
    try:
        if not file_id:
            raise ValueError("ဖိုင်ကို မဖတ်နိုင်ပါ။ PDF ဖိုင်ကို ပြန်ပို့ပေးပါ။")
        if not filename.lower().endswith(".pdf") and mime_type != "application/pdf":
            raise ValueError("PDF ဖိုင်ကိုပဲ ပို့ပေးပါ။")
        size = int(document.get("file_size") or 0)
        if size > MAX_PDF_MB * 1024 * 1024:
            raise ValueError(f"PDF ဖိုင်က {MAX_PDF_MB} MB ထက်ကြီးနေပါတယ်။")

        send_message(chat_id, "PDF လက်ခံရရှိပါပြီ။ JPG ပုံတွေပြောင်းပြီး Channel မှာ တင်နေပါတယ်။")
        pdf_bytes = get_pdf_file(file_id)
        with tempfile.TemporaryDirectory(prefix="pdf-jpg-") as tmp:
            paths = pdf_to_jpegs(pdf_bytes, tmp)
            caption = "ယနေ့ထုတ် ရတနာပုံသတင်းစာ"
            post_jpegs(paths, caption)
            count = len(paths)
        mark_document(file_key, "done")
        send_message(chat_id, f"ပြီးပါပြီ။ JPG ပုံ {count} ပုံကို Channel မှာ တင်ပြီးပါပြီ။")
    except Exception as exc:
        mark_document(file_key, "failed")
        log.exception("Failed to process PDF")
        try:
            if isinstance(exc, ValueError) and str(exc).startswith(("PDF ဖိုင်", "PDF ဖိုင်ကို", "ဖိုင်ကို")):
                error_text = str(exc)
            else:
                error_text = "PDF ပြောင်းပြီး Channel တင်ရာမှာ အမှားဖြစ်သွားပါတယ်။ Render Logs ကို စစ်ဆေးပေးပါ။"
            send_message(chat_id, error_text)
        except Exception:
            log.exception("Could not send error message")


def handle_update(update, file_key=None):
    try:
        message = update.get("message")
        if message and message.get("document") and file_key:
            process_document(message, file_key)
        elif message and message.get("text", "").split("@")[0] in ("/start", "/help"):
            welcome_message(message["chat"]["id"])
        elif message and message.get("text") == "📄 PDF စတင်ပို့ရန်":
            send_message(
                message["chat"]["id"],
                "အခု ယနေ့ထုတ် ရတနာပုံသတင်းစာ PDF ဖိုင်ကို ပို့ပေးပါ။\n"
                "စာမျက်နှာအားလုံးကို JPG ပြောင်းပြီး Channel မှာ တင်ပေးပါမယ်။",
            )
    except Exception:
        log.exception("Unhandled error while processing Telegram update")


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "pdf-to-jpg-telegram-bot"}), 200


@app.post("/telegram-webhook")
def telegram_webhook():
    if WEBHOOK_SECRET:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 403

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")
    if update_id is not None:
        with _seen_updates_lock:
            if update_id in _seen_updates:
                log.info("Ignoring duplicate Telegram update_id=%s", update_id)
                return jsonify({"ok": True, "duplicate": True}), 200
            _seen_updates.add(update_id)
            if len(_seen_updates) > _MAX_SEEN_UPDATES:
                _seen_updates.clear()
                _seen_updates.add(update_id)

    message = update.get("message") or {}
    document = message.get("document") or {}
    file_key = None
    if document:
        # file_unique_id remains stable when the same PDF is sent again.
        file_key = str(document.get("file_unique_id") or document.get("file_id") or "")
        if not file_key:
            return jsonify({"ok": True, "ignored": "missing file id"}), 200
        try:
            if not claim_document(file_key):
                log.info("Ignoring recently processed PDF file_key=%s", file_key)
                return jsonify({"ok": True, "duplicate_document": True}), 200
        except Exception:
            log.exception("Deduplication database error; refusing to process to avoid duplicate posts")
            return jsonify({"ok": False, "error": "deduplication database unavailable"}), 500

    # Acknowledge Telegram immediately, then process in the background.
    threading.Thread(target=handle_update, args=(update, file_key), daemon=True).start()
    return jsonify({"ok": True}), 200


@app.post("/configure-webhook")
def configure_webhook():
    if WEBHOOK_SECRET:
        provided = request.headers.get("X-Webhook-Secret", "")
        if provided != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 403
    try:
        set_webhook()
        return jsonify({"ok": True, "webhook_url": WEBHOOK_URL}), 200
    except Exception as exc:
        log.exception("Could not configure webhook")
        return jsonify({"ok": False, "error": str(exc)}), 500


# Initialize deduplication storage on app startup.
init_db()

# Gunicorn imports this module, so configure the Telegram menu and webhook on startup.
try:
    set_bot_commands()
    log.info("Telegram bot commands configured")
except Exception:
    log.exception("Could not configure Telegram bot commands at startup")

try:
    set_webhook()
except Exception:
    log.exception("Could not configure Telegram webhook at startup")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
