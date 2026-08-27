# StokerDownloader

Telegram sticker/custom emoji pack downloader bot (webhook mode, Render deployment).

## Ishlash tartibi

Foydalanuvchi stiker/custom emoji forward qiladi yoki `/getpack <pack_nomi>` yuboradi.
Bot pack ichidagi barcha fayllarni (`.tgs` / `.webp` / `.webm`) topib, ZIP qilib yuboradi.
Har bir so'rov haqida superadminga darhol xabar boradi.

## 🆕 Yangi: Kanalga custom emoji bilan post yuborish

Superadmin panelida **"📤 Kanalga post (emoji bilan)"** tugmasi orqali:

1. Bot admin bo'lgan kanallar ro'yxatidan birini tanlaysiz (sahifalab ko'rsatiladi)
2. Post matnini yozasiz — custom/premium emoji kerak bo'lgan joyga uning ID'sini
   `[5458672011788167217]` shaklida yozasiz
3. Bot matnni to'g'ri `custom_emoji` entity bilan tanlangan kanalga yuboradi

Emoji ID'sini olish uchun — botga o'sha custom emojini forward qiling yoki havolasini
tashlang, bot mavjud "🆔 ID sini berish" funksiyasi orqali ID'ni qaytaradi.

Texnik eslatma: bu funksiya ishlashi uchun bot **owner**'ining (BotFather orqali
ownership transfer qilingan akkauntning) Telegram Premium obunasi bo'lishi kerak —
aks holda `custom_emoji` entity Telegram tomonidan qabul qilinmaydi.

## Render'da deploy qilish

1. Bu repo'ni Render'ga ulang (New Web Service)
2. Build command: `pip install -r requirements.txt`
3. Start command: `python sticker_bot_webhook.py`
4. Environment Variables:
   - `BOT_TOKEN` — bot tokeni (@BotFather)
   - `SUPERADMIN_ID` — sizning Telegram user ID (@userinfobot orqali)
   - `WEBHOOK_URL` — Render bergan domen, masalan `https://stokerdownloader.onrender.com`
   - `DB_GROUP_ID` — DB sifatida ishlatiladigan Telegram guruh ID'si
   - `CACHE_GROUP_ID` — (ixtiyoriy) Pack ZIP fayllarini keshlash uchun guruh ID'si
   - `PORT` — Render avtomatik beradi

Deploy tugagach, bot birinchi ishga tushganda avtomatik webhook o'rnatadi.

## Muhim

Render Start Command'da bitta worker ishlatilishi kerak (state xotirada saqlanadi):

```
gunicorn sticker_bot_webhook:app --workers 1
```

⚠️ **`--workers 1` dan ORTIQ ishlatmang.** Har bir worker o'zining alohida
xotira (RAM) nusxasiga ega bo'ladi — 2+ worker bilan ishga tushirilsa,
foydalanuvchi limitlari, bonuslar va boshqa state ma'lumotlari **jimgina
yo'qolishi yoki eskirishi mumkin** (workerlar bir-birining yozganini
ko'rmaydi, oxirgi saqlagan worker g'olib chiqadi). Xato hech qanday log
yoki bildirishnoma bermaydi — shuning uchun bu talab qat'iy.

State endi debounce (2 soniyalik) bilan saqlanadi — tez ketma-ket kelgan
o'zgarishlar bitta yozuvga birlashadi. Kritik amallar (admin qo'shish,
bonus/premium berish) esa kutmasdan darhol yoziladi.
