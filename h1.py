from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, os, re, secrets, string, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonDefault)
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("h1")
BOT_TOKEN=os.environ["BOT_TOKEN"]
BOT_OWNER=int(os.environ["BOT_OWNER"])
def normalize_webapp_url(value: str | None) -> str:
 raw=(value or "https://h1radar.relaxdev.ru").strip()
 if not raw:
  raw="https://h1radar.relaxdev.ru"
 if raw.startswith("//"):
  raw="https:"+raw
 elif "://" not in raw:
  raw="https://"+raw
 try:
  parts=urlsplit(raw)
  if parts.scheme.lower()=="http":
   parts=parts._replace(scheme="https")
  if parts.scheme.lower()!="https" or not parts.hostname:
   raise ValueError
  if parts.username or parts.password or any(ch.isspace() for ch in raw):
   raise ValueError
  # Проверяем порт заранее: urlsplit может выбросить ValueError при некорректном значении.
  _=parts.port
 except ValueError as exc:
  raise ValueError("WEBAPP_URL must be a valid public HTTPS URL, e.g. https://h1radar.relaxdev.ru") from exc
 return urlunsplit(("https",parts.netloc,parts.path.rstrip("/"),parts.query,parts.fragment))

WEBAPP_URL=normalize_webapp_url(os.getenv("WEBAPP_URL","https://map.h1radar.relaxdev.ru"))
MAP_URL=normalize_webapp_url(os.getenv("MAP_URL","https://map.h1radar.relaxdev.ru"))
if urlsplit(MAP_URL).hostname=="h1radar.relaxdev.ru":
 MAP_URL="https://map.h1radar.relaxdev.ru"
log.info("Public H1 RADAR map URL: %s", MAP_URL)
DATABASE_URL=os.environ["DATABASE_URL"]
with open(os.path.join(os.path.dirname(__file__),"static","russia.geojson"),encoding="utf-8") as _geo_file:
 RUSSIA_GEOMETRY=json.load(_geo_file)["geometry"]
def _point_in_ring(lon:float,lat:float,ring:list)->bool:
 inside=False
 j=len(ring)-1
 for i in range(len(ring)):
  xi,yi=ring[i][0],ring[i][1]; xj,yj=ring[j][0],ring[j][1]
  if (yi>lat)!=(yj>lat) and lon<(xj-xi)*(lat-yi)/(yj-yi+1e-15)+xi: inside=not inside
  j=i
 return inside
def point_in_russia(lon:float,lat:float)->bool:
 if not (-180<=lon<=180 and -90<=lat<=90): return False
 polygons=[RUSSIA_GEOMETRY["coordinates"]] if RUSSIA_GEOMETRY["type"]=="Polygon" else RUSSIA_GEOMETRY["coordinates"]
 return any(_point_in_ring(lon,lat,poly[0]) and not any(_point_in_ring(lon,lat,hole) for hole in poly[1:]) for poly in polygons)
BOT_USERNAME=os.getenv("BOT_USERNAME","").lstrip("@")
_auth_attempts:dict[str,list[float]]={}
INITDATA_MAX_AGE=int(os.getenv("TG_INITDATA_MAX_AGE","86400"))
pool: asyncpg.Pool|None=None
bot: Bot|None=None
dp=Dispatcher()
polling_task: asyncio.Task|None=None
cleanup_task: asyncio.Task|None=None

