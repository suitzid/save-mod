import os
import html
import sqlite3
import hashlib
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, BusinessConnection, BusinessMessagesDeleted, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH = "/webhook"
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
CONNECTED_TEXT = "🤖 Бот подключён к автоматизации и следит за твоими чатами."

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = sqlite3.connect("data.db", check_same_thread=False)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, owner_chat_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
    sender_id INTEGER, sender_name TEXT, text TEXT,
    media_type TEXT, file_id TEXT,
    PRIMARY KEY (conn_id, chat_id, msg_id)
);
"""
)
db.commit()

MEDIA_SENDERS = {
    "photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice,
    "video_note": bot.send_video_note, "document": bot.send_document,
    "audio": bot.send_audio, "animation": bot.send_animation, "sticker": bot.send_sticker,
}


class LogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data):
        log.info("UPDATE: %s", event.model_dump(exclude_none=True))
        return await handler(event, data)


dp.update.outer_middleware(LogMiddleware())


def extract_media(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    for t in ("video", "voice", "video_note", "document", "audio", "animation", "sticker"):
        obj = getattr(m, t, None)
        if obj:
            return t, obj.file_id
    return None, None


async def owner_of(conn_id: str):
    """Владелец подключения. Если в базе нет (рестарт/потеря диска) — спрашиваем у Telegram."""
    row = db.execute("SELECT owner_chat_id FROM connections WHERE id=?", (conn_id,)).fetchone()
    if row:
        return row[0]
    try:
        c = await bot.get_business_connection(conn_id)
        db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?)", (c.id, c.user_chat_id))
        db.commit()
        return c.user_chat_id
    except Exception:
        log.exception("get_business_connection failed")
        return None


def who(name: str, uid: int) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 Я сохраняю удалённые и изменённые сообщения.\n\n"
        "Подключение: <b>Настройки → Telegram для бизнеса → Чат-боты</b> → "
        "выбери меня и дай доступ к чатам.\n\n" + CONNECTED_TEXT
    )


@dp.business_connection()
async def on_connection(c: BusinessConnection):
    if c.is_enabled:
        db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?)", (c.id, c.user_chat_id))
        db.commit()
        await bot.send_message(c.user_chat_id, CONNECTED_TEXT)
    else:
        db.execute("DELETE FROM connections WHERE id=?", (c.id,))
        db.commit()
        await bot.send_message(c.user_chat_id, "🔌 Бот отключён от автоматизации.")


@dp.business_message()
async def on_business_message(m: Message):
    await owner_of(m.business_connection_id)  # заодно запоминаем владельца
    mtype, file_id = extract_media(m)
    u = m.from_user
    db.execute(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)",
        (m.business_connection_id, m.chat.id, m.message_id,
         u.id if u else 0, u.full_name if u else "?",
         m.text or m.caption or "", mtype, file_id),
    )
    db.commit()


@dp.edited_business_message()
async def on_edit(m: Message):
    owner = await owner_of(m.business_connection_id)
    row = db.execute(
        "SELECT sender_id, sender_name, text FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (m.business_connection_id, m.chat.id, m.message_id),
    ).fetchone()
    new_text = m.text or m.caption or ""
    u = m.from_user

    if owner and (not u or u.id != owner):
        old = html.escape(row[2] or "—") if row else "<i>не сохранено (сообщение пришло до запуска бота)</i>"
        await bot.send_message(
            owner,
            f"✏️ {who(u.full_name, u.id) if u else 'Собеседник'} изменил(а) сообщение\n\n"
            f"<b>Было:</b>\n{old}\n\n<b>Стало:</b>\n{html.escape(new_text or '—')}",
        )

    mtype, file_id = extract_media(m)
    db.execute(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)",
        (m.business_connection_id, m.chat.id, m.message_id,
         u.id if u else 0, u.full_name if u else "?", new_text,
         mtype or (row and None), file_id),
    )
    db.commit()


@dp.deleted_business_messages()
async def on_deleted(d: BusinessMessagesDeleted):
    owner = await owner_of(d.business_connection_id)
    if not owner:
        return
    for mid in d.message_ids:
        row = db.execute(
            "SELECT sender_id, sender_name, text, media_type, file_id FROM messages "
            "WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (d.business_connection_id, d.chat.id, mid),
        ).fetchone()
        if not row:
            await bot.send_message(
                owner,
                f"🗑 В чате с {html.escape(d.chat.full_name or str(d.chat.id))} удалено сообщение, "
                f"но бот его не успел сохранить.",
            )
            continue
        uid, name, text, mtype, file_id = row
        if uid == owner:
            continue
        caption = f"🗑 {who(name, uid)} удалил(а) сообщение"
        if text:
            caption += f"\n\n{html.escape(text)[:850]}"
        try:
            send = MEDIA_SENDERS.get(mtype)
            if send and file_id:
                if mtype in ("video_note", "sticker"):
                    await send(owner, file_id)
                    await bot.send_message(owner, caption)
                else:
                    await send(owner, file_id, caption=caption)
            else:
                await bot.send_message(owner, caption)
        except Exception:
            log.exception("failed to forward deleted message")


async def on_startup(bot: Bot):
    url = f"{BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
    types = dp.resolve_used_update_types()
    await bot.set_webhook(url, secret_token=SECRET, allowed_updates=types, drop_pending_updates=True)
    log.info("Webhook set: %s | allowed: %s", url, types)


def main():
    if not BASE_URL:
        raise RuntimeError("Нет RENDER_EXTERNAL_URL / WEBHOOK_BASE_URL")
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
