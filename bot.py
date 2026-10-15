import html
import json
import logging
import os
import re
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ───────────────────────── Конфигурация ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
PORT = int(os.environ.get("PORT", 8080))
HOST = "0.0.0.0"
DB_PATH = os.environ.get("DB_PATH", "saved_messages.db")  # на Render лучше путь на Disk, напр. /data/bot.db
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", 3))  # часовой пояс для «сегодня» (3 = Москва)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ───────────────────────── Премиум-эмодзи ─────────────────────────
# слот: (обычный эмодзи-запасной, название). ID премиум-эмодзи задаются в админке.
EMOJI_SLOTS = {
    "fire": ("🔥", "Огонь"), "star": ("⭐", "Звезда"), "check": ("✅", "Галочка"),
    "cross": ("❌", "Крестик"), "trash": ("🗑", "Корзина"), "user": ("👤", "Юзер"),
    "chat": ("💬", "Чат"), "stats": ("📊", "Статистика"), "text": ("✏️", "Текст"),
    "button": ("🔘", "Кнопка"), "photo": ("🖼", "Фото"), "eye": ("👁", "Глаз"),
    "lock": ("🔒", "Замок"), "crown": ("👑", "Корона"), "rocket": ("🚀", "Ракета"),
    "back": ("⬅️", "Назад"), "plus": ("➕", "Плюс"), "gift": ("🎁", "Подарок"),
}

PLAIN: ContextVar[bool] = ContextVar("plain", default=False)  # True = без премиум-эмодзи (запасной режим)

# ───────────────────────── База данных ─────────────────────────


def q(sql, params=(), one=False, commit=False):
    c = sqlite3.connect(DB_PATH)
    try:
        cur = c.execute(sql, params)
        if commit:
            c.commit()
        return cur.fetchone() if one else cur.fetchall()
    finally:
        c.close()


