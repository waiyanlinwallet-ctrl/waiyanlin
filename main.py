import os
import logging
import tempfile
import threading
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

# Telegram may retry webhook updates if processing a large PDF takes too long.
# Acknowledge each update immediately and ignore duplicate update_id values.
_seen_updates = set()
_seen_updates_lock = threading.Lock()
_MAX_SEEN_UPDATES = 5000
session.headers.update({"User-Agent": "YadanabonPDFtoJPGBot/1.0"})


def telegram(method, data=None, files=None, timeout=60):
    response = session.post(
        f"{API}/{method}", data=data, files=files, timeout=timeout
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {result}")
    return result["result"]


def send_message(chat_id, text):
    telegram("sendMessage", {"chat_id": chat_id, "text": text})


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
                {"chat_id": CHANNEL_ID, "media": __import__("json").dumps(media)},
                files=multipart,
                timeout=180,
            )
        finally:
            for handle in opened:
                handle.close()


def process_document(message):
    chat_id = message["chat"]["id"]
    document = message.get("document") or {}
    filename = document.get("file_name", "document.pdf")
    mime_type = document.get("mime_type", "")
    file_id = document.get("file_id")
    if not file_id:
        send_message(chat_id, "ဖိုင်ကို မဖတ်နိုင်ပါ။ PDF ဖိုင်ကို ပြန်ပို့ပေးပါ။")
        return
    if not filename.lower().endswith(".pdf") and mime_type != "application/pdf":
        send_message(chat_id, "PDF ဖိုင်ကိုပဲ ပို့ပေးပါ။")
        return
    size = int(document.get("file_size") or 0)
    if size > MAX_PDF_MB * 1024 * 1024:
        send_message(chat_id, f"PDF ဖိုင်က {MAX_PDF_MB} MB ထက်ကြီးနေပါတယ်။")
        return

    send_message(chat_id, "PDF လက်ခံရရှိပါပြီ။ JPG ပုံတွေပြောင်းပြီး Channel မှာ တင်နေပါတယ်။")
    try:
        pdf_bytes = get_pdf_file(file_id)
        with tempfile.TemporaryDirectory(prefix="pdf-jpg-") as tmp:
            paths = pdf_to_jpegs(pdf_bytes, tmp)
            caption = f"{filename} — စာမျက်နှာ {len(paths)} မျက်နှာ"
            post_jpegs(paths, caption)
        send_message(chat_id, f"ပြီးပါပြီ။ JPG ပုံ {len(paths)} ပုံကို Channel မှာ တင်ပြီးပါပြီ။")
    except Exception:
        log.exception("Failed to process PDF")
        send_message(
            chat_id,
            "PDF ပြောင်းပြီး Channel တင်ရာမှာ အမှားဖြစ်သွားပါတယ်။ "
            "Render Logs ကို စစ်ဆေးပေးပါ။",
        )


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "pdf-to-jpg-telegram-bot"}), 200


def handle_update(update):
    """Process an update outside the webhook request, so Telegram gets a fast 200."""
    try:
        message = update.get("message")
        if message and message.get("document"):
            process_document(message)
        elif message and message.get("text") == "/start":
            send_message(
                message["chat"]["id"],
                "မင်္ဂလာပါ။ သတင်းစာ PDF ဖိုင်ကို ဒီ Bot ဆီ ပို့ပါ။ "
                "JPG ပုံပြောင်းပြီး သတ်မှတ်ထားတဲ့ Channel မှာ တင်ပေးပါမယ်။",
            )
    except Exception:
        log.exception("Unhandled error while processing Telegram update")


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

    # Start processing in the background and acknowledge Telegram immediately.
    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return jsonify({"ok": True}), 200


@app.post("/configure-webhook")
def configure_webhook():
    # Optional manual endpoint for troubleshooting; protect with WEBHOOK_SECRET if configured.
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


if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=PORT)