SCHEMA="""
CREATE TABLE IF NOT EXISTS users(
 telegram_id BIGINT PRIMARY KEY, username TEXT, display_name TEXT NOT NULL,
 phone TEXT NOT NULL, public_code TEXT UNIQUE NOT NULL,
 role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','moderator','owner')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS login_challenges(
 nonce TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 expires_at TIMESTAMPTZ NOT NULL, telegram_id BIGINT, consumed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS web_sessions(
 token_hash TEXT PRIMARY KEY, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx ON web_sessions(expires_at);
CREATE TABLE IF NOT EXISTS reports(
 id BIGSERIAL PRIMARY KEY, author_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 city TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('alert','all_clear','shelter','info','drone','explosion','siren','rocket')),
 description TEXT NOT NULL DEFAULT '', latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
 confirmations INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','published','rejected')),
 moderator_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ
);
ALTER TABLE reports ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS confirmations INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reports DROP CONSTRAINT IF EXISTS reports_category_check;
ALTER TABLE reports ADD CONSTRAINT reports_category_check CHECK(category IN ('alert','all_clear','shelter','info','drone','explosion','siren','rocket'));
CREATE TABLE IF NOT EXISTS report_confirmations(report_id BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(report_id,telegram_id));
CREATE INDEX IF NOT EXISTS reports_feed_idx ON reports(status,created_at DESC);
CREATE TABLE IF NOT EXISTS city_chat_messages(id BIGSERIAL PRIMARY KEY, city TEXT NOT NULL, telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL, username TEXT, display_name TEXT NOT NULL, message TEXT NOT NULL CHECK(length(message) BETWEEN 1 AND 1000), created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS city_chat_city_time_idx ON city_chat_messages(city,created_at DESC);
CREATE TABLE IF NOT EXISTS moderation_log(
 id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL, action TEXT NOT NULL,
 target_id BIGINT, details TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cities(id BIGSERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,region TEXT NOT NULL DEFAULT '',latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,enabled BOOLEAN NOT NULL DEFAULT true);
INSERT INTO cities(name,region,latitude,longitude) VALUES
('Москва','Москва',55.7558,37.6173),
('Санкт-Петербург','Санкт-Петербург',59.9343,30.3351),
('Ростов-на-Дону','Ростовская область',47.2357,39.7015),
('Батайск','Ростовская область',47.14,39.75),
('Краснодар','Краснодарский край',45.0355,38.9753),
('Сочи','Краснодарский край',43.5855,39.7231),
('Волгоград','Волгоградская область',48.708,44.5133),
('Воронеж','Воронежская область',51.6608,39.2003),
('Казань','Республика Татарстан',55.7961,49.1064),
('Нижний Новгород','Нижегородская область',56.2965,43.9361),
('Самара','Самарская область',53.1959,50.1002),
('Саратов','Саратовская область',51.5336,46.0343),
('Уфа','Республика Башкортостан',54.7388,55.9721),
('Пермь','Пермский край',58.0105,56.2502),
('Екатеринбург','Свердловская область',56.8389,60.6057),
('Челябинск','Челябинская область',55.1644,61.4368),
('Тюмень','Тюменская область',57.1522,65.5272),
('Омск','Омская область',54.9885,73.3242),
('Новосибирск','Новосибирская область',55.0084,82.9357),
('Томск','Томская область',56.501,84.9924),
('Кемерово','Кемеровская область',55.3547,86.0873),
('Красноярск','Красноярский край',56.0153,92.8932),
('Иркутск','Иркутская область',52.2864,104.2807),
('Улан-Удэ','Республика Бурятия',51.8348,107.5845),
('Чита','Забайкальский край',52.034,113.4994),
('Якутск','Республика Саха',62.0355,129.6755),
('Хабаровск','Хабаровский край',48.4802,135.0719),
('Владивосток','Приморский край',43.1155,131.8855),
('Южно-Сахалинск','Сахалинская область',46.9591,142.738),
('Петропавловск-Камчатский','Камчатский край',53.037,158.6559),
('Мурманск','Мурманская область',68.9707,33.0749),
('Архангельск','Архангельская область',64.5393,40.5187),
('Калининград','Калининградская область',54.7104,20.4522),
('Курск','Курская область',51.7304,36.1926),
('Белгород','Белгородская область',50.5957,36.5872),
('Липецк','Липецкая область',52.6088,39.5992),
('Тула','Тульская область',54.1931,37.6173),
('Ярославль','Ярославская область',57.6261,39.8845),
('Ижевск','Удмуртская Республика',56.8527,53.2115),
('Оренбург','Оренбургская область',51.7682,55.0969),
('Великий Новгород','Новгородская область',58.5215,31.2755),
('Псков','Псковская область',57.8194,28.3318),
('Брянск','Брянская область',53.2434,34.3642),
('Смоленск','Смоленская область',54.7826,32.0453),
('Орёл','Орловская область',52.9671,36.0696),
('Тамбов','Тамбовская область',52.7212,41.4523),
('Рязань','Рязанская область',54.6095,39.7126),
('Владимир','Владимирская область',56.129,40.4066),
('Иваново','Ивановская область',56.9972,40.9714),
('Кострома','Костромская область',57.7679,40.9269),
('Вологда','Вологодская область',59.2205,39.8915),
('Череповец','Вологодская область',59.1269,37.9091),
('Петрозаводск','Республика Карелия',61.789,34.3596),
('Сыктывкар','Республика Коми',61.6688,50.8356),
('Киров','Кировская область',58.6035,49.6679),
('Йошкар-Ола','Республика Марий Эл',56.6344,47.8999),
('Чебоксары','Чувашская Республика',56.1439,47.2489),
('Ульяновск','Ульяновская область',54.3142,48.4031),
('Пенза','Пензенская область',53.195,45.0183),
('Тольятти','Самарская область',53.5078,49.4204),
('Набережные Челны','Республика Татарстан',55.7436,52.3958),
('Стерлитамак','Республика Башкортостан',53.6303,55.9304),
('Магнитогорск','Челябинская область',53.4072,58.9791),
('Курган','Курганская область',55.441,65.3411),
('Барнаул','Алтайский край',53.3474,83.7784),
('Новокузнецк','Кемеровская область',53.7596,87.1216),
('Абакан','Республика Хакасия',53.7224,91.4438),
('Кызыл','Республика Тыва',51.7191,94.4378),
('Горно-Алтайск','Республика Алтай',51.9581,85.9603),
('Благовещенск','Амурская область',50.2906,127.5272),
('Комсомольск-на-Амуре','Хабаровский край',50.5503,137.0079),
('Биробиджан','Еврейская автономная область',48.7946,132.9218),
('Анадырь','Чукотский автономный округ',64.7337,177.5089),
('Магадан','Магаданская область',59.5682,150.8085),
('Нарьян-Мар','Ненецкий автономный округ',67.638,53.0069),
('Салехард','Ямало-Ненецкий автономный округ',66.5494,66.6084),
('Ханты-Мансийск','Ханты-Мансийский автономный округ',61.0032,69.0189),
('Сургут','Ханты-Мансийский автономный округ',61.254,73.3962),
('Нижневартовск','Ханты-Мансийский автономный округ',60.9397,76.5696),
('Новый Уренгой','Ямало-Ненецкий автономный округ',66.0853,76.6812),
('Норильск','Красноярский край',69.3558,88.1893),
('Элиста','Республика Калмыкия',46.3077,44.2558),
('Астрахань','Астраханская область',46.3497,48.0408),
('Махачкала','Республика Дагестан',42.9849,47.5047),
('Грозный','Чеченская Республика',43.3178,45.6981),
('Назрань','Республика Ингушетия',43.2257,44.7646),
('Владикавказ','Республика Северная Осетия — Алания',43.0246,44.6818),
('Нальчик','Кабардино-Балкарская Республика',43.4853,43.6071),
('Черкесск','Карачаево-Черкесская Республика',44.2269,42.0468),
('Ставрополь','Ставропольский край',45.0445,41.9691),
('Пятигорск','Ставропольский край',44.0486,43.0594),
('Майкоп','Республика Адыгея',44.6098,40.1006),
('Таганрог','Ростовская область',47.2095,38.9358),
('Новороссийск','Краснодарский край',44.7239,37.7689),
('Новочеркасск','Ростовская область',47.4221,40.0937),
('Азов','Ростовская область',47.1122,39.4233),
('Шахты','Ростовская область',47.7085,40.2159),
('Волгодонск','Ростовская область',47.5165,42.1989),
('Гуково','Ростовская область',48.062,39.9355),
('Симферополь','Республика Крым',44.9521,34.1024),
('Севастополь','Севастополь',44.6167,33.5254),
('Евпатория','Республика Крым',45.1904,33.3669),
('Ялта','Республика Крым',44.4952,34.1663)

ON CONFLICT(name) DO UPDATE SET region=EXCLUDED.region,latitude=EXCLUDED.latitude,longitude=EXCLUDED.longitude;
"""

