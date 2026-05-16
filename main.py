# ============================================================
# АНОНИМНАЯ ДОСКА СЕКРЕТОВ — main.py
# Здесь живёт весь бэкенд: бот, API и раздача фронтенда.
# ============================================================

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, unquote

import aiosqlite
import aiofiles
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Загрузка переменных из .env файла ──────────────────────
# Создай файл .env рядом с main.py и добавь туда:
#   BOT_TOKEN=123456:ABCdef...
#   WEBAPP_URL=https://твой-домен.com
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000")
DB_PATH = "secrets.db"

# ── Логирование — чтобы видеть что происходит в консоли ────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Создаём объекты бота и диспетчера ──────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ╔══════════════════════════════════════════════════════════╗
# ║                      БАЗА ДАННЫХ                        ║
# ╚══════════════════════════════════════════════════════════╝

async def init_db():
    """Создаём таблицы при первом запуске, если их нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей: храним только telegram_id
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY,
                tg_id      INTEGER UNIQUE NOT NULL,
                created_at REAL    DEFAULT (unixepoch())
            )
        """)
        # Таблица секретов/постов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                author_tg  INTEGER NOT NULL,
                content    TEXT    NOT NULL,
                created_at REAL    DEFAULT (unixepoch())
            )
        """)
        # Таблица лайков и дизлайков
        # vote = 1 (лайк) или -1 (дизлайк)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_tg INTEGER NOT NULL,
                vote    INTEGER NOT NULL,
                UNIQUE(post_id, user_tg)
            )
        """)
        # Таблица комментариев
        await db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id    INTEGER NOT NULL,
                author_tg  INTEGER NOT NULL,
                content    TEXT    NOT NULL,
                created_at REAL    DEFAULT (unixepoch())
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована ✅")


# ╔══════════════════════════════════════════════════════════╗
# ║               ПРОВЕРКА TELEGRAM initData                ║
# ╚══════════════════════════════════════════════════════════╝

