import asyncio
import logging
import os
import sqlite3
import time

from aiogram import Bot, Dispatcher

# Токен и ID берутся из переменных окружения — никогда не хранить их в коде.
# Локально: создайте файл .env рядом (см. .env.example) и запускайте через
# `python -m dotenv run -- python save_mod_bot.py`, либо экспортируйте
# переменные в терминале перед запуском (export BOT_TOKEN=...).
# На Render: Dashboard -> ваш сервис -> Environment -> Add Environment Variable.
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

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
    conn.commit()
    conn.close()


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


@dp.business_message()
async def on_business_message(message):
    """Сохраняем каждое сообщение в бизнес-чате сразу, как оно пришло."""
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
    """Срабатывает, когда собеседник удаляет сообщение(я). Достаём из базы сохранённые копии."""
    rows = get_messages(event.business_connection_id, event.chat.id, event.message_ids)

    if not rows:
        await bot.send_message(
            OWNER_ID,
            f"⚠️ Удалено сообщение в чате {event.chat.id}, но копия не найдена в базе "
            f"(возможно, бот не успел его сохранить до удаления).",
        )
        return

    for message_id, from_name, text, photo_file_id in rows:
        caption = f"🗑 Удалённое сообщение\nОт: {from_name}\nЧат: {event.chat.id}\n\n{text or ''}"
        if photo_file_id:
            await bot.send_photo(OWNER_ID, photo_file_id, caption=caption)
        else:
            await bot.send_message(OWNER_ID, caption)


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
