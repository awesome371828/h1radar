# H1 RADAR

Telegram bot + browser-based civic map and moderated community reports. The map is a regular website, not a Telegram Mini App.

## Runtime
- Python 3.12, aiogram 3, FastAPI, asyncpg, PostgreSQL.
- MapLibre GL JS with OpenStreetMap raster tiles.
- One process: FastAPI lifespan starts the Telegram long-polling task.
- No GitHub Actions are required.

## Environment
Required: `BOT_TOKEN`, `BOT_OWNER` (numeric Telegram ID), `DATABASE_URL`, `WEBAPP_URL`.
Optional: `BOT_USERNAME`, `PORT`, `DB_POOL_SIZE`, `TG_INITDATA_MAX_AGE`, `LOG_LEVEL`.

Set `WEBAPP_URL` to the public HTTPS URL serving this application. Configure the bot's domain with BotFather. Set RelaxDev health check to `/api/health`.

## Run
```bash
pip install -r requirements.txt
uvicorn h1:app --host 0.0.0.0 --port 8000
```
or build the included Dockerfile.

## Bot flow
`/start` creates the user's account and assigns a random RU-XXXX code. The bot sends an ordinary HTTPS URL button below its message; it does not use a reply keyboard, Web App button, or Telegram Mini App. Browser login uses a short-lived one-time deep-link challenge and a server-side session token.

## Commands
- `/start` registration / open app
- `/radar` open map
- `/moderation` team panel
- `/help`

## API
- `GET /api/health`, `GET /api/cities`, `GET /api/feed`
- Authenticated: `GET /api/me`, `POST /api/reports`
- Moderator: `GET /api/moderation/summary`, `GET /api/moderation/reports`, `POST /api/moderation/reports/{id}/{publish|reject}`, `GET /api/moderation/users`, `GET /api/moderation/log`
- Owner only: `POST /api/moderation/users/{telegram_id}/role/{user|moderator}`

## Important operational notes
This initial implementation is a civic reporting platform, not an authoritative emergency-alert feed. Do not submit tactical, military, or sensitive location details. The starter city directory includes 40 major Russian cities; expand the cities table with verified coordinates for additional settlements and regions. OSM tile usage is subject to its public tile policy; for production nationwide traffic, configure a compliant tile provider/style and attribution. Push delivery and SMS verification are not configured: Telegram contact sharing is the only phone confirmation method here.
