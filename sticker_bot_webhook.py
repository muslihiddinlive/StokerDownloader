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
import re
import json
import html
import time
import uuid
import atexit
import random
import difflib
import base64
import zipfile
import hashlib
import logging
import tempfile
import subprocess
import threading
import asyncio
import concurrent.futures
from datetime import datetime, timezone, timedelta

from flask import Flask, request
import requests
import imageio_ffmpeg
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PhoneNumberInvalidError, ApiIdInvalidError, FloodWaitError, RPCError,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sticker-bot")
log.info("=== Bot process ishga tushdi (bio_clock live-refresh patch bilan) ===")

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

# .matnlar / .matn buyruqlari uchun — botdagi barcha (dublikatsiz) matn/tugma katalogi.
# Har birining ID'si (key) shu matnning o'zidan hisoblangan barqaror hash — kod qayta
# tuzilsa ham, matnning o'zi o'zgarmasa, key ham o'zgarmaydi. Faqat superadmin uchun.
# ESLATMA: bu ro'yxat kod yozilgan paytda avtomatik generatsiya qilingan (statik) —
# kelajakda yangi matn qo'shilsa, bu katalog qo'lda yangilanishi kerak.
TEXT_CATALOG = [
    {"n": 1, "key": "m25ccaebc", "kind": "message", "preview": "Sticker/emoji/GIF forward qiling yoki pastdagi menyudan foydalaning \U0001f447"},
    {"n": 2, "key": "m5615f198", "kind": "message", "preview": "Nima qilishimni xohlaysiz?"},
    {"n": 3, "key": "b3a0bebcb", "kind": "button", "preview": "\U0001f194 Butun pack ID\'larini berish"},
    {"n": 4, "key": "b74e371db", "kind": "button", "preview": "\U0001f4e6 Butun pack\'ni ZIP qilib olish"},
    {"n": 5, "key": "m520b8c41", "kind": "message", "preview": "GIF bilan nima qilishimni xohlaysiz?"},
    {"n": 6, "key": "b584786e4", "kind": "button", "preview": "\U0001f194 ID sini berish"},
    {"n": 7, "key": "b130d9467", "kind": "button", "preview": "\U0001f39e WebM qilib berish"},
    {"n": 8, "key": "b16ef0d6b", "kind": "button", "preview": "\U0001f194 Shu stiker/emojining ID\'sini berish"},
    {"n": 9, "key": "b1c0a429a", "kind": "button", "preview": "\U0001f4be Faqat shu stiker/emojini ZIP qilish"},
    {"n": 10, "key": "mc57902f8", "kind": "message", "preview": "Pack manzilini/nomini aniqlab bo\'lmadi."},
    {"n": 11, "key": "md7a7e1b3", "kind": "message", "preview": "Tartib raqami butun son bo\'lishi kerak."},
    {"n": 12, "key": "m76ecb4e1", "kind": "message", "preview": "Format: .tgs <pack_manzili_yoki_nomi> <tartib_raqami>"},
    {"n": 13, "key": "m6332caca", "kind": "message", "preview": "Bu buyruq faqat adminlar uchun."},
    {"n": 14, "key": "m3da8a391", "kind": "message", "preview": "\U0001f389 Premium faollashtirildi! {until_str} sanagacha cheksiz foydalanasiz."},
    {"n": 15, "key": "m0bb0279d", "kind": "message", "preview": "\u274c {requester_label(reply[\'from\'])} bot adminligidan olindi."},
    {"n": 16, "key": "me48578e4", "kind": "message", "preview": "\u2705 {requester_label(reply[\'from\'])} endi bot admini."},
    {"n": 17, "key": "mc60ea96b", "kind": "message", "preview": "Bu xabardan pack nomini topa olmadim."},
    {"n": 18, "key": "m44e82f2d", "kind": "message", "preview": "Stiker/custom emoji xabariga reply qilib .zip yozing."},
    {"n": 19, "key": "m677b1ae0", "kind": "message", "preview": "GIF xabariga reply qilib .zipgif yozing."},
    {"n": 20, "key": "m290b5e85", "kind": "message", "preview": "Sticker/custom emoji xabariga reply qilib .zipstiker yozing."},
    {"n": 21, "key": "md84ce349", "kind": "message", "preview": "Sticker/custom emoji xabariga reply qilib yozing."},
    {"n": 22, "key": "m3649b7e8", "kind": "message", "preview": "\U0001f4e3 Xabar {sent} ta foydalanuvchiga yuborildi."},
    {"n": 23, "key": "mbe472968", "kind": "message", "preview": "\u2705 Bonus kanal qo\'shildi: {ch[\'title\']}"},
    {"n": 24, "key": "m8f59e82d", "kind": "message", "preview": "Kanal topilmadi. Bot shu kanalda a\'zo/admin ekanini tekshiring."},
    {"n": 25, "key": "mc08e7370", "kind": "message", "preview": "\u2705 Majburiy kanal qo\'shildi: {ch[\'title\']}"},
    {"n": 26, "key": "m57a14e3a", "kind": "message", "preview": "\u2705 id:{target_id} endi bot admini."},
    {"n": 27, "key": "m321bc5a2", "kind": "message", "preview": "Butun ID kiriting. Bekor qilindi."},
    {"n": 28, "key": "mfe36d993", "kind": "message", "preview": "\u2705 id:{target_id} uchun bonus limit +{amount} qo\'shildi. Yangi {peri..."},
    {"n": 29, "key": "me01027b3", "kind": "message", "preview": "Butun son kiriting. Bekor qilindi."},
    {"n": 30, "key": "m8b0fb938", "kind": "message", "preview": "\U0001f4e8 Xabaringiz superadminga tasdiq uchun yuborildi."},
    {"n": 31, "key": "m91979466", "kind": "message", "preview": "\u2709\ufe0f <b>{sender_label}</b> boshqa adminlarga xabar yubormoqchi:\n\n{msg..."},
    {"n": 32, "key": "b9c5263c9", "kind": "button", "preview": "\u274c Rad etish"},
    {"n": 33, "key": "b38662198", "kind": "button", "preview": "\u2705 Ruxsat berish"},
    {"n": 34, "key": "mba322c07", "kind": "message", "preview": "\u2705 Xabaringiz {sent} ta adminga yuborildi."},
    {"n": 35, "key": "mc54503fd", "kind": "message", "preview": "\u2709\ufe0f <b>Superadmindan xabar:</b>\n\n{msg_text}"},
    {"n": 36, "key": "m2ebf9ed5", "kind": "message", "preview": "Bo\'sh xabar yuborib bo\'lmaydi. Bekor qilindi."},
    {"n": 37, "key": "m2eeeff3c", "kind": "message", "preview": "\u274c Yuborib bo\'lmadi (foydalanuvchi botni bloklagan bo\'lishi mumkin)."},
    {"n": 38, "key": "m35d7ebd9", "kind": "message", "preview": "\u2705 Yuborildi."},
    {"n": 39, "key": "mb7d9bf2b", "kind": "message", "preview": "\u2705 Endi siz {seconds} soniya ichida o\'zingiz javob yozmasangiz, avto..."},
    {"n": 40, "key": "m7547ba2a", "kind": "message", "preview": "\u2705 Kechikish o\'chirildi \u2014 javoblar darhol ketadi."},
    {"n": 41, "key": "m69f82c40", "kind": "message", "preview": "0 yoki musbat son bo\'lishi kerak."},
    {"n": 42, "key": "mbe72fdec", "kind": "message", "preview": "Butun son kiriting (soniya), masalan: 60"},
    {"n": 43, "key": "mbe2dfffc", "kind": "message", "preview": "\u2705 Bot imzosi o\'rnatildi (ID: {custom_emoji_id}). Endi shu bilan yub..."},
    {"n": 44, "key": "mf2df93fa", "kind": "message", "preview": "ID topilmadi. Premium emojining o\'zini yuboring yoki uning raqamli ..."},
    {"n": 45, "key": "m9a6c4f85", "kind": "message", "preview": "\u2705 {label} endi premium emoji: ID {custom_emoji_id}"},
    {"n": 46, "key": "m6c32a4dd", "kind": "message", "preview": "\u274c {result}"},
    {"n": 47, "key": "m63613a88", "kind": "message", "preview": "\u2705 Kalit qo\'shildi.{note}"},
    {"n": 48, "key": "m64c480ef", "kind": "message", "preview": "Bo\'sh bo\'lishi mumkin emas. Bekor qilindi."},
    {"n": 49, "key": "m066dff15", "kind": "message", "preview": "\xab{trigger}\xbb kelganda qanday javob yozilsin?"},
    {"n": 50, "key": "be9c0a4e4", "kind": "button", "preview": "\u2b05\ufe0f Orqaga"},
    {"n": 51, "key": "b9f96fedb", "kind": "button", "preview": "\u2b05\ufe0f Boshqaruv paneli"},
    {"n": 52, "key": "b919f4cb6", "kind": "button", "preview": "\U0001f6ab O\'chirish"},
    {"n": 53, "key": "b3bcc1ac1", "kind": "button", "preview": "\u2728 O\'rnatish/almashtirish"},
    {"n": 54, "key": "be4a68ce9", "kind": "button", "preview": "\u2728 Premium emoji (ID orqali)"},
    {"n": 55, "key": "bfc9115ce", "kind": "button", "preview": "\u2b05\ufe0f Superadmin panel"},
    {"n": 56, "key": "bbf16b40c", "kind": "button", "preview": "\u2795 Kanal qo\'shish"},
    {"n": 57, "key": "b94e5cc42", "kind": "button", "preview": "\u274c {c[\'title\']}"},
    {"n": 58, "key": "b5b01e9bf", "kind": "button", "preview": "\u2b05\ufe0f Panel"},
    {"n": 59, "key": "bae13f90c", "kind": "button", "preview": "\u2795 Admin qo\'shish"},
    {"n": 60, "key": "ba5ff21a9", "kind": "button", "preview": "\u274c id:{a}"},
    {"n": 61, "key": "b152a672c", "kind": "button", "preview": "Kalit +1"},
    {"n": 62, "key": "b676c46f9", "kind": "button", "preview": "Kalit \u22121"},
    {"n": 63, "key": "bc1189e15", "kind": "button", "preview": "Chegara +1"},
    {"n": 64, "key": "b3161c2e3", "kind": "button", "preview": "Chegara \u22121"},
    {"n": 65, "key": "b61f05eae", "kind": "button", "preview": "Bazaviy +1"},
    {"n": 66, "key": "b08162977", "kind": "button", "preview": "Bazaviy \u22121"},
    {"n": 67, "key": "m87e22d63", "kind": "message", "preview": "\u274c Xabaringiz superadmin tomonidan rad etildi."},
    {"n": 68, "key": "m93d04d03", "kind": "message", "preview": "\u2705 Xabaringiz superadmin tomonidan tasdiqlandi va {sent} ta adminga ..."},
    {"n": 69, "key": "m90b944ef", "kind": "message", "preview": "\u2709\ufe0f <b>{pending[\'from_label\']}</b> dan xabar:\n\n{pending[\'text\']}"},
    {"n": 70, "key": "bb589faa0", "kind": "button", "preview": "\u2b05\ufe0f Admin panel"},
    {"n": 71, "key": "bd71ea228", "kind": "button", "preview": "\u27a1\ufe0f"},
    {"n": 72, "key": "bcd46a3e3", "kind": "button", "preview": "\u2b05\ufe0f"},
    {"n": 73, "key": "bf7c0ed06", "kind": "button", "preview": "\U0001f4e2 {info.get(\'title\', cid)}"},
    {"n": 74, "key": "b252ed856", "kind": "button", "preview": "\U0001f468\u200d\U0001f469\u200d\U0001f467 {info.get(\'title\', gid)}"},
    {"n": 75, "key": "c6eeeda11", "kind": "caption", "preview": "\U0001f4e4 Foydalanuvchilar eksporti (CSV)."},
    {"n": 76, "key": "b59c34c96", "kind": "button", "preview": "{i}. {user_label(uid)}"},
    {"n": 77, "key": "mfeb46424", "kind": "message", "preview": "Taklif havolasini yaratib bo\'lmadi \u2014 bot shu chatda admin ekanini t..."},
    {"n": 78, "key": "m18725027", "kind": "message", "preview": "\U0001f517 Eslatma: Telegram Bot API orqali botning sizni majburan a\'zo qili..."},
    {"n": 79, "key": "b15ab527e", "kind": "button", "preview": "\u2b05\ufe0f Kanallar"},
    {"n": 80, "key": "be87a5a3a", "kind": "button", "preview": "\U0001f517 Meni taklif qil (invite link)"},
    {"n": 81, "key": "bdb98e9c3", "kind": "button", "preview": "\u2b05\ufe0f Guruhlar"},
    {"n": 82, "key": "b32669fe9", "kind": "button", "preview": "\u2b05\ufe0f Foydalanuvchilar"},
    {"n": 83, "key": "b14464cfa", "kind": "button", "preview": "\U0001f4ac Unga yozish"},
    {"n": 84, "key": "b454b1f68", "kind": "button", "preview": "\u2795 Limit berish"},
    {"n": 85, "key": "b56e75613", "kind": "button", "preview": "\U0001f4e6 Pack ({counts.get(\'pack\', 0)})"},
    {"n": 86, "key": "bad705d3d", "kind": "button", "preview": "\U0001f39e GIF ({counts.get(\'gif\', 0)})"},
    {"n": 87, "key": "b567bab70", "kind": "button", "preview": "\U0001f600 Emoji ({counts.get(\'emoji\', 0)})"},
    {"n": 88, "key": "b5295f72b", "kind": "button", "preview": "\U0001f5bc Sticker ({counts.get(\'sticker\', 0)})"},
    {"n": 89, "key": "bf81f97b5", "kind": "button", "preview": "\U0001f4cb Matn - to\'liq"},
    {"n": 90, "key": "b21300db4", "kind": "button", "preview": "\U0001f5bc Avval stiker, keyin ID\'si"},
    {"n": 91, "key": "b834c0729", "kind": "button", "preview": "\U0001f4c4 Txt fayl qilib jo\'natish"},
    {"n": 92, "key": "b760488f3", "kind": "button", "preview": "\U0001f4dd Matn qilib chatga jo\'natish"},
    {"n": 93, "key": "b88dedf35", "kind": "button", "preview": "\u2b05\ufe0f Reyting"},
    {"n": 94, "key": "ba5ddc162", "kind": "button", "preview": "\u2b05\ufe0f Bosh menyu"},
    {"n": 95, "key": "bd31f81a3", "kind": "button", "preview": "{i}. {user_label(uid)} \u2014 {refs} ta referal"},
    {"n": 96, "key": "b5423ca44", "kind": "button", "preview": "\u2b05\ufe0f Kalitlarim"},
    {"n": 97, "key": "bb9b933b9", "kind": "button", "preview": "\U0001f5d1 O\'chirish"},
    {"n": 98, "key": "b27a4b97f", "kind": "button", "preview": "\U0001f310 Har qanday xabarga (default javob)"},
    {"n": 99, "key": "b4543c5f2", "kind": "button", "preview": "\U0001f3af Aniq so\'z/ibora bo\'yicha"},
    {"n": 100, "key": "ba196826a", "kind": "button", "preview": "\u23f1 Javob kechikishini sozlash (offline)"},
    {"n": 101, "key": "bc59d32a2", "kind": "button", "preview": "\u2b50 Premium olish (cheksiz)"},
    {"n": 102, "key": "b0cea0117", "kind": "button", "preview": "\u2795 Yangi kalit qo\'shish"},
    {"n": 103, "key": "bbb72fe44", "kind": "button", "preview": "\U0001f5d1"},
    {"n": 104, "key": "b726d87a2", "kind": "button", "preview": "\u2b50 100 Stars uchun sotib olish"},
    {"n": 105, "key": "b8ad57f28", "kind": "button", "preview": "\U0001f4ac Foydalanuvchiga yozish"},
    {"n": 106, "key": "b9f0f8ac0", "kind": "button", "preview": "\u270d\ufe0f Adminlarga xabar"},
    {"n": 107, "key": "bdc831b88", "kind": "button", "preview": "\U0001f4e3 Broadcast"},
    {"n": 108, "key": "b091eaf7f", "kind": "button", "preview": "\U0001f916 Bot admin joylar"},
    {"n": 109, "key": "ba354ef5b", "kind": "button", "preview": "\U0001f4e4 Eksport (CSV)"},
    {"n": 110, "key": "bcbd1d6da", "kind": "button", "preview": "\U0001f3c6 Referal reyting"},
    {"n": 111, "key": "bc08459b5", "kind": "button", "preview": "\u2728 Bot imzosi (premium emoji)"},
    {"n": 112, "key": "be8297373", "kind": "button", "preview": "\u26a1 Reaksiya emoji"},
    {"n": 113, "key": "b89e7c902", "kind": "button", "preview": "\U0001f6e1 Adminlar"},
    {"n": 114, "key": "b946559cc", "kind": "button", "preview": "\u2699\ufe0f Limit sozlamalari"},
    {"n": 115, "key": "be45e0721", "kind": "button", "preview": "\U0001f381 Bonus kanallar"},
    {"n": 116, "key": "b4f6111d5", "kind": "button", "preview": "\U0001f512 Majburiy kanallar"},
    {"n": 117, "key": "b2c5ffb7a", "kind": "button", "preview": "\U0001f4e2 Kanallar"},
    {"n": 118, "key": "b0fb982b7", "kind": "button", "preview": "\U0001f468\u200d\U0001f469\u200d\U0001f467 Guruhlar"},
    {"n": 119, "key": "bc10128d4", "kind": "button", "preview": "\U0001f465 Foydalanuvchilar"},
    {"n": 120, "key": "b2419753d", "kind": "button", "preview": "\U0001f511 Avto-javob (Business)"},
    {"n": 121, "key": "b71db05db", "kind": "button", "preview": "\U0001f3c6 Reyting"},
    {"n": 122, "key": "b7f830788", "kind": "button", "preview": "\u2753 Yordam"},
    {"n": 123, "key": "bef2977c4", "kind": "button", "preview": "\u2b50 Premium"},
    {"n": 124, "key": "b02fb8090", "kind": "button", "preview": "\U0001f381 Bonus"},
    {"n": 125, "key": "bb48040e8", "kind": "button", "preview": "\U0001f4ca Limitim"},
    {"n": 126, "key": "b2fe74690", "kind": "button", "preview": "\U0001f517 Referal"},
    {"n": 127, "key": "bafbc3e7e", "kind": "button", "preview": "\U0001f4e6 Pack yuklab olish"},
    {"n": 128, "key": "cc09ad5b6", "kind": "caption", "preview": "{requester_info} yuklagan GIF"},
    {"n": 129, "key": "cc16f75b4", "kind": "caption", "preview": "Faylni ochish uchun ZIP\'ni yeching."},
    {"n": 130, "key": "md07b6802", "kind": "message", "preview": "Faylni olishda xato yuz berdi."},
    {"n": 131, "key": "m723cc1c8", "kind": "message", "preview": "Bu xabarda GIF/animatsiya topilmadi."},
    {"n": 132, "key": "c4726f6ec", "kind": "caption", "preview": "{pending[\'requester_info\']} yuklagan sticker"},
    {"n": 133, "key": "c7c1e2f8f", "kind": "caption", "preview": "{requester_info} yuklagan sticker"},
    {"n": 134, "key": "md7cb107b", "kind": "message", "preview": "Bu xabarda sticker/custom emoji topilmadi."},
    {"n": 135, "key": "cf6890d75", "kind": "caption", "preview": "{pending[\'requester_info\']} \u2014 webm GIF"},
    {"n": 136, "key": "c43f9af6c", "kind": "caption", "preview": "\U0001f39e WebM tayyor."},
    {"n": 137, "key": "m5de9a7ba", "kind": "message", "preview": "GIF\'ni webm\'ga o\'girishda xato yuz berdi. Qaytadan urinib ko\'ring."},
    {"n": 138, "key": "mb55c4ee6", "kind": "message", "preview": "Pack topilmadi. Nomini tekshiring."},
    {"n": 139, "key": "m82174114", "kind": "message", "preview": "\U0001f4e6 {pack_name} \u2014 {len(stickers)} ta element, birma-bir yuboryapman..."},
    {"n": 140, "key": "mfc20648e", "kind": "message", "preview": "Bu pack bo\'sh ko\'rinadi."},
    {"n": 141, "key": "m1949e217", "kind": "message", "preview": "{placeholder} (jonli ko\'rinishni yubora olmadim \u2014 bot egasida Teleg..."},
    {"n": 142, "key": "mb78187fa", "kind": "message", "preview": "\u26a0\ufe0f Jonli emoji ko\'rsatib bo\'lmadi \u2014 bot egasida Telegram Premium bo..."},
    {"n": 143, "key": "c8c76f66b", "kind": "caption", "preview": "{pack_name} \u2014 barcha ID\'lar"},
    {"n": 144, "key": "cd6ab52a7", "kind": "caption", "preview": "{requester_info} so\'ragan pack: {pack_name} ({result} ta fayl)"},
    {"n": 145, "key": "c1f84b9b9", "kind": "caption", "preview": "{result} ta fayl topildi."},
    {"n": 146, "key": "c2f0992e5", "kind": "caption", "preview": "{requester_info} so\'ragan pack: {pack_name} (kesh)"},
    {"n": 147, "key": "cfc37bec3", "kind": "caption", "preview": "{cached[\'sticker_count\']} ta fayl topildi. (kesh)"},
    {"n": 148, "key": "m3b80c09e", "kind": "message", "preview": "\'{pack_name}\' qidirilmoqda, kuting..."},
    {"n": 149, "key": "c5f48678c", "kind": "caption", "preview": "{requester_info} \u2014 {filename}"},
    {"n": 150, "key": "mdb26262a", "kind": "message", "preview": "Bu pack\'da {len(stickers)} ta element bor. 1 dan {len(stickers)} ga..."},
    {"n": 151, "key": "mf6fef6fa", "kind": "message", "preview": "Pack topilmadi. Nomini/havolani tekshiring."},
    {"n": 152, "key": "m4aad1f7a", "kind": "message", "preview": "\U0001f389 Sizning referal havolangiz orqali yangi foydalanuvchi qo\'shildi!\n..."},
    {"n": 153, "key": "bf4520fe4", "kind": "button", "preview": "Tekshirish"},
    {"n": 154, "key": "m89eab3cf", "kind": "message", "preview": "\U0001f512 Botdan foydalanishdan oldin quyidagi kanal(lar)ga a\'zo bo\'ling, s..."},
    {"n": 155, "key": "b813422c1", "kind": "button", "preview": "\u2705 A\'zo bo\'ldim, tekshirish"},
]

app = Flask(__name__)

BOT_ID = None
BOT_USERNAME = None
_pinned_message_id = None
_state_dirty = False  # save_state_locked() True qiladi; flusher thread yozib False qiladi

# Barcha STATE (persistent, guruhga pinned JSON orqali saqlanadi) ni
# o'qish/yozish shu RLock ostida bajariladi. RLock chunki ba'zi funksiyalar
# bir-birini chaqiradi (masalan ensure_period_reset ichida save_state()).
_state_lock = threading.RLock()

# Vaqtinchalik (persistent bo'lmagan) holatlar — process qayta ishga
# tushganda yo'qolishi mumkin, bu qabul qilinadi (forward qilingan
# stiker/fayl tanlovlari — foydalanuvchi shunchaki qayta forward qiladi):
_pending_choices = {}      # forward qilingan stiker uchun "pack/single" tanlovi
_pending_lock = threading.Lock()


def store_pending_choice(payload):
    token = uuid.uuid4().hex[:10]
    with _pending_lock:
        _pending_choices[token] = payload
    return token


def pop_pending_choice(token):
    with _pending_lock:
        return _pending_choices.pop(token, None)


# _pending_input ESA endi STATE (persistent) ichida saqlanadi —
# chunki bu ko'p bosqichli oqimlar (masalan Stars to'lash, publish
# sarlavha/tur/emoji so'rash) orasida Render kabi platformalarda process
# "sleep"ga ketib qayta uyg'onishi (yoki deploy) sodir bo'lishi mumkin;
# RAM'da saqlansa, foydalanuvchi bosqich o'rtasida "yo'qoladi" va hech
# qanday javob ololmay qoladi. STATE["pending_input"] shaklida saqlanadi.


def set_pending_input(user_id, action, data=None):
    with _state_lock:
        STATE.setdefault("pending_input", {})[str(user_id)] = {"action": action, "data": data or {}}
        save_state_locked()
        log.info("set_pending_input: user=%s action=%s", user_id, action)


def get_pending_input(user_id):
    with _state_lock:
        return STATE.get("pending_input", {}).get(str(user_id))


def clear_pending_input(user_id):
    with _state_lock:
        STATE.setdefault("pending_input", {}).pop(str(user_id), None)
        save_state_locked()


# ---------- Telegram API helper funksiyalar ----------

# Har bir so'rovda yangi TCP/TLS ulanish ochmaslik uchun global Session (connection pooling).
# Parallel yuklashlar (ThreadPoolExecutor) bilan birga bu tezlikni sezilarli oshiradi.
_http_session = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=32,
    pool_maxsize=32,
    max_retries=requests.adapters.Retry(
        total=2, backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
    ),
)
_http_session.mount("https://", _http_adapter)
_http_session.mount("http://", _http_adapter)


