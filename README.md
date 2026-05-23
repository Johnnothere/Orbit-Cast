# Event Radar 📡

Intelligence, tech, defence and builder event monitor. Scrapes 19 sources, sends Telegram alerts, runs a live web dashboard.

## Sources

### Intelligence & Security
- RUSI
- BISI (Bloomsbury Intelligence & Security Institute)
- Intelligence Forums
- OSMOSIS

### Defence & Geopolitics
- London Defence Conference

### Cyber & Infosec
- Infosecurity Europe

### Tech & AI
- Critical Communications World
- Digital Government
- AI Expo Global

### Education & Research
- Imperial College London
- BrainStation London

### Builder & Tech Community (Luma)
- Plugged
- Encode Club
- Claude Community
- AI Native Dev
- SRV Frontier
- Vercel Events
- Jody Saunders
- GDG London

---

## Local Development

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Add env vars in Railway dashboard:
   - `TELEGRAM_BOT_TOKEN` — from @BotFather
   - `TELEGRAM_CHAT_ID` — your Telegram chat ID
4. Railway auto-detects `Procfile` and deploys

## Telegram Setup

1. Open Telegram → search `@BotFather`
2. `/newbot` → name it `Event Radar`
3. Copy the API token → add as `TELEGRAM_BOT_TOKEN`
4. Start the bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Find your `chat.id` → add as `TELEGRAM_CHAT_ID`

## GitHub Actions (Scheduled Scraping)

Add secrets to your repo (Settings → Secrets → Actions):
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The scraper runs every 6 hours and sends Telegram alerts for new events.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard |
| `GET /api/events` | All events (JSON) |
| `GET /api/events?category=Intel` | Filter by category |
| `GET /api/summary` | Source summary |
| `POST /api/refresh` | Trigger scrape |
| `GET /api/status` | Scrape status |
