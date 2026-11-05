from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, os, re, secrets, string, time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlsplit, urlunsplit
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp, WebAppInfo)
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
CREATE TABLE IF NOT EXISTS user_restrictions(telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,reason TEXT NOT NULL DEFAULT '',expires_at TIMESTAMPTZ,created_by BIGINT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS city_requests(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,city_name TEXT NOT NULL,region TEXT NOT NULL DEFAULT '',latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),moderator_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),reviewed_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS cities(id BIGSERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,region TEXT NOT NULL DEFAULT '',latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,enabled BOOLEAN NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS moderator_city_permissions(
 id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
 city_id BIGINT NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
 permission TEXT NOT NULL CHECK(permission IN ('events','marks','people','beta','outages','statistics','appeals','announcements','feed','support','ambassador')),
 granted_by BIGINT NOT NULL REFERENCES users(telegram_id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(telegram_id,city_id,permission)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_city_ambassador_idx ON moderator_city_permissions(city_id) WHERE permission='ambassador';
CREATE INDEX IF NOT EXISTS moderator_city_permissions_user_idx ON moderator_city_permissions(telegram_id,city_id);
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
 restriction=await pool.fetchrow("SELECT reason,expires_at FROM user_restrictions WHERE telegram_id=$1",row["telegram_id"])
 if restriction and (restriction["expires_at"] is None or restriction["expires_at"]>datetime.now(timezone.utc)):
  raise HTTPException(403,"Доступ ограничен модерацией."+((" Причина: "+restriction["reason"]) if restriction["reason"] else ""))
 if restriction: await pool.execute("DELETE FROM user_restrictions WHERE telegram_id=$1",row["telegram_id"])
 await pool.execute("UPDATE users SET last_seen_at=now() WHERE telegram_id=$1",row["telegram_id"])
 return row

async def moderator(auth:str|None,permission:str|None=None,city:str|None=None):
 row=await current_user(auth)
 if row["role"] not in ("owner","moderator"): raise HTTPException(403,"У аккаунта нет доступа к панели команды.")
 if permission: await require_permission(row,permission,city)
 return row

PERMISSIONS={"events","marks","people","beta","outages","statistics","appeals","announcements","feed","support","ambassador"}
async def permissions_for(user):
 if user["role"]=="owner": return sorted(PERMISSIONS)
 rows=await pool.fetch("SELECT c.name,p.permission FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 ORDER BY c.name,p.permission",user["telegram_id"])
 return [{"city":r["name"],"permission":r["permission"]} for r in rows]
async def require_permission(actor,permission,city=None):
 if actor["role"]=="owner": return
 if actor["role"]!="moderator": raise HTTPException(403,"У аккаунта нет доступа к этому разделу.")
 if city:
  ok=await pool.fetchval("SELECT EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission=$2 AND c.name=$3)",actor["telegram_id"],permission,city)
 else:
  ok=await pool.fetchval("SELECT EXISTS(SELECT 1 FROM moderator_city_permissions WHERE telegram_id=$1 AND permission=$2)",actor["telegram_id"],permission)
 if not ok: raise HTTPException(403,"У аккаунта нет доступа к этому разделу.")
def user_json(r):
 return {"id":r["telegram_id"],"username":r["username"],"telegram_url":("https://t.me/"+r["username"]) if r["username"] else ("tg://user?id="+str(r["telegram_id"])),"name":r["display_name"],
 "code":r["public_code"],"role":r["role"],"created_at":r["created_at"].isoformat()}

def feed_json(r):
 return {"id":r["id"],"city":r["city"],"category":r["category"],
 "description":r["description"],"latitude":r["latitude"] if "latitude" in r.keys() else None,"longitude":r["longitude"] if "longitude" in r.keys() else None,"status":r["status"] if "status" in r.keys() else "published","created_at":r["created_at"].isoformat()}

def app_kb(path: str = ""):
 url=MAP_URL + (path if path.startswith("/") else ("/"+path if path else ""))
 return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛰 Открыть карту H1 RADAR",url=url)],[InlineKeyboardButton(text="Политика конфиденциальности",url=url+"/privacy")]])

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
 # Профиль и меню настраиваются независимо: сбой одной операции не ломает остальные.
 operations=[
  ("name",lambda:b.set_my_name("H1 RADAR")),
  ("description",lambda:b.set_my_description("H1 RADAR — гражданская карта, городские сообщения и сообщества. Вход и регистрация через Telegram.")),
  ("short_description",lambda:b.set_my_short_description("Гражданская карта и городские сообщества.")),
  ("commands",lambda:b.set_my_commands([BotCommand(command="start",description="Открыть H1 RADAR"),BotCommand(command="help",description="Помощь")])),
  ("menu_button",lambda:b.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть H1 RADAR",web_app=WebAppInfo(url=MAP_URL)))),
 ]
 async def apply_setting(label,operation):
  try:
   await asyncio.wait_for(operation(),timeout=8)
   log.info("Telegram bot profile setting applied: %s",label)
  except asyncio.CancelledError:
   raise
  except Exception:
   log.exception("Telegram bot profile setting failed: %s",label)
 await asyncio.gather(*(apply_setting(label,operation) for label,operation in operations))
 # Рисуем JPG-аватар в фирменном стиле H1. Bot API принимает профильное фото именно в JPG.
 try:
  from PIL import Image,ImageDraw,ImageFont
  from io import BytesIO
  from aiogram.types import BufferedInputFile,InputProfilePhotoStatic
  if Path("static/h1-avatar.svg").is_file():
   size=512
   image=Image.new("RGB",(size,size))
   pixels=image.load()
   for y in range(size):
    for x in range(size):
     t=(x+y)/(2*(size-1))
     pixels[x,y]=(int(18*(1-t)+6*t),int(62*(1-t)+17*t),int(88*(1-t)+31*t))
   draw=ImageDraw.Draw(image)
   draw.rounded_rectangle((18,18,494,494),radius=104,outline=(117,223,210),width=4)
   for radius,alpha in ((178,85),(143,55)):
    box=(256-radius,248-radius,256+radius,248+radius)
    draw.ellipse(box,outline=(105,210,202),width=3)
   draw.line((256,248,390,116),fill=(158,244,224),width=5)
   draw.ellipse((379,105,401,127),fill=(169,245,228))
   draw.ellipse((248,240,264,256),fill=(169,245,228))
   font_dir=os.path.join(os.path.dirname(ImageFont.__file__),"fonts")
   title_font=ImageFont.truetype(os.path.join(font_dir,"DejaVuSans-Bold.ttf"),150)
   small_font=ImageFont.truetype(os.path.join(font_dir,"DejaVuSans-Bold.ttf"),27)
   title="H1"; box=draw.textbbox((0,0),title,font=title_font)
   draw.text(((size-(box[2]-box[0]))/2,160),title,font=title_font,fill=(207,255,241),stroke_width=1,stroke_fill=(106,192,205))
   subtitle="R A D A R"; box=draw.textbbox((0,0),subtitle,font=small_font)
   draw.text(((size-(box[2]-box[0]))/2,383),subtitle,font=small_font,fill=(142,219,215))
   jpg=BytesIO()
   image.save(jpg,format="JPEG",quality=94,optimize=True)
   photo=BufferedInputFile(jpg.getvalue(),filename="h1-radar-avatar.jpg")
   await asyncio.wait_for(b.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=photo)),timeout=15)
   log.info("Telegram bot profile photo updated with H1 RADAR avatar")
  else:
   log.warning("Bot avatar source static/h1-avatar.svg is missing")
 except asyncio.CancelledError:
  raise
 except Exception:
  log.exception("Telegram bot avatar update failed; bot polling will continue")

@dp.message(CommandStart())
async def start(m:Message):
 code,fresh=await ensure_user(m.from_user)
 payload=(m.text or "").split(maxsplit=1)
 if len(payload)>1 and payload[1].startswith("login_"):
  nonce=payload[1][6:]
  valid=await pool.fetchval("UPDATE login_challenges SET telegram_id=$2 WHERE nonce=$1 AND expires_at>now() AND consumed_at IS NULL RETURNING nonce",nonce,m.from_user.id)
  if valid:
   await m.answer("✅ <b>Вход подтверждён</b>\n\nТвой аккаунт H1 RADAR готов. Вернись на страницу карты — доступ откроется автоматически.\n\nЕсли страница ещё открыта, ничего дополнительно нажимать не нужно.",reply_markup=app_kb(),parse_mode="HTML")
   return
  await m.answer("⌛ <b>Ссылка входа устарела</b>\n\nОткрой карту H1 RADAR и нажми «Войти через Telegram» ещё раз.",reply_markup=app_kb(),parse_mode="HTML")
  return
 greeting="Аккаунт создан автоматически." if fresh else "Рады снова видеть тебя."
 await m.answer(
  f"🛰 <b>H1 RADAR</b>\n<i>Гражданская карта и городские сообщества</i>\n\n{greeting}\n\n"
  f"Твой код участника: <code>{code}</code>\n\n"
  "Нажми кнопку ниже, чтобы открыть карту. При первом входе сайт подтвердит аккаунт через этот чат. "
  "После подтверждения карта, городская лента и чат станут доступны.\n\n"
  "<i>Сервис носит информационный характер и не заменяет официальные экстренные оповещения.</i>",
  reply_markup=app_kb(),parse_mode="HTML"
 )

@dp.message(Command("help"))
async def help_cmd(m:Message):
 await m.answer(
  "🛰 <b>Помощь H1 RADAR</b>\n\n"
  "<b>Как войти</b>\n"
  "1. Открой карту H1 RADAR.\n"
  "2. Нажми «Войти через Telegram».\n"
  "3. В открывшемся боте нажми Start.\n"
  "4. Вернись на сайт — карта откроется автоматически.\n\n"
  "<b>Команды</b>\n/start — регистрация и вход\n/help — эта справка\n\n"
  "Персональный код участника доступен в сообщении /start и в профиле на сайте.",
  reply_markup=app_kb(),parse_mode="HTML"
 )

async def expire_pending_reports():
 while True:
  try:
   if pool is not None:
    result=await pool.execute("DELETE FROM reports WHERE status='pending' AND created_at<now()-interval '5 minutes'")
    if result!="DELETE 0": log.info("Expired unconfirmed marks: %s",result)
    await pool.execute("DELETE FROM login_challenges WHERE expires_at<now() OR consumed_at IS NOT NULL")
    await pool.execute("DELETE FROM web_sessions WHERE expires_at<now()")
  except asyncio.CancelledError:
   raise
  except Exception:
   log.exception("Pending mark cleanup failed")
  await asyncio.sleep(20)

async def run_polling_forever():
 # aiogram retries transient getUpdates errors internally; this supervisor also recovers
 # from unexpected dispatcher exits instead of leaving the API alive with a dead bot.
 while True:
  try:
   await dp.start_polling(bot,handle_signals=False,polling_timeout=25)
   log.error("Telegram polling stopped unexpectedly; restarting in 5 seconds")
  except asyncio.CancelledError:
   raise
  except Exception:
   log.exception("Telegram polling crashed; restarting in 5 seconds")
  await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app:FastAPI):
 global pool,bot,polling_task,cleanup_task
 pool=await asyncpg.create_pool(DATABASE_URL,min_size=2,max_size=int(os.getenv("DB_POOL_SIZE","12")),command_timeout=15,ssl=False,statement_cache_size=0,max_inactive_connection_lifetime=300)
 async with pool.acquire() as c: await c.execute(SCHEMA)
 bot=Bot(BOT_TOKEN)
 try:
  global BOT_USERNAME
  if not BOT_USERNAME:
   me=await asyncio.wait_for(bot.get_me(),timeout=8)
   BOT_USERNAME=me.username or ""
 except asyncio.CancelledError:
  raise
 except Exception:
  log.exception("Could not resolve Telegram bot username during startup")
 polling_task=asyncio.create_task(run_polling_forever(),name="h1-telegram-polling")
 # Profile/avatar setup is non-critical and must not delay API readiness.
 profile_task=asyncio.create_task(configure_bot_profile(bot),name="h1-bot-profile-setup")
 cleanup_task=asyncio.create_task(expire_pending_reports(),name="h1-cleanup")
 try:
  yield
 finally:
  profile_task.cancel()
  await asyncio.gather(profile_task,return_exceptions=True)
  if cleanup_task:
   cleanup_task.cancel()
   await asyncio.gather(cleanup_task,return_exceptions=True)
  if polling_task:
   polling_task.cancel()
   await asyncio.gather(polling_task,return_exceptions=True)
  if bot:
   await bot.session.close()
  if pool:
   await pool.close()

app=FastAPI(title="H1 RADAR API",version="1.0.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[WEBAPP_URL,MAP_URL],allow_credentials=False,allow_methods=["GET","POST","PATCH","DELETE"],allow_headers=["Authorization","Content-Type"])
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
   (SELECT count(*) FROM reports WHERE status='pending' AND created_at>=now()-interval '5 minutes') AS pending_recent""")
  status=await c.fetchrow("""SELECT (SELECT count(*) FROM users WHERE last_seen_at>now()-interval '15 minutes') AS online,
   (SELECT max(created_at) FROM reports WHERE status='published') AS last_report""")
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
 global BOT_USERNAME
 if not BOT_USERNAME and bot is not None:
  try:
   me=await asyncio.wait_for(bot.get_me(),timeout=5)
   BOT_USERNAME=me.username or ""
  except asyncio.CancelledError:
   raise
  except Exception:
   log.exception("Could not refresh bot username for login link")
 if not BOT_USERNAME: raise HTTPException(503,"Бот временно недоступен. Попробуйте ещё раз через несколько секунд.")
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
 r=await current_user(authorization)
 stats=await pool.fetchrow("""SELECT
  (SELECT count(*) FROM reports WHERE author_id=$1) AS reports_total,
  (SELECT count(*) FROM reports WHERE author_id=$1 AND status='published') AS reports_published,
  (SELECT count(*) FROM report_confirmations WHERE telegram_id=$1) AS confirmations_given,
  (SELECT count(*) FROM city_chat_messages WHERE telegram_id=$1) AS chat_messages
 """,r["telegram_id"])
 return {"user":{**user_json(r),"permissions":await permissions_for(r)},"stats":{k:int(stats[k] or 0) for k in ("reports_total","reports_published","confirmations_given","chat_messages")}}

@app.get("/api/public/cities")
async def public_cities():
 rows=await pool.fetch("SELECT id,name,region FROM cities WHERE enabled=true ORDER BY name")
 return {"cities":[dict(r) for r in rows]}

@app.get("/api/cities")
async def cities(authorization:str|None=Header(default=None)):
 await current_user(authorization)
 rows=await pool.fetch("SELECT id,name,region,latitude,longitude FROM cities WHERE enabled=true ORDER BY name")
 return {"cities":[dict(r) for r in rows]}

@app.get("/api/feed")
async def feed(city:str|None=None,limit:int=50,hours:int=24,authorization:str|None=Header(default=None)):
 await current_user(authorization)
 limit=max(1,min(limit,100)); hours=max(1,min(hours,720))
 # Pending marks remain visible for up to five minutes while the community checks them.
 if city: rows=await pool.fetch("SELECT r.id,r.city,r.category,r.description,r.latitude,r.longitude,r.confirmations,r.created_at,r.status,u.public_code FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status IN ('published','pending') AND (r.status='published' OR r.created_at>=now()-interval '5 minutes') AND r.city=$1 AND r.created_at>=now()-make_interval(hours=>$2) ORDER BY r.created_at DESC LIMIT $3",city,hours,limit)
 else: rows=await pool.fetch("SELECT r.id,r.city,r.category,r.description,r.latitude,r.longitude,r.confirmations,r.created_at,r.status,u.public_code FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status IN ('published','pending') AND (r.status='published' OR r.created_at>=now()-interval '5 minutes') AND r.created_at>=now()-make_interval(hours=>$1) ORDER BY r.created_at DESC LIMIT $2",hours,limit)
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
 return {"ok":True,"id":row["id"],"status":"pending","message":"Отметка появилась на карте. Если в течение 5 минут её не подтвердят другие участники, она исчезнет автоматически."}

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

@app.get("/api/moderation/assignments")
async def moderator_assignments(authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Назначать команду может только владелец.")
 rows=await pool.fetch("""SELECT p.telegram_id,u.public_code,u.display_name,u.username,c.id AS city_id,c.name AS city,p.permission
 FROM moderator_city_permissions p JOIN users u ON u.telegram_id=p.telegram_id JOIN cities c ON c.id=p.city_id
 ORDER BY c.name,u.display_name,p.permission""")
 return {"items":[dict(r) for r in rows]}

@app.post("/api/moderation/assignments")
async def assign_city_moderator(body:dict,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Назначать модераторов может только владелец.")
 code=str(body.get("code","")).strip().upper()
 city=str(body.get("city","")).strip()
 requested=body.get("permissions")
 allowed=PERMISSIONS
 if not code or not city or not isinstance(requested,list) or not requested:
  raise HTTPException(400,"Укажи код жителя, город и хотя бы одно право.")
 if any(not isinstance(x,str) or x not in allowed for x in requested):
  raise HTTPException(400,"В списке есть неизвестное или недоступное право.")
 target=await pool.fetchrow("SELECT telegram_id,public_code,display_name FROM users WHERE upper(public_code)=$1",code)
 if not target: raise HTTPException(404,"Житель с таким номером не найден. Сначала найди его в разделе «Люди».")
 city_row=await pool.fetchrow("SELECT id,name FROM cities WHERE name=$1 AND enabled=true",city)
 if not city_row: raise HTTPException(404,"Город не найден или отключён.")
 try:
  async with pool.acquire() as c:
   async with c.transaction():
    await c.execute("UPDATE users SET role='moderator' WHERE telegram_id=$1",target["telegram_id"])
    for permission in sorted(set(requested)):
     await c.execute("INSERT INTO moderator_city_permissions(telegram_id,city_id,permission,granted_by) VALUES($1,$2,$3,$4) ON CONFLICT(telegram_id,city_id,permission) DO NOTHING",target["telegram_id"],city_row["id"],permission,actor["telegram_id"])
    await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'city_moderator_assigned',$2,$3)",actor["telegram_id"],target["telegram_id"],city_row["name"]+" · "+", ".join(sorted(set(requested))))
 except asyncpg.UniqueViolationError:
  raise HTTPException(409,"У этого города уже есть амбассадор. Сначала сними прежнее назначение.")
 if bot:
  try:
   await bot.send_message(target["telegram_id"],"🛰 Тебе выданы права команды H1 RADAR\nГород: "+city_row["name"]+"\nПрава: "+", ".join(sorted(set(requested)))+"\nОткрой панель команды: "+MAP_URL)
  except Exception: log.warning("Could not notify newly assigned moderator %s",target["telegram_id"],exc_info=True)
 return {"ok":True,"telegram_id":target["telegram_id"],"city":city_row["name"],"permissions":sorted(set(requested))}

@app.delete("/api/moderation/assignments/{telegram_id}/{city_id}/{permission}")
async def revoke_city_permission(telegram_id:int,city_id:int,permission:str,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Изменять права команды может только владелец.")
 if permission not in PERMISSIONS: raise HTTPException(400,"Неизвестное право.")
 async with pool.acquire() as c:
  async with c.transaction():
   result=await c.execute("DELETE FROM moderator_city_permissions WHERE telegram_id=$1 AND city_id=$2 AND permission=$3",telegram_id,city_id,permission)
   if result.endswith("0"): raise HTTPException(404,"Такое назначение не найдено.")
   left=await c.fetchval("SELECT count(*) FROM moderator_city_permissions WHERE telegram_id=$1",telegram_id)
   if not left: await c.execute("UPDATE users SET role='user' WHERE telegram_id=$1 AND telegram_id<>$2",telegram_id,BOT_OWNER)
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'city_permission_revoked',$2,$3)",actor["telegram_id"],telegram_id,permission)
 if bot:
  try: await bot.send_message(telegram_id,"ℹ️ Одно из прав команды H1 RADAR было снято.")
  except Exception: log.warning("Could not notify permission revocation",exc_info=True)
 return {"ok":True,"remaining_permissions":int(left or 0)}

@app.get("/api/moderation/summary")
async def mod_summary(authorization:str|None=Header(default=None)):
 actor=await moderator(authorization,"statistics")
 if actor["role"]=="owner":
  row=await pool.fetchrow("""SELECT
   (SELECT count(*) FROM reports WHERE status='pending') AS pending,
   (SELECT count(*) FROM users) AS users,
   (SELECT count(*) FROM reports WHERE status='published') AS published,
   (SELECT count(*) FROM users WHERE created_at>=date_trunc('day',now())) AS today_users,
   (SELECT count(*) FROM reports WHERE created_at>=date_trunc('day',now())) AS today_events,
   (SELECT count(*) FROM users WHERE last_seen_at>now()-interval '15 minutes') AS online""")
 else:
  row=await pool.fetchrow("""SELECT
   (SELECT count(*) FROM reports r WHERE r.status='pending' AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=r.city)) AS pending,
   (SELECT count(DISTINCT u.telegram_id) FROM users u WHERE EXISTS(SELECT 1 FROM reports r WHERE r.author_id=u.telegram_id AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=r.city)) OR EXISTS(SELECT 1 FROM city_chat_messages m WHERE m.telegram_id=u.telegram_id AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=m.city))) AS users,
   (SELECT count(*) FROM reports r WHERE r.status='published' AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=r.city)) AS published,
   (SELECT count(DISTINCT u.telegram_id) FROM users u WHERE u.created_at>=date_trunc('day',now()) AND (EXISTS(SELECT 1 FROM reports r WHERE r.author_id=u.telegram_id AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=r.city)) OR EXISTS(SELECT 1 FROM city_chat_messages m WHERE m.telegram_id=u.telegram_id AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=m.city)))) AS today_users,
   (SELECT count(*) FROM reports r WHERE r.created_at>=date_trunc('day',now()) AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=r.city)) AS today_events,
   (SELECT count(DISTINCT u.telegram_id) FROM users u WHERE u.last_seen_at>now()-interval '15 minutes' AND (EXISTS(SELECT 1 FROM reports r WHERE r.author_id=u.telegram_id AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=r.city)) OR EXISTS(SELECT 1 FROM city_chat_messages m WHERE m.telegram_id=u.telegram_id AND EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$1 AND p.permission='statistics' AND c.name=m.city)))) AS online""",actor["telegram_id"])
 return {key:int(row[key] or 0) for key in ("pending","users","published","today_users","today_events","online")}

@app.get("/api/moderation/reports")
async def mod_reports(authorization:str|None=Header(default=None)):
 await moderator(authorization,"events")
 rows=await pool.fetch("""SELECT r.id,r.city,r.category,r.description,r.created_at,u.public_code FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status='pending' AND ($1 OR EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$2 AND p.permission='events' AND c.name=r.city)) ORDER BY r.created_at LIMIT 100""", (await current_user(authorization))["role"]=="owner", (await current_user(authorization))["telegram_id"])
 return {"items":[{**feed_json(r),"author_code":r["public_code"]} for r in rows]}

@app.get("/api/moderation/events")
async def mod_events(authorization:str|None=Header(default=None),q:str="",hours:int=168):
 actor=await moderator(authorization,"events")
 hours=max(1,min(hours,720)); q=q.strip()
 rows=await pool.fetch("""SELECT r.id,r.city,r.category,r.description,r.latitude,r.longitude,r.confirmations,r.status,r.created_at,u.public_code,u.username,u.telegram_id
 FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id
 WHERE r.created_at>=now()-($1::text||' hours')::interval
 AND ($2='' OR r.city ILIKE $3 OR r.category ILIKE $3 OR r.description ILIKE $3 OR u.username ILIKE $3 OR u.public_code ILIKE $3)
 AND ($4 OR EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$5 AND p.permission='events' AND c.name=r.city))
 ORDER BY r.created_at DESC LIMIT 500""",str(hours),q,"%"+q+"%",actor["role"]=="owner",actor["telegram_id"])
 return {"items":[{**feed_json(r),"status":r["status"],"author_code":r["public_code"],"username":r["username"],"telegram_id":r["telegram_id"]} for r in rows]}

@app.get("/api/moderation/marks")
async def mod_marks(authorization:str|None=Header(default=None),city:str|None=None):
 actor=await moderator(authorization,"marks",city)
 rows=await pool.fetch("SELECT r.id,r.city,r.category,r.description,r.latitude,r.longitude,r.confirmations,r.status,r.created_at,u.public_code,u.username,u.telegram_id FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status='published' AND ($1::text IS NULL OR r.city=$1) AND ($2 OR EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$3 AND p.permission='marks' AND c.name=r.city)) ORDER BY r.created_at DESC LIMIT 500",city,actor["role"]=="owner",actor["telegram_id"])
 return {"items":[{**feed_json(r),"status":r["status"],"author_code":r["public_code"],"username":r["username"],"telegram_id":r["telegram_id"]} for r in rows]}

@app.post("/api/moderation/reports/{report_id}/{decision}")
async def review(report_id:int,decision:str,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization,"events")
 if decision not in ("publish","reject"): raise HTTPException(400,"Недопустимое решение.")
 status="published" if decision=="publish" else "rejected"
 city=await pool.fetchval("SELECT city FROM reports WHERE id=$1",report_id)
 if not city: raise HTTPException(404,"Сообщение не найдено.")
 await require_permission(actor,"events",city)
 async with pool.acquire() as c:
  async with c.transaction():
   result=await c.execute("UPDATE reports SET status=$1,moderator_id=$2,reviewed_at=now() WHERE id=$3 AND status='pending'",status,actor["telegram_id"],report_id)
   if result.endswith("0"): raise HTTPException(404,"Сообщение уже обработано или не найдено.")
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id) VALUES($1,$2,$3)",actor["telegram_id"],status,report_id)
 return {"ok":True,"status":status}

@app.get("/api/moderation/users")
async def mod_users(authorization:str|None=Header(default=None),q:str="",city:str|None=None):
 actor=await moderator(authorization,"people",city)
 if actor["role"]!="owner" and not city: raise HTTPException(400,"Выбери город, к которому выданы права.")
 q=q.strip()
 if q:
  rows=await pool.fetch("""SELECT u.telegram_id,u.username,u.display_name,u.public_code,u.role,u.created_at,u.last_seen_at,
   (ur.telegram_id IS NOT NULL AND (ur.expires_at IS NULL OR ur.expires_at>now())) AS restricted,
   ur.reason AS restriction_reason,ur.expires_at AS restriction_expires_at
   FROM users u LEFT JOIN user_restrictions ur ON ur.telegram_id=u.telegram_id
   WHERE (u.public_code ILIKE $1 OR u.username ILIKE $1 OR u.display_name ILIKE $1 OR u.telegram_id::text ILIKE $1)
   AND ($2 OR EXISTS(SELECT 1 FROM reports r WHERE r.author_id=u.telegram_id AND r.city=$3) OR EXISTS(SELECT 1 FROM city_chat_messages m WHERE m.telegram_id=u.telegram_id AND m.city=$3))
   ORDER BY u.created_at DESC LIMIT 500""","%"+q+"%",actor["role"]=="owner",city)
 else: rows=await pool.fetch("""SELECT u.telegram_id,u.username,u.display_name,u.public_code,u.role,u.created_at,u.last_seen_at,
   (ur.telegram_id IS NOT NULL AND (ur.expires_at IS NULL OR ur.expires_at>now())) AS restricted,
   ur.reason AS restriction_reason,ur.expires_at AS restriction_expires_at
   FROM users u LEFT JOIN user_restrictions ur ON ur.telegram_id=u.telegram_id
   WHERE ($1 OR EXISTS(SELECT 1 FROM reports r WHERE r.author_id=u.telegram_id AND r.city=$2) OR EXISTS(SELECT 1 FROM city_chat_messages m WHERE m.telegram_id=u.telegram_id AND m.city=$2))
   ORDER BY u.created_at DESC LIMIT 500""",actor["role"]=="owner",city)
 return {"items":[{"id":r["telegram_id"],"telegram_id":r["telegram_id"],"username":r["username"],"telegram_url":("https://t.me/"+r["username"]) if r["username"] else ("tg://user?id="+str(r["telegram_id"])),"name":r["display_name"],"code":r["public_code"],"role":r["role"],"created_at":r["created_at"].isoformat(),"last_seen_at":r["last_seen_at"].isoformat(),"online":r["last_seen_at"]>datetime.now(timezone.utc)-timedelta(minutes=15),"restricted":r["restricted"],"restriction_reason":r["restriction_reason"],"restriction_expires_at":r["restriction_expires_at"].isoformat() if r["restriction_expires_at"] else None} for r in rows]}

@app.post("/api/moderation/users/{telegram_id}/role/{role}")
async def set_role(telegram_id:int,role:str,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Назначать модераторов может только владелец.")
 if role!="user": raise HTTPException(400,"Для назначения модератора используй форму с выбором города и прав.")
 if telegram_id==BOT_OWNER: raise HTTPException(400,"Нельзя изменить роль владельца.")
 async with pool.acquire() as c:
  async with c.transaction():
   result=await c.execute("UPDATE users SET role=$1 WHERE telegram_id=$2",role,telegram_id)
   if result.endswith("0"): raise HTTPException(404,"Пользователь не найден.")
   await c.execute("DELETE FROM moderator_city_permissions WHERE telegram_id=$1",telegram_id)
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'role_change',$2,$3)",actor["telegram_id"],telegram_id,role)
 return {"ok":True,"role":role}

@app.get("/api/city-chat")
async def city_chat(city:str,after:int=0,authorization:str|None=Header(default=None)):
 await current_user(authorization)
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
 actor=await moderator(authorization,"support")
 async with pool.acquire() as c:
  async with c.transaction():
   row=await c.fetchrow("SELECT city,telegram_id,message FROM city_chat_messages WHERE id=$1 FOR UPDATE",message_id)
   if not row: raise HTTPException(404,"Сообщение уже удалено или не найдено.")
   await require_permission(actor,"support",row["city"])
   await c.execute("DELETE FROM city_chat_messages WHERE id=$1",message_id)
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'chat_message_deleted',$2,$3)",actor["telegram_id"],row["telegram_id"],"Город: "+row["city"]+" · сообщение #"+str(message_id))
 return {"ok":True}


@app.get("/api/moderation/cities")
async def moderation_cities(authorization:str|None=Header(default=None),q:str=""):
 await moderator(authorization)
 q=q.strip()
 rows=await pool.fetch("""SELECT c.id,c.name,c.region,c.latitude,c.longitude,c.enabled,
  (SELECT count(*) FROM reports r WHERE r.city=c.name) AS reports
  FROM cities c WHERE ($1='' OR c.name ILIKE $2 OR c.region ILIKE $2)
  ORDER BY c.enabled DESC,c.name LIMIT 1000""",q,"%"+q+"%")
 return {"items":[dict(r) for r in rows]}

@app.post("/api/moderation/cities")
async def moderation_city_create(body:dict,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Добавлять города может только владелец.")
 name=str(body.get("name","")).strip(); region=str(body.get("region","")).strip()
 lat=body.get("latitude"); lon=body.get("longitude")
 if not name or len(name)>100 or len(region)>120: raise HTTPException(400,"Проверь название и регион.")
 if (lat is None)!=(lon is None): raise HTTPException(400,"Координаты должны быть указаны вместе.")
 if lat is not None and not point_in_russia(float(lon),float(lat)): raise HTTPException(400,"Координаты должны находиться в России.")
 try:
  row=await pool.fetchrow("INSERT INTO cities(name,region,latitude,longitude) VALUES($1,$2,$3,$4) RETURNING id,name,region,latitude,longitude,enabled",name,region,float(lat) if lat is not None else None,float(lon) if lon is not None else None)
 except asyncpg.UniqueViolationError: raise HTTPException(409,"Такой город уже существует.")
 await pool.execute("INSERT INTO moderation_log(actor_id,action,details) VALUES($1,'city_created',$2)",actor["telegram_id"],name)
 return {"ok":True,"city":dict(row)}

@app.patch("/api/moderation/cities/{city_id}")
async def moderation_city_update(city_id:int,body:dict,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Изменять каталог может только владелец.")
 old=await pool.fetchrow("SELECT * FROM cities WHERE id=$1",city_id)
 if not old: raise HTTPException(404,"Город не найден.")
 name=str(body.get("name",old["name"])).strip(); region=str(body.get("region",old["region"])).strip()
 enabled=bool(body.get("enabled",old["enabled"]))
 lat=body.get("latitude",old["latitude"]); lon=body.get("longitude",old["longitude"])
 if not name or len(name)>100 or len(region)>120 or (lat is None)!=(lon is None): raise HTTPException(400,"Проверь поля города.")
 if lat is not None and not point_in_russia(float(lon),float(lat)): raise HTTPException(400,"Координаты должны находиться в России.")
 try:
  row=await pool.fetchrow("UPDATE cities SET name=$1,region=$2,enabled=$3,latitude=$4,longitude=$5 WHERE id=$6 RETURNING id,name,region,latitude,longitude,enabled",name,region,enabled,float(lat) if lat is not None else None,float(lon) if lon is not None else None,city_id)
 except asyncpg.UniqueViolationError: raise HTTPException(409,"Такой город уже существует.")
 await pool.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'city_updated',$2,$3)",actor["telegram_id"],city_id,name)
 return {"ok":True,"city":dict(row)}

@app.get("/api/moderation/chat")
async def moderation_city_chat(authorization:str|None=Header(default=None),city:str|None=None,q:str=""):
 actor=await moderator(authorization,"support",city)
 q=q.strip()
 rows=await pool.fetch("""SELECT m.id,m.city,m.telegram_id,m.username,m.display_name,m.message,m.created_at
 FROM city_chat_messages m WHERE ($1::text IS NULL OR m.city=$1)
 AND ($2='' OR m.message ILIKE $3 OR m.username ILIKE $3 OR m.display_name ILIKE $3)
 AND ($4 OR EXISTS(SELECT 1 FROM moderator_city_permissions p JOIN cities c ON c.id=p.city_id WHERE p.telegram_id=$5 AND p.permission='support' AND c.name=m.city))
 ORDER BY m.created_at DESC LIMIT 500""",city,q,"%"+q+"%",actor["role"]=="owner",actor["telegram_id"])
 return {"items":[{"id":r["id"],"city":r["city"],"telegram_id":r["telegram_id"],"username":r["username"],"name":r["display_name"],"message":r["message"],"created_at":r["created_at"].isoformat()} for r in rows]}

@app.post("/api/moderation/users/{telegram_id}/restriction")
async def moderation_user_restriction(telegram_id:int,body:dict,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Ограничивать аккаунты может только владелец.")
 if telegram_id==BOT_OWNER: raise HTTPException(400,"Нельзя ограничить владельца.")
 reason=str(body.get("reason","")).strip()[:500]; hours=body.get("hours")
 if hours is not None:
  try: hours=int(hours)
  except (TypeError,ValueError): raise HTTPException(400,"Срок должен быть числом часов.")
  if not 1<=hours<=8760: raise HTTPException(400,"Срок должен быть от 1 до 8760 часов.")
 if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE telegram_id=$1)",telegram_id): raise HTTPException(404,"Пользователь не найден.")
 await pool.execute("""INSERT INTO user_restrictions(telegram_id,reason,expires_at,created_by)
 VALUES($1,$2,CASE WHEN $3::int IS NULL THEN NULL ELSE now()+make_interval(hours=>$3) END,$4)
 ON CONFLICT(telegram_id) DO UPDATE SET reason=EXCLUDED.reason,expires_at=EXCLUDED.expires_at,created_by=EXCLUDED.created_by,created_at=now()""",telegram_id,reason,hours,actor["telegram_id"])
 await pool.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,'user_restricted',$2,$3)",actor["telegram_id"],telegram_id,reason)
 return {"ok":True,"restricted":True,"hours":hours}

@app.delete("/api/moderation/users/{telegram_id}/restriction")
async def moderation_user_unrestrict(telegram_id:int,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization)
 if actor["role"]!="owner": raise HTTPException(403,"Снимать ограничения может только владелец.")
 result=await pool.execute("DELETE FROM user_restrictions WHERE telegram_id=$1",telegram_id)
 if result.endswith("0"): raise HTTPException(404,"Активное ограничение не найдено.")
 await pool.execute("INSERT INTO moderation_log(actor_id,action,target_id) VALUES($1,'user_unrestricted',$2)",actor["telegram_id"],telegram_id)
 return {"ok":True,"restricted":False}

@app.get("/api/moderation/city-requests")
async def moderation_city_requests(authorization:str|None=Header(default=None),status:str="pending"):
 await moderator(authorization,"beta")
 if status not in ("pending","approved","rejected","all"): raise HTTPException(400,"Недопустимый статус.")
 rows=await pool.fetch("""SELECT cr.*,u.public_code,u.username FROM city_requests cr LEFT JOIN users u ON u.telegram_id=cr.telegram_id
 WHERE ($1='all' OR cr.status=$1) ORDER BY cr.created_at DESC LIMIT 500""",status)
 return {"items":[{"id":r["id"],"name":r["city_name"],"region":r["region"],"latitude":r["latitude"],"longitude":r["longitude"],"status":r["status"],"created_at":r["created_at"].isoformat(),"user_code":r["public_code"],"username":r["username"]} for r in rows]}

@app.post("/api/city-requests")
async def submit_city_request(body:dict,authorization:str|None=Header(default=None)):
 u=await current_user(authorization)
 name=str(body.get("name","")).strip(); region=str(body.get("region","")).strip()
 lat=body.get("latitude"); lon=body.get("longitude")
 if not name or len(name)>100 or len(region)>120 or (lat is None)!=(lon is None): raise HTTPException(400,"Укажи название города, регион и корректные координаты.")
 if lat is not None and not point_in_russia(float(lon),float(lat)): raise HTTPException(400,"Город должен находиться в России.")
 if await pool.fetchval("SELECT EXISTS(SELECT 1 FROM cities WHERE lower(name)=lower($1))",name): raise HTTPException(409,"Этот город уже есть в каталоге.")
 if await pool.fetchval("SELECT count(*) FROM city_requests WHERE telegram_id=$1 AND created_at>now()-interval '24 hours'",u["telegram_id"])>=3: raise HTTPException(429,"Не более трёх заявок за сутки.")
 row=await pool.fetchrow("INSERT INTO city_requests(telegram_id,city_name,region,latitude,longitude) VALUES($1,$2,$3,$4,$5) RETURNING id",u["telegram_id"],name,region,float(lat) if lat is not None else None,float(lon) if lon is not None else None)
 return {"ok":True,"id":row["id"],"status":"pending"}

@app.post("/api/moderation/city-requests/{request_id}/{decision}")
async def moderation_city_request_decide(request_id:int,decision:str,authorization:str|None=Header(default=None)):
 actor=await moderator(authorization,"beta")
 if decision not in ("approve","reject"): raise HTTPException(400,"Недопустимое решение.")
 async with pool.acquire() as c:
  async with c.transaction():
   req=await c.fetchrow("SELECT * FROM city_requests WHERE id=$1 AND status='pending' FOR UPDATE",request_id)
   if not req: raise HTTPException(404,"Заявка уже обработана или не найдена.")
   if decision=="approve":
    await c.execute("INSERT INTO cities(name,region,latitude,longitude) VALUES($1,$2,$3,$4) ON CONFLICT(name) DO UPDATE SET region=EXCLUDED.region,latitude=COALESCE(EXCLUDED.latitude,cities.latitude),longitude=COALESCE(EXCLUDED.longitude,cities.longitude),enabled=true",req["city_name"],req["region"],req["latitude"],req["longitude"])
   status="approved" if decision=="approve" else "rejected"
   await c.execute("UPDATE city_requests SET status=$1,moderator_id=$2,reviewed_at=now() WHERE id=$3",status,actor["telegram_id"],request_id)
   await c.execute("INSERT INTO moderation_log(actor_id,action,target_id,details) VALUES($1,$2,$3,$4)",actor["telegram_id"],"city_request_"+status,request_id,req["city_name"])
 return {"ok":True,"status":status}

@app.get("/api/moderation/log")
async def mod_log(authorization:str|None=Header(default=None)):
 await moderator(authorization,"statistics")
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
