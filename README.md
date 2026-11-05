# H1 RADAR

Telegram bot + browser-based civic map and moderated community reports. The map is a regular website protected by mandatory Telegram sign-in. The bot also exposes a Telegram Web App menu shortcut.

## Runtime
- Python 3.12, aiogram 3, FastAPI, asyncpg, PostgreSQL.
- MapLibre GL JS with OpenStreetMap raster tiles.
- One process: FastAPI lifespan starts the Telegram long-polling task.
- No GitHub Actions are required.

## Environment
Required: `BOT_TOKEN`, `BOT_OWNER` (numeric Telegram ID), `DATABASE_URL`. Optional: `MAP_URL` (defaults to `https://map.h1radar.relaxdev.ru`) and `BOT_USERNAME` (otherwise read from Telegram on startup).
Optional: `PORT`, `DB_POOL_SIZE`, `LOG_LEVEL`.

Point `h1radar.relaxdev.ru`, `map.h1radar.relaxdev.ru`, and `moder.h1radar.relaxdev.ru` to this same RelaxDev service and enable HTTPS for all three. The main host serves the landing page; `map.` serves the live map; `moder.` serves the moderation dashboard. Set RelaxDev health check to `/api/health`.

## Run
```bash
pip install -r requirements.txt
uvicorn h1:app --host 0.0.0.0 --port 8000
```
or build the included Dockerfile.

## Bot flow
`/start` creates the user's account and assigns a random RU-XXXX code. The bot sends an ordinary HTTPS URL button below its message; it does not use a reply keyboard, Web App button, or Telegram Mini App. Browser login uses a short-lived one-time deep-link challenge and a server-side session token.

## Commands
- `/start` create account / confirm a browser sign-in / open the map
- `/help` sign-in instructions
- `/help` help with sign-in and using the map

## API
- `GET /api/health`, `GET /api/cities`, `GET /api/feed`, `GET /api/city-chat`, `POST /api/city-chat`
- Authenticated: `GET /api/me`, `POST /api/reports`, `POST /api/reports/{id}/confirm`
- Moderator: `GET /api/moderation/summary`, `GET /api/moderation/events`, `GET /api/moderation/marks`, `GET /api/moderation/reports`, `POST /api/moderation/reports/{id}/{publish|reject}`, `GET /api/moderation/users`, `GET /api/moderation/log`
- Owner only: `POST /api/moderation/users/{telegram_id}/role/{user|moderator}`

## Important operational notes
This initial implementation is a civic reporting platform, not an authoritative emergency-alert feed. Do not submit tactical, military, or sensitive location details. The startup schema seeds more than 100 Russian cities and settlements. The base map covers the full Russian territory; the city directory can be extended with additional verified coordinates as needed. OSM tile usage is subject to its public tile policy; for production nationwide traffic, configure a compliant tile provider/style and attribution. Nearby marks store rounded coordinates (two decimal places); the author counts as the first confirmation. Two additional distinct users are required for automatic publication. Moderators can also publish or reject a mark. Push delivery is not configured. Sign-in confirms control of the Telegram account; the current browser-login flow does not request or store a phone number.


## Расширенная модерация и каталог городов

В текущем backend доступны следующие административные API (все требуют Bearer-сессию Telegram):

- `GET /api/moderation/cities?q=` — поиск по каталогу городов и регионам.
- `POST /api/moderation/cities` — добавление города (только владелец). Принимает `name`, `region`, опционально `latitude` и `longitude`.
- `PATCH /api/moderation/cities/{city_id}` — изменение названия, региона, координат и включённости (только владелец).
- `POST /api/city-requests` — заявка пользователя на новый город (до 3 заявок за 24 часа).
- `GET /api/moderation/city-requests?status=pending|approved|rejected|all` — очередь заявок.
- `POST /api/moderation/city-requests/{request_id}/approve` или `/reject` — решение владельца.
- `GET /api/moderation/chat?city=` — поиск сообщений городских чатов для команды.
- `POST /api/moderation/users/{telegram_id}/restriction` — ограничение аккаунта (только владелец); JSON: `{"reason":"...","hours":24}`. Без `hours` ограничение бессрочное.
- `DELETE /api/moderation/users/{telegram_id}/restriction` — снятие ограничения.

Ограничения аккаунта проверяются централизованно в `current_user`, поэтому распространяются на все защищённые API, а не только на один экран. Новые таблицы создаются при запуске через `SCHEMA`; отдельная повторно применимая миграция находится в `migrations/002_moderation_expansion.sql`.

Городские координаты валидируются по геометрии России. Заявки и изменения каталога записываются в журнал модерации. Публичная статистика агрегирована и не раскрывает персональные данные.


## Current interface capabilities
- The main landing page is an original H1 visual system with live public aggregate counters, searchable/collapsible city directory, Moscow-time clock, FAQ and publication workflow.
- The map requires a verified Telegram session before rendering the map or loading city/feed/chat data. The landing page exposes only aggregate statistics and a public city directory. The authenticated map updates its feed every 15 seconds while visible, displays timestamps in Europe/Moscow, supports live/history modes, city filtering, city chat, collapsible side panel and browser notifications for new published reports while the page is open.
- A report is submitted immediately after the user selects a category and taps a point inside Russia; there is no second manual submit button. Geolocation selection also submits directly.
- User profiles show a public code, role, join date and personal activity totals. City suggestions are submitted for owner review.
- Moderation includes city catalog editing, city-request review, account restrictions and city-chat message search/deletion.
- Browser notifications are page-session notifications, not server push; they work while the map tab is open and permission is granted.

The interface is a feature-level H1 implementation, not a literal copy of GDE.WTF. Military tracking, live operational positions, weapon/PVO targeting data and other sensitive operational features are intentionally not implemented. H1 is a civil community-reporting service and is not an official emergency alert source.


## Обязательный вход и оформление

- Страница карты закрыта полноэкранным экраном входа до подтверждения Telegram-аккаунта.
- API городов, ленты и чтения городского чата требуют Bearer-сессию; для главной страницы предусмотрен отдельный публичный каталог без координат.
- После подтверждения одноразовой ссылки карта открывается автоматически. Сессия действует 30 дней.
- Бот показывает кнопку Web App «Открыть H1 RADAR» в меню Telegram и обновляет приветствие/справку при старте.
- Фирменные SVG-ресурсы: `static/h1-avatar.svg` (аватар) и `static/favicon.svg` (favicon).
- Telegram не позволяет боту менять собственную фотографию профиля через Bot API. Файл аватара подготовлен в репозитории; его нужно один раз установить в профиле бота через @BotFather.