def ru_phone(raw:str)->str|None:
 d=re.sub(r"\D","",raw or "")
 if len(d)==11 and d[0]=="8": d="7"+d[1:]
 if len(d)==10: d="7"+d
 return "+"+d if len(d)==11 and d.startswith("7") else None

def verify_init_data(raw:str)->dict:
 if not raw or len(raw)>16384: raise ValueError("invalid initData")
 data=dict(parse_qsl(raw,keep_blank_values=True,strict_parsing=True))
 got=data.pop("hash",None)
 if not got or not re.fullmatch(r"[a-f0-9]{64}",got): raise ValueError("missing hash")
 check="\n".join(f"{k}={v}" for k,v in sorted(data.items()))
 secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
 expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
 if not hmac.compare_digest(expected,got): raise ValueError("bad signature")
 issued=int(data.get("auth_date","0")); now=int(time.time())
 if issued>now+60 or now-issued>INITDATA_MAX_AGE: raise ValueError("expired initData")
 user=json.loads(data.get("user","{}"))
 if not user.get("id"): raise ValueError("missing Telegram user")
 return user

async def current_user(auth:str|None):
 if not auth or not auth.startswith("Bearer "):
  raise HTTPException(401,"Войдите через Telegram.")
 token=auth[7:].strip()
 if len(token)<32 or len(token)>256: raise HTTPException(401,"Недействительная сессия.")
 token_hash=hashlib.sha256(token.encode()).hexdigest()
 row=await pool.fetchrow("SELECT u.* FROM web_sessions s JOIN users u ON u.telegram_id=s.telegram_id WHERE s.token_hash=$1 AND s.expires_at>now()",token_hash)
 if not row: raise HTTPException(401,"Сессия истекла. Войдите через Telegram ещё раз.")
 await pool.execute("UPDATE users SET last_seen_at=now() WHERE telegram_id=$1",row["telegram_id"])
 return row

async def moderator(auth:str|None):
 row=await current_user(auth)
 if row["role"] not in ("owner","moderator"): raise HTTPException(403,"Недостаточно прав.")
 return row

def user_json(r):
 return {"id":r["telegram_id"],"username":r["username"],"telegram_url":("https://t.me/"+r["username"]) if r["username"] else ("tg://user?id="+str(r["telegram_id"])),"name":r["display_name"],
 "code":r["public_code"],"role":r["role"],"created_at":r["created_at"].isoformat()}

def feed_json(r):
 return {"id":r["id"],"city":r["city"],"category":r["category"],
 "description":r["description"],"latitude":r["latitude"] if "latitude" in r.keys() else None,"longitude":r["longitude"] if "longitude" in r.keys() else None,"status":r["status"] if "status" in r.keys() else "published","created_at":r["created_at"].isoformat()}

