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
  - Majburiy obuna: superadmin qo'shgan kanal(lar)ga a'zo bo'lmagan foydalanuvchi
    botdan foydalana olmaydi (/addforcechannel)
  - Bonus kanallar: a'zo bo'lgan foydalanuvchiga bir martalik +2 limit (/bonus)
  - Premium: Telegram Stars orqali 6 oylik cheksiz foydalanish (/premium)
  - Bitta sticker forward qilinganda: "butun pack" yoki "faqat shu fayl" tanlovi
  - Pack ZIP fayllar keshi: bir xil pack qayta so'ralganda alohida guruhga
    (CACHE_GROUP_ID) saqlangan file_id orqali qayta yuborilib, Telegram'dan
    qayta yuklab olinmaydi
  - /stats (umumiy) va /mystats (shaxsiy) statistikasi
  - /chatinfo — admin uchun guruh/kanal a'zolari sonini ko'rish

ENV o'zgaruvchilar (Render Environment tab):
  BOT_TOKEN       - bot tokeni
  SUPERADMIN_ID   - sizning Telegram user ID'ingiz (butun son)
  WEBHOOK_URL     - https://<render-app-nomi>.onrender.com
  DB_GROUP_ID     - DB sifatida ishlatiladigan Telegram guruh ID'si (bot shu
                    guruhda admin bo'lishi va xabar yuborish huquqiga ega
                    bo'lishi kerak)
  CACHE_GROUP_ID  - (ixtiyoriy) Pack ZIP fayllarini keshlash uchun alohida
                    Telegram guruh ID'si (bot shu guruhda ham a'zo/admin
                    bo'lishi kerak). Bo'lmasa, keshlash oddiygina o'chiriladi.
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
import threading
from datetime import datetime, timezone

from flask import Flask, request
import requests

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

REACTION_EMOJI = "⚡"

app = Flask(__name__)

BOT_ID = None          # getMe orqali to'ldiriladi
BOT_USERNAME = None
_pinned_message_id = None  # DB pinned xabar ID keshi
_state_lock = threading.Lock()  # Fon oqimlar bir vaqtda STATE'ni yozib yubormasligi uchun

# Forward qilingan bitta sticker/emoji uchun "Butun pack" / "Faqat shu" tanlovi
# xotirada saqlanadi (qayta ishga tushganda yo'qoladi — bu qabul qilinadi, chunki
# tanlov faqat bir necha daqiqa amal qilishi kerak).
_pending_choices = {}
_pending_lock = threading.Lock()


def store_pending_choice(payload):
    import uuid
    token = uuid.uuid4().hex[:10]
    with _pending_lock:
        _pending_choices[token] = payload
    return token


def pop_pending_choice(token):
    with _pending_lock:
        return _pending_choices.pop(token, None)


# ---------- Telegram API helper funksiyalar ----------

def tg_call(method, **params):
    resp = requests.post(f"{API_BASE}/{method}", json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram xato (%s): %s", method, data)
    return data


def send_message(chat_id, text, reply_to=None, parse_mode_html=False, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if parse_mode_html:
        params["parse_mode"] = "HTML"
    if reply_markup:
        params["reply_markup"] = reply_markup
    return tg_call("sendMessage", **params)


def edit_message_text(chat_id, message_id, text, parse_mode_html=False, reply_markup=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode_html:
        params["parse_mode"] = "HTML"
    if reply_markup:
        params["reply_markup"] = reply_markup
    return tg_call("editMessageText", **params)


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
        params["show_alert"] = show_alert
    return tg_call("answerCallbackQuery", **params)


def main_menu_keyboard():
    return {
        "inline_keyboard": [
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
        ]
    }


def back_to_menu_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}]]}


