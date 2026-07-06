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


def get_custom_emoji_set_name(custom_emoji_id):
    """Custom emoji ID orqali uning pack (set_name) nomini topadi."""
    data = tg_call("getCustomEmojiStickers", custom_emoji_ids=[custom_emoji_id])
    if data.get("ok") and data["result"]:
        return data["result"][0].get("set_name")
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

    # Superadminga darhol xabar + ZIP faylning nusxasi
    notify_admin(
        f"✅ Yangi so'rov bajarildi\n"
        f"Kimdan: {requester_info}\n"
        f"Pack: {pack_name}\n"
        f"Fayllar soni: {result}"
    )
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(
            SUPERADMIN_ID,
            f"{pack_name}.zip",
            zip_bytes,
            caption=f"{requester_info} so'ragan pack: {pack_name} ({result} ta fayl)",
        )


def extract_pack_name_from_link(text):
    """Matn ichidan t.me/addstickers/NAME yoki t.me/addemoji/NAME ko'rinishidagi
    havoladan pack nomini ajratib oladi."""
    if not text:
        return None
    for marker in ("addstickers/", "addemoji/"):
        if marker in text:
            after = text.split(marker, 1)[1]
            # so'zdan keyingi bo'sh joy/qo'shimcha belgilarni kesib tashlaymiz
            name = after.split()[0] if after.split() else after
            name = name.strip("/?").split("?")[0]
            if name:
                return name
    return None


def extract_pack_name_from_message(msg):
    """Forward qilingan sticker/custom emoji xabaridan pack nomini topadi."""
    # 1) Oddiy sticker (forward qilingan)
    sticker = msg.get("sticker")
    if sticker and sticker.get("set_name"):
        return sticker["set_name"]

    # 2) Custom emoji — matn/caption ichidagi entity sifatida keladi
    for field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        content = msg.get(field)
        entities = msg.get(entity_field) or []
        for ent in entities:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
                set_name = get_custom_emoji_set_name(ent["custom_emoji_id"])
                if set_name:
                    return set_name

    # 3) Matn ichida t.me/addstickers/... yoki t.me/addemoji/... havolasi bo'lsa
    link_name = extract_pack_name_from_link(msg.get("text"))
    if link_name:
        return link_name

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
        raw = parts[1].strip()
        pack_name = extract_pack_name_from_link(raw) or raw
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


# Gunicorn faylni import qilganda ham webhook o'rnatilishi uchun
# shu qatorni modul darajasida chaqiramiz (faqat __main__ ichida emas).
set_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
