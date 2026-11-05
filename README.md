# H1 RADAR

Telegram bot + browser-based civic map and moderated community reports. The map is a regular website, not a Telegram Mini App.

## Runtime
- Python 3.12, aiogram 3, FastAPI, asyncpg, PostgreSQL.
- MapLibre GL JS with OpenStreetMap raster tiles.
- One process: FastAPI lifespan starts the Telegram long-polling task.
- No GitHub Actions are required.

## Environment
Required: `BOT_TOKEN`, `BOT_OWNER` (numeric Telegram ID), `DATABASE_URL`. Optional: `MAP_URL` (defaults to `https://map.h1radar.relaxdev.ru`) and `BOT_USERNAME` (otherwise read from Telegram on startup).
Optional: `PORT`, `DB_POOL_SIZE`, `LOG_LEVEL`.

Point `h1radar.relaxdev.ru` and `moder.h1radar.relaxdev.ru` to this same RelaxDev service and enable HTTPS for both. The main host serves the live map; `moder.` serves the dedicated moderation dashboard. The optional `map.` hostname also serves the map. Set RelaxDev health check to `/api/health`.

## Run
```bash
pip install -r requirements.txt
uvicorn h1:app --host 0.0.0.0 --port 8000
```
or build the included Dockerfile.

## Bot flow
`/start` creates the user's account and assigns a random RU-XXXX code. The bot sends an ordinary HTTPS URL button below its message; it does not use a reply keyboard, Web App button, or Telegram Mini App. Browser login uses a short-lived one-time deep-link challenge and a server-side session token.

## Commands
- `/start` create account / open website
- `/radar` open map in browser
- `/moderation` team panel
- `/help`

## API
- `GET /api/health`, `GET /api/cities`, `GET /api/feed`, `GET /api/city-chat`, `POST /api/city-chat`
- Authenticated: `GET /api/me`, `POST /api/reports`
- Moderator: `GET /api/moderation/summary`, `GET /api/moderation/reports`, `POST /api/moderation/reports/{id}/{publish|reject}`, `GET /api/moderation/users`, `GET /api/moderation/log`
- Owner only: `POST /api/moderation/users/{telegram_id}/role/{user|moderator}`

## Important operational notes
This initial implementation is a civic reporting platform, not an authoritative emergency-alert feed. Do not submit tactical, military, or sensitive location details. The startup schema seeds more than 100 Russian cities and settlements. The base map covers the full Russian territory; the city directory can be extended with additional verified coordinates as needed. OSM tile usage is subject to its public tile policy; for production nationwide traffic, configure a compliant tile provider/style and attribution. Nearby marks store rounded coordinates (two decimal places); the author counts as the first confirmation. Two additional distinct users are required for automatic publication. Moderators can also publish or reject a mark. Push delivery is not configured. Sign-in confirms control of the Telegram account; the current browser-login flow does not request or store a phone number.