def build_help_text(user_id):
    cfg = get_limit_config()
    base = cfg["base_weekly"]
    weekly_cap = cfg["weekly_cap"]
    help_text = (
        "📋 <b>Buyruqlar ro'yxati</b>\n\n"
        "👤 <b>Hammaga:</b>\n"
        "/start — botni ishga tushirish\n"
        "/getpack &lt;pack_nomi&gt; — pack'ni ZIP qilib olish\n"
        "(yoki sticker/custom emoji'ni to'g'ridan-to'g'ri forward qiling)\n"
        "/ref — referal havolangiz va hozirgi limitingiz\n"
        "/limit — bugungi/shu haftadagi foydalanishingiz\n"
        "/bonus — bonus kanallarga a'zo bo'lib qo'shimcha limit olish\n"
        "/premium — Stars orqali cheksiz foydalanish (premium) sotib olish\n"
        "/mystats — shaxsiy statistikangiz\n"
        "/help — shu xabar\n\n"
        "⚙️ <b>Limit qoidalari:</b>\n"
        f"• Yangi foydalanuvchi: haftasiga {base} marta bepul so'rov.\n"
        f"• Har bir referal haftalik imkoniyatingizni +1 taga oshiradi "
        f"({base} → {base + 1} → {base + 2} → ... → {weekly_cap}).\n"
        f"• Imkoniyatlar {weekly_cap} taga (kuniga 1 martaga teng) yetganda, "
        f"tizim HAFTALIKdan KUNLIKka o'tadi.\n"
        f"• Shundan keyingi HAR BIR qo'shimcha referal kunlik limitingizni "
        f"2 baravar oshiradi (1 → 2 → 4 → 8 ...).\n"
        "• Adminlar uchun limit yo'q.\n\n"
    )
    if is_admin(user_id):
        help_text += (
            "🛠 <b>Admin buyruqlari:</b>\n"
            "/broadcast xabar — barcha foydalanuvchilarga xabar yuborish\n"
            "/chatinfo @username_yoki_id — guruh/kanal a'zolari sonini ko'rish\n\n"
            "👥 <b>Guruhda (reply orqali, nuqta bilan boshlanadi):</b>\n"
            ".zip — reply qilingan xabardagi pack'ni ZIP qilib berish\n"
            ".zipstiker — reply qilingan bitta sticker/emoji'ni yuklab berish\n"
        )
    if user_id == SUPERADMIN_ID:
        help_text += (
            "\n👑 <b>Faqat superadmin uchun:</b>\n"
            "/addadmin @username yoki user_id — admin qo'shish\n"
            "/deladmin @username yoki user_id — adminlikdan olish\n"
            "/addlimit @username_yoki_id miqdor — foydalanuvchiga qo'shimcha bonus limit berish\n"
            "/setbaselimit son — referalsiz foydalanuvchilar uchun haftalik bazaviy limitni o'zgartirish\n"
            "/setweeklycap son — haftalik imkoniyat qaysi songa yetganda kunlikka o'tishini belgilash\n"
            "/addforcechannel @username_yoki_id — majburiy obuna kanal qo'shish\n"
            "/delforcechannel @username_yoki_id — majburiy obuna kanalni olib tashlash\n"
            "/listforcechannels — majburiy kanallar ro'yxati\n"
            "/addbonuschannel @username_yoki_id — bonus kanal qo'shish (+2 limit)\n"
            "/delbonuschannel @username_yoki_id — bonus kanalni olib tashlash\n"
            "/listbonuschannels — bonus kanallar ro'yxati\n"
            "/stats — umumiy bot statistikasi\n"
            "/reload — DB'ni guruhdan qayta yuklash\n"
            ".addadmin — reply qilingan odamni admin qilish (guruhda)\n"
            ".deladmin — reply qilingan odamni adminlikdan olish (guruhda)\n"
            ".del — reply qilingan xabarni o'chirish (guruhda)\n"
        )
    return help_text


