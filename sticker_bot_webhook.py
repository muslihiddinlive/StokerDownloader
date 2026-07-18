"""
Sticker/Emoji Downloader Bot — Webhook mode (Render) — v2
-----------------------------------------------------------
v2 o'zgarishlari (Akajon so'roviga ko'ra):

- BARCHA "/" komandalar olib tashlandi (faqat /start qoladi — Telegram
  botlari uchun standart kirish nuqtasi). Hamma narsa inline tugmalar orqali.
- Superadmin /start bosganda oddiy menyu + "Superadmin panel" tugmasi bilan
  ochiladi.
- Superadmin panelda: Foydalanuvchilar / Guruhlar / Kanallar ro'yxati —
  har birining ustiga bosilsa botning shu obyekt haqida to'plagan BARCHA
  ma'lumoti ko'rsatiladi.
- Superadmin istalgan foydalanuvchiga istalgan miqdorda limit bera oladi
  (panel ichidan, matn kiritish orqali — bosqichma-bosqich).
- Superadmin bazaviy/haftalik limit sozlamalarini panel orqali boshqaradi.
- Bot qo'shilgan HAR BIR guruh/kanal reaksiya bosish uchun ishlatadigan
  emoji superadmin tomonidan panel orqali tanlanadi (config["reaction_emoji"]).
- Guruhda superadminning xabariga avtomatik shu reaksiya bosiladi (bot
  o'sha guruhda admin bo'lishi sharti bilan — Telegram reaksiya API talabi).
- Bot superadminni biror guruh/kanalga "majburan qo'sha olmaydi" — bu
  Telegram Bot API'da mavjud emas (faqat MTProto user-klient orqali mumkin).
  Buning o'rniga: superadmin panelidan har bir guruh/kanal uchun bitta
  tugma bosilsa, bot taklif havolasi (invite link) yaratib, uni
  superadminning shaxsiy chatiga yuboradi.
- Race-condition tuzatildi: STATE'ga har qanday o'qish+yozish (read-modify-
  write) endi global _state_lock (RLock) ostida bajariladi — bir nechta
  background thread bir vaqtda yozganda ma'lumot yo'qolmasligi uchun.
- Eski, ikki marta yozilgan (va shundan biri hech qachon ishlamaydigan)
  ".zipstiker" bloki olib tashlandi — endi bitta joyda, to'g'ri ishlaydi.
- Pack keshi endi faqat sticker SONI emas, balki barcha file_id'larning
  hashi orqali tekshiriladi — pack ichidagi bitta fayl almashtirilsa ham
  kesh avtomatik bekor qilinadi.
- Bot qaysi guruh/kanallarga qo'shilganini (my_chat_member orqali) va undan
  chiqarilganini kuzatib boradi — superadmin panelidagi ro'yxatlar shundan
  to'ldiriladi.

ENV o'zgaruvchilar (Render Environment tab):
  BOT_TOKEN        - bot tokeni
  SUPERADMIN_ID    - sizning Telegram user ID'ingiz (butun son)
  WEBHOOK_URL      - https://<render-app-nomi>.onrender.com
  DB_GROUP_ID      - DB sifatida ishlatiladigan Telegram guruh ID'si
  CACHE_GROUP_ID   - (ixtiyoriy) Pack ZIP fayllarini keshlash uchun guruh
  PORT             - Render avtomatik beradi (default 10000)

MUHIM: Render Start Command'da bitta worker ishlatilishi kerak:
  gunicorn sticker_bot_webhook:app --workers 1
"""

import os
import io
import json
import html
import time
import uuid
import zipfile
import hashlib
import logging
import tempfile
import subprocess
import threading
from datetime import datetime, timezone