def app_kb(path: str = ""):
 url=MAP_URL + (path if path.startswith("/") else ("/"+path if path else ""))
 return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛰 Открыть H1 RADAR",url=url)]])

async def ensure_user(u):
 name=" ".join(x for x in [u.first_name,u.last_name] if x) or "Пользователь"
 role="owner" if u.id==BOT_OWNER else "user"
 row=await pool.fetchrow("SELECT public_code FROM users WHERE telegram_id=$1",u.id)
 if row:
  await pool.execute("UPDATE users SET username=$2,display_name=$3,last_seen_at=now(),role=CASE WHEN $1=$4 THEN 'owner' ELSE role END WHERE telegram_id=$1",u.id,u.username,name,BOT_OWNER)
  return row["public_code"],False
 for _ in range(40):
  code="RU-"+ "".join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(4))
  try:
   await pool.execute("INSERT INTO users(telegram_id,username,display_name,phone,public_code,role) VALUES($1,$2,$3,'',$4,$5)",u.id,u.username,name,code,role)
   return code,True
  except asyncpg.UniqueViolationError:
   continue
 raise RuntimeError("Не удалось выделить уникальный код пользователя")

async def configure_bot_profile(b:Bot):
 await b.set_my_name("H1 RADAR")
 await b.set_my_description("H1 RADAR — гражданская карта и лента сообщений сообщества. Не является официальным источником экстренных оповещений.")
 await b.set_my_short_description("Гражданская карта и сообщения сообщества.")
 await b.set_my_commands([BotCommand(command="start",description="Открыть H1 RADAR"),BotCommand(command="help",description="Помощь")])
 # Убираем прежнюю кнопку Mini App из меню Telegram.
 await b.set_chat_menu_button(menu_button=MenuButtonDefault())

@dp.message(CommandStart())
async def start(m:Message):
 code,fresh=await ensure_user(m.from_user)
 payload=(m.text or "").split(maxsplit=1)
 if len(payload)>1 and payload[1].startswith("login_"):
  nonce=payload[1][6:]
  valid=await pool.fetchval("UPDATE login_challenges SET telegram_id=$2 WHERE nonce=$1 AND expires_at>now() AND consumed_at IS NULL RETURNING nonce",nonce,m.from_user.id)
  if valid:
   await m.answer("Вход подтверждён. Возвращайтесь во вкладку браузера — сайт завершит авторизацию автоматически.",reply_markup=app_kb())
   return
 greeting="Аккаунт H1 RADAR создан." if fresh else "С возвращением в H1 RADAR."
 await m.answer(f"{greeting}\nВаш код: <b>{code}</b>\n\nКарта открывается в обычном браузере — без Telegram Mini App.",reply_markup=app_kb(),parse_mode="HTML")

@dp.message(Command("help"))
async def help_cmd(m:Message):
 await m.answer("/start — открыть H1 RADAR и войти\n/help — помощь по входу и использованию карты")

async def expire_pending_reports():
 while True:
  try:
   if pool is not None:
    result=await pool.execute("DELETE FROM reports WHERE status='pending' AND created_at<now()-interval '3 minutes'")
    if result!="DELETE 0": log.info("Expired unconfirmed marks: %s",result)
    await pool.execute("DELETE FROM login_challenges WHERE expires_at<now() OR consumed_at IS NOT NULL")
    await pool.execute("DELETE FROM web_sessions WHERE expires_at<now()")
  except asyncio.CancelledError:
   raise
  except Exception:
   log.exception("Pending mark cleanup failed")
  await asyncio.sleep(20)

@asynccontextmanager
async def lifespan(app:FastAPI):
 global pool,bot,polling_task,cleanup_task
 pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=int(os.getenv("DB_POOL_SIZE","10")),command_timeout=8,ssl=False,statement_cache_size=0)
 async with pool.acquire() as c: await c.execute(SCHEMA)
 bot=Bot(BOT_TOKEN)
 try:
  global BOT_USERNAME
  if not BOT_USERNAME:
   me=await bot.get_me(); BOT_USERNAME=me.username or ""
  await configure_bot_profile(bot)
  log.info("Telegram bot profile configured; reply keyboards and Mini App menu disabled")
 except Exception:
  log.exception("Could not configure Telegram bot profile/menu; polling will continue")
 polling_task=asyncio.create_task(dp.start_polling(bot,handle_signals=False))
 cleanup_task=asyncio.create_task(expire_pending_reports())
 yield
 if cleanup_task:
  cleanup_task.cancel()
  await asyncio.gather(cleanup_task,return_exceptions=True)
 if polling_task: polling_task.cancel()
 if bot: await bot.session.close()
 if pool: await pool.close()