def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            business_connection_id TEXT, chat_id INTEGER, message_id INTEGER,
            from_id INTEGER, from_name TEXT, text TEXT, photo_file_id TEXT, saved_at INTEGER,
            PRIMARY KEY (business_connection_id, chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, first_seen INTEGER, last_seen INTEGER
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, added_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS deleted_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, chat_id INTEGER, total INTEGER, found INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_deleted_ts ON deleted_log(ts);
        """
    )
    c.commit()
    c.close()


_cache: dict = {}


def get_setting(key, default=None):
    if key not in _cache:
        r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
        _cache[key] = r[0] if r else None
    v = _cache[key]
    return default if v is None else v


def set_setting(key, value):
    if value is None:
        q("DELETE FROM settings WHERE key=?", (key,), commit=True)
    else:
        q("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value), commit=True)
    _cache[key] = value


def save_message(business_connection_id, chat_id, message_id, from_id, from_name, text, photo_file_id):
    q(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?)",
        (business_connection_id, chat_id, message_id, from_id, from_name, text, photo_file_id, int(time.time())),
        commit=True,
    )


def get_messages(business_connection_id, chat_id, message_ids):
    ph = ",".join("?" * len(message_ids))
    return q(
        f"SELECT message_id, from_name, text, photo_file_id FROM messages "
        f"WHERE business_connection_id=? AND chat_id=? AND message_id IN ({ph})",
        (business_connection_id, chat_id, *message_ids),
    )


def touch_user(u):
    now = int(time.time())
    q(
        "INSERT INTO users(user_id,username,full_name,first_seen,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
        "full_name=excluded.full_name, last_seen=excluded.last_seen",
        (u.id, u.username, u.full_name, now, now),
        commit=True,
    )


def is_admin(uid):
    return uid == OWNER_ID or q("SELECT 1 FROM admins WHERE user_id=?", (uid,), one=True) is not None


def is_hidden():
    return get_setting("hide_admins", "0") == "1"


# ───────────────────────── Тексты ─────────────────────────
DEFAULT_TEXTS = {
    "welcome": (
        "Приветствие (/start)", "{name}, {user_id}",
        "{e_fire} <b>Привет, {name}!</b>\n\n"
        "{e_star} Я сохраняю сообщения из бизнес-чатов и присылаю их тебе, когда собеседник их удаляет.\n\n"
        "{e_check} Подключение: <i>Настройки → Telegram для бизнеса → Чат-боты</i>",
    ),
    "help": (
        "Помощь (/help)", "{name}",
        "{e_rocket} <b>Как это работает</b>\n\n"
        "1. {e_chat} Подключи бота в Telegram для бизнеса.\n"
        "2. {e_lock} Бот тихо сохраняет сообщения.\n"
        "3. {e_trash} Когда собеседник удалит сообщение — я пришлю копию сюда.",
    ),
    "deleted": (
        "Уведомление об удалении", "{name}, {chat}, {text}",
        "{e_trash} <b>Удалённое сообщение</b>\n"
        "{e_user} От: <b>{name}</b>\n"
        "{e_chat} Чат: <code>{chat}</code>\n\n{text}",
    ),
    "not_found": (
        "Удалено, но копии нет", "{chat}",
        "{e_cross} Удалено сообщение в чате <code>{chat}</code>, но копии нет в базе "
        "(бот не успел сохранить его до удаления).",
    ),
}

PHOTO_SLOTS = {"welcome": "Фото в приветствии", "help": "Фото в помощи"}


def E(key):
    fb = EMOJI_SLOTS[key][0]
    cid = get_setting(f"emoji:{key}")
    if cid and not PLAIN.get():
        return f'<tg-emoji emoji-id="{cid}">{fb}</tg-emoji>'
    return fb


def render(tpl, **vars_):
    """Подставляет {e_слот} (премиум-эмодзи) и переменные за один проход."""

    def repl(m):
        k = m.group(1)
        if k.startswith("e_") and k[2:] in EMOJI_SLOTS:
            return E(k[2:])
        if k in vars_:
            return html.escape(str(vars_[k]))
        return m.group(0)

    tpl = re.sub(r"\{(\w+)\}", repl, tpl)
    if PLAIN.get():  # на всякий случай вычищаем премиум-теги, которые админ вставил в текст
        tpl = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", tpl, flags=re.S)
    return tpl


def get_text(key):
    return get_setting(f"text:{key}") or DEFAULT_TEXTS[key][2]


# ───────────────────────── Кнопки ─────────────────────────
DEFAULT_BUTTONS = [
    {"id": 1, "label": "Как это работает", "style": "primary", "icon": "slot:rocket",
     "type": "text", "value": "{e_rocket} <b>Всё просто:</b> подключи бота в Telegram для бизнеса — "
                              "и все удалённые сообщения придут тебе сюда.", "inline": False},
    {"id": 2, "label": "Канал", "style": "success", "icon": "slot:star",
     "type": "url", "value": "https://t.me/telegram", "inline": False},
    {"id": 3, "label": "Поддержка", "style": "danger", "icon": "slot:chat",
     "type": "url", "value": "https://t.me/telegram", "inline": True},
]

STYLE_NAMES = {"primary": "🔵 синяя", "success": "🟢 зелёная", "danger": "🔴 красная", "": "⚪ обычная"}


def get_buttons():
    raw = get_setting("buttons")
    return json.loads(raw) if raw else json.loads(json.dumps(DEFAULT_BUTTONS))


def save_buttons(lst):
    set_setting("buttons", json.dumps(lst, ensure_ascii=False))


def find_btn(bid):
    return next((b for b in get_buttons() if b["id"] == bid), None)


def update_btn(bid, **kw):
    lst = get_buttons()
    for b in lst:
        if b["id"] == bid:
            b.update(kw)
    save_buttons(lst)


def B(text, cb=None, style=None, icon=None, url=None):
    """Кнопка: цвет (style) + премиум-иконка (icon = 'slot:fire' / 'fire' / 'id:123...').
    Если премиум-эмодзи не задано или запасной режим — иконка превращается в обычный эмодзи в тексте."""
    kw = {}
    if style in ("primary", "success", "danger"):
        kw["style"] = style
    if icon:
        if icon.startswith("id:"):
            cid, fb = icon[3:], "•"
        else:
            slot = icon[5:] if icon.startswith("slot:") else icon
            cid, fb = get_setting(f"emoji:{slot}"), EMOJI_SLOTS.get(slot, ("•",))[0]
        if cid and not PLAIN.get():
            kw["icon_custom_emoji_id"] = cid
        else:
            text = f"{fb} {text}"
    return InlineKeyboardButton(text=text, callback_data=cb, url=url, **kw)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def BACK(cb):
    return [B("Назад", cb, icon="back")]


def normalize_url(v):
    v = v.strip()
    if v.startswith("@"):
        return "https://t.me/" + v[1:]
    if v.startswith(("http://", "https://", "tg://")):
        return v
    if re.match(r"^[\w.-]+\.[a-z]{2,}", v, re.I):
        return "https://" + v
    return None


def user_kb():
    rows = []
    for b in get_buttons():
        style, icon = b.get("style") or None, b.get("icon") or None
        if b["type"] == "url":
            btn = B(b["label"], style=style, icon=icon, url=b["value"])
        else:
            btn = B(b["label"], cb=f"ub:{b['id']}", style=style, icon=icon)
        if b.get("inline") and rows and len(rows[-1]) < 3:
            rows[-1].append(btn)
        else:
            rows.append([btn])
    return kb(rows) if rows else None


# ───────────────────────── Отправка с запасным режимом ─────────────────────────
async def show(event, fn, *args, edit=False, photo=None):
    """Строит экран через fn(*args) -> (text, markup) и отправляет.
    Если Telegram не принял премиум-эмодзи/иконки — автоматически повторяет без них."""
    for plain in (False, True):
        tok = PLAIN.set(plain)
        try:
            text, markup = fn(*args)
            if isinstance(event, int):
                if photo and len(text) > 1000:
                    await bot.send_photo(event, photo)
                    await bot.send_message(event, text, reply_markup=markup)
                elif photo:
                    await bot.send_photo(event, photo, caption=text, reply_markup=markup)
                else:
                    await bot.send_message(event, text, reply_markup=markup)
                return
            msg = event.message if isinstance(event, CallbackQuery) else event
            if edit:
                await msg.edit_text(text, reply_markup=markup)
            elif photo and len(text) > 1000:
                await msg.answer_photo(photo)
                await msg.answer(text, reply_markup=markup)
            elif photo:
                await msg.answer_photo(photo, caption=text, reply_markup=markup)
            else:
                await msg.answer(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            logging.warning("Отправка с премиум-эмодзи не удалась (%s), пробую без них", e)
            if plain:
                raise
        finally:
            PLAIN.reset(tok)


# ───────────────────────── Статистика ─────────────────────────
def _tz():
    return timezone(timedelta(hours=TZ_OFFSET))


def day_start(days_ago=0):
    now = datetime.now(_tz())
    s = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(s.timestamp())


def deleted_between(since, until=None):
    until = until or int(time.time()) + 1
    r = q("SELECT COALESCE(SUM(total),0), COALESCE(SUM(found),0) FROM deleted_log WHERE ts>=? AND ts<?",
          (since, until), one=True)
    return r[0], r[1]


def stats_screen():
    t, tf = deleted_between(day_start(0))
    y, _ = deleted_between(day_start(1), day_start(0))
    w, wf = deleted_between(day_start(6))
    m, mf = deleted_between(day_start(29))
    a, af = deleted_between(0)

    days = []
    for i in range(6, -1, -1):
        s = day_start(i)
        e = day_start(i - 1) if i > 0 else None
        days.append((datetime.fromtimestamp(s, _tz()).strftime("%d.%m"), deleted_between(s, e)[0]))
    mx = max([n for _, n in days] + [1])
    chart = "\n".join(f"{d} {'▇' * round(n / mx * 10) or '·'} {n}" for d, n in days)

    now = int(time.time())
    users_total = q("SELECT COUNT(*) FROM users", one=True)[0]
    new_today = q("SELECT COUNT(*) FROM users WHERE first_seen>=?", (day_start(0),), one=True)[0]
    new_week = q("SELECT COUNT(*) FROM users WHERE first_seen>=?", (day_start(6),), one=True)[0]
    active = q("SELECT COUNT(*) FROM users WHERE last_seen>=?", (now - 86400,), one=True)[0]
    saved = q("SELECT COUNT(*) FROM messages", one=True)[0]
    chats = q("SELECT COUNT(DISTINCT chat_id) FROM messages", one=True)[0]
    try:
        size = f"{os.path.getsize(DB_PATH) / 1024 / 1024:.2f} МБ"
    except OSError:
        size = "—"

    text = (
        f"{E('stats')} <b>Статистика бота</b>\n\n"
        f"{E('trash')} <b>Удалено сообщений</b>\n"
        f"• Сегодня: <b>{t}</b> (спасено {tf})\n"
        f"• Вчера: <b>{y}</b>\n"
        f"• За 7 дней: <b>{w}</b> (спасено {wf})\n"
        f"• За 30 дней: <b>{m}</b> (спасено {mf})\n"
        f"• Всего: <b>{a}</b> (спасено {af})\n\n"
        f"<pre>{chart}</pre>\n"
        f"{E('user')} <b>Пользователи</b>\n"
        f"• Всего в боте: <b>{users_total}</b>\n"
        f"• Новых сегодня: <b>{new_today}</b>, за неделю: <b>{new_week}</b>\n"
        f"• Активны за 24 часа: <b>{active}</b>\n\n"
        f"{E('chat')} <b>База</b>\n"
        f"• Сохранено сообщений: <b>{saved}</b> в <b>{chats}</b> чатах\n"
        f"• Размер базы: <b>{size}</b>"
    )
    return text, kb([[B("Обновить", "adm:stats", "primary", "fire")], BACK("adm:main")])


# ───────────────────────── Экраны админ-панели ─────────────────────────
def main_screen(u):
    hidden = is_hidden()
    who = "•••••• (скрыто)" if hidden else f"{html.escape(u.full_name)} · <code>{u.id}</code>"
    role = "Владелец" if u.id == OWNER_ID else "Админ"
    text = f"{E('crown')} <b>Админ-панель</b>\n\n{E('user')} {role}: {who}\n\n{E('fire')} Выбери раздел:"
    rows = [
        [B("Статистика", "adm:stats", "primary", "stats")],
        [B("Тексты", "adm:texts", "success", "text"), B("Кнопки", "adm:btns", "success", "button")],
        [B("Фото", "adm:photos", "primary", "photo"), B("Эмодзи", "adm:emoji", "primary", "star")],
        [B("Предпросмотр", "adm:preview", None, "eye")],
    ]
    if u.id == OWNER_ID:
        rows.append([
            B("Админы", "adm:admins", "danger", "lock"),
            B(f"Скрыть ID: {'ВКЛ' if hidden else 'ВЫКЛ'}", "adm:hide:main", "danger" if hidden else "success", "eye"),
        ])
    return text, kb(rows)


def prompt_screen(text):
    return text, kb([[B("Отмена", "adm:cancel", "danger", "cross")]])


def texts_screen():
    rows = [[B(v[0], f"adm:text:{k}", "success", "text")] for k, v in DEFAULT_TEXTS.items()]
    rows.append(BACK("adm:main"))
    return f"{E('text')} <b>Тексты бота</b>\nВыбери, что редактировать:", kb(rows)


def text_screen(key):
    title, vars_, _ = DEFAULT_TEXTS[key]
    preview = render(get_text(key), name="Иван", user_id=123456, chat=987654, text="Пример текста сообщения")
    text = (
        f"{E('text')} <b>{title}</b>\n\n<b>Так выглядит сейчас:</b>\n{preview}\n\n"
        f"<i>Переменные: {html.escape(vars_)}. Премиум-эмодзи слотов: "
        f"{{e_fire}} {{e_star}} {{e_check}} {{e_trash}} {{e_user}} {{e_chat}} … (см. раздел «Эмодзи»).</i>"
    )
    return text, kb([
        [B("Изменить", f"adm:tedit:{key}", "success", "text")],
        [B("Сбросить на стандартный", f"adm:treset:{key}", "danger", "cross")],
        BACK("adm:texts"),
    ])


def btns_screen():
    rows = [[B(b["label"], f"adm:btn:{b['id']}", b.get("style") or None, b.get("icon") or None)]
            for b in get_buttons()]
    rows.append([B("Добавить кнопку", "adm:btnadd", "success", "plus")])
    rows.append(BACK("adm:main"))
    return (f"{E('button')} <b>Кнопки в меню бота</b>\nТак они выглядят у пользователей. "
            f"Нажми на кнопку, чтобы изменить:"), kb(rows)


def btn_screen(bid):
    b = find_btn(bid)
    if not b:
        return btns_screen()
    icon = b.get("icon") or ""
    icon_txt = "не задана" if not icon else ("слот «" + icon[5:] + "»" if icon.startswith("slot:") else "премиум-эмодзи")
    kind = "ссылка" if b["type"] == "url" else "текстовый ответ"
    val = html.escape(b["value"][:300]) if b["type"] == "url" else "(см. ниже)"
    text = (
        f"{E('button')} <b>Кнопка: {html.escape(b['label'])}</b>\n\n"
        f"Тип: {kind}\nЗначение: {val}\nЦвет: {STYLE_NAMES.get(b.get('style') or '')}\n"
        f"Иконка: {icon_txt}\nВ ряд с предыдущей: {'да' if b.get('inline') else 'нет'}"
    )
    if b["type"] == "text":
        text += f"\n\n<b>Ответ:</b>\n{render(b['value'], name='Иван')}"
    i = b["id"]
    return text, kb([
        [B("Название", f"adm:bl:{i}", icon="text"), B("Значение", f"adm:bv:{i}", icon="text")],
        [B("Иконка", f"adm:bi:{i}", icon="star"), B("Убрать иконку", f"adm:bnoi:{i}")],
        [B("Синяя", f"adm:bcol:{i}:primary", "primary"), B("Зелёная", f"adm:bcol:{i}:success", "success")],
        [B("Красная", f"adm:bcol:{i}:danger", "danger"), B("Обычная", f"adm:bcol:{i}:none")],
        [B(f"В ряд с предыдущей: {'да' if b.get('inline') else 'нет'}", f"adm:binl:{i}")],
        [B("⬆️ Выше", f"adm:bup:{i}"), B("⬇️ Ниже", f"adm:bdn:{i}")],
        [B("Удалить", f"adm:bdel:{i}", "danger", "trash")],
        BACK("adm:btns"),
    ])


def photos_screen():
    rows = []
    for k, name in PHOTO_SLOTS.items():
        on = get_setting(f"photo:{k}")
        rows.append([B(f"{name}: {'есть' if on else 'нет'}", f"adm:photo:{k}", "success" if on else "primary", "photo")])
    rows.append(BACK("adm:main"))
    return f"{E('photo')} <b>Фото</b>\nВыбери, где менять картинку:", kb(rows)


def photo_screen(slot):
    on = get_setting(f"photo:{slot}")
    text = (f"{E('photo')} <b>{PHOTO_SLOTS[slot]}</b>\nСейчас: {'есть ✅' if on else 'нет'}\n\n"
            f"<i>Если текст длиннее ~1000 символов, фото отправится отдельным сообщением.</i>")
    rows = [[B("Загрузить новое", f"adm:pset:{slot}", "success", "plus")]]
    if on:
        rows.append([B("Удалить фото", f"adm:pdel:{slot}", "danger", "trash")])
    rows.append(BACK("adm:photos"))
    return text, kb(rows)


def emoji_screen():
    lines = []
    for k, (fb, name) in EMOJI_SLOTS.items():
        lines.append(f"{fb} {name} — {'✅ премиум' if get_setting(f'emoji:{k}') else '▫️ обычный'}")
    text = (
        f"{E('star')} <b>Премиум-эмодзи</b>\n\n" + "\n".join(lines) +
        "\n\n<i>Премиум-эмодзи может отправить только аккаунт с Telegram Premium.</i>"
    )
    rows = [[B("Быстрая настройка (все сразу)", "adm:embulk", "success", "fire")]]
    slots = list(EMOJI_SLOTS)
    for i in range(0, len(slots), 3):
        rows.append([B(EMOJI_SLOTS[k][1], f"adm:emo1:{k}", icon=k) for k in slots[i:i + 3]])
    rows.append([B("Сбросить все", "adm:emreset", "danger", "trash")])
    rows.append(BACK("adm:main"))
    return text, kb(rows)


def admins_screen():
    hidden = is_hidden()
    rows_db = q("SELECT user_id, username, full_name FROM admins ORDER BY added_at")
    lines = [f"{E('crown')} <b>Админы</b>\n"]
    lines.append("Владелец: " + ("••••••" if hidden else f"<code>{OWNER_ID}</code>"))
    kb_rows = []
    for n, (uid, un, name) in enumerate(rows_db, 1):
        if hidden:
            label, line = f"Админ #{n}", f"Админ #{n}: ••••••"
        else:
            label = f"{name or 'Админ'} {'@' + un if un else ''} · {uid}".strip()
            line = f"• {html.escape(name or '—')} {'@' + un if un else ''} · <code>{uid}</code>"
        lines.append(line)
        kb_rows.append([B(f"Убрать: {label}", f"adm:adel:{uid}", "danger", "trash")])
    if not rows_db:
        lines.append("<i>Других админов нет.</i>")
    lines.append(f"\n{E('eye')} Скрытие юзов и ID: <b>{'ВКЛ' if hidden else 'ВЫКЛ'}</b>")
    kb_rows.append([B("Добавить админа", "adm:aadd", "success", "plus")])
    kb_rows.append([B(f"Скрывать юзы и ID: {'ВКЛ' if hidden else 'ВЫКЛ'}", "adm:hide:admins",
                      "danger" if hidden else "success", "eye")])
    kb_rows.append(BACK("adm:main"))
    return "\n".join(lines), kb(kb_rows)


# ───────────────────────── Пользовательская часть ─────────────────────────
def welcome_screen(u):
    return render(get_text("welcome"), name=u.full_name, user_id=u.id), user_kb()


def help_screen(u):
    return render(get_text("help"), name=u.full_name), user_kb()


user_router = Router()


@user_router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message):
    touch_user(m.from_user)
    await show(m, welcome_screen, m.from_user, photo=get_setting("photo:welcome"))


@user_router.message(Command("help"), F.chat.type == "private")
async def cmd_help(m: Message):
    touch_user(m.from_user)
    await show(m, help_screen, m.from_user, photo=get_setting("photo:help"))


@user_router.callback_query(F.data.startswith("ub:"))
async def user_button(cb: CallbackQuery):
    await cb.answer()
    touch_user(cb.from_user)
    b = find_btn(int(cb.data[3:]))
    if b:
        await show(cb, lambda: (render(b["value"], name=cb.from_user.full_name), None))


# ───────────────────────── Бизнес-сообщения ─────────────────────────
biz_router = Router()


@biz_router.business_message()
async def on_business_message(message: Message):
    photo_file_id = message.photo[-1].file_id if message.photo else None
    save_message(
        message.business_connection_id, message.chat.id, message.message_id,
        message.from_user.id if message.from_user else None,
        message.from_user.full_name if message.from_user else "Неизвестно",
        message.text or message.caption, photo_file_id,
    )


@biz_router.deleted_business_messages()
async def on_deleted_business_messages(event):
    rows = get_messages(event.business_connection_id, event.chat.id, event.message_ids)
    q("INSERT INTO deleted_log(ts, chat_id, total, found) VALUES (?,?,?,?)",
      (int(time.time()), event.chat.id, len(event.message_ids), len(rows)), commit=True)

    if not rows:
        await show(OWNER_ID, lambda: (render(get_text("not_found"), chat=event.chat.id), None))
        return
    for _mid, from_name, text, photo_file_id in rows:
        body = (text or "")[:3000]
        await show(
            OWNER_ID,
            lambda: (render(get_text("deleted"), name=from_name, chat=event.chat.id, text=body), None),
            photo=photo_file_id,
        )


# ───────────────────────── Админ-панель: хендлеры ─────────────────────────
class Edit(StatesGroup):
    text = State()
    btn_label = State()
    btn_value = State()
    btn_icon = State()
    nb_label = State()
    nb_type = State()
    nb_value = State()
    photo = State()
    emoji_one = State()
    emoji_bulk = State()
    add_admin = State()


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = event.from_user
        return bool(u) and is_admin(u.id)


admin_router = Router()
admin_router.message.filter(IsAdmin(), F.chat.type == "private")
admin_router.callback_query.filter(IsAdmin())

OWNER_ONLY = {"admins", "hide", "aadd", "adel"}


def custom_emoji_ids(m: Message):
    return [e.custom_emoji_id for e in (m.entities or []) if e.type == "custom_emoji"]


@admin_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    touch_user(m.from_user)
    await show(m, main_screen, m.from_user)


@admin_router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await show(m, main_screen, m.from_user)


@admin_router.callback_query(F.data.startswith("adm:"))
async def admin_cb(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")
    act = p[1]
    u = cb.from_user
    if act in OWNER_ONLY and u.id != OWNER_ID:
        await cb.answer("Только владелец", show_alert=True)
        return
    await cb.answer()

    async def go(fn, *args):
        await show(cb, fn, *args, edit=True)

    if act in ("main", "cancel"):
        await state.clear()
        await go(main_screen, u)
    elif act == "stats":
        await go(stats_screen)
    elif act == "preview":
        await show(cb, welcome_screen, u, photo=get_setting("photo:welcome"))

    # ── тексты
    elif act == "texts":
        await go(texts_screen)
    elif act == "text":
        await go(text_screen, p[2])
    elif act == "tedit":
        await state.set_state(Edit.text)
        await state.update_data(key=p[2])
        await go(prompt_screen, "✏️ Отправь новый текст. Можно форматирование и <b>премиум-эмодзи</b> — "
                                "они сохранятся как есть.")
    elif act == "treset":
        set_setting(f"text:{p[2]}", None)
        await go(text_screen, p[2])

    # ── кнопки
    elif act == "btns":
        await go(btns_screen)
    elif act == "btn":
        await go(btn_screen, int(p[2]))
    elif act == "bcol":
        update_btn(int(p[2]), style="" if p[3] == "none" else p[3])
        await go(btn_screen, int(p[2]))
    elif act == "binl":
        b = find_btn(int(p[2]))
        update_btn(b["id"], inline=not b.get("inline"))
        await go(btn_screen, b["id"])
    elif act in ("bup", "bdn"):
        lst = get_buttons()
        i = next((n for n, b in enumerate(lst) if b["id"] == int(p[2])), None)
        j = i - 1 if act == "bup" else i + 1
        if i is not None and 0 <= j < len(lst):
            lst[i], lst[j] = lst[j], lst[i]
            save_buttons(lst)
        await go(btn_screen, int(p[2]))
    elif act == "bnoi":
        update_btn(int(p[2]), icon="")
        await go(btn_screen, int(p[2]))
    elif act == "bdel":
        save_buttons([b for b in get_buttons() if b["id"] != int(p[2])])
        await go(btns_screen)
    elif act in ("bl", "bv", "bi"):
        st = {"bl": Edit.btn_label, "bv": Edit.btn_value, "bi": Edit.btn_icon}[act]
        hint = {
            "bl": "✏️ Отправь новое название кнопки.",
            "bv": "🔗 Отправь новое значение: ссылку (для кнопки-ссылки) или текст ответа (для текстовой кнопки).",
            "bi": "⭐ Отправь одно <b>премиум-эмодзи</b> — оно станет иконкой кнопки.",
        }[act]
        await state.set_state(st)
        await state.update_data(bid=int(p[2]))
        await go(prompt_screen, hint)
    elif act == "btnadd":
        await state.set_state(Edit.nb_label)
        await go(prompt_screen, "➕ Отправь название новой кнопки.")
    elif act == "nbt":
        await state.update_data(type=p[2])
        await state.set_state(Edit.nb_value)
        await go(prompt_screen, "🔗 Отправь ссылку (https://… или @канал)." if p[2] == "url"
                 else "💬 Отправь текст, который бот покажет при нажатии (можно с премиум-эмодзи).")

    # ── фото
    elif act == "photos":
        await go(photos_screen)
    elif act == "photo":
        await go(photo_screen, p[2])
    elif act == "pset":
        await state.set_state(Edit.photo)
        await state.update_data(slot=p[2])
        await go(prompt_screen, "🖼 Отправь фото.")
    elif act == "pdel":
        set_setting(f"photo:{p[2]}", None)
        await go(photo_screen, p[2])

    # ── эмодзи
    elif act == "emoji":
        await go(emoji_screen)
    elif act == "emo1":
        await state.set_state(Edit.emoji_one)
        await state.update_data(slot=p[2])
        await go(prompt_screen, f"⭐ Отправь одно премиум-эмодзи для слота «{EMOJI_SLOTS[p[2]][1]}».")
    elif act == "embulk":
        order = "\n".join(f"{n}. {fb} {name}" for n, (fb, name) in enumerate(EMOJI_SLOTS.values(), 1))
        await state.set_state(Edit.emoji_bulk)
        await go(prompt_screen, "⚡ Отправь <b>одним сообщением</b> премиум-эмодзи в таком порядке "
                                "(можно меньше — заполнятся первые):\n\n" + order)
    elif act == "emreset":
        for k in EMOJI_SLOTS:
            set_setting(f"emoji:{k}", None)
        await go(emoji_screen)

    # ── админы (только владелец)
    elif act == "admins":
        await go(admins_screen)
    elif act == "hide":
        set_setting("hide_admins", "0" if is_hidden() else "1")
        await go(admins_screen if p[2] == "admins" else main_screen, *([] if p[2] == "admins" else [u]))
    elif act == "aadd":
        await state.set_state(Edit.add_admin)
        await go(prompt_screen, "👤 Отправь ID админа (число) или @username (если он уже запускал бота).")
    elif act == "adel":
        q("DELETE FROM admins WHERE user_id=?", (int(p[2]),), commit=True)
        await go(admins_screen)


# ── приём сообщений в состояниях
@admin_router.message(Edit.text)
async def st_text(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("Нужен текст.")
    key = (await state.get_data())["key"]
    set_setting(f"text:{key}", m.html_text)
    await state.clear()
    await show(m, text_screen, key)


@admin_router.message(Edit.btn_label)
async def st_btn_label(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("Нужен текст.")
    bid = (await state.get_data())["bid"]
    update_btn(bid, label=m.text.strip()[:60])
    await state.clear()
    await show(m, btn_screen, bid)


@admin_router.message(Edit.btn_value)
async def st_btn_value(m: Message, state: FSMContext):
    bid = (await state.get_data())["bid"]
    b = find_btn(bid)
    if not m.text or not b:
        return await m.answer("Нужен текст.")
    if b["type"] == "url":
        url = normalize_url(m.text)
        if not url:
            return await m.answer("❌ Это не похоже на ссылку. Попробуй ещё раз.")
        update_btn(bid, value=url)
    else:
        update_btn(bid, value=m.html_text)
    await state.clear()
    await show(m, btn_screen, bid)


@admin_router.message(Edit.btn_icon)
async def st_btn_icon(m: Message, state: FSMContext):
    ids = custom_emoji_ids(m)
    if not ids:
        return await m.answer("❌ Не вижу премиум-эмодзи. Отправь именно премиум-эмодзи.")
    bid = (await state.get_data())["bid"]
    update_btn(bid, icon=f"id:{ids[0]}")
    await state.clear()
    await show(m, btn_screen, bid)


@admin_router.message(Edit.nb_label)
async def st_nb_label(m: Message, state: FSMContext):
    if not m.text:
        return await m.answer("Нужен текст.")
    await state.update_data(label=m.text.strip()[:60])
    await state.set_state(Edit.nb_type)
    await m.answer("Что должна делать кнопка?", reply_markup=kb([
        [B("Открывать ссылку", "adm:nbt:url", "primary", "rocket")],
        [B("Отвечать текстом", "adm:nbt:text", "success", "chat")],
        [B("Отмена", "adm:cancel", "danger", "cross")],
    ]))


@admin_router.message(Edit.nb_value)
async def st_nb_value(m: Message, state: FSMContext):
    d = await state.get_data()
    if not m.text:
        return await m.answer("Нужен текст.")
    value = m.html_text
    if d["type"] == "url":
        value = normalize_url(m.text)
        if not value:
            return await m.answer("❌ Это не похоже на ссылку. Попробуй ещё раз.")
    lst = get_buttons()
    nid = max([b["id"] for b in lst] + [0]) + 1
    lst.append({"id": nid, "label": d["label"], "style": "primary", "icon": "", "type": d["type"],
                "value": value, "inline": False})
    save_buttons(lst)
    await state.clear()
    await show(m, btn_screen, nid)


@admin_router.message(Edit.photo)
async def st_photo(m: Message, state: FSMContext):
    if not m.photo:
        return await m.answer("❌ Нужно именно фото.")
    slot = (await state.get_data())["slot"]
    set_setting(f"photo:{slot}", m.photo[-1].file_id)
    await state.clear()
    await show(m, photo_screen, slot)


@admin_router.message(Edit.emoji_one)
async def st_emoji_one(m: Message, state: FSMContext):
    ids = custom_emoji_ids(m)
    if not ids:
        return await m.answer("❌ Не вижу премиум-эмодзи. Отправь именно премиум-эмодзи.")
    slot = (await state.get_data())["slot"]
    set_setting(f"emoji:{slot}", ids[0])
    await state.clear()
    await show(m, emoji_screen)


@admin_router.message(Edit.emoji_bulk)
async def st_emoji_bulk(m: Message, state: FSMContext):
    ids = custom_emoji_ids(m)
    if not ids:
        return await m.answer("❌ Не вижу премиум-эмодзи. Отправь их одним сообщением.")
    for slot, cid in zip(EMOJI_SLOTS, ids):
        set_setting(f"emoji:{slot}", cid)
    await state.clear()
    await show(m, emoji_screen)


@admin_router.message(Edit.add_admin)
async def st_add_admin(m: Message, state: FSMContext):
    if m.from_user.id != OWNER_ID or not m.text:
        return
    raw = m.text.strip()
    row = None
    if raw.lstrip("-").isdigit():
        uid = int(raw)
        row = q("SELECT user_id, username, full_name FROM users WHERE user_id=?", (uid,), one=True) or (uid, None, None)
    elif raw.startswith("@"):
        row = q("SELECT user_id, username, full_name FROM users WHERE lower(username)=lower(?)", (raw[1:],), one=True)
    if not row:
        return await m.answer("❌ Не нашёл. Отправь числовой ID или @username человека, который запускал бота.")
    q("INSERT OR REPLACE INTO admins VALUES (?,?,?,?)", (row[0], row[1], row[2], int(time.time())), commit=True)
    await state.clear()
    if is_hidden():  # чтобы ID не светился в переписке
        try:
            await m.delete()
        except TelegramBadRequest:
            pass
    await show(m, admins_screen)


# ───────────────────────── Запуск ─────────────────────────
async def on_startup(bot: Bot):
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("Задайте WEBHOOK_URL или запустите на Render (RENDER_EXTERNAL_URL).")
    webhook_url = f"{base_url.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logging.info(f"Вебхук установлен: {webhook_url}")


async def on_shutdown(bot: Bot):
    await bot.delete_webhook()
    logging.info("Вебхук удалён.")


def main():
    init_db()
    dp.include_router(admin_router)  # админ-панель первой, чтобы FSM-сообщения не перехватывались
    dp.include_router(user_router)
    dp.include_router(biz_router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
