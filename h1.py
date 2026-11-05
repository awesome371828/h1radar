from __future__ import annotations
import asyncio, hashlib, hmac, json, logging, os, re, secrets, string, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("h1")
BOT_TOKEN=os.environ["BOT_TOKEN"]
BOT_OWNER=int(os.environ["BOT_OWNER"])
WEBAPP_URL=os.environ["WEBAPP_URL"].rstrip("/")
DATABASE_URL=os.environ["DATABASE_URL"]
BOT_USERNAME=os.getenv("BOT_USERNAME","").lstrip("@")
INITDATA_MAX_AGE=int(os.getenv("TG_INITDATA_MAX_AGE","86400"))
pool: asyncpg.Pool|None=None
bot: Bot|None=None
dp=Dispatcher()
polling_task: asyncio.Task|None=None

SCHEMA="""
CREATE TABLE IF NOT EXISTS users(
 telegram_id BIGINT PRIMARY KEY, username TEXT, display_name TEXT NOT NULL,
 phone TEXT NOT NULL, public_code TEXT UNIQUE NOT NULL,
 role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','moderator','owner')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reports(
 id BIGSERIAL PRIMARY KEY, author_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 city TEXT NOT NULL, category TEXT NOT NULL CHECK(category IN ('alert','all_clear','shelter','info')),
 description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','published','rejected')),
 moderator_id BIGINT REFERENCES users(telegram_id) ON DELETE SET NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reports_feed_idx ON reports(status,created_at DESC);
CREATE TABLE IF NOT EXISTS moderation_log(
 id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL, action TEXT NOT NULL,
 target_id BIGINT, details TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cities(id BIGSERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,region TEXT NOT NULL DEFAULT '',enabled BOOLEAN NOT NULL DEFAULT true);
INSERT INTO cities(name,region) VALUES('Москва','Москва'),('Санкт-Петербург','Санкт-Петербург'),
('Ростов-на-Дону','Ростовская область'),('Батайск','Ростовская область')
ON CONFLICT(name) DO NOTHING;
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
 if not auth or not auth.startswith("tma "): raise HTTPException(401,"Откройте приложение через Telegram.")
 try: tg=verify_init_data(auth[4:])
 except Exception: raise HTTPException(401,"Не удалось проверить Telegram-сессию. Перезапустите приложение.")
 row=await pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1",int(tg["id"]))
 if not row: raise HTTPException(403,"Сначала подтвердите номер телефона в боте командой /start.")
 await pool.execute("UPDATE users SET last_seen_at=now(),username=$2 WHERE telegram_id=$1",int(tg["id"]),tg.get("username"))
 return row

async def moderator(auth:str|None):
 row=await current_user(auth)
 if row["role"] not in ("owner","moderator"): raise HTTPException(403,"Недостаточно прав.")
 return row

def user_json(r):
 return {"id":r["telegram_id"],"username":r["username"],"name":r["display_name"],
 "code":r["public_code"],"role":r["role"],"created_at":r["created_at"].isoformat()}

def feed_json(r):
 return {"id":r["id"],"city":r["city"],"category":r["category"],
 "description":r["description"],"created_at":r["created_at"].isoformat()}

def app_kb():
 return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть H1 RADAR",web_app=WebAppInfo(url=WEBAPP_URL))]])

@dp.message(CommandStart())
async def start(m:Message):
 row=await pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1",m.from_user.id)
 if row:
  await m.answer(f"С возвращением! Ваш код H1: <b>{row['public_code']}</b>",reply_markup=app_kb(),parse_mode="HTML"); return
 kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Подтвердить номер телефона",request_contact=True)]],
  resize_keyboard=True,one_time_keyboard=True)
 await m.answer("Добро пожаловать в <b>H1 RADAR</b>.\n\nПодтвердите собственный российский номер кнопкой ниже. Принимаются только номера +7. Контакт используется для защиты от автоматических регистраций.",reply_markup=kb,parse_mode="HTML")

@dp.message(F.contact)
async def contact(m:Message):
 if not m.from_user or not m.contact or m.contact.user_id!=m.from_user.id:
  await m.answer("Отправьте собственный контакт через кнопку подтверждения.",reply_markup=ReplyKeyboardRemove()); return
 phone=ru_phone(m.contact.phone_number)
 if not phone:
  await m.answer("Нужен российский номер +7XXXXXXXXXX.",reply_markup=ReplyKeyboardRemove()); return
 u=m.from_user; name=" ".join(x for x in [u.first_name,u.last_name] if x) or "Пользователь"
 role="owner" if u.id==BOT_OWNER else "user"
 existing=await pool.fetchrow("SELECT public_code FROM users WHERE telegram_id=$1",u.id)
 if existing:
  code=existing["public_code"]
  if u.id==BOT_OWNER:
   await pool.execute("UPDATE users SET role='owner',phone=$2,username=$3,display_name=$4 WHERE telegram_id=$1",u.id,phone,u.username,name)
 else:
  code=None
  for _ in range(30):
   candidate="RU-"+ "".join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(4))
   try:
    await pool.execute("INSERT INTO users(telegram_id,username,display_name,phone,public_code,role) VALUES($1,$2,$3,$4,$5,$6)",u.id,u.username,name,phone,candidate,role)
    code=candidate; break
   except asyncpg.UniqueViolationError: continue
  if not code: raise RuntimeError("code allocation failed")
 await m.answer(f"Номер подтверждён. Ваш код: <b>{code}</b>",reply_markup=ReplyKeyboardRemove(),parse_mode="HTML")
 await m.answer("H1 RADAR готов к работе.",reply_markup=app_kb())
 if role=="owner": await m.answer("Вам доступна панель владельца.")

@dp.message(Command("radar"))
async def radar(m:Message): await m.answer("Открыть карту H1 RADAR:",reply_markup=app_kb())

@dp.message(Command("moderation"))
async def moderation(m:Message):
 row=await pool.fetchrow("SELECT role FROM users WHERE telegram_id=$1",m.from_user.id)
 if not row or row["role"] not in ("owner","moderator"):
  await m.answer("Панель доступна только владельцу и назначенным модераторам."); return
 kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть модерацию",web_app=WebAppInfo(url=WEBAPP_URL+"?panel=moderation"))]])
 await m.answer("Панель управления H1 RADAR:",reply_markup=kb)

@dp.message(Command("help"))
async def help_cmd(m:Message):
 await m.answer("/start — регистрация\n/radar — открыть карту\n/moderation — панель модерации (для команды)")

@asynccontextmanager
async def lifespan(app:FastAPI):
 global pool,bot,polling_task
 pool=await asyncpg.create_pool(DATABASE_URL,min_size=1,max_size=int(os.getenv("DB_POOL_SIZE","10")),command_timeout=8)
 async with pool.acquire() as c: await c.execute(SCHEMA)
 bot=Bot(BOT_TOKEN)
 polling_task=asyncio.create_task(dp.start_polling(bot,handle_signals=False))
 yield
 if polling_task: polling_task.cancel()
 if bot: await bot.session.close()
 if pool: await pool.close()

app=FastAPI(title="H1 RADAR API",version="1.0.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[WEBAPP_URL],allow_credentials=False,allow_methods=["GET","POST"],allow_headers=["Authorization","Content-Type"])
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")


class ReportIn(BaseModel):
 city:str=Field(min_length=2,max_length=80)
 category:str=Field(pattern="^(alert|all_clear|shelter|info)$")
 description:str=Field(default="",max_length=500)

@app.get("/api/health")
async def health(): return {"ok":True,"service":"h1-radar","time":datetime.now(timezone.utc).isoformat()}

@app.get("/api/me")
async def me(authorization:str|None=Header(default=None)):
 r=await current_user(authorization); return {"user":user_json(r)}

@app.get("/api/cities")
async def cities():
 rows=await pool.fetch("SELECT id,name,region FROM cities WHERE enabled=true ORDER BY name")
 return {"cities":[dict(r) for r in rows]}

@app.get("/api/feed")
async def feed(city:str|None=None,limit:int=50):
 limit=max(1,min(limit,100))
 if city: rows=await pool.fetch("SELECT id,city,category,description,created_at FROM reports WHERE status='published' AND city=$1 ORDER BY created_at DESC LIMIT $2",city,limit)
 else: rows=await pool.fetch("SELECT id,city,category,description,created_at FROM reports WHERE status='published' ORDER BY created_at DESC LIMIT $1",limit)
 return {"items":[feed_json(r) for r in rows]}

@app.post("/api/reports")
async def create_report(body:ReportIn,authorization:str|None=Header(default=None)):
 u=await current_user(authorization)
 city=body.city.strip(); desc=body.description.strip()
 if not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM cities WHERE name=$1 AND enabled=true)",city):
  raise HTTPException(400,"Выберите город из списка.")
 r=await pool.fetchrow("INSERT INTO reports(author_id,city,category,description) VALUES($1,$2,$3,$4) RETURNING id",u["telegram_id"],city,body.category,desc)
 return {"ok":True,"id":r["id"],"status":"pending","message":"Сообщение отправлено на проверку."}

@app.get("/api/moderation/summary")
async def mod_summary(authorization:str|None=Header(default=None)):
 await moderator(authorization)
 return {"pending":await pool.fetchval("SELECT count(*) FROM reports WHERE status='pending'"),
 "users":await pool.fetchval("SELECT count(*) FROM users"),
 "published":await pool.fetchval("SELECT count(*) FROM reports WHERE status='published'" )}

@app.get("/api/moderation/reports")
async def mod_reports(authorization:str|None=Header(default=None)):
 await moderator(authorization)
 rows=await pool.fetch("SELECT r.id,r.city,r.category,r.description,r.created_at,u.public_code FROM reports r LEFT JOIN users u ON u.telegram_id=r.author_id WHERE r.status='pending' ORDER BY r.created_at LIMIT 100")
 return {"items":[{**feed_json(r),"author_code":r["public_code"]} for r in rows]}

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
  rows=await pool.fetch("SELECT telegram_id,username,display_name,public_code,role,created_at FROM users WHERE public_code ILIKE $1 OR username ILIKE $1 OR display_name ILIKE $1 ORDER BY created_at DESC LIMIT 50","%"+q+"%")
 else: rows=await pool.fetch("SELECT telegram_id,username,display_name,public_code,role,created_at FROM users ORDER BY created_at DESC LIMIT 50")
 return {"items":[{"id":r["telegram_id"],"username":r["username"],"name":r["display_name"],"code":r["public_code"],"role":r["role"],"created_at":r["created_at"].isoformat()} for r in rows]}

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

@app.get("/api/moderation/log")
async def mod_log(authorization:str|None=Header(default=None)):
 await moderator(authorization)
 rows=await pool.fetch("SELECT actor_id,action,target_id,details,created_at FROM moderation_log ORDER BY created_at DESC LIMIT 100")
 return {"items":[{"actor":r["actor_id"],"action":r["action"],"target":r["target_id"],"details":r["details"],"created_at":r["created_at"].isoformat()} for r in rows]}