app=FastAPI(title="H1 RADAR API",version="1.0.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[WEBAPP_URL,MAP_URL],allow_credentials=False,allow_methods=["GET","POST","DELETE"],allow_headers=["Authorization","Content-Type"])
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index(request:Request):
    host=request.headers.get("host","").split(":")[0].lower()
    if host.startswith("moder."):
        return FileResponse("static/moderation.html")
    if host.startswith("map."):
        return FileResponse("static/index.html")
    return FileResponse("static/home.html")

@app.get("/map")
async def map_page():
    return FileResponse("static/index.html")

@app.get("/moderation")
async def moderation_page():
    return FileResponse("static/moderation.html")


class ReportIn(BaseModel):
 city:str=Field(min_length=2,max_length=80)
 category:str=Field(pattern="^(alert|all_clear|shelter|info|drone|explosion|siren|rocket)$")
 description:str=Field(default="",max_length=500)
 latitude:float|None=Field(default=None,ge=-90,le=90)
 longitude:float|None=Field(default=None,ge=-180,le=180)

@app.get("/privacy")
async def privacy():
    return FileResponse("static/privacy.html")

@app.get("/api/health")
async def health(): return {"ok":True,"service":"h1-radar","time":datetime.now(timezone.utc).isoformat()}

@app.get("/api/config")
async def public_config():
 return {"bot_username":BOT_USERNAME,"map_url":MAP_URL}

@app.get("/api/public/overview")
async def public_overview():
 # Public aggregate metrics only: no personal identifiers or precise live report locations.
 async with pool.acquire() as c:
  row=await c.fetchrow("""SELECT
   (SELECT count(*) FROM cities WHERE enabled=true) AS cities,
   (SELECT count(*) FROM users) AS members,
   (SELECT count(*) FROM reports WHERE status='published') AS published_total,
   (SELECT count(*) FROM reports WHERE status='published' AND created_at>=now()-interval '24 hours') AS published_24h,
   (SELECT count(*) FROM city_chat_messages WHERE created_at>=now()-interval '24 hours') AS chat_24h,
   (SELECT count(*) FROM reports WHERE status='pending' AND created_at>=now()-interval '3 minutes') AS pending_recent""")
  status=await c.fetchrow("""SELECT count(*) FILTER (WHERE last_seen_at>now()-interval '15 minutes') AS online,
   max(created_at) AS last_report FROM reports WHERE status='published'""")
 return {"ok":True,"updated_at":datetime.now(timezone.utc).isoformat(),
  "cities":int(row["cities"] or 0),"members":int(row["members"] or 0),
  "published_total":int(row["published_total"] or 0),"published_24h":int(row["published_24h"] or 0),
  "chat_24h":int(row["chat_24h"] or 0),"pending_recent":int(row["pending_recent"] or 0),
  "online":int(status["online"] or 0),"last_activity":status["last_report"].isoformat() if status["last_report"] else None}

@app.post("/api/auth/start")
async def auth_start(request:Request):
 client_ip=request.client.host if request.client else "unknown"
 now=time.monotonic()
 attempts=[stamp for stamp in _auth_attempts.get(client_ip,[]) if now-stamp<600]
 if len(attempts)>=8:
  raise HTTPException(429,"Слишком много попыток входа. Попробуйте через несколько минут.")
 attempts.append(now); _auth_attempts[client_ip]=attempts
 if not BOT_USERNAME: raise HTTPException(503,"Бот ещё запускается. Попробуйте снова.")
 nonce=secrets.token_urlsafe(24)
 await pool.execute("INSERT INTO login_challenges(nonce,expires_at) VALUES($1,now()+interval '10 minutes')",nonce)
 return {"ok":True,"nonce":nonce,"bot_url":f"https://t.me/{BOT_USERNAME}?start=login_{nonce}"}

@app.get("/api/auth/poll")
async def auth_poll(nonce:str):
 if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}",nonce): raise HTTPException(400,"Некорректный запрос входа.")
 async with pool.acquire() as c:
  async with c.transaction():
   challenge=await c.fetchrow("SELECT telegram_id,expires_at,consumed_at FROM login_challenges WHERE nonce=$1 FOR UPDATE",nonce)
   if not challenge or challenge["expires_at"]<=datetime.now(timezone.utc) or challenge["consumed_at"]:
    raise HTTPException(410,"Ссылка входа истекла. Начните заново.")
   if not challenge["telegram_id"]: return {"ok":True,"pending":True}
   token=secrets.token_urlsafe(40)
   digest=hashlib.sha256(token.encode()).hexdigest()
   await c.execute("INSERT INTO web_sessions(token_hash,telegram_id,expires_at) VALUES($1,$2,now()+interval '30 days')",digest,challenge["telegram_id"])
   await c.execute("UPDATE login_challenges SET consumed_at=now() WHERE nonce=$1",nonce)
   return {"ok":True,"pending":False,"token":token}

@app.get("/api/me")
async def me(authorization:str|None=Header(default=None)):
 r=await current_user(authorization); return {"user":user_json(r)}

@app.get("/api/cities")
async def cities():
 rows=await pool.fetch("SELECT id,name,region,latitude,longitude FROM cities WHERE enabled=true ORDER BY name")
 return {"cities":[dict(r) for r in rows]}

