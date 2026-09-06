# StokerDownloader 🎯

> **Telegram sticker & custom emoji pack downloader** — get file IDs, ZIP entire packs, post with custom emojis — all without Telegram Premium.

🤖 **Live bot:** [@ThehackerRobot](https://t.me/ThehackerRobot)

---

## 🔴 Live Demo

This repository is the **exact source code running in production** behind **[@ThehackerRobot](https://t.me/ThehackerRobot)** on Telegram.

It's not a sample, mock, or stripped-down version — it's the real, deployed bot. Open Telegram, start a chat with [@ThehackerRobot](https://t.me/ThehackerRobot), and try any feature listed below to see this code running live.

---

## ✨ Features

- 📦 **Pack downloader** — Forward any sticker/emoji or send `/getpack <pack_name>` to get a full ZIP (`.tgs` / `.webp` / `.webm`)
- 🆔 **ID extractor** — Get custom emoji & sticker IDs instantly (forward, paste pack link, or drop the file — all work)
- 📤 **Post with custom emoji** — Send channel posts with premium custom emojis via ID, no Premium needed on user side
- 👑 **Superadmin panel** — Full control: user management, bonus/premium system, broadcast, paginated channel list
- 🗄️ **Zero-cost DB** — Telegram supergroup as database (no external DB needed)
- ⚡ **Debounced state** — Fast sequential updates merged into one write; critical actions (admin add, premium grant) written instantly
- 🔄 **Webhook mode** — Runs on Render free tier with UptimeRobot keepalive

---

## 🚀 Quick Deploy (Render)

1. Fork this repo & connect to [Render](https://render.com) → **New Web Service**
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `gunicorn sticker_bot_webhook:app --workers 1`
4. Set environment variables:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Your bot token from [@BotFather](https://t.me/BotFather) |
| `SUPERADMIN_ID` | Your Telegram user ID (get from [@userinfobot](https://t.me/userinfobot)) |
| `WEBHOOK_URL` | Your Render domain e.g. `https://stokerdownloader.onrender.com` |
| `DB_GROUP_ID` | Telegram supergroup ID used as database |
| `CACHE_GROUP_ID` | *(Optional)* Group for caching ZIP files |
| `PORT` | Set automatically by Render |

> Webhook is registered automatically on first startup.

---

## ⚠️ Important — 1 Worker Only

```bash
gunicorn sticker_bot_webhook:app --workers 1
```

**Do NOT use more than 1 worker.** Each worker has its own RAM — with 2+ workers, user state (limits, bonuses, premium) will silently desync. No errors, no logs. This is a hard constraint.

---

## 💡 How the "no Premium needed" trick works

Telegram requires Premium to send `custom_emoji` entities. This bot works around it:
- The **bot owner account** (transferred via BotFather) holds Premium
- Users interact through the bot and never need their own Premium subscription
- ID extraction works via forward, pack link, or direct file — all edge cases covered

---

## 🛠️ Stack

| Layer | Tech |
|---|---|
| Language | Python 3 |
| Bot framework | python-telegram-bot (webhook) |
| Hosting | Render free tier + UptimeRobot |
| Database | Telegram Supergroup |
| State | In-memory with debounced persistence |

---

## 📄 License

[MIT](LICENSE)

---

<details>
<summary>🇺🇿 O'zbekcha qo'llanma</summary>

Foydalanuvchi stiker/custom emoji forward qiladi yoki `/getpack <pack_nomi>` yuboradi.
Bot pack ichidagi barcha fayllarni (`.tgs` / `.webp` / `.webm`) topib, ZIP qilib yuboradi.

Deploy uchun yuqoridagi Render bo'limiga qarang.

</details>
