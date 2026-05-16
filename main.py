# ============================================================
# АНОНИМНАЯ ДОСКА СЕКРЕТОВ — main.py
# ============================================================

import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import parse_qsl, unquote

import aiosqlite
import aiofiles
from aiogram import Bot, Dispatcher, types
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000")
DB_PATH    = "secrets.db"

MAX_POST_LEN    = 10_000   # символов в посте
MAX_COMMENT_LEN = 2_000    # символов в комментарии

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ╔══════════════════════════════════════════════════════════╗
# ║               КЭШЛОЕР В ПАМЯТИ (TTL-cache)             ║
# ║  Снижает нагрузку на БД при высоком трафике.            ║
# ║  Лента обновляется раз в 5 секунд — этого достаточно.  ║
# ╚══════════════════════════════════════════════════════════╝

_cache: dict[str, dict] = {}

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.monotonic() < entry["exp"]:
        return entry["val"]
    return None

def cache_set(key: str, val, ttl: float = 5.0):
    _cache[key] = {"val": val, "exp": time.monotonic() + ttl}

def cache_bust(prefix: str):
    """Удалить все ключи начинающиеся с prefix."""
    for k in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(k, None)


# ╔══════════════════════════════════════════════════════════╗
# ║                      БАЗА ДАННЫХ                        ║
# ╚══════════════════════════════════════════════════════════╝