def tg_call(method, **params):
    try:
        resp = _http_session.post(f"{API_BASE}/{method}", json=params, timeout=30)
        data = resp.json()
    except Exception as e:
        log.error("tg_call tarmoq/parsing xatosi (%s): %s", method, e)
        return {"ok": False, "error": str(e)}
    if not data.get("ok"):
        log.error("Telegram xato (%s): %s", method, data)
    return data


def run_safe_thread(target, *args, chat_id=None, reply_to=None, business_connection_id=None, **kwargs):
    """threading.Thread(daemon=True) o'rniga ishlatiladi: agar target funksiya
    ichida kutilmagan exception chiqsa, thread jimgina o'lib qolmaydi —
    xato loglanadi, adminga xabar boradi va (chat_id berilgan bo'lsa)
    foydalanuvchiga ham "xatolik" xabari yuboriladi (aks holda foydalanuvchi
    'qidirilmoqda...' xabaridan keyin abadiy javobsiz qolib ketardi)."""
    def _wrapped():
        try:
            target(*args, **kwargs)
        except Exception as e:
            fn_name = getattr(target, "__name__", str(target))
            log.exception("Thread xatosi (%s): %s", fn_name, e)
            try:
                notify_admin_error(f"Fon jarayoni ({fn_name})", extra=f"chat_id={chat_id}: {e}")
            except Exception:
                pass
            if chat_id is not None:
                try:
                    send_message(chat_id, "Xatolik yuz berdi, birozdan keyin qayta urinib ko'ring.",
                                 reply_to=reply_to, business_connection_id=business_connection_id)
                except Exception:
                    pass
    threading.Thread(target=_wrapped, daemon=True).start()


