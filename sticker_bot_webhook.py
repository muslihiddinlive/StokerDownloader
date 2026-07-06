"""
Sticker/Emoji Downloader Bot — Webhook mode (Render)
------------------------------------------------------
Xususiyatlar:
  - Sticker/custom emoji forward qilish yoki /getpack <nomi/link> orqali
    pack'ni ZIP qilib berish (faqat shaxsiy chatda avtomatik)
  - Kunlik limit: 1 foydalanuvchi uchun kuniga 1 marta (adminlar cheksiz)
  - Referal orqali limitni oshirish (/ref buyrug'i)
  - Superadmin: reklama tarqatish (/broadcast), admin tayinlash (.addadmin),
    xabar o'chirish (.del), guruhda ZIP so'rash (.zip, reply orqali)
  - Guruhda superadmin/admin xabarlariga avtomatik ⚡ reaksiya
  - Kanalga admin qilinsa, har bir postga avtomatik ⚡ reaksiya
  - Bot guruhda admin bo'lmasa — guruhda ishlamaydi
  - DB: Telegram guruhida pinned xabar orqali JSON saqlanadi (DB_GROUP_ID)

ENV o'zgaruvchilar (Render Environment tab):
  BOT_TOKEN       - bot tokeni
  SUPERADMIN_ID   - sizning Telegram user ID'ingiz (butun son)
  WEBHOOK_URL     - https://<render-app-nomi>.onrender.com
  DB_GROUP_ID     - DB sifatida ishlatiladigan Telegram guruh ID'si (bot shu
                    guruhda admin bo'lishi va xabar yuborish huquqiga ega
                    bo'lishi kerak)
  PORT            - Render avtomatik beradi (default 10000)

MUHIM: Render Start Command'da bitta worker ishlatilishi kerak (state
xotirada saqlanadi): 
  gunicorn sticker_bot_webhook:app --workers 1
"""

import os
import io
import json
import zipfile
import logging
from datetime import datetime, timezone

