# StokerDownloader

Telegram sticker/custom emoji pack downloader bot (webhook mode, Render deployment).

## Ishlash tartibi

Foydalanuvchi stiker/custom emoji forward qiladi yoki `/getpack <pack_nomi>` yuboradi.
Bot pack ichidagi barcha fayllarni (`.tgs` / `.webp` / `.webm`) topib, ZIP qilib yuboradi.
Har bir so'rov haqida superadminga darhol xabar boradi.

## Render'da deploy qilish

1. Bu repo'ni Render'ga ulang (New Web Service)
2. Build command: `pip install -r requirements.txt`
3. Start command: `python sticker_bot_webhook.py`
4. Environment Variables:
   - `BOT_TOKEN` — bot tokeni (@BotFather)
   - `SUPERADMIN_ID` — sizning Telegram user ID (@userinfobot orqali)
   - `WEBHOOK_URL` — Render bergan domen, masalan `https://stokerdownloader.onrender.com`
   - `PORT` — Render avtomatik beradi

Deploy tugagach, bot birinchi ishga tushganda avtomatik webhook o'rnatadi.
