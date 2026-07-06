"""
Sticker/Emoji Downloader Bot — Webhook mode (Render)
------------------------------------------------------
Foydalanuvchi:
  - stiker/custom emoji forward qilsa, YOKI
  - /getpack <pack_name> yuborsa
bot shu pack'dagi barcha fayllarni (.tgs/.webp/.webm) topib, arxiv qilib yuboradi.

Har bir foydalanish haqida SUPERADMIN_ID ga darhol xabar boradi.

ENV o'zgaruvchilar (Render Environment tab):
  BOT_TOKEN       - bot tokeni
  SUPERADMIN_ID   - sizning Telegram user ID'ingiz (butun son)
  WEBHOOK_URL     - https://<render-app-nomi>.onrender.com
  PORT            - Render avtomatik beradi (default 10000)
"""

import os
import io
import zipfile
import logging

from flask import Flask, request
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sticker-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPERADMIN_ID = int(os.environ["SUPERADMIN_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

app = Flask(__name__)


# ---------- Telegram API helper funksiyalar ----------

def tg_call(method, **params):
    resp = requests.post(f"{API_BASE}/{method}", json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram xato (%s): %s", method, data)
    return data


def send_message(chat_id, text):
    tg_call("sendMessage", chat_id=chat_id, text=text)


def send_document_bytes(chat_id, filename, file_bytes, caption=None):
    files = {"document": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    requests.post(f"{API_BASE}/sendDocument", data=payload, files=files, timeout=60)


def notify_admin(text):
    if SUPERADMIN_ID:
        send_message(SUPERADMIN_ID, text)


def get_sticker_set(pack_name):
    data = tg_call("getStickerSet", name=pack_name)
    if data.get("ok"):
        return data["result"]
    return None


def get_file_path(file_id):
    data = tg_call("getFile", file_id=file_id)
    if data.get("ok"):
        return data["result"]["file_path"]
    return None


def download_file_bytes(file_path):
    resp = requests.get(f"{FILE_BASE}/{file_path}", timeout=60)
    resp.raise_for_status()
    return resp.content


def file_ext_for(sticker):
    if sticker.get("is_animated"):
        return ".tgs"
    if sticker.get("is_video"):
        return ".webm"
    return ".webp"


# ---------- Asosiy mantiq ----------

def process_pack(pack_name, requester):
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        return None, "Pack topilmadi. Nomini tekshiring."

    stickers = sticker_set["stickers"]
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, sticker in enumerate(stickers, start=1):
            file_path = get_file_path(sticker["file_id"])
            if not file_path:
                continue
            content = download_file_bytes(file_path)
            ext = file_ext_for(sticker)
            emoji_char = sticker.get("emoji", "")
            fname = f"{i:03d}_{emoji_char}{ext}".replace("/", "_")
            zf.writestr(fname, content)
            count += 1

    buf.seek(0)
    return buf, count


def handle_pack_request(chat_id, pack_name, requester_info):
    send_message(chat_id, f"'{pack_name}' qidirilmoqda, kuting...")

    buf, result = process_pack(pack_name, requester_info)
    if buf is None:
        send_message(chat_id, result)
        notify_admin(
            f"⚠️ Muvaffaqiyatsiz so'rov\n"
            f"Kimdan: {requester_info}\n"
            f"Pack: {pack_name}\n"
            f"Sabab: {result}"
        )
        return

    zip_bytes = buf.getvalue()
    send_document_bytes(
        chat_id,
        f"{pack_name}.zip",
        zip_bytes,
        caption=f"{result} ta fayl topildi.",
    )

    # Superadminga darhol xabar
    notify_admin(
        f"✅ Yangi so'rov bajarildi\n"
        f"Kimdan: {requester_info}\n"
        f"Pack: {pack_name}\n"
        f"Fayllar soni: {result}"
    )


def extract_pack_name_from_message(msg):
    """Forward qilingan sticker/custom emoji xabaridan pack nomini topadi."""
    sticker = msg.get("sticker")
    if sticker and sticker.get("set_name"):
        return sticker["set_name"]
    return None


# ---------- Webhook endpoint ----------

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    msg = update.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    from_user = msg.get("from", {})
    requester_info = (
        f"@{from_user.get('username')} (id:{from_user.get('id')})"
        if from_user.get("username")
        else f"id:{from_user.get('id')}"
    )

    text = msg.get("text", "")

    if text.startswith("/start"):
        send_message(
            chat_id,
            "Salom! Menga sticker/custom emoji forward qiling yoki "
            "/getpack <pack_nomi> deb yozing — men barcha fayllarni ZIP qilib beraman.",
        )
        return {"ok": True}

    if text.startswith("/getpack"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /getpack pack_nomi")
            return {"ok": True}
        pack_name = parts[1].strip()
        handle_pack_request(chat_id, pack_name, requester_info)
        return {"ok": True}

    pack_name = extract_pack_name_from_message(msg)
    if pack_name:
        handle_pack_request(chat_id, pack_name, requester_info)
        return {"ok": True}

    send_message(chat_id, "Sticker/emoji forward qiling yoki /getpack pack_nomi yuboring.")
    return {"ok": True}


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


def set_webhook():
    url = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
    result = tg_call("setWebhook", url=url)
    log.info("Webhook o'rnatildi: %s -> %s", url, result)


if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=PORT)
