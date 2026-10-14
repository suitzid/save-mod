import os
import re
import html
import time
import asyncio
import sqlite3
import hashlib
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart, Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, BusinessConnection, BusinessMessagesDeleted,
    Update, User, InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH = "/webhook"
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
ROOT_ADMIN = 5570425300
CONNECTED_TEXT = "🤖 Бот подключён к автоматизации и следит за твоими чатами."

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ============================== БД ==============================
db = sqlite3.connect("data.db", check_same_thread=False)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, owner_chat_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS msgs (
    conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
    sender_id INTEGER, sender_username TEXT, sender_name TEXT, text TEXT,
    media_type TEXT, file_id TEXT,
    PRIMARY KEY (conn_id, chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS banned (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS channels (chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO admins VALUES (%d);
INSERT OR IGNORE INTO settings VALUES ('enabled', '1');
"""
    % ROOT_ADMIN
)
db.commit()


def is_admin(uid: int) -> bool:
    return db.execute("SELECT 1 FROM admins WHERE id=?", (uid,)).fetchone() is not None


def is_banned(uid: int) -> bool:
    return db.execute("SELECT 1 FROM banned WHERE id=?", (uid,)).fetchone() is not None


def is_enabled() -> bool:
    r = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
    return not r or r[0] == "1"


def upsert_user(u: User):
    db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (u.id, u.username, u.full_name))
    db.commit()


# ============================== утилиты ==============================
class LogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data):
        log.info("UPDATE: %s", event.event_type)
        return await handler(event, data)


dp.update.outer_middleware(LogMiddleware())

MEDIA_SENDERS = {
    "photo": bot.send_photo, "video": bot.send_video, "voice": bot.send_voice,
    "video_note": bot.send_video_note, "document": bot.send_document,
    "audio": bot.send_audio, "animation": bot.send_animation, "sticker": bot.send_sticker,
}


def extract_media(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    for t in ("video", "voice", "video_note", "document", "audio", "animation", "sticker"):
        obj = getattr(m, t, None)
        if obj:
            return t, obj.file_id
    return None, None


def mention(uid: int, username, name: str) -> str:
    if username:
        return f"@{username}"
    return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'


def quote(text: str, limit: int = 1500) -> str:
    return f"<blockquote>{html.escape((text or '—')[:limit])}</blockquote>"


async def owner_of(conn_id: str):
    row = db.execute("SELECT owner_chat_id FROM connections WHERE id=?", (conn_id,)).fetchone()
    if row:
        return row[0]
    try:
        c = await bot.get_business_connection(conn_id)
        db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?)", (c.id, c.user_chat_id))
        db.commit()
        upsert_user(c.user)
        return c.user_chat_id
    except Exception:
        log.exception("get_business_connection failed")
        return None


# ---------- обязательная подписка ----------
_last_prompt: dict[int, float] = {}


async def missing_channels(uid: int):
    miss = []
    for chat_id, title, link in db.execute("SELECT chat_id, title, link FROM channels").fetchall():
        try:
            mem = await bot.get_chat_member(chat_id, uid)
            ok = mem.status in ("member", "administrator", "creator") or (
                mem.status == "restricted" and getattr(mem, "is_member", False)
            )
        except Exception:
            log.exception("get_chat_member failed for %s", chat_id)
            ok = True  # если не можем проверить — не блокируем
        if not ok:
            miss.append((title, link))
    return miss


async def send_sub_prompt(uid: int, miss, force: bool = False):
    if not force and time.time() - _last_prompt.get(uid, 0) < 600:
        return
    _last_prompt[uid] = time.time()
    b = InlineKeyboardBuilder()
    for title, link in miss:
        b.button(text=f"📌 {title}", url=link)
    b.button(text="✅ Я подписался", callback_data="chk")
    b.adjust(1)
    try:
        await bot.send_message(
            uid, "Чтобы бот работал, подпишись на каналы ниже и нажми «Я подписался».",
            reply_markup=b.as_markup(),
        )
    except Exception:
        log.exception("sub prompt failed")


async def notify_ok(uid: int) -> bool:
    """Можно ли слать уведомления этому владельцу."""
    if not is_enabled() or is_banned(uid):
        return False
    if is_admin(uid):
        return True
    miss = await missing_channels(uid)
    if miss:
        await send_sub_prompt(uid, miss)
        return False
    return True


# ============================== пользователь ==============================
@dp.message(CommandStart())
async def start(m: Message):
    u = m.from_user
    upsert_user(u)
    admin = is_admin(u.id)
    if is_banned(u.id) and not admin:
        return await m.answer("⛔️ Ты заблокирован.")
    if not is_enabled() and not admin:
        return await m.answer("🔴 Бот временно выключен.")
    if not admin:
        miss = await missing_channels(u.id)
        if miss:
            return await send_sub_prompt(u.id, miss, force=True)
    text = (
        "👋 Я сохраняю удалённые и изменённые сообщения.\n\n"
        "Подключение: <b>Настройки → Telegram для бизнеса → Чат-боты</b> → "
        "выбери меня и дай доступ к чатам.\n\n" + CONNECTED_TEXT
    )
    if admin:
        text += "\n\n⚙️ Админ-панель: /admin"
    await m.answer(text)


@dp.callback_query(F.data == "chk")
async def check_sub(cb: CallbackQuery):
    miss = await missing_channels(cb.from_user.id)
    if miss:
        return await cb.answer("Ты ещё не подписан на все каналы", show_alert=True)
    await cb.answer()
    try:
        await cb.message.edit_text("✅ Спасибо за подписку!\n\n" + CONNECTED_TEXT)
    except TelegramBadRequest:
        pass


# ============================== business ==============================
@dp.business_connection()
async def on_connection(c: BusinessConnection):
    upsert_user(c.user)
    if c.is_enabled:
        db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?)", (c.id, c.user_chat_id))
        db.commit()
        if await notify_ok(c.user_chat_id):
            await bot.send_message(c.user_chat_id, CONNECTED_TEXT)
    else:
        db.execute("DELETE FROM connections WHERE id=?", (c.id,))
        db.commit()
        try:
            await bot.send_message(c.user_chat_id, "🔌 Бот отключён от автоматизации.")
        except Exception:
            pass


@dp.business_message()
async def on_business_message(m: Message):
    owner = await owner_of(m.business_connection_id)
    if not owner or is_banned(owner) or not is_enabled():
        return
    mtype, file_id = extract_media(m)
    u = m.from_user
    db.execute(
        "INSERT OR REPLACE INTO msgs VALUES (?,?,?,?,?,?,?,?,?)",
        (m.business_connection_id, m.chat.id, m.message_id,
         u.id if u else 0, u.username if u else None, u.full_name if u else "?",
         m.text or m.caption or "", mtype, file_id),
    )
    db.commit()


@dp.edited_business_message()
async def on_edit(m: Message):
    owner = await owner_of(m.business_connection_id)
    if not owner or is_banned(owner) or not is_enabled():
        return
    row = db.execute(
        "SELECT text, media_type, file_id FROM msgs WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (m.business_connection_id, m.chat.id, m.message_id),
    ).fetchone()
    new_text = m.text or m.caption or ""
    u = m.from_user

    if u and u.id != owner and await notify_ok(owner):
        old = row[0] if row else None
        await bot.send_message(
            owner,
            f"✏️ {mention(u.id, u.username, u.full_name)} изменил сообщение:\n\n"
            f"Было:\n{quote(old) if old else '<i>не сохранено</i>'}\n"
            f"Стало:\n{quote(new_text)}",
        )

    mtype, file_id = extract_media(m)
    db.execute(
        "INSERT OR REPLACE INTO msgs VALUES (?,?,?,?,?,?,?,?,?)",
        (m.business_connection_id, m.chat.id, m.message_id,
         u.id if u else 0, u.username if u else None, u.full_name if u else "?",
         new_text, mtype or (row[1] if row else None), file_id or (row[2] if row else None)),
    )
    db.commit()


@dp.deleted_business_messages()
async def on_deleted(d: BusinessMessagesDeleted):
    owner = await owner_of(d.business_connection_id)
    if not owner or not await notify_ok(owner):
        return
    for mid in d.message_ids:
        row = db.execute(
            "SELECT sender_id, sender_username, sender_name, text, media_type, file_id FROM msgs "
            "WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (d.business_connection_id, d.chat.id, mid),
        ).fetchone()
        if not row:
            await bot.send_message(
                owner,
                f"🗑 В чате с {html.escape(d.chat.full_name or str(d.chat.id))} "
                f"удалено сообщение, но бот его не успел сохранить.",
            )
            continue
        uid, username, name, text, mtype, file_id = row
        if uid == owner:
            continue
        caption = f"🗑 {mention(uid, username, name)} удалил сообщение:"
        if text:
            caption += f"\n\n{quote(text, 800)}"
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


# ============================== админ-панель ==============================
class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        if not u or not is_admin(u.id):
            return False
        if isinstance(event, Message) and event.chat.type != "private":
            return False
        return True


class A(StatesGroup):
    add_admin = State()
    del_admin = State()
    ban = State()
    unban = State()
    broadcast = State()
    add_channel = State()


PROMPTS = {
    "add_admin": ("➕ Пришли ID, @username или перешли сообщение нового админа.", A.add_admin),
    "del_admin": ("➖ Пришли ID, @username или перешли сообщение админа, которого убрать.", A.del_admin),
    "ban": ("🚫 Пришли ID, @username или перешли сообщение пользователя для блокировки.", A.ban),
    "unban": ("✅ Пришли ID, @username или перешли сообщение пользователя для разблокировки.", A.unban),
    "bc": ("📢 Пришли сообщение для рассылки (любое: текст, фото, видео...).", A.broadcast),
    "add_channel": (
        "➕ Пришли @username канала, ссылку t.me/... или ID (-100...).\n"
        "Бот должен быть админом в этом канале.", A.add_channel),
}


def panel_text() -> str:
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = db.execute("SELECT COUNT(*) FROM banned").fetchone()[0]
    chans = db.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    admins = [str(r[0]) for r in db.execute("SELECT id FROM admins")]
    return (
        "⚙️ <b>Админ-панель</b>\n\n"
        f"Статус: {'🟢 включён' if is_enabled() else '🔴 выключен'}\n"
        f"Пользователей: {users}\nЗаблокировано: {banned}\n"
        f"Каналов подписки: {chans}\nАдмины: {', '.join(admins)}"
    )


def menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить админа", callback_data="adm:ask:add_admin")
    b.button(text="➖ Удалить админа", callback_data="adm:ask:del_admin")
    b.button(text="🚫 Заблокировать", callback_data="adm:ask:ban")
    b.button(text="✅ Разблокировать", callback_data="adm:ask:unban")
    b.button(text="📢 Рассылка", callback_data="adm:ask:bc")
    b.button(text="📌 Обяз. подписка", callback_data="adm:subs")
    b.button(
        text="🔴 Выключить бота" if is_enabled() else "🟢 Включить бота",
        callback_data="adm:toggle",
    )
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def subs_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for chat_id, title, _ in db.execute("SELECT chat_id, title, link FROM channels"):
        b.button(text=f"🗑 {title}", callback_data=f"adm:subdel:{chat_id}")
    b.button(text="➕ Добавить канал", callback_data="adm:ask:add_channel")
    b.button(text="⬅️ Назад", callback_data="adm:menu")
    b.adjust(1)
    return b.as_markup()


async def show(ev, text: str, kb: InlineKeyboardMarkup):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=kb)


async def show_menu(ev):
    await show(ev, panel_text(), menu_kb())


async def show_subs(ev):
    rows = db.execute("SELECT title, link FROM channels").fetchall()
    lst = "\n".join(f"• <a href=\"{l}\">{html.escape(t)}</a>" for t, l in rows) or "Каналов нет."
    await show(ev, f"📌 <b>Обязательная подписка</b>\n\n{lst}\n\nНажми на канал, чтобы удалить.", subs_kb())


def get_target(m: Message):
    fo = getattr(m, "forward_origin", None)
    su = getattr(fo, "sender_user", None)
    if su:
        return su.id
    t = (m.text or "").strip()
    if t.startswith("@"):
        r = db.execute("SELECT id FROM users WHERE lower(username)=?", (t[1:].lower(),)).fetchone()
        return r[0] if r else None
    r = re.search(r"\d{5,}", t)
    return int(r.group()) if r else None


@dp.message(IsAdmin(), Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m)


@dp.message(IsAdmin(), Command("cancel"))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m)


@dp.callback_query(IsAdmin(), F.data == "adm:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_menu(cb)


@dp.callback_query(IsAdmin(), F.data == "adm:subs")
async def cb_subs(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_subs(cb)


@dp.callback_query(IsAdmin(), F.data == "adm:toggle")
async def cb_toggle(cb: CallbackQuery):
    db.execute("UPDATE settings SET value=? WHERE key='enabled'", ("0" if is_enabled() else "1",))
    db.commit()
    await show_menu(cb)


@dp.callback_query(IsAdmin(), F.data.startswith("adm:ask:"))
async def cb_ask(cb: CallbackQuery, state: FSMContext):
    text, st = PROMPTS[cb.data.split(":")[2]]
    await state.set_state(st)
    try:
        await cb.message.edit_text(text + "\n\n/cancel — отмена")
    except TelegramBadRequest:
        pass
    await cb.answer()


@dp.callback_query(IsAdmin(), F.data.startswith("adm:subdel:"))
async def cb_subdel(cb: CallbackQuery):
    db.execute("DELETE FROM channels WHERE chat_id=?", (int(cb.data.split(":")[2]),))
    db.commit()
    await show_subs(cb)


async def done(m: Message, state: FSMContext, text: str, to_subs: bool = False):
    await state.clear()
    await m.answer(text)
    await (show_subs(m) if to_subs else show_menu(m))


@dp.message(IsAdmin(), A.add_admin)
async def st_add_admin(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer("Не понял. Пришли ID, @username или перешли сообщение. /cancel — отмена")
    db.execute("INSERT OR IGNORE INTO admins VALUES (?)", (uid,))
    db.commit()
    try:
        await bot.send_message(uid, "⚙️ Тебя назначили админом. Панель: /admin")
    except Exception:
        pass
    await done(m, state, f"✅ {uid} теперь админ.")


@dp.message(IsAdmin(), A.del_admin)
async def st_del_admin(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer("Не понял. Пришли ID, @username или перешли сообщение. /cancel — отмена")
    if uid == ROOT_ADMIN:
        return await done(m, state, "⛔️ Главного админа удалить нельзя.")
    db.execute("DELETE FROM admins WHERE id=?", (uid,))
    db.commit()
    await done(m, state, f"✅ {uid} больше не админ.")


@dp.message(IsAdmin(), A.ban)
async def st_ban(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer("Не понял. Пришли ID, @username или перешли сообщение. /cancel — отмена")
    if is_admin(uid):
        return await done(m, state, "⛔️ Админа заблокировать нельзя. Сначала убери из админов.")
    db.execute("INSERT OR IGNORE INTO banned VALUES (?)", (uid,))
    db.commit()
    await done(m, state, f"🚫 {uid} заблокирован.")


@dp.message(IsAdmin(), A.unban)
async def st_unban(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer("Не понял. Пришли ID, @username или перешли сообщение. /cancel — отмена")
    db.execute("DELETE FROM banned WHERE id=?", (uid,))
    db.commit()
    await done(m, state, f"✅ {uid} разблокирован.")


@dp.message(IsAdmin(), A.add_channel)
async def st_add_channel(m: Message, state: FSMContext):
    ref = (m.text or "").strip()
    ref = re.sub(r"^(https?://)?t\.me/", "@", ref)
    if re.fullmatch(r"-?\d+", ref):
        ref = int(ref)
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in ("administrator", "creator"):
            return await m.answer("Сначала сделай бота админом в этом канале и пришли ещё раз. /cancel — отмена")
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            link = chat.invite_link or await bot.export_chat_invite_link(chat.id)
    except Exception as e:
        return await m.answer(f"Не получилось: {html.escape(str(e))}\n/cancel — отмена")
    db.execute("INSERT OR REPLACE INTO channels VALUES (?,?,?)", (chat.id, chat.title or str(chat.id), link))
    db.commit()
    await done(m, state, f"✅ Канал «{html.escape(chat.title or '')}» добавлен.", to_subs=True)


@dp.message(IsAdmin(), A.broadcast)
async def st_broadcast(m: Message, state: FSMContext):
    await state.clear()
    ids = [r[0] for r in db.execute("SELECT id FROM users WHERE id NOT IN (SELECT id FROM banned)")]
    status = await m.answer(f"📢 Рассылка запущена ({len(ids)} получателей)...")
    ok = fail = 0
    for uid in ids:
        for _ in range(2):
            try:
                await m.copy_to(uid)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    await status.edit_text(f"📢 Рассылка завершена.\nДоставлено: {ok}\nНе доставлено: {fail}")
    await show_menu(m)


# ============================== запуск ==============================
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