from flask import Flask, request
import requests
import imageio_ffmpeg

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sticker-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPERADMIN_ID = int(os.environ["SUPERADMIN_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
DB_GROUP_ID = int(os.environ["DB_GROUP_ID"])
CACHE_GROUP_ID = os.environ.get("CACHE_GROUP_ID")
CACHE_GROUP_ID = int(CACHE_GROUP_ID) if CACHE_GROUP_ID else None
PORT = int(os.environ.get("PORT", 10000))

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

DEFAULT_REACTION_EMOJI = "⚡"
REACTION_EMOJI_CHOICES = ["⚡", "🔥", "❤️", "👍", "🎉", "😎", "🙏", "💯"]

app = Flask(__name__)

BOT_ID = None
BOT_USERNAME = None
_pinned_message_id = None

# Barcha STATE (persistent, guruhga pinned JSON orqali saqlanadi) ni
# o'qish/yozish shu RLock ostida bajariladi. RLock chunki ba'zi funksiyalar
# bir-birini chaqiradi (masalan ensure_period_reset ichida save_state()).
_state_lock = threading.RLock()

# Vaqtinchalik (persistent bo'lmagan) holatlar — process qayta ishga
# tushganda yo'qolishi mumkin, bu qabul qilinadi:
_pending_choices = {}      # forward qilingan stiker uchun "pack/single" tanlovi
_pending_lock = threading.Lock()

_pending_input = {}        # superadmin panelidagi ko'p bosqichli matn kiritish
_pending_input_lock = threading.Lock()


def store_pending_choice(payload):
    import uuid
    token = uuid.uuid4().hex[:10]
    with _pending_lock:
        _pending_choices[token] = payload
    return token


def pop_pending_choice(token):
    with _pending_lock:
        return _pending_choices.pop(token, None)


def set_pending_input(user_id, action, data=None):
    with _pending_input_lock:
        _pending_input[user_id] = {"action": action, "data": data or {}}


def get_pending_input(user_id):
    with _pending_input_lock:
        return _pending_input.get(user_id)


def clear_pending_input(user_id):
    with _pending_input_lock:
        _pending_input.pop(user_id, None)


# ---------- Telegram API helper funksiyalar ----------

def tg_call(method, **params):
    resp = requests.post(f"{API_BASE}/{method}", json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram xato (%s): %s", method, data)
    return data


def send_message(chat_id, text, reply_to=None, parse_mode_html=False, reply_markup=None,
                  business_connection_id=None, entities=None, add_signature=True):
    send_text, send_entities = text, entities
    sig_id = get_signature_emoji() if (add_signature and not entities) else None
    if sig_id:
        placeholder = get_signature_placeholder()
        if parse_mode_html:
            send_text = f'{text} <tg-emoji emoji-id="{html.escape(sig_id, quote=True)}">{html.escape(placeholder, quote=False)}</tg-emoji>'
        else:
            base_len = utf16_len(text)
            send_text = f"{text} {placeholder}"
            send_entities = [{"type": "custom_emoji", "offset": base_len + 1,
                               "length": utf16_len(placeholder), "custom_emoji_id": sig_id}]

    params = {"chat_id": chat_id, "text": send_text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if send_entities:
        params["entities"] = send_entities  # entities va parse_mode birga bo'lmaydi
    elif parse_mode_html:
        params["parse_mode"] = "HTML"
    if reply_markup:
        params["reply_markup"] = reply_markup
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    result = tg_call("sendMessage", **params)

    if sig_id and not (result and result.get("ok")):
        # Imzo (premium emoji) rad etildi — original xabarni imzosiz, o'zgarishsiz qayta yuboramiz
        return send_message(chat_id, text, reply_to=reply_to, parse_mode_html=parse_mode_html,
                             reply_markup=reply_markup, business_connection_id=business_connection_id,
                             entities=entities, add_signature=False)
    return result


def edit_message_text(chat_id, message_id, text, parse_mode_html=False, reply_markup=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode_html:
        params["parse_mode"] = "HTML"
    if reply_markup:
        params["reply_markup"] = reply_markup
    return tg_call("editMessageText", **params)


def safe_edit_or_send(chat_id, message_id, text, parse_mode_html=False, reply_markup=None):
    result = edit_message_text(chat_id, message_id, text, parse_mode_html=parse_mode_html, reply_markup=reply_markup)
    if not result.get("ok"):
        send_message(chat_id, text, parse_mode_html=parse_mode_html, reply_markup=reply_markup)


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    params["show_alert"] = show_alert
    return tg_call("answerCallbackQuery", **params)


def send_document_bytes(chat_id, filename, file_bytes, caption=None, business_connection_id=None):
    files = {"document": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = requests.post(f"{API_BASE}/sendDocument", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendDocument javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendDocument xato: %s", data)
    return data


def send_video_bytes(chat_id, filename, file_bytes, caption=None, business_connection_id=None):
    """.webm formatdagi custom emoji/stikerlarni video sifatida (fayl emas) yuboradi,
    shunda Telegram uni ichkarida ijro etadi."""
    files = {"video": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = requests.post(f"{API_BASE}/sendVideo", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendVideo javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendVideo xato: %s", data)
    return data


def send_animation_bytes(chat_id, filename, file_bytes, caption=None, business_connection_id=None):
    """.webm formatdagi custom emoji/stikerlarni animatsiya sifatida (GIF kabi) yuboradi —
    bu qisqa, tovushsiz, halqali cliplar uchun sendVideo'dan ko'ra to'g'ri usul,
    Telegram uni ichkarida halqali ijro etadi, fayl sifatida ko'rsatmaydi."""
    files = {"animation": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = requests.post(f"{API_BASE}/sendAnimation", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendAnimation javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendAnimation xato: %s", data)
    return data


def send_sticker_by_file_id(chat_id, file_id, business_connection_id=None):
    params = {"chat_id": chat_id, "sticker": file_id}
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return tg_call("sendSticker", **params)


def send_document_by_file_id(chat_id, file_id, caption=None, business_connection_id=None):
    params = {"chat_id": chat_id, "document": file_id}
    if caption:
        params["caption"] = caption
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return tg_call("sendDocument", **params)


def notify_admin(text):
    if SUPERADMIN_ID:
        send_message(SUPERADMIN_ID, text)


def react(chat_id, message_id, emoji=None, custom_emoji_id=None):
    if custom_emoji_id:
        reaction = [{"type": "custom_emoji", "custom_emoji_id": custom_emoji_id}]
    else:
        reaction = [{"type": "emoji", "emoji": emoji or get_reaction_emoji()}]
    return tg_call("setMessageReaction", chat_id=chat_id, message_id=message_id, reaction=reaction)


def react_with_kind(chat_id, message_id, kind):
    """REACTION_KIND_CONFIG'dagi kindga (admin/superadmin/channel) mos sozlangan
    reaksiyani qo'yadi — oddiy emoji yoki premium (custom_emoji_id) bo'lishi mumkin."""
    rtype, value = get_reaction_config_for(kind)
    if rtype == "custom_emoji":
        result = react(chat_id, message_id, custom_emoji_id=value)
        if not (result and result.get("ok")):
            # Guruh custom emoji reaksiyaga ruxsat bermagan bo'lishi mumkin — oddiyga tushamiz
            react(chat_id, message_id, emoji=DEFAULT_REACTION_EMOJI)
        return result
    return react(chat_id, message_id, emoji=value)


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
    return {
        "admins": [],
        "users": {},
        "known_users": [],
        # Bot qo'shilgan guruh/kanallar kuzatuvi:
        "groups": {},    # {chat_id_str: {"title","type","added_at"}}
        "channels": {},  # {chat_id_str: {"title","username","added_at"}}
        "config": {
            "base_weekly": 7,
            "weekly_cap": 7,
            "reaction_emoji": DEFAULT_REACTION_EMOJI,
            "superadmin_reaction_emoji": DEFAULT_REACTION_EMOJI,
            "channel_reaction_emoji": DEFAULT_REACTION_EMOJI,
            "keyword_free_limit": 2,
        },
        "force_channels": [],
        "bonus_channels": [],
        "pack_cache": {},  # {pack_name_lower: {"file_id","sticker_count","content_hash","cached_at"}}
        "stats": {"total_requests": 0},
        "business_connections": {},  # {connection_id: {"owner_id": int, "enabled": bool}}
        "keywords": {},  # {str(owner_id): [{"id","trigger","type"("exact"/"any"),"response"}]}
    }


def default_user_record():
    return {
        "period_key": None,
        "mode": None,
        "count": 0,
        "referrals": 0,
        "referred_by": None,
        "bonus": 0,
        "premium_until": None,
        "claimed_bonus_channels": [],
        "lifetime_requests": 0,
        "username": None,
        "first_name": None,
        "type_counts": {"sticker": 0, "emoji": 0, "gif": 0, "pack": 0},
        "history": [],
        "first_seen": None,
    }


def get_file_path(file_id):
    data = tg_call("getFile", file_id=file_id)
    if data.get("ok"):
        return data["result"]["file_path"]
    return None


def download_file_bytes(file_path):
    resp = requests.get(f"{FILE_BASE}/{file_path}", timeout=60)
    resp.raise_for_status()
    return resp.content


def _merge_with_defaults(loaded):
    merged = default_state()
    merged.update(loaded)
    # ichki dictlarni ham to'ldirish (yangi kalitlar bo'lsa)
    for key in ("config",):
        d = default_state()[key]
        d.update(merged.get(key, {}))
        merged[key] = d
    for key in ("groups", "channels"):
        merged.setdefault(key, {})
    return merged


def load_state():
    """DB guruhga pinlangan JSON FAYL (document) orqali o'qiydi. Matn xabar
    formatidagi eski holatni ham (migratsiya uchun) o'qiy oladi — birinchi
    save_state chaqirilganda avtomatik fayl formatiga o'tkaziladi."""
    global _pinned_message_id
    data = tg_call("getChat", chat_id=DB_GROUP_ID)
    if not data.get("ok"):
        return default_state()
    pinned = data["result"].get("pinned_message")
    if not pinned:
        return default_state()
    _pinned_message_id = pinned["message_id"]

    document = pinned.get("document")
    if document:
        file_path = get_file_path(document["file_id"])
        if file_path:
            try:
                raw = download_file_bytes(file_path)
                loaded = json.loads(raw.decode("utf-8"))
                return _merge_with_defaults(loaded)
            except (json.JSONDecodeError, UnicodeDecodeError, requests.RequestException) as exc:
                log.warning("DB faylini o'qib bo'lmadi (%s), yangi state yaratiladi.", exc)
                return default_state()

    if pinned.get("text"):
        try:
            loaded = json.loads(pinned["text"])
            log.info("Eski matnli DB formati topildi — birinchi saqlashda faylga migratsiya qilinadi.")
            return _merge_with_defaults(loaded)
        except (json.JSONDecodeError, TypeError):
            log.warning("DB xabari JSON emas, yangi state yaratiladi.")

    return default_state()


STATE = load_state()


def _upload_state_document(method_extra_fields=None, message_id=None):
    """STATE'ni JSON fayl sifatida DB guruhga yuboradi/yangilaydi.
    Qaytaradi: (ok, response_dict)"""
    payload = json.dumps(STATE, ensure_ascii=False).encode("utf-8")
    files = {"document": ("state.json", payload, "application/json")}
    if message_id:
        media = json.dumps({"type": "document", "media": "attach://document"})
        form = {"chat_id": DB_GROUP_ID, "message_id": message_id, "media": media}
        resp = requests.post(f"{API_BASE}/editMessageMedia", data=form, files=files, timeout=30)
    else:
        form = {"chat_id": DB_GROUP_ID}
        resp = requests.post(f"{API_BASE}/sendDocument", data=form, files=files, timeout=30)
    try:
        result = resp.json()
    except ValueError:
        log.error("STATE yuklashda JSON bo'lmagan javob: %s", resp.text[:300])
        return False, {}
    return result.get("ok", False), result


def save_state_locked():
    """_state_lock ALLAQACHON ushlanган holatda chaqirilishi kerak.
    STATE'ni pinlangan JSON FAYL (document) sifatida DB guruhga saqlaydi —
    matnli xabardagi 4096 belgi chegarasidan xoli, ma'lumot qancha katta
    bo'lmasin ishlayveradi."""
    global _pinned_message_id
    if _pinned_message_id:
        ok, result = _upload_state_document(message_id=_pinned_message_id)
        if ok:
            return
        log.warning("editMessageMedia muvaffaqiyatsiz (%s), yangi fayl yuboriladi.", result)

    ok, result = _upload_state_document()
    if ok:
        _pinned_message_id = result["result"]["message_id"]
        tg_call("pinChatMessage", chat_id=DB_GROUP_ID, message_id=_pinned_message_id, disable_notification=True)
    else:
        log.error("STATE saqlashda xato: %s", result)


def save_state():
    with _state_lock:
        save_state_locked()


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def iso_week_str():
    return datetime.now(timezone.utc).strftime("%G-W%V")


def get_limit_config():
    with _state_lock:
        STATE.setdefault("config", {})
        cfg = STATE["config"]
        cfg.setdefault("base_weekly", 7)
        cfg.setdefault("weekly_cap", 7)
        cfg.setdefault("reaction_emoji", DEFAULT_REACTION_EMOJI)
        cfg.setdefault("superadmin_reaction_emoji", DEFAULT_REACTION_EMOJI)
        cfg.setdefault("channel_reaction_emoji", DEFAULT_REACTION_EMOJI)
        return dict(cfg)


def get_referral_leaderboard(top_n=10):
    with _state_lock:
        users = dict(STATE["users"])
    rows = []
    for uid, rec in users.items():
        refs = rec.get("referrals", 0)
        if refs > 0:
            rows.append((int(uid), refs, rec.get("lifetime_requests", 0)))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top_n]


def build_users_csv():
    import csv
    with _state_lock:
        users = dict(STATE["users"])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "user_id", "username", "first_name", "referrals", "referred_by",
        "count", "mode", "bonus", "lifetime_requests", "premium_until", "first_seen",
    ])
    for uid, rec in users.items():
        writer.writerow([
            uid, rec.get("username") or "", rec.get("first_name") or "",
            rec.get("referrals", 0), rec.get("referred_by") or "",
            rec.get("count", 0), rec.get("mode") or "", rec.get("bonus", 0),
            rec.get("lifetime_requests", 0), rec.get("premium_until") or "",
            rec.get("first_seen") or "",
        ])
    return buf.getvalue().encode("utf-8")


REACTION_KIND_CONFIG = {
    "admin": ("reaction_emoji", "👤 Admin (guruh) reaksiyasi"),
    "superadmin": ("superadmin_reaction_emoji", "👑 Superadmin (guruh) reaksiyasi"),
    "channel": ("channel_reaction_emoji", "📢 Kanal posti reaksiyasi"),
}


def utf16_len(s):
    """Telegram entity offset/length UTF-16 kod birliklarida hisoblanadi, Python belgi
    soni bilan emas — ba'zi emoji surrogate juftlik sifatida 2 birlik egallaydi."""
    return len(s.encode("utf-16-le")) // 2


def get_signature_emoji():
    cfg = get_limit_config().get("signature_emoji")
    return cfg.get("custom_emoji_id") if isinstance(cfg, dict) else None


def get_signature_placeholder():
    cfg = get_limit_config().get("signature_emoji")
    return ((cfg or {}).get("placeholder") if isinstance(cfg, dict) else None) or "✨"


def set_signature_emoji(custom_emoji_id, placeholder="✨"):
    with _state_lock:
        STATE.setdefault("config", {})["signature_emoji"] = {
            "custom_emoji_id": custom_emoji_id, "placeholder": placeholder,
        }
        save_state_locked()


def clear_signature_emoji():
    with _state_lock:
        STATE.setdefault("config", {}).pop("signature_emoji", None)
        save_state_locked()


def get_reaction_config_for(kind):
    """(reaction_type, value) qaytaradi: ('emoji', '👍') yoki ('custom_emoji', '123...')."""
    key, _ = REACTION_KIND_CONFIG[kind]
    cfg = get_limit_config().get(key)
    if isinstance(cfg, dict) and cfg.get("value"):
        return cfg.get("type", "emoji"), cfg["value"]
    if isinstance(cfg, str) and cfg:
        return "emoji", cfg  # eski (faqat oddiy emoji) format bilan moslik
    return "emoji", DEFAULT_REACTION_EMOJI


def get_reaction_emoji_for(kind):
    """Eski nom bilan moslik — faqat qiymatni (emoji yoki custom_emoji_id) qaytaradi."""
    return get_reaction_config_for(kind)[1]


def set_reaction_emoji_for(kind, emoji):
    key, _ = REACTION_KIND_CONFIG[kind]
    with _state_lock:
        STATE.setdefault("config", {})[key] = {"type": "emoji", "value": emoji}
        save_state_locked()


def set_reaction_custom_emoji_for(kind, custom_emoji_id):
    key, _ = REACTION_KIND_CONFIG[kind]
    with _state_lock:
        STATE.setdefault("config", {})[key] = {"type": "custom_emoji", "value": custom_emoji_id}
        save_state_locked()


def get_reaction_emoji():
    """Eski nom bilan moslik uchun — guruhdagi oddiy admin reaksiyasi."""
    return get_reaction_emoji_for("admin")


def set_reaction_emoji(emoji):
    set_reaction_emoji_for("admin", emoji)


def set_base_weekly(value):
    with _state_lock:
        STATE.setdefault("config", {})["base_weekly"] = value
        save_state_locked()


def set_weekly_cap(value):
    with _state_lock:
        STATE.setdefault("config", {})["weekly_cap"] = value
        save_state_locked()


def get_force_channels():
    with _state_lock:
        STATE.setdefault("force_channels", [])
        return list(STATE["force_channels"])


def get_bonus_channels():
    with _state_lock:
        STATE.setdefault("bonus_channels", [])
        return list(STATE["bonus_channels"])


def get_pack_cache():
    with _state_lock:
        STATE.setdefault("pack_cache", {})
        return STATE["pack_cache"]


def get_stats():
    with _state_lock:
        STATE.setdefault("stats", {"total_requests": 0})
        STATE["stats"].setdefault("total_requests", 0)
        return dict(STATE["stats"])


def bump_total_requests():
    with _state_lock:
        stats = STATE.setdefault("stats", {"total_requests": 0})
        stats["total_requests"] = stats.get("total_requests", 0) + 1
        save_state_locked()


# ---------- Guruh/kanal kuzatuvi ----------

def register_group(chat):
    with _state_lock:
        groups = STATE.setdefault("groups", {})
        key = str(chat["id"])
        if key not in groups:
            groups[key] = {
                "title": chat.get("title") or str(chat["id"]),
                "type": chat.get("type"),
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state_locked()
        else:
            groups[key]["title"] = chat.get("title") or groups[key]["title"]


def register_channel(chat):
    with _state_lock:
        channels = STATE.setdefault("channels", {})
        key = str(chat["id"])
        if key not in channels:
            channels[key] = {
                "title": chat.get("title") or str(chat["id"]),
                "username": chat.get("username"),
                "added_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state_locked()
        else:
            channels[key]["title"] = chat.get("title") or channels[key]["title"]
            channels[key]["username"] = chat.get("username")


def forget_group(chat_id):
    with _state_lock:
        STATE.setdefault("groups", {}).pop(str(chat_id), None)
        save_state_locked()


def forget_channel(chat_id):
    with _state_lock:
        STATE.setdefault("channels", {}).pop(str(chat_id), None)
        save_state_locked()


# ---------- Majburiy / bonus kanallar ----------

def _resolve_chat(token):
    token = token.strip()
    chat_id = token
    try:
        chat_id = int(token)
    except ValueError:
        if not token.startswith("@"):
            chat_id = "@" + token
    data = tg_call("getChat", chat_id=chat_id)
    if not data.get("ok"):
        return None
    result = data["result"]
    return {
        "chat_id": result["id"],
        "title": result.get("title") or result.get("first_name") or str(result["id"]),
        "username": result.get("username"),
    }


def _invite_link_for(chat_id):
    data = tg_call("createChatInviteLink", chat_id=chat_id)
    if data.get("ok"):
        return data["result"]["invite_link"]
    return None


def _channel_join_button(ch):
    if ch.get("username"):
        url = f"https://t.me/{ch['username']}"
    else:
        url = _invite_link_for(ch["chat_id"]) or f"https://t.me/{ch['chat_id']}"
    return {"text": f"➕ {ch['title']}", "url": url}


def missing_force_channels(user_id):
    missing = []
    for ch in get_force_channels():
        status = get_chat_member_status(ch["chat_id"], user_id)
        if status not in ("member", "administrator", "creator"):
            missing.append(ch)
    return missing


def enforce_force_join(chat_id, user_id):
    if is_admin(user_id):
        return True
    missing = missing_force_channels(user_id)
    if not missing:
        return True
    keyboard = [[_channel_join_button(ch)] for ch in missing]
    keyboard.append([{"text": "✅ A'zo bo'ldim, tekshirish", "callback_data": "check_force_join"}])
    send_message(
        chat_id,
        "🔒 Botdan foydalanishdan oldin quyidagi kanal(lar)ga a'zo bo'ling, "
        "so'ng \"A'zo bo'ldim\" tugmasini bosing:",
        reply_markup={"inline_keyboard": keyboard},
    )
    return False


def build_bonus_menu_text_and_keyboard(user_id):
    record = get_user_record(user_id)
    channels = get_bonus_channels()
    unclaimed = [c for c in channels if c["chat_id"] not in record["claimed_bonus_channels"]]
    if not channels:
        return "Hozircha bonus kanallar mavjud emas.", back_to_menu_keyboard()
    if not unclaimed:
        return "🎁 Barcha bonus kanallar uchun mukofotni olib bo'lgansiz!", back_to_menu_keyboard()
    lines = ["🎁 Bonus kanalga a'zo bo'ling — har biri uchun +2 bir martalik limit:\n"]
    keyboard = []
    for ch in unclaimed:
        lines.append(f"• {ch['title']}")
        keyboard.append([
            _channel_join_button(ch),
            {"text": "Tekshirish", "callback_data": f"claim_bonus:{ch['chat_id']}"},
        ])
    keyboard.append([{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def get_user_record(user_id):
    with _state_lock:
        uid = str(user_id)
        if uid not in STATE["users"]:
            STATE["users"][uid] = default_user_record()
        record = STATE["users"][uid]
        for key, value in default_user_record().items():
            record.setdefault(key, value)
        return record


def is_premium(user_id):
    record = get_user_record(user_id)
    until = record.get("premium_until")
    if not until:
        return False
    return datetime.now(timezone.utc).timestamp() < until


# ---------- Kalit so'z (avto-javob) tizimi ----------

def get_keyword_limit(user_id):
    """None = cheksiz."""
    if is_admin(user_id):
        return None
    if is_premium(user_id):
        return None
    return get_limit_config().get("keyword_free_limit", 2)


def get_user_keywords(user_id):
    with _state_lock:
        return list(STATE.setdefault("keywords", {}).get(str(user_id), []))


def add_keyword(user_id, trigger, ktype, response, entities=None):
    with _state_lock:
        kws = STATE.setdefault("keywords", {}).setdefault(str(user_id), [])
        limit = get_keyword_limit(user_id)
        if limit is not None and len(kws) >= limit:
            return False, f"Limitingiz {limit} ta. Premium (100 Stars) olsangiz cheksiz bo'ladi."
        kw_id = uuid.uuid4().hex[:8]
        kw = {"id": kw_id, "trigger": trigger, "type": ktype, "response": response}
        if entities:
            kw["entities"] = entities
        kws.append(kw)
        save_state_locked()
        return True, kw_id


def delete_keyword(user_id, kw_id):
    with _state_lock:
        kws = STATE.setdefault("keywords", {}).get(str(user_id), [])
        before = len(kws)
        kws[:] = [k for k in kws if k["id"] != kw_id]
        save_state_locked()
        return len(kws) < before


def find_keyword_response(owner_id, text):
    """(response_text, entities) qaytaradi — entities bo'lmasa None."""
    kws = get_user_keywords(owner_id)
    text_l = (text or "").lower().strip()
    if not text_l:
        return None, None
    fallback = None
    for kw in kws:
        if kw.get("type") == "any":
            fallback = kw
            continue
        trig = (kw.get("trigger") or "").lower().strip()
        if trig and trig in text_l:
            return kw.get("response"), kw.get("entities")
    if fallback:
        return fallback.get("response"), fallback.get("entities")
    return None, None


# ---------- Offline/away — javobni kechiktirish ----------
# Telegram Bot API foydalanuvchining online/offline holatini bermaydi, shu sabab
# "N soniya ichida egasi shu chatga o'zi yozmasa" tarzida amalga oshiriladi — bu
# amalda xuddi "away" kabi ishlaydi va rasmiy API orqali ishonchli aniqlanadi.

def get_away_delay(owner_id):
    """0 = kechikishsiz (darhol), aks holda soniya."""
    with _state_lock:
        return int(STATE.setdefault("away_delay", {}).get(str(owner_id), 0))


def set_away_delay(owner_id, seconds):
    with _state_lock:
        STATE.setdefault("away_delay", {})[str(owner_id)] = max(0, int(seconds))
        save_state_locked()


def mark_owner_activity(owner_id, chat_id):
    with _state_lock:
        STATE.setdefault("owner_activity", {})[f"{owner_id}:{chat_id}"] = time.time()
        save_state_locked()


def owner_replied_since(owner_id, chat_id, since_ts):
    with _state_lock:
        ts = STATE.get("owner_activity", {}).get(f"{owner_id}:{chat_id}")
        return bool(ts and ts > since_ts)


def grant_premium(user_id, days=182):
    with _state_lock:
        record = get_user_record(user_id)
        now = datetime.now(timezone.utc).timestamp()
        current_until = record.get("premium_until") or now
        base = max(now, current_until)
        record["premium_until"] = base + days * 86400
        save_state_locked()
        return record["premium_until"]


def is_admin(user_id):
    with _state_lock:
        return user_id == SUPERADMIN_ID or user_id in STATE["admins"]


def compute_user_limit(user_id):
    record = get_user_record(user_id)
    cfg = get_limit_config()
    base = cfg["base_weekly"]
    weekly_cap = cfg["weekly_cap"]
    referrals = record["referrals"]
    threshold = max(0, weekly_cap - base)
    slots = base + referrals
    if slots < weekly_cap:
        mode = "weekly"
        limit = slots
    else:
        mode = "daily"
        extra = max(0, referrals - threshold)
        limit = 2 ** extra
    limit += record.get("bonus", 0)
    return mode, max(1, limit)


def ensure_period_reset(user_id):
    with _state_lock:
        record = get_user_record(user_id)
        mode, limit = compute_user_limit(user_id)
        key = today_str() if mode == "daily" else iso_week_str()
        if record.get("mode") != mode or record.get("period_key") != key:
            record["mode"] = mode
            record["period_key"] = key
            record["count"] = 0
            save_state_locked()
        return mode, limit


def limit_period_label(mode):
    return "bugun" if mode == "daily" else "shu hafta"


def can_make_request(user_id):
    if is_admin(user_id) or is_premium(user_id):
        return True, None
    record = get_user_record(user_id)
    mode, limit = ensure_period_reset(user_id)
    if record["count"] >= limit:
        period = "kunlik" if mode == "daily" else "haftalik"
        return False, (
            f"{period.capitalize()} limitingiz tugadi ({limit}/{limit}, {limit_period_label(mode)}).\n"
            f"Limitni oshirish uchun referal havolangiz orqali do'stlaringizni taklif qiling."
        )
    return True, None


def register_request(user_id, kind=None, detail=None):
    with _state_lock:
        record = get_user_record(user_id)
        record["lifetime_requests"] = record.get("lifetime_requests", 0) + 1
        if kind:
            counts = record.setdefault("type_counts", {"sticker": 0, "emoji": 0, "gif": 0, "pack": 0})
            counts[kind] = counts.get(kind, 0) + 1
            history = record.setdefault("history", [])
            history.append({
                "type": kind, "detail": detail or "",
                "at": datetime.now(timezone.utc).isoformat(),
            })
            if len(history) > 50:
                del history[: len(history) - 50]
        stats = STATE.setdefault("stats", {"total_requests": 0})
        stats["total_requests"] = stats.get("total_requests", 0) + 1
        if is_admin(user_id) or is_premium(user_id):
            save_state_locked()
            return
        ensure_period_reset(user_id)
        record["count"] += 1
        save_state_locked()


def register_known_user(user_id, from_user=None):
    with _state_lock:
        if user_id not in STATE["known_users"]:
            STATE["known_users"].append(user_id)
        record = get_user_record(user_id)
        if not record.get("first_seen"):
            record["first_seen"] = datetime.now(timezone.utc).isoformat()
        if from_user and from_user.get("username"):
            record["username"] = from_user["username"]
        if from_user and from_user.get("first_name"):
            record["first_name"] = from_user["first_name"]
        save_state_locked()


def user_label(uid):
    """Foydalanuvchi uchun o'qiladigan yorliq: @username, bo'lmasa ism, bo'lmasa id.
    Eski yozuvlarda ism/username saqlanmagan bo'lsa, Telegram'dan jonli so'rab keshlaydi."""
    rec = get_user_record(uid)
    if not rec.get("username") and not rec.get("first_name"):
        data = tg_call("getChat", chat_id=uid)
        if data.get("ok"):
            info = data["result"]
            with _state_lock:
                r = get_user_record(uid)
                if info.get("username"):
                    r["username"] = info["username"]
                if info.get("first_name"):
                    r["first_name"] = info["first_name"]
                save_state_locked()
            rec = r
    if rec.get("username"):
        return f"@{rec['username']}"
    if rec.get("first_name"):
        return rec["first_name"]
    return f"id:{uid}"


def register_referral(new_user_id, referrer_id):
    with _state_lock:
        new_record = get_user_record(new_user_id)
        if new_record["referred_by"] is not None or new_user_id == referrer_id:
            return
        new_record["referred_by"] = referrer_id
        referrer_record = get_user_record(referrer_id)
        referrer_record["referrals"] += 1
        save_state_locked()
        mode, limit = ensure_period_reset(referrer_id)
    period = "kunlik" if mode == "daily" else "haftalik"
    send_message(
        referrer_id,
        f"🎉 Sizning referal havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
        f"Yangi {period} limitingiz: {limit} ta.",
    )


def add_bonus_to_user(user_id, amount):
    with _state_lock:
        record = get_user_record(user_id)
        record["bonus"] = record.get("bonus", 0) + amount
        save_state_locked()
        mode, limit = ensure_period_reset(user_id)
    return mode, limit


def add_admin(user_id):
    with _state_lock:
        if user_id not in STATE["admins"]:
            STATE["admins"].append(user_id)
            save_state_locked()
    tg_call("deleteMyCommands", scope={"type": "chat", "chat_id": user_id})


def remove_admin(user_id):
    with _state_lock:
        if user_id in STATE["admins"]:
            STATE["admins"].remove(user_id)
            save_state_locked()


def add_force_channel(ch):
    with _state_lock:
        channels = STATE.setdefault("force_channels", [])
        if not any(c["chat_id"] == ch["chat_id"] for c in channels):
            channels.append(ch)
            save_state_locked()


def add_bonus_channel(ch):
    with _state_lock:
        channels = STATE.setdefault("bonus_channels", [])
        if not any(c["chat_id"] == ch["chat_id"] for c in channels):
            channels.append(ch)
            save_state_locked()


def remove_force_channel(chat_id):
    with _state_lock:
        channels = STATE.setdefault("force_channels", [])
        before = len(channels)
        STATE["force_channels"] = [c for c in channels if c["chat_id"] != chat_id]
        save_state_locked()
        return before - len(STATE["force_channels"])


def remove_bonus_channel(chat_id):
    with _state_lock:
        channels = STATE.setdefault("bonus_channels", [])
        before = len(channels)
        STATE["bonus_channels"] = [c for c in channels if c["chat_id"] != chat_id]
        save_state_locked()
        return before - len(STATE["bonus_channels"])


def claim_bonus_channel(user_id, target_chat_id):
    with _state_lock:
        record = get_user_record(user_id)
        if target_chat_id in record["claimed_bonus_channels"]:
            return False
        record["claimed_bonus_channels"].append(target_chat_id)
        record["bonus"] = record.get("bonus", 0) + 2
        save_state_locked()
        return True


# ---------- Sticker/emoji pack yuklash mantiqi ----------

def get_sticker_set(pack_name):
    data = tg_call("getStickerSet", name=pack_name)
    if data.get("ok"):
        return data["result"]
    return None


def resolve_pack_name_from_text(raw):
    raw = (raw or "").strip()
    name = extract_pack_name_from_link(raw)
    if name:
        return name
    return raw.strip("/ ") or None


def handle_tgs_by_index(chat_id, requester_info, requester_id, pack_name, index, reply_to=None, business_connection_id=None):
    threading.Thread(
        target=_handle_tgs_by_index_sync,
        args=(chat_id, requester_info, requester_id, pack_name, index, reply_to, business_connection_id),
        daemon=True,
    ).start()


def _handle_tgs_by_index_sync(chat_id, requester_info, requester_id, pack_name, index, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        send_message(chat_id, "Pack topilmadi. Nomini/havolani tekshiring.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    stickers = sticker_set.get("stickers", [])
    if index < 1 or index > len(stickers):
        send_message(chat_id, f"Bu pack'da {len(stickers)} ta element bor. 1 dan {len(stickers)} gacha raqam kiriting.",
                     reply_to=reply_to, business_connection_id=business_connection_id)
        return
    sticker = stickers[index - 1]
    file_path = get_file_path(sticker["file_id"])
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    content = download_file_bytes(file_path)
    ext = file_ext_for(sticker)
    filename = f"{pack_name}_{index}{ext}"
    register_request(requester_id, kind="emoji", detail=filename)
    caption = f"{pack_name} — #{index}"
    send_document_bytes(chat_id, filename, content, caption=caption, business_connection_id=business_connection_id)
    notify_admin(f"✅ .tgs orqali yuklandi\nKimdan: {requester_info}\nFayl: {filename}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, filename, content, caption=f"{requester_info} — {filename}")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, filename, content, caption=f"{requester_info} — {filename}")


def get_custom_emoji_set_name(custom_emoji_id):
    data = tg_call("getCustomEmojiStickers", custom_emoji_ids=[custom_emoji_id])
    if data.get("ok") and data["result"]:
        return data["result"][0].get("set_name")
    return None


def file_ext_for(sticker):
    if sticker.get("is_animated"):
        return ".tgs"
    if sticker.get("is_video"):
        return ".webm"
    return ".webp"


def pack_content_hash(sticker_set):
    """Pack ichidagi barcha file_unique_id'lardan hash — pack tarkibi (bitta fayl
    almashtirilgan bo'lsa ham) o'zgarganini aniqlash uchun, faqat sonini emas."""
    ids = sorted(s.get("file_unique_id", "") for s in sticker_set.get("stickers", []))
    return hashlib.sha256("|".join(ids).encode()).hexdigest()


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


def handle_pack_request(chat_id, pack_name, requester_info, requester_id, reply_to=None, business_connection_id=None):
    threading.Thread(
        target=_handle_pack_request_sync,
        args=(chat_id, pack_name, requester_info, requester_id, reply_to, business_connection_id),
        daemon=True,
    ).start()


def _handle_pack_request_sync(chat_id, pack_name, requester_info, requester_id, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return

    send_message(chat_id, f"'{pack_name}' qidirilmoqda, kuting...", reply_to=reply_to,
                 business_connection_id=business_connection_id)

    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        send_message(chat_id, "Pack topilmadi. Nomini tekshiring.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        notify_admin(f"⚠️ Muvaffaqiyatsiz so'rov\nKimdan: {requester_info}\nPack: {pack_name}\nSabab: topilmadi")
        return

    current_hash = pack_content_hash(sticker_set)

    if CACHE_GROUP_ID:
        cache = get_pack_cache()
        cached = cache.get(pack_name.lower())
        if cached and cached.get("content_hash") == current_hash:
            result = send_document_by_file_id(
                chat_id, cached["file_id"], caption=f"{cached['sticker_count']} ta fayl topildi. (kesh)",
                business_connection_id=business_connection_id,
            )
            if result.get("ok"):
                register_request(requester_id, kind="pack", detail=pack_name)
                notify_admin(f"✅ So'rov keshdan bajarildi\nKimdan: {requester_info}\nPack: {pack_name}")
                if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
                    send_document_by_file_id(
                        SUPERADMIN_ID, cached["file_id"],
                        caption=f"{requester_info} so'ragan pack: {pack_name} (kesh)",
                    )
                return
        elif cached:
            with _state_lock:
                get_pack_cache().pop(pack_name.lower(), None)
                save_state_locked()

    buf, result = process_pack(pack_name)
    if buf is None:
        send_message(chat_id, result, reply_to=reply_to, business_connection_id=business_connection_id)
        notify_admin(f"⚠️ Muvaffaqiyatsiz so'rov\nKimdan: {requester_info}\nPack: {pack_name}\nSabab: {result}")
        return

    register_request(requester_id, kind="pack", detail=pack_name)
    zip_bytes = buf.getvalue()
    send_result = send_document_bytes(chat_id, f"{pack_name}.zip", zip_bytes, caption=f"{result} ta fayl topildi.",
                                       business_connection_id=business_connection_id)

    if CACHE_GROUP_ID:
        cache_result = send_document_bytes(CACHE_GROUP_ID, f"{pack_name}.zip", zip_bytes)
        if cache_result.get("ok"):
            doc = cache_result["result"]["document"]
            with _state_lock:
                get_pack_cache()[pack_name.lower()] = {
                    "file_id": doc["file_id"],
                    "sticker_count": result,
                    "content_hash": current_hash,
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                }
                save_state_locked()

    notify_admin(f"✅ Yangi so'rov bajarildi\nKimdan: {requester_info}\nPack: {pack_name}\nFayllar soni: {result}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        if send_result.get("ok"):
            send_document_by_file_id(
                SUPERADMIN_ID, send_result["result"]["document"]["file_id"],
                caption=f"{requester_info} so'ragan pack: {pack_name} ({result} ta fayl)",
            )
        else:
            send_document_bytes(
                SUPERADMIN_ID, f"{pack_name}.zip", zip_bytes,
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


def extract_animation_file(msg):
    """Telegram GIF'lari 'animation' obyekti sifatida keladi (odatda mime_type=video/mp4)."""
    animation = msg.get("animation")
    if animation:
        return animation["file_id"], ".mp4"
    return None, None


def extract_single_sticker_file(msg):
    sticker = msg.get("sticker")
    if sticker:
        return sticker["file_id"], file_ext_for(sticker), sticker.get("emoji", ""), "sticker", sticker.get("custom_emoji_id")
    for field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        entities = msg.get(entity_field) or []
        for ent in entities:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
                data = tg_call("getCustomEmojiStickers", custom_emoji_ids=[ent["custom_emoji_id"]])
                if data.get("ok") and data["result"]:
                    em = data["result"][0]
                    return em["file_id"], file_ext_for(em), em.get("emoji", ""), "emoji", ent["custom_emoji_id"]
    return None, None, None, None, None


def zip_single_file(filename, content):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    buf.seek(0)
    return buf.getvalue()


# ================= ID olish (single + butun pack) =================

ID_LIST_CHUNK_BUDGET = 3200  # Telegram 4096 belgi chegarasidan xavfsiz kam


def send_clean_id(chat_id, label, emoji_char, file_id, custom_emoji_id=None, business_connection_id=None):
    """Bot ishlata oladigan haqiqiy ID'ni (file_id, custom emoji uchun custom_emoji_id ham)
    toza, chiziqcha bilan ajratilgan formatda yuboradi."""
    placeholder = html.escape(emoji_char or "🔸", quote=False)
    lines = [f"🆔 {label} ID:"]
    if custom_emoji_id:
        lines.append(
            f"{placeholder} - <code>{html.escape(str(custom_emoji_id), quote=False)}</code>"
            f"  (custom_emoji_id — jonli emoji sifatida ishlatish uchun)"
        )
        lines.append(f"file_id - <code>{html.escape(str(file_id), quote=False)}</code>  (faylni qayta yuborish uchun)")
    else:
        lines.append(f"{placeholder} - <code>{html.escape(str(file_id), quote=False)}</code>")
    return send_message(chat_id, "\n".join(lines), parse_mode_html=True, business_connection_id=business_connection_id)


def _respect_retry_after(result):
    """Telegram 429 (flood) qaytarsa, ko'rsatilgan vaqtcha kutadi."""
    if result and not result.get("ok"):
        retry_after = (result.get("parameters") or {}).get("retry_after")
        if retry_after:
            time.sleep(min(retry_after, 30) + 0.5)


def build_pack_id_txt_content(sticker_set, pack_name):
    is_emoji_pack = sticker_set.get("sticker_type") == "custom_emoji"
    title = sticker_set.get("title") or pack_name
    lines = [f"{title} - {pack_name}", ""]
    for i, sticker in enumerate(sticker_set.get("stickers", []), start=1):
        if is_emoji_pack:
            id_value = sticker.get("custom_emoji_id") or sticker.get("file_id", "")
        else:
            id_value = sticker.get("file_id", "")
        lines.append(f"{i}-{sticker.get('emoji', '')} - {id_value}")
    return "\n".join(lines)


def send_pack_ids_as_txt_file(chat_id, pack_name, sticker_set, business_connection_id=None):
    content = build_pack_id_txt_content(sticker_set, pack_name)
    send_document_bytes(
        chat_id, f"{pack_name}_id.txt", content.encode("utf-8"),
        caption=f"{pack_name} — barcha ID'lar", business_connection_id=business_connection_id,
    )


def build_pack_id_live_chunks(sticker_set, pack_name, use_tg_emoji):
    """'Matn - to'liq' varianti: 'N-emoji - ID' qatorlari. use_tg_emoji=True bo'lsa custom
    emoji <tg-emoji> orqali jonli ko'rsatiladi (buning uchun bot egasida Telegram Premium
    bo'lishi shart — aks holda Telegram xabarni rad etadi, bu holat chaqiruvchi tomonda
    tekshiriladi va oddiy formatga o'tiladi), ID esa <code> bilan bir-bosib-nusxalanadi.
    Xabar uzunligiga qarab bir nechta xabarga bo'linadi."""
    stickers = sticker_set.get("stickers", [])
    title = sticker_set.get("title") or pack_name
    header = f"{html.escape(title, quote=False)} — <code>{html.escape(pack_name, quote=False)}</code>"
    chunks = []
    current_lines = [header, ""]
    current_len = len(header) + 1
    for i, sticker in enumerate(stickers, start=1):
        placeholder = html.escape(sticker.get("emoji") or "🔸", quote=False)
        if use_tg_emoji and sticker.get("custom_emoji_id"):
            id_value = sticker["custom_emoji_id"]
            # emoji-id bu yerda HTML atribut ichida — bu yagona joy, tirnoqlarni ham escape qilish kerak
            emoji_part = f'<tg-emoji emoji-id="{html.escape(id_value, quote=True)}">{placeholder}</tg-emoji>'
        else:
            id_value = sticker.get("file_id", "")
            emoji_part = placeholder
        line = f"{i}-{emoji_part} - <code>{html.escape(str(id_value), quote=False)}</code>"
        if current_len + len(line) + 1 > ID_LIST_CHUNK_BUDGET and len(current_lines) > 2:
            chunks.append("\n".join(current_lines))
            current_lines = [header + " (davomi)", ""]
            current_len = len(current_lines[0]) + 1
        current_lines.append(line)
        current_len += len(line) + 1
    if len(current_lines) > 2:
        chunks.append("\n".join(current_lines))
    return chunks


def send_pack_ids_full_text(chat_id, pack_name, sticker_set, business_connection_id=None):
    is_emoji_pack = sticker_set.get("sticker_type") == "custom_emoji"
    chunks = build_pack_id_live_chunks(sticker_set, pack_name, use_tg_emoji=is_emoji_pack)
    if not chunks:
        send_message(chat_id, "Bu pack bo'sh ko'rinadi.", business_connection_id=business_connection_id)
        return
    if is_emoji_pack:
        first = send_message(chat_id, chunks[0], parse_mode_html=True, business_connection_id=business_connection_id)
        if not (first and first.get("ok")):
            send_message(
                chat_id,
                "⚠️ Jonli emoji ko'rsatib bo'lmadi — bot egasida Telegram Premium bo'lishi kerak. "
                "ID'larni oddiy (jonli ko'rinishsiz) formatda yuboryapman.",
                business_connection_id=business_connection_id,
            )
            for chunk in build_pack_id_live_chunks(sticker_set, pack_name, use_tg_emoji=False):
                send_message(chat_id, chunk, parse_mode_html=True, business_connection_id=business_connection_id)
            return
        for chunk in chunks[1:]:
            send_message(chat_id, chunk, parse_mode_html=True, business_connection_id=business_connection_id)
    else:
        for chunk in chunks:
            send_message(chat_id, chunk, parse_mode_html=True, business_connection_id=business_connection_id)


def send_custom_emoji_preview(chat_id, custom_emoji_id, placeholder_char, business_connection_id=None):
    """Custom emoji sendSticker orqali ko'rinmaydi — Telegram uni faqat matn ichida
    (<tg-emoji>) ko'rsatishga ruxsat beradi, shu uchun shu yo'l bilan yuboramiz. Buning
    uchun bot egasida Telegram Premium bo'lishi shart; bo'lmasa xato qaytadi va oddiy
    ogohlantirish bilan davom etamiz (oqim to'xtamaydi)."""
    placeholder = html.escape(placeholder_char or "🔸", quote=False)
    text = f'<tg-emoji emoji-id="{html.escape(custom_emoji_id, quote=True)}">{placeholder}</tg-emoji>'
    result = send_message(chat_id, text, parse_mode_html=True, business_connection_id=business_connection_id)
    if not (result and result.get("ok")):
        send_message(
            chat_id,
            f"{placeholder} (jonli ko'rinishni yubora olmadim — bot egasida Telegram Premium kerak)",
            business_connection_id=business_connection_id,
        )
    return result


def send_pack_ids_sequential(chat_id, pack_name, sticker_set, business_connection_id=None):
    """Har bir element uchun: avval o'sha stiker/emojining o'zini (custom emoji bo'lsa jonli
    ko'rinishini, oddiy sticker bo'lsa sendSticker orqali), keyin uning ID'sini alohida xabar
    qilib yuboradi. Katta pack'larda flood-limitga tegmaslik uchun orada kichik pauza bor va
    Telegram 429 qaytarsa retry_after'ga qarab kutiladi."""
    is_emoji_pack = sticker_set.get("sticker_type") == "custom_emoji"
    stickers = sticker_set.get("stickers", [])
    if not stickers:
        send_message(chat_id, "Bu pack bo'sh ko'rinadi.", business_connection_id=business_connection_id)
        return
    send_message(
        chat_id, f"📦 {pack_name} — {len(stickers)} ta element, birma-bir yuboryapman...",
        business_connection_id=business_connection_id,
    )
    for i, sticker in enumerate(stickers, start=1):
        file_id = sticker.get("file_id")
        if not file_id:
            continue
        custom_emoji_id = sticker.get("custom_emoji_id") if is_emoji_pack else None
        if custom_emoji_id:
            r1 = send_custom_emoji_preview(chat_id, custom_emoji_id, sticker.get("emoji"), business_connection_id)
        else:
            r1 = send_sticker_by_file_id(chat_id, file_id, business_connection_id=business_connection_id)
        _respect_retry_after(r1)
        r2 = send_clean_id(
            chat_id, f"{i}/{len(stickers)}", sticker.get("emoji"), file_id, custom_emoji_id,
            business_connection_id=business_connection_id,
        )
        _respect_retry_after(r2)
        time.sleep(0.35)


def convert_to_webm(content_bytes):
    """MP4/GIF baytlarini ovozsiz VP9 webm'ga o'giradi. Muvaffaqiyatsiz bo'lsa None qaytaradi."""
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "input.mp4")
        out_path = os.path.join(tmp, "output.webm")
        with open(in_path, "wb") as f:
            f.write(content_bytes)
        cmd = [
            ffmpeg_path, "-y", "-i", in_path,
            "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-an",
            out_path,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.error("ffmpeg webm konvertatsiya xatosi: %s", e)
            return None
        if not os.path.exists(out_path):
            return None
        with open(out_path, "rb") as f:
            return f.read()


def handle_id_single_request(chat_id, pending, requester_id):
    threading.Thread(
        target=_handle_id_single_request_sync,
        args=(chat_id, pending, requester_id),
        daemon=True,
    ).start()


def _handle_id_single_request_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    kind_label = "Custom emoji" if pending.get("kind") == "emoji" else "Sticker"
    register_request(requester_id, kind=f"{pending.get('kind', 'sticker')}_id", detail="raw_id")
    send_clean_id(chat_id, kind_label, pending.get("emoji_char"), pending["file_id"], pending.get("custom_emoji_id"))
    notify_admin(f"✅ {kind_label} ID so'raldi\nKimdan: {pending['requester_info']}")


def handle_id_pack_request(chat_id, pack_name, requester_info, requester_id, mode):
    threading.Thread(
        target=_handle_id_pack_request_sync,
        args=(chat_id, pack_name, requester_info, requester_id, mode),
        daemon=True,
    ).start()


def _handle_id_pack_request_sync(chat_id, pack_name, requester_info, requester_id, mode):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        send_message(chat_id, "Pack topilmadi. Nomini tekshiring.")
        notify_admin(f"⚠️ Muvaffaqiyatsiz ID so'rovi\nKimdan: {requester_info}\nPack: {pack_name}\nSabab: topilmadi")
        return
    register_request(requester_id, kind="pack_id", detail=pack_name)
    if mode == "txt":
        send_pack_ids_as_txt_file(chat_id, pack_name, sticker_set)
    elif mode == "seq":
        send_pack_ids_sequential(chat_id, pack_name, sticker_set)
    else:
        send_pack_ids_full_text(chat_id, pack_name, sticker_set)
    notify_admin(
        f"✅ Pack ID so'raldi ({mode})\nKimdan: {requester_info}\nPack: {pack_name}\n"
        f"Fayllar soni: {len(sticker_set.get('stickers', []))}"
    )


def handle_gif_webm_request(chat_id, pending, requester_id):
    threading.Thread(
        target=_handle_gif_webm_request_sync,
        args=(chat_id, pending, requester_id),
        daemon=True,
    ).start()


def _handle_gif_webm_request_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    file_path = get_file_path(pending["file_id"])
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.")
        return
    content = download_file_bytes(file_path)
    webm_bytes = convert_to_webm(content)
    if not webm_bytes:
        send_message(chat_id, "GIF'ni webm'ga o'girishda xato yuz berdi. Qaytadan urinib ko'ring.")
        return
    register_request(requester_id, kind="gif_webm", detail="gif.webm")
    send_animation_bytes(chat_id, "gif.webm", webm_bytes, caption="🎞 WebM tayyor.")
    notify_admin(f"✅ GIF webm'ga o'girildi\nKimdan: {pending['requester_info']}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_animation_bytes(SUPERADMIN_ID, "gif.webm", webm_bytes, caption=f"{pending['requester_info']} — webm GIF")
    if CACHE_GROUP_ID:
        send_animation_bytes(CACHE_GROUP_ID, "gif.webm", webm_bytes, caption=f"{pending['requester_info']} — webm GIF")


def handle_gif_id_request(chat_id, pending, requester_id):
    threading.Thread(
        target=_handle_gif_id_request_sync,
        args=(chat_id, pending, requester_id),
        daemon=True,
    ).start()


def _handle_gif_id_request_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    register_request(requester_id, kind="gif_id", detail="gif_id")
    send_clean_id(chat_id, "GIF", "🎞", pending["file_id"])
    notify_admin(f"✅ GIF ID so'raldi\nKimdan: {pending['requester_info']}")


def handle_single_sticker_request(chat_id, reply, requester_info, requester_id, reply_to=None, business_connection_id=None):
    threading.Thread(
        target=_handle_single_sticker_request_sync,
        args=(chat_id, reply, requester_info, requester_id, reply_to, business_connection_id),
        daemon=True,
    ).start()


def _handle_single_sticker_request_sync(chat_id, reply, requester_info, requester_id, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    file_id, ext, emoji_char, kind, _ = extract_single_sticker_file(reply)
    if not file_id:
        send_message(chat_id, "Bu xabarda sticker/custom emoji topilmadi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    file_path = get_file_path(file_id)
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    content = download_file_bytes(file_path)
    filename = f"sticker_{emoji_char}{ext}".replace("/", "_")
    register_request(requester_id, kind=kind, detail=filename)
    zip_bytes = zip_single_file(filename, content)
    zip_name = f"{filename}.zip"
    send_document_bytes(chat_id, zip_name, zip_bytes, caption="Faylni ochish uchun ZIP'ni yeching.",
                        business_connection_id=business_connection_id)
    notify_admin(f"✅ Bitta sticker yuklandi\nKimdan: {requester_info}\nFayl: {filename}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan sticker")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan sticker")


def handle_single_sticker_request_from_pending(chat_id, pending, requester_id):
    threading.Thread(
        target=_handle_single_sticker_request_from_pending_sync,
        args=(chat_id, pending, requester_id),
        daemon=True,
    ).start()


def _handle_single_sticker_request_from_pending_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    file_path = get_file_path(pending["file_id"])
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.")
        return
    content = download_file_bytes(file_path)
    filename = f"sticker_{pending['emoji_char']}{pending['ext']}".replace("/", "_")
    register_request(requester_id, kind=pending.get("kind", "sticker"), detail=filename)
    zip_bytes = zip_single_file(filename, content)
    zip_name = f"{filename}.zip"
    send_document_bytes(chat_id, zip_name, zip_bytes, caption="Faylni ochish uchun ZIP'ni yeching.")
    notify_admin(f"✅ Bitta sticker yuklandi\nKimdan: {pending['requester_info']}\nFayl: {filename}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{pending['requester_info']} yuklagan sticker")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{pending['requester_info']} yuklagan sticker")


def handle_animation_request(chat_id, msg, requester_info, requester_id, reply_to=None, business_connection_id=None):
    threading.Thread(
        target=_handle_animation_request_sync,
        args=(chat_id, msg, requester_info, requester_id, reply_to, business_connection_id),
        daemon=True,
    ).start()


def _handle_animation_request_sync(chat_id, msg, requester_info, requester_id, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    file_id, ext = extract_animation_file(msg)
    if not file_id:
        send_message(chat_id, "Bu xabarda GIF/animatsiya topilmadi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    file_path = get_file_path(file_id)
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    content = download_file_bytes(file_path)
    filename = f"gif_{int(datetime.now(timezone.utc).timestamp())}{ext}"
    register_request(requester_id, kind="gif", detail=filename)
    zip_bytes = zip_single_file(filename, content)
    zip_name = f"{filename}.zip"
    send_document_bytes(chat_id, zip_name, zip_bytes, caption="Faylni ochish uchun ZIP'ni yeching.",
                        business_connection_id=business_connection_id)
    notify_admin(f"✅ GIF yuklandi\nKimdan: {requester_info}\nFayl: {filename}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan GIF")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan GIF")


def requester_label(from_user):
    return (
        f"@{from_user.get('username')} (id:{from_user.get('id')})"
        if from_user.get("username")
        else f"id:{from_user.get('id')}"
    )


# ================= INLINE MENYULAR =================

def main_menu_keyboard(user_id):
    rows = [
        [{"text": "📦 Pack yuklab olish", "callback_data": "menu_getpack"}],
        [
            {"text": "🔗 Referal", "callback_data": "menu_ref"},
            {"text": "📊 Limitim", "callback_data": "menu_limit"},
        ],
        [
            {"text": "🎁 Bonus", "callback_data": "menu_bonus"},
            {"text": "⭐ Premium", "callback_data": "menu_premium"},
        ],
        [{"text": "❓ Yordam", "callback_data": "menu_help"}],
        [{"text": "🏆 Reyting", "callback_data": "menu_leaderboard"}],
        [{"text": "🔑 Avto-javob (Business)", "callback_data": "menu_keywords"}],
    ]
    if is_admin(user_id):
        rows.append([{"text": "👑 Superadmin panel" if user_id == SUPERADMIN_ID else "🛠 Admin panel",
                       "callback_data": "menu_admin_panel"}])
    return {"inline_keyboard": rows}


def back_to_menu_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}]]}


def back_to_panel_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}]]}


def admin_panel_keyboard(user_id):
    """Oddiy admin — cheklangan panel (ko'rish + broadcast).
    Superadmin — to'liq boshqaruv panelini ko'radi."""
    is_super = user_id == SUPERADMIN_ID
    rows = [
        [{"text": "👥 Foydalanuvchilar", "callback_data": "panel_users:0"}],
        [
            {"text": "👨‍👩‍👧 Guruhlar", "callback_data": "panel_groups:0"},
            {"text": "📢 Kanallar", "callback_data": "panel_channels:0"},
        ],
        [
            {"text": "🔒 Majburiy kanallar", "callback_data": "panel_forcechannels"},
            {"text": "🎁 Bonus kanallar", "callback_data": "panel_bonuschannels"},
        ],
    ]
    if is_super:
        rows.append([
            {"text": "⚙️ Limit sozlamalari", "callback_data": "panel_limits"},
            {"text": "📣 Broadcast", "callback_data": "panel_broadcast"},
        ])
        rows.append([
            {"text": "✍️ Adminlarga xabar", "callback_data": "panel_admin_message"},
            {"text": "💬 Foydalanuvchiga yozish", "callback_data": "panel_dm_user"},
        ])
        rows.append([
            {"text": "🛡 Adminlar", "callback_data": "panel_admins"},
            {"text": "⚡ Reaksiya emoji", "callback_data": "panel_reaction"},
        ])
        rows.append([{"text": "✨ Bot imzosi (premium emoji)", "callback_data": "panel_signature"}])
        rows.append([
            {"text": "🏆 Referal reyting", "callback_data": "panel_leaderboard"},
            {"text": "📤 Eksport (CSV)", "callback_data": "panel_export"},
        ])
        rows.append([{"text": "🤖 Bot admin joylar", "callback_data": "panel_botadmin"}])
    else:
        rows.append([
            {"text": "📣 Broadcast", "callback_data": "panel_broadcast"},
            {"text": "✍️ Adminlarga xabar", "callback_data": "panel_admin_message"},
        ])
        rows.append([{"text": "💬 Foydalanuvchiga yozish", "callback_data": "panel_dm_user"}])
    rows.append([{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}])
    return {"inline_keyboard": rows}


PAGE_SIZE = 8


def _paginate_keyboard(items, prefix, page):
    """items: list of (key, label). prefix: callback prefix, ex 'user_detail'."""
    start = page * PAGE_SIZE
    chunk = items[start:start + PAGE_SIZE]
    rows = [[{"text": label, "callback_data": f"{prefix}:{key}"}] for key, label in chunk]
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"panel_{prefix.split('_')[0]}s:{page - 1}"})
    if start + PAGE_SIZE < len(items):
        nav.append({"text": "➡️", "callback_data": f"panel_{prefix.split('_')[0]}s:{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
    return {"inline_keyboard": rows}


def build_help_text(user_id):
    cfg = get_limit_config()
    base = cfg["base_weekly"]
    weekly_cap = cfg["weekly_cap"]
    return (
        "📋 <b>Bot haqida</b>\n\n"
        "Menga sticker/custom emoji/GIF forward qiling yoki \"📦 Pack yuklab olish\" "
        "tugmasini bosib pack nomini yuboring — men barcha fayllarni ZIP qilib beraman.\n\n"
        "⚙️ <b>Limit qoidalari:</b>\n"
        f"• Yangi foydalanuvchi: haftasiga {base} marta bepul so'rov.\n"
        f"• Har bir referal haftalik imkoniyatingizni +1 taga oshiradi "
        f"({base} → {base + 1} → ... → {weekly_cap}).\n"
        f"• Imkoniyatlar {weekly_cap} taga yetganda, tizim HAFTALIKdan KUNLIKka o'tadi.\n"
        f"• Shundan keyin har bir qo'shimcha referal kunlik limitni 2 baravar oshiradi.\n"
        "• Adminlar/premium uchun limit yo'q.\n"
    )


# ---------- Callback query handler ----------

def handle_callback_query(cq):
    cq_id = cq["id"]
    data = cq.get("data", "")
    from_user = cq.get("from", {})
    user_id = from_user.get("id")
    message = cq.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if chat_id is None or message_id is None:
        answer_callback_query(cq_id)
        return

    register_known_user(user_id, from_user)

    # ---- Umumiy foydalanuvchi menyulari ----
    if data == "menu_home":
        answer_callback_query(cq_id)
        safe_edit_or_send(
            chat_id, message_id,
            "Salom! Menga sticker/custom emoji forward qiling yoki pastdagi "
            "\"📦 Pack yuklab olish\" tugmasi orqali pack nomini yuboring.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if data == "menu_getpack":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "getpack")
        safe_edit_or_send(
            chat_id, message_id,
            "📦 Pack nomini (yoki t.me/addstickers/... havolasini) yozib yuboring:",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "menu_ref":
        answer_callback_query(cq_id)
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        record = get_user_record(user_id)
        mode, limit = ensure_period_reset(user_id)
        period = "kunlik" if mode == "daily" else "haftalik"
        safe_edit_or_send(
            chat_id, message_id,
            f"🔗 Referal havolangiz:\n{link}\n\n"
            f"Hozirgi referallar: {record['referrals']}\n"
            f"Yangi {period} limitingiz: {limit} ta.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "menu_limit":
        answer_callback_query(cq_id)
        if is_admin(user_id):
            text = "📊 Foydalanishingiz: cheksiz (admin)"
        else:
            mode, limit = ensure_period_reset(user_id)
            record = get_user_record(user_id)
            period_label = "Bugungi" if mode == "daily" else "Shu haftadagi"
            text = f"📊 {period_label} foydalanish: {record['count']}/{limit}"
        safe_edit_or_send(chat_id, message_id, text, reply_markup=back_to_menu_keyboard())
        return

    if data == "menu_help":
        answer_callback_query(cq_id)
        safe_edit_or_send(chat_id, message_id, build_help_text(user_id), parse_mode_html=True,
                           reply_markup=back_to_menu_keyboard())
        return

    if data == "menu_bonus":
        answer_callback_query(cq_id)
        text, keyboard = build_bonus_menu_text_and_keyboard(user_id)
        safe_edit_or_send(chat_id, message_id, text, reply_markup=keyboard)
        return

    if data == "menu_premium":
        answer_callback_query(cq_id)
        if is_premium(user_id):
            record = get_user_record(user_id)
            until = datetime.fromtimestamp(record["premium_until"], tz=timezone.utc).strftime("%Y-%m-%d")
            safe_edit_or_send(chat_id, message_id,
                               f"⭐ Sizda premium allaqachon faol — {until} sanagacha cheksiz foydalanasiz.",
                               reply_markup=back_to_menu_keyboard())
        else:
            text = ("⭐ <b>Premium</b>\n\nPremium bilan kunlik/haftalik limitlarsiz, cheksiz pack yuklab olasiz "
                    "(6 oy muddatga, Telegram Stars orqali).")
            keyboard = {"inline_keyboard": [
                [{"text": "⭐ 100 Stars uchun sotib olish", "callback_data": "buy_premium"}],
                [{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}],
            ]}
            safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "buy_premium":
        answer_callback_query(cq_id)
        tg_call(
            "sendInvoice", chat_id=chat_id, title="StokerDownloader Premium (6 oy)",
            description="Cheksiz pack yuklab olish, kunlik/haftalik limitlarsiz — 6 oy muddatga.",
            payload=f"premium_182:{user_id}", provider_token="", currency="XTR",
            prices=[{"label": "Premium 6 oy", "amount": 100}],
        )
        return

    if data == "menu_keywords":
        answer_callback_query(cq_id)
        kws = get_user_keywords(user_id)
        limit = get_keyword_limit(user_id)
        limit_text = "cheksiz" if limit is None else f"{len(kws)}/{limit}"
        delay = get_away_delay(user_id)
        delay_text = "darhol (kechikishsiz)" if delay == 0 else f"{delay} soniya kutib, keyin"
        text = (
            "🔑 <b>Avto-javob (Telegram Business)</b>\n\n"
            "Bu kalitlar faqat profilingizga Business orqali ulangan botga "
            "boshqalar yozganda ishlaydi.\n\n"
            f"Joriy: {limit_text}\n"
            f"⏱ Javob: {delay_text}\n\n"
            "Masalan: kimdir \"salom\" desa, bot avtomatik javob yozadi."
        )
        rows = []
        for kw in kws:
            label = "🌐 Har qanday xabar" if kw["type"] == "any" else f"🎯 «{kw['trigger']}»"
            if kw.get("entities"):
                label += " ✨"
            rows.append([
                {"text": label, "callback_data": f"kw_view:{kw['id']}"},
                {"text": "🗑", "callback_data": f"kw_delete:{kw['id']}"},
            ])
        if limit is None or len(kws) < limit:
            rows.append([{"text": "➕ Yangi kalit qo'shish", "callback_data": "kw_add_start"}])
        else:
            rows.append([{"text": "⭐ Premium olish (cheksiz)", "callback_data": "menu_premium"}])
        rows.append([{"text": "⏱ Javob kechikishini sozlash (offline)", "callback_data": "kw_delay_start"}])
        rows.append([{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}])
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})
        return

    if data == "kw_delay_start":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "away_delay")
        safe_edit_or_send(
            chat_id, message_id,
            "Necha soniya kutilsin? Shu vaqt ichida o'zingiz yozib ulgurmasangiz, "
            "avto-javob ishga tushadi.\n\n0 — darhol javob (kechikishsiz).",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "kw_add_start":
        answer_callback_query(cq_id)
        limit = get_keyword_limit(user_id)
        kws = get_user_keywords(user_id)
        if limit is not None and len(kws) >= limit:
            safe_edit_or_send(chat_id, message_id, "Limitingiz tugagan.",
                               reply_markup={"inline_keyboard": [[{"text": "⬅️ Orqaga", "callback_data": "menu_keywords"}]]})
            return
        rows = [
            [{"text": "🎯 Aniq so'z/ibora bo'yicha", "callback_data": "kw_type:exact"}],
            [{"text": "🌐 Har qanday xabarga (default javob)", "callback_data": "kw_type:any"}],
            [{"text": "⬅️ Orqaga", "callback_data": "menu_keywords"}],
        ]
        safe_edit_or_send(chat_id, message_id, "Kalit turini tanlang:", reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("kw_type:"):
        answer_callback_query(cq_id)
        ktype = data.split(":", 1)[1]
        if ktype == "any":
            set_pending_input(user_id, "kw_response", {"trigger": "*", "type": "any"})
            safe_edit_or_send(chat_id, message_id,
                               "Endi boshqalar yozganda avtomatik yuboriladigan javob matnini yozing:",
                               reply_markup=back_to_menu_keyboard())
        else:
            set_pending_input(user_id, "kw_trigger")
            safe_edit_or_send(chat_id, message_id,
                               "Qaysi so'z/ibora kelsa ishga tushsin? (masalan: salom)",
                               reply_markup=back_to_menu_keyboard())
        return

    if data.startswith("kw_delete:"):
        answer_callback_query(cq_id)
        kw_id = data.split(":", 1)[1]
        delete_keyword(user_id, kw_id)
        safe_edit_or_send(chat_id, message_id, "🗑 O'chirildi.",
                           reply_markup={"inline_keyboard": [[{"text": "⬅️ Kalitlarim", "callback_data": "menu_keywords"}]]})
        return

    if data.startswith("kw_view:"):
        answer_callback_query(cq_id)
        kw_id = data.split(":", 1)[1]
        kws = get_user_keywords(user_id)
        kw = next((k for k in kws if k["id"] == kw_id), None)
        if not kw:
            safe_edit_or_send(chat_id, message_id, "Topilmadi.",
                               reply_markup={"inline_keyboard": [[{"text": "⬅️ Kalitlarim", "callback_data": "menu_keywords"}]]})
            return
        trig = "Har qanday xabar" if kw["type"] == "any" else kw["trigger"]
        note = "\n✨ (bu javobda maxsus formatlash/premium emoji bor — shu yerda oddiy ko'rinadi, jo'natilganda to'g'ri chiqadi)" if kw.get("entities") else ""
        text = f"🎯 Kalit: {trig}\n💬 Javob: {kw['response']}{note}"
        rows = [[{"text": "🗑 O'chirish", "callback_data": f"kw_delete:{kw['id']}"}],
                [{"text": "⬅️ Kalitlarim", "callback_data": "menu_keywords"}]]
        safe_edit_or_send(chat_id, message_id, text, reply_markup={"inline_keyboard": rows})
        return

    if data == "menu_leaderboard":
        answer_callback_query(cq_id)
        top = get_referral_leaderboard(10)
        if not top:
            text = "🏆 <b>Referal reyting</b>\n\nHali hech kim referal orqali taklif qilmagan."
            rows = []
        else:
            text = "🏆 <b>Top referallar</b>\n\nKo'proq ma'lumot uchun foydalanuvchi ustiga bosing:"
            rows = [[{"text": f"{i}. {user_label(uid)} — {refs} ta referal",
                      "callback_data": f"pub_stats:{uid}"}]
                    for i, (uid, refs, _lifetime) in enumerate(top, start=1)]
        rows.append([{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}])
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("pub_stats:"):
        answer_callback_query(cq_id)
        target_id = int(data.split(":", 1)[1])
        rec = get_user_record(target_id)
        counts = rec.get("type_counts", {}) or {}
        text = (
            f"👤 <b>{user_label(target_id)}</b>\n\n"
            f"🔗 Referallar: {rec.get('referrals', 0)}\n"
            f"📊 Jami so'rovlar: {rec.get('lifetime_requests', 0)}\n"
            f"🖼 Sticker: {counts.get('sticker', 0)}  😀 Emoji: {counts.get('emoji', 0)}\n"
            f"🎞 GIF: {counts.get('gif', 0)}  📦 Pack: {counts.get('pack', 0)}\n"
        )
        keyboard = {"inline_keyboard": [[{"text": "⬅️ Reyting", "callback_data": "menu_leaderboard"}]]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "check_force_join":
        missing = missing_force_channels(user_id)
        if missing:
            answer_callback_query(cq_id, "Hali ham barcha kanallarga a'zo emassiz.", show_alert=True)
            return
        answer_callback_query(cq_id, "✅ Rahmat! Endi botdan foydalanishingiz mumkin.", show_alert=True)
        safe_edit_or_send(chat_id, message_id, "✅ Barcha majburiy kanallarga a'zo bo'ldingiz.",
                           reply_markup=main_menu_keyboard(user_id))
        return

    if data.startswith("claim_bonus:"):
        target_chat_id = int(data.split(":", 1)[1])
        status = get_chat_member_status(target_chat_id, user_id)
        if status not in ("member", "administrator", "creator"):
            answer_callback_query(cq_id, "Hali bu kanalga a'zo emassiz.", show_alert=True)
            return
        if not claim_bonus_channel(user_id, target_chat_id):
            answer_callback_query(cq_id, "Bu bonusni allaqachon olgansiz.", show_alert=True)
            return
        answer_callback_query(cq_id, "🎉 +2 limit qo'shildi!", show_alert=True)
        text, keyboard = build_bonus_menu_text_and_keyboard(user_id)
        safe_edit_or_send(chat_id, message_id, text, reply_markup=keyboard)
        return

    if data.startswith("dl_pack:") or data.startswith("dl_single:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        if data.startswith("dl_pack:"):
            handle_pack_request(chat_id, pending["pack_name"], pending["requester_info"], user_id)
        else:
            handle_single_sticker_request_from_pending(chat_id, pending, user_id)
        return

    if data.startswith("dl_id_single:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        handle_id_single_request(chat_id, pending, user_id)
        return

    if data.startswith("dl_id_pack:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending or not pending.get("pack_name"):
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        token2 = store_pending_choice({
            "pack_name": pending["pack_name"], "requester_info": pending["requester_info"],
        })
        edit_message_text(
            chat_id, message_id, "ID'larni qanday shaklda olishni xohlaysiz?",
            reply_markup={"inline_keyboard": [
                [{"text": "📝 Matn qilib chatga jo'natish", "callback_data": f"idpack_text:{token2}"}],
                [{"text": "📄 Txt fayl qilib jo'natish", "callback_data": f"idpack_txt:{token2}"}],
            ]},
        )
        return

    if data.startswith("idpack_txt:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        handle_id_pack_request(chat_id, pending["pack_name"], pending["requester_info"], user_id, "txt")
        return

    if data.startswith("idpack_text:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        token3 = store_pending_choice({
            "pack_name": pending["pack_name"], "requester_info": pending["requester_info"],
        })
        edit_message_text(
            chat_id, message_id, "Qaysi ko'rinishda kelsin?",
            reply_markup={"inline_keyboard": [
                [{"text": "🖼 Avval stiker, keyin ID'si", "callback_data": f"idpack_seq:{token3}"}],
                [{"text": "📋 Matn - to'liq", "callback_data": f"idpack_full:{token3}"}],
            ]},
        )
        return

    if data.startswith("idpack_seq:") or data.startswith("idpack_full:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        mode = "seq" if data.startswith("idpack_seq:") else "full"
        handle_id_pack_request(chat_id, pending["pack_name"], pending["requester_info"], user_id, mode)
        return

    if data.startswith("dl_gif_webm:") or data.startswith("dl_gif_id:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. GIF'ni qaytadan yuboring.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        if data.startswith("dl_gif_webm:"):
            handle_gif_webm_request(chat_id, pending, user_id)
        else:
            handle_gif_id_request(chat_id, pending, user_id)
        return

    # ---- Quyidagilar faqat adminlar uchun ----
    if not is_admin(user_id):
        answer_callback_query(cq_id)
        return

    if data == "menu_admin_panel":
        answer_callback_query(cq_id)
        safe_edit_or_send(chat_id, message_id, "🛠 Boshqaruv paneli:", reply_markup=admin_panel_keyboard(user_id))
        return

    if data.startswith("panel_users:"):
        answer_callback_query(cq_id)
        page = int(data.split(":", 1)[1])
        with _state_lock:
            uids = list(STATE["known_users"])
        items = []
        for uid in uids:
            label = user_label(uid)
            items.append((uid, label))
        safe_edit_or_send(chat_id, message_id, f"👥 Foydalanuvchilar ({len(items)}):",
                           reply_markup=_paginate_keyboard(items, "user_detail", page))
        return

    if data.startswith("user_detail:"):
        answer_callback_query(cq_id)
        target_id = int(data.split(":", 1)[1])
        rec = get_user_record(target_id)
        counts = rec.get("type_counts", {}) or {}
        premium_label = "ha" if is_premium(target_id) else "yoq"
        text = (
            f"👤 <b>{user_label(target_id)}</b> (id:{target_id})\n\n"
            f"📊 Jami so'rovlar: {rec.get('lifetime_requests', 0)}\n"
            f"🖼 Sticker: {counts.get('sticker', 0)}\n"
            f"😀 Custom emoji: {counts.get('emoji', 0)}\n"
            f"🎞 GIF: {counts.get('gif', 0)}\n"
            f"📦 Pack: {counts.get('pack', 0)}\n\n"
            f"🔗 Referallar: {rec.get('referrals', 0)}\n"
            f"🎁 Bonus: {rec.get('bonus', 0)}\n"
            f"⭐ Premium: {premium_label}\n"
        )
        rows = [
            [
                {"text": f"🖼 Sticker ({counts.get('sticker', 0)})", "callback_data": f"user_history:{target_id}:sticker"},
                {"text": f"😀 Emoji ({counts.get('emoji', 0)})", "callback_data": f"user_history:{target_id}:emoji"},
            ],
            [
                {"text": f"🎞 GIF ({counts.get('gif', 0)})", "callback_data": f"user_history:{target_id}:gif"},
                {"text": f"📦 Pack ({counts.get('pack', 0)})", "callback_data": f"user_history:{target_id}:pack"},
            ],
        ]
        if user_id == SUPERADMIN_ID:
            rows.append([
                {"text": "➕ Limit berish", "callback_data": f"give_limit:{target_id}"},
                {"text": "💬 Unga yozish", "callback_data": f"dm_start:{target_id}"},
            ])
        else:
            rows.append([{"text": "💬 Unga yozish", "callback_data": f"dm_start:{target_id}"}])
        rows.append([{"text": "⬅️ Foydalanuvchilar", "callback_data": "panel_users:0"}])
        keyboard = {"inline_keyboard": rows}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data.startswith("user_history:"):
        answer_callback_query(cq_id)
        _, target_id_str, kind = data.split(":", 2)
        target_id = int(target_id_str)
        rec = get_user_record(target_id)
        history = [h for h in (rec.get("history") or []) if h.get("type") == kind]
        history = list(reversed(history))[:20]
        kind_labels = {"sticker": "🖼 Sticker", "emoji": "😀 Custom emoji", "gif": "🎞 GIF", "pack": "📦 Pack"}
        if not history:
            text = f"{kind_labels.get(kind, kind)} bo'yicha hali so'rov yo'q."
        else:
            lines = [f"{kind_labels.get(kind, kind)} tarixi ({user_label(target_id)}):\n"]
            for h in history:
                at = (h.get("at") or "")[:16].replace("T", " ")
                lines.append(f"• {h.get('detail') or '—'}  ({at})")
            text = "\n".join(lines)
        keyboard = {"inline_keyboard": [[{"text": "⬅️ Orqaga", "callback_data": f"user_detail:{target_id}"}]]}
        safe_edit_or_send(chat_id, message_id, text, reply_markup=keyboard)
        return

    if data.startswith("give_limit:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        target_id = int(data.split(":", 1)[1])
        set_pending_input(user_id, "give_limit_amount", {"target_id": target_id})
        safe_edit_or_send(chat_id, message_id, f"id:{target_id} uchun qo'shiladigan limit sonini yozing (masalan: 5):",
                           reply_markup=back_to_panel_keyboard())
        return

    if data.startswith("panel_groups:"):
        answer_callback_query(cq_id)
        page = int(data.split(":", 1)[1])
        with _state_lock:
            groups = dict(STATE.get("groups", {}))
        items = [(gid, info.get("title", gid)) for gid, info in groups.items()]
        safe_edit_or_send(chat_id, message_id, f"👨‍👩‍👧 Guruhlar ({len(items)}):",
                           reply_markup=_paginate_keyboard(items, "group_detail", page))
        return

    if data.startswith("group_detail:"):
        answer_callback_query(cq_id)
        gid = data.split(":", 1)[1]
        with _state_lock:
            info = dict(STATE.get("groups", {}).get(gid, {}))
        pretty = json.dumps(info, ensure_ascii=False, indent=2)
        text = f"👨‍👩‍👧 <b>Guruh {gid}</b>\n<pre>{pretty}</pre>"
        keyboard = {"inline_keyboard": [
            [{"text": "🔗 Meni taklif qil (invite link)", "callback_data": f"invite_me:{gid}"}],
            [{"text": "⬅️ Guruhlar", "callback_data": "panel_groups:0"}],
        ]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data.startswith("panel_channels:"):
        answer_callback_query(cq_id)
        page = int(data.split(":", 1)[1])
        with _state_lock:
            channels = dict(STATE.get("channels", {}))
        items = [(cid, info.get("title", cid)) for cid, info in channels.items()]
        safe_edit_or_send(chat_id, message_id, f"📢 Kanallar ({len(items)}):",
                           reply_markup=_paginate_keyboard(items, "channel_detail", page))
        return

    if data.startswith("channel_detail:"):
        answer_callback_query(cq_id)
        cid = data.split(":", 1)[1]
        with _state_lock:
            info = dict(STATE.get("channels", {}).get(cid, {}))
        pretty = json.dumps(info, ensure_ascii=False, indent=2)
        text = f"📢 <b>Kanal {cid}</b>\n<pre>{pretty}</pre>"
        keyboard = {"inline_keyboard": [
            [{"text": "🔗 Meni taklif qil (invite link)", "callback_data": f"invite_me:{cid}"}],
            [{"text": "⬅️ Kanallar", "callback_data": "panel_channels:0"}],
        ]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data.startswith("invite_me:"):
        answer_callback_query(cq_id)
        target_chat_id = int(data.split(":", 1)[1])
        link = _invite_link_for(target_chat_id)
        if link:
            send_message(
                user_id,
                "🔗 Eslatma: Telegram Bot API orqali botning sizni majburan a'zo qilishi "
                "imkonsiz (bu faqat MTProto user-klientda mavjud). Quyidagi havola orqali "
                f"o'zingiz qo'shilishingiz mumkin:\n{link}",
            )
        else:
            send_message(user_id, "Taklif havolasini yaratib bo'lmadi — bot shu chatda admin ekanini tekshiring.")
        return

    if data == "panel_leaderboard":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        top = get_referral_leaderboard(15)
        if not top:
            text = "🏆 Hali hech kim referal orqali taklif qilmagan."
            rows = []
        else:
            lines = ["🏆 <b>Referal reyting (to'liq)</b>\n"]
            rows = []
            for i, (uid, refs, lifetime) in enumerate(top, start=1):
                lines.append(f"{i}. {user_label(uid)} — {refs} referal, {lifetime} so'rov")
                rows.append([{"text": f"{i}. {user_label(uid)}", "callback_data": f"user_detail:{uid}"}])
            text = "\n".join(lines)
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})
        return

    if data == "panel_export":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        csv_bytes = build_users_csv()
        fname = f"users_export_{today_str()}.csv"
        send_document_bytes(chat_id, fname, csv_bytes, caption="📤 Foydalanuvchilar eksporti (CSV).")
        return

    if data == "panel_botadmin":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        with _state_lock:
            groups = dict(STATE.get("groups", {}))
            channels = dict(STATE.get("channels", {}))
        rows = []
        for gid, info in groups.items():
            if bot_is_group_admin(int(gid)):
                rows.append([{"text": f"👨‍👩‍👧 {info.get('title', gid)}", "callback_data": f"group_detail:{gid}"}])
        for cid, info in channels.items():
            if bot_is_group_admin(int(cid)):
                rows.append([{"text": f"📢 {info.get('title', cid)}", "callback_data": f"channel_detail:{cid}"}])
        text = f"🤖 Bot admin bo'lgan joylar ({len(rows)}):" if rows else "🤖 Bot hali hech qayerda admin emas."
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, text, reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("panel_dm_user"):
        answer_callback_query(cq_id)
        page = 0
        if ":" in data:
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
        with _state_lock:
            uids = list(STATE["known_users"])
        items = [(uid, user_label(uid)) for uid in uids]
        start = page * PAGE_SIZE
        chunk = items[start:start + PAGE_SIZE]
        rows = [[{"text": label, "callback_data": f"dm_start:{uid}"}] for uid, label in chunk]
        nav = []
        if page > 0:
            nav.append({"text": "⬅️", "callback_data": f"panel_dm_user:{page - 1}"})
        if start + PAGE_SIZE < len(items):
            nav.append({"text": "➡️", "callback_data": f"panel_dm_user:{page + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "⬅️ Admin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"💬 Kimga xabar yubormoqchisiz? ({len(items)} foydalanuvchi)",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("dm_start:"):
        answer_callback_query(cq_id)
        target_id = int(data.split(":", 1)[1])
        set_pending_input(user_id, "dm_text", {"target_id": target_id})
        safe_edit_or_send(chat_id, message_id, f"💬 {user_label(target_id)}ga yuboriladigan xabarni yozing:",
                           reply_markup=back_to_panel_keyboard())
        return

    if data == "panel_admin_message":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "admin_message")
        hint = ("Superadmin sifatida xabaringiz to'g'ridan-to'g'ri barcha adminlarga yuboriladi."
                if user_id == SUPERADMIN_ID else
                "Xabaringiz avval superadminga tasdiq uchun boradi, u ruxsat bersa boshqa adminlarga yuboriladi.")
        safe_edit_or_send(chat_id, message_id, f"✍️ Xabaringizni yozing.\n\n{hint}",
                           reply_markup=back_to_panel_keyboard())
        return

    if data.startswith("adminmsg_ok:") or data.startswith("adminmsg_no:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        approve = data.startswith("adminmsg_ok:")
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu so'rov muddati o'tgan yoki allaqachon ko'rib chiqilgan.")
            return
        if approve:
            with _state_lock:
                targets = [a for a in STATE["admins"] if a != pending["from_id"]]
            sent = 0
            for aid in targets:
                r = send_message(aid, f"✉️ <b>{pending['from_label']}</b> dan xabar:\n\n{pending['text']}",
                                  parse_mode_html=True)
                if r.get("ok"):
                    sent += 1
            edit_message_text(chat_id, message_id, f"✅ Tasdiqlandi. Xabar {sent} ta adminga yuborildi.")
            send_message(pending["from_id"], f"✅ Xabaringiz superadmin tomonidan tasdiqlandi va {sent} ta adminga yuborildi.")
        else:
            edit_message_text(chat_id, message_id, "❌ Rad etildi.")
            send_message(pending["from_id"], "❌ Xabaringiz superadmin tomonidan rad etildi.")
        return

    if data == "panel_limits":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        cfg = get_limit_config()
        text = (
            f"⚙️ <b>Limit sozlamalari</b>\n\n"
            f"Bazaviy haftalik: {cfg['base_weekly']}\n"
            f"Kunlikka o'tish chegarasi: {cfg['weekly_cap']}\n"
            f"Bepul kalit so'z limiti: {cfg.get('keyword_free_limit', 2)}\n"
        )
        keyboard = {"inline_keyboard": [
            [
                {"text": "Bazaviy −1", "callback_data": "limit_base_dec"},
                {"text": "Bazaviy +1", "callback_data": "limit_base_inc"},
            ],
            [
                {"text": "Chegara −1", "callback_data": "limit_cap_dec"},
                {"text": "Chegara +1", "callback_data": "limit_cap_inc"},
            ],
            [
                {"text": "Kalit −1", "callback_data": "limit_kw_dec"},
                {"text": "Kalit +1", "callback_data": "limit_kw_inc"},
            ],
            [{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}],
        ]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data in ("limit_kw_dec", "limit_kw_inc"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        cfg = get_limit_config()
        current = cfg.get("keyword_free_limit", 2)
        new_val = max(0, current - 1) if data == "limit_kw_dec" else current + 1
        with _state_lock:
            STATE.setdefault("config", {})["keyword_free_limit"] = new_val
            save_state_locked()
        cfg = get_limit_config()
        text = (
            f"⚙️ <b>Limit sozlamalari</b>\n\n"
            f"Bazaviy haftalik: {cfg['base_weekly']}\n"
            f"Kunlikka o'tish chegarasi: {cfg['weekly_cap']}\n"
            f"Bepul kalit so'z limiti: {cfg.get('keyword_free_limit', 2)}\n"
        )
        keyboard = {"inline_keyboard": [
            [
                {"text": "Bazaviy −1", "callback_data": "limit_base_dec"},
                {"text": "Bazaviy +1", "callback_data": "limit_base_inc"},
            ],
            [
                {"text": "Chegara −1", "callback_data": "limit_cap_dec"},
                {"text": "Chegara +1", "callback_data": "limit_cap_inc"},
            ],
            [
                {"text": "Kalit −1", "callback_data": "limit_kw_dec"},
                {"text": "Kalit +1", "callback_data": "limit_kw_inc"},
            ],
            [{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}],
        ]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data in ("limit_base_dec", "limit_base_inc", "limit_cap_dec", "limit_cap_inc"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        cfg = get_limit_config()
        if data == "limit_base_dec":
            set_base_weekly(max(1, cfg["base_weekly"] - 1))
        elif data == "limit_base_inc":
            set_base_weekly(cfg["base_weekly"] + 1)
        elif data == "limit_cap_dec":
            set_weekly_cap(max(1, cfg["weekly_cap"] - 1))
        elif data == "limit_cap_inc":
            set_weekly_cap(cfg["weekly_cap"] + 1)
        cfg = get_limit_config()
        text = (
            f"⚙️ <b>Limit sozlamalari</b>\n\n"
            f"Bazaviy haftalik: {cfg['base_weekly']}\n"
            f"Kunlikka o'tish chegarasi: {cfg['weekly_cap']}\n"
            f"Bepul kalit so'z limiti: {cfg.get('keyword_free_limit', 2)}\n"
        )
        keyboard = {"inline_keyboard": [
            [
                {"text": "Bazaviy −1", "callback_data": "limit_base_dec"},
                {"text": "Bazaviy +1", "callback_data": "limit_base_inc"},
            ],
            [
                {"text": "Chegara −1", "callback_data": "limit_cap_dec"},
                {"text": "Chegara +1", "callback_data": "limit_cap_inc"},
            ],
            [
                {"text": "Kalit −1", "callback_data": "limit_kw_dec"},
                {"text": "Kalit +1", "callback_data": "limit_kw_inc"},
            ],
            [{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}],
        ]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "panel_admins":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        with _state_lock:
            admins = list(STATE["admins"])
        rows = [[{"text": f"❌ id:{a}", "callback_data": f"remove_admin:{a}"}] for a in admins]
        rows.append([{"text": "➕ Admin qo'shish", "callback_data": "add_admin_start"}])
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"🛡 Adminlar ({len(admins)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("remove_admin:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        target_id = int(data.split(":", 1)[1])
        remove_admin(target_id)
        with _state_lock:
            admins = list(STATE["admins"])
        rows = [[{"text": f"❌ id:{a}", "callback_data": f"remove_admin:{a}"}] for a in admins]
        rows.append([{"text": "➕ Admin qo'shish", "callback_data": "add_admin_start"}])
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"🛡 Adminlar ({len(admins)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data == "add_admin_start":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "add_admin")
        safe_edit_or_send(chat_id, message_id, "Yangi admin qilinadigan foydalanuvchi ID raqamini yuboring:",
                           reply_markup=back_to_panel_keyboard())
        return

    if data == "panel_forcechannels":
        answer_callback_query(cq_id)
        channels = get_force_channels()
        if user_id == SUPERADMIN_ID:
            rows = [[{"text": f"❌ {c['title']}", "callback_data": f"remove_force:{c['chat_id']}"}] for c in channels]
            rows.append([{"text": "➕ Kanal qo'shish", "callback_data": "add_force_start"}])
        else:
            rows = [[{"text": c["title"], "callback_data": "noop"}] for c in channels]
        rows.append([{"text": "⬅️ Panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"🔒 Majburiy kanallar ({len(channels)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("remove_force:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        target_id = int(data.split(":", 1)[1])
        remove_force_channel(target_id)
        channels = get_force_channels()
        rows = [[{"text": f"❌ {c['title']}", "callback_data": f"remove_force:{c['chat_id']}"}] for c in channels]
        rows.append([{"text": "➕ Kanal qo'shish", "callback_data": "add_force_start"}])
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"🔒 Majburiy kanallar ({len(channels)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data == "add_force_start":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "add_force_channel")
        safe_edit_or_send(
            chat_id, message_id,
            "Majburiy kanal/guruh @username yoki chat_id'sini yuboring.\n\n"
            "⚠️ Bot o'sha kanal/guruhda ADMIN bo'lishi shart (a'zolarni tekshirish "
            "va taklif havolasi yaratish uchun).",
            reply_markup=back_to_panel_keyboard(),
        )
        return

    if data == "panel_bonuschannels":
        answer_callback_query(cq_id)
        channels = get_bonus_channels()
        if user_id == SUPERADMIN_ID:
            rows = [[{"text": f"❌ {c['title']}", "callback_data": f"remove_bonus:{c['chat_id']}"}] for c in channels]
            rows.append([{"text": "➕ Kanal qo'shish", "callback_data": "add_bonus_start"}])
        else:
            rows = [[{"text": c["title"], "callback_data": "noop"}] for c in channels]
        rows.append([{"text": "⬅️ Panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"🎁 Bonus kanallar ({len(channels)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("remove_bonus:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        target_id = int(data.split(":", 1)[1])
        remove_bonus_channel(target_id)
        channels = get_bonus_channels()
        rows = [[{"text": f"❌ {c['title']}", "callback_data": f"remove_bonus:{c['chat_id']}"}] for c in channels]
        rows.append([{"text": "➕ Kanal qo'shish", "callback_data": "add_bonus_start"}])
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"🎁 Bonus kanallar ({len(channels)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data == "add_bonus_start":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "add_bonus_channel")
        safe_edit_or_send(
            chat_id, message_id,
            "Bonus kanal/guruh @username yoki chat_id'sini yuboring.\n\n"
            "⚠️ Bot o'sha kanal/guruhda ADMIN bo'lishi shart.",
            reply_markup=back_to_panel_keyboard(),
        )
        return

    if data == "panel_reaction":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        rows = [[{"text": label, "callback_data": f"panel_reaction_kind:{kind}"}]
                for kind, (_, label) in REACTION_KIND_CONFIG.items()]
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, "⚡ Qaysi reaksiyani sozlaysiz?",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("panel_reaction_kind:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        kind = data.split(":", 1)[1]
        current_type, current_value = get_reaction_config_for(kind)
        _, label = REACTION_KIND_CONFIG[kind]
        current_text = f"✨ premium (ID: {current_value})" if current_type == "custom_emoji" else current_value
        rows = [[{"text": (f"✅ {e}" if current_type == "emoji" and e == current_value else e),
                  "callback_data": f"set_reaction:{kind}:{e}"}]
                for e in REACTION_EMOJI_CHOICES]
        rows.append([{"text": "✨ Premium emoji (ID orqali)", "callback_data": f"set_reaction_custom:{kind}"}])
        rows.append([{"text": "⬅️ Orqaga", "callback_data": "panel_reaction"}])
        safe_edit_or_send(chat_id, message_id, f"{label} (hozirgi: {current_text}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("set_reaction_custom:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        kind = data.split(":", 1)[1]
        set_pending_input(user_id, "reaction_custom", {"kind": kind})
        safe_edit_or_send(
            chat_id, message_id,
            "Premium emojini yuboring (o'sha emojining o'zini yozib/tashlab) "
            "yoki uning ID raqamini yozing.\n\n"
            "⚠️ Eslatma: guruhda custom emoji reaksiyaga ruxsat berilgan bo'lishi kerak "
            "(guruh sozlamalari → Reactions), aks holda Telegram rad etadi va oddiy emojiga tushib qoladi.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "panel_signature":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        sig_id = get_signature_emoji()
        status = f"yoqilgan ✅ (ID: {sig_id})" if sig_id else "o'chirilgan"
        text = (
            "✨ <b>Bot imzosi</b>\n\n"
            "Yoqilsa, bot yuboradigan (deyarli) har bir oddiy xabar oxiriga shu premium "
            "emoji avtomatik qo'shiladi — botga bir xil, tanib bo'ladigan uslub beradi.\n\n"
            "Eslatma: bu SENING akkountingda Telegram Premium borligiga bog'liq (bot sen "
            "yaratgan bot bo'lgani uchun); bo'lmasa Telegram rad etadi va imzosiz ketaveradi.\n\n"
            f"Holati: {status}"
        )
        rows = [[{"text": "✨ O'rnatish/almashtirish", "callback_data": "set_signature_start"}]]
        if sig_id:
            rows.append([{"text": "🚫 O'chirish", "callback_data": "clear_signature"}])
        rows.append([{"text": "⬅️ Boshqaruv paneli", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})
        return

    if data == "set_signature_start":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "signature_custom")
        safe_edit_or_send(
            chat_id, message_id,
            "Imzo qilib qo'yiladigan premium emojini yuboring (uning o'zini) yoki ID raqamini yozing.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "clear_signature":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        clear_signature_emoji()
        safe_edit_or_send(chat_id, message_id, "✅ Bot imzosi o'chirildi.", reply_markup=back_to_menu_keyboard())
        return

    if data.startswith("set_reaction:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        _, kind, emoji = data.split(":", 2)
        set_reaction_emoji_for(kind, emoji)
        _, label = REACTION_KIND_CONFIG[kind]
        rows = [[{"text": (f"✅ {e}" if e == emoji else e), "callback_data": f"set_reaction:{kind}:{e}"}]
                for e in REACTION_EMOJI_CHOICES]
        rows.append([{"text": "⬅️ Orqaga", "callback_data": "panel_reaction"}])
        safe_edit_or_send(chat_id, message_id, f"✅ {label} o'rnatildi: {emoji}",
                           reply_markup={"inline_keyboard": rows})
        return

    if data == "panel_broadcast":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "broadcast")
        safe_edit_or_send(chat_id, message_id, "📣 Barchaga yuboriladigan xabar matnini yozing:",
                           reply_markup=back_to_panel_keyboard())
        return

    answer_callback_query(cq_id)


# ---------- Superadmin/admin panelining matnli (pending_input) javoblari ----------

def _extract_custom_emoji_id(text, entities):
    """Xabarda haqiqiy custom emoji bo'lsa uning ID'sini oladi, bo'lmasa qo'lda
    yozilgan raqamli ID'ni qabul qiladi."""
    for ent in (entities or []):
        if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
            return ent["custom_emoji_id"]
    raw = (text or "").strip()
    return raw if raw.isdigit() else None


def handle_pending_input(chat_id, user_id, text, entities=None):
    """True qaytarsa — xabar shu yerda to'liq qayta ishlangan (webhook to'xtaydi)."""
    pending = get_pending_input(user_id)
    if not pending:
        return False

    action = pending["action"]

    if action == "getpack":
        clear_pending_input(user_id)
        if not enforce_force_join(chat_id, user_id):
            return True
        pack_name = extract_pack_name_from_link(text) or text.strip()
        handle_pack_request(chat_id, pack_name, requester_label({"id": user_id}), user_id)
        return True

    if action == "kw_trigger":
        clear_pending_input(user_id)
        trigger = text.strip()
        if not trigger:
            send_message(chat_id, "Bo'sh bo'lishi mumkin emas. Bekor qilindi.", reply_markup=back_to_menu_keyboard())
            return True
        set_pending_input(user_id, "kw_response", {"trigger": trigger, "type": "exact"})
        send_message(chat_id, f"«{trigger}» kelganda qanday javob yozilsin?", reply_markup=back_to_menu_keyboard())
        return True

    if action == "kw_response":
        clear_pending_input(user_id)
        response = text.strip()
        if not response:
            send_message(chat_id, "Bo'sh bo'lishi mumkin emas. Bekor qilindi.", reply_markup=back_to_menu_keyboard())
            return True
        data = pending.get("data") or {}
        ok, result = add_keyword(user_id, data.get("trigger", "*"), data.get("type", "exact"), response, entities)
        if ok:
            note = " (premium emoji/formatlash saqlandi ✨)" if entities else ""
            send_message(chat_id, f"✅ Kalit qo'shildi.{note}", reply_markup=back_to_menu_keyboard())
        else:
            send_message(chat_id, f"❌ {result}", reply_markup=back_to_menu_keyboard())
        return True

    if action == "reaction_custom":
        clear_pending_input(user_id)
        data = pending.get("data") or {}
        kind = data.get("kind")
        custom_emoji_id = _extract_custom_emoji_id(text, entities)
        if not custom_emoji_id or kind not in REACTION_KIND_CONFIG:
            send_message(chat_id, "ID topilmadi. Premium emojining o'zini yuboring yoki uning raqamli ID'sini yozing.",
                         reply_markup=back_to_menu_keyboard())
            return True
        set_reaction_custom_emoji_for(kind, custom_emoji_id)
        _, label = REACTION_KIND_CONFIG[kind]
        send_message(chat_id, f"✅ {label} endi premium emoji: ID {custom_emoji_id}",
                     reply_markup=back_to_menu_keyboard())
        return True

    if action == "signature_custom":
        clear_pending_input(user_id)
        custom_emoji_id = _extract_custom_emoji_id(text, entities)
        if not custom_emoji_id:
            send_message(chat_id, "ID topilmadi. Premium emojining o'zini yuboring yoki uning raqamli ID'sini yozing.",
                         reply_markup=back_to_menu_keyboard())
            return True
        placeholder = None
        for ent in (entities or []):
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id") == custom_emoji_id:
                # UTF-16 offset/length'dan asl placeholder belgisini ajratib olamiz
                raw = text.encode("utf-16-le")
                start, end = ent["offset"] * 2, (ent["offset"] + ent["length"]) * 2
                placeholder = raw[start:end].decode("utf-16-le")
                break
        set_signature_emoji(custom_emoji_id, placeholder or "✨")
        send_message(chat_id, f"✅ Bot imzosi o'rnatildi (ID: {custom_emoji_id}). "
                              f"Endi shu bilan yuboraman:", reply_markup=back_to_menu_keyboard())
        return True

    if action == "away_delay":
        clear_pending_input(user_id)
        raw = text.strip()
        try:
            seconds = int(raw)
        except ValueError:
            send_message(chat_id, "Butun son kiriting (soniya), masalan: 60", reply_markup=back_to_menu_keyboard())
            return True
        if seconds < 0:
            send_message(chat_id, "0 yoki musbat son bo'lishi kerak.", reply_markup=back_to_menu_keyboard())
            return True
        set_away_delay(user_id, seconds)
        if seconds == 0:
            send_message(chat_id, "✅ Kechikish o'chirildi — javoblar darhol ketadi.", reply_markup=back_to_menu_keyboard())
        else:
            send_message(chat_id, f"✅ Endi siz {seconds} soniya ichida o'zingiz javob yozmasangiz, "
                                   f"avto-javob ishga tushadi.", reply_markup=back_to_menu_keyboard())
        return True

    # Quyidagilar faqat adminlar uchun ishlaydi:
    if not is_admin(user_id):
        clear_pending_input(user_id)
        return True

    if action == "dm_text":
        clear_pending_input(user_id)
        target_id = pending["data"]["target_id"]
        msg_text = text
        result = send_message(target_id, msg_text)
        if result.get("ok"):
            send_message(chat_id, "✅ Yuborildi.", reply_markup=back_to_panel_keyboard())
        else:
            send_message(chat_id, "❌ Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).",
                         reply_markup=back_to_panel_keyboard())
        return True

    if action == "admin_message":
        clear_pending_input(user_id)
        msg_text = text.strip()
        if not msg_text:
            send_message(chat_id, "Bo'sh xabar yuborib bo'lmaydi. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        sender_label = user_label(user_id)
        if user_id == SUPERADMIN_ID:
            with _state_lock:
                targets = list(STATE["admins"])
            sent = 0
            for aid in targets:
                r = send_message(aid, f"✉️ <b>Superadmindan xabar:</b>\n\n{msg_text}", parse_mode_html=True)
                if r.get("ok"):
                    sent += 1
            send_message(chat_id, f"✅ Xabaringiz {sent} ta adminga yuborildi.", reply_markup=back_to_panel_keyboard())
        else:
            token = store_pending_choice({"from_id": user_id, "from_label": sender_label, "text": msg_text})
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Ruxsat berish", "callback_data": f"adminmsg_ok:{token}"},
                {"text": "❌ Rad etish", "callback_data": f"adminmsg_no:{token}"},
            ]]}
            send_message(SUPERADMIN_ID, f"✉️ <b>{sender_label}</b> boshqa adminlarga xabar yubormoqchi:\n\n{msg_text}",
                         parse_mode_html=True, reply_markup=keyboard)
            send_message(chat_id, "📨 Xabaringiz superadminga tasdiq uchun yuborildi.", reply_markup=back_to_panel_keyboard())
        return True

    if action == "give_limit_amount":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        target_id = pending["data"]["target_id"]
        try:
            amount = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        mode, new_limit = add_bonus_to_user(target_id, amount)
        period = "kunlik" if mode == "daily" else "haftalik"
        send_message(chat_id, f"✅ id:{target_id} uchun bonus limit +{amount} qo'shildi. Yangi {period} limit: {new_limit}",
                     reply_markup=back_to_panel_keyboard())
        return True

    if action == "add_admin":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        try:
            target_id = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun ID kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        add_admin(target_id)
        send_message(chat_id, f"✅ id:{target_id} endi bot admini.", reply_markup=back_to_panel_keyboard())
        return True

    if action == "add_force_channel":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        ch = _resolve_chat(text)
        if not ch:
            send_message(chat_id, "Kanal topilmadi. Bot shu kanalda a'zo/admin ekanini tekshiring.",
                         reply_markup=back_to_panel_keyboard())
            return True
        add_force_channel(ch)
        send_message(chat_id, f"✅ Majburiy kanal qo'shildi: {ch['title']}", reply_markup=back_to_panel_keyboard())
        return True

    if action == "add_bonus_channel":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        ch = _resolve_chat(text)
        if not ch:
            send_message(chat_id, "Kanal topilmadi. Bot shu kanalda a'zo/admin ekanini tekshiring.",
                         reply_markup=back_to_panel_keyboard())
            return True
        add_bonus_channel(ch)
        send_message(chat_id, f"✅ Bonus kanal qo'shildi: {ch['title']}", reply_markup=back_to_panel_keyboard())
        return True

    if action == "broadcast":
        clear_pending_input(user_id)
        with _state_lock:
            uids = list(STATE["known_users"])
        sent = 0
        for uid in uids:
            r = send_message(uid, text)
            if r.get("ok"):
                sent += 1
        send_message(chat_id, f"📣 Xabar {sent} ta foydalanuvchiga yuborildi.", reply_markup=back_to_panel_keyboard())
        return True

    clear_pending_input(user_id)
    return False


# ---------- Telegram Business xabarlari ----------
# Bu funksiya, superadmin/admin o'z shaxsiy profilini (Telegram Business orqali)
# botga ulab qo'yganda, o'sha profilga yozgan boshqa odamlarning xabarlarini
# qayta ishlaydi. Javoblar business_connection_id bilan yuboriladi, aks holda
# xabar noto'g'ri joyga (botning o'z chatiga) ketib qoladi.
# Eslatma: bu yerda majburiy-a'zolik (force-join) tekshiruvi ishlatilmaydi —
# faqat haftalik/kunlik so'rov limiti amal qiladi.

def get_business_owner(connection_id):
    with _state_lock:
        conn = STATE.get("business_connections", {}).get(connection_id)
        return conn.get("owner_id") if conn else None


def send_auto_reply(chat_id, text, entities, business_connection_id):
    """Kalit so'z javobini yuboradi. entities bo'lsa (masalan premium emoji) shu bilan
    urinadi; Telegram rad etsa (masalan bot egasida Telegram Premium bo'lmasa), oddiy
    matn sifatida qayta yuboradi — javob baribir yetib boradi."""
    result = send_message(chat_id, text, entities=entities, business_connection_id=business_connection_id)
    if entities and not (result and result.get("ok")):
        result = send_message(chat_id, text, business_connection_id=business_connection_id)
    return result


def _delayed_auto_reply(owner_id, chat_id, text, entities, business_connection_id, trigger_ts, delay):
    time.sleep(delay)
    if owner_replied_since(owner_id, chat_id, trigger_ts):
        return  # egasi shu orada o'zi javob berib ulgurdi — avto-javob shart emas
    send_auto_reply(chat_id, text, entities, business_connection_id)


def handle_business_message(msg, business_connection_id):
    chat_id = msg["chat"]["id"]
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    requester_info = requester_label(from_user)
    text = msg.get("text", "") or ""
    stripped = text.strip()
    reply = msg.get("reply_to_message")

    register_known_user(user_id, from_user)

    owner_id = get_business_owner(business_connection_id)
    is_owner = owner_id is not None and user_id == owner_id
    can_run_commands = is_owner or is_admin(user_id)

    if is_owner:
        # Egasi shu chatga o'zi yozdi (buyruq bo'lsa ham, oddiy javob bo'lsa ham) —
        # kutilayotgan kechiktirilgan avto-javob(lar) shundan keyin o'zini bekor qiladi.
        mark_owner_activity(owner_id, chat_id)

    # ---- .tgs <pack> <raqam> — faqat profil egasi (yoki bot admini) uchun ----
    if stripped.startswith(".tgs "):
        if not can_run_commands:
            return
        parts = stripped.split()
        if len(parts) < 3:
            send_message(chat_id, "Format: .tgs <pack_manzili_yoki_nomi> <tartib_raqami>",
                         business_connection_id=business_connection_id)
            return
        pack_name = resolve_pack_name_from_text(parts[1])
        try:
            index = int(parts[2])
        except ValueError:
            send_message(chat_id, "Tartib raqami butun son bo'lishi kerak.",
                         business_connection_id=business_connection_id)
            return
        if not pack_name:
            send_message(chat_id, "Pack manzilini/nomini aniqlab bo'lmadi.",
                         business_connection_id=business_connection_id)
            return
        handle_tgs_by_index(chat_id, requester_info, user_id, pack_name, index,
                            business_connection_id=business_connection_id)
        return

    # ---- .zip / .zipstiker / .zipgif — reply qilingan faylni kod (ZIP) qilib beradi ----
    if stripped in (".zip", ".zipstiker") and can_run_commands:
        if not reply:
            send_message(chat_id, "Sticker/custom emoji xabariga reply qilib yozing.",
                         business_connection_id=business_connection_id)
            return
        if stripped == ".zip":
            pack_name = extract_pack_name_from_message(reply)
            if pack_name:
                handle_pack_request(chat_id, pack_name, requester_info, user_id,
                                     business_connection_id=business_connection_id)
                return
        handle_single_sticker_request(chat_id, reply, requester_info, user_id,
                                       business_connection_id=business_connection_id)
        return

    if stripped == ".zipgif" and can_run_commands:
        if not reply:
            send_message(chat_id, "GIF xabariga reply qilib .zipgif yozing.",
                         business_connection_id=business_connection_id)
            return
        handle_animation_request(chat_id, reply, requester_info, user_id,
                                  business_connection_id=business_connection_id)
        return

    # ---- Boshqa hamma narsa: faqat kalit so'z (avto-javob) tizimi ishlaydi ----
    # Stiker/GIF/pack-havola avtomatik qayta ishlanmaydi — buyruq berilmagunicha bot jim turadi.
    if owner_id and not is_owner:
        reply_text, reply_entities = find_keyword_response(owner_id, text)
        if reply_text:
            delay = get_away_delay(owner_id)
            if delay > 0:
                threading.Thread(
                    target=_delayed_auto_reply,
                    args=(owner_id, chat_id, reply_text, reply_entities, business_connection_id, time.time(), delay),
                    daemon=True,
                ).start()
            else:
                send_auto_reply(chat_id, reply_text, reply_entities, business_connection_id)


# ---------- Guruh ".zip" / ".zipstiker" (moderatsion, admin-only) ----------

def handle_group_dot_commands(msg, chat_id, user_id, text):
    reply = msg.get("reply_to_message")
    stripped = text.strip()

    if stripped == ".zipstiker":
        if not is_admin(user_id):
            return True
        if not reply:
            send_message(chat_id, "Sticker/custom emoji xabariga reply qilib .zipstiker yozing.")
            return True
        handle_single_sticker_request(chat_id, reply, requester_label(msg.get("from", {})), user_id, reply_to=msg["message_id"])
        return True

    if stripped == ".zipgif":
        if not is_admin(user_id):
            return True
        if not reply:
            send_message(chat_id, "GIF xabariga reply qilib .zipgif yozing.")
            return True
        handle_animation_request(chat_id, reply, requester_label(msg.get("from", {})), user_id, reply_to=msg["message_id"])
        return True

    if stripped.startswith(".tgs "):
        if not is_admin(user_id):
            return True
        parts = stripped.split()
        if len(parts) < 3:
            send_message(chat_id, "Format: .tgs <pack_manzili_yoki_nomi> <tartib_raqami>")
            return True
        pack_name = resolve_pack_name_from_text(parts[1])
        try:
            index = int(parts[2])
        except ValueError:
            send_message(chat_id, "Tartib raqami butun son bo'lishi kerak.")
            return True
        if not pack_name:
            send_message(chat_id, "Pack manzilini/nomini aniqlab bo'lmadi.")
            return True
        handle_tgs_by_index(chat_id, requester_label(msg.get("from", {})), user_id, pack_name, index,
                             reply_to=msg["message_id"])
        return True

    if stripped == ".zip":
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

    if stripped == ".addadmin":
        if user_id != SUPERADMIN_ID or not reply:
            return True
        add_admin(reply["from"]["id"])
        send_message(chat_id, f"✅ {requester_label(reply['from'])} endi bot admini.")
        return True

    if stripped == ".deladmin":
        if user_id != SUPERADMIN_ID or not reply:
            return True
        remove_admin(reply["from"]["id"])
        send_message(chat_id, f"❌ {requester_label(reply['from'])} bot adminligidan olindi.")
        return True

    if stripped == ".del":
        if user_id != SUPERADMIN_ID or not reply:
            return True
        delete_message(chat_id, reply["message_id"])
        return True

    return False


# ---------- Webhook endpoint ----------

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global STATE
    update = request.get_json(force=True)

    callback_query = update.get("callback_query")
    if callback_query:
        handle_callback_query(callback_query)
        return {"ok": True}

    # ---- Bot biror guruh/kanalga qo'shildi/chiqarildi ----
    my_chat_member = update.get("my_chat_member")
    if my_chat_member:
        chat = my_chat_member["chat"]
        new_status = my_chat_member["new_chat_member"]["status"]
        if chat.get("type") == "channel":
            if new_status in ("administrator", "member"):
                register_channel(chat)
            elif new_status in ("left", "kicked"):
                forget_channel(chat["id"])
        elif chat.get("type") in ("group", "supergroup"):
            if new_status in ("administrator", "member"):
                register_group(chat)
            elif new_status in ("left", "kicked"):
                forget_group(chat["id"])
        return {"ok": True}

    # ---- Telegram Business ulanishi: egasini eslab qolamiz (kalit so'zlar shu userga tegishli bo'ladi) ----
    business_connection = update.get("business_connection")
    if business_connection:
        conn_id = business_connection.get("id")
        owner = business_connection.get("user", {})
        with _state_lock:
            STATE.setdefault("business_connections", {})[conn_id] = {
                "owner_id": owner.get("id"),
                "enabled": business_connection.get("is_enabled", True),
            }
            save_state_locked()
        return {"ok": True}

    business_message = update.get("business_message")
    if business_message:
        handle_business_message(business_message, business_message.get("business_connection_id"))
        return {"ok": True}

    channel_post = update.get("channel_post")
    if channel_post:
        chat_id = channel_post["chat"]["id"]
        register_channel(channel_post["chat"])
        if bot_is_group_admin(chat_id):
            react_with_kind(chat_id, channel_post["message_id"], "channel")
        return {"ok": True}

    pre_checkout_query = update.get("pre_checkout_query")
    if pre_checkout_query:
        tg_call("answerPreCheckoutQuery", pre_checkout_query_id=pre_checkout_query["id"], ok=True)
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    successful_payment = msg.get("successful_payment")
    if successful_payment:
        payer_id = msg["from"]["id"]
        until_ts = grant_premium(payer_id, days=182)
        until_str = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        send_message(msg["chat"]["id"], f"🎉 Premium faollashtirildi! {until_str} sanagacha cheksiz foydalanasiz.")
        notify_admin(f"⭐ Yangi premium xarid: id:{payer_id}, {until_str} gacha")
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"]["type"]
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    requester_info = requester_label(from_user)
    text = msg.get("text", "") or ""

    is_group = chat_type in ("group", "supergroup")

    if is_group:
        register_group(msg["chat"])
        if is_admin(user_id):
            reaction_kind = "superadmin" if user_id == SUPERADMIN_ID else "admin"
            react_with_kind(chat_id, msg["message_id"], reaction_kind)
        if text.strip().startswith("."):
            if handle_group_dot_commands(msg, chat_id, user_id, text):
                return {"ok": True}
        reply = msg.get("reply_to_message")
        if reply and reply.get("from", {}).get("id") and is_admin(reply["from"]["id"]) and not is_admin(user_id):
            reply_text, reply_entities = find_keyword_response(reply["from"]["id"], text)
            if reply_text:
                send_message(chat_id, reply_text, reply_to=msg["message_id"], entities=reply_entities)
        return {"ok": True}

    # ================= Shaxsiy chat (private) =================

    register_known_user(user_id, from_user)

    # Superadmin panelidan kutilayotgan matn kiritish bo'lsa, avval shuni tekshiramiz:
    if handle_pending_input(chat_id, user_id, text, msg.get("entities")):
        return {"ok": True}

    if text.strip().startswith(".tgs "):
        if not is_admin(user_id):
            send_message(chat_id, "Bu buyruq faqat adminlar uchun.")
            return {"ok": True}
        parts = text.strip().split()
        if len(parts) < 3:
            send_message(chat_id, "Format: .tgs <pack_manzili_yoki_nomi> <tartib_raqami>")
            return {"ok": True}
        pack_name = resolve_pack_name_from_text(parts[1])
        try:
            index = int(parts[2])
        except ValueError:
            send_message(chat_id, "Tartib raqami butun son bo'lishi kerak.")
            return {"ok": True}
        if not pack_name:
            send_message(chat_id, "Pack manzilini/nomini aniqlab bo'lmadi.")
            return {"ok": True}
        handle_tgs_by_index(chat_id, requester_info, user_id, pack_name, index)
        return {"ok": True}

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1][4:])
                register_referral(user_id, referrer_id)
            except ValueError:
                pass
        greeting = (
            "Salom! Menga sticker/custom emoji yoki GIF forward qiling, yoki pastdagi "
            "\"📦 Pack yuklab olish\" tugmasi orqali pack nomini yuboring."
        )
        if user_id == SUPERADMIN_ID:
            greeting += "\n\n👑 Superadmin sifatida quyida boshqaruv paneliga ham kirishingiz mumkin."
        send_message(chat_id, greeting, reply_markup=main_menu_keyboard(user_id))
        return {"ok": True}

    # ---- Bitta sticker/custom emoji forward qilindi: tanlov beramiz ----
    file_id, ext, emoji_char, sticker_kind, custom_emoji_id = extract_single_sticker_file(msg)
    if file_id:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        pack_name = extract_pack_name_from_message(msg)
        token = store_pending_choice({
            "pack_name": pack_name, "file_id": file_id, "ext": ext,
            "emoji_char": emoji_char, "requester_info": requester_info, "kind": sticker_kind,
            "raw_message": msg, "update_id": update.get("update_id"), "custom_emoji_id": custom_emoji_id,
        })
        keyboard_rows = [[{"text": "💾 Faqat shu stiker/emojini ZIP qilish", "callback_data": f"dl_single:{token}"}]]
        if pack_name:
            keyboard_rows.append([{"text": "📦 Butun pack'ni ZIP qilib olish", "callback_data": f"dl_pack:{token}"}])
        keyboard_rows.append([{"text": "🆔 Shu stiker/emojining ID'sini berish", "callback_data": f"dl_id_single:{token}"}])
        if pack_name:
            keyboard_rows.append([{"text": "🆔 Butun pack ID'larini berish", "callback_data": f"dl_id_pack:{token}"}])
        send_message(chat_id, "Nima qilishimni xohlaysiz?", reply_markup={"inline_keyboard": keyboard_rows})
        return {"ok": True}

    # ---- GIF (animation) yuborildi: tanlov beramiz (webm yoki ID) ----
    animation_file_id, _ = extract_animation_file(msg)
    if animation_file_id:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        token = store_pending_choice({
            "file_id": animation_file_id, "requester_info": requester_info,
            "raw_message": msg, "update_id": update.get("update_id"),
        })
        keyboard_rows = [
            [{"text": "🎞 WebM qilib berish", "callback_data": f"dl_gif_webm:{token}"}],
            [{"text": "🆔 ID sini berish", "callback_data": f"dl_gif_id:{token}"}],
        ]
        send_message(chat_id, "GIF bilan nima qilishimni xohlaysiz?", reply_markup={"inline_keyboard": keyboard_rows})
        return {"ok": True}

    pack_name = extract_pack_name_from_message(msg)
    if pack_name:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        token = store_pending_choice({"pack_name": pack_name, "requester_info": requester_info})
        keyboard_rows = [
            [{"text": "📦 Butun pack'ni ZIP qilib olish", "callback_data": f"dl_pack:{token}"}],
            [{"text": "🆔 Butun pack ID'larini berish", "callback_data": f"dl_id_pack:{token}"}],
        ]
        send_message(chat_id, "Nima qilishimni xohlaysiz?", reply_markup={"inline_keyboard": keyboard_rows})
        return {"ok": True}

    send_message(chat_id, "Sticker/emoji/GIF forward qiling yoki pastdagi menyudan foydalaning 👇",
                 reply_markup=main_menu_keyboard(user_id))
    return {"ok": True}


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


def set_webhook():
    url = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
    result = tg_call(
        "setWebhook", url=url,
        allowed_updates=["message", "callback_query", "channel_post", "my_chat_member",
                         "pre_checkout_query", "business_connection", "business_message",
                         "edited_business_message", "deleted_business_messages"],
    )
    log.info("Webhook o'rnatildi: %s -> %s", url, result)


def init_bot_identity():
    global BOT_ID, BOT_USERNAME
    data = tg_call("getMe")
    if data.get("ok"):
        BOT_ID = data["result"]["id"]
        BOT_USERNAME = data["result"]["username"]
        log.info("Bot identifikatsiyasi: id=%s username=%s", BOT_ID, BOT_USERNAME)


def set_default_commands():
    """Menyuda faqat /start ko'rinadi — qolgan hammasi inline tugmalar orqali."""
    tg_call("setMyCommands", commands=[{"command": "start", "description": "Botni ishga tushirish"}])


def clear_stale_command_scopes():
    """Eski (v1) botda superadmin/adminlar uchun ALOHIDA (chat-specific) komandalar
    menyusi o'rnatilgan edi (setMyCommands + scope=chat). Bu Telegram serverida
    saqlanib qoladi va global setMyCommands uni qamrab olmaydi — shu sabab eski
    menyu hamon ko'rinib turadi. Har bir admin/superadmin uchun aynan o'sha scope'ni
    o'chirib tashlaymiz, shundan keyin ular ham faqat /start'ni ko'radi."""
    with _state_lock:
        targets = set(STATE.get("admins", [])) | {SUPERADMIN_ID}
    for uid in targets:
        tg_call("deleteMyCommands", scope={"type": "chat", "chat_id": uid})


init_bot_identity()
set_webhook()
set_default_commands()
clear_stale_command_scopes()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