from flask import Flask, request
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sticker-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPERADMIN_ID = int(os.environ["SUPERADMIN_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
DB_GROUP_ID = int(os.environ["DB_GROUP_ID"])
PORT = int(os.environ.get("PORT", 10000))

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

BASE_DAILY_LIMIT = 1
REACTION_EMOJI = "⚡"

app = Flask(__name__)

BOT_ID = None          # getMe orqali to'ldiriladi
BOT_USERNAME = None
_pinned_message_id = None  # DB pinned xabar ID keshi


# ---------- Telegram API helper funksiyalar ----------

def tg_call(method, **params):
    resp = requests.post(f"{API_BASE}/{method}", json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram xato (%s): %s", method, data)
    return data


def send_message(chat_id, text, reply_to=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    return tg_call("sendMessage", **params)


def send_document_bytes(chat_id, filename, file_bytes, caption=None):
    files = {"document": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    requests.post(f"{API_BASE}/sendDocument", data=payload, files=files, timeout=60)


def notify_admin(text):
    if SUPERADMIN_ID:
        send_message(SUPERADMIN_ID, text)


def react(chat_id, message_id, emoji=REACTION_EMOJI):
    tg_call(
        "setMessageReaction",
        chat_id=chat_id,
        message_id=message_id,
        reaction=[{"type": "emoji", "emoji": emoji}],
    )


def delete_message(chat_id, message_id):
    tg_call("deleteMessage", chat_id=chat_id, message_id=message_id)


def get_chat_member_status(chat_id, user_id):
    data = tg_call("getChatMember", chat_id=chat_id, user_id=user_id)
    if data.get("ok"):
        return data["result"].get("status")
    return None


def bot_is_group_admin(chat_id):
    status = get_chat_member_status(chat_id, BOT_ID)
    return status in ("administrator", "creator")


# ---------- DB (Telegram guruh + pinned xabar orqali) ----------

def default_state():
    return {"admins": [], "users": {}, "known_users": []}


def load_state():
    global _pinned_message_id
    data = tg_call("getChat", chat_id=DB_GROUP_ID)
    if data.get("ok"):
        pinned = data["result"].get("pinned_message")
        if pinned and pinned.get("text"):
            _pinned_message_id = pinned["message_id"]
            try:
                return json.loads(pinned["text"])
            except (json.JSONDecodeError, TypeError):
                log.warning("DB xabari JSON emas, yangi state yaratiladi.")
    return default_state()


STATE = load_state()


def save_state():
    global _pinned_message_id
    text = json.dumps(STATE, ensure_ascii=False)
    if _pinned_message_id:
        result = tg_call(
            "editMessageText",
            chat_id=DB_GROUP_ID,
            message_id=_pinned_message_id,
            text=text,
        )
        if result.get("ok"):
            return
    # Pinned xabar yo'q yoki edit muvaffaqiyatsiz — yangisini yuboramiz
    result = tg_call("sendMessage", chat_id=DB_GROUP_ID, text=text)
    if result.get("ok"):
        _pinned_message_id = result["result"]["message_id"]
        tg_call(
            "pinChatMessage",
            chat_id=DB_GROUP_ID,
            message_id=_pinned_message_id,
            disable_notification=True,
        )


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_user_record(user_id):
    uid = str(user_id)
    if uid not in STATE["users"]:
        STATE["users"][uid] = {"date": today_str(), "count": 0, "referrals": 0, "referred_by": None, "bonus": 0}
    record = STATE["users"][uid]
    record.setdefault("bonus", 0)
    if record["date"] != today_str():
        record["date"] = today_str()
        record["count"] = 0
    return record


def is_admin(user_id):
    return user_id == SUPERADMIN_ID or user_id in STATE["admins"]


def user_daily_limit(user_id):
    record = get_user_record(user_id)
    return BASE_DAILY_LIMIT + record["referrals"] + record["bonus"]


def can_make_request(user_id):
    if is_admin(user_id):
        return True, None
    record = get_user_record(user_id)
    limit = user_daily_limit(user_id)
    if record["count"] >= limit:
        return False, (
            f"Kunlik limitingiz tugadi ({limit}/{limit}).\n"
            f"Limitni oshirish uchun /ref orqali do'stlaringizni taklif qiling — "
            f"har bir referal +1 kunlik limit beradi."
        )
    return True, None


def register_request(user_id):
    if is_admin(user_id):
        return
    record = get_user_record(user_id)
    record["count"] += 1
    save_state()


def register_known_user(user_id):
    if user_id not in STATE["known_users"]:
        STATE["known_users"].append(user_id)
        save_state()


def register_referral(new_user_id, referrer_id):
    new_record = get_user_record(new_user_id)
    if new_record["referred_by"] is not None:
        return  # avval ro'yxatdan o'tgan, qayta hisoblanmaydi
    if new_user_id == referrer_id:
        return
    new_record["referred_by"] = referrer_id
    referrer_record = get_user_record(referrer_id)
    referrer_record["referrals"] += 1
    save_state()
    send_message(
        referrer_id,
        f"🎉 Sizning referal havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
        f"Kunlik limitingiz endi {user_daily_limit(referrer_id)} taga oshdi.",
    )


# ---------- Sticker/emoji pack yuklash mantiqi ----------

def get_sticker_set(pack_name):
    data = tg_call("getStickerSet", name=pack_name)
    if data.get("ok"):
        return data["result"]
    return None


def get_custom_emoji_set_name(custom_emoji_id):
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


def process_pack(pack_name):
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


def handle_pack_request(chat_id, pack_name, requester_info, requester_id, reply_to=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to)
        return

    send_message(chat_id, f"'{pack_name}' qidirilmoqda, kuting...", reply_to=reply_to)

    buf, result = process_pack(pack_name)
    if buf is None:
        send_message(chat_id, result, reply_to=reply_to)
        notify_admin(
            f"⚠️ Muvaffaqiyatsiz so'rov\n"
            f"Kimdan: {requester_info}\n"
            f"Pack: {pack_name}\n"
            f"Sabab: {result}"
        )
        return

    register_request(requester_id)

    zip_bytes = buf.getvalue()
    send_document_bytes(
        chat_id,
        f"{pack_name}.zip",
        zip_bytes,
        caption=f"{result} ta fayl topildi.",
    )

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
    if not text:
        return None
    for marker in ("addstickers/", "addemoji/"):
        if marker in text:
            after = text.split(marker, 1)[1]
            name = after.split()[0] if after.split() else after
            name = name.strip("/?").split("?")[0]
            if name:
                return name
    return None


def extract_pack_name_from_message(msg):
    sticker = msg.get("sticker")
    if sticker and sticker.get("set_name"):
        return sticker["set_name"]

    for field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        entities = msg.get(entity_field) or []
        for ent in entities:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
                set_name = get_custom_emoji_set_name(ent["custom_emoji_id"])
                if set_name:
                    return set_name

    link_name = extract_pack_name_from_link(msg.get("text"))
    if link_name:
        return link_name

    return None


def extract_single_sticker_file(msg):
    """Xabardagi bitta sticker/custom emoji faylini (file_id, ext, emoji) qaytaradi."""
    sticker = msg.get("sticker")
    if sticker:
        return sticker["file_id"], file_ext_for(sticker), sticker.get("emoji", "")

    for field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        entities = msg.get(entity_field) or []
        for ent in entities:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
                data = tg_call("getCustomEmojiStickers", custom_emoji_ids=[ent["custom_emoji_id"]])
                if data.get("ok") and data["result"]:
                    em = data["result"][0]
                    return em["file_id"], file_ext_for(em), em.get("emoji", "")

    return None, None, None


def handle_single_sticker_request(chat_id, reply, requester_info, requester_id, reply_to=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to)
        return

    file_id, ext, emoji_char = extract_single_sticker_file(reply)
    if not file_id:
        send_message(chat_id, "Bu xabarda sticker/custom emoji topilmadi.", reply_to=reply_to)
        return

    file_path = get_file_path(file_id)
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.", reply_to=reply_to)
        return

    content = download_file_bytes(file_path)
    register_request(requester_id)

    filename = f"sticker_{emoji_char}{ext}".replace("/", "_")
    send_document_bytes(chat_id, filename, content)

    notify_admin(
        f"✅ Bitta sticker yuklandi\n"
        f"Kimdan: {requester_info}\n"
        f"Fayl: {filename}"
    )
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, filename, content, caption=f"{requester_info} yuklagan sticker")


def resolve_user_id(token):
    """'@username' yoki raqamli ID'ni user_id'ga aylantiradi."""
    token = token.strip()
    if token.startswith("@"):
        data = tg_call("getChat", chat_id=token)
        if data.get("ok"):
            return data["result"]["id"]
        return None
    try:
        return int(token)
    except ValueError:
        return None


def requester_label(from_user):
    return (
        f"@{from_user.get('username')} (id:{from_user.get('id')})"
        if from_user.get("username")
        else f"id:{from_user.get('id')}"
    )


# ---------- Guruh buyruqlari (.zip, .addadmin, .deladmin, .del) ----------

def handle_group_dot_commands(msg, chat_id, user_id, text):
    reply = msg.get("reply_to_message")

    if text.strip() == ".zipstiker":
        if not is_admin(user_id):
            return True
        if not reply:
            send_message(chat_id, "Sticker/custom emoji xabariga reply qilib .zipstiker yozing.")
            return True
        handle_single_sticker_request(chat_id, reply, requester_label(msg.get("from", {})), user_id, reply_to=msg["message_id"])
        return True

    if text.strip() == ".zip":
        if not is_admin(user_id):
            return True
        if not reply:
            send_message(chat_id, "Stiker/custom emoji xabariga reply qilib .zip yozing.")
            return True
        pack_name = extract_pack_name_from_message(reply)
        if not pack_name:
            send_message(chat_id, "Bu xabardan pack nomini topa olmadim.")
            return True
        handle_pack_request(chat_id, pack_name, requester_label(msg.get("from", {})), user_id, reply_to=msg["message_id"])
        return True

    if text.strip() == ".addadmin":
        if user_id != SUPERADMIN_ID:
            return True
        if not reply:
            send_message(chat_id, "Admin qilmoqchi bo'lgan odamning xabariga reply qiling.")
            return True
        target_id = reply["from"]["id"]
        if target_id not in STATE["admins"]:
            STATE["admins"].append(target_id)
            save_state()
        send_message(chat_id, f"✅ {requester_label(reply['from'])} endi bot admini.")
        return True

    if text.strip() == ".deladmin":
        if user_id != SUPERADMIN_ID:
            return True
        if not reply:
            send_message(chat_id, "Admindan olib tashlamoqchi bo'lgan odamning xabariga reply qiling.")
            return True
        target_id = reply["from"]["id"]
        if target_id in STATE["admins"]:
            STATE["admins"].remove(target_id)
            save_state()
        send_message(chat_id, f"❌ {requester_label(reply['from'])} bot adminligidan olindi.")
        return True

    if text.strip() == ".del":
        if user_id != SUPERADMIN_ID:
            return True
        if not reply:
            send_message(chat_id, "O'chirmoqchi bo'lgan xabarga reply qilib .del yozing.")
            return True
        delete_message(chat_id, reply["message_id"])
        return True

    return False


# ---------- Webhook endpoint ----------

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    # ---- Kanal postlari: bot admin bo'lsa avtomatik reaksiya ----
    channel_post = update.get("channel_post")
    if channel_post:
        chat_id = channel_post["chat"]["id"]
        if bot_is_group_admin(chat_id):
            react(chat_id, channel_post["message_id"])
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"]["type"]
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    requester_info = requester_label(from_user)
    text = msg.get("text", "") or ""

    is_group = chat_type in ("group", "supergroup")

    # ---- Guruhda bot admin emasligini tekshirish ----
    if is_group and not bot_is_group_admin(chat_id):
        return {"ok": True}

    # ---- Guruhda superadmin/admin xabariga avtomatik reaksiya ----
    if is_group and is_admin(user_id):
        react(chat_id, msg["message_id"])

    # ---- Guruh nuqtali buyruqlari (.zip, .addadmin, .deladmin, .del) ----
    if is_group and text.strip().startswith("."):
        if handle_group_dot_commands(msg, chat_id, user_id, text):
            return {"ok": True}

    # ---- Guruhda stiker/emoji tushsa — avtomatik zip QILINMAYDI ----
    if is_group:
        return {"ok": True}

    # ================= Shaxsiy chat (private) mantiqi =================

    register_known_user(user_id)

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1][4:])
                register_referral(user_id, referrer_id)
            except ValueError:
                pass
        send_message(
            chat_id,
            "Salom! Menga sticker/custom emoji forward qiling yoki "
            "/getpack <pack_nomi> deb yozing — men barcha fayllarni ZIP qilib beraman.\n\n"
            "Kuniga 1 marta bepul. Limitni oshirish uchun /ref buyrug'idan foydalaning.",
        )
        return {"ok": True}

    if text.startswith("/ref"):
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        record = get_user_record(user_id)
        send_message(
            chat_id,
            f"Referal havolangiz:\n{link}\n\n"
            f"Hozirgi referallar: {record['referrals']}\n"
            f"Kunlik limitingiz: {user_daily_limit(user_id)}",
        )
        return {"ok": True}

    if text.startswith("/limit"):
        record = get_user_record(user_id)
        limit = user_daily_limit(user_id)
        used = record["count"] if not is_admin(user_id) else 0
        status = "cheksiz (admin)" if is_admin(user_id) else f"{used}/{limit}"
        send_message(chat_id, f"Bugungi foydalanish: {status}")
        return {"ok": True}

    if text.lower().startswith("/reload"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        global STATE
        STATE = load_state()
        send_message(
            chat_id,
            f"🔄 Ma'lumotlar DB guruhidan qayta yuklandi.\n"
            f"Adminlar: {len(STATE['admins'])}\n"
            f"Foydalanuvchilar: {len(STATE['users'])}",
        )
        return {"ok": True}

    if text.startswith("/addadmin"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /addadmin @username yoki /addadmin user_id")
            return {"ok": True}
        target_id = resolve_user_id(parts[1])
        if not target_id:
            send_message(chat_id, "Foydalanuvchi topilmadi. ID yoki @username to'g'ri ekanini tekshiring.")
            return {"ok": True}
        if target_id not in STATE["admins"]:
            STATE["admins"].append(target_id)
            save_state()
        send_message(chat_id, f"✅ id:{target_id} endi bot admini.")
        return {"ok": True}

    if text.startswith("/deladmin"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /deladmin @username yoki /deladmin user_id")
            return {"ok": True}
        target_id = resolve_user_id(parts[1])
        if not target_id:
            send_message(chat_id, "Foydalanuvchi topilmadi.")
            return {"ok": True}
        if target_id in STATE["admins"]:
            STATE["admins"].remove(target_id)
            save_state()
        send_message(chat_id, f"❌ id:{target_id} bot adminligidan olindi.")
        return {"ok": True}

    if text.startswith("/addlimit"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split()
        if len(parts) < 3:
            send_message(chat_id, "Foydalanish: /addlimit @username_yoki_id miqdor\nMasalan: /addlimit 123456789 5")
            return {"ok": True}
        target_id = resolve_user_id(parts[1])
        if not target_id:
            send_message(chat_id, "Foydalanuvchi topilmadi.")
            return {"ok": True}
        try:
            amount = int(parts[2])
        except ValueError:
            send_message(chat_id, "Miqdor butun son bo'lishi kerak.")
            return {"ok": True}
        record = get_user_record(target_id)
        record["bonus"] += amount
        save_state()
        send_message(chat_id, f"✅ id:{target_id} uchun kunlik limit +{amount} qo'shildi. Yangi limit: {user_daily_limit(target_id)}")
        return {"ok": True}

    if text.startswith("/broadcast"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /broadcast xabar matni")
            return {"ok": True}
        broadcast_text = parts[1]
        sent = 0
        for uid in STATE["known_users"]:
            r = send_message(uid, broadcast_text)
            if r.get("ok"):
                sent += 1
        send_message(chat_id, f"Reklama {sent} ta foydalanuvchiga yuborildi.")
        return {"ok": True}

    if text.strip() == ".zipstiker":
        if not is_admin(user_id):
            return {"ok": True}
        reply = msg.get("reply_to_message")
        if not reply:
            send_message(chat_id, "Sticker/custom emoji xabariga reply qilib .zipstiker yozing.")
            return {"ok": True}
        handle_single_sticker_request(chat_id, reply, requester_info, user_id)
        return {"ok": True}

    if text.startswith("/getpack"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /getpack pack_nomi")
            return {"ok": True}
        raw = parts[1].strip()
        pack_name = extract_pack_name_from_link(raw) or raw
        handle_pack_request(chat_id, pack_name, requester_info, user_id)
        return {"ok": True}

    pack_name = extract_pack_name_from_message(msg)
    if pack_name:
        handle_pack_request(chat_id, pack_name, requester_info, user_id)
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


def init_bot_identity():
    global BOT_ID, BOT_USERNAME
    data = tg_call("getMe")
    if data.get("ok"):
        BOT_ID = data["result"]["id"]
        BOT_USERNAME = data["result"]["username"]
        log.info("Bot identifikatsiyasi: id=%s username=%s", BOT_ID, BOT_USERNAME)


# Gunicorn faylni import qilganda ham ishga tushishi uchun modul darajasida:
init_bot_identity()
set_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
