import logging
import os
import sqlite3
import time

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# --- Конфигурация из переменных окружения ---
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Render автоматически подставляет RENDER_EXTERNAL_URL, например https://mybot.onrender.com
# Если переменной нет (локальный запуск), можно задать WEBHOOK_URL вручную.
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")  # опционально, но рекомендуется

# Порт: Render требует, чтобы приложение слушало порт из переменной PORT
PORT = int(os.environ.get("PORT", 8080))
HOST = "0.0.0.0"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_PATH = "saved_messages.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            business_connection_id TEXT,
            chat_id INTEGER,
            message_id INTEGER,
            from_id INTEGER,
            from_name TEXT,
            text TEXT,
            photo_file_id TEXT,
            saved_at INTEGER,
            PRIMARY KEY (business_connection_id, chat_id, message_id)
        )
        """
    )
    # Таблица связи: какое business_connection_id кому принадлежит
    # (т.е. какому пользователю Telegram слать уведомления об удалении
    # в чатах, подключённых через ЭТО соединение).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS connections (
            business_connection_id TEXT PRIMARY KEY,
            owner_user_id INTEGER,
            is_enabled INTEGER,
            updated_at INTEGER
        )
        """
    )
    conn.commit()
    conn.close()


def upsert_connection(business_connection_id, owner_user_id, is_enabled):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT OR REPLACE INTO connections
        (business_connection_id, owner_user_id, is_enabled, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (business_connection_id, owner_user_id, int(is_enabled), int(time.time())),
    )
    conn.commit()
    conn.close()


def get_connection_owner(business_connection_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT owner_user_id, is_enabled FROM connections WHERE business_connection_id = ?",
        (business_connection_id,),
    ).fetchone()
    conn.close()
    return row  # (owner_user_id, is_enabled) или None


def save_message(business_connection_id, chat_id, message_id, from_id, from_name, text, photo_file_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT OR REPLACE INTO messages
        (business_connection_id, chat_id, message_id, from_id, from_name, text, photo_file_id, saved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (business_connection_id, chat_id, message_id, from_id, from_name, text, photo_file_id, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_messages(business_connection_id, chat_id, message_ids):
    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"""
        SELECT message_id, from_name, text, photo_file_id
        FROM messages
        WHERE business_connection_id = ? AND chat_id = ? AND message_id IN ({placeholders})
        """,
        (business_connection_id, chat_id, *message_ids),
    ).fetchall()
    conn.close()
    return rows


@dp.business_connection()
async def on_business_connection(connection):
    """
    Срабатывает, когда ЛЮБОЙ пользователь подключает или отключает этого бота
    в Настройки -> Telegram для бизнеса -> Чат-боты. Здесь мы запоминаем,
    какой business_connection_id принадлежит какому Telegram-пользователю —
    это и есть ключ мульти-пользовательской работы: у каждого подключившегося
    свой owner_user_id, и уведомления уходят именно ему, а не одному жёстко
    заданному человеку.
    """
    upsert_connection(
        business_connection_id=connection.id,
        owner_user_id=connection.user.id,
        is_enabled=connection.is_enabled,
    )
    logging.info(
        f"Business connection {'включено' if connection.is_enabled else 'выключено'}: "
        f"{connection.id} -> владелец {connection.user.id} ({connection.user.full_name})"
    )
    if connection.is_enabled:
        await bot.send_message(
            connection.user.id,
            "✅ Бот подключён к вашему аккаунту. Теперь, если в ваших личных "
            "чатах кто-то удалит сообщение, я пришлю вам его копию здесь.",
        )


@dp.business_message()
async def on_business_message(message):
    """Сохраняем каждое сообщение (своё и собеседника) в бизнес-чате сразу, как оно пришло."""
    photo_file_id = message.photo[-1].file_id if message.photo else None
    save_message(
        business_connection_id=message.business_connection_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        from_id=message.from_user.id if message.from_user else None,
        from_name=message.from_user.full_name if message.from_user else "Неизвестно",
        text=message.text or message.caption,
        photo_file_id=photo_file_id,
    )


@dp.deleted_business_messages()
async def on_deleted_business_messages(event):
    """
    Срабатывает при удалении сообщения в чате, подключённом через бизнес-режим.
    Определяем владельца именно этого business_connection_id и шлём уведомление
    только ему — у другого подключившегося пользователя будут свои события
    со своим business_connection_id и своим owner_user_id.
    """
    owner = get_connection_owner(event.business_connection_id)
    if not owner or not owner[1]:
        logging.warning(f"Нет владельца или соединение выключено: {event.business_connection_id}")
        return
    owner_user_id = owner[0]

    rows = get_messages(event.business_connection_id, event.chat.id, event.message_ids)

    if not rows:
        await bot.send_message(
            owner_user_id,
            f"⚠️ Удалено сообщение в чате {event.chat.id}, но копия не найдена в базе "
            f"(возможно, бот не успел его сохранить до удаления).",
        )
        return

    for message_id, from_name, text, photo_file_id in rows:
        caption = f"🗑 Удалённое сообщение\nОт: {from_name}\nЧат: {event.chat.id}\n\n{text or ''}"
        if photo_file_id:
            await bot.send_photo(owner_user_id, photo_file_id, caption=caption)
        else:
            await bot.send_message(owner_user_id, caption)


async def on_startup(bot: Bot):
    """Устанавливаем вебхук при старте приложения."""
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError(
            "Не удалось определить URL для вебхука. "
            "Задайте переменную WEBHOOK_URL или запустите на Render (RENDER_EXTERNAL_URL)."
        )
    webhook_url = f"{base_url.rstrip('/')}{WEBHOOK_PATH}"

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    logging.info(f"Вебхук установлен: {webhook_url}")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logging.info("Вебхук удалён.")


def main():
    init_db()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