def send_message(chat_id, text, reply_to=None, parse_mode_html=False, reply_markup=None,
                  business_connection_id=None, entities=None, add_signature=True,
                  decoration_key=None, _retry_plain=False):
    send_text, send_entities = text, entities
    used_extra = False

    if not entities and not _retry_plain:
        key = decoration_key or _auto_text_key(text)
        dtext, dentities = decorate_text(key, text)
        if dentities:
            send_text, send_entities = dtext, dentities
            used_extra = True

    sig_id = None
    if not _retry_plain:
        sig_id = get_signature_emoji() if (add_signature and not send_entities) else None
    if sig_id:
        placeholder = get_signature_placeholder()
        if parse_mode_html:
            send_text = f'{send_text} <tg-emoji emoji-id="{html.escape(sig_id, quote=True)}">{html.escape(placeholder, quote=False)}</tg-emoji>'
        else:
            base_len = utf16_len(send_text)
            send_text = f"{send_text} {placeholder}"
            send_entities = [{"type": "custom_emoji", "offset": base_len + 1,
                               "length": utf16_len(placeholder), "custom_emoji_id": sig_id}]
        used_extra = True

    params = {"chat_id": chat_id, "text": send_text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    if send_entities:
        params["entities"] = send_entities  # entities va parse_mode birga bo'lmaydi
    elif parse_mode_html:
        params["parse_mode"] = "HTML"
    if reply_markup:
        params["reply_markup"] = reply_markup if _retry_plain else apply_button_icons(reply_markup)
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    result = tg_call("sendMessage", **params)

    if used_extra and not (result and result.get("ok")):
        return send_message(chat_id, text, reply_to=reply_to, parse_mode_html=parse_mode_html,
                             reply_markup=reply_markup, business_connection_id=business_connection_id,
                             entities=entities, add_signature=False, _retry_plain=True)
    return result


def edit_message_text(chat_id, message_id, text, parse_mode_html=False, reply_markup=None,
                       decoration_key=None, _retry_plain=False):
    send_text, send_entities = text, None
    if not _retry_plain:
        key = decoration_key or _auto_text_key(text)
        dtext, dentities = decorate_text(key, text)
        if dentities:
            send_text, send_entities = dtext, dentities

    params = {"chat_id": chat_id, "message_id": message_id, "text": send_text}
    if send_entities:
        params["entities"] = send_entities
    elif parse_mode_html:
        params["parse_mode"] = "HTML"
    if reply_markup:
        params["reply_markup"] = reply_markup if _retry_plain else apply_button_icons(reply_markup)
    result = tg_call("editMessageText", **params)

    if send_entities and not (result and result.get("ok")):
        return edit_message_text(chat_id, message_id, text, parse_mode_html=parse_mode_html,
                                  reply_markup=reply_markup, _retry_plain=True)
    return result


def safe_edit_or_send(chat_id, message_id, text, parse_mode_html=False, reply_markup=None, decoration_key=None):
    result = edit_message_text(chat_id, message_id, text, parse_mode_html=parse_mode_html,
                                reply_markup=reply_markup, decoration_key=decoration_key)
    if not result.get("ok"):
        send_message(chat_id, text, parse_mode_html=parse_mode_html, reply_markup=reply_markup,
                      decoration_key=decoration_key)


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    params["show_alert"] = show_alert
    return tg_call("answerCallbackQuery", **params)


def send_document_bytes(chat_id, filename, file_bytes, caption=None, business_connection_id=None,
                         caption_entities=None, decoration_key=None):
    files = {"document": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        send_caption, send_ents = caption, caption_entities
        if not caption_entities:
            key = decoration_key or _auto_text_key(caption)
            dtext, dents = decorate_text(key, caption)
            if dents:
                send_caption, send_ents = dtext, dents
        payload["caption"] = send_caption
        if send_ents:
            payload["caption_entities"] = json.dumps(send_ents)
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = _http_session.post(f"{API_BASE}/sendDocument", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendDocument javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendDocument xato: %s", data)
    return data


def send_video_bytes(chat_id, filename, file_bytes, caption=None, business_connection_id=None,
                      caption_entities=None, decoration_key=None):
    """.webm formatdagi custom emoji/stikerlarni video sifatida (fayl emas) yuboradi,
    shunda Telegram uni ichkarida ijro etadi."""
    files = {"video": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        send_caption, send_ents = caption, caption_entities
        if not caption_entities:
            key = decoration_key or _auto_text_key(caption)
            dtext, dents = decorate_text(key, caption)
            if dents:
                send_caption, send_ents = dtext, dents
        payload["caption"] = send_caption
        if send_ents:
            payload["caption_entities"] = json.dumps(send_ents)
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = _http_session.post(f"{API_BASE}/sendVideo", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendVideo javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendVideo xato: %s", data)
    return data


def send_animation_bytes(chat_id, filename, file_bytes, caption=None, business_connection_id=None,
                          caption_entities=None, decoration_key=None):
    """.webm formatdagi custom emoji/stikerlarni animatsiya sifatida (GIF kabi) yuboradi —
    bu qisqa, tovushsiz, halqali cliplar uchun sendVideo'dan ko'ra to'g'ri usul,
    Telegram uni ichkarida halqali ijro etadi, fayl sifatida ko'rsatmaydi."""
    files = {"animation": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if caption:
        send_caption, send_ents = caption, caption_entities
        if not caption_entities:
            key = decoration_key or _auto_text_key(caption)
            dtext, dents = decorate_text(key, caption)
            if dents:
                send_caption, send_ents = dtext, dents
        payload["caption"] = send_caption
        if send_ents:
            payload["caption_entities"] = json.dumps(send_ents)
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = _http_session.post(f"{API_BASE}/sendAnimation", data=payload, files=files, timeout=60)
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


def send_sticker_bytes(chat_id, filename, file_bytes, business_connection_id=None):
    """Video-sticker (.webm) baytlarini sendSticker orqali multipart
    yuboradi — shu tarzda Telegram uni HAQIQIY sticker sifatida
    tan oladi va foydalanuvchiga '➕ to'plamga qo'shish' tugmasini
    ko'rsatadi (sendDocument bilan bunday bo'lmaydi)."""
    files = {"sticker": (filename, file_bytes)}
    payload = {"chat_id": chat_id}
    if business_connection_id:
        payload["business_connection_id"] = business_connection_id
    resp = _http_session.post(f"{API_BASE}/sendSticker", data=payload, files=files, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        log.error("sendSticker (bytes) javobi JSON emas: %s", resp.text[:300])
        return None
    if not data.get("ok"):
        log.error("sendSticker (bytes) xato: %s", data)
    return data


def send_document_by_file_id(chat_id, file_id, caption=None, business_connection_id=None,
                              caption_entities=None, decoration_key=None):
    params = {"chat_id": chat_id, "document": file_id}
    if caption:
        send_caption, send_ents = caption, caption_entities
        if not caption_entities:
            key = decoration_key or _auto_text_key(caption)
            dtext, dents = decorate_text(key, caption)
            if dents:
                send_caption, send_ents = dtext, dents
        params["caption"] = send_caption
        if send_ents:
            params["caption_entities"] = send_ents  # tg_call json= orqali yuboradi, dumps kerak emas
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return tg_call("sendDocument", **params)


def notify_admin(text, reply_markup=None):
    if SUPERADMIN_ID:
        send_message(SUPERADMIN_ID, text, reply_markup=reply_markup)


def dm_button_for_user(user_id):
    """Adminga yuboriladigan log/xatolik xabarlariga qo'shiladigan tezkor
    'Foydalanuvchiga yozish' tugmasi — mavjud dm_start:{uid} oqimidan foydalanadi."""
    if user_id is None:
        return None
    return {"inline_keyboard": [[
        {"text": "💬 Foydalanuvchiga yozish", "callback_data": f"dm_start:{user_id}"}
    ]]}


def user_label_for_admin(user_id):
    """Adminga xabar berishda foydalanuvchini aniqlash uchun qulay yorliq
    ('@username (id:123)' yoki 'Ism (id:123)' yoki oddiy 'id:123')."""
    if user_id is None:
        return "noma'lum"
    try:
        record = get_user_record(user_id)
    except Exception:
        record = {}
    username = record.get("username")
    first_name = record.get("first_name")
    if username:
        return f"@{username} (id:{user_id})"
    if first_name:
        return f"{first_name} (id:{user_id})"
    return f"id:{user_id}"


def notify_admin_error(action, user_id=None, extra=""):
    """Botning ISTALGAN joyida xato/muvaffaqiyatsizlik yuz berganda
    superadminga izchil formatda xabar yuboradi: KIM, QACHON, NIMA
    qilayotganda xato chiqdi. Har doim shu formatdan foydalaning —
    tarqoq/formatlanmagan notify_admin() chaqiruvlariga alternativ."""
    when = datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d %H:%M:%S")  # Asia/Tashkent
    who = user_label_for_admin(user_id)
    text = f"🔥 Xatolik\nKimda: {who}\nQachon: {when} (UZ vaqti)\nAmal: {action}"
    if extra:
        text += f"\nTafsilot: {extra}"
    notify_admin(text, reply_markup=dm_button_for_user(user_id))


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


# ---------- Guruh moderatsiyasi (.del/.ban/.mute/.kick) uchun huquq tekshiruvi ----------

def is_group_owner(chat_id, user_id):
    return get_chat_member_status(chat_id, user_id) == "creator"


def is_group_admin_or_owner(chat_id, user_id):
    return get_chat_member_status(chat_id, user_id) in ("administrator", "creator")


def can_moderate_group(chat_id, user_id):
    """.del/.ban/.mute/.kick buyruqlarini kim ishlata oladi:
    bot superadmini, bot admini, guruh admini yoki guruh egasi."""
    if is_admin(user_id):
        return True
    return is_group_admin_or_owner(chat_id, user_id)


_DURATION_UNITS = ("soniya", "daqiqa", "soat", "kun", "oy")
_DURATION_SECONDS = (1, 60, 3600, 86400, 2592000)  # oy ~ 30 kun


def parse_mute_duration(parts):
    """[soniya, daqiqa, soat, kun, oy] tartibidagi 5 ta sonni umumiy soniyaga aylantiradi.
    Noto'g'ri format bo'lsa None qaytaradi."""
    if len(parts) != 5:
        return None
    total = 0
    for value, mult in zip(parts, _DURATION_SECONDS):
        try:
            n = int(value)
        except ValueError:
            return None
        if n < 0:
            return None
        total += n * mult
    return total if total > 0 else None


def resolve_target_user(chat_id, reply, args_text):
    """Moderatsiya buyrug'i uchun nishon userni aniqlaydi:
    1) reply qilingan xabar bo'lsa — o'sha xabar egasi
    2) bo'lmasa, args_text ichidan @username yoki raqamli ID qidiriladi (faqat botga
       ma'lum bo'lgan userlar orasidan — Bot API begona username'ni ID'ga aylantira olmaydi)
    Qaytaradi: (user_id, label) yoki (None, xato_matni)
    """
    if reply and reply.get("from"):
        return reply["from"]["id"], requester_label(reply["from"])

    token = (args_text or "").strip().split()[0] if (args_text or "").strip() else ""
    if not token:
        return None, "Xabarga reply qiling yoki username/ID ko'rsating."

    if token.lstrip("-").isdigit():
        target_id = int(token)
        return target_id, user_label(target_id)

    uname = token.lstrip("@").lower()
    with _state_lock:
        known_ids = list(STATE.get("known_users", []))
    for uid in known_ids:
        rec = get_user_record(uid)
        if (rec.get("username") or "").lower() == uname:
            return uid, user_label(uid)
    return None, f"@{uname} — botga tanish emas (u bot bilan hech gaplashmagan bo'lishi mumkin)."


# ---------- DB (Telegram guruh + pinned xabar orqali) ----------

def default_state():
    return {
        "admins": [],
        "users": {},
        "known_users": [],
        # Bot qo'shilgan guruh/kanallar kuzatuvi:
        "groups": {},    # {chat_id_str: {"title","type","added_at"}}
        "channels": {},  # {chat_id_str: {"title","username","added_at"}}
        "reak_modes": {},  # {chat_id_str: {"emoji","set_by","paid","set_at"}} — /reak mode: on holati
        "reak_pending": {},  # {str(user_id): {"chat_id","group_message_id","free"}} — invoice/emoji tanlash oralig'i
        "hack_mode": False,  # global: bot admin/superadmin moderatsiya buyruqlari iz qoldirmasin
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
        "processed_payments": [],  # [telegram_payment_charge_id, ...] — dublikat to'lovni oldini olish uchun
        "userbot_sessions": {},  # {str(admin_id): {"api_id","api_hash","phone","session_string","connected_at"}}
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
        "stars_wallet": 0,  # foydalanuvchining bot ichidagi Stars hamyoni (publish va h.k. uchun)
        "last_message_id": None,  # "User qidirish" panelida forward qilish uchun
        "last_chat_id": None,
    }


def get_file_path(file_id):
    data = tg_call("getFile", file_id=file_id)
    if data.get("ok"):
        return data["result"]["file_path"]
    return None


def download_file_bytes(file_path):
    resp = _http_session.get(f"{FILE_BASE}/{file_path}", timeout=60)
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
    for key in ("groups", "channels", "reak_modes", "reak_pending"):
        merged.setdefault(key, {})
    merged.setdefault("processed_payments", [])
    merged.setdefault("hack_mode", False)
    merged.setdefault("userbot_sessions", {})
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
    try:
        payload = json.dumps(STATE, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as e:
        # STATE ichiga JSON-mos bo'lmagan narsa (masalan xom bytes) tushib
        # qolgan bo'lishi mumkin — bu butun saqlash zanjirini abadiy
        # to'xtatib qo'ymasligi kerak, shuning uchun aniq log bilan
        # to'xtaymiz (chaqiruvchi False ko'rib qayta uradi, lekin ildiz
        # sababi log'da ko'rinadi va tuzatish oson bo'ladi).
        log.error("STATE JSON-serialize qilib bo'lmadi (STATE buzilgan bo'lishi mumkin): %s", e)
        return False, {}
    files = {"document": ("state.json", payload, "application/json")}
    if message_id:
        media = json.dumps({"type": "document", "media": "attach://document"})
        form = {"chat_id": DB_GROUP_ID, "message_id": message_id, "media": media}
        resp = _http_session.post(f"{API_BASE}/editMessageMedia", data=form, files=files, timeout=30)
    else:
        form = {"chat_id": DB_GROUP_ID}
        resp = _http_session.post(f"{API_BASE}/sendDocument", data=form, files=files, timeout=30)
    try:
        result = resp.json()
    except ValueError:
        log.error("STATE yuklashda JSON bo'lmagan javob: %s", resp.text[:300])
        return False, {}
    return result.get("ok", False), result


def save_state_locked():
    """_state_lock ALLAQACHON ushlangan holatda chaqirilishi kerak.
    DIQQAT: bu funksiya endi Telegramga DARHOL yozmaydi — faqat STATE'ni
    'dirty' (o'zgargan) deb belgilaydi. Haqiqiy yozish fonda ishlaydigan
    _state_flusher_loop orqali debounce qilinib (SAVE_DEBOUNCE_SECONDS
    oralig'ida bittalashtirilib) amalga oshiriladi. Bu bir nechta ketma-ket
    tez o'zgarishlarni (masalan parallel so'rovlar) bitta HTTP yozuvga
    birlashtiradi va botni sezilarli tezlashtiradi."""
    global _state_dirty
    _state_dirty = True


def _flush_state_now_locked():
    """STATE'ni HAQIQATDA Telegramga yozadi. _state_lock ALLAQACHON
    ushlangan holatda chaqirilishi kerak."""
    global _pinned_message_id, _state_dirty
    if _pinned_message_id:
        ok, result = _upload_state_document(message_id=_pinned_message_id)
        if ok:
            log.info("STATE saqlandi (edit, message_id=%s)", _pinned_message_id)
            _state_dirty = False
            return
        log.warning("editMessageMedia muvaffaqiyatsiz (%s), yangi fayl yuboriladi.", result)

    ok, result = _upload_state_document()
    if ok:
        _pinned_message_id = result["result"]["message_id"]
        tg_call("pinChatMessage", chat_id=DB_GROUP_ID, message_id=_pinned_message_id, disable_notification=True)
        log.info("STATE saqlandi (yangi fayl, message_id=%s)", _pinned_message_id)
        _state_dirty = False
    else:
        log.error("STATE saqlashda xato: %s", result)
        # _state_dirty=True qoladi — keyingi flush urinishida qayta uriniladi.


SAVE_DEBOUNCE_SECONDS = 2.0


def _state_flusher_loop():
    """Fon thread: har SAVE_DEBOUNCE_SECONDS da bir marta, agar STATE
    o'zgargan bo'lsa (dirty), uni Telegramga yozadi. Shu tarzda bir necha
    o'nlab tez ketma-ket o'zgarish bitta HTTP yozuvga birlashadi."""
    while True:
        time.sleep(SAVE_DEBOUNCE_SECONDS)
        try:
            with _state_lock:
                if _state_dirty:
                    _flush_state_now_locked()
        except Exception as e:
            log.exception("State flusher xatosi: %s", e)


def force_flush_state():
    """STATE o'zgargan bo'lsa, kutmasdan DARHOL yozadi. Superadmin
    panelidan chiqishdan oldin yoki kritik amallardan keyin ishlatiladi —
    masalan foydalanuvchiga darhol ko'rinishi kerak bo'lgan holatlarda."""
    with _state_lock:
        if _state_dirty:
            _flush_state_now_locked()


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
        cfg.setdefault("premium_price_stars", 300)
        cfg.setdefault("premium_days", 182)
        cfg.setdefault("stars_per_limit", 1)  # 1 Star = shuncha ta limit
        cfg.setdefault("publish_price_stars", 1)  # 1 marta publish qilish narxi (Stars)
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


# ================= Har bir matn/tugma uchun alohida premium emoji =================
def _auto_text_key(text):
    return "m" + hashlib.sha256(("message::" + text).encode("utf-8")).hexdigest()[:8]


def _auto_button_key(text):
    return "b" + hashlib.sha256(("button::" + text).encode("utf-8")).hexdigest()[:8]


def get_text_decoration(key):
    return STATE.get("text_decorations", {}).get(key)


def set_text_decoration(key, custom_emoji_id, position="end", placeholder="✨"):
    with _state_lock:
        STATE.setdefault("text_decorations", {})[key] = {
            "custom_emoji_id": custom_emoji_id, "position": position, "placeholder": placeholder,
        }
        save_state_locked()


def clear_text_decoration(key):
    with _state_lock:
        removed = STATE.setdefault("text_decorations", {}).pop(key, None) is not None
        save_state_locked()
        return removed




def decorate_text(key, text):
    deco = get_text_decoration(key)
    if not deco or not deco.get("custom_emoji_id"):
        return text, None
    placeholder = deco.get("placeholder") or "✨"
    cid = deco["custom_emoji_id"]
    if deco.get("position") == "start":
        new_text = f"{placeholder} {text}"
        offset = 0
    else:
        new_text = f"{text} {placeholder}"
        offset = utf16_len(text) + 1
    entities = [{"type": "custom_emoji", "offset": offset, "length": utf16_len(placeholder), "custom_emoji_id": cid}]
    return new_text, entities


def apply_button_icons(reply_markup):
    if not reply_markup or not reply_markup.get("inline_keyboard"):
        return reply_markup
    for row in reply_markup["inline_keyboard"]:
        for btn in row:
            if "icon_custom_emoji_id" in btn or not btn.get("text"):
                continue
            key = btn.pop("_deco_key", None) or _auto_button_key(btn["text"])
            deco = get_text_decoration(key)
            if deco and deco.get("custom_emoji_id"):
                btn["icon_custom_emoji_id"] = deco["custom_emoji_id"]
    return reply_markup


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


def set_config_value(key, value):
    with _state_lock:
        STATE.setdefault("config", {})[key] = value
        save_state_locked()
        _flush_state_now_locked()  # narx/limit sozlamasi — kutmasdan darhol yoziladi


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


# ---------- Userbot (Telethon/MTProto) backup — FAQAT admin/superadmin ----------
# ESKI YECHIM (Bot API orqali forward qilib yig'ish) OLIB TASHLANDI — u faqat
# "yoqilgandan keyingi" xabarlarni ko'ra olardi va bot+admin ikkalasi ham
# o'sha chatda admin bo'lishini talab qilardi (Bot API cheklovi).
#
# YANGI YECHIM: admin o'z shaxsiy Telegram akkountini (my.telegram.org'dan
# olingan API_ID/API_HASH + telefon raqami orqali) botga ulaydi. Shundan
# keyin bot Telethon (MTProto) orqali o'sha akkount a'zo bo'lgan istalgan
# chatning TO'LIQ TARIXINI o'qiy oladi — bu Bot API bilan mumkin emas edi.
#
# ⚠️ XAVFSIZLIK: bu — juda kuchli huquq (ulangan akkountga TO'LIQ kirish).
# Shuning uchun bu funksiya FAQAT is_admin() bo'lganlar uchun ochiq.
# Session (STATE ichida, boshqa hamma narsa bilan birga DB guruhida
# saqlanadi) — kimda bo'lsa, o'sha odam ulangan akkountga kira oladi.
# Buni faqat to'liq ishonadigan adminlaringizga bering.

_userbot_loop = asyncio.new_event_loop()


def _userbot_loop_runner():
    asyncio.set_event_loop(_userbot_loop)
    _userbot_loop.run_forever()


threading.Thread(target=_userbot_loop_runner, daemon=True, name="userbot-loop").start()


def run_userbot_coro(coro, timeout=120):
    """Flask (sinxron) handlerlardan Telethon (asinxron) kodini xavfsiz
    chaqirish uchun. Coroutine tugaguncha (yoki timeout bo'lguncha) kutadi."""
    future = asyncio.run_coroutine_threadsafe(coro, _userbot_loop)
    return future.result(timeout=timeout)


# admin_id -> ulangan (authorize qilingan) Telethon client keshi (RAMda)
_userbot_clients = {}
# admin_id -> login jarayoni davomidagi vaqtinchalik holat
# {"client","api_id","api_hash","phone","phone_code_hash"}
_userbot_login_state = {}
# admin_id -> so'nggi ko'rsatilgan dialoglar ro'yxati (RAMda, STATE'ga
# YOZILMAYDI — bu shunchaki UI keshi, persistent state bilan aralashtirib
# uni shishirmaslik kerak, aks holda backup o'zi qayta ixtiro qilgan
# muammoga uchraymiz)
_userbot_dialog_cache = {}


def get_userbot_session(admin_id):
    with _state_lock:
        return STATE.get("userbot_sessions", {}).get(str(admin_id))


def save_userbot_session(admin_id, **fields):
    with _state_lock:
        sessions = STATE.setdefault("userbot_sessions", {})
        rec = sessions.setdefault(str(admin_id), {})
        rec.update(fields)
        save_state_locked()
    force_flush_state()


def delete_userbot_session(admin_id):
    with _state_lock:
        STATE.setdefault("userbot_sessions", {}).pop(str(admin_id), None)
        save_state_locked()
    force_flush_state()
    _userbot_clients.pop(admin_id, None)


async def _get_authorized_client(admin_id):
    """Adminning saqlangan sessiyasidan ulangan Telethon client qaytaradi,
    yoki sessiya yo'q/eskirgan/bekor qilingan bo'lsa None."""
    cached = _userbot_clients.get(admin_id)
    if cached is not None:
        try:
            if not cached.is_connected():
                await cached.connect()
            if cached.is_connected() and await cached.is_user_authorized():
                return cached
        except Exception:
            pass
        _userbot_clients.pop(admin_id, None)

    sess = get_userbot_session(admin_id)
    if not sess or not sess.get("session_string"):
        return None
    client = TelegramClient(
        StringSession(sess["session_string"]), int(sess["api_id"]), sess["api_hash"],
    )
    await client.connect()
    if not await client.is_user_authorized():
        return None
    _userbot_clients[admin_id] = client
    return client


async def _userbot_send_code(admin_id, api_id, api_hash, phone):
    """Yangi (hali autentifikatsiya qilinmagan) client yaratadi va SMS/Telegram
    kod yuborishni so'raydi. Client _userbot_login_state ichida vaqtinchalik
    saqlanadi (keyingi qadam — kodni tasdiqlash — shu clientni ishlatadi)."""
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()
    sent = await client.send_code_request(phone)
    _userbot_login_state[admin_id] = {
        "client": client, "api_id": int(api_id), "api_hash": api_hash,
        "phone": phone, "phone_code_hash": sent.phone_code_hash,
    }


async def _userbot_sign_in_code(admin_id, code):
    """Qaytaradi: 'ok' | 'need_password' — xato bo'lsa exception ko'tariladi
    (chaqiruvchi tomonda ushlanadi)."""
    st = _userbot_login_state.get(admin_id)
    if not st:
        raise RuntimeError("Login sessiyasi topilmadi, qaytadan boshlang.")
    client = st["client"]
    try:
        await client.sign_in(phone=st["phone"], code=code, phone_code_hash=st["phone_code_hash"])
    except SessionPasswordNeededError:
        return "need_password"
    return "ok"


async def _userbot_sign_in_password(admin_id, password):
    st = _userbot_login_state.get(admin_id)
    if not st:
        raise RuntimeError("Login sessiyasi topilmadi, qaytadan boshlang.")
    await st["client"].sign_in(password=password)
    return "ok"


def _userbot_finalize_login(admin_id):
    """Muvaffaqiyatli sign_in'dan keyin: session_string'ni saqlaydi va
    clientni doimiy keshga o'tkazadi (qayta ulanishga hojat qolmaydi)."""
    st = _userbot_login_state.pop(admin_id, None)
    if not st:
        return None
    client = st["client"]
    session_string = client.session.save()
    save_userbot_session(
        admin_id, api_id=st["api_id"], api_hash=st["api_hash"],
        phone=st["phone"], session_string=session_string,
        connected_at=datetime.now(timezone.utc).isoformat(),
    )
    _userbot_clients[admin_id] = client
    return st["phone"]


async def _userbot_list_dialogs(admin_id, limit=80):
    client = await _get_authorized_client(admin_id)
    if client is None:
        return None
    dialogs = await client.get_dialogs(limit=limit)
    out = []
    for d in dialogs:
        out.append({
            "id": d.id,
            "title": (d.name or getattr(d.entity, "title", None) or str(d.id))[:60],
            "is_group": bool(d.is_group),
            "is_channel": bool(d.is_channel) and not d.is_group,
            "is_user": bool(d.is_user),
        })
    return out


USERBOT_BACKUP_RANGES = [
    ("1d", "🕐 Oxirgi 1 kun", 1),
    ("1w", "📅 Oxirgi 1 hafta", 7),
    ("1m", "🗓 Oxirgi 1 oy", 30),
    ("1y", "📆 Oxirgi 1 yil", 365),
    ("all", "♾ Butun tarix", None),
]
USERBOT_MAX_MESSAGES = 20000  # bitta eksportda o'qiladigan maksimal xabar soni (server resurslarini himoya qilish)


async def _userbot_build_backup_zip(admin_id, dialog_id, days):
    """Tanlangan chat/kanal/guruhning TO'LIQ tarixini (yoki so'nggi `days`
    kunini) Telethon orqali o'qib, matn+media bilan ZIP tayyorlaydi.
    Qaytaradi: (BytesIO yoki None, xabarlar soni)."""
    client = await _get_authorized_client(admin_id)
    if client is None:
        return None, 0

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    entries = []
    media_jobs = []
    async for msg in client.iter_messages(dialog_id, reverse=False, limit=USERBOT_MAX_MESSAGES):
        if cutoff and msg.date and msg.date < cutoff:
            break
        sender_label = "?"
        try:
            sender = await msg.get_sender()
            if sender is not None:
                uname = getattr(sender, "username", None)
                fname = getattr(sender, "first_name", "") or ""
                lname = getattr(sender, "last_name", "") or ""
                sender_label = f"@{uname}" if uname else (f"{fname} {lname}".strip() or str(getattr(sender, "id", "?")))
        except Exception:
            pass
        entry = {
            "id": msg.id,
            "date": msg.date.isoformat() if msg.date else None,
            "from": sender_label,
            "text": msg.message or "",
            "media_name": None,
        }
        if msg.media:
            fname = f"media_{msg.id}"
            entry["media_name"] = fname
            media_jobs.append((fname, msg))
        entries.append(entry)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("messages.json", json.dumps(entries, ensure_ascii=False, indent=2))
        for fname, msg in media_jobs:
            try:
                data = await client.download_media(msg, file=bytes)
                if data:
                    zf.writestr(f"media/{fname}", data)
            except Exception as e:
                log.warning("Userbot media yuklashda xato (msg_id=%s): %s", msg.id, e)
    buf.seek(0)
    return buf, len(entries)


def _send_userbot_backup_zip(admin_id, dialog_id, dialog_title, days, chat_id):
    """run_safe_thread orqali fonda ishlaydi — katta chatlarni o'qish
    vaqt olishi mumkin, webhook'ni bloklamaslik uchun."""
    try:
        buf, count = run_userbot_coro(_userbot_build_backup_zip(admin_id, dialog_id, days), timeout=600)
    except Exception as e:
        log.exception("Userbot backup xatosi: %s", e)
        send_message(chat_id, f"❌ Backup olishda xato: {e}")
        return
    if not buf or count == 0:
        send_message(chat_id, "Bu davr uchun hech qanday xabar topilmadi.")
        return
    safe_title = re.sub(r"[^\w\-]+", "_", str(dialog_title))[:40]
    fname = f"backup_{safe_title}.zip"
    send_document_bytes(chat_id, fname, buf.getvalue(), caption=f"🗄 {dialog_title} — {count} ta xabar (API orqali, to'liq tarix)")

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


def get_stars_wallet(user_id):
    return get_user_record(user_id).get("stars_wallet", 0)


def add_to_stars_wallet(user_id, amount):
    """Hamyonga Stars qo'shadi (masalan foydalanuvchi to'lov qilganda)."""
    with _state_lock:
        record = get_user_record(user_id)
        record["stars_wallet"] = record.get("stars_wallet", 0) + amount
        save_state_locked()
        return record["stars_wallet"]


def try_spend_from_wallet(user_id, amount):
    """Hamyondan yechishga urinadi. Yetarli bo'lsa True va yechadi,
    yetmasa False qaytaradi (hech narsa yechilmaydi)."""
    with _state_lock:
        record = get_user_record(user_id)
        if record.get("stars_wallet", 0) < amount:
            return False
        record["stars_wallet"] -= amount
        save_state_locked()
        _flush_state_now_locked()  # pul sarflandi — kutmasdan darhol yoziladi
        return True


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


def grant_premium(user_id, days=None):
    if days is None:
        days = get_limit_config()["premium_days"]
    with _state_lock:
        record = get_user_record(user_id)
        now = datetime.now(timezone.utc).timestamp()
        current_until = record.get("premium_until") or now
        base = max(now, current_until)
        record["premium_until"] = base + days * 86400
        save_state_locked()
        _flush_state_now_locked()  # to'lov/premium — kutmasdan darhol yoziladi
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
    """ATOMIC: tekshirish VA joy band qilish bitta lock ostida bajariladi
    (avval bular alohida edi — ikki parallel so'rov orasida limitdan
    oshib ketish (race condition) mumkin edi). Ruxsat berilsa, count
    DARHOL +1 qilinadi ('reserve'). Agar so'rov keyinchalik muvaffaqiyatsiz
    tugasa (masalan pack topilmasa), chaqiruvchi shart release_request_slot()
    ni chaqirib bandlikni bekor qilishi kerak — aks holda foydalanuvchi
    muvaffaqiyatsiz urinish uchun ham limitidan yeb qo'yadi."""
    with _state_lock:
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
        record["count"] += 1
        save_state_locked()
        return True, None


def release_request_slot(user_id):
    """can_make_request() band qilgan joyni bekor qiladi — so'rov
    keyinchalik muvaffaqiyatsiz tugagan hollarda (pack topilmadi,
    yuborishda xato va h.k.) chaqirilishi kerak, aks holda foydalanuvchi
    behuda limitidan yutqazadi."""
    with _state_lock:
        if is_admin(user_id) or is_premium(user_id):
            return
        record = get_user_record(user_id)
        if record["count"] > 0:
            record["count"] -= 1
        save_state_locked()


def register_request(user_id, kind=None, detail=None):
    """DIQQAT: count endi bu yerda emas, can_make_request() ichida
    atomic tarzda oshiriladi (race condition oldini olish uchun).
    Bu funksiya faqat statistika/tarixni yozadi."""
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
        save_state_locked()


def register_known_user(user_id, from_user=None, message_id=None, chat_id=None):
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
        if message_id is not None:
            record["last_message_id"] = message_id
            record["last_chat_id"] = chat_id
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


# ---------- "User qidirish" paneli (ID orqali va harflab) ----------

def find_user_by_id(target_id):
    """Berilgan ID bo'yicha user recordini qaytaradi (mavjud bo'lsa), aks holda None."""
    with _state_lock:
        known = target_id in STATE.get("known_users", [])
    if not known:
        return None
    return get_user_record(target_id)


def search_users_by_prefix(query, limit=30):
    """username yoki first_name ichida (case-insensitive) berilgan qatorni o'z ichiga
    olgan userlarni qaytaradi: [(user_id, label), ...]. Faqat known_users orasidan qidiradi."""
    q = (query or "").strip().lower()
    if not q:
        return []
    results = []
    with _state_lock:
        known_ids = list(STATE.get("known_users", []))
    for uid in known_ids:
        rec = get_user_record(uid)
        uname = (rec.get("username") or "").lower()
        fname = (rec.get("first_name") or "").lower()
        if q in uname or q in fname:
            results.append((uid, user_label(uid)))
        if len(results) >= limit:
            break
    return results


def user_search_letters_keyboard(collected, results, page=0, page_size=6):
    """A-Z harflar klaviaturasi + hozircha yig'ilgan so'z + topilgan userlar ro'yxati."""
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    letter_rows = []
    row = []
    for i, ch in enumerate(letters, 1):
        row.append({"text": ch, "callback_data": f"usearch_letter:{ch}"})
        if i % 7 == 0:
            letter_rows.append(row)
            row = []
    if row:
        letter_rows.append(row)

    control_row = [{"text": "⌫ O'chirish", "callback_data": "usearch_backspace"}]
    if collected:
        control_row.append({"text": "♻️ Tozalash", "callback_data": "usearch_clear"})
    letter_rows.append(control_row)

    result_rows = []
    start = page * page_size
    chunk = results[start:start + page_size]
    for uid, label in chunk:
        result_rows.append([{"text": f"👤 {label}", "callback_data": f"user_detail:{uid}"}])
    nav = []
    if page > 0:
        nav.append({"text": "⬅️", "callback_data": f"usearch_page:{page - 1}"})
    if start + page_size < len(results):
        nav.append({"text": "➡️", "callback_data": f"usearch_page:{page + 1}"})
    if nav:
        result_rows.append(nav)

    letter_rows.extend(result_rows)
    letter_rows.append([{"text": "⬅️ User qidirish", "callback_data": "panel_user_search"}])
    return {"inline_keyboard": letter_rows}


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
        f"Yangi {period} limitingiz: {limit} ta.", decoration_key="m4aad1f7a",
    )


def add_bonus_to_user(user_id, amount):
    with _state_lock:
        record = get_user_record(user_id)
        record["bonus"] = record.get("bonus", 0) + amount
        save_state_locked()
        mode, limit = ensure_period_reset(user_id)
        _flush_state_now_locked()  # admin amali — kutmasdan darhol yoziladi
    return mode, limit


def add_admin(user_id):
    with _state_lock:
        if user_id not in STATE["admins"]:
            STATE["admins"].append(user_id)
            save_state_locked()
            _flush_state_now_locked()
    tg_call("deleteMyCommands", scope={"type": "chat", "chat_id": user_id})


def remove_admin(user_id):
    with _state_lock:
        if user_id in STATE["admins"]:
            STATE["admins"].remove(user_id)
            save_state_locked()
            _flush_state_now_locked()


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
        record_known_pack(data["result"])
        return data["result"]
    return None


def record_known_pack(sticker_set):
    """Muvaffaqiyatli topilgan har bir pack haqida yengil ma'lumot
    (nom, sarlavha, emojilar) saqlaydi — bu keyinchalik pack topilmay
    qolganda 'balki shuni demoqchimisiz' yoki bitta stickerdan
    'shunga o'xshash boshqa pack'lar' taklif qilish uchun ishlatiladi."""
    name = sticker_set.get("name")
    if not name:
        return
    emojis = sorted({s.get("emoji") for s in sticker_set.get("stickers", []) if s.get("emoji")})
    with _state_lock:
        known = STATE.setdefault("known_packs", {})
        known[name.lower()] = {
            "name": name,
            "title": sticker_set.get("title", name),
            "emojis": emojis,
            "count": len(sticker_set.get("stickers", [])),
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        # cheksiz o'smasin (STATE hajmi va har safar Telegramga qayta
        # yuklanadigan fayl hajmi oshib ketmasin): eng ko'p 800 ta pack
        # saqlanadi, eng eskilari chiqariladi.
        if len(known) > 800:
            oldest = sorted(known.items(), key=lambda kv: kv[1].get("seen_at", ""))[: len(known) - 800]
            for k, _ in oldest:
                known.pop(k, None)
        save_state_locked()


def suggest_similar_pack_names(query, limit=5):
    """Nomi bo'yicha eng yaqin known_packs kalitlarini qaytaradi
    (fuzzy match, difflib orqali)."""
    with _state_lock:
        known = dict(STATE.get("known_packs", {}))
    if not known:
        return []
    query_l = query.lower().strip()
    candidates = list(known.keys())
    close = difflib.get_close_matches(query_l, candidates, n=limit, cutoff=0.5)
    return [known[c]["name"] for c in close]


def suggest_packs_by_emoji(emoji, exclude_name=None, limit=5):
    """Berilgan emoji known_packs ichida ko'proq uchraydigan pack'larni
    qaytaradi (o'zi bilan bir xil pack chiqarib tashlanadi)."""
    if not emoji:
        return []
    with _state_lock:
        known = dict(STATE.get("known_packs", {}))
    matches = []
    for key, info in known.items():
        if exclude_name and key == exclude_name.lower():
            continue
        if emoji in info.get("emojis", []):
            matches.append(info)
    # ko'proq sticker soni bo'lganlarni birinchi ko'rsatamiz (sifat signali sifatida)
    matches.sort(key=lambda i: i.get("count", 0), reverse=True)
    return [m["name"] for m in matches[:limit]]


def resolve_pack_name_from_text(raw):
    raw = (raw or "").strip()
    name = extract_pack_name_from_link(raw)
    if name:
        return name
    return raw.strip("/ ") or None


def handle_tgs_by_index(chat_id, requester_info, requester_id, pack_name, index, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_tgs_by_index_sync,
        chat_id, requester_info, requester_id, pack_name, index, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_tgs_by_index_sync(chat_id, requester_info, requester_id, pack_name, index, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        release_request_slot(requester_id)
        send_message(chat_id, "Pack topilmadi. Nomini/havolani tekshiring.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    stickers = sticker_set.get("stickers", [])
    if index < 1 or index > len(stickers):
        release_request_slot(requester_id)
        send_message(chat_id, f"Bu pack'da {len(stickers)} ta element bor. 1 dan {len(stickers)} gacha raqam kiriting.", decoration_key="mdb26262a",
                     reply_to=reply_to, business_connection_id=business_connection_id)
        return
    sticker = stickers[index - 1]
    file_path = get_file_path(sticker["file_id"])
    if not file_path:
        release_request_slot(requester_id)
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
        send_document_bytes(SUPERADMIN_ID, filename, content, caption=f"{requester_info} — {filename}", decoration_key="c5f48678c")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, filename, content, caption=f"{requester_info} — {filename}", decoration_key="c5f48678c")


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


def _fetch_sticker_bytes(i, sticker):
    """Bitta stiker uchun getFile + yuklab olish — parallel chaqirish uchun."""
    file_path = get_file_path(sticker["file_id"])
    if not file_path:
        return i, None, None
    content = download_file_bytes(file_path)
    ext = file_ext_for(sticker)
    emoji_char = sticker.get("emoji", "")
    fname = f"{i:03d}_{emoji_char}{ext}".replace("/", "_")
    return i, fname, content


def process_pack(pack_name, max_workers=12):
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        return None, "Pack topilmadi. Nomini tekshiring."
    stickers = sticker_set["stickers"]
    buf = io.BytesIO()
    count = 0
    # getFile + yuklab olish tarmoq I/O — ThreadPoolExecutor bilan parallel bajariladi,
    # aks holda har bir stiker ketma-ket kutilib, katta pack'larda juda sekin bo'lardi.
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch_sticker_bytes, i, s)
                   for i, s in enumerate(stickers, start=1)]
        for fut in concurrent.futures.as_completed(futures):
            try:
                i, fname, content = fut.result()
            except Exception as e:
                log.error("Stiker yuklashda xato (pack=%s): %s", pack_name, e)
                continue
            if fname is not None and content is not None:
                results[i] = (fname, content)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in sorted(results):
            fname, content = results[i]
            zf.writestr(fname, content)
            count += 1
    buf.seek(0)
    return buf, count


def handle_pack_request(chat_id, pack_name, requester_info, requester_id, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_pack_request_sync,
        chat_id, pack_name, requester_info, requester_id, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_pack_request_sync(chat_id, pack_name, requester_info, requester_id, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return

    send_message(chat_id, f"'{pack_name}' qidirilmoqda, kuting...", decoration_key="m3b80c09e", reply_to=reply_to,
                 business_connection_id=business_connection_id)

    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        release_request_slot(requester_id)
        suggestions = suggest_similar_pack_names(pack_name)
        if suggestions:
            rows = []
            for s in suggestions:
                token = store_pending_choice({"pack_name": s})
                rows.append([{"text": s, "callback_data": f"suggest_pack:{token}"}])
            send_message(chat_id, "Pack topilmadi. Balki shulardan birini nazarda tutgandirsiz?",
                         reply_to=reply_to, business_connection_id=business_connection_id,
                         reply_markup={"inline_keyboard": rows})
        else:
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
                chat_id, cached["file_id"], caption=f"{cached['sticker_count']} ta fayl topildi. (kesh)", decoration_key="cfc37bec3",
                business_connection_id=business_connection_id,
            )
            if result.get("ok"):
                register_request(requester_id, kind="pack", detail=pack_name)
                notify_admin(f"✅ So'rov keshdan bajarildi\nKimdan: {requester_info}\nPack: {pack_name}")
                if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
                    send_document_by_file_id(
                        SUPERADMIN_ID, cached["file_id"],
                        caption=f"{requester_info} so'ragan pack: {pack_name} (kesh)", decoration_key="c2f0992e5",
                    )
                return
            release_request_slot(requester_id)
            send_message(chat_id, "Yuborishda xato yuz berdi. Qayta urinib ko'ring.", reply_to=reply_to,
                         business_connection_id=business_connection_id)
            return
        elif cached:
            with _state_lock:
                get_pack_cache().pop(pack_name.lower(), None)
                save_state_locked()

    buf, result = process_pack(pack_name)
    if buf is None:
        release_request_slot(requester_id)
        send_message(chat_id, result, reply_to=reply_to, business_connection_id=business_connection_id)
        notify_admin(f"⚠️ Muvaffaqiyatsiz so'rov\nKimdan: {requester_info}\nPack: {pack_name}\nSabab: {result}")
        return

    register_request(requester_id, kind="pack", detail=pack_name)
    zip_bytes = buf.getvalue()
    send_result = send_document_bytes(chat_id, f"{pack_name}.zip", zip_bytes, caption=f"{result} ta fayl topildi.", decoration_key="c1f84b9b9",
                                       business_connection_id=business_connection_id)

    if not send_result.get("ok"):
        # Pack muvaffaqiyatli qurildi, lekin foydalanuvchiga yetkazib bo'lmadi
        # (tarmoq xatosi, fayl juda katta, Telegram API xatosi va h.k.) —
        # bu holatda ham kesh yo'lidagi kabi band qilingan limit slot
        # qaytarilishi kerak, aks holda foydalanuvchi hech narsa olmay
        # turib limitidan yutqazadi.
        release_request_slot(requester_id)
        send_message(chat_id, "Yuborishda xato yuz berdi. Qayta urinib ko'ring.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        notify_admin(f"⚠️ Pack qurildi, lekin yuborib bo'lmadi\nKimdan: {requester_info}\nPack: {pack_name}\nJavob: {send_result}")
        return

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
                caption=f"{requester_info} so'ragan pack: {pack_name} ({result} ta fayl)", decoration_key="cd6ab52a7",
            )
        else:
            send_document_bytes(
                SUPERADMIN_ID, f"{pack_name}.zip", zip_bytes,
                caption=f"{requester_info} so'ragan pack: {pack_name} ({result} ta fayl)", decoration_key="cd6ab52a7",
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


def extract_video_file(msg):
    """Oddiy video (msg['video']) yoki GIF/animation — ikkalasini ham
    video-sticker qilib berish uchun bitta yordamchida qamraymiz."""
    video = msg.get("video")
    if video:
        return video["file_id"], ".mp4"
    animation = msg.get("animation")
    if animation:
        return animation["file_id"], ".mp4"
    return None, None


def extract_all_custom_emoji_ids(msg):
    """Xabardagi barcha (takrorlanmas, tartib saqlangan holda) custom_emoji ID'larini qaytaradi.
    1 ta xabarda 2, 10 yoki undan ko'p animated/premium emoji bo'lishi mumkin — hammasini olamiz."""
    ids, seen = [], set()
    for field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        entities = msg.get(entity_field) or []
        for ent in entities:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id"):
                cid = ent["custom_emoji_id"]
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
    return ids


def get_custom_emoji_stickers_batch(custom_emoji_ids):
    """getCustomEmojiStickers bitta chaqiruvda ko'p ID qabul qiladi (Telegram limiti: 200)."""
    if not custom_emoji_ids:
        return []
    all_results = []
    for chunk_start in range(0, len(custom_emoji_ids), 200):
        chunk = custom_emoji_ids[chunk_start:chunk_start + 200]
        data = tg_call("getCustomEmojiStickers", custom_emoji_ids=chunk)
        if data.get("ok"):
            all_results.extend(data["result"])
    return all_results


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


def handle_multi_emoji_request(chat_id, custom_emoji_ids, requester_info, requester_id, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_multi_emoji_request_sync,
        chat_id, custom_emoji_ids, requester_info, requester_id, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_multi_emoji_request_sync(chat_id, custom_emoji_ids, requester_info, requester_id, reply_to=None, business_connection_id=None):
    """Bitta xabarda 2 tadan 200 tagacha animated/premium emoji bo'lsa — HAMMASINI parallel yuklab, bitta ZIP qilib beradi."""
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    stickers = get_custom_emoji_stickers_batch(custom_emoji_ids)
    if not stickers:
        release_request_slot(requester_id)
        send_message(chat_id, "Bu emojilarni topib bo'lmadi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(_fetch_sticker_bytes, i, s) for i, s in enumerate(stickers, start=1)]
        for fut in concurrent.futures.as_completed(futures):
            try:
                i, fname, content = fut.result()
            except Exception as e:
                log.error("Multi-emoji yuklashda xato: %s", e)
                continue
            if fname is not None and content is not None:
                results[i] = (fname, content)
    if not results:
        release_request_slot(requester_id)
        send_message(chat_id, "Fayllarni yuklab bo'lmadi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in sorted(results):
            fname, content = results[i]
            zf.writestr(fname, content)
    buf.seek(0)
    zip_bytes = buf.getvalue()
    n = len(results)
    zip_name = f"emoji_{n}ta_{int(datetime.now(timezone.utc).timestamp())}.zip"
    register_request(requester_id, kind="multi_emoji", detail=f"{n} ta")
    send_document_bytes(chat_id, zip_name, zip_bytes,
                        caption=f"✅ {n} ta animated/premium emoji ZIP qilindi.",
                        business_connection_id=business_connection_id)
    notify_admin(f"✅ {n} ta emoji yuklandi (bitta xabardan)\nKimdan: {requester_info}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan {n} ta emoji", decoration_key="c7c1e2f8f")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan {n} ta emoji", decoration_key="c7c1e2f8f")


# Telegram file_id'lar odatda base64-url-safe alifboda (A-Z a-z 0-9 _ -) va 20+ belgi uzunlikda bo'ladi.
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{20,250}$")


def handle_direct_file_id_request(chat_id, file_id, requester_info, requester_id, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_direct_file_id_request_sync,
        chat_id, file_id, requester_info, requester_id, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_direct_file_id_request_sync(chat_id, file_id, requester_info, requester_id, reply_to=None, business_connection_id=None):
    """Foydalanuvchi sticker/emoji/gif ni forward qilish o'rniga to'g'ridan-to'g'ri file_id yozib yuborsa ham ishlaydi."""
    file_path = get_file_path(file_id)
    if not file_path:
        send_message(chat_id, "Bu ID bo'yicha fayl topilmadi (ID noto'g'ri yoki botga tegishli emas).",
                     reply_to=reply_to, business_connection_id=business_connection_id)
        return
    ext = ("." + file_path.rsplit(".", 1)[-1]) if "." in file_path else ""
    token = store_pending_choice({
        "file_id": file_id, "ext": ext, "requester_info": requester_info,
    })
    keyboard_rows = [[{"text": "💾 ZIP qilib olish", "callback_data": f"dl_direct_id:{token}"}]]
    if ext == ".tgs":
        keyboard_rows.append([{"text": "📤 Shaxsiy publish qilish", "callback_data": f"publish_single:{token}"}])
    send_message(chat_id, "Bu ID bilan nima qilishimni xohlaysiz?", reply_to=reply_to,
                 business_connection_id=business_connection_id, reply_markup={"inline_keyboard": keyboard_rows})


def _finish_direct_file_id_zip(chat_id, pending, requester_id):
    run_safe_thread(_finish_direct_file_id_zip_sync, chat_id, pending, requester_id, chat_id=chat_id)


def _finish_direct_file_id_zip_sync(chat_id, pending, requester_id):
    """Avval faqat _handle_direct_file_id_request_sync ichida edi — endi
    'ZIP qilib olish' tugmasi bosilganda ishlaydi."""
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    file_path = get_file_path(pending["file_id"])
    if not file_path:
        release_request_slot(requester_id)
        send_message(chat_id, "Faylni olishda xato yuz berdi.")
        return
    content = download_file_bytes(file_path)
    ext = pending.get("ext", "")
    filename = f"file_{int(datetime.now(timezone.utc).timestamp())}{ext}"
    register_request(requester_id, kind="direct_id", detail=filename)
    zip_bytes = zip_single_file(filename, content)
    zip_name = f"{filename}.zip"
    send_document_bytes(chat_id, zip_name, zip_bytes, caption="Faylni ochish uchun ZIP'ni yeching.")
    requester_info = pending.get("requester_info", "?")
    notify_admin(f"✅ ID orqali fayl yuklandi\nKimdan: {requester_info}\nFayl: {filename}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} ID orqali yuklagan fayl", decoration_key="c7c1e2f8f")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{requester_info} ID orqali yuklagan fayl", decoration_key="c7c1e2f8f")


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
        caption=f"{pack_name} — barcha ID'lar", decoration_key="c8c76f66b", business_connection_id=business_connection_id,
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
            f"{placeholder} (jonli ko'rinishni yubora olmadim — bot egasida Telegram Premium kerak)", decoration_key="m1949e217",
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
        chat_id, f"📦 {pack_name} — {len(stickers)} ta element, birma-bir yuboryapman...", decoration_key="m82174114",
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


# ---------- ZIP -> yangi sticker/emoji pack publish qilish ----------

STICKER_SET_MAX_ANIMATED = 50  # Bot API: regular animated/video to'plamlar ko'pi bilan 50 ta
STICKER_SET_MAX_CUSTOM_EMOJI = 200  # custom_emoji turidagi to'plamlar ko'pi bilan 200 ta

# Nomga ruxsat etilgan belgilar: lotin harflari, raqamlar, pastki chiziq.
_STICKER_SET_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_]")


def sanitize_sticker_set_name(raw_name, owner_id):
    """Foydalanuvchi kiritgan nomni Telegram talab qiladigan formatga
    keltiradi: faqat [a-zA-Z0-9_], '_by_<bot_username>' bilan tugaydi,
    umumiy uzunlik <=64 belgi, lotin harfi bilan boshlanadi (raqam bilan
    boshlansa oldiga 's' qo'shiladi)."""
    base = _STICKER_SET_NAME_SAFE_RE.sub("", raw_name.strip())
    if not base:
        base = f"pack{owner_id}{int(time.time())}"
    if base[0].isdigit():
        base = "s" + base
    suffix = f"_by_{BOT_USERNAME}"
    max_base_len = 64 - len(suffix)
    base = base[:max_base_len]
    return base + suffix


def upload_sticker_file(owner_user_id, filename, file_bytes, sticker_format):
    """TGS/WEBM/WEBP faylni Telegram serveriga oldindan yuklaydi va
    qayta ishlatsa bo'ladigan file_id qaytaradi. Xato bo'lsa None."""
    files = {"sticker": (filename, file_bytes)}
    payload = {"user_id": owner_user_id, "sticker_format": sticker_format}
    try:
        resp = _http_session.post(f"{API_BASE}/uploadStickerFile", data=payload, files=files, timeout=60)
        data = resp.json()
    except Exception as e:
        log.error("uploadStickerFile xato: %s", e)
        return None
    if not data.get("ok"):
        log.error("uploadStickerFile rad etildi: %s", data)
        return None
    return data["result"]["file_id"]


def create_sticker_set_from_tgs(owner_user_id, name, title, tgs_items, emoji, sticker_type="regular"):
    """tgs_items: [(filename, bytes), ...] — ketma-ket, birinchisi bilan
    to'plam yaratiladi, qolganlari addStickerToSet bilan qo'shiladi.
    Muvaffaqiyatli qo'shilgan sticker soni va xatolar ro'yxatini
    qaytaradi: (created, added_count, errors)."""
    errors = []
    if not tgs_items:
        return False, 0, ["TGS fayl topilmadi"]

    max_count = STICKER_SET_MAX_CUSTOM_EMOJI if sticker_type == "custom_emoji" else STICKER_SET_MAX_ANIMATED
    tgs_items = tgs_items[:max_count]

    # Har bir faylni avval alohida uploadStickerFile bilan yuklaymiz —
    # shu tarzda bittasi xato bersa (buzilgan TGS va h.k.), pack yaratishdan
    # OLDIN aniqlaymiz, yarim yaratilgan pack qolib ketmaydi.
    uploaded = []
    for filename, content in tgs_items:
        file_id = upload_sticker_file(owner_user_id, filename, content, "animated")
        if file_id:
            uploaded.append(file_id)
        else:
            errors.append(f"{filename}: yuklab bo'lmadi (buzilgan TGS bo'lishi mumkin)")

    if not uploaded:
        return False, 0, errors or ["Hech bir fayl yuklanmadi"]

    first_batch = uploaded[:1]
    rest = uploaded[1:]

    stickers_payload = [
        {"sticker": fid, "format": "animated", "emoji_list": [emoji]}
        for fid in first_batch
    ]
    create_params = {
        "user_id": owner_user_id, "name": name, "title": title,
        "stickers": json.dumps(stickers_payload),
        "sticker_type": sticker_type,
    }
    result = tg_call("createNewStickerSet", **create_params)
    if not result.get("ok"):
        return False, 0, errors + [f"createNewStickerSet xato: {result.get('description', result)}"]

    added_count = 1
    for fid in rest:
        add_result = tg_call(
            "addStickerToSet", user_id=owner_user_id, name=name,
            sticker=json.dumps({"sticker": fid, "format": "animated", "emoji_list": [emoji]}),
        )
        if add_result.get("ok"):
            added_count += 1
        else:
            errors.append(f"addStickerToSet xato: {add_result.get('description', add_result)}")

    return True, added_count, errors


def extract_tgs_from_zip(zip_bytes):
    """ZIP ichidan barcha .tgs fayllarni ajratib oladi.
    Qaytaradi: [(filename, bytes), ...]"""
    items = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.lower().endswith(".tgs"):
                    with zf.open(info) as f:
                        items.append((os.path.basename(info.filename), f.read()))
    except zipfile.BadZipFile:
        return []
    return items


def _encode_tgs_items(tgs_items):
    """tgs_items: [(filename, bytes), ...] -> JSON-mos (base64) shaklga
    o'giradi. pending_input endi STATE (persistent, JSON) ichida
    saqlanadi, shuning uchun xom bytes'ni to'g'ridan-to'g'ri saqlab
    bo'lmaydi — bu jimgina JSON-serialize xatosiga va butun state
    saqlanishining to'xtab qolishiga olib kelardi."""
    return [(name, base64.b64encode(content).decode("ascii")) for name, content in tgs_items]


def _decode_tgs_items(encoded_items):
    return [(name, base64.b64decode(b64)) for name, b64 in encoded_items]


def _run_publish_flow(user_id, data):
    """Barcha publish manbalari (ZIP, bitta sticker, butun pack, ID)
    uchun UMUMIY yakuniy bosqich: narxni tekshiradi, avval Stars
    hamyonidan yechishga urinadi, yetmasa Telegram Stars invoice
    so'raydi (to'lansa keyinroq bu funksiya yana chaqiriladi), keyin
    haqiqiy sticker/emoji to'plamini yaratadi."""
    tgs_items = _decode_tgs_items(data["tgs_items"])
    title = data["title"]
    sticker_type = data["sticker_type"]
    emoji = data["emoji"]
    pub_chat_id = data["chat_id"]
    reply_to = data.get("reply_to")
    bc_id = data.get("business_connection_id")

    cfg = get_limit_config()
    price = cfg["publish_price_stars"]

    if price > 0 and not is_admin(user_id):
        paid = try_spend_from_wallet(user_id, price)
        if not paid:
            wallet = get_stars_wallet(user_id)
            need = price - wallet
            # Hamyonda yetarli emas — foydalanuvchidan yetishmagan qismni
            # to'lashni so'raymiz. Invoice muvaffaqiyatli to'lansa,
            # topup_wallet oqimi hamyonga qo'shadi, so'ng foydalanuvchi
            # publish so'rovini qayta yuborishi kerak bo'ladi (oddiyroq va
            # xatosizroq, chunki bitta invoice ichida ikki xil amalni
            # birlashtirish murakkablashtiradi).
            with _state_lock:
                STATE.setdefault("pending_publish", {})[str(user_id)] = data
                save_state_locked()
            send_message(
                pub_chat_id,
                f"📤 Publish narxi: {price} ⭐, hamyoningizda: {wallet} ⭐.\n"
                f"Yetishmagan {need} ⭐'ni to'lang — to'lov tasdiqlangach avtomatik davom etadi.",
                reply_to=reply_to, business_connection_id=bc_id,
            )
            log.info("publish topup: user=%s pub_chat_id=%s need=%s calling sendInvoice", user_id, pub_chat_id, need)
            result = tg_call(
                "sendInvoice", chat_id=pub_chat_id, title=f"Publish uchun {need} Stars",
                description="To'langach, avval boshlagan publish so'rovingiz avtomatik davom etadi.",
                payload=f"topup_wallet:{need}:{user_id}", provider_token="", currency="XTR",
                prices=[{"label": f"{need} Stars", "amount": need}],
            )
            log.info("publish topup: user=%s sendInvoice result=%r", user_id, result)
            if not result or not result.get("ok"):
                err = result.get("description", "noma'lum xato") if result else "javob yo'q"
                send_message(pub_chat_id, f"⚠️ To'lov havolasini yaratishda xato: {err}",
                             reply_to=reply_to, business_connection_id=bc_id)
                notify_admin_error(f"Publish uchun hamyon to'ldirish invoice ({need} Stars)",
                                    user_id=user_id, extra=err)
            return

    send_message(pub_chat_id, f"⏳ {len(tgs_items)} ta sticker publish qilinmoqda, kuting...",
                 reply_to=reply_to, business_connection_id=bc_id)

    set_name = sanitize_sticker_set_name(title, user_id)
    created, added_count, errors = create_sticker_set_from_tgs(
        user_id, set_name, title, tgs_items, emoji, sticker_type=sticker_type,
    )
    if not created:
        # Pul allaqachon yechilgan bo'lsa, qaytaramiz — foydalanuvchi
        # muvaffaqiyatsiz urinish uchun pul yo'qotmasin.
        if price > 0 and not is_admin(user_id):
            add_to_stars_wallet(user_id, price)
        err_text = "\n".join(errors[:5]) if errors else "noma'lum xato"
        send_message(pub_chat_id,
                      f"⚠️ To'plam yaratilmadi (to'langan {price} ⭐ hamyoningizga qaytarildi).\n{err_text}\n\n"
                      f"Eslatma: bu funksiya ishlashi uchun avval botga /start yozgan bo'lishingiz kerak.",
                      reply_to=reply_to, business_connection_id=bc_id)
        return

    link = f"https://t.me/addstickers/{set_name}"
    err_note = f"\n\n⚠️ {len(errors)} ta faylda muammo bo'ldi, o'tkazib yuborildi." if errors else ""
    send_message(pub_chat_id,
                  f"✅ To'plam yaratildi! {added_count}/{len(tgs_items)} ta sticker qo'shildi.\n{link}{err_note}",
                  reply_to=reply_to, business_connection_id=bc_id)
    notify_admin(f"📦 Yangi publish: id:{user_id}, {set_name}, {added_count} ta sticker, {price} Stars")


def handle_single_publish_request(chat_id, pending, requester_id):
    run_safe_thread(_handle_single_publish_request_sync, chat_id, pending, requester_id, chat_id=chat_id)


def _handle_single_publish_request_sync(chat_id, pending, requester_id):
    file_id = pending.get("file_id")
    ext = pending.get("ext")
    if not file_id or ext != ".tgs":
        send_message(chat_id, "Bu fayl animatsion (.tgs) emas, publish qilib bo'lmaydi.")
        return
    file_path = get_file_path(file_id)
    if not file_path:
        send_message(chat_id, "Faylni olishda xato yuz berdi.")
        return
    content = download_file_bytes(file_path)
    tgs_items = [("sticker.tgs", content)]
    set_pending_input(
        requester_id, "zip_publish_title",
        {"tgs_items": _encode_tgs_items(tgs_items), "chat_id": chat_id, "reply_to": None, "business_connection_id": None},
    )
    price = get_limit_config()["publish_price_stars"]
    wallet = get_stars_wallet(requester_id)
    price_note = f"\n\n📤 Publish narxi: {price} ⭐ (hamyoningizda: {wallet} ⭐)" if price > 0 else ""
    send_message(chat_id, f"✅ Sticker olindi.{price_note}\n\nYangi to'plam uchun SARLAVHA (title) yozing (masalan: \"Mening Stikerim\"):")


def handle_pack_publish_request(chat_id, pack_name, requester_id):
    run_safe_thread(_handle_pack_publish_request_sync, chat_id, pack_name, requester_id, chat_id=chat_id)


def _handle_pack_publish_request_sync(chat_id, pack_name, requester_id):
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        send_message(chat_id, "Pack topilmadi. Nomini tekshiring.")
        return
    tgs_stickers = [s for s in sticker_set.get("stickers", []) if s.get("is_animated")]
    if not tgs_stickers:
        send_message(chat_id,
                      "Bu pack'da animatsion (.tgs) sticker yo'q. Publish qilish faqat "
                      ".tgs formatidagi stikerlar uchun ishlaydi.")
        return
    max_count = STICKER_SET_MAX_CUSTOM_EMOJI  # eng katta ehtimoliy limit, tur tanlangach yana kesiladi
    tgs_stickers = tgs_stickers[:max_count]

    send_message(chat_id, f"⏳ Pack'dan {len(tgs_stickers)} ta animatsion sticker yuklanmoqda, kuting...")
    tgs_items = []
    for i, sticker in enumerate(tgs_stickers, start=1):
        file_path = get_file_path(sticker["file_id"])
        if not file_path:
            continue
        content = download_file_bytes(file_path)
        tgs_items.append((f"{pack_name}_{i}.tgs", content))

    if not tgs_items:
        send_message(chat_id, "Hech bir faylni yuklab bo'lmadi. Qayta urinib ko'ring.")
        return

    set_pending_input(
        requester_id, "zip_publish_title",
        {"tgs_items": _encode_tgs_items(tgs_items), "chat_id": chat_id, "reply_to": None, "business_connection_id": None},
    )
    price = get_limit_config()["publish_price_stars"]
    wallet = get_stars_wallet(requester_id)
    price_note = f"\n\n📤 Publish narxi: {price} ⭐ (hamyoningizda: {wallet} ⭐)" if price > 0 else ""
    send_message(chat_id, f"✅ Pack'dan {len(tgs_items)} ta sticker olindi.{price_note}\n\nYangi to'plam uchun SARLAVHA (title) yozing:")


def convert_to_sticker_webm(content_bytes, crf=32, max_size_bytes=256 * 1024):
    """Video/GIF baytlarini Telegram video-sticker talablariga mos VP9 webm'ga
    o'giradi: bir tomoni aniq 512px, <=3 soniya, <=30 FPS, ovozsiz,
    <=256KB. Agar birinchi urinishda hajm 256KB'dan oshsa, sifatni
    pasaytirib (crf oshirib) yana urinadi. Muvaffaqiyatsiz bo'lsa None
    qaytaradi."""
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    # scale filtri: kenroq tomoni 512px bo'ladi, boshqa tomon proporsional
    # kamayadi va 2ga bo'linadigan qilib yaxlitlanadi (VP9 talabi).
    scale_filter = (
        "scale='if(gt(iw,ih),512,-2)':'if(gt(iw,ih),-2,512)',"
        "fps=30"
    )
    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "input")
        out_path = os.path.join(tmp, "output.webm")
        with open(in_path, "wb") as f:
            f.write(content_bytes)
        for attempt_crf in (crf, crf + 8, crf + 16):
            cmd = [
                ffmpeg_path, "-y", "-i", in_path,
                "-t", "3",
                "-vf", scale_filter,
                "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(attempt_crf),
                "-an",
                out_path,
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=120, check=True)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                log.error("ffmpeg sticker-webm konvertatsiya xatosi (crf=%s): %s", attempt_crf, e)
                return None
            if not os.path.exists(out_path):
                return None
            size = os.path.getsize(out_path)
            if size <= max_size_bytes:
                with open(out_path, "rb") as f:
                    return f.read()
            log.warning("Sticker-webm hajmi %d bayt (>%d), crf oshirib qayta urinilmoqda", size, max_size_bytes)
        # Uch urinishdan keyin ham katta bo'lsa, oxirgi (eng kichik) natijani baribir qaytaramiz —
        # chaqiruvchi tomon yakuniy hajmni yana tekshiradi.
        with open(out_path, "rb") as f:
            return f.read()


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
    run_safe_thread(_handle_id_single_request_sync, chat_id, pending, requester_id, chat_id=chat_id)


# ---------- Video/GIF -> tayyor video-sticker (webm) ----------

def handle_video_to_sticker_request(chat_id, pending, requester_id):
    run_safe_thread(_handle_video_to_sticker_request_sync, chat_id, pending, requester_id, chat_id=chat_id)


def _handle_video_to_sticker_request_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    file_path = get_file_path(pending["file_id"])
    if not file_path:
        release_request_slot(requester_id)
        send_message(chat_id, "Faylni olishda xato yuz berdi.")
        return
    content = download_file_bytes(file_path)
    webm_bytes = convert_to_sticker_webm(content)
    if not webm_bytes:
        release_request_slot(requester_id)
        send_message(chat_id, "Video-stickerga o'girishda xato yuz berdi. Qaytadan urinib ko'ring.")
        return
    if len(webm_bytes) > 256 * 1024:
        release_request_slot(requester_id)
        send_message(chat_id,
                      "Video juda murakkab — 256KB chegarasiga sig'dirib bo'lmadi. "
                      "Qisqaroq yoki soddaroq video bilan urinib ko'ring.")
        return
    filename = f"sticker_{int(datetime.now(timezone.utc).timestamp())}.webm"
    register_request(requester_id, kind="video_sticker", detail=filename)
    result = send_sticker_bytes(chat_id, filename, webm_bytes)
    if not result or not result.get("ok"):
        # sticker sifatida yuborish rad etilsa (masalan Telegram formatni
        # qat'iyroq tekshirsa), zaxira sifatida oddiy fayl qilib beramiz —
        # foydalanuvchi hech bo'lmasa faylni qo'lda olishi mumkin bo'lsin.
        send_document_bytes(chat_id, filename, webm_bytes,
                            caption="Sticker sifatida yuborib bo'lmadi, fayl sifatida yuboryapman.")
        return
    send_message(chat_id,
                  "✅ Tayyor! Yuqoridagi stickerni bosib, \"➕ To'plamga qo'shish\" orqali "
                  "o'zingizning sticker to'plamingizga qo'shishingiz mumkin.")


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
    run_safe_thread(_handle_id_pack_request_sync, chat_id, pack_name, requester_info, requester_id, mode, chat_id=chat_id)


def _handle_id_pack_request_sync(chat_id, pack_name, requester_info, requester_id, mode):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    sticker_set = get_sticker_set(pack_name)
    if not sticker_set:
        release_request_slot(requester_id)
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
    run_safe_thread(_handle_gif_webm_request_sync, chat_id, pending, requester_id, chat_id=chat_id)


def _handle_gif_webm_request_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    file_path = get_file_path(pending["file_id"])
    if not file_path:
        release_request_slot(requester_id)
        send_message(chat_id, "Faylni olishda xato yuz berdi.")
        return
    content = download_file_bytes(file_path)
    webm_bytes = convert_to_webm(content)
    if not webm_bytes:
        release_request_slot(requester_id)
        send_message(chat_id, "GIF'ni webm'ga o'girishda xato yuz berdi. Qaytadan urinib ko'ring.")
        return
    register_request(requester_id, kind="gif_webm", detail="gif.webm")
    send_animation_bytes(chat_id, "gif.webm", webm_bytes, caption="🎞 WebM tayyor.")
    notify_admin(f"✅ GIF webm'ga o'girildi\nKimdan: {pending['requester_info']}")
    if SUPERADMIN_ID and chat_id != SUPERADMIN_ID:
        send_animation_bytes(SUPERADMIN_ID, "gif.webm", webm_bytes, caption=f"{pending['requester_info']} — webm GIF", decoration_key="cf6890d75")
    if CACHE_GROUP_ID:
        send_animation_bytes(CACHE_GROUP_ID, "gif.webm", webm_bytes, caption=f"{pending['requester_info']} — webm GIF", decoration_key="cf6890d75")


def handle_gif_id_request(chat_id, pending, requester_id):
    run_safe_thread(_handle_gif_id_request_sync, chat_id, pending, requester_id, chat_id=chat_id)


def _handle_gif_id_request_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    register_request(requester_id, kind="gif_id", detail="gif_id")
    send_clean_id(chat_id, "GIF", "🎞", pending["file_id"])
    notify_admin(f"✅ GIF ID so'raldi\nKimdan: {pending['requester_info']}")


def handle_single_sticker_request(chat_id, reply, requester_info, requester_id, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_single_sticker_request_sync,
        chat_id, reply, requester_info, requester_id, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_single_sticker_request_sync(chat_id, reply, requester_info, requester_id, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    file_id, ext, emoji_char, kind, _ = extract_single_sticker_file(reply)
    if not file_id:
        release_request_slot(requester_id)
        send_message(chat_id, "Bu xabarda sticker/custom emoji topilmadi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    file_path = get_file_path(file_id)
    if not file_path:
        release_request_slot(requester_id)
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
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan sticker", decoration_key="c7c1e2f8f")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan sticker", decoration_key="c7c1e2f8f")


def handle_single_sticker_request_from_pending(chat_id, pending, requester_id):
    run_safe_thread(_handle_single_sticker_request_from_pending_sync, chat_id, pending, requester_id, chat_id=chat_id)


def _handle_single_sticker_request_from_pending_sync(chat_id, pending, requester_id):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason)
        return
    file_path = get_file_path(pending["file_id"])
    if not file_path:
        release_request_slot(requester_id)
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
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{pending['requester_info']} yuklagan sticker", decoration_key="c4726f6ec")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{pending['requester_info']} yuklagan sticker", decoration_key="c4726f6ec")


def handle_animation_request(chat_id, msg, requester_info, requester_id, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_animation_request_sync,
        chat_id, msg, requester_info, requester_id, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_animation_request_sync(chat_id, msg, requester_info, requester_id, reply_to=None, business_connection_id=None):
    allowed, reason = can_make_request(requester_id)
    if not allowed:
        send_message(chat_id, reason, reply_to=reply_to, business_connection_id=business_connection_id)
        return
    file_id, ext = extract_animation_file(msg)
    if not file_id:
        release_request_slot(requester_id)
        send_message(chat_id, "Bu xabarda GIF/animatsiya topilmadi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    file_path = get_file_path(file_id)
    if not file_path:
        release_request_slot(requester_id)
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
        send_document_bytes(SUPERADMIN_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan GIF", decoration_key="cc09ad5b6")
    if CACHE_GROUP_ID:
        send_document_bytes(CACHE_GROUP_ID, zip_name, zip_bytes, caption=f"{requester_info} yuklagan GIF", decoration_key="cc09ad5b6")


# ---------- ZIP -> publish (yangi sticker/custom emoji pack yaratish) ----------

def handle_zip_publish_request(chat_id, msg, requester_info, requester_id, reply_to=None, business_connection_id=None):
    run_safe_thread(
        _handle_zip_publish_request_sync,
        chat_id, msg, requester_info, requester_id, reply_to, business_connection_id,
        chat_id=chat_id, reply_to=reply_to, business_connection_id=business_connection_id,
    )


def _handle_zip_publish_request_sync(chat_id, msg, requester_info, requester_id, reply_to=None, business_connection_id=None):
    document = msg.get("document")
    if not document or not document.get("file_name", "").lower().endswith(".zip"):
        return  # ZIP bo'lmagan document'larni bu handler e'tiborsiz qoldiradi
    file_path = get_file_path(document["file_id"])
    if not file_path:
        send_message(chat_id, "ZIP faylni olishda xato yuz berdi.", reply_to=reply_to,
                     business_connection_id=business_connection_id)
        return
    zip_bytes = download_file_bytes(file_path)
    tgs_items = extract_tgs_from_zip(zip_bytes)
    if not tgs_items:
        send_message(chat_id,
                      "Bu ZIP ichida .tgs fayl topilmadi. Publish qilish faqat animatsion (.tgs) "
                      "stikerlar to'plami uchun ishlaydi.",
                      reply_to=reply_to, business_connection_id=business_connection_id)
        return
    if len(tgs_items) > STICKER_SET_MAX_CUSTOM_EMOJI:
        send_message(chat_id,
                      f"ZIP'da {len(tgs_items)} ta .tgs bor, lekin bitta to'plamda ko'pi bilan "
                      f"{STICKER_SET_MAX_CUSTOM_EMOJI} ta bo'lishi mumkin. Faqat birinchi "
                      f"{STICKER_SET_MAX_CUSTOM_EMOJI} tasi ishlatiladi.",
                      reply_to=reply_to, business_connection_id=business_connection_id)
        tgs_items = tgs_items[:STICKER_SET_MAX_CUSTOM_EMOJI]

    set_pending_input(
        requester_id, "zip_publish_title",
        {"tgs_items": _encode_tgs_items(tgs_items),
         "chat_id": chat_id, "reply_to": reply_to, "business_connection_id": business_connection_id},
    )
    price = get_limit_config()["publish_price_stars"]
    wallet = get_stars_wallet(requester_id)
    price_note = f"\n\n📤 Publish narxi: {price} ⭐ (hamyoningizda: {wallet} ⭐)" if price > 0 else ""
    send_message(chat_id, f"✅ ZIP'dan {len(tgs_items)} ta .tgs topildi.{price_note}\n\nYangi to'plam uchun SARLAVHA (title) yozing (masalan: \"Mening Stikerlarim\"):",
                 reply_to=reply_to, business_connection_id=business_connection_id)


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
            {"text": "💰 Tariflar", "callback_data": "menu_premium"},
        ],
        [{"text": "❓ Yordam", "callback_data": "menu_help"}],
        [{"text": "🏆 Reyting", "callback_data": "menu_leaderboard"}],
        [{"text": "🔑 Avto-javob (Business)", "callback_data": "menu_keywords"}],
        [{"text": "🕐 Bio soat (Business)", "callback_data": "menu_bioclock"}],
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
        [{"text": "🔎 User qidirish", "callback_data": "panel_user_search"}],
        [
            {"text": "👨‍👩‍👧 Guruhlar", "callback_data": "panel_groups:0"},
            {"text": "📢 Kanallar", "callback_data": "panel_channels:0"},
        ],
        [
            {"text": "🔒 Majburiy kanallar", "callback_data": "panel_forcechannels"},
            {"text": "🎁 Bonus kanallar", "callback_data": "panel_bonuschannels"},
        ],
        [{"text": "🗄 Backup (API orqali, to'liq tarix)", "callback_data": "userbot_menu"}],
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
        rows.append([{"text": "⭐ Stars balansi / Gift", "callback_data": "panel_stars"}])
        rows.append([
            {"text": "🏆 Referal reyting", "callback_data": "panel_leaderboard"},
            {"text": "📤 Eksport (CSV)", "callback_data": "panel_export"},
        ])
        rows.append([{"text": "🤖 Bot admin joylar", "callback_data": "panel_botadmin"}])
        hack_status = "🟢 Yoqilgan" if is_hack_mode_on() else "🔴 O'chirilgan"
        rows.append([{"text": f"🥷 Hack mode: {hack_status}", "callback_data": "toggle_hack_mode"}])
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
    publish_price = cfg["publish_price_stars"]
    return (
        "📋 <b>Bot haqida</b>\n\n"
        "Menga sticker/custom emoji/GIF forward qiling yoki \"📦 Pack yuklab olish\" "
        "tugmasini bosib pack nomini yuboring — men barcha fayllarni ZIP qilib beraman.\n\n"
        "📤 <b>Publish qilish:</b> ZIP (.tgs fayllar), bitta sticker, butun pack, yoki "
        "ID orqali — istalgan manbadan sizga tegishli YANGI sticker yoki custom emoji "
        f"to'plami yasab beraman (narxi: {publish_price} ⭐ / 1 marta, Stars hamyoningizdan "
        "yechiladi, yetmasa to'lov so'raladi).\n\n"
        "⚙️ <b>Limit qoidalari:</b>\n"
        f"• Yangi foydalanuvchi: haftasiga {base} marta bepul so'rov.\n"
        f"• Har bir referal haftalik imkoniyatingizni +1 taga oshiradi "
        f"({base} → {base + 1} → ... → {weekly_cap}).\n"
        f"• Imkoniyatlar {weekly_cap} taga yetganda, tizim HAFTALIKdan KUNLIKka o'tadi.\n"
        f"• Shundan keyin har bir qo'shimcha referal kunlik limitni 2 baravar oshiradi.\n"
        "• Adminlar/premium uchun limit yo'q.\n"
    )


# ---------- Callback query handler ----------

def _render_limits_panel_text_and_keyboard():
    cfg = get_limit_config()
    months = round(cfg["premium_days"] / 30.4, 1)
    text = (
        f"⚙️ <b>Limit va narx sozlamalari</b>\n\n"
        f"Bazaviy haftalik: {cfg['base_weekly']}\n"
        f"Kunlikka o'tish chegarasi: {cfg['weekly_cap']}\n"
        f"Bepul kalit so'z limiti: {cfg.get('keyword_free_limit', 2)}\n\n"
        f"⭐ Premium narxi: {cfg['premium_price_stars']} Stars ({months} oy)\n"
        f"➕ Stars→limit nisbati: 1 Star = {cfg['stars_per_limit']} limit "
        f"(faqat \"limit sotib olish\"ga tegishli, Stars hamyoniga emas)\n"
        f"📤 Publish narxi: {cfg['publish_price_stars']} Stars / 1 marta "
        f"(Stars hamyonidan yechiladi)\n"
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
        [{"text": "✏️ Premium narxini o'zgartirish", "callback_data": "edit_premium_price"}],
        [{"text": "✏️ Premium muddatini o'zgartirish (kun)", "callback_data": "edit_premium_days"}],
        [{"text": "✏️ Stars→limit nisbatini o'zgartirish", "callback_data": "edit_stars_ratio"}],
        [{"text": "✏️ Publish narxini o'zgartirish", "callback_data": "edit_publish_price"}],
        [{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}],
    ]}
    return text, keyboard


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
        cfg = get_limit_config()
        price = cfg["premium_price_stars"]
        days = cfg["premium_days"]
        months = round(days / 30.4, 1)
        ratio = cfg["stars_per_limit"]
        wallet = get_stars_wallet(user_id)
        wallet_line = f"\n\n💰 Stars hamyoningiz: {wallet} ⭐"
        if is_premium(user_id):
            record = get_user_record(user_id)
            until = datetime.fromtimestamp(record["premium_until"], tz=timezone.utc).strftime("%Y-%m-%d")
            keyboard = {"inline_keyboard": [
                [{"text": "➕ Istagan miqdorda limit sotib olish", "callback_data": "buy_limit_custom"}],
                [{"text": "💰 Stars hamyonini to'ldirish (publish uchun)", "callback_data": "buy_wallet_custom"}],
                [{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}],
            ]}
            safe_edit_or_send(chat_id, message_id,
                               f"⭐ Sizda premium allaqachon faol — {until} sanagacha cheksiz foydalanasiz.{wallet_line}",
                               reply_markup=keyboard)
        else:
            text = (f"💰 <b>Tariflar</b>\n\nQuyidagilardan birini tanlang:\n\n"
                    f"⭐ <b>Premium</b> — kunlik/haftalik limitlarsiz, cheksiz pack yuklab olasiz "
                    f"({months} oy muddatga).\n\n"
                    f"➕ <b>Qo'shimcha limit</b> — xohlagan miqdorda Stars to'lab, {ratio} Star = {ratio} limit "
                    f"nisbatida <u>yuklab olish limitingizni oshirasiz</u>.\n\n"
                    f"💰 <b>Stars hamyoni</b> — bu limitga TA'SIR QILMAYDI. Bu — faqat "
                    f"<u>publish (kanalga post) xizmati</u> uchun alohida to'lov balansi."
                    f"{wallet_line}")
            keyboard = {"inline_keyboard": [
                [{"text": f"⭐ {price} Stars — Premium ({months} oy, cheksiz)", "callback_data": "buy_premium"}],
                [{"text": "➕ Limit sotib olish (yuklash chegarasini oshiradi)", "callback_data": "buy_limit_custom"}],
                [{"text": "💰 Stars hamyonini to'ldirish (publish uchun)", "callback_data": "buy_wallet_custom"}],
                [{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}],
            ]}
            safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "buy_wallet_custom":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "buy_wallet_amount", {})
        safe_edit_or_send(
            chat_id, message_id,
            "💰 Bu — <b>Stars hamyoni</b>, u yuklab olish limitingizga TA'SIR QILMAYDI, "
            "faqat publish (kanalga post) xizmati uchun ishlatiladi.\n\n"
            "Nechta Star to'ldirmoqchisiz? (1 dan 10000 gacha, masalan: 10):",
            parse_mode_html=True,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "buy_premium":
        answer_callback_query(cq_id)
        cfg = get_limit_config()
        price = cfg["premium_price_stars"]
        days = cfg["premium_days"]
        months = round(days / 30.4, 1)
        log.info("buy_premium: user=%s chat_id=%s price=%s calling sendInvoice", user_id, chat_id, price)
        result = tg_call(
            "sendInvoice", chat_id=chat_id, title=f"StokerDownloader Premium ({months} oy)",
            description=f"Cheksiz pack yuklab olish, kunlik/haftalik limitlarsiz — {months} oy muddatga.",
            payload=f"premium:{user_id}", provider_token="", currency="XTR",
            prices=[{"label": f"Premium {months} oy", "amount": price}],
        )
        log.info("buy_premium: user=%s sendInvoice result=%r", user_id, result)
        if not result or not result.get("ok"):
            err = result.get("description", "noma'lum xato") if result else "javob yo'q"
            send_message(chat_id, f"⚠️ To'lov havolasini yaratishda xato: {err}", reply_markup=back_to_menu_keyboard())
            notify_admin_error("Premium sotib olish invoice", user_id=user_id, extra=err)
        return

    if data == "buy_limit_custom":
        answer_callback_query(cq_id)
        cfg = get_limit_config()
        ratio = cfg["stars_per_limit"]
        set_pending_input(user_id, "buy_limit_amount", {})
        safe_edit_or_send(
            chat_id, message_id,
            "📊 Bu — <b>yuklab olish limitingizni</b> oshirish (hamyon emas).\n\n"
            f"Nechta Star to'lamoqchisiz? Har bir Star uchun {ratio} ta limit qo'shiladi "
            f"(1 dan 10000 gacha, masalan: 50):",
            parse_mode_html=True,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # ---------- Backup ----------
    # ---------- Userbot (Telethon) backup — FAQAT admin/superadmin ----------
    if data == "userbot_menu":
        answer_callback_query(cq_id)
        if not is_admin(user_id):
            return
        sess = get_userbot_session(user_id)
        if sess and sess.get("session_string"):
            phone = sess.get("phone", "?")
            text = (
                f"🗄 <b>Backup (API orqali)</b>\n\n"
                f"✅ Ulangan akkount: {phone}\n\n"
                f"Bu — Bot API emas, sizning shaxsiy Telegram akkountingiz orqali ishlaydi "
                f"va istalgan chatning <u>TO'LIQ TARIXINI</u> o'qiy oladi (bot admin bo'lishi shart emas)."
            )
            keyboard = {"inline_keyboard": [
                [{"text": "📂 Chatlar ro'yxati", "callback_data": "userbot_dialogs:0"}],
                [{"text": "🔌 Akkountni uzish", "callback_data": "userbot_disconnect"}],
                [{"text": "⬅️ Admin panel", "callback_data": "menu_admin_panel"}],
            ]}
        else:
            text = (
                "🗄 <b>Backup (API orqali)</b>\n\n"
                "Bu funksiya ishlashi uchun shaxsiy Telegram akkountingizni botga ulashingiz kerak "
                "(my.telegram.org saytidan olinadigan API_ID/API_HASH + telefon raqamingiz orqali).\n\n"
                "⚠️ <b>Diqqat:</b> ulangandan keyin bot sizning akkountingiz nomidan Telegram'ga "
                "so'rov yubora oladi — bu amalda akkountingizga to'liq kirish huquqi degani. "
                "Faqat o'zingiz to'liq ishonadigan holatda davom eting."
            )
            keyboard = {"inline_keyboard": [
                [{"text": "🔑 Akkountni ulash", "callback_data": "userbot_connect_start"}],
                [{"text": "⬅️ Admin panel", "callback_data": "menu_admin_panel"}],
            ]}
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "userbot_connect_start":
        answer_callback_query(cq_id)
        if not is_admin(user_id):
            return
        set_pending_input(user_id, "userbot_api_id", {})
        safe_edit_or_send(
            chat_id, message_id,
            "1/4 — my.telegram.org saytidan olingan <b>API_ID</b> raqamingizni yuboring:",
            parse_mode_html=True, reply_markup=back_to_panel_keyboard(),
        )
        return

    if data == "userbot_disconnect":
        answer_callback_query(cq_id)
        if not is_admin(user_id):
            return
        delete_userbot_session(user_id)
        safe_edit_or_send(chat_id, message_id, "🔌 Akkount uzildi.", reply_markup=back_to_panel_keyboard())
        return

    if data.startswith("userbot_dialogs:"):
        answer_callback_query(cq_id, "Yuklanmoqda...")
        if not is_admin(user_id):
            return
        page = int(data.split(":", 1)[1])
        try:
            dialogs = run_userbot_coro(_userbot_list_dialogs(user_id))
        except Exception as e:
            log.exception("Userbot dialog ro'yxatini olishda xato: %s", e)
            safe_edit_or_send(chat_id, message_id, f"❌ Xato: {e}", reply_markup=back_to_panel_keyboard())
            return
        if dialogs is None:
            safe_edit_or_send(
                chat_id, message_id,
                "❌ Akkount ulanmagan yoki sessiya o'chgan (masalan boshqa qurilmadan chiqib ketilgan bo'lishi "
                "mumkin). Qaytadan ulang.",
                reply_markup=back_to_panel_keyboard(),
            )
            return
        _userbot_dialog_cache[user_id] = dialogs
        items = [
            (d["id"], (("👥 " if d["is_group"] else "📢 " if d["is_channel"] else "👤 ") + d["title"]))
            for d in dialogs
        ]
        PAGE_SIZE = 8
        start_idx = page * PAGE_SIZE
        rows = [[{"text": label[:60], "callback_data": f"userbot_pick:{did}"}]
                for did, label in items[start_idx:start_idx + PAGE_SIZE]]
        nav = []
        if page > 0:
            nav.append({"text": "⬅️", "callback_data": f"userbot_dialogs:{page - 1}"})
        if start_idx + PAGE_SIZE < len(items):
            nav.append({"text": "➡️", "callback_data": f"userbot_dialogs:{page + 1}"})
        if nav:
            rows.append(nav)
        rows.append([{"text": "⬅️ Orqaga", "callback_data": "userbot_menu"}])
        safe_edit_or_send(chat_id, message_id, f"📂 Zaxiralamoqchi bo'lgan chatni tanlang ({len(items)}):",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("userbot_pick:"):
        answer_callback_query(cq_id)
        if not is_admin(user_id):
            return
        dialog_id = int(data.split(":", 1)[1])
        rows = [[{"text": label, "callback_data": f"userbot_run:{dialog_id}:{code}"}]
                for code, label, _days in USERBOT_BACKUP_RANGES]
        rows.append([{"text": "⬅️ Orqaga", "callback_data": "userbot_dialogs:0"}])
        safe_edit_or_send(chat_id, message_id, "Qaysi davr uchun zaxira olamiz?",
                           reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("userbot_run:"):
        answer_callback_query(cq_id, "Boshlandi, kuting...")
        if not is_admin(user_id):
            return
        _, dialog_id_s, code = data.split(":", 2)
        dialog_id = int(dialog_id_s)
        days = next((d for c, _l, d in USERBOT_BACKUP_RANGES if c == code), 7)
        cached = _userbot_dialog_cache.get(user_id, [])
        title = next((d["title"] for d in cached if d["id"] == dialog_id), str(dialog_id))
        run_safe_thread(_send_userbot_backup_zip, user_id, dialog_id, title, days, user_id, chat_id=user_id)
        safe_edit_or_send(
            chat_id, message_id,
            "📦 To'liq tarix o'qilmoqda va ZIP tayyorlanmoqda — katta chatlar uchun bir necha "
            "daqiqa vaqt olishi mumkin, tayyor bo'lgach avtomatik yuboriladi...",
            reply_markup=back_to_panel_keyboard(),
        )
        return

    if data == "menu_bioclock":
        answer_callback_query(cq_id)
        _render_bioclock_screen(chat_id, message_id, user_id)
        return

    if data.startswith("bioclock_target:"):
        answer_callback_query(cq_id)
        target = data.split(":", 1)[1]
        _render_bioclock_target_screen(chat_id, message_id, user_id, target)
        return

    if data.startswith("bioclock_toggle:"):
        answer_callback_query(cq_id)
        target = data.split(":", 1)[1]
        if not get_business_connection_id(user_id):
            return
        t = get_bio_clock_target(user_id, target)
        was_enabled = bool(t.get("enabled"))
        log.info("Bio soat: TOGGLE bosildi (owner=%s, target=%s, %s -> %s)",
                 user_id, target, was_enabled, not was_enabled)
        set_bio_clock_target(user_id, target, enabled=not was_enabled)
        confirm = get_bio_clock_target(user_id, target)
        log.info("Bio soat: TOGGLE'dan keyin saqlangan qiymat (owner=%s, target=%s): %s",
                 user_id, target, confirm)
        if not was_enabled:
            threading.Thread(target=_bio_clock_tick, daemon=True).start()  # 1 daqiqa kutmasin
        _render_bioclock_target_screen(chat_id, message_id, user_id, target)
        return

    if data.startswith("bioclock_edit:"):
        answer_callback_query(cq_id)
        target = data.split(":", 1)[1]
        if not get_business_connection_id(user_id):
            return
        set_pending_input(user_id, "bioclock_template", {"target": target})
        label = BIO_CLOCK_TARGETS[target]["label"]
        example = DEFAULT_BIO_CLOCK_TEMPLATE if target == "bio" else "Muslihiddin"
        safe_edit_or_send(
            chat_id, message_id,
            f"{label} uchun — vaqtdan OLDIN nima yozilib tursin? Xohlagan matningni yoz — vaqt "
            "avtomatik oxiriga qo'shiladi.\n\n"
            f"Masalan \"{example}\" yozsang, \"{example} 14:35\" kabi chiqadi.\n\n"
            "Faqat vaqtning o'zi chiqishini xohlasang — bo'sh joy (bitta probel) yuborsang bo'ldi.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "bioclock_digits_start":
        answer_callback_query(cq_id)
        if not get_business_connection_id(user_id):
            return
        set_pending_input(user_id, "bioclock_digits")
        safe_edit_or_send(
            chat_id, message_id,
            "Raqamlaringizni o'z uslubingizda, CHIZIQCHA bilan ajratib, shu tartibda yuboring:\n"
            "1 - 2 - 3 - 4 - 5 - 6 - 7 - 8 - 9 - 0\n\n"
            "Masalan shunday yozing (nusxa oling, o'zingiznikiga almashtiring):\n"
            "①-②-③-④-⑤-⑥-⑦-⑧-⑨-⓪\n\n"
            "Aynan 10 ta belgi, chiziqcha bilan ajratilgan bo'lishi kerak, tartib yuqoridagidek: "
            "1,2,3,4,5,6,7,8,9,0. Bu barcha yoqilgan joylar (bio, ism, familiya) uchun bir xilda ishlatiladi.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "bioclock_digits_reset":
        answer_callback_query(cq_id)
        if not get_business_connection_id(user_id):
            return
        clear_bio_clock_digit_map(user_id)
        _render_bioclock_screen(chat_id, message_id, user_id)
        return

    if data == "bioclock_extra_start":
        answer_callback_query(cq_id)
        if not get_business_connection_id(user_id):
            return
        if not is_premium(user_id):
            safe_edit_or_send(
                chat_id, message_id,
                "✨ Bu — Premium foydalanuvchilar uchun. Bio soatingizga vaqtdan tashqari "
                "qo'shimcha matn/emoji qo'shish imkoniyati beradi.",
                reply_markup={"inline_keyboard": [
                    [{"text": "⭐ Premium olish", "callback_data": "menu_premium"}],
                    [{"text": "⬅️ Orqaga", "callback_data": "menu_bioclock"}],
                ]},
            )
            return
        set_pending_input(user_id, "bioclock_extra")
        cfg = get_bio_clock_config(user_id) or {}
        bio_t = cfg.get("targets", {}).get("bio", {})
        used = len(format_bio_clock(bio_t.get("template"), cfg.get("digit_map"), max_len=BIO_MAX_LEN))
        safe_edit_or_send(
            chat_id, message_id,
            "Vaqtdan KEYIN qo'shiladigan matn/emojini yozing (bio uchun; ism/familiya qisqaroq "
            "chegaraga ega, u yerda avtomatik qisqaroq qo'llaniladi).\n\n"
            f"Joriy sozlamalaringiz ({used} belgi) hisobga olinib, taxminan "
            f"{max(0, BIO_MAX_LEN - used - 1)} belgigacha joy bor.\n\n"
            "O'chirish uchun \"yo'q\" deb yozing.",
            reply_markup=back_to_menu_keyboard(),
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

    if data.startswith("publish_single:") or data.startswith("publish_pack:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Stikerni qayta forward qiling.")
            return
        if data.startswith("publish_pack:"):
            handle_pack_publish_request(chat_id, pending["pack_name"], user_id)
        else:
            handle_single_publish_request(chat_id, pending, user_id)
        return

    if data.startswith("dl_direct_id:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. ID'ni qayta yuboring.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        _finish_direct_file_id_zip(chat_id, pending, user_id)
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

    if data.startswith("suggest_pack:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending or not pending.get("pack_name"):
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan.")
            return
        pack_name = pending["pack_name"]
        edit_message_text(chat_id, message_id, f"⏳ '{pack_name}' qidirilmoqda...")
        handle_pack_request(chat_id, pack_name, requester_label({"id": user_id}), user_id)
        return

    if data.startswith("similar_packs:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending or not pending.get("packs"):
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan.")
            return
        rows = []
        for pname in pending["packs"]:
            ptoken = store_pending_choice({"pack_name": pname})
            rows.append([{"text": pname, "callback_data": f"suggest_pack:{ptoken}"}])
        edit_message_text(chat_id, message_id, "Shu emoji bilan bog'liq boshqa pack'lar:",
                          reply_markup={"inline_keyboard": rows})
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

    if data.startswith("dl_gif_webm:") or data.startswith("dl_gif_id:") or data.startswith("dl_video_sticker:"):
        answer_callback_query(cq_id)
        token = data.split(":", 1)[1]
        pending = pop_pending_choice(token)
        if not pending:
            edit_message_text(chat_id, message_id, "⏱ Bu tanlov muddati o'tgan. Faylni qaytadan yuboring.")
            return
        edit_message_text(chat_id, message_id, "⏳ Tayyorlanmoqda...")
        if data.startswith("dl_gif_webm:"):
            handle_gif_webm_request(chat_id, pending, user_id)
        elif data.startswith("dl_video_sticker:"):
            handle_video_to_sticker_request(chat_id, pending, user_id)
        else:
            handle_gif_id_request(chat_id, pending, user_id)
        return

    if data == "reak_pick_random":
        answer_callback_query(cq_id)
        pending = get_pending_input(user_id) or {}
        if pending.get("action") != "reak_pick_emoji":
            answer_callback_query(cq_id, "Bu tanlov muddati o'tgan.", show_alert=True)
            return
        pdata = pending.get("data") or {}
        target_chat_id = pdata.get("chat_id")
        clear_pending_input(user_id)
        if not target_chat_id:
            return
        set_reak_mode(target_chat_id, None, set_by=user_id, random_mode=True)
        safe_edit_or_send(chat_id, message_id,
                           "✅ Reak mode (🎲 Random) yoqildi: endi har bir yangi xabarga tasodifiy emoji qo'yiladi.")
        return

    if data.startswith("reak_pick:"):
        answer_callback_query(cq_id)
        emoji = data.split(":", 1)[1]
        pending = get_pending_input(user_id) or {}
        if pending.get("action") != "reak_pick_emoji":
            answer_callback_query(cq_id, "Bu tanlov muddati o'tgan.", show_alert=True)
            return
        pdata = pending.get("data") or {}
        target_chat_id = pdata.get("chat_id")
        clear_pending_input(user_id)
        if not target_chat_id:
            return
        set_reak_mode(target_chat_id, emoji, set_by=user_id)
        safe_edit_or_send(chat_id, message_id, f"✅ Reak mode yoqildi: {emoji} endi har bir yangi xabarga qo'yiladi.")
        return

    # ---- Quyidagilar faqat adminlar uchun ----
    if not is_admin(user_id):
        answer_callback_query(cq_id)
        return

    if data == "menu_admin_panel":
        answer_callback_query(cq_id)
        safe_edit_or_send(chat_id, message_id, "🛠 Boshqaruv paneli:", reply_markup=admin_panel_keyboard(user_id))
        return

    if data == "toggle_hack_mode":
        if user_id != SUPERADMIN_ID:
            answer_callback_query(cq_id, "Bu faqat superadmin uchun.", show_alert=True)
            return
        new_value = toggle_hack_mode()
        answer_callback_query(cq_id, "Hack mode: " + ("yoqildi 🟢" if new_value else "o'chirildi 🔴"))
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

    if data == "panel_user_search":
        answer_callback_query(cq_id)
        keyboard = {"inline_keyboard": [
            [{"text": "🆔 ID orqali", "callback_data": "usearch_by_id"}],
            [{"text": "🔤 Harflab", "callback_data": "usearch_letters:"}],
            [{"text": "⬅️ Panel", "callback_data": "menu_admin_panel"}],
        ]}
        safe_edit_or_send(chat_id, message_id, "🔎 User qidirish — usulni tanlang:", reply_markup=keyboard)
        return

    if data == "usearch_by_id":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "usearch_id_input", {})
        safe_edit_or_send(chat_id, message_id, "Foydalanuvchi ID'sini yuboring:",
                           reply_markup=back_to_panel_keyboard())
        return

    if data.startswith("usearch_letters:"):
        answer_callback_query(cq_id)
        collected = data.split(":", 1)[1]
        results = search_users_by_prefix(collected) if collected else []
        set_pending_input(user_id, "usearch_letters_state", {"collected": collected})
        title = f"🔤 Harflab qidiruv\n\nYig'ilgan: <code>{collected or '—'}</code>\nTopilgan: {len(results)} ta"
        safe_edit_or_send(chat_id, message_id, title, parse_mode_html=True,
                           reply_markup=user_search_letters_keyboard(collected, results))
        return

    if data.startswith("usearch_letter:"):
        answer_callback_query(cq_id)
        letter = data.split(":", 1)[1]
        pending = get_pending_input(user_id) or {}
        collected = (pending.get("data") or {}).get("collected", "") if pending.get("action") == "usearch_letters_state" else ""
        collected += letter
        results = search_users_by_prefix(collected)
        set_pending_input(user_id, "usearch_letters_state", {"collected": collected})
        title = f"🔤 Harflab qidiruv\n\nYig'ilgan: <code>{collected}</code>\nTopilgan: {len(results)} ta"
        safe_edit_or_send(chat_id, message_id, title, parse_mode_html=True,
                           reply_markup=user_search_letters_keyboard(collected, results))
        return

    if data == "usearch_backspace":
        answer_callback_query(cq_id)
        pending = get_pending_input(user_id) or {}
        collected = (pending.get("data") or {}).get("collected", "") if pending.get("action") == "usearch_letters_state" else ""
        collected = collected[:-1]
        results = search_users_by_prefix(collected) if collected else []
        set_pending_input(user_id, "usearch_letters_state", {"collected": collected})
        title = f"🔤 Harflab qidiruv\n\nYig'ilgan: <code>{collected or '—'}</code>\nTopilgan: {len(results)} ta"
        safe_edit_or_send(chat_id, message_id, title, parse_mode_html=True,
                           reply_markup=user_search_letters_keyboard(collected, results))
        return

    if data == "usearch_clear":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "usearch_letters_state", {"collected": ""})
        title = "🔤 Harflab qidiruv\n\nYig'ilgan: <code>—</code>\nTopilgan: 0 ta"
        safe_edit_or_send(chat_id, message_id, title, parse_mode_html=True,
                           reply_markup=user_search_letters_keyboard("", []))
        return

    if data.startswith("usearch_page:"):
        answer_callback_query(cq_id)
        page = int(data.split(":", 1)[1])
        pending = get_pending_input(user_id) or {}
        collected = (pending.get("data") or {}).get("collected", "") if pending.get("action") == "usearch_letters_state" else ""
        results = search_users_by_prefix(collected) if collected else []
        title = f"🔤 Harflab qidiruv\n\nYig'ilgan: <code>{collected or '—'}</code>\nTopilgan: {len(results)} ta"
        safe_edit_or_send(chat_id, message_id, title, parse_mode_html=True,
                           reply_markup=user_search_letters_keyboard(collected, results, page=page))
        return

    if data.startswith("usearch_forward:"):
        answer_callback_query(cq_id)
        target_id = int(data.split(":", 1)[1])
        rec = get_user_record(target_id)
        src_chat = rec.get("last_chat_id")
        src_msg = rec.get("last_message_id")
        if not src_chat or not src_msg:
            answer_callback_query(cq_id, "Bu userdan hali xabar yo'q, forward qilib bo'lmaydi.", show_alert=True)
            return
        result = tg_call("forwardMessage", chat_id=chat_id, from_chat_id=src_chat, message_id=src_msg)
        if not (result and result.get("ok")):
            send_message(chat_id, "Forward qilishda xato yuz berdi (xabar o'chirilgan bo'lishi mumkin).")
        return

    if data.startswith("user_detail:"):
        answer_callback_query(cq_id)
        target_id = int(data.split(":", 1)[1])
        rec = get_user_record(target_id)
        counts = rec.get("type_counts", {}) or {}
        premium_label = "ha" if is_premium(target_id) else "yoq"
        mode, limit = compute_user_limit(target_id)
        period_label = "kunlik" if mode == "daily" else "haftalik"
        used = rec.get("count", 0)
        remaining = max(0, limit - used)
        text = (
            f"👤 <b>{user_label(target_id)}</b> (id:{target_id})\n\n"
            f"📊 Jami so'rovlar: {rec.get('lifetime_requests', 0)}\n"
            f"🖼 Sticker: {counts.get('sticker', 0)}\n"
            f"😀 Custom emoji: {counts.get('emoji', 0)}\n"
            f"🎞 GIF: {counts.get('gif', 0)}\n"
            f"📦 Pack: {counts.get('pack', 0)}\n\n"
            f"🔗 Referallar: {rec.get('referrals', 0)}\n"
            f"🎁 Bonus: {rec.get('bonus', 0)}\n"
            f"⭐ Premium: {premium_label}\n\n"
            f"📈 Joriy {period_label} limit: <b>{limit}</b> ta\n"
            f"   (ishlatilgan: {used}, qolgan: {remaining})\n"
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
        if not rec.get("username") and rec.get("last_message_id"):
            rows.append([{"text": "↪️ So'nggi xabarini forward qilish", "callback_data": f"usearch_forward:{target_id}"}])
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
        _, current_limit = compute_user_limit(target_id)
        set_pending_input(user_id, "give_limit_amount", {"target_id": target_id})
        safe_edit_or_send(
            chat_id, message_id,
            f"id:{target_id} — joriy yakuniy limit: {current_limit} ta.\n"
            f"Ustiga QO'SHILADIGAN (bonus) miqdorni yozing (masalan: 5):",
            reply_markup=back_to_panel_keyboard(),
        )
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
                f"o'zingiz qo'shilishingiz mumkin:\n{link}", decoration_key="m18725027",
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
        rows.append([{"text": "🔍 ID orqali qidirish", "callback_data": "panel_dm_search_id"}])
        rows.append([{"text": "⬅️ Admin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, f"💬 Kimga xabar yubormoqchisiz? ({len(items)} foydalanuvchi)",
                           reply_markup={"inline_keyboard": rows})
        return

    if data == "panel_dm_search_id":
        answer_callback_query(cq_id)
        set_pending_input(user_id, "dm_search_id", {})
        safe_edit_or_send(chat_id, message_id, "🔍 Qidirilayotgan foydalanuvchining Telegram ID raqamini kiriting:",
                           reply_markup=back_to_panel_keyboard())
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
                r = send_message(aid, f"✉️ <b>{pending['from_label']}</b> dan xabar:\n\n{pending['text']}", decoration_key="m90b944ef",
                                  parse_mode_html=True)
                if r.get("ok"):
                    sent += 1
            edit_message_text(chat_id, message_id, f"✅ Tasdiqlandi. Xabar {sent} ta adminga yuborildi.")
            send_message(pending["from_id"], f"✅ Xabaringiz superadmin tomonidan tasdiqlandi va {sent} ta adminga yuborildi.", decoration_key="m93d04d03")
        else:
            edit_message_text(chat_id, message_id, "❌ Rad etildi.")
            send_message(pending["from_id"], "❌ Xabaringiz superadmin tomonidan rad etildi.")
        return

    if data == "panel_limits":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        text, keyboard = _render_limits_panel_text_and_keyboard()
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "panel_stars":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        balance_result = tg_call("getMyStarBalance")
        if not balance_result or not balance_result.get("ok"):
            safe_edit_or_send(chat_id, message_id, "⚠️ Star balansini olishda xato yuz berdi. Qayta urinib ko'ring.",
                               reply_markup=back_to_panel_keyboard())
            return
        balance = balance_result["result"].get("amount", 0)

        gifts_result = tg_call("getAvailableGifts")
        text = f"⭐ <b>Bot Stars balansi:</b> {balance}\n\n"
        rows = []
        if gifts_result and gifts_result.get("ok"):
            gifts = gifts_result["result"].get("gifts", [])
            # faqat botning balansi yetadigan gift'larni ko'rsatamiz, eng
            # arzonidan boshlab. Limited (soni cheklangan) gift'lar tugab
            # qolishi mumkin (remaining_count=0) — bunday holda sendGift
            # GIFT_INVALID xato beradi, shuning uchun ularni chiqarib
            # tashlaymiz.
            def _still_available(g):
                if "remaining_count" in g and g.get("remaining_count") is not None:
                    return g["remaining_count"] > 0
                return True  # cheklanmagan (doimiy) gift

            affordable = [
                g for g in gifts
                if g.get("star_count", 0) <= balance and _still_available(g)
            ]
            affordable.sort(key=lambda g: g.get("star_count", 0))
            if not affordable:
                text += "Hozircha balans hech qanday gift sotib olishga yetmaydi."
            else:
                text += "Quyidagilardan birini tanlang (kimga yuborishni keyin so'rayman):"
                for g in affordable[:16]:
                    rows.append([{
                        "text": f"🎁 {g.get('star_count', 0)}⭐",
                        "callback_data": f"stars_gift_pick:{g['id']}",
                    }])
        else:
            text += "⚠️ Gift'lar ro'yxatini olishda xato yuz berdi."
        rows.append([{"text": "⬅️ Superadmin panel", "callback_data": "menu_admin_panel"}])
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("stars_gift_pick:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        gift_id = data.split(":", 1)[1]
        rows = [
            [{"text": "🙋 O'zimga (superadmin)", "callback_data": f"stars_gift_send:{gift_id}:{SUPERADMIN_ID}"}],
        ]
        with _state_lock:
            for admin_id in STATE.get("admins", []):
                rows.append([{
                    "text": f"👤 {user_label(admin_id)}",
                    "callback_data": f"stars_gift_send:{gift_id}:{admin_id}",
                }])
        rows.append([{"text": "✍️ Boshqa ID kiritish", "callback_data": f"stars_gift_custom:{gift_id}"}])
        rows.append([{"text": "⬅️ Orqaga", "callback_data": "panel_stars"}])
        safe_edit_or_send(chat_id, message_id, "Bu gift'ni kimga yuboraman?", reply_markup={"inline_keyboard": rows})
        return

    if data.startswith("stars_gift_custom:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        gift_id = data.split(":", 1)[1]
        set_pending_input(user_id, "stars_gift_send_custom", {"gift_id": gift_id})
        safe_edit_or_send(chat_id, message_id, "Qabul qiluvchining Telegram ID raqamini yuboring:",
                           reply_markup=back_to_panel_keyboard())
        return

    if data.startswith("stars_gift_send:"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        _, gift_id, target_id = data.split(":", 2)
        target_id = int(target_id)
        result = tg_call("sendGift", user_id=target_id, gift_id=gift_id)
        if result and result.get("ok"):
            safe_edit_or_send(chat_id, message_id,
                               f"✅ Gift muvaffaqiyatli yuborildi (id:{target_id}).\n"
                               f"Qabul qiluvchi buni o'z Telegram profilida ko'radi va xohlasa "
                               f"\"Stars'ga aylantirish\" orqali o'z Star balansiga o'tkazishi mumkin.",
                               reply_markup=back_to_panel_keyboard())
        else:
            err = result.get("description", "noma'lum xato") if result else "javob yo'q"
            safe_edit_or_send(chat_id, message_id, f"⚠️ Gift yuborishda xato: {err}",
                               reply_markup=back_to_panel_keyboard())
        return

    if data in ("limit_kw_dec", "limit_kw_inc"):
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        cfg = get_limit_config()
        current = cfg.get("keyword_free_limit", 2)
        new_val = max(0, current - 1) if data == "limit_kw_dec" else current + 1
        set_config_value("keyword_free_limit", new_val)
        text, keyboard = _render_limits_panel_text_and_keyboard()
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
        text, keyboard = _render_limits_panel_text_and_keyboard()
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup=keyboard)
        return

    if data == "edit_premium_price":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "edit_premium_price", {})
        safe_edit_or_send(chat_id, message_id, "Yangi Premium narxini Stars'da kiriting (masalan: 300):",
                           reply_markup=back_to_panel_keyboard())
        return

    if data == "edit_premium_days":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "edit_premium_days", {})
        safe_edit_or_send(chat_id, message_id, "Yangi Premium muddatini kunlarda kiriting (masalan: 182):",
                           reply_markup=back_to_panel_keyboard())
        return

    if data == "edit_stars_ratio":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "edit_stars_ratio", {})
        safe_edit_or_send(chat_id, message_id, "1 Star necha limitga teng bo'lsin? Butun son kiriting (masalan: 1):",
                           reply_markup=back_to_panel_keyboard())
        return

    if data == "edit_publish_price":
        answer_callback_query(cq_id)
        if user_id != SUPERADMIN_ID:
            return
        set_pending_input(user_id, "edit_publish_price", {})
        safe_edit_or_send(chat_id, message_id, "1 marta publish qilish necha Stars tursin? Butun son kiriting (masalan: 1):",
                           reply_markup=back_to_panel_keyboard())
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
    log.info("handle_pending_input: user=%s text=%r pending=%r", user_id, text, pending)
    if not pending:
        return False

    action = pending["action"]
    if action in ("buy_wallet_amount", "buy_limit_amount"):
        try:
            who = user_label_for_admin(user_id)
            notify_admin(
                f"🔎 DEBUG: action ajratildi = {action!r} (turi: {type(action).__name__}), "
                f"user={who}, text={text!r}",
                reply_markup=dm_button_for_user(user_id),
            )
        except Exception:
            pass

    if action == "zip_publish_title":
        clear_pending_input(user_id)
        title = text.strip()[:64]
        if not title:
            send_message(chat_id, "Sarlavha bo'sh bo'lmasin. Bekor qilindi.")
            return True
        data = dict(pending["data"])
        data["title"] = title
        set_pending_input(user_id, "zip_publish_type", data)
        send_message(
            chat_id,
            "To'plam turini tanlang — yozib yuboring:\n"
            "• <code>emoji</code> — Custom Emoji sifatida (matn ichida ishlatiladi, 200 tagacha)\n"
            "• <code>sticker</code> — oddiy Sticker sifatida (50 tagacha)",
            parse_mode_html=True,
        )
        return True

    if action == "zip_publish_type":
        raw = text.strip().lower()
        if raw not in ("emoji", "sticker"):
            send_message(chat_id, "Faqat \"emoji\" yoki \"sticker\" deb yozing.")
            return True
        clear_pending_input(user_id)
        data = dict(pending["data"])
        data["sticker_type"] = "custom_emoji" if raw == "emoji" else "regular"
        set_pending_input(user_id, "zip_publish_emoji", data)
        send_message(chat_id, "Barcha stikerlarga biriktiriladigan bitta emoji yuboring (masalan: 😀):")
        return True

    if action == "zip_publish_emoji":
        clear_pending_input(user_id)
        emoji = text.strip()
        # oddiy tekshiruv: bitta emoji-uzunlikdagi belgi (ortiqcha matn kelsa ham birinchi belgisini olamiz)
        if not emoji:
            send_message(chat_id, "Emoji bo'sh bo'lmasin. Bekor qilindi.")
            return True
        emoji = emoji[:8]  # ba'zi emojilar bir nechta code point (masalan flag), ehtiyot chegarasi
        data = pending["data"]
        data["emoji"] = emoji
        _run_publish_flow(user_id, data)
        return True

    if action == "userbot_api_id":
        clear_pending_input(user_id)
        if not is_admin(user_id):
            return True
        raw = text.strip()
        if not raw.isdigit():
            send_message(chat_id, "❌ API_ID faqat raqamlardan iborat bo'lishi kerak. Qaytadan boshlang: /start")
            return True
        set_pending_input(user_id, "userbot_api_hash", {"api_id": raw})
        send_message(chat_id, "2/4 — endi <b>API_HASH</b> qatoringizni yuboring:", parse_mode_html=True)
        return True

    if action == "userbot_api_hash":
        clear_pending_input(user_id)
        if not is_admin(user_id):
            return True
        api_hash = text.strip()
        api_id = pending["data"].get("api_id")
        set_pending_input(user_id, "userbot_phone", {"api_id": api_id, "api_hash": api_hash})
        send_message(chat_id, "3/4 — telefon raqamingizni xalqaro formatda yuboring (masalan: +998901234567):")
        return True

    if action == "userbot_phone":
        clear_pending_input(user_id)
        if not is_admin(user_id):
            return True
        phone = text.strip()
        api_id = pending["data"].get("api_id")
        api_hash = pending["data"].get("api_hash")
        try:
            run_userbot_coro(_userbot_send_code(user_id, api_id, api_hash, phone))
        except ApiIdInvalidError:
            send_message(chat_id, "❌ API_ID/API_HASH noto'g'ri. Qaytadan boshlang: /start")
            return True
        except PhoneNumberInvalidError:
            send_message(chat_id, "❌ Telefon raqam formati noto'g'ri. Qaytadan boshlang: /start")
            return True
        except FloodWaitError as e:
            send_message(chat_id, f"⏳ Juda ko'p urinish, {e.seconds} soniyadan keyin qaytadan urinib ko'ring.")
            return True
        except Exception as e:
            log.exception("Userbot send_code xatosi: %s", e)
            send_message(chat_id, f"❌ Xato: {e}\nQaytadan boshlang: /start")
            return True
        set_pending_input(user_id, "userbot_code", {})
        send_message(chat_id, "4/4 — Telegram sizga (shu botga emas, asosiy Telegram ilovangizga) "
                               "yuborgan tasdiqlash kodini kiriting:")
        return True

    if action == "userbot_code":
        clear_pending_input(user_id)
        if not is_admin(user_id):
            return True
        code = text.strip()
        try:
            result = run_userbot_coro(_userbot_sign_in_code(user_id, code))
        except (PhoneCodeInvalidError, PhoneCodeExpiredError):
            send_message(chat_id, "❌ Kod noto'g'ri yoki eskirgan. Qaytadan boshlang: /start")
            return True
        except Exception as e:
            log.exception("Userbot sign_in (code) xatosi: %s", e)
            send_message(chat_id, f"❌ Xato: {e}\nQaytadan boshlang: /start")
            return True
        if result == "need_password":
            set_pending_input(user_id, "userbot_password", {})
            send_message(chat_id, "🔐 Bu akkountda 2 bosqichli tasdiqlash (2FA) yoqilgan — parolingizni yuboring:")
            return True
        phone = _userbot_finalize_login(user_id)
        send_message(chat_id, f"✅ Akkount ulandi: {phone}\n\nEndi \"🗄 Backup (API orqali)\" bo'limidan "
                               f"chatlar ro'yxatini ko'rishingiz mumkin.")
        return True

    if action == "userbot_password":
        clear_pending_input(user_id)
        if not is_admin(user_id):
            return True
        password = text
        try:
            run_userbot_coro(_userbot_sign_in_password(user_id, password))
        except Exception as e:
            log.exception("Userbot sign_in (password) xatosi: %s", e)
            send_message(chat_id, f"❌ Parol noto'g'ri yoki xato: {e}\nQaytadan boshlang: /start")
            return True
        phone = _userbot_finalize_login(user_id)
        send_message(chat_id, f"✅ Akkount ulandi: {phone}\n\nEndi \"🗄 Backup (API orqali)\" bo'limidan "
                               f"chatlar ro'yxatini ko'rishingiz mumkin.")
        return True

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
        send_message(chat_id, f"«{trigger}» kelganda qanday javob yozilsin?", decoration_key="m066dff15", reply_markup=back_to_menu_keyboard())
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
            send_message(chat_id, f"✅ Kalit qo'shildi.{note}", decoration_key="m63613a88", reply_markup=back_to_menu_keyboard())
        else:
            send_message(chat_id, f"❌ {result}", decoration_key="m6c32a4dd", reply_markup=back_to_menu_keyboard())
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
        send_message(chat_id, f"✅ {label} endi premium emoji: ID {custom_emoji_id}", decoration_key="m9a6c4f85",
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
                              f"Endi shu bilan yuboraman:", decoration_key="mbe2dfffc", reply_markup=back_to_menu_keyboard())
        return True

    if action == "bioclock_template":
        clear_pending_input(user_id)
        data = pending.get("data") or {}
        target = data.get("target", "bio")
        info = BIO_CLOCK_TARGETS.get(target, BIO_CLOCK_TARGETS["bio"])
        prefix = text.strip()
        if prefix.lower() in ("-", "yo'q", "yoq", "hech narsa", "yoʻq"):
            prefix = ""
        set_bio_clock_target(user_id, target, template=prefix)
        cfg = get_bio_clock_config(user_id) or {}
        extra = cfg.get("extra") if (target == "bio" and is_premium(user_id)) else None
        preview = format_bio_clock(prefix, cfg.get("digit_map"), extra, max_len=info["max_len"])
        send_message(chat_id, f"✅ Saqlandi. {info['label']}'da shunday chiqadi: {preview}",
                     reply_markup=back_to_menu_keyboard())
        return True

    if action == "bioclock_digits":
        clear_pending_input(user_id)
        parts = [p.strip() for p in text.split("-")]
        parts = [p for p in parts if p]
        if len(parts) != 10:
            send_message(
                chat_id,
                f"10 ta belgi kerak edi, {len(parts)} ta topdim. Format: 1-2-3-4-5-6-7-8-9-0 "
                "tartibida, o'zingiznikini shu joylarga qo'yib yuboring.",
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        order = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]
        digit_map = dict(zip(order, parts))
        set_bio_clock_shared(user_id, digit_map=digit_map)
        cfg = get_bio_clock_config(user_id) or {}
        bio_t = cfg.get("targets", {}).get("bio", {})
        extra = cfg.get("extra") if is_premium(user_id) else None
        preview = format_bio_clock(bio_t.get("template"), digit_map, extra, max_len=BIO_MAX_LEN)
        send_message(chat_id, f"✅ Saqlandi. Masalan bio'da shunday chiqadi: {preview}",
                     reply_markup=back_to_menu_keyboard())
        return True

    if action == "bioclock_extra":
        clear_pending_input(user_id)
        extra = text.strip()
        if extra.lower() in ("-", "yo'q", "yoq", "hech narsa", "yoʻq"):
            extra = ""
        cfg = get_bio_clock_config(user_id) or {}
        bio_t = cfg.get("targets", {}).get("bio", {})
        base_len = len(format_bio_clock(bio_t.get("template"), cfg.get("digit_map"), max_len=BIO_MAX_LEN))
        if base_len + len(extra) + 1 > BIO_MAX_LEN:
            send_message(
                chat_id,
                f"Bu juda uzun bo'lib ketadi ({base_len + len(extra) + 1}/{BIO_MAX_LEN} belgi). "
                "Qisqaroq matn yuboring.",
                reply_markup=back_to_menu_keyboard(),
            )
            return True
        set_bio_clock_shared(user_id, extra=extra)
        preview = format_bio_clock(bio_t.get("template"), cfg.get("digit_map"), extra, max_len=BIO_MAX_LEN)
        send_message(chat_id, f"✅ Saqlandi. Bio'da shunday chiqadi: {preview}", reply_markup=back_to_menu_keyboard())
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
                                   f"avto-javob ishga tushadi.", decoration_key="mb7d9bf2b", reply_markup=back_to_menu_keyboard())
        return True

    # Faqat quyidagi amallar adminlar uchun mo'ljallangan — shuning uchun
    # is_admin tekshiruvi FAQAT shu ro'yxatdagi action'lar uchun ishlaydi.
    # (Oldin bu tekshiruv shartsiz edi va buy_limit_amount/buy_wallet_amount
    # kabi ODDIY foydalanuvchilar uchun mo'ljallangan amallarni ham
    # jimgina bloklab qo'yardi — bu BUG edi, tuzatildi.)
    ADMIN_ONLY_ACTIONS = ("dm_text", "admin_message", "give_limit_amount", "dm_search_id")
    if action in ADMIN_ONLY_ACTIONS and not is_admin(user_id):
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
                r = send_message(aid, f"✉️ <b>Superadmindan xabar:</b>\n\n{msg_text}", decoration_key="mc54503fd", parse_mode_html=True)
                if r.get("ok"):
                    sent += 1
            send_message(chat_id, f"✅ Xabaringiz {sent} ta adminga yuborildi.", decoration_key="mba322c07", reply_markup=back_to_panel_keyboard())
        else:
            token = store_pending_choice({"from_id": user_id, "from_label": sender_label, "text": msg_text})
            keyboard = {"inline_keyboard": [[
                {"text": "✅ Ruxsat berish", "callback_data": f"adminmsg_ok:{token}"},
                {"text": "❌ Rad etish", "callback_data": f"adminmsg_no:{token}"},
            ]]}
            send_message(SUPERADMIN_ID, f"✉️ <b>{sender_label}</b> boshqa adminlarga xabar yubormoqchi:\n\n{msg_text}", decoration_key="m91979466",
                         parse_mode_html=True, reply_markup=keyboard)
            send_message(chat_id, "📨 Xabaringiz superadminga tasdiq uchun yuborildi.", reply_markup=back_to_panel_keyboard())
        return True

    if action == "dm_search_id":
        clear_pending_input(user_id)
        raw = text.strip().lstrip("@")
        try:
            target_id = int(raw)
        except ValueError:
            send_message(chat_id, "❌ Noto'g'ri format. Faqat Telegram ID (butun son) kiriting.",
                         reply_markup=back_to_panel_keyboard())
            return True
        with _state_lock:
            known = target_id in STATE["known_users"]
        if not known:
            send_message(chat_id, f"❌ id:{target_id} bazada topilmadi (bot bilan hali gaplashmagan bo'lishi mumkin).",
                         reply_markup=back_to_panel_keyboard())
            return True
        set_pending_input(user_id, "dm_text", {"target_id": target_id})
        send_message(chat_id, f"💬 {user_label(target_id)}ga yuboriladigan xabarni yozing:",
                     reply_markup=back_to_panel_keyboard())
        return True

    if action == "usearch_id_input":
        clear_pending_input(user_id)
        raw = text.strip()
        try:
            target_id = int(raw)
        except ValueError:
            send_message(chat_id, "ID butun son bo'lishi kerak. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        rec = find_user_by_id(target_id)
        if not rec:
            send_message(chat_id, f"id:{target_id} — botda topilmadi.", reply_markup=back_to_panel_keyboard())
            return True
        label = user_label(target_id)
        lines = [f"👤 <b>{label}</b> (id:{target_id})"]
        rows = []
        if not rec.get("username") and rec.get("last_message_id"):
            lines.append("\nUsername yo'q — so'nggi xabarini forward qilib profilga o'tishingiz mumkin.")
            rows.append([{"text": "↪️ So'nggi xabarini forward qilish", "callback_data": f"usearch_forward:{target_id}"}])
        rows.append([{"text": "📋 To'liq profil", "callback_data": f"user_detail:{target_id}"}])
        rows.append([{"text": "⬅️ User qidirish", "callback_data": "panel_user_search"}])
        send_message(chat_id, "\n".join(lines), parse_mode_html=True, reply_markup={"inline_keyboard": rows})
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
        send_message(chat_id, f"✅ id:{target_id} uchun bonus limit +{amount} qo'shildi. Yangi {period} limit: {new_limit}", decoration_key="mfe36d993",
                     reply_markup=back_to_panel_keyboard())
        return True

    if action == "buy_limit_amount":
        clear_pending_input(user_id)
        log.info("buy_limit_amount: BLOKKA KIRDI user=%s text=%r", user_id, text)
        try:
            try:
                amount = int(text.strip())
            except ValueError:
                send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_menu_keyboard())
                return True
            if amount < 1 or amount > 10000:
                send_message(chat_id, "1 dan 10000 gacha son kiriting. Bekor qilindi.", reply_markup=back_to_menu_keyboard())
                return True
            cfg = get_limit_config()
            ratio = cfg["stars_per_limit"]
            gained = amount * ratio
            log.info("buy_limit_amount: user=%s calling sendInvoice amount=%s gained=%s", user_id, amount, gained)
            result = tg_call(
                "sendInvoice", chat_id=chat_id, title=f"{gained} ta qo'shimcha limit",
                description=f"{amount} Stars to'lab, {gained} ta qo'shimcha so'rov limiti olasiz (bir martalik, bonus sifatida qo'shiladi).",
                payload=f"buy_limit:{amount}:{user_id}", provider_token="", currency="XTR",
                prices=[{"label": f"{gained} ta limit", "amount": amount}],
            )
            log.info("buy_limit_amount: user=%s sendInvoice result=%r", user_id, result)
            if not result or not result.get("ok"):
                err = result.get("description", "noma'lum xato") if result else "javob yo'q"
                send_message(chat_id, f"⚠️ To'lov havolasini yaratishda xato: {err}", reply_markup=back_to_menu_keyboard())
                notify_admin_error(f"Qo'shimcha limit sotib olish invoice ({amount} Stars)", user_id=user_id, extra=err)
        except Exception as e:
            log.exception("buy_limit_amount: KUTILMAGAN XATO user=%s: %s", user_id, e)
            notify_admin_error("Qo'shimcha limit sotib olish (kutilmagan xato)", user_id=user_id, extra=str(e))
            send_message(chat_id, "⚠️ Kutilmagan xatolik. Qaytadan urinib ko'ring.", reply_markup=back_to_menu_keyboard())
        return True

    if action == "buy_wallet_amount":
        clear_pending_input(user_id)
        log.info("buy_wallet_amount: BLOKKA KIRDI user=%s text=%r", user_id, text)
        try:
            try:
                amount = int(text.strip())
            except ValueError:
                log.info("buy_wallet_amount: user=%s ValueError text=%r", user_id, text)
                send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_menu_keyboard())
                return True
            if amount < 1 or amount > 10000:
                log.info("buy_wallet_amount: user=%s out of range amount=%s", user_id, amount)
                send_message(chat_id, "1 dan 10000 gacha son kiriting. Bekor qilindi.", reply_markup=back_to_menu_keyboard())
                return True
            log.info("buy_wallet_amount: user=%s calling sendInvoice amount=%s", user_id, amount)
            result = tg_call(
                "sendInvoice", chat_id=chat_id, title=f"{amount} Stars hamyonga to'ldirish",
                description=f"{amount} Stars hamyoningizga qo'shiladi va publish kabi xizmatlar uchun ishlatishingiz mumkin bo'ladi.",
                payload=f"topup_wallet:{amount}:{user_id}", provider_token="", currency="XTR",
                prices=[{"label": f"{amount} Stars hamyon to'ldirish", "amount": amount}],
            )
            log.info("buy_wallet_amount: user=%s sendInvoice result=%r", user_id, result)
            if not result or not result.get("ok"):
                err = result.get("description", "noma'lum xato") if result else "javob yo'q"
                send_message(chat_id, f"⚠️ To'lov havolasini yaratishda xato: {err}", reply_markup=back_to_menu_keyboard())
                notify_admin_error(f"Hamyon to'ldirish invoice ({amount} Stars)", user_id=user_id, extra=err)
        except Exception as e:
            log.exception("buy_wallet_amount: KUTILMAGAN XATO user=%s: %s", user_id, e)
            notify_admin_error("Hamyon to'ldirish (kutilmagan xato)", user_id=user_id, extra=str(e))
            send_message(chat_id, "⚠️ Kutilmagan xatolik. Qaytadan urinib ko'ring.", reply_markup=back_to_menu_keyboard())
        return True

    if action == "edit_premium_price":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        try:
            value = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        if value < 1 or value > 10000:
            send_message(chat_id, "1 dan 10000 gacha son kiriting (Bot API cheklovi). Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        set_config_value("premium_price_stars", value)
        send_message(chat_id, f"✅ Premium narxi endi {value} Stars.", reply_markup=back_to_panel_keyboard())
        return True

    if action == "edit_premium_days":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        try:
            value = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        if value < 1:
            send_message(chat_id, "1 dan katta son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        set_config_value("premium_days", value)
        send_message(chat_id, f"✅ Premium muddati endi {value} kun.", reply_markup=back_to_panel_keyboard())
        return True

    if action == "edit_stars_ratio":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        try:
            value = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        if value < 1:
            send_message(chat_id, "1 dan katta son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        set_config_value("stars_per_limit", value)
        send_message(chat_id, f"✅ Endi 1 Star = {value} limit.", reply_markup=back_to_panel_keyboard())
        return True

    if action == "edit_publish_price":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        try:
            value = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        if value < 0:
            send_message(chat_id, "0 yoki undan katta son kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        set_config_value("publish_price_stars", value)
        send_message(chat_id, f"✅ Endi publish narxi {value} Stars.", reply_markup=back_to_panel_keyboard())
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
        send_message(chat_id, f"✅ id:{target_id} endi bot admini.", decoration_key="m57a14e3a", reply_markup=back_to_panel_keyboard())
        return True

    if action == "stars_gift_send_custom":
        clear_pending_input(user_id)
        if user_id != SUPERADMIN_ID:
            return True
        gift_id = pending["data"]["gift_id"]
        try:
            target_id = int(text.strip())
        except ValueError:
            send_message(chat_id, "Butun ID kiriting. Bekor qilindi.", reply_markup=back_to_panel_keyboard())
            return True
        result = tg_call("sendGift", user_id=target_id, gift_id=gift_id)
        if result and result.get("ok"):
            send_message(chat_id,
                          f"✅ Gift muvaffaqiyatli yuborildi (id:{target_id}).\n"
                          f"Qabul qiluvchi buni o'z Telegram profilida ko'radi va xohlasa "
                          f"\"Stars'ga aylantirish\" orqali o'z Star balansiga o'tkazishi mumkin.",
                          reply_markup=back_to_panel_keyboard())
        else:
            err = result.get("description", "noma'lum xato") if result else "javob yo'q"
            send_message(chat_id, f"⚠️ Gift yuborishda xato: {err}", reply_markup=back_to_panel_keyboard())
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
        send_message(chat_id, f"✅ Majburiy kanal qo'shildi: {ch['title']}", decoration_key="mc08e7370", reply_markup=back_to_panel_keyboard())
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
        send_message(chat_id, f"✅ Bonus kanal qo'shildi: {ch['title']}", decoration_key="mbe472968", reply_markup=back_to_panel_keyboard())
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
        send_message(chat_id, f"📣 Xabar {sent} ta foydalanuvchiga yuborildi.", decoration_key="m3649b7e8", reply_markup=back_to_panel_keyboard())
        return True

    log.warning("handle_pending_input: HECH BIR action mos kelmadi (fallback) — action=%r user=%s", action, user_id)
    notify_admin(f"🔎 DEBUG: handle_pending_input FALLBACK'ga tushdi — action={action!r}, user={user_id}")
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


def refresh_business_connection(conn_id):
    """Telegram'dan business connection'ning HAQIQIY joriy holatini so'raydi va
    lokal keshni shunga moslab yangilaydi. Bu, webhook o'tkazib yuborilgan bo'lsa
    (masalan hosting bir muddat uxlab qolgan / qayta ishga tushgan payt), lokal
    'enabled' bayrog'i abadiy eskirib qolib qolishining oldini oladi."""
    log.info("Bio soat: conn=%s uchun Telegram'dan JONLI holat so'ralmoqda...", conn_id)
    data = tg_call("getBusinessConnection", business_connection_id=conn_id)
    if not data or not data.get("ok"):
        log.warning("Bio soat: conn=%s uchun getBusinessConnection MUVAFFAQIYATSIZ: %s", conn_id, data)
        return None
    result = data["result"]
    owner = result.get("user", {})
    with _state_lock:
        entry = STATE.setdefault("business_connections", {}).setdefault(conn_id, {})
        old_enabled = entry.get("enabled")
        entry["owner_id"] = owner.get("id", entry.get("owner_id"))
        entry["enabled"] = result.get("is_enabled", entry.get("enabled", False))
        entry["first_name"] = owner.get("first_name", entry.get("first_name", ""))
        entry["last_name"] = owner.get("last_name", entry.get("last_name", ""))
        save_state_locked()
        log.info("Bio soat: conn=%s Telegram javobi: is_enabled=%s (eski kesh: %s) to'liq javob: %s",
                  conn_id, result.get("is_enabled"), old_enabled, result)
        return dict(entry)


def get_business_connection_id(owner_id):
    """Berilgan userning HOZIRGI faol business_connection_id'sini topadi (aksincha
    qidiruv). Kesh 'o'chirilgan' (enabled=False) deb ko'rsatgan ulanishlar uchun ham
    Telegram'dan JONLI holatni tekshiradi — o'tkazib yuborilgan webhook tufayli
    kesh noto'g'ri qotib qolmasligi uchun (pastdagi refresh_business_connection'ga
    qarang)."""
    with _state_lock:
        snapshot = list(STATE.get("business_connections", {}).items())
    for conn_id, conn in snapshot:
        if conn.get("owner_id") != owner_id:
            continue
        if conn.get("enabled", True):
            return conn_id
        refreshed = refresh_business_connection(conn_id)
        if refreshed and refreshed.get("enabled"):
            return conn_id
    return None


UZ_TZ = timezone(timedelta(hours=5))  # Asia/Tashkent, DST yo'q — sobit offset yetarli
DEFAULT_BIO_CLOCK_TEMPLATE = "🕐"
BIO_MAX_LEN = 140  # setBusinessAccountBio o'zi ruxsat beradigan chegara
NAME_MAX_LEN = 64  # setBusinessAccountName (ism/familiya) chegarasi
_bio_clock_state = {"last_tick": 0}  # faqat shu process uchun, saqlanmaydi — fon jarayoni
# to'xtab qolsa (masalan hosting worker qayta ishga tushsa) webhook orqali ushlab olish uchun
BIO_CLOCK_TARGETS = {
    "bio": {"label": "Bio", "max_len": BIO_MAX_LEN},
    "first_name": {"label": "Ism (first name)", "max_len": NAME_MAX_LEN},
    "last_name": {"label": "Familiya (last name)", "max_len": NAME_MAX_LEN},
}


def _migrate_bio_clock_cfg(cfg):
    """Eski (faqat bio, 'targets'siz) formatni yangi ko'p-maqsadli formatga o'giradi.
    (cfg, migration_boldimi) qaytaradi."""
    if "targets" not in cfg:
        old_enabled = cfg.pop("enabled", False)
        old_template = cfg.pop("template", DEFAULT_BIO_CLOCK_TEMPLATE)
        cfg["targets"] = {"bio": {"enabled": old_enabled, "template": old_template}}
        return cfg, True
    return cfg, False


def get_bio_clock_config(owner_id):
    with _state_lock:
        cfg = STATE.setdefault("bio_clock", {}).get(str(owner_id))
        if cfg is None:
            return None
        migrated, changed = _migrate_bio_clock_cfg(cfg)
        if changed:
            save_state_locked()
        return migrated


def get_bio_clock_target(owner_id, target):
    cfg = get_bio_clock_config(owner_id) or {}
    return cfg.get("targets", {}).get(target, {})


def set_bio_clock_target(owner_id, target, enabled=None, template=None):
    with _state_lock:
        cfg = STATE.setdefault("bio_clock", {}).setdefault(str(owner_id), {})
        cfg, _ = _migrate_bio_clock_cfg(cfg)
        t = cfg.setdefault("targets", {}).setdefault(target, {})
        if enabled is not None:
            t["enabled"] = enabled
        t.setdefault("enabled", False)
        if template is not None:
            t["template"] = template
        t.setdefault("template", DEFAULT_BIO_CLOCK_TEMPLATE if target == "bio" else "")
        save_state_locked()


def set_bio_clock_shared(owner_id, digit_map=None, extra=None):
    with _state_lock:
        cfg = STATE.setdefault("bio_clock", {}).setdefault(str(owner_id), {})
        cfg, _ = _migrate_bio_clock_cfg(cfg)
        if digit_map is not None:
            cfg["digit_map"] = digit_map
        if extra is not None:
            cfg["extra"] = extra
        save_state_locked()


def clear_bio_clock_digit_map(owner_id):
    with _state_lock:
        cfg = STATE.setdefault("bio_clock", {}).setdefault(str(owner_id), {})
        cfg.pop("digit_map", None)
        save_state_locked()


def any_bio_clock_target_enabled(owner_id):
    cfg = get_bio_clock_config(owner_id) or {}
    return any(t.get("enabled") for t in cfg.get("targets", {}).values())


def apply_digit_map(time_str, digit_map):
    if not digit_map:
        return time_str
    return "".join(digit_map.get(ch, ch) for ch in time_str)


def format_bio_clock(prefix, digit_map=None, extra=None, max_len=BIO_MAX_LEN):
    now = apply_digit_map(datetime.now(UZ_TZ).strftime("%H:%M"), digit_map)
    prefix = (prefix if prefix is not None else DEFAULT_BIO_CLOCK_TEMPLATE).strip()
    parts = [p for p in (prefix, now, (extra or "").strip()) if p]
    return " ".join(parts)[:max_len]


def _bio_clock_tick():
    with _state_lock:
        owner_ids = list(STATE.get("bio_clock", {}).keys())
        conn_snapshot = {cid: {"owner_id": c.get("owner_id"), "enabled": c.get("enabled", True)}
                          for cid, c in STATE.get("business_connections", {}).items()}
    updated = 0
    for uid in owner_ids:
        owner_id = int(uid)
        conn_id = get_business_connection_id(owner_id)
        if not conn_id:
            log.warning(
                "Bio soat: owner=%s uchun FAOL business_connection topilmadi, o'tkazib yuborildi. "
                "Hozirgi ulanishlar (conn_id -> {owner_id, enabled}): %s",
                owner_id, conn_snapshot,
            )
            continue
        cfg = get_bio_clock_config(owner_id) or {}
        targets = cfg.get("targets", {})
        digit_map = cfg.get("digit_map")
        extra = cfg.get("extra") if is_premium(owner_id) else None

        bio_t = targets.get("bio", {})
        fn_t, ln_t = targets.get("first_name", {}), targets.get("last_name", {})
        if not (bio_t.get("enabled") or fn_t.get("enabled") or ln_t.get("enabled")):
            log.warning("Bio soat: owner=%s uchun conn=%s topildi, lekin HECH BIR target yoqilmagan: %s",
                        owner_id, conn_id, targets)
            continue

        if bio_t.get("enabled"):
            text = format_bio_clock(bio_t.get("template"), digit_map, extra, max_len=BIO_MAX_LEN)
            result = tg_call("setBusinessAccountBio", business_connection_id=conn_id, bio=text)
            if result and result.get("ok"):
                updated += 1
                log.info("Bio soat: bio yangilandi (owner=%s): %s", owner_id, text)
            else:
                log.error("Bio soat: bio yangilanmadi (owner=%s, conn=%s): %s", owner_id, conn_id, result)

        if fn_t.get("enabled") or ln_t.get("enabled"):
            conn = STATE.get("business_connections", {}).get(conn_id, {})
            if fn_t.get("enabled"):
                first_name = format_bio_clock(fn_t.get("template"), digit_map, None, max_len=NAME_MAX_LEN)
            else:
                first_name = conn.get("first_name") or ""
            if ln_t.get("enabled"):
                last_name = format_bio_clock(ln_t.get("template"), digit_map, None, max_len=NAME_MAX_LEN)
            else:
                last_name = conn.get("last_name") or ""
            if not first_name:
                log.error("Bio soat: owner=%s uchun ism topilmadi (known conn data: %s), o'tkazib yuborildi",
                         owner_id, conn)
            else:
                params = {"business_connection_id": conn_id, "first_name": first_name}
                if last_name:
                    params["last_name"] = last_name
                result = tg_call("setBusinessAccountName", **params)
                if result and result.get("ok"):
                    updated += 1
                    log.info("Bio soat: ism yangilandi (owner=%s): %s %s", owner_id, first_name, last_name)
                else:
                    log.error("Bio soat: ism yangilanmadi (owner=%s, conn=%s): %s", owner_id, conn_id, result)

    _bio_clock_state["last_tick"] = time.time()
    if owner_ids:
        log.info("Bio soat tick: %s owner tekshirildi, %s ta yangilandi", len(owner_ids), updated)


def _render_bioclock_screen(chat_id, message_id, user_id):
    conn_id = get_business_connection_id(user_id)
    if not conn_id:
        text = (
            "🕐 <b>Bio soat</b>\n\n"
            "Bu funksiya ishlashi uchun botni Telegram Business orqali shaxsiy "
            "profilingizga ulashingiz kerak (Sozlamalar → Telegram Business → "
            "Chatbots) va \"profilni tahrirlash\" huquqini berishingiz kerak.\n\n"
            "Hozircha ulanish topilmadi."
        )
        safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True,
                           reply_markup={"inline_keyboard": [[{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}]]})
        return
    cfg = get_bio_clock_config(user_id) or {}
    targets = cfg.get("targets", {})
    digit_map = cfg.get("digit_map")
    user_is_premium = is_premium(user_id)
    extra = cfg.get("extra") if user_is_premium else None
    digit_sample = apply_digit_map("0123456789", digit_map) if digit_map else "standart (0123456789)"
    lines = [
        "🕐 <b>Bio soat</b>\n",
        "Vaqtni bio, ism va/yoki familiyangizda avtomatik (har daqiqa) ko'rsatib turadi "
        "(O'zbekiston vaqti). Har biri alohida yoqiladi:\n",
    ]
    rows = []
    for key, info in BIO_CLOCK_TARGETS.items():
        t = targets.get(key, {})
        on = bool(t.get("enabled"))
        mark = "✅" if on else "⚪️"
        preview = format_bio_clock(t.get("template"), digit_map, extra if key == "bio" else None,
                                    max_len=info["max_len"]) if on else "—"
        lines.append(f"{mark} <b>{info['label']}</b>: {html.escape(preview, quote=False)}")
        rows.append([{"text": f"{info['label']} sozlash", "callback_data": f"bioclock_target:{key}"}])
    lines.append(f"\nRaqamlar shrifti: {html.escape(digit_sample, quote=False)}")
    if user_is_premium:
        lines.append(f"Qo'shimcha matn (Premium, bio'ga): <code>{html.escape(extra, quote=False) if extra else '(yo\u02bcq)'}</code>")
    text = "\n".join(lines)
    rows.append([{"text": "🔢 Raqamlar shrifti", "callback_data": "bioclock_digits_start"}])
    if digit_map:
        rows.append([{"text": "↩️ Raqamlarni standartga qaytarish", "callback_data": "bioclock_digits_reset"}])
    rows.append([{"text": "✨ Qo'shimcha matn (Premium)", "callback_data": "bioclock_extra_start"}])
    rows.append([{"text": "⬅️ Bosh menyu", "callback_data": "menu_home"}])
    safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})


def _render_bioclock_target_screen(chat_id, message_id, user_id, target):
    if target not in BIO_CLOCK_TARGETS:
        _render_bioclock_screen(chat_id, message_id, user_id)
        return
    info = BIO_CLOCK_TARGETS[target]
    cfg = get_bio_clock_config(user_id) or {}
    t = cfg.get("targets", {}).get(target, {})
    enabled = bool(t.get("enabled"))
    template = t.get("template", DEFAULT_BIO_CLOCK_TEMPLATE if target == "bio" else "")
    digit_map = cfg.get("digit_map")
    extra = cfg.get("extra") if (target == "bio" and is_premium(user_id)) else None
    status = "yoqilgan ✅" if enabled else "o'chirilgan"
    preview = format_bio_clock(template, digit_map, extra, max_len=info["max_len"])
    text = (
        f"<b>{info['label']}</b>\n\n"
        f"Holati: {status}\n"
        f"Vaqtdan oldingi matn: <code>{html.escape(template, quote=False) or '(yo\u02bcq)'}</code>\n"
        f"Hozir shunday ko'rinadi ({len(preview)}/{info['max_len']} belgi): {html.escape(preview, quote=False)}"
    )
    toggle_label = "🚫 O'chirish" if enabled else "▶️ Yoqish"
    rows = [
        [{"text": toggle_label, "callback_data": f"bioclock_toggle:{target}"}],
        [{"text": "✏️ Matnni o'zgartirish", "callback_data": f"bioclock_edit:{target}"}],
        [{"text": "⬅️ Bio soat", "callback_data": "menu_bioclock"}],
    ]
    safe_edit_or_send(chat_id, message_id, text, parse_mode_html=True, reply_markup={"inline_keyboard": rows})


def _bio_clock_loop():
    log.info("Bio soat fon jarayoni ishga tushdi")
    while True:
        try:
            _bio_clock_tick()
        except Exception:
            log.exception("Bio soat tsiklida xato")
        time.sleep(30)


threading.Thread(target=_bio_clock_loop, daemon=True).start()
threading.Thread(target=_state_flusher_loop, daemon=True).start()
atexit.register(force_flush_state)  # process to'xtaganda saqlanmagan o'zgarish yo'qolmasin


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
                run_safe_thread(
                    _delayed_auto_reply,
                    owner_id, chat_id, reply_text, reply_entities, business_connection_id, time.time(), delay,
                )
            else:
                send_auto_reply(chat_id, reply_text, reply_entities, business_connection_id)


# ---------- Guruh ".zip" / ".zipstiker" (moderatsion, admin-only) ----------

def can_manage_reak_mode(chat_id, user_id):
    """/reak mode: on ni kim ishlata oladi: bot superadmini/admini (bepul),
    guruh egasi yoki guruh admini (to'lov bilan)."""
    if is_admin(user_id):
        return True
    return is_group_admin_or_owner(chat_id, user_id)


def can_disable_reak_mode(chat_id, user_id):
    """/reak mode: off ni faqat bot superadmini/admini yoki guruh egasi qila oladi."""
    if is_admin(user_id):
        return True
    return is_group_owner(chat_id, user_id)


# ---------- Hack mode (superadmin panelidan yoqib-o'chiriladi) ----------

def is_hack_mode_on():
    with _state_lock:
        return bool(STATE.get("hack_mode", False))


def toggle_hack_mode():
    with _state_lock:
        new_value = not bool(STATE.get("hack_mode", False))
        STATE["hack_mode"] = new_value
        save_state_locked()
        return new_value


def get_reak_mode(chat_id):
    with _state_lock:
        return STATE.get("reak_modes", {}).get(str(chat_id))


def set_reak_mode(chat_id, emoji, set_by, random_mode=False):
    with _state_lock:
        STATE.setdefault("reak_modes", {})[str(chat_id)] = {
            "emoji": emoji, "set_by": set_by, "random": random_mode,
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state_locked()


def clear_reak_mode(chat_id):
    with _state_lock:
        STATE.setdefault("reak_modes", {}).pop(str(chat_id), None)
        save_state_locked()


REAK_EMOJI_CHOICES = [
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👀", "🎃", "🙈", "😇", "😨", "🤝",
    "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒",
    "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷",
    "🤷‍♀", "😡",
]


def reak_emoji_pick_keyboard(random_selected=False):
    rows = []
    row = []
    for i, e in enumerate(REAK_EMOJI_CHOICES, 1):
        row.append({"text": e, "callback_data": f"reak_pick:{e}"})
        if i % 6 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    random_label = "🎲 Random (hammaga har xil) ✅" if random_selected else "🎲 Random (hammaga har xil)"
    rows.append([{"text": random_label, "callback_data": "reak_pick_random"}])
    return {"inline_keyboard": rows}


def handle_group_dot_commands(msg, chat_id, user_id, text):
    reply = msg.get("reply_to_message")
    stripped = text.strip()

    if stripped == ".del":
        if not can_moderate_group(chat_id, user_id):
            return True
        if not reply:
            send_message(chat_id, "O'chirmoqchi bo'lgan xabaringizga reply qilib .del yozing.")
            return True
        delete_message(chat_id, reply["message_id"])
        delete_message(chat_id, msg["message_id"])
        return True

    if stripped == ".ban" or stripped.startswith(".ban "):
        if not can_moderate_group(chat_id, user_id):
            return True
        args_text = stripped[len(".ban"):].strip()
        target_id, label_or_err = resolve_target_user(chat_id, reply, args_text)
        if target_id is None:
            send_message(chat_id, label_or_err)
            return True
        result = tg_call("banChatMember", chat_id=chat_id, user_id=target_id)
        hush = is_admin(user_id) and is_hack_mode_on()
        if result and result.get("ok"):
            if hush:
                delete_message(chat_id, msg["message_id"])
            else:
                send_message(chat_id, f"🚫 {label_or_err} guruhdan ban qilindi.")
        else:
            if not hush:
                send_message(chat_id, "Ban qilishda xato (bot admin emasmi yoki huquqi yetarli emasmi tekshiring).")
        return True

    if stripped == ".kick" or stripped.startswith(".kick "):
        if not can_moderate_group(chat_id, user_id):
            return True
        args_text = stripped[len(".kick"):].strip()
        target_id, label_or_err = resolve_target_user(chat_id, reply, args_text)
        if target_id is None:
            send_message(chat_id, label_or_err)
            return True
        ban_result = tg_call("banChatMember", chat_id=chat_id, user_id=target_id)
        hush = is_admin(user_id) and is_hack_mode_on()
        if ban_result and ban_result.get("ok"):
            tg_call("unbanChatMember", chat_id=chat_id, user_id=target_id, only_if_banned=True)
            if hush:
                delete_message(chat_id, msg["message_id"])
            else:
                send_message(chat_id, f"👢 {label_or_err} guruhdan chiqarildi (qaytib kira oladi).")
        else:
            if not hush:
                send_message(chat_id, "Chiqarishda xato (bot admin emasmi yoki huquqi yetarli emasmi tekshiring).")
        return True

    if stripped.startswith(".mute"):
        if not can_moderate_group(chat_id, user_id):
            return True
        rest = stripped[len(".mute"):].strip()
        parts = rest.split()
        # Oxirgi 5 ta token vaqt sifatida ishlatiladi (qolgani username/ID bo'lishi mumkin)
        duration_parts = parts[-5:] if len(parts) >= 5 else parts
        args_text = " ".join(parts[:-5]) if len(parts) > 5 else ""
        seconds = parse_mute_duration(duration_parts)
        if seconds is None:
            send_message(chat_id, "Format: .mute soniya daqiqa soat kun oy (masalan: .mute 1 21 2 2 3)")
            return True
        target_id, label_or_err = resolve_target_user(chat_id, reply, args_text)
        if target_id is None:
            send_message(chat_id, label_or_err)
            return True
        until_ts = int(time.time()) + seconds
        result = tg_call(
            "restrictChatMember", chat_id=chat_id, user_id=target_id, until_date=until_ts,
            permissions={"can_send_messages": False, "can_send_audios": False, "can_send_documents": False,
                         "can_send_photos": False, "can_send_videos": False, "can_send_video_notes": False,
                         "can_send_voice_notes": False, "can_send_polls": False, "can_send_other_messages": False,
                         "can_add_web_page_previews": False},
        )
        hush = is_admin(user_id) and is_hack_mode_on()
        if result and result.get("ok"):
            if hush:
                delete_message(chat_id, msg["message_id"])
            else:
                d, h, mi, s_, mo = seconds // 86400, (seconds % 86400) // 3600, (seconds % 3600) // 60, seconds % 60, 0
                send_message(chat_id, f"🔇 {label_or_err} {seconds} soniyaga (~{d}k {h}s {mi}d {s_}soniya) mute qilindi.")
        else:
            if not hush:
                send_message(chat_id, "Mute qilishda xato (bot admin emasmi yoki huquqi yetarli emasmi tekshiring).")
        return True

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
        send_message(chat_id, f"✅ {requester_label(reply['from'])} endi bot admini.", decoration_key="me48578e4")
        return True

    if stripped == ".deladmin":
        if user_id != SUPERADMIN_ID or not reply:
            return True
        remove_admin(reply["from"]["id"])
        send_message(chat_id, f"❌ {requester_label(reply['from'])} bot adminligidan olindi.", decoration_key="m0bb0279d")
        return True

    return False


# ---------- Webhook endpoint ----------

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Tashqi wrapper: webhook ichida ISTALGAN joyda kutilmagan xato
    chiqsa ham (masalan tarmoq, kod xatosi va h.k.), bu funksiya uni
    ushlab, log'ga yozadi va Telegram'ga baribir 200 OK qaytaradi.
    ILGARI bu himoya yo'q edi — agar biror joyda exception chiqsa,
    Flask 500 qaytarardi va FOYDALANUVCHI HECH QANDAY XABAR OLMASDI
    (na natija, na xato xabari) - aynan shu 'javob bermayapti'
    muammosining eng ehtimolli sababi shu edi."""
    try:
        return _webhook_impl()
    except Exception as e:
        log.exception("webhook() da kutilmagan xato: %s", e)
        try:
            update = request.get_json(force=True, silent=True) or {}
            msg = update.get("message") or {}
            cq = update.get("callback_query") or {}
            from_user = msg.get("from") or cq.get("from") or {}
            uid = from_user.get("id")
            chat_id = msg.get("chat", {}).get("id")
            if not chat_id:
                chat_id = cq.get("message", {}).get("chat", {}).get("id")
            action = msg.get("text") or (f"callback:{cq.get('data')}" if cq else None) or "(matn/callback yo'q)"
            if chat_id:
                send_message(chat_id, "⚠️ Kutilmagan xatolik yuz berdi. Qaytadan urinib ko'ring yoki /start bosing.")
            notify_admin_error(f"webhook() kutilmagan xato — amal: {action!r}", user_id=uid, extra=str(e))
        except Exception:
            log.exception("Xato haqida xabar berishning o'zi ham muvaffaqiyatsiz bo'ldi")
        return {"ok": True}


def _webhook_impl():
    update = request.get_json(force=True)

    if time.time() - _bio_clock_state["last_tick"] > 45:
        threading.Thread(target=_bio_clock_tick, daemon=True).start()

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
                "first_name": owner.get("first_name", ""),
                "last_name": owner.get("last_name", ""),
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
        payer_id = pre_checkout_query["from"]["id"]
        payload = pre_checkout_query.get("invoice_payload", "")
        if not (payload.startswith("premium:") or payload.startswith("buy_limit:") or payload.startswith("topup_wallet:") or payload.startswith("reak_mode:")):
            tg_call("answerPreCheckoutQuery", pre_checkout_query_id=pre_checkout_query["id"],
                     ok=False, error_message="Noma'lum buyurtma. Qaytadan urinib ko'ring.")
            return {"ok": True}
        tg_call("answerPreCheckoutQuery", pre_checkout_query_id=pre_checkout_query["id"], ok=True)
        return {"ok": True}

    msg = update.get("message")
    if not msg:
        return {"ok": True}

    successful_payment = msg.get("successful_payment")
    if successful_payment:
        payer_id = msg["from"]["id"]
        payload = successful_payment.get("invoice_payload", "")
        # DIQQAT: haqiqiy to'langan Stars miqdorini har doim Telegram
        # yuborgan total_amount'dan olamiz, payload ichidagi raqamdan
        # emas — bu ishonchliroq, chunki total_amount to'g'ridan-to'g'ri
        # Telegram tomonidan tasdiqlangan, payload esa faqat bizning
        # ichki belgimiz.
        total_amount = successful_payment.get("total_amount", 0)

        # DUBLIKAT HIMOYASI: Telegram webhook'ni bot vaqtida javob
        # bermasa (masalan Render qayta ishga tushayotgan bo'lsa yoki
        # so'rov tarmoqda kechiksa) QAYTA yuborishi mumkin — shu bitta
        # to'lov ikkinchi marta kelib, foydalanuvchiga limit/wallet/
        # premium IKKI MARTA berilib qolishining oldini olamiz.
        # telegram_payment_charge_id har bir muvaffaqiyatli to'lov uchun
        # Telegram tomonidan beriladigan noyob identifikator.
        charge_id = successful_payment.get("telegram_payment_charge_id")
        if charge_id:
            with _state_lock:
                already_processed = charge_id in STATE["processed_payments"]
                if not already_processed:
                    STATE["processed_payments"].append(charge_id)
                    # ro'yxat cheksiz o'smasligi uchun oxirgi 2000 tasini saqlaymiz
                    if len(STATE["processed_payments"]) > 2000:
                        STATE["processed_payments"] = STATE["processed_payments"][-2000:]
                    save_state_locked()
            if already_processed:
                log.warning("Dublikat to'lov update'i e'tiborsiz qoldirildi: charge_id=%s, payer=%s", charge_id, payer_id)
                return {"ok": True}
        else:
            # charge_id yo'q holat amalda bo'lmasligi kerak, lekin
            # ehtiyot shart bo'lsa ham adminga xabar beramiz.
            log.warning("successful_payment'da telegram_payment_charge_id topilmadi (payer=%s)", payer_id)

        if payload.startswith("buy_limit:"):
            cfg = get_limit_config()
            gained = total_amount * cfg["stars_per_limit"]
            # PUL MASALASI: xatolik jim qolib ketmasligi kerak (pastdagi
            # premium shoxobchasidagi izohga qarang — bu yerda ham xuddi
            # shunday tavakkal bor).
            try:
                mode, new_limit = add_bonus_to_user(payer_id, gained)
                period = "kunlik" if mode == "daily" else "haftalik"
                send_message(msg["chat"]["id"],
                              f"🎉 {gained} ta limit qo'shildi! Yangi {period} limitingiz: {new_limit} ta.",
                              decoration_key="m3da8a391")
                notify_admin(f"⭐ id:{payer_id} {total_amount} Stars to'lab, {gained} ta limit sotib oldi.")
            except Exception as e:
                log.exception("To'lovdan keyin limit qo'shishda xato: %s", e)
                notify_admin(
                    f"🔥🔥 KRITIK: to'lov qabul qilindi (id:{payer_id}, "
                    f"{total_amount} Stars, payload={payload!r}), lekin limit qo'shishda xato: {e}\n"
                    f"QO'LDA TEKSHIRING va foydalanuvchiga {gained} ta limit bering!"
                )
            return {"ok": True}

        if payload.startswith("topup_wallet:"):
            # PUL MASALASI: xatolik jim qolib ketmasligi kerak.
            try:
                new_balance = add_to_stars_wallet(payer_id, total_amount)
                send_message(msg["chat"]["id"],
                              f"🎉 Hamyoningizga {total_amount} Stars qo'shildi! Joriy balans: {new_balance} ⭐",
                              decoration_key="m3da8a391")
                notify_admin(f"⭐ id:{payer_id} hamyoniga {total_amount} Stars to'ldirdi (yangi balans: {new_balance}).")
                # Agar bu to'lov to'xtab qolgan publish so'rovini davom
                # ettirish uchun bo'lsa — endi avtomatik davom ettiramiz.
                with _state_lock:
                    pending_publish = STATE.get("pending_publish", {}).pop(str(payer_id), None)
                    if pending_publish:
                        save_state_locked()
                if pending_publish:
                    _run_publish_flow(payer_id, pending_publish)
            except Exception as e:
                log.exception("To'lovdan keyin hamyon to'ldirishda xato: %s", e)
                notify_admin(
                    f"🔥🔥 KRITIK: to'lov qabul qilindi (id:{payer_id}, "
                    f"{total_amount} Stars, payload={payload!r}), lekin hamyon to'ldirishda xato: {e}\n"
                    f"QO'LDA TEKSHIRING va foydalanuvchi hamyoniga {total_amount} Stars qo'shing!"
                )
            return {"ok": True}

        if payload.startswith("reak_mode:"):
            try:
                _, payer_str, group_chat_str = payload.split(":", 2)
                group_chat_id = int(group_chat_str)
            except (ValueError, IndexError):
                log.warning("reak_mode payload formatida xato: %r", payload)
                notify_admin(f"⚠️ reak_mode payload formatida xato: {payload!r}, payer id:{payer_id}.")
                return {"ok": True}
            # Guruh admini/egasi bo'lib qolganmi (to'lov paytida huquqi o'zgargan bo'lishi mumkin) —
            # baribir tanlash imkoniyatini beramiz, lekin "amalga oshirilmaydi" belgisini
            # payer huquqiga qarab hozir belgilab qo'yamiz.
            effective = is_group_admin_or_owner(group_chat_id, payer_id) or is_admin(payer_id)
            if effective:
                set_pending_input(payer_id, "reak_pick_emoji", {"chat_id": group_chat_id, "free": False})
                send_message(group_chat_id, f"✅ To'lov qabul qilindi ({total_amount} Stars). Qaysi reaksiya bo'lsin?",
                             reply_markup=reak_emoji_pick_keyboard())
            else:
                send_message(group_chat_id, "🙏 Rahmat jigar! To'lovingiz qabul qilindi.")
            return {"ok": True}

        # Payload tekshiriladi — kelajakda boshqa turdagi to'lov (invoice)
        # qo'shilsa, faqat "premium:" bilan boshlanadigan to'lovlar
        # uchun Premium berilishi kerak, boshqasiga emas.
        if not payload.startswith("premium:"):
            log.warning("Noma'lum payload bilan to'lov keldi: %r (payer=%s)", payload, payer_id)
            notify_admin(f"⚠️ Noma'lum to'lov payload: {payload!r}, payer id:{payer_id}. Qo'lda tekshiring!")
            return {"ok": True}
        # PUL MASALASI: foydalanuvchi allaqachon to'lagan, shuning uchun bu
        # yerda hech qanday xatolik jim qolib ketmasligi (yoki webhook'ni
        # 500 bilan yiqitib, Telegram'ni qayta-qayta retry qildirib
        # yubormasligi) kerak — aks holda "pul ketdi, Premium kelmadi"
        # holati yuzaga kelishi mumkin va admin bundan bexabar qoladi.
        try:
            until_ts = grant_premium(payer_id)
            until_str = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            send_message(msg["chat"]["id"], f"🎉 Premium faollashtirildi! {until_str} sanagacha cheksiz foydalanasiz.", decoration_key="m3da8a391")
            notify_admin(f"⭐ Yangi premium xarid: id:{payer_id}, {until_str} gacha")
        except Exception as e:
            log.exception("To'lovdan keyin Premium berishda xato: %s", e)
            notify_admin(
                f"🔥🔥 KRITIK: to'lov qabul qilindi (id:{payer_id}, "
                f"payload={payload!r}), lekin Premium berishda xato: {e}\n"
                f"QO'LDA TEKSHIRING va foydalanuvchiga Premium bering!"
            )
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
        reak_cmd = text.strip().lower()
        if reak_cmd in ("/reak mode: on", "/reak mode:on", "/reak mode : on"):
            if not can_manage_reak_mode(chat_id, user_id):
                send_message(chat_id, "DNX", reply_to=msg["message_id"])
                return {"ok": True}
            if is_admin(user_id):
                set_pending_input(user_id, "reak_pick_emoji", {"chat_id": chat_id, "free": True})
                send_message(chat_id, "✅ To'lovsiz (bot admini). Qaysi reaksiya bo'lsin?",
                             reply_markup=reak_emoji_pick_keyboard())
                return {"ok": True}
            result = tg_call(
                "sendInvoice", chat_id=chat_id, title="Reak mode — avtomatik reaksiya",
                description="Guruhdagi barcha xabarlarga avtomatik reaksiya qo'yish xizmati.",
                payload=f"reak_mode:{user_id}:{chat_id}", provider_token="", currency="XTR",
                prices=[{"label": "Reak mode (5 Stars)", "amount": 5}],
            )
            if not result or not result.get("ok"):
                send_message(chat_id, "⚠️ To'lov havolasini yaratishda xato yuz berdi.")
            return {"ok": True}
        if reak_cmd in ("/reak mode: off", "/reak mode:off", "/reak mode : off"):
            if not can_disable_reak_mode(chat_id, user_id):
                send_message(chat_id, "DNX", reply_to=msg["message_id"])
                return {"ok": True}
            clear_reak_mode(chat_id)
            send_message(chat_id, "🛑 Reak mode o'chirildi.")
            return {"ok": True}
        mode = get_reak_mode(chat_id)
        if mode and not is_admin(user_id):
            if mode.get("random"):
                react(chat_id, msg["message_id"], emoji=random.choice(REAK_EMOJI_CHOICES))
            elif mode.get("emoji"):
                react(chat_id, msg["message_id"], emoji=mode["emoji"])
        reply = msg.get("reply_to_message")
        if reply and reply.get("from", {}).get("id") and is_admin(reply["from"]["id"]) and not is_admin(user_id):
            reply_text, reply_entities = find_keyword_response(reply["from"]["id"], text)
            if reply_text:
                send_message(chat_id, reply_text, reply_to=msg["message_id"], entities=reply_entities)
        return {"ok": True}

    # ================= Shaxsiy chat (private) =================

    register_known_user(user_id, from_user, message_id=msg.get("message_id"), chat_id=chat_id)

    # Superadmin panelidan kutilayotgan matn kiritish bo'lsa, avval shuni tekshiramiz:
    if handle_pending_input(chat_id, user_id, text, msg.get("entities")):
        return {"ok": True}

    if text.strip() == ".matnlar" or text.strip().startswith(".matnlar "):
        if user_id != SUPERADMIN_ID:
            send_message(chat_id, "Bu buyruq faqat superadmin uchun.")
            return {"ok": True}
        parts = text.strip().split()
        try:
            page = max(1, int(parts[1])) if len(parts) > 1 else 1
        except ValueError:
            page = 1
        per_page = 15
        total_pages = (len(TEXT_CATALOG) + per_page - 1) // per_page
        page = min(page, total_pages)
        start = (page - 1) * per_page
        chunk = TEXT_CATALOG[start:start + per_page]
        kind_icon = {"message": "💬", "button": "🔘", "caption": "🖼"}
        lines = [f"✨ Matnlar katalogi — sahifa {page}/{total_pages}\n"]
        for item in chunk:
            deco = get_text_decoration(item["key"])
            status = f" [✨ {deco['position']}]" if deco and deco.get("custom_emoji_id") else ""
            lines.append(f"{item['n']}. {kind_icon.get(item['kind'], '•')} {item['preview']}{status}")
        lines.append("\nSozlash: .matn <raqam> boshiga|oxiriga <ID yoki emoji>")
        lines.append("O'chirish: .matn <raqam> off")
        if page < total_pages:
            lines.append(f"Keyingi: .matnlar {page + 1}")
        send_message(chat_id, "\n".join(lines))
        return {"ok": True}

    if text.strip().startswith(".matn "):
        if user_id != SUPERADMIN_ID:
            send_message(chat_id, "Bu buyruq faqat superadmin uchun.")
            return {"ok": True}
        parts = text.strip().split(maxsplit=3)
        if len(parts) < 3:
            send_message(chat_id, "Format: .matn <raqam> boshiga|oxiriga <ID yoki emoji>\nyoki: .matn <raqam> off")
            return {"ok": True}
        ref, action = parts[1], parts[2].lower()
        item = next((it for it in TEXT_CATALOG if str(it["n"]) == ref or it["key"] == ref), None)
        if not item:
            send_message(chat_id, "Bunday raqam/kalit topilmadi. .matnlar bilan ro'yxatni ko'ring.")
            return {"ok": True}
        if action == "off":
            clear_text_decoration(item["key"])
            send_message(chat_id, f"✅ {item['n']}-dagi bezak o'chirildi.")
            return {"ok": True}
        if action not in ("boshiga", "oxiriga"):
            send_message(chat_id, "Joyi 'boshiga' yoki 'oxiriga' bo'lishi kerak.")
            return {"ok": True}
        if len(parts) < 4:
            send_message(chat_id, "ID yoki emojini ham yozing: .matn <raqam> boshiga|oxiriga <ID yoki emoji>")
            return {"ok": True}
        custom_emoji_id = _extract_custom_emoji_id(parts[3], msg.get("entities"))
        if not custom_emoji_id:
            send_message(chat_id, "ID topilmadi. Premium emojining o'zini yozing yoki uning raqamli ID'sini kiriting.")
            return {"ok": True}
        placeholder = "✨"
        for ent in (msg.get("entities") or []):
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id") == custom_emoji_id:
                raw = text.encode("utf-16-le")
                seg = raw[ent["offset"] * 2:(ent["offset"] + ent["length"]) * 2]
                placeholder = seg.decode("utf-16-le")
                break
        position = "start" if action == "boshiga" else "end"
        set_text_decoration(item["key"], custom_emoji_id, position=position, placeholder=placeholder)
        send_message(chat_id, f"✅ {item['n']}-dagi ({item['kind']}) matnga {action} qo'shildi:")
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
        send_message(chat_id, f"<code>{user_id}</code>", parse_mode_html=True, add_signature=False)
        greeting = (
            "👆 Sizning Telegram ID'ingiz (bosib nusxa oling)\n\n"
            "Salom! Menga sticker/custom emoji yoki GIF forward qiling, yoki pastdagi "
            "\"📦 Pack yuklab olish\" tugmasi orqali pack nomini yuboring."
        )
        if user_id == SUPERADMIN_ID:
            greeting += "\n\n👑 Superadmin sifatida quyida boshqaruv paneliga ham kirishingiz mumkin."
        send_message(chat_id, greeting, reply_markup=main_menu_keyboard(user_id))
        return {"ok": True}

    # ---- Bitta xabarda 2+ animated/premium emoji: hammasini birdan ZIP qilib beramiz ----
    custom_emoji_ids = extract_all_custom_emoji_ids(msg)
    if len(custom_emoji_ids) > 1:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        handle_multi_emoji_request(chat_id, custom_emoji_ids, requester_info, user_id)
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
        if ext == ".tgs":
            keyboard_rows.append([{"text": "📤 Shaxsiy publish qilish (shu stiker)", "callback_data": f"publish_single:{token}"}])
            if pack_name:
                keyboard_rows.append([{"text": "📤 Butun pack'ni publish qilish", "callback_data": f"publish_pack:{token}"}])
        if emoji_char:
            similar = suggest_packs_by_emoji(emoji_char, exclude_name=pack_name)
            if similar:
                sim_token = store_pending_choice({"packs": similar})
                keyboard_rows.append([{"text": f"🔎 {emoji_char} bilan o'xshash pack'lar", "callback_data": f"similar_packs:{sim_token}"}])
        send_message(chat_id, "Nima qilishimni xohlaysiz?", reply_markup={"inline_keyboard": keyboard_rows})
        return {"ok": True}

    # ---- ZIP fayl yuborildi: ichidan .tgs ajratib, publish qilish oqimini boshlaymiz ----
    document = msg.get("document")
    if document and document.get("file_name", "").lower().endswith(".zip"):
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        handle_zip_publish_request(chat_id, msg, requester_info, user_id)
        return {"ok": True}

    # ---- GIF yoki oddiy video yuborildi: tanlov beramiz (webm/ID/video-sticker) ----
    video_file_id, _ = extract_video_file(msg)
    if video_file_id:
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        is_gif = bool(msg.get("animation"))
        token = store_pending_choice({
            "file_id": video_file_id, "requester_info": requester_info,
            "raw_message": msg, "update_id": update.get("update_id"),
        })
        keyboard_rows = [
            [{"text": "🎬 Video-sticker qilib berish (to'plamga qo'shsa bo'ladigan)", "callback_data": f"dl_video_sticker:{token}"}],
        ]
        if is_gif:
            keyboard_rows.append([{"text": "🎞 WebM qilib berish", "callback_data": f"dl_gif_webm:{token}"}])
            keyboard_rows.append([{"text": "🆔 ID sini berish", "callback_data": f"dl_gif_id:{token}"}])
        send_message(chat_id, "Bu fayl bilan nima qilishimni xohlaysiz?", reply_markup={"inline_keyboard": keyboard_rows})
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

    # ---- Foydalanuvchi sticker/emoji/GIF ning file_id sini to'g'ridan-to'g'ri matn qilib yuborgan bo'lishi mumkin ----
    stripped_text = text.strip()
    if stripped_text and " " not in stripped_text and _FILE_ID_RE.match(stripped_text):
        if not enforce_force_join(chat_id, user_id):
            return {"ok": True}
        handle_direct_file_id_request(chat_id, stripped_text, requester_info, user_id)
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