def send_document_bytes(chat_id, filename, file_bytes, caption=None):
    files = {"document": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    resp = requests.post(f"{API_BASE}/sendDocument", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendDocument javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendDocument xato: %s", data)
    return data


def send_document_by_file_id(chat_id, file_id, caption=None):
    params = {"chat_id": chat_id, "document": file_id}
    if caption:
        params["caption"] = caption
    return tg_call("sendDocument", **params)


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
    return {
        "admins": [],
        "users": {},
        "known_users": [],
        # Superadmin sozlashi mumkin bo'lgan limit konfiguratsiyasi:
        #   base_weekly  - referalsiz foydalanuvchi uchun haftalik imkoniyatlar soni (default: 7)
        #   weekly_cap   - haftalik imkoniyatlar shu songa yetganda tizim "kunlik" rejimga o'tadi (default: 7)
        "config": {"base_weekly": 7, "weekly_cap": 7},
        # Majburiy kanal/guruhlar — superadmin cheksiz sonda qo'sha oladi.
        # Foydalanuvchi shaxsiy chatda botdan foydalanishdan oldin ularga a'zo bo'lishi shart.
        "force_channels": [],  # [{"chat_id": int, "title": str, "username": str|None}]
        # Bonus kanallar — majburiy emas, lekin a'zo bo'lgan (va tasdiqlagan)
        # foydalanuvchiga bir martalik +2 limit beriladi.
        "bonus_channels": [],  # [{"chat_id": int, "title": str, "username": str|None}]
        # Pack ZIP fayllar keshi (CACHE_GROUP_ID sozlangan bo'lsa ishlaydi).
        "pack_cache": {},  # {pack_name_lower: {"file_id": str, "sticker_count": int, "cached_at": iso}}
        # Umumiy statistika (superadmin /stats orqali ko'radi).
        "stats": {"total_requests": 0},
    }


def default_user_record():
    return {
        "period_key": None,
        "mode": None,
        "count": 0,
        "referrals": 0,
        "referred_by": None,
        "bonus": 0,
        "premium_until": None,  # UNIX timestamp (UTC) — Stars orqali xarid qilingan cheksiz muddat
        "claimed_bonus_channels": [],  # bonus olingan kanal chat_id'lari (qayta olinmasligi uchun)
        "lifetime_requests": 0,  # /mystats uchun — umr bo'yi jami so'rovlar
    }


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
    with _state_lock:
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


def iso_week_str():
    # ISO-8601 hafta identifikatori, masalan "2026-W28"
    return datetime.now(timezone.utc).strftime("%G-W%V")


def get_limit_config():
    STATE.setdefault("config", {})
    cfg = STATE["config"]
    cfg.setdefault("base_weekly", 7)
    cfg.setdefault("weekly_cap", 7)
    return cfg


def get_force_channels():
    STATE.setdefault("force_channels", [])
    return STATE["force_channels"]


def get_bonus_channels():
    STATE.setdefault("bonus_channels", [])
    return STATE["bonus_channels"]


def get_pack_cache():
    STATE.setdefault("pack_cache", {})
    return STATE["pack_cache"]


def get_stats():
    STATE.setdefault("stats", {"total_requests": 0})
    STATE["stats"].setdefault("total_requests", 0)
    return STATE["stats"]


def bump_total_requests():
    stats = get_stats()
    stats["total_requests"] += 1


# ---------- Majburiy / bonus kanallar ----------

def _resolve_chat(token):
    """'@username' yoki chat_id'ni getChat orqali {"chat_id","title","username"} ga aylantiradi."""
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


def _channel_join_button(ch):
    if ch.get("username"):
        url = f"https://t.me/{ch['username']}"
    else:
        # Public username bo'lmagan kanal/guruh uchun taklif havolasi yaratamiz
        # (bot shu chatda admin bo'lishi va invite link yaratish huquqiga ega bo'lishi kerak).
        data = tg_call("createChatInviteLink", chat_id=ch["chat_id"])
        url = data["result"]["invite_link"] if data.get("ok") else f"https://t.me/{ch['chat_id']}"
    return {"text": f"➕ {ch['title']}", "url": url}


def missing_force_channels(user_id):
    """Foydalanuvchi hali a'zo bo'lmagan majburiy kanallar ro'yxatini qaytaradi."""
    missing = []
    for ch in get_force_channels():
        status = get_chat_member_status(ch["chat_id"], user_id)
        if status not in ("member", "administrator", "creator"):
            missing.append(ch)
    return missing


def enforce_force_join(chat_id, user_id):
    """True — foydalanuvchi davom etishi mumkin. False — majburiy kanal(lar)ga
    a'zo bo'lishi kerakligi haqida xabar yuborilgan, chaqiruvchi funksiya to'xtashi kerak."""
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


# ---------- Xabar tahrirlashda muvaffaqiyatsizlik bo'lsa yangi xabar yuborish ----------

def safe_edit_or_send(chat_id, message_id, text, parse_mode_html=False, reply_markup=None):
    """editMessageText muvaffaqiyatsiz bo'lsa (masalan xabar juda eski yoki
    o'chirilgan), jim qolish o'rniga yangi xabar yuboradi — 'tugma ishlamayapti'
    kabi muammolarning oldini oladi."""
    result = edit_message_text(chat_id, message_id, text, parse_mode_html=parse_mode_html, reply_markup=reply_markup)
    if not result.get("ok"):
        send_message(chat_id, text, parse_mode_html=parse_mode_html, reply_markup=reply_markup)


def get_user_record(user_id):
    uid = str(user_id)
    if uid not in STATE["users"]:
        STATE["users"][uid] = default_user_record()
    record = STATE["users"][uid]
    # Eski yozuvlar bilan moslik uchun:
    defaults = default_user_record()
    for key, value in defaults.items():
        record.setdefault(key, value)
    return record


def is_premium(user_id):
    record = get_user_record(user_id)
    until = record.get("premium_until")
    if not until:
        return False
    return datetime.now(timezone.utc).timestamp() < until


def grant_premium(user_id, days=182):
    """Stars orqali xarid qilingandan keyin cheksiz muddatni yoqadi (default ~6 oy)."""
    record = get_user_record(user_id)
    now = datetime.now(timezone.utc).timestamp()
    current_until = record.get("premium_until") or now
    base = max(now, current_until)
    record["premium_until"] = base + days * 86400
    save_state()
    return record["premium_until"]


# ---------- Rol asosidagi "/" buyruqlar menyusi (setMyCommands) ----------
# BotFather orqali qo'lda sozlash o'rniga — har bir foydalanuvchi guruhiga
# (oddiy/admin/superadmin) mos ro'yxat avtomatik ko'rsatiladi.

USER_COMMANDS = [
    ("start", "Botni ishga tushirish"),
    ("getpack", "Pack'ni ZIP qilib olish"),
    ("ref", "Referal havolangiz va limitingiz"),
    ("limit", "Joriy foydalanishingiz"),
    ("bonus", "Bonus kanallar orqali limit oshirish"),
    ("premium", "Cheksiz foydalanish (Stars)"),
    ("mystats", "Shaxsiy statistikangiz"),
    ("help", "Yordam va buyruqlar ro'yxati"),
]

ADMIN_EXTRA_COMMANDS = [
    ("broadcast", "Barcha foydalanuvchilarga xabar yuborish"),
    ("chatinfo", "Guruh/kanal a'zolari sonini ko'rish"),
]

SUPERADMIN_EXTRA_COMMANDS = [
    ("addadmin", "Admin qo'shish"),
    ("deladmin", "Adminlikdan olish"),
    ("addlimit", "Foydalanuvchiga bonus limit berish"),
    ("setbaselimit", "Haftalik bazaviy limitni o'zgartirish"),
    ("setweeklycap", "Kunlikka o'tish chegarasini belgilash"),
    ("addforcechannel", "Majburiy obuna kanal qo'shish"),
    ("delforcechannel", "Majburiy obuna kanalni olib tashlash"),
    ("listforcechannels", "Majburiy kanallar ro'yxati"),
    ("addbonuschannel", "Bonus kanal qo'shish"),
    ("delbonuschannel", "Bonus kanalni olib tashlash"),
    ("listbonuschannels", "Bonus kanallar ro'yxati"),
    ("stats", "Umumiy bot statistikasi"),
    ("reload", "DB'ni guruhdan qayta yuklash"),
]


def _commands_payload(commands):
    return [{"command": name, "description": desc} for name, desc in commands]


def set_default_commands():
    """Hech qanday maxsus scope'i bo'lmagan barcha foydalanuvchilar uchun bazaviy ro'yxat."""
    tg_call("setMyCommands", commands=_commands_payload(USER_COMMANDS))


def set_chat_commands(chat_id, commands):
    """Muayyan foydalanuvchi (chat_id) uchun kengaytirilgan buyruqlar ro'yxatini o'rnatadi."""
    tg_call(
        "setMyCommands",
        commands=_commands_payload(commands),
        scope={"type": "chat", "chat_id": chat_id},
    )


def reset_chat_commands(chat_id):
    """Foydalanuvchini bazaviy (default) ro'yxatga qaytaradi (masalan, admin olib tashlanganda)."""
    tg_call("deleteMyCommands", scope={"type": "chat", "chat_id": chat_id})


def sync_role_commands(user_id):
    """user_id'ning joriy roliga qarab uning shaxsiy chatidagi '/' menyusini yangilaydi."""
    if user_id == SUPERADMIN_ID:
        set_chat_commands(user_id, USER_COMMANDS + ADMIN_EXTRA_COMMANDS + SUPERADMIN_EXTRA_COMMANDS)
    elif user_id in STATE["admins"]:
        set_chat_commands(user_id, USER_COMMANDS + ADMIN_EXTRA_COMMANDS)
    else:
        reset_chat_commands(user_id)


def sync_all_role_commands():
    """Bot ishga tushganda superadmin va barcha adminlar uchun menyularni qayta o'rnatadi."""
    set_default_commands()
    sync_role_commands(SUPERADMIN_ID)
    for admin_id in STATE.get("admins", []):
        sync_role_commands(admin_id)


def is_admin(user_id):
    return user_id == SUPERADMIN_ID or user_id in STATE["admins"]


def compute_user_limit(user_id):
    """
    Limit mantig'i:
      - Referalsiz: haftasiga `base_weekly` marta (default 1/hafta).
      - Har bir referal haftalik imkoniyatni +1 qiladi: 1 -> 2 -> 3 -> ... -> weekly_cap.
      - Imkoniyatlar soni `weekly_cap`ga (default 7, ya'ni har kuni 1 marta) yetganda,
        tizim HAFTALIKdan KUNLIKka o'tadi (kunlik 1 marta).
      - Shundan keyingi HAR BIR qo'shimcha referal kunlik limitni 2 baravar oshiradi
        (1 -> 2 -> 4 -> 8 -> ...).
      - Superadmin bergan qo'shimcha bonus (/addlimit) ustiga qo'shiladi.
    Qaytaradi: (mode, limit) bu yerda mode "weekly" yoki "daily".
    """
    record = get_user_record(user_id)
    cfg = get_limit_config()
    base = cfg["base_weekly"]
    weekly_cap = cfg["weekly_cap"]
    referrals = record["referrals"]
    threshold = max(0, weekly_cap - base)  # kunlik rejimga o'tish uchun kerak bo'lgan referallar soni

    slots = base + referrals
    if slots < weekly_cap:
        mode = "weekly"
        limit = slots
    else:
        mode = "daily"
        extra = max(0, referrals - threshold)
        limit = 2 ** extra  # har qo'shimcha referal uchun 2x

    limit += record.get("bonus", 0)
    return mode, max(1, limit)


def ensure_period_reset(user_id):
    """Foydalanuvchi rejimi (haftalik/kunlik) o'zgargan yoki davr (hafta/kun) yangilangan bo'lsa, hisoblagichni nolga tushiradi."""
    record = get_user_record(user_id)
    mode, limit = compute_user_limit(user_id)
    key = today_str() if mode == "daily" else iso_week_str()
    if record.get("mode") != mode or record.get("period_key") != key:
        record["mode"] = mode
        record["period_key"] = key
        record["count"] = 0
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
            f"Limitni oshirish uchun /ref orqali do'stlaringizni taklif qiling."
        )
    return True, None


def register_request(user_id):
    record = get_user_record(user_id)
    record["lifetime_requests"] = record.get("lifetime_requests", 0) + 1
    bump_total_requests()
    if is_admin(user_id) or is_premium(user_id):
        save_state()
        return
    ensure_period_reset(user_id)
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
    mode, limit = ensure_period_reset(referrer_id)
    save_state()
    period = "kunlik" if mode == "daily" else "haftalik"
    send_message(
        referrer_id,
        f"🎉 Sizning referal havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
        f"Yangi {period} limitingiz: {limit} ta.",
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
    """Webhook so'rovini darhol bo'shatish uchun fon oqimida ishlaydi
    (Telegram webhook timeout / qayta-yuborishning oldini olish uchun)."""
    threading.Thread(
        target=_handle_pack_request_sync,
        args=(chat_id, pack_name, requester_info, requester_id, reply_to),
        daemon=True,
    ).start()


def _handle_pack_request_sync(chat_id, pack_name, requester_info, requester_id, reply_to=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to)
        return

    send_message(chat_id, f"'{pack_name}' qidirilmoqda, kuting...", reply_to=reply_to)

    # ---- Keshni tekshirish (CACHE_GROUP_ID sozlangan bo'lsa) ----
    if CACHE_GROUP_ID:
        cache = get_pack_cache()
        cached = cache.get(pack_name.lower())
        if cached:
            sticker_set = get_sticker_set(pack_name)
            if sticker_set and len(sticker_set["stickers"]) == cached.get("sticker_count"):
                result = send_document_by_file_id(
                    chat_id, cached["file_id"], caption=f"{cached['sticker_count']} ta fayl topildi. (kesh)"
                )
                if result.get("ok"):
                    register_request(requester_id)
                    notify_admin(
                        f"✅ So'rov keshdan bajarildi\n"
                        f"Kimdan: {requester_info}\n"
                        f"Pack: {pack_name}"
                    )
                    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
                        send_document_by_file_id(
                            SUPERADMIN_ID, cached["file_id"],
                            caption=f"{requester_info} so'ragan pack: {pack_name} (kesh)",
                        )
                    return
            else:
                # Pack yangilangan (stiker soni o'zgargan) — eski keshni bekor qilamiz
                cache.pop(pack_name.lower(), None)

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
    send_result = send_document_bytes(
        chat_id,
        f"{pack_name}.zip",
        zip_bytes,
        caption=f"{result} ta fayl topildi.",
    )

    # ---- Keshga saqlash: cache guruhiga alohida nusxa yuborib file_id'ni saqlaymiz ----
    if CACHE_GROUP_ID:
        cache_result = send_document_bytes(CACHE_GROUP_ID, f"{pack_name}.zip", zip_bytes)
        if cache_result.get("ok"):
            doc = cache_result["result"]["document"]
            get_pack_cache()[pack_name.lower()] = {
                "file_id": doc["file_id"],
                "sticker_count": result,
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }

    save_state()

    notify_admin(
        f"✅ Yangi so'rov bajarildi\n"
        f"Kimdan: {requester_info}\n"
        f"Pack: {pack_name}\n"
        f"Fayllar soni: {result}"
    )
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        if send_result.get("ok"):
            send_document_by_file_id(
                SUPERADMIN_ID,
                send_result["result"]["document"]["file_id"],
                caption=f"{requester_info} so'ragan pack: {pack_name} ({result} ta fayl)",
            )
        else:
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


def zip_single_file(filename, content):
    """Bitta faylni ZIP ichiga joylaydi. Telegram .tgs/.webm kabi fayllarni
    hujjat sifatida emas, animatsion stiker sifatida avtomatik tanib, foydalanuvchi
    uni oddiy fayl kabi yuklab ololmay qolishining oldini olish uchun kerak —
    ZIP arxivini esa Telegram hech qachon maxsus ravishda qayta ishlamaydi."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    buf.seek(0)
    return buf.getvalue()


def handle_single_sticker_request(chat_id, reply, requester_info, requester_id, reply_to=None):
    """Fon oqimida ishlaydi (webhook darhol javob qaytarishi uchun)."""
    threading.Thread(
        target=_handle_single_sticker_request_sync,
        args=(chat_id, reply, requester_info, requester_id, reply_to),
        daemon=True,
    ).start()


def _handle_single_sticker_request_sync(chat_id, reply, requester_info, requester_id, reply_to=None):
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
    zip_bytes = zip_single_file(filename, content)
    zip_name = f"{filename}.zip"
    send_document_bytes(chat_id, zip_name, zip_bytes, caption="Faylni ochish uchun ZIP'ni yeching.")

    notify_admin(
        f"✅ Bitta sticker yuklandi\n"
        f"Kimdan: {requester_info}\n"
        f"Fayl: {filename}"
    )
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan sticker")


def handle_single_sticker_request_from_pending(chat_id, pending, requester_id):
    """dl_single: callback orqali — pending tanlovdagi file_id asosida bitta faylni yuboradi."""
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
    register_request(requester_id)

    filename = f"sticker_{pending['emoji_char']}{pending['ext']}".replace("/", "_")
    zip_bytes = zip_single_file(filename, content)
    zip_name = f"{filename}.zip"
    send_document_bytes(chat_id, zip_name, zip_bytes, caption="Faylni ochish uchun ZIP'ni yeching.")

    notify_admin(
        f"✅ Bitta sticker yuklandi\n"
        f"Kimdan: {pending['requester_info']}\n"
        f"Fayl: {filename}"
    )
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{pending['requester_info']} yuklagan sticker")


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


# ---------- Inline menyu (callback_query) ----------

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

    register_known_user(user_id)

    if data == "menu_home":
        answer_callback_query(cq_id)
        safe_edit_or_send(
            chat_id,
            message_id,
            "Salom! Menga sticker/custom emoji forward qiling yoki "
            "/getpack <pack_nomi> deb yozing — men barcha fayllarni ZIP qilib beraman.\n\n"
            "Quyidagi tugmalardan ham foydalanishingiz mumkin 👇",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "menu_getpack":
        answer_callback_query(cq_id)
        safe_edit_or_send(
            chat_id,
            message_id,
            "📦 Pack yuklab olish uchun:\n\n"
            "• <code>/getpack pack_nomi</code> deb yozing, YOKI\n"
            "• pack ichidagi istalgan bitta sticker/custom emoji'ni menga forward qiling.\n\n"
            "Pack nomini uning ulashish havolasidan ham olsangiz bo'ladi "
            "(masalan t.me/addstickers/<b>pack_nomi</b>).",
            parse_mode_html=True,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "menu_ref":
        answer_callback_query(cq_id)
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        record = get_user_record(user_id)
        mode, limit = ensure_period_reset(user_id)
        save_state()
        period = "kunlik" if mode == "daily" else "haftalik"
        safe_edit_or_send(
            chat_id,
            message_id,
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
            save_state()
            record = get_user_record(user_id)
            period_label = "Bugungi" if mode == "daily" else "Shu haftadagi"
            text = f"📊 {period_label} foydalanish: {record['count']}/{limit}"
        safe_edit_or_send(chat_id, message_id, text, reply_markup=back_to_menu_keyboard())
        return

    if data == "menu_help":
        answer_callback_query(cq_id)
        safe_edit_or_send(
            chat_id,
            message_id,
            build_help_text(user_id),
            parse_mode_html=True,
            reply_markup=back_to_menu_keyboard(),
        )
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
            text = f"⭐ Sizda premium allaqachon faol — {until} sanagacha cheksiz foydalanasiz."
            safe_edit_or_send(chat_id, message_id, text, reply_markup=back_to_menu_keyboard())
        else:
            text = (
                "⭐ <b>Premium</b>\n\n"
                "Premium bilan kunlik/haftalik limitlarsiz, cheksiz pack yuklab olasiz "
                "(6 oy muddatga, Telegram Stars orqali)."
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "⭐ 100 Stars uchun sotib olish", "callback_data": "buy_premium"}],
                    [{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}],
                ]
            }
            safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "buy_premium":
        answer_callback_query(cq_id)
        tg_call(
            "sendInvoice",
            chat_id=chat_id,
            title="StokerDownloader Premium (6 oy)",
            description="Cheksiz pack yuklab olish, kunlik/haftalik limitlarsiz — 6 oy muddatga.",
            payload=f"premium_182:{user_id}",
            provider_token="",  # Telegram Stars (XTR) uchun bo'sh string bo'lishi shart
            currency="XTR",
            prices=[{"label": "Premium 6 oy", "amount": 100}],
        )
        return

    if data == "check_force_join":
        missing = missing_force_channels(user_id)
        if missing:
            answer_callback_query(cq_id, "Hali ham barcha kanallarga a'zo emassiz.", show_alert=True)
            return
        answer_callback_query(cq_id, "✅ Rahmat! Endi botdan foydalanishingiz mumkin.", show_alert=True)
        safe_edit_or_send(
            chat_id,
            message_id,
            "✅ Barcha majburiy kanallarga a'zo bo'ldingiz. Botdan foydalanishingiz mumkin!",
            reply_markup=main_menu_keyboard(),
        )
        return

    if data.startswith("claim_bonus:"):
        target_chat_id = int(data.split(":", 1)[1])
        status = get_chat_member_status(target_chat_id, user_id)
        record = get_user_record(user_id)
        if status not in ("member", "administrator", "creator"):
            answer_callback_query(cq_id, "Hali bu kanalga a'zo emassiz.", show_alert=True)
            return
        if target_chat_id in record["claimed_bonus_channels"]:
            answer_callback_query(cq_id, "Bu bonusni allaqachon olgansiz.", show_alert=True)
            return
        record["claimed_bonus_channels"].append(target_chat_id)
        record["bonus"] = record.get("bonus", 0) + 2
        save_state()
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

    answer_callback_query(cq_id)


# ---------- Webhook endpoint ----------

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global STATE
    update = request.get_json(force=True)

    # ---- Inline tugma bosilishi (callback_query) ----
    callback_query = update.get("callback_query")
    if callback_query:
        handle_callback_query(callback_query)
        return {"ok": True}

    # ---- Kanal postlari: bot admin bo'lsa avtomatik reaksiya ----
    channel_post = update.get("channel_post")
    if channel_post:
        chat_id = channel_post["chat"]["id"]
        if bot_is_group_admin(chat_id):
            react(chat_id, channel_post["message_id"])
        return {"ok": True}

    # ---- Stars to'lovi: checkout tasdiqlash ----
    pre_checkout_query = update.get("pre_checkout_query")
    if pre_checkout_query:
        tg_call("answerPreCheckoutQuery", pre_checkout_query_id=pre_checkout_query["id"], ok=True)
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    # ---- Stars to'lovi muvaffaqiyatli yakunlandi ----
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
            "Quyidagi tugmalardan ham foydalanishingiz mumkin 👇",
            reply_markup=main_menu_keyboard(),
        )
        return {"ok": True}

    if text.startswith("/help"):
        send_message(chat_id, build_help_text(user_id), parse_mode_html=True, reply_markup=back_to_menu_keyboard())
        return {"ok": True}

    if text.startswith("/ref"):
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
        record = get_user_record(user_id)
        mode, limit = ensure_period_reset(user_id)
        period = "kunlik" if mode == "daily" else "haftalik"
        send_message(
            chat_id,
            f"Referal havolangiz:\n{link}\n\n"
            f"Hozirgi referallar: {record['referrals']}\n"
            f"Yangi {period} limitingiz: {limit} ta.",
        )
        return {"ok": True}

    if text.startswith("/setbaselimit"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /setbaselimit son\nMasalan: /setbaselimit 1")
            return {"ok": True}
        try:
            value = int(parts[1])
            if value < 1:
                raise ValueError
        except ValueError:
            send_message(chat_id, "Son musbat butun bo'lishi kerak.")
            return {"ok": True}
        cfg = get_limit_config()
        cfg["base_weekly"] = value
        save_state()
        send_message(chat_id, f"✅ Bazaviy haftalik limit endi: {value} ta.")
        return {"ok": True}

    if text.startswith("/setweeklycap"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split()
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /setweeklycap son\nMasalan: /setweeklycap 7")
            return {"ok": True}
        try:
            value = int(parts[1])
            if value < 1:
                raise ValueError
        except ValueError:
            send_message(chat_id, "Son musbat butun bo'lishi kerak.")
            return {"ok": True}
        cfg = get_limit_config()
        cfg["weekly_cap"] = value
        save_state()
        send_message(chat_id, f"✅ Haftalik-kunlikka o'tish chegarasi endi: {value} ta/hafta.")
        return {"ok": True}

    if text.startswith("/limit"):
        if is_admin(user_id):
            send_message(chat_id, "Foydalanishingiz: cheksiz (admin)")
            return {"ok": True}
        mode, limit = ensure_period_reset(user_id)
        record = get_user_record(user_id)
        period_label = "Bugungi" if mode == "daily" else "Shu haftadagi"
        send_message(chat_id, f"{period_label} foydalanish: {record['count']}/{limit}")
        return {"ok": True}

    if text.startswith("/mystats"):
        record = get_user_record(user_id)
        premium_line = ""
        if is_premium(user_id):
            until = datetime.fromtimestamp(record["premium_until"], tz=timezone.utc).strftime("%Y-%m-%d")
            premium_line = f"⭐ Premium: {until} sanagacha faol\n"
        send_message(
            chat_id,
            "📈 <b>Shaxsiy statistikangiz</b>\n\n"
            f"Jami yuklab olingan fayllar: {record.get('lifetime_requests', 0)}\n"
            f"Referallar soni: {record['referrals']}\n"
            f"Bonus limit: {record.get('bonus', 0)}\n"
            f"{premium_line}",
            parse_mode_html=True,
        )
        return {"ok": True}

    if text.startswith("/bonus"):
        text_out, keyboard = build_bonus_menu_text_and_keyboard(user_id)
        send_message(chat_id, text_out, reply_markup=keyboard)
        return {"ok": True}

    if text.startswith("/premium"):
        if is_premium(user_id):
            record = get_user_record(user_id)
            until = datetime.fromtimestamp(record["premium_until"], tz=timezone.utc).strftime("%Y-%m-%d")
            send_message(chat_id, f"⭐ Sizda premium allaqachon faol — {until} sanagacha cheksiz foydalanasiz.")
        else:
            send_message(
                chat_id,
                "⭐ <b>Premium</b>\n\nCheksiz pack yuklab olish, 6 oy muddatga.",
                parse_mode_html=True,
                reply_markup={"inline_keyboard": [[{"text": "⭐ 100 Stars uchun sotib olish", "callback_data": "buy_premium"}]]},
            )
        return {"ok": True}

    if text.startswith("/chatinfo"):
        if not is_admin(user_id):
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /chatinfo @username yoki chat_id")
            return {"ok": True}
        ch = _resolve_chat(parts[1])
        if not ch:
            send_message(chat_id, "Guruh/kanal topilmadi.")
            return {"ok": True}
        count_data = tg_call("getChatMemberCount", chat_id=ch["chat_id"])
        count = count_data["result"] if count_data.get("ok") else "noma'lum"
        info_lines = [f"📋 {ch['title']}", f"ID: {ch['chat_id']}"]
        if ch["username"]:
            info_lines.append(f"Username: @{ch['username']}")
        info_lines.append(f"A'zolar soni: {count}")
        send_message(chat_id, "\n".join(info_lines))
        return {"ok": True}

    if text.startswith("/stats"):
        if not is_admin(user_id):
            return {"ok": True}
        stats = get_stats()
        send_message(
            chat_id,
            "📊 <b>Umumiy statistika</b>\n\n"
            f"Foydalanuvchilar (known_users): {len(STATE['known_users'])}\n"
            f"Adminlar: {len(STATE['admins'])}\n"
            f"Jami so'rovlar (umr bo'yi): {stats['total_requests']}\n"
            f"Majburiy kanallar: {len(get_force_channels())}\n"
            f"Bonus kanallar: {len(get_bonus_channels())}\n"
            f"Keshdagi packlar: {len(get_pack_cache())}\n",
            parse_mode_html=True,
        )
        return {"ok": True}

    if text.startswith("/addforcechannel"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /addforcechannel @username yoki chat_id")
            return {"ok": True}
        ch = _resolve_chat(parts[1])
        if not ch:
            send_message(chat_id, "Kanal topilmadi. Bot shu kanalda a'zo/admin ekanini tekshiring.")
            return {"ok": True}
        channels = get_force_channels()
        if not any(c["chat_id"] == ch["chat_id"] for c in channels):
            channels.append(ch)
            save_state()
        send_message(chat_id, f"✅ Majburiy kanal qo'shildi: {ch['title']}")
        return {"ok": True}

    if text.startswith("/delforcechannel"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /delforcechannel @username yoki chat_id")
            return {"ok": True}
        ch = _resolve_chat(parts[1])
        target_id = ch["chat_id"] if ch else None
        channels = get_force_channels()
        before = len(channels)
        STATE["force_channels"] = [c for c in channels if c["chat_id"] != target_id]
        save_state()
        removed = before - len(STATE["force_channels"])
        send_message(chat_id, f"✅ Olib tashlandi ({removed})." if removed else "Topilmadi.")
        return {"ok": True}

    if text.startswith("/listforcechannels"):
        if not is_admin(user_id):
            return {"ok": True}
        channels = get_force_channels()
        if not channels:
            send_message(chat_id, "Majburiy kanallar yo'q.")
        else:
            lines = "\n".join(f"• {c['title']} (id:{c['chat_id']})" for c in channels)
            send_message(chat_id, f"🔒 Majburiy kanallar:\n{lines}")
        return {"ok": True}

    if text.startswith("/addbonuschannel"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /addbonuschannel @username yoki chat_id")
            return {"ok": True}
        ch = _resolve_chat(parts[1])
        if not ch:
            send_message(chat_id, "Kanal topilmadi. Bot shu kanalda a'zo/admin ekanini tekshiring.")
            return {"ok": True}
        channels = get_bonus_channels()
        if not any(c["chat_id"] == ch["chat_id"] for c in channels):
            channels.append(ch)
            save_state()
        send_message(chat_id, f"✅ Bonus kanal qo'shildi: {ch['title']}")
        return {"ok": True}

    if text.startswith("/delbonuschannel"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Foydalanish: /delbonuschannel @username yoki chat_id")
            return {"ok": True}
        ch = _resolve_chat(parts[1])
        target_id = ch["chat_id"] if ch else None
        channels = get_bonus_channels()
        before = len(channels)
        STATE["bonus_channels"] = [c for c in channels if c["chat_id"] != target_id]
        save_state()
        removed = before - len(STATE["bonus_channels"])
        send_message(chat_id, f"✅ Olib tashlandi ({removed})." if removed else "Topilmadi.")
        return {"ok": True}

    if text.startswith("/listbonuschannels"):
        if not is_admin(user_id):
            return {"ok": True}
        channels = get_bonus_channels()
        if not channels:
            send_message(chat_id, "Bonus kanallar yo'q.")
        else:
            lines = "\n".join(f"• {c['title']} (id:{c['chat_id']})" for c in channels)
            send_message(chat_id, f"🎁 Bonus kanallar:\n{lines}")
        return {"ok": True}

    if text.lower().startswith("/reload"):
        if user_id != SUPERADMIN_ID:
            return {"ok": True}
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
        sync_role_commands(target_id)
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
        sync_role_commands(target_id)
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
        mode, new_limit = ensure_period_reset(target_id)
        save_state()
        period = "kunlik" if mode == "daily" else "haftalik"
        send_message(chat_id, f"✅ id:{target_id} uchun bonus limit +{amount} qo'shildi. Yangi {period} limit: {new_limit}")
        return {"ok": True}

    if text.startswith("/broadcast"):
        if not is_admin(user_id):
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
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        pack_name = extract_pack_name_from_link(raw) or raw
        handle_pack_request(chat_id, pack_name, requester_info, user_id)
        return {"ok": True}

    # ---- Bitta sticker/custom emoji forward qilindi: tanlov beramiz ----
    file_id, ext, emoji_char = extract_single_sticker_file(msg)
    if file_id:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        pack_name = extract_pack_name_from_message(msg)
        token = store_pending_choice({
            "pack_name": pack_name,
            "file_id": file_id,
            "ext": ext,
            "emoji_char": emoji_char,
            "requester_info": requester_info,
        })
        keyboard_rows = []
        if pack_name:
            keyboard_rows.append([{"text": "📦 Butun pack'ni ZIP qilib olish", "callback_data": f"dl_pack:{token}"}])
        keyboard_rows.append([{"text": "💾 Faqat shu stikerni olish", "callback_data": f"dl_single:{token}"}])
        send_message(
            chat_id,
            "Nima qilishimni xohlaysiz?",
            reply_markup={"inline_keyboard": keyboard_rows},
        )
        return {"ok": True}

    pack_name = extract_pack_name_from_message(msg)
    if pack_name:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
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
sync_all_role_commands()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
