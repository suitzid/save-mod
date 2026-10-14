import os
import html
import sqlite3
import hashlib
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    BusinessConnection,
    BusinessMessagesDeleted,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("WEBHOOK_BASE_URL") or os.environ["RENDER_EXTERNAL_URL"]
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH = "/webhook"
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ---------- БД (sqlite; на бесплатном Render диск стирается при рестарте) ----------
db = sqlite3.connect("data.db", check_same_thread=False)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    owner_chat_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    conn_id TEXT,
    chat_id INTEGER,
    msg_id INTEGER,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT,
    media_type TEXT,
    file_id TEXT,
    PRIMARY KEY (conn_id, chat_id, msg_id)
);
"""
)
db.commit()

MEDIA_SENDERS = {
    "photo": bot.send_photo,
    "video": bot.send_video,
    "voice": bot.send_voice,
    "video_note": bot.send_video_note,
    "document": bot.send_document,
    "audio": bot.send_audio,
    "animation": bot.send_animation,
    "sticker": bot.send_sticker,
}


def extract_media(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    for t in ("video", "voice", "video_note", "document", "audio", "animation", "sticker"):
        obj = getattr(m, t, None)
        if obj:
            return t, obj.file_id
    return None, None


def owner_of(conn_id: str):
    row = db.execute("SELECT owner_chat_id FROM connections WHERE id=?", (conn_id,)).fetchone()
    return row[0] if row else None


def who(name: str, uid: int) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


# ---------- хендлеры ----------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Я сохраняю удалённые и изменённые сообщения.\n\n"
        "Подключение: <b>Настройки → Telegram для бизнеса → Чат-боты</b> → "
        "выбери меня и дай доступ к чатам."
    )


@dp.business_connection()
async def on_connection(c: BusinessConnection):
    if c.is_enabled:
        db.execute(
            "INSERT OR REPLACE INTO connections VALUES (?, ?)", (c.id, c.user_chat_id)
        )
        db.commit()
        await bot.send_message(c.user_chat_id, "✅ Подключено. Слежу за чатами.")
    else:
        db.execute("DELETE FROM connections WHERE id=?", (c.id,))
        db.commit()


@dp.business_message()
async def on_business_message(m: Message):
    mtype, file_id = extract_media(m)
    sender = m.from_user
    db.execute(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)",
        (
            m.business_connection_id,
            m.chat.id,
            m.message_id,
            sender.id if sender else 0,
            sender.full_name if sender else "?",
            m.text or m.caption or "",
            mtype,
            file_id,
        ),
    )
    db.commit()


@dp.edited_business_message()
async def on_edit(m: Message):
    owner = owner_of(m.business_connection_id)
    row = db.execute(
        "SELECT sender_id, sender_name, text FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (m.business_connection_id, m.chat.id, m.message_id),
    ).fetchone()
    new_text = m.text or m.caption or ""

    if owner and row and row[0] != owner:  # не уведомляем о правках самого владельца
        await bot.send_message(
            owner,
            f"✏️ {who(row[1], row[0])} изменил(а) сообщение\n\n"
            f"<b>Было:</b>\n{html.escape(row[2] or '—')}\n\n"
            f"<b>Стало:</b>\n{html.escape(new_text or '—')}",
        )

    mtype, file_id = extract_media(m)
    db.execute(
        "UPDATE messages SET text=?, media_type=COALESCE(?, media_type), file_id=COALESCE(?, file_id) "
        "WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (new_text, mtype, file_id, m.business_connection_id, m.chat.id, m.message_id),
    )
    db.commit()


@dp.deleted_business_messages()
async def on_deleted(d: BusinessMessagesDeleted):
    owner = owner_of(d.business_connection_id)
    if not owner:
        return
    for mid in d.message_ids:
        row = db.execute(
            "SELECT sender_id, sender_name, text, media_type, file_id FROM messages "
            "WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (d.business_connection_id, d.chat.id, mid),
        ).fetchone()
        if not row or row[0] == owner:
            continue
        uid, name, text, mtype, file_id = row
        caption = f"🗑 {who(name, uid)} удалил(а) сообщение"
        if text:
            caption += f"\n\n{html.escape(text)}"
        try:
            sender = MEDIA_SENDERS.get(mtype)
            if sender and file_id:
                if mtype in ("video_note", "sticker"):
                    await sender(owner, file_id)
                    await bot.send_message(owner, caption)
                else:
                    await sender(owner, file_id, caption=caption)
            else:
                await bot.send_message(owner, caption)
        except Exception:
            logging.exception("failed to forward deleted message")


# ---------- веб-сервер ----------
async def on_startup(bot: Bot):
    await bot.set_webhook(
        f"{BASE_URL.rstrip('/')}{WEBHOOK_PATH}",
        secret_token=SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
    )


def main():
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