def verify_telegram_init_data(init_data: str) -> dict | None:
    """
    Безопасно проверяем, что данные действительно от Telegram.
    Telegram подписывает initData своим секретом — мы это проверяем.
    Если подпись не совпадает — данные поддельные, отклоняем.
    Возвращает dict с данными пользователя или None если провал.
    """
    try:
        # Разбираем строку вида "key=value&key2=value2&hash=abc123"
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Telegram требует сортировать поля и соединять через \n
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        # Секретный ключ = HMAC-SHA256("WebAppData", BOT_TOKEN)
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()

        # Вычисляем правильный хэш
        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        # Сравниваем безопасно (защита от timing-атак)
        if not hmac.compare_digest(expected_hash, received_hash):
            logger.warning("Неверная подпись initData!")
            return None

        # Проверяем что данные не старше 24 часов
        auth_date = int(parsed.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            logger.warning("initData устарел!")
            return None

        # Достаём данные пользователя из поля "user"
        user_data = json.loads(unquote(parsed.get("user", "{}")))
        return user_data

    except Exception as e:
        logger.error(f"Ошибка верификации: {e}")
        return None


async def get_or_create_user(tg_id: int) -> int:
    """Регистрируем пользователя в БД если его ещё нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,)
        )
        await db.commit()
    return tg_id


# ╔══════════════════════════════════════════════════════════╗
# ║             ХЭНДЛЕРЫ БОТА (Aiogram)                     ║
# ╚══════════════════════════════════════════════════════════╝

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    """Обработчик команды /start — приветствие и кнопка."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔮 Открыть секреты",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]])
    await message.answer(
        "👋 Добро пожаловать в *Анонимную доску секретов*!\n\n"
        "Здесь все анонимны. Делись мыслями — никто не узнает 🤫",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# ╔══════════════════════════════════════════════════════════╗
# ║              LIFESPAN — запуск и остановка              ║
# ╚══════════════════════════════════════════════════════════╝

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Этот блок запускается при старте сервера (до yield)
    и при остановке (после yield).
    """
    # --- СТАРТ ---
    await init_db()

    # Запускаем polling бота в фоновой задаче
    # drop_pending_updates=True — игнорируем старые сообщения при рестарте
    bot_task = asyncio.create_task(
        dp.start_polling(bot, drop_pending_updates=True)
    )
    logger.info("Бот запущен ✅")

    yield  # <-- сервер работает здесь

    # --- ОСТАНОВКА ---
    bot_task.cancel()
    await bot.session.close()
    logger.info("Бот остановлен")


# ╔══════════════════════════════════════════════════════════╗
# ║                    FASTAPI ПРИЛОЖЕНИЕ                   ║
# ╚══════════════════════════════════════════════════════════╝

app = FastAPI(lifespan=lifespan, title="Secrets Board API")


# ── Вспомогательная функция: достаём user из заголовка ──────
async def auth_user(x_init_data: str = Header(...)) -> int:
    """
    Dependency (зависимость) FastAPI.
    Берёт заголовок X-Init-Data, проверяет его и возвращает tg_id.
    Если что-то не так — отдаёт 401 Unauthorized.
    """
    user = verify_telegram_init_data(x_init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Неверные данные авторизации")
    tg_id = user.get("id")
    if not tg_id:
        raise HTTPException(status_code=401, detail="Не найден user.id")
    await get_or_create_user(tg_id)
    return tg_id


# ── Pydantic-схемы — описываем форму входящих данных ────────
class PostCreate(BaseModel):
    content: str  # Текст секрета

class CommentCreate(BaseModel):
    content: str  # Текст комментария

class VoteCreate(BaseModel):
    vote: int  # 1 = лайк, -1 = дизлайк


# ── Отдаём index.html для всех "не-API" маршрутов ───────────
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Отдаём фронтенд."""
    async with aiofiles.open("index.html", "r", encoding="utf-8") as f:
        return await f.read()


# ──────────────────────────────────────────────────────────
#  API: ПОСТЫ
# ──────────────────────────────────────────────────────────

@app.get("/api/posts")
async def get_posts(x_init_data: str = Header(...)):
    """Получить ленту постов (от новых к старым) с лайками и числом комментов."""
    tg_id = await auth_user(x_init_data)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row  # чтобы получать dict-подобные строки
        cursor = await db.execute("""
            SELECT
                p.id,
                p.content,
                p.created_at,
                COALESCE(SUM(CASE WHEN v.vote = 1  THEN 1 ELSE 0 END), 0) AS likes,
                COALESCE(SUM(CASE WHEN v.vote = -1 THEN 1 ELSE 0 END), 0) AS dislikes,
                (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comments_count,
                (SELECT vote FROM votes WHERE post_id = p.id AND user_tg = ?) AS my_vote
            FROM posts p
            LEFT JOIN votes v ON v.post_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            LIMIT 50
        """, (tg_id,))
        rows = await cursor.fetchall()

    posts = [dict(row) for row in rows]
    return JSONResponse(posts)


@app.post("/api/posts")
async def create_post(body: PostCreate, x_init_data: str = Header(...)):
    """Создать новый секрет."""
    tg_id = await auth_user(x_init_data)

    # Защита от пустого и слишком длинного текста
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Текст не может быть пустым")
    if len(content) > 1000:
        raise HTTPException(status_code=400, detail="Максимум 1000 символов")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO posts (author_tg, content) VALUES (?, ?)",
            (tg_id, content)
        )
        await db.commit()
        post_id = cursor.lastrowid

    return JSONResponse({"id": post_id, "content": content})


# ──────────────────────────────────────────────────────────
#  API: ГОЛОСОВАНИЕ (Лайки / Дизлайки)
# ──────────────────────────────────────────────────────────