@app.get("/api/feed")
async def feed(city:str|None=None,limit:int=50,hours:int=24):
 limit=max(1,min(limit,100)); hours=max(1,min(hours,720))
 # Pending marks are visible to everyone for their three-minute confirmation window.
 if city: rows=await pool.fetch("SELECT id,city,category,description,latitude,longitude,confirmations,created_at,status FROM reports WHERE status IN ('published','pending') AND (status='published' OR created_at>=now()-interval '3 minutes') AND city=$1 AND created_at>=now()-make_interval(hours=>$2) ORDER BY created_at DESC LIMIT $3",city,hours,limit)
 else: rows=await pool.fetch("SELECT id,city,category,description,latitude,longitude,confirmations,created_at,status FROM reports WHERE status IN ('published','pending') AND (status='published' OR created_at>=now()-interval '3 minutes') AND created_at>=now()-make_interval(hours=>$1) ORDER BY created_at DESC LIMIT $2",hours,limit)
 return {"items":[feed_json(r) for r in rows]}

@app.post("/api/reports")
async def create_report(body:ReportIn,authorization:str|None=Header(default=None)):
 u=await current_user(authorization)
 city=body.city.strip(); desc=body.description.strip()
 if body.latitude is None or body.longitude is None:
  raise HTTPException(400,"Укажи точку на карте или разреши геопозицию.")
 if not point_in_russia(body.longitude,body.latitude):
  raise HTTPException(400,"Отметки можно ставить только на территории России.")
 if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM cities WHERE name=$1 AND enabled=true)",city):
  raise HTTPException(400,"Выберите город из списка.")
 lat=round(body.latitude,2) if body.latitude is not None else None
 lon=round(body.longitude,2) if body.longitude is not None else None
 async with pool.acquire() as c:
  async with c.transaction():
   await c.execute("SELECT pg_advisory_xact_lock(hashtext($1),hashtext($2))",city,body.category)
   await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))",str(u["telegram_id"]))
   last_mark=await c.fetchval("""SELECT max(mark_time) FROM (SELECT created_at AS mark_time FROM reports WHERE author_id=$1 UNION ALL SELECT created_at AS mark_time FROM report_confirmations WHERE telegram_id=$1) recent_marks WHERE mark_time>now()-interval '3 minutes'""",u["telegram_id"])
   if last_mark:
    raise HTTPException(429,"Одну отметку можно отправлять раз в 3 минуты. Попробуй немного позже.")
   nearby=None
   if lat is not None and lon is not None:
    nearby=await c.fetchrow("""SELECT id,author_id FROM reports
     WHERE status='pending' AND city=$1 AND category=$2
     AND latitude BETWEEN $3-0.015 AND $3+0.015
     AND longitude BETWEEN $4-0.02 AND $4+0.02
     AND created_at>now()-interval '3 minutes'
     ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",city,body.category,lat,lon)
   if nearby:
    if nearby["author_id"]==u["telegram_id"]:
     raise HTTPException(409,"Ты уже отправлял отметку рядом. Дождись подтверждений других людей.")
    await c.execute("INSERT INTO report_confirmations(report_id,telegram_id) VALUES($1,$2) ON CONFLICT DO NOTHING",nearby["id"],u["telegram_id"])
    count=await c.fetchval("SELECT count(*) FROM report_confirmations WHERE report_id=$1",nearby["id"])
    await c.execute("UPDATE reports SET confirmations=$2,status=CASE WHEN $2>=3 THEN 'published' ELSE status END,reviewed_at=CASE WHEN $2>=3 THEN now() ELSE reviewed_at END WHERE id=$1",nearby["id"],count)
    return {"ok":True,"id":nearby["id"],"status":"published" if count>=3 else "pending","message":"Отметка подтверждена сообществом." if count>=3 else "Твоя отметка добавлена к сообщению. Проверка сообщества продолжается."}
   row=await c.fetchrow("INSERT INTO reports(author_id,city,category,description,latitude,longitude) VALUES($1,$2,$3,$4,$5,$6) RETURNING id",u["telegram_id"],city,body.category,desc,lat,lon)
   await c.execute("INSERT INTO report_confirmations(report_id,telegram_id) VALUES($1,$2) ON CONFLICT DO NOTHING",row["id"],u["telegram_id"])
 return {"ok":True,"id":row["id"],"status":"pending","message":"Отметка появилась на карте. Если в течение 3 минут её не подтвердят другие участники, она исчезнет автоматически."}

@app.post("/api/reports/{report_id}/confirm")
async def confirm_report(report_id:int,authorization:str|None=Header(default=None)):
 u=await current_user(authorization)
 async with pool.acquire() as c:
  async with c.transaction():
   row=await c.fetchrow("SELECT id,status,author_id FROM reports WHERE id=$1 FOR UPDATE",report_id)
   if not row or row["status"]!="pending": raise HTTPException(404,"Отметка не найдена или уже обработана.")
   if row["author_id"]==u["telegram_id"]: raise HTTPException(400,"Нельзя подтверждать собственную отметку.")
   await c.execute("INSERT INTO report_confirmations(report_id,telegram_id) VALUES($1,$2) ON CONFLICT DO NOTHING",report_id,u["telegram_id"])
   count=await c.fetchval("SELECT count(*) FROM report_confirmations WHERE report_id=$1",report_id)
   await c.execute("UPDATE reports SET confirmations=$2,status=CASE WHEN $2>=3 THEN 'published' ELSE status END,reviewed_at=CASE WHEN $2>=3 THEN now() ELSE reviewed_at END WHERE id=$1",report_id,count)
 return {"ok":True,"confirmations":count,"published":count>=3}

@app.get("/api/moderation/summary")
async def mod_summary(authorization:str|None=Header(default=None)):
 await moderator(authorization)
 return {"pending":await pool.fetchval("SELECT count(*) FROM reports WHERE status='pending'"),
 "users":await pool.fetchval("SELECT count(*) FROM users"),
 "published":await pool.fetchval("SELECT count(*) FROM reports WHERE status='published'" ),"today_users":await pool.fetchval("SELECT count(*) FROM users WHERE created_at>=date_trunc('day',now())"),"today_events":await pool.fetchval("SELECT count(*) FROM reports WHERE created_at>=date_trunc('day',now())"),"online":await pool.fetchval("SELECT count(*) FROM users WHERE last_seen_at>now()-interval '15 minutes'")}

@app.get("/api/moderation/reports")
async def mod_reports(authorization:str|None=Header(default=None)):
 await moderator(authorization)
 rows=await pool.fetch("SELECT r.id,r.city,r.category,r.description,r.created_at,u.public_code FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status='pending' ORDER BY r.created_at LIMIT 100")
 return {"items":[{**feed_json(r),"author_code":r["public_code"]} for r in rows]}

@app.get("/api/moderation/events")
async def mod_events(authorization:str|None=Header(default=None),q:str="",hours:int=168):
 await moderator(authorization)
 hours=max(1,min(hours,720)); q=q.strip()
 rows=await pool.fetch("""SELECT r.id,r.city,r.category,r.description,r.latitude,r.longitude,r.confirmations,r.status,r.created_at,u.public_code,u.username,u.telegram_id
 FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id
 WHERE r.created_at>=now()-($1::text||' hours')::interval
 AND ($2='' OR r.city ILIKE $3 OR r.category ILIKE $3 OR r.description ILIKE $3 OR u.username ILIKE $3 OR u.public_code ILIKE $3)
 ORDER BY r.created_at DESC LIMIT 500""",str(hours),q,"%"+q+"%")
 return {"items":[{**feed_json(r),"status":r["status"],"author_code":r["public_code"],"username":r["username"],"telegram_id":r["telegram_id"]} for r in rows]}

@app.get("/api/moderation/marks")
async def mod_marks(authorization:str|None=Header(default=None),city:str|None=None):
 await moderator(authorization)
 rows=await pool.fetch("SELECT r.id,r.city,r.category,r.description,r.latitude,r.longitude,r.confirmations,r.status,r.created_at,u.public_code,u.username,u.telegram_id FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status='published' AND ($1::text IS NULL OR r.city=$1) ORDER BY r.created_at DESC LIMIT 500",city)
 return {"items":[{**feed_json(r),"status":r["status"],"author_code":r["public_code"],"username":r["username"],"telegram_id":r["telegram_id"]} for r in rows]}

@app.post("/api/moderation/reports/{report_id}/{decision}")
async def review(report_id:int,decision:str,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if decision not in ("publish","reject"): raise HTTPException(400,"Недопустимое решение.")
 status="published" if decision=="publish" else "rejected"
 async with pool.acquire() as c:
  async with c.transaction():
   result=await c.execute("UPDATE reports SET status=$1,moderator_id=$2,reviewed_at=now() WHERE id=$3 AND status='pending'",status,actor["telegram_id"],report_id)
   if result.endswith("0"): raise HTTPException(404,"Сообщение уже обработано или не найдено.")
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id) VALUES($1,$2,$3)",actor["telegram_id"],status,report_id)
 return {"ok":True,"status":status}

@app.get("/api/moderation/users")
async def mod_users(authorization:str|None=Header(default=None),q:str=""):
 await moderator(authorization)
 q=q.strip()
 if q:
  rows=await pool.fetch("SELECT telegram_id,username,display_name,public_code,role,created_at,last_seen_at FROM users WHERE public_code ILIKE $1 OR username ILIKE $1 OR display_name ILIKE $1 OR telegram_id::text ILIKE $1 ORDER BY created_at DESC LIMIT 500","%"+q+"%")
 else: rows=await pool.fetch("SELECT telegram_id,username,display_name,public_code,role,created_at,last_seen_at FROM users ORDER BY created_at DESC LIMIT 500")
 return {"items":[{"id":r["telegram_id"],"telegram_id":r["telegram_id"],"username":r["username"],"telegram_url":("https://t.me/"+r["username"]) if r["username"] else ("tg://user?id="+str(r["telegram_id"])),"name":r["display_name"],"code":r["public_code"],"role":r["role"],"created_at":r["created_at"].isoformat(),"last_seen_at":r["last_seen_at"].isoformat(),"online":r["last_seen_at"]>datetime.now(timezone.utc)-timedelta(minutes=15)} for r in rows]}

@app.post("/api/moderation/users/{telegram_id}/role/{role}")
async def set_role(telegram_id:int,role:str,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Назначать модераторов может только владелец.")
 if role not in ("user","moderator"): raise HTTPException(400,"Недопустимая роль.")
 if telegram_id==BOT_OWNER: raise HTTPException(400,"Нельзя изменить роль владельца.")
 async with pool.acquire() as c:
  async with c.transaction():
   result=await c.execute("UPDATE users SET role=$1 WHERE telegram_id=$2",role,telegram_id)
   if result.endswith("0"): raise HTTPException(404,"Пользователь не найден.")
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'role_change',$2,$3)",actor["telegram_id"],telegram_id,role)
 return {"ok":True,"role":role}

@app.get("/api/city-chat")
async def city_chat(city:str,after:int=0):
 if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM cities WHERE name=$1 AND enabled=true)",city):
  raise HTTPException(400,"Неизвестный город.")
 rows=await pool.fetch("SELECT id,username,display_name,message,created_at FROM city_chat_messages WHERE city=$1 AND id>$2 ORDER BY id DESC LIMIT 100",city,after)
 return {"items":[{"id":r["id"],"username":r["username"],"name":r["display_name"],"message":r["message"],"created_at":r["created_at"].isoformat()} for r in reversed(rows)]}

@app.post("/api/city-chat")
async def city_chat_send(body:dict,authorization:str|None=Header(default=None)):
 u=await current_user(authorization)
 city=str(body.get("city","")).strip(); message=str(body.get("message","")).strip()
 if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM cities WHERE name=$1 AND enabled=true)",city):
  raise HTTPException(400,"Выберите город.")
 if not message or len(message)>1000: raise HTTPException(400,"Сообщение должно быть от 1 до 1000 символов.")
 async with pool.acquire() as c:
  async with c.transaction():
   await c.execute("SELECT pg_advisory_xact_lock(hashtext($1))",str(u["telegram_id"]))
   recent=await c.fetchval("SELECT count(*) FROM city_chat_messages WHERE telegram_id=$1 AND created_at>now()-interval '10 minutes'",u["telegram_id"])
   last=await c.fetchval("SELECT created_at FROM city_chat_messages WHERE telegram_id=$1 ORDER BY created_at DESC LIMIT 1",u["telegram_id"])
   if last and (datetime.now(timezone.utc)-last).total_seconds()<2:
    raise HTTPException(429,"Не отправляй сообщения чаще одного раза в 2 секунды.")
   if recent>=30:
    raise HTTPException(429,"Лимит сообщений в городском чате на 10 минут исчерпан.")
   r=await c.fetchrow("INSERT INTO city_chat_messages(city,telegram_id,username,display_name,message) VALUES($1,$2,$3,$4,$5) RETURNING id,created_at",city,u["telegram_id"],u["username"],u["display_name"],message)
 return {"ok":True,"id":r["id"],"created_at":r["created_at"].isoformat()}

@app.delete("/api/moderation/chat/{message_id}")
async def moderation_delete_chat(message_id:int,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 async with pool.acquire() as c:
  async with c.transaction():
   row=await c.fetchrow("SELECT city,telegram_id,message FROM city_chat_messages WHERE id=$1 FOR UPDATE",message_id)
   if not row: raise HTTPException(404,"Сообщение уже удалено или не найдено.")
   await c.execute("DELETE FROM city_chat_messages WHERE id=$1",message_id)
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'chat_message_deleted',$2,$3)",actor["telegram_id"],row["telegram_id"],"Город: "+row["city"]+" · сообщение #"+str(message_id))
 return {"ok":True}

@app.get("/api/moderation/log")
async def mod_log(authorization:str|None=Header(default=None)):
 await moderator(authorization)
 rows=await pool.fetch("SELECT actor_id,action,target_id,details,created_at FROM moderation_log ORDER BY created_at DESC LIMIT 100")
 return {"items":[{"actor":r["actor_id"],"action":r["action"],"target":r["target_id"],"details":r["details"],"created_at":r["created_at"].isoformat()} for r in rows]}


@app.delete("/api/account")
async def delete_account(authorization:str|None=Header(default=None)):
    u=await current_user(authorization)
    if u["role"]=="owner":
        raise HTTPException(400,"Удаление аккаунта владельца через приложение отключено.")
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute("DELETE FROM moderation_log WHERE actor_id=$1 OR target_id=$1",u["telegram_id"])
            await c.execute("DELETE FROM users WHERE telegram_id=$1",u["telegram_id"])
    return {"ok":True,"message":"Аккаунт и номер удалены. Опубликованные сообщения обезличены."}