async def _apply_pragmas(db: aiosqlite.Connection):
    """Применяем настройки производительности к каждому соединению."""
    await db.execute("PRAGMA journal_mode = WAL")        # конкурентные чтения
    await db.execute("PRAGMA synchronous  = NORMAL")     # быстро + безопасно с WAL
    await db.execute("PRAGMA busy_timeout = 5000")       # ждём до 5 с при блокировке
    await db.execute("PRAGMA cache_size   = -32000")     # 32 МБ page-кэша
    await db.execute("PRAGMA temp_store   = MEMORY")     # temp-таблицы в RAM


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY,
                tg_id      INTEGER UNIQUE NOT NULL,
                created_at REAL DEFAULT (unixepoch())
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                author_tg  INTEGER NOT NULL,
                content    TEXT    NOT NULL,
                created_at REAL DEFAULT (unixepoch())
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_tg INTEGER NOT NULL,
                vote    INTEGER NOT NULL,           -- 1 = лайк, -1 = дизлайк
                UNIQUE(post_id, user_tg)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                author_tg  INTEGER NOT NULL,
                content    TEXT    NOT NULL,
                created_at REAL DEFAULT (unixepoch())
            )
        """)

        # ── Индексы для быстрых выборок ──────────────────────
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_id_desc  ON posts(id DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_author    ON posts(author_tg, id DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_votes_post      ON votes(post_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_votes_user_post ON votes(user_tg, post_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id, created_at)")

        await db.commit()
    logger.info("База данных инициализирована ✅")


# ╔══════════════════════════════════════════════════════════╗
# ║               ПРОВЕРКА TELEGRAM initData                ║
# ╚══════════════════════════════════════════════════════════╝

def verify_telegram_init_data(init_data: str) -> dict | None:
    try:
        parsed        = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key        = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash     = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        if time.time() - int(parsed.get("auth_date", 0)) > 86400:
            return None

        return json.loads(unquote(parsed.get("user", "{}")))
    except Exception as e:
        logger.error(f"Ошибка верификации: {e}")
        return None


async def get_or_create_user(tg_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)
        await db.execute("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,))
        await db.commit()
    return tg_id


# ╔══════════════════════════════════════════════════════════╗
# ║             ХЭНДЛЕРЫ БОТА (Aiogram)                     ║
# ╚══════════════════════════════════════════════════════════╝

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔮 Открыть секреты", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await message.answer(
        "👋 Добро пожаловать в *Анонимную доску секретов*!\n\n"
        "Здесь все анонимны. Делись мыслями — никто не узнает 🤫",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ╔══════════════════════════════════════════════════════════╗
# ║              LIFESPAN — запуск и остановка              ║
# ╚══════════════════════════════════════════════════════════╝

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Webhook безопасен при любом кол-ве воркеров Gunicorn.
    # Polling (start_polling) нельзя использовать с WORKERS > 1:
    # каждый воркер открывает своё соединение → Telegram возвращает
    # 409 Conflict, polling перезапускается с drop_pending_updates=True
    # и дропает /start-команды → пользователи не запускают бота →
    # bot.send_message() падает с 403 Forbidden → уведомления не приходят.
    webhook_url = f"{WEBAPP_URL}/webhook"
    # При WORKERS > 1 все воркеры стартуют одновременно и одновременно
    # вызывают set_webhook. Telegram разрешает один вызов раз в несколько
    # секунд и отвечает 429 тем, кто опоздал. Это нормально — webhook
    # уже установлен первым воркером, остальные просто пропускают.
    try:
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info(f"Webhook установлен: {webhook_url} ✅")
    except TelegramRetryAfter:
        logger.info("Webhook уже установлен другим воркером — пропускаем ✅")
    yield
    try:
        await bot.delete_webhook()
    except Exception:
        pass  # другие воркеры уже удалили webhook
    await bot.session.close()
    logger.info("Бот остановлен")


# ╔══════════════════════════════════════════════════════════╗
# ║                    FASTAPI ПРИЛОЖЕНИЕ                   ║
# ╚══════════════════════════════════════════════════════════╝

app = FastAPI(lifespan=lifespan, title="Secrets Board API")


# ──────────────────────────────────────────────────────────
#  WEBHOOK — приём обновлений от Telegram
# ──────────────────────────────────────────────────────────

@app.post("/webhook")
async def handle_webhook(request: Request):
    """Telegram шлёт сюда все обновления (команды, сообщения и т.д.)."""
    data   = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return {"ok": True}


async def auth_user(x_init_data: str = Header(...)) -> int:
    user = verify_telegram_init_data(x_init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Неверные данные авторизации")
    tg_id = user.get("id")
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не найден user.id")
    await get_or_create_user(tg_id)
    return tg_id


# ── Pydantic-схемы ───────────────────────────────────────────
class PostCreate(BaseModel):
    content: str

class CommentCreate(BaseModel):
    content: str

class VoteCreate(BaseModel):
    vote: int   # 1 = лайк, -1 = дизлайк


# ── Общий SQL для поста с агрегатами ─────────────────────────
_POST_SELECT = """
    SELECT
        p.id,
        p.content,
        p.created_at,
        COALESCE(SUM(CASE WHEN v.vote =  1 THEN 1 ELSE 0 END), 0) AS likes,
        COALESCE(SUM(CASE WHEN v.vote = -1 THEN 1 ELSE 0 END), 0) AS dislikes,
        (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comments_count,
        {my_vote_expr} AS my_vote
    FROM posts p
    LEFT JOIN votes v ON v.post_id = p.id
"""


# ──────────────────────────────────────────────────────────
#  Фронтенд
# ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    async with aiofiles.open("index.html", "r", encoding="utf-8") as f:
        return await f.read()


# ──────────────────────────────────────────────────────────
#  API: ПОСТЫ (cursor-based пагинация)
# ──────────────────────────────────────────────────────────

@app.get("/api/posts")
async def get_posts(
    before_id: Optional[int] = Query(None, description="Показать посты до этого id"),
    limit: int                = Query(20, ge=1, le=50),
    x_init_data: str          = Header(...),
):
    """Лента постов с cursor-based пагинацией. Кэш 5 сек."""
    tg_id = await auth_user(x_init_data)

    cache_key = f"feed:{tg_id}:{before_id}:{limit}"
    if (cached := cache_get(cache_key)) is not None:
        return JSONResponse(cached)

    my_vote_expr = f"(SELECT vote FROM votes WHERE post_id = p.id AND user_tg = {tg_id})"
    sql = _POST_SELECT.format(my_vote_expr=my_vote_expr)

    if before_id:
        sql += " WHERE p.id < :before_id GROUP BY p.id ORDER BY p.id DESC LIMIT :limit"
        params = {"before_id": before_id, "limit": limit}
    else:
        sql += " GROUP BY p.id ORDER BY p.id DESC LIMIT :limit"
        params = {"limit": limit}

    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows   = await cursor.fetchall()

    posts = [dict(r) for r in rows]
    cache_set(cache_key, posts, ttl=5)
    return JSONResponse(posts)


@app.post("/api/posts")
async def create_post(body: PostCreate, x_init_data: str = Header(...)):
    tg_id   = await auth_user(x_init_data)
    content = body.content.strip()

    if not content:
        raise HTTPException(400, "Текст не может быть пустым")
    if len(content) > MAX_POST_LEN:
        raise HTTPException(400, f"Максимум {MAX_POST_LEN} символов")

    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)
        cursor  = await db.execute(
            "INSERT INTO posts (author_tg, content) VALUES (?, ?)", (tg_id, content)
        )
        await db.commit()
        post_id = cursor.lastrowid

    cache_bust("feed:")   # сбрасываем кэш ленты
    return JSONResponse({"id": post_id, "content": content})


# ──────────────────────────────────────────────────────────
#  API: ПРОФИЛЬ (мои посты)
# ──────────────────────────────────────────────────────────

@app.get("/api/profile")
async def get_profile(x_init_data: str = Header(...)):
    """Возвращает посты текущего пользователя + сводную статистику."""
    tg_id = await auth_user(x_init_data)

    cache_key = f"profile:{tg_id}"
    if (cached := cache_get(cache_key)) is not None:
        return JSONResponse(cached)

    sql = (
        _POST_SELECT.format(my_vote_expr="NULL")
        + " WHERE p.author_tg = :tg_id GROUP BY p.id ORDER BY p.id DESC"
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, {"tg_id": tg_id})
        rows   = await cursor.fetchall()

    posts = [dict(r) for r in rows]
    stats = {
        "total_posts":    len(posts),
        "total_likes":    sum(p["likes"]          for p in posts),
        "total_dislikes": sum(p["dislikes"]        for p in posts),
        "total_comments": sum(p["comments_count"]  for p in posts),
    }

    result = {"posts": posts, "stats": stats}
    cache_set(cache_key, result, ttl=10)
    return JSONResponse(result)


# ──────────────────────────────────────────────────────────
#  API: ГОЛОСОВАНИЕ
# ──────────────────────────────────────────────────────────

@app.post("/api/posts/{post_id}/vote")
async def vote_post(post_id: int, body: VoteCreate, x_init_data: str = Header(...)):
    tg_id = await auth_user(x_init_data)

    if body.vote not in (1, -1):
        raise HTTPException(400, "vote должен быть 1 или -1")

    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row

        # Существующий голос
        cur      = await db.execute(
            "SELECT vote FROM votes WHERE post_id = ? AND user_tg = ?", (post_id, tg_id)
        )
        existing = await cur.fetchone()

        was_new_like = False   # нужно ли уведомить автора о лайке

        if existing:
            if existing["vote"] == body.vote:
                await db.execute(
                    "DELETE FROM votes WHERE post_id = ? AND user_tg = ?", (post_id, tg_id)
                )
            else:
                await db.execute(
                    "UPDATE votes SET vote = ? WHERE post_id = ? AND user_tg = ?",
                    (body.vote, post_id, tg_id),
                )
                was_new_like = (body.vote == 1)
        else:
            await db.execute(
                "INSERT INTO votes (post_id, user_tg, vote) VALUES (?, ?, ?)",
                (post_id, tg_id, body.vote),
            )
            was_new_like = (body.vote == 1)

        await db.commit()

        # Обновлённые счётчики
        cur = await db.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN vote =  1 THEN 1 ELSE 0 END), 0) AS likes,
                COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) AS dislikes,
                (SELECT vote FROM votes WHERE post_id = ? AND user_tg = ?)  AS my_vote
            FROM votes WHERE post_id = ?
        """, (post_id, tg_id, post_id))
        row = await cur.fetchone()

        # Достаём автора поста для уведомления
        if was_new_like:
            cur2      = await db.execute("SELECT author_tg, content FROM posts WHERE id = ?", (post_id,))
            post_row  = await cur2.fetchone()
        else:
            post_row = None

    # Сбрасываем кэш после голосования
    cache_bust(f"feed:")
    cache_bust(f"profile:")

    # ── Уведомление о лайке ──────────────────────────────────
    if was_new_like and post_row and post_row["author_tg"] != tg_id:
        try:
            preview = post_row["content"][:80] + ("…" if len(post_row["content"]) > 80 else "")
            await bot.send_message(
                chat_id=post_row["author_tg"],
                text=f"❤️ *Кто-то лайкнул ваш секрет:*\n\n_{preview}_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"[УВЕДОМЛЕНИЕ] Не удалось отправить лайк пользователю {post_row['author_tg']}: {e}")

    return JSONResponse({
        "likes":    row["likes"],
        "dislikes": row["dislikes"],
        "my_vote":  row["my_vote"],
    })


# ──────────────────────────────────────────────────────────
#  API: КОММЕНТАРИИ
# ──────────────────────────────────────────────────────────

@app.get("/api/posts/{post_id}/comments")
async def get_comments(post_id: int, x_init_data: str = Header(...)):
    await auth_user(x_init_data)

    cache_key = f"comments:{post_id}"
    if (cached := cache_get(cache_key)) is not None:
        return JSONResponse(cached)

    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)
        db.row_factory = aiosqlite.Row
        cur  = await db.execute(
            "SELECT id, content, created_at FROM comments WHERE post_id = ? ORDER BY created_at ASC",
            (post_id,),
        )
        rows = await cur.fetchall()

    result = [dict(r) for r in rows]
    cache_set(cache_key, result, ttl=5)
    return JSONResponse(result)