@app.post("/api/posts/{post_id}/vote")
async def vote_post(post_id: int, body: VoteCreate, x_init_data: str = Header(...)):
    """
    Поставить лайк (vote=1) или дизлайк (vote=-1).
    Повторное нажатие той же кнопки — снимает голос.
    """
    tg_id = await auth_user(x_init_data)

    if body.vote not in (1, -1):
        raise HTTPException(status_code=400, detail="vote должен быть 1 или -1")

    async with aiosqlite.connect(DB_PATH) as db:
        # Смотрим, есть ли уже голос от этого пользователя
        cursor = await db.execute(
            "SELECT vote FROM votes WHERE post_id = ? AND user_tg = ?",
            (post_id, tg_id)
        )
        existing = await cursor.fetchone()

        if existing:
            if existing[0] == body.vote:
                # Повторное нажатие — удаляем голос (toggle)
                await db.execute(
                    "DELETE FROM votes WHERE post_id = ? AND user_tg = ?",
                    (post_id, tg_id)
                )
            else:
                # Меняем голос (с лайка на дизлайк или наоборот)
                await db.execute(
                    "UPDATE votes SET vote = ? WHERE post_id = ? AND user_tg = ?",
                    (body.vote, post_id, tg_id)
                )
        else:
            # Новый голос
            await db.execute(
                "INSERT INTO votes (post_id, user_tg, vote) VALUES (?, ?, ?)",
                (post_id, tg_id, body.vote)
            )

        await db.commit()

        # Возвращаем обновлённые счётчики
        cursor = await db.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN vote = 1  THEN 1 ELSE 0 END), 0) AS likes,
                COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) AS dislikes,
                (SELECT vote FROM votes WHERE post_id = ? AND user_tg = ?) AS my_vote
            FROM votes WHERE post_id = ?
        """, (post_id, tg_id, post_id))
        row = await cursor.fetchone()

    return JSONResponse({
        "likes": row[0],
        "dislikes": row[1],
        "my_vote": row[2]
    })


# ──────────────────────────────────────────────────────────
#  API: КОММЕНТАРИИ
# ──────────────────────────────────────────────────────────

@app.get("/api/posts/{post_id}/comments")
async def get_comments(post_id: int, x_init_data: str = Header(...)):
    """Получить все комментарии к посту."""
    await auth_user(x_init_data)  # просто проверяем авторизацию

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT id, content, created_at
            FROM comments
            WHERE post_id = ?
            ORDER BY created_at ASC
        """, (post_id,))
        rows = await cursor.fetchall()

    return JSONResponse([dict(row) for row in rows])


@app.post("/api/posts/{post_id}/comments")
async def add_comment(post_id: int, body: CommentCreate, x_init_data: str = Header(...)):
    """
    Добавить комментарий.
    КИЛЛЕР-ФИЧА: отправляем уведомление автору поста в Telegram.
    """
    tg_id = await auth_user(x_init_data)

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Комментарий не может быть пустым")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="Максимум 500 символов")

    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем что пост существует и достаём автора
        cursor = await db.execute(
            "SELECT author_tg FROM posts WHERE id = ?", (post_id,)
        )
        post = await cursor.fetchone()
        if not post:
            raise HTTPException(status_code=404, detail="Пост не найден")

        author_tg_id = post[0]

        # Сохраняем комментарий
        cursor = await db.execute(
            "INSERT INTO comments (post_id, author_tg, content) VALUES (?, ?, ?)",
            (post_id, tg_id, content)
        )
        await db.commit()
        comment_id = cursor.lastrowid

    # ── КИЛЛЕР-ФИЧА: уведомление автору поста ───────────────
    # Не уведомляем, если человек комментирует свой же пост
    if author_tg_id != tg_id:
        try:
            # Обрезаем текст комментария до 100 символов для превью
            preview = content[:100] + ("..." if len(content) > 100 else "")
            await bot.send_message(
                chat_id=author_tg_id,
                text=f"💬 *Кто-то прокомментировал ваш секрет:*\n\n_{preview}_",
                parse_mode="Markdown"
            )
        except Exception as e:
            # Если юзер заблокировал бота — просто логируем, не падаем
            logger.warning(f"Не удалось отправить уведомление {author_tg_id}: {e}")

    return JSONResponse({"id": comment_id, "content": content})


# ──────────────────────────────────────────────────────────
#  Точка входа — запуск сервера
# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # host="0.0.0.0" — слушаем все сетевые интерфейсы (нужно для сервера)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