@app.post("/api/posts/{post_id}/comments")
async def add_comment(post_id: int, body: CommentCreate, x_init_data: str = Header(...)):
    tg_id   = await auth_user(x_init_data)
    content = body.content.strip()

    if not content:
        raise HTTPException(400, "Комментарий не может быть пустым")
    if len(content) > MAX_COMMENT_LEN:
        raise HTTPException(400, f"Максимум {MAX_COMMENT_LEN} символов")

    async with aiosqlite.connect(DB_PATH) as db:
        await _apply_pragmas(db)

        cur  = await db.execute("SELECT author_tg FROM posts WHERE id = ?", (post_id,))
        post = await cur.fetchone()
        if not post:
            raise HTTPException(404, "Пост не найден")

        author_tg_id = post[0]

        cur = await db.execute(
            "INSERT INTO comments (post_id, author_tg, content) VALUES (?, ?, ?)",
            (post_id, tg_id, content),
        )
        await db.commit()
        comment_id = cur.lastrowid

    cache_bust(f"comments:{post_id}")
    cache_bust("feed:")
    cache_bust("profile:")

    # ── Уведомление о комментарии ────────────────────────────
    if author_tg_id != tg_id:
        try:
            preview = content[:100] + ("…" if len(content) > 100 else "")
            await bot.send_message(
                chat_id=author_tg_id,
                text=f"💬 *Кто-то прокомментировал ваш секрет:*\n\n_{preview}_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"[УВЕДОМЛЕНИЕ] Не удалось отправить комментарий пользователю {author_tg_id}: {e}")

    return JSONResponse({"id": comment_id, "content": content})


# ──────────────────────────────────────────────────────────
#  Точка входа
# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    workers = int(os.getenv("WORKERS", 1))
    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=workers)
