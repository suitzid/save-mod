import os
import re
import html
import time
import asyncio
import sqlite3
import hashlib
import logging
import ipaddress
from urllib.parse import urlsplit

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
BASE_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("WEBHOOK_BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL", "")
)
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_PATH = "/webhook"
SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
ROOT_ADMIN = 5570425300
CONNECTED_TEXT = "🤖 Бот подключён к автоматизации и следит за твоими чатами."
FOREVER = 4102444800  # 2100-01-01

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
CREATE TABLE IF NOT EXISTS hidden (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS channels (chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chat_state (
    owner INTEGER, chat_id INTEGER, muted_until INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
    PRIMARY KEY (owner, chat_id)
);
CREATE TABLE IF NOT EXISTS owner_settings (
    owner INTEGER PRIMARY KEY, info_all INTEGER DEFAULT 1, scam INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS stats (
    owner INTEGER, key TEXT, n INTEGER DEFAULT 0, PRIMARY KEY (owner, key)
);
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


def is_hidden(uid: int) -> bool:
    return db.execute("SELECT 1 FROM hidden WHERE id=?", (uid,)).fetchone() is not None


def is_enabled() -> bool:
    r = db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()
    return not r or r[0] == "1"


def upsert_user(u: User):
    db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?)", (u.id, u.username, u.full_name))
    db.commit()


# --- состояние чатов (.mute / .ban) ---
def _ensure_chat(owner: int, chat_id: int):
    db.execute("INSERT OR IGNORE INTO chat_state (owner, chat_id) VALUES (?,?)", (owner, chat_id))


def is_muted(owner: int, chat_id: int) -> bool:
    r = db.execute(
        "SELECT muted_until FROM chat_state WHERE owner=? AND chat_id=?", (owner, chat_id)
    ).fetchone()
    return bool(r and r[0] > time.time())


def mute_until(owner: int, chat_id: int) -> int:
    r = db.execute(
        "SELECT muted_until FROM chat_state WHERE owner=? AND chat_id=?", (owner, chat_id)
    ).fetchone()
    return r[0] if r else 0


def set_muted(owner: int, chat_id: int, until: int):
    _ensure_chat(owner, chat_id)
    db.execute("UPDATE chat_state SET muted_until=? WHERE owner=? AND chat_id=?", (until, owner, chat_id))
    db.commit()


def is_chat_banned(owner: int, chat_id: int) -> bool:
    r = db.execute("SELECT banned FROM chat_state WHERE owner=? AND chat_id=?", (owner, chat_id)).fetchone()
    return bool(r and r[0])


def set_chat_banned(owner: int, chat_id: int, val: bool):
    _ensure_chat(owner, chat_id)
    db.execute("UPDATE chat_state SET banned=? WHERE owner=? AND chat_id=?", (int(val), owner, chat_id))
    db.commit()


# --- личные настройки владельца ---
def get_setting(owner: int, key: str) -> bool:
    assert key in ("info_all", "scam")
    r = db.execute(f"SELECT {key} FROM owner_settings WHERE owner=?", (owner,)).fetchone()
    return True if r is None else bool(r[0])


def set_setting(owner: int, key: str, val: bool):
    assert key in ("info_all", "scam")
    db.execute("INSERT OR IGNORE INTO owner_settings (owner) VALUES (?)", (owner,))
    db.execute(f"UPDATE owner_settings SET {key}=? WHERE owner=?", (int(val), owner))
    db.commit()


def bump(owner: int, key: str):
    db.execute(
        "INSERT INTO stats (owner, key, n) VALUES (?,?,1) "
        "ON CONFLICT(owner, key) DO UPDATE SET n = n + 1",
        (owner, key),
    )
    db.commit()


def stat(owner: int, key: str) -> int:
    r = db.execute("SELECT n FROM stats WHERE owner=? AND key=?", (owner, key)).fetchone()
    return r[0] if r else 0


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
    """У скрытых админов не показываем ни ссылку с ID, ни @username — только имя."""
    if is_hidden(uid):
        return html.escape(name or "Скрытый пользователь")
    if username:
        return f"@{username}"
    return f'<a href="tg://user?id={uid}">{html.escape(name or str(uid))}</a>'


def quote(text: str, limit: int = 1500) -> str:
    return f"<blockquote>{html.escape((text or '—')[:limit])}</blockquote>"


async def deliver(owner: int, header: str, text: str, mtype, file_id):
    send = MEDIA_SENDERS.get(mtype)
    has_media = bool(send and file_id)
    caption = header + (f"\n\n{quote(text, 800 if has_media else 3000)}" if text else "")
    if has_media:
        if mtype in ("video_note", "sticker"):
            await send(owner, file_id)
            await bot.send_message(owner, caption)
        else:
            await send(owner, file_id, caption=caption)
    else:
        await bot.send_message(owner, caption)


async def try_delete(conn_id: str, ids: list[int]):
    try:
        await bot.delete_business_messages(conn_id, ids)
    except Exception:
        log.info("delete_business_messages не удалось (нет права у бота?)")


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
            ok = True
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
    if not is_enabled() or is_banned(uid):
        return False
    if is_admin(uid):
        return True
    miss = await missing_channels(uid)
    if miss:
        await send_sub_prompt(uid, miss)
        return False
    return True


# ============================== безопасность: ссылки ==============================
SHORTENERS = {
    "bit.ly", "tinyurl.com", "t.co", "cutt.ly", "clck.ru", "goo.gl", "is.gd", "rebrand.ly",
    "ow.ly", "vk.cc", "shorturl.at", "tiny.cc", "rb.gy", "v.gd", "s.id",
}
IP_LOGGERS = {
    "grabify.link", "iplogger.org", "iplogger.com", "iplogger.ru", "iplogger.co", "2no.co",
    "yip.su", "ipgrabber.ru", "blasze.tk", "leancoding.co", "stopify.co", "freegiftcards.co",
    "joinmy.site", "curiouscat.club", "gyazos.com", "iplis.ru", "ezstat.ru",
}
CLOUD = {
    "mega.nz", "drive.google.com", "mediafire.com", "dropbox.com", "wetransfer.com", "we.tl",
    "gofile.io", "anonfiles.com", "disk.yandex.ru", "yadi.sk", "cloud.mail.ru", "sendspace.com",
    "1drv.ms", "onedrive.live.com", "transfer.sh", "file.io", "pixeldrain.com",
}
FREE_HOSTS = (
    ".workers.dev", ".pages.dev", ".github.io", ".web.app", ".firebaseapp.com", ".vercel.app",
    ".netlify.app", ".herokuapp.com", ".000webhostapp.com", ".weebly.com", ".wixsite.com",
    ".blogspot.com", ".glitch.me", ".repl.co", ".replit.dev", ".ngrok-free.app", ".ngrok.io",
    ".trycloudflare.com",
)
TG_HOSTS = {"t.me", "telegram.me", "telegram.dog", "tx.me"}
SUSP_TLDS = {
    "xyz", "top", "click", "gq", "tk", "ml", "cf", "ga", "icu", "cyou", "buzz", "rest",
    "zip", "mov", "work", "monster", "sbs", "cfd", "lol", "fun", "bid", "loan",
}
BRANDS = {
    "telegram": {"t.me", "telegram.org", "telegram.me", "telegra.ph", "telegram.dog", "tx.me", "telesco.pe"},
    "whatsapp": {"whatsapp.com", "whatsapp.net", "wa.me"},
    "instagram": {"instagram.com"},
    "facebook": {"facebook.com", "fb.com"},
    "paypal": {"paypal.com", "paypal.me"},
    "binance": {"binance.com"},
    "metamask": {"metamask.io"},
    "coinbase": {"coinbase.com"},
    "steam": {"steampowered.com", "steamcommunity.com", "steamstatic.com"},
    "discord": {"discord.com", "discord.gg", "discordapp.com"},
    "tiktok": {"tiktok.com"},
    "netflix": {"netflix.com"},
    "gosuslugi": {"gosuslugi.ru"},
    "sberbank": {"sberbank.ru", "sberbank.com"},
    "tinkoff": {"tinkoff.ru", "tbank.ru"},
    "wildberries": {"wildberries.ru"},
}
BAIT_WORDS = {
    "airdrop", "claim", "giveaway", "wallet", "seed", "recovery", "verify", "verification",
    "login", "signin", "gift", "free", "bonus", "prize", "unlock", "confirm", "secure",
}


def reg_domain(host: str) -> str:
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if len(labels[-1]) == 2 and labels[-2] in {"co", "com", "org", "net", "gov", "edu"}:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def analyze_url(raw: str, label=None):
    """-> (оценка, причины, заметки). Оценка >= 2 — ссылка считается подозрительной."""
    reasons, notes, score = [], [], 0
    raw = (raw or "").strip()
    has_scheme = bool(re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I))
    try:
        p = urlsplit(raw if has_scheme else "http://" + raw)
        host = (p.hostname or "").lower().strip(".")
    except ValueError:
        return 0, [], []
    if not host:
        return 0, [], []

    if has_scheme and p.scheme.lower() == "tg":
        if host in ("proxy", "socks", "login"):
            return 3, [f"ссылка tg://{host} может менять настройки или данные аккаунта"], []
        return 0, [], []

    if has_scheme and p.scheme.lower() == "http":
        score += 1
        reasons.append("нет шифрования (http)")
    if "@" in p.netloc:
        score += 3
        reasons.append("в адресе есть «@» — реальный сайт спрятан после него")
    try:
        ipaddress.ip_address(host)
        score += 3
        reasons.append("вместо домена указан IP-адрес")
    except ValueError:
        pass
    if "xn--" in host or any(ord(c) > 127 for c in host):
        score += 3
        reasons.append("домен с похожими символами (возможна подмена)")
    if _in(host, IP_LOGGERS):
        score += 4
        reasons.append("сервис для слежки по IP: по клику твой IP-адрес попадёт отправителю")
    if host in SHORTENERS:
        score += 2
        reasons.append("сокращённая ссылка — не видно, куда она ведёт")

    tld = host.rsplit(".", 1)[-1]
    if tld in SUSP_TLDS:
        score += 2
        reasons.append(f"подозрительная доменная зона .{tld}")

    rd = reg_domain(host)
    flat = host.replace("-", "")
    for kw, legit in BRANDS.items():
        if kw in flat and rd not in legit:
            score += 3
            reasons.append(f"домен имитирует «{kw}», но не является официальным")
            break

    if host.endswith(FREE_HOSTS):
        score += 1
        reasons.append("бесплатный хостинг — на таких часто прячут фишинг")
    if host.count(".") >= 4:
        score += 1
        reasons.append("слишком много поддоменов")
    if host.count("-") >= 3:
        score += 1
        reasons.append("много дефисов в домене")
    if re.search(r"https?(://|%3a%2f%2f)", p.query.lower()):
        score += 1
        reasons.append("внутри ссылки спрятан другой адрес (перенаправление)")

    tail = (p.path + "?" + p.query).lower()
    words = sorted(w for w in BAIT_WORDS if w in tail or w in host)
    if words:
        score += 1
        reasons.append("слова-приманки в адресе: " + ", ".join(words[:3]))
    if p.path.lower().endswith(tuple("." + e for e in EXEC_EXT)):
        score += 3
        reasons.append("ссылка ведёт на скачивание программы/установщика")

    if label and "." in label and " " not in label.strip() and label.strip().lower() != raw.lower():
        try:
            lh = (urlsplit(label if "://" in label else "http://" + label).hostname or "").lower()
        except ValueError:
            lh = ""
        if lh and "." in lh and reg_domain(lh) != rd:
            score += 4
            reasons.append(f"текст ссылки показывает {lh}, а ведёт она на {host}")

    # --- Telegram-ссылки ---
    if host in TG_HOSTS:
        first = p.path.strip("/").split("/")[0]
        if first in ("proxy", "socks"):
            score += 3
            reasons.append("прокси-ссылка: через чужой прокси могут перехватывать трафик")
        elif first.startswith("+") or first == "joinchat":
            notes.append("приглашение в группу/канал — внутри бывают спам и мошенники")
        elif first == "addlist":
            notes.append("приглашение в папку с чатами")
        elif first.lower().endswith("bot"):
            notes.append("ссылка на бота — не давай ему доступ и данные без нужды")
    if _in(host, CLOUD):
        notes.append("файлообменник: по ссылке может лежать файл с вирусом")
    return score, reasons, notes


def defang(u: str) -> str:
    u = u.replace("http", "hxxp", 1) if u.lower().startswith("http") else u
    return u.replace(".", "[.]")[:140]


# ============================== безопасность: файлы ==============================
EXEC_EXT = {
    "apk", "xapk", "apks", "exe", "msi", "msix", "appx", "bat", "cmd", "com", "scr", "pif", "dll",
    "jar", "vbs", "vbe", "js", "jse", "wsf", "wsh", "ps1", "hta", "cpl", "reg", "lnk", "dmg",
    "pkg", "app", "deb", "rpm", "sh", "run", "bin", "command",
}
MACRO_EXT = {"docm", "xlsm", "pptm", "dotm", "xlam", "xlsb", "ppam"}
DISK_EXT = {"iso", "img", "vhd", "vhdx"}
WEB_EXT = {"html", "htm", "xhtml", "mht", "mhtml"}
ARCHIVE_EXT = {"zip", "rar", "7z", "tar", "gz", "tgz", "cab", "ace", "arj"}
HARMLESS_EXT = {
    "jpg", "jpeg", "png", "gif", "pdf", "doc", "docx", "txt", "xls", "xlsx", "mp3", "mp4",
    "mov", "avi", "heic", "webp", "ppt", "pptx", "csv",
}
DANGER_MIME = {
    "application/vnd.android.package-archive", "application/x-msdownload", "application/x-msdos-program",
    "application/x-msi", "application/x-dosexec", "application/java-archive", "application/x-sh",
    "application/x-bat", "application/x-apple-diskimage", "application/x-executable",
}


def analyze_file(name, mime=None):
    """-> (уровень, причины). 2 — опасный тип, 1 — осторожно, 0 — ок."""
    name = (name or "").strip()
    parts = name.lower().split(".")
    ext = parts[-1] if len(parts) > 1 else ""
    level, reasons = 0, []
    if "\u202e" in name or "\u202d" in name:
        level = 2
        reasons.append("в имени файла спрятан символ, который подменяет расширение")
    if ext in EXEC_EXT:
        level = 2
        reasons.append(f"это программа или установщик (.{ext}) — может содержать вирус")
    elif ext in MACRO_EXT:
        level = 2
        reasons.append(f"документ с макросами (.{ext}) — макросы часто несут вирусы")
    elif ext in DISK_EXT:
        level = 2
        reasons.append(f"образ диска (.{ext}) — внутри может быть программа")
    elif ext in WEB_EXT:
        level = 2
        reasons.append("HTML-файл может открыть поддельную страницу входа")
    elif ext == "svg":
        level = max(level, 1)
        reasons.append("SVG-файл может содержать скрипты")
    elif ext in ARCHIVE_EXT:
        level = max(level, 1)
        reasons.append("архив — внутри может быть вирус, особенно если он с паролем")
    if len(parts) >= 3 and ext in (EXEC_EXT | MACRO_EXT | WEB_EXT) and parts[-2] in HARMLESS_EXT:
        level = 2
        reasons.append("двойное расширение — файл маскируется под безобидный")
    if (mime or "").lower() in DANGER_MIME and level < 2:
        level = 2
        reasons.append("тип файла как у программы")
    return level, reasons


# ============================== безопасность: мошенничество ==============================
def scam_reasons(text: str, has_link: bool = False) -> list[str]:
    t = (text or "").lower()
    r = []
    if (re.search(r"\bкод\w*|\bcode\b", t)
            and re.search(r"смс|sms|телеграм|telegram|подтвержд|входа|авториз|login|verification", t)
            and re.search(r"скин|пришл|отправ|назов|сообщ|продиктуй|напиш|дай\b|send|share|give|tell", t)):
        r.append("просят код из СМС/Telegram — это доступ к твоему аккаунту, никому его не давай")
    if re.search(r"сид[- ]?фраз|seed phrase|recovery phrase|мнемоническ|приватн\w+ ключ|private key", t):
        r.append("речь о сид-фразе или приватном ключе — это полный доступ к крипто-кошельку")
    if re.search(r"номер карты|данные карты|\bcvv\b|\bcvc\b|срок действия карты", t):
        r.append("просят данные банковской карты")
    if (re.search(r"служб\w+ безопасности|сотрудник\w* банка|безопасн\w+ счёт|безопасн\w+ счет|центробанк|следовател", t)
            and re.search(r"перевед|карт|счёт|счет|код", t)):
        r.append("похоже на «сотрудника банка/силовиков» — классическая схема мошенников")
    if re.search(r"(переведи|скинь|одолжи|займи|пополни)\w*\W+.{0,25}(деньги|денег|на карту|на номер|usdt|руб|₽|\$)", t):
        r.append("просьба перевести деньги — убедись голосом или видео, что пишет тот, за кого себя выдаёт")
    if re.search(r"гарантированн\w+ (доход|заработок|прибыль)|удвоим|выиграл\w*.{0,30}(приз|iphone|айфон|подарок)|"
                 r"бесплатн\w+ (telegram )?premium|бесплатн\w+ премиум", t):
        r.append("обещание лёгких денег или приза — типичная приманка")
    if has_link and re.search(r"проголосу\w+|голосов\w+ за|это ты (на )?(фото|видео)|смотри что (про тебя|о тебе)", t):
        r.append("приманка «проголосуй / это ты на фото» + ссылка — типичный фишинг")
    return r


# ============================== проверка сообщения собеседника ==============================
async def scan_message(m: Message, owner: int, old_text=None):
    u = m.from_user
    if not u or u.id == owner:
        return
    who = mention(u.id, u.username, u.full_name)
    text = m.text or m.caption or ""
    info_all = get_setting(owner, "info_all")
    outbox: list[str] = []

    # --- ссылки ---
    links, seen = [], set()
    for e in (m.entities or m.caption_entities or []):
        if e.type == "url":
            url, label = e.extract_from(text), None
        elif e.type == "text_link" and e.url:
            url, label = e.url, e.extract_from(text)
        else:
            continue
        if url in seen or (old_text and url in old_text):
            continue
        seen.add(url)
        links.append((url, label))

    if links:
        lines, has_risk = [f"🔗 {who} отправил ссылку:"], False
        for url, label in links[:3]:
            score, reasons, notes = analyze_url(url, label)
            if score >= 2:
                has_risk = True
                lines.append(f"\n<code>{html.escape(defang(url))}</code>")
                lines.append("⚠️ <b>Выглядит подозрительно:</b>")
                lines += [f"• {html.escape(x)}" for x in reasons[:4]]
            else:
                lines.append(f"\n<code>{html.escape(url[:200])}</code>")
                lines.append("ℹ️ " + html.escape("; ".join(notes)) if notes
                             else "Явных признаков опасности нет, но будь внимателен.")
        if has_risk:
            lines.append("\nНе открывай её и не вводи там данные. Адрес показан в «обезвреженном» виде.")
            bump(owner, "threats")
        if has_risk or info_all:
            bump(owner, "links")
            outbox.append("\n".join(lines))

    # --- файлы ---
    doc = m.document
    if doc:
        level, reasons = analyze_file(doc.file_name, doc.mime_type)
        if level == 2 or (level == 1 and info_all):
            icon = "⚠️ <b>Опасный тип файла</b>" if level == 2 else "ℹ️ Осторожно"
            body = [f"📦 {who} отправил файл:", f"<code>{html.escape(doc.file_name or 'без имени')}</code>", icon]
            body += [f"• {html.escape(x)}" for x in reasons]
            if level == 2:
                body.append("\nНе открывай и не устанавливай, если не ждал этот файл и не уверен в отправителе.")
                bump(owner, "threats")
            outbox.append("\n".join(body))

    # --- мошенничество ---
    if text and get_setting(owner, "scam"):
        reasons = scam_reasons(text, has_link=bool(links))
        if reasons:
            bump(owner, "threats")
            outbox.append(
                f"🚨 {who} — возможное мошенничество:\n{quote(text, 700)}\n"
                + "\n".join(f"• {html.escape(x)}" for x in reasons)
            )

    if not outbox or not await notify_ok(owner):
        return
    for part in outbox:
        try:
            await bot.send_message(owner, part, disable_web_page_preview=True)
        except Exception:
            log.exception("security notice failed")


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
        "👋 Я сохраняю удалённые и изменённые сообщения, а ещё предупреждаю об опасных ссылках, "
        "файлах и мошенниках.\n\n"
        "Подключение: <b>Настройки → Telegram для бизнеса → Чат-боты</b> → "
        "выбери меня и дай доступ к чатам.\n\n" + CONNECTED_TEXT +
        "\n\nКоманды в чатах (с точкой): .help"
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


# ============================== команды в чате ==============================
ALIASES = {
    ".mute": "mute", ".мут": "mute",
    ".unmute": "unmute", ".анмут": "unmute", ".размут": "unmute",
    ".ban": "ban", ".бан": "ban",
    ".unban": "unban", ".разбан": "unban", ".анбан": "unban",
    ".save": "save", ".сохр": "save", ".сохранить": "save",
    ".info": "info", ".инфо": "info",
    ".stats": "stats", ".стата": "stats", ".стат": "stats", ".статистика": "stats",
    ".clear": "clear", ".очистить": "clear",
    ".links": "links", ".ссылки": "links",
    ".scam": "scam", ".скам": "scam", ".антискам": "scam",
    ".ping": "ping", ".пинг": "ping",
    ".help": "help", ".хелп": "help", ".помощь": "help", ".команды": "help",
}

HELP_TEXT = (
    "🛠 <b>Команды</b> — пиши в любом чате с подключённым ботом. "
    "Сообщение с командой бот постарается удалить, ответ придёт сюда.\n\n"
    "<code>.mute</code> / <code>.мут</code> [время] — не присылать уведомления об удалении и правках "
    "в этом чате. Время: <code>.мут 30</code> (мин), <code>2ч</code>, <code>1д</code>; без времени — навсегда\n"
    "<code>.unmute</code> / <code>.размут</code> — вернуть уведомления\n"
    "<code>.ban</code> / <code>.бан</code> — удалять все новые сообщения собеседника в этом чате "
    "(нужно право удалять сообщения в настройках бота)\n"
    "<code>.unban</code> / <code>.разбан</code> — снять бан\n"
    "<code>.save</code> / <code>.сохр</code> — ответом на сообщение: пришлю его сюда\n"
    "<code>.info</code> / <code>.инфо</code> — данные о собеседнике и статус чата\n"
    "<code>.stats</code> / <code>.стата</code> — статистика бота\n"
    "<code>.clear</code> / <code>.очистить</code> — стереть сохранённые сообщения этого чата\n"
    "<code>.links</code> / <code>.ссылки</code> — вкл/выкл уведомления об обычных ссылках и архивах "
    "(подозрительное и опасное приходит всегда)\n"
    "<code>.scam</code> / <code>.скам</code> — вкл/выкл предупреждения о мошенничестве в тексте\n"
    "<code>.ping</code> / <code>.пинг</code> — проверка, что бот жив\n"
    "<code>.help</code> / <code>.помощь</code> — эта справка"
)


def parse_duration(arg: str):
    """'30' -> 1800 сек, '2ч' -> 7200, '1д' -> 86400. None — не понял."""
    r = re.fullmatch(r"(\d{1,5})\s*([a-zа-я]*)", (arg or "").lower())
    if not r:
        return None
    n, unit = int(r.group(1)), r.group(2)
    if unit in ("", "m", "min", "м", "мин"):
        return n * 60
    if unit in ("h", "ч", "час", "часа", "часов"):
        return n * 3600
    if unit in ("d", "д", "дн", "день", "дня", "дней"):
        return n * 86400
    return None


def fmt_until(ts: int) -> str:
    if ts >= FOREVER:
        return "навсегда"
    left = max(0, int(ts - time.time()))
    if left >= 86400:
        return f"ещё {left // 86400} д."
    if left >= 3600:
        return f"ещё {left // 3600} ч."
    return f"ещё {max(1, left // 60)} мин."


async def handle_command(m: Message, owner: int) -> bool:
    parts = (m.text or "").strip().split()
    cmd = ALIASES.get(parts[0].lower())
    if not cmd:
        return False
    arg = parts[1] if len(parts) > 1 else ""
    if not await notify_ok(owner):
        return True
    chat = m.chat
    who = mention(chat.id, chat.username, chat.full_name)
    conn = m.business_connection_id
    reply = None

    if cmd == "mute":
        if arg:
            secs = parse_duration(arg)
            if not secs:
                reply = "Не понял время. Примеры: <code>.мут 30</code>, <code>.мут 2ч</code>, <code>.мут 1д</code>"
            else:
                set_muted(owner, chat.id, int(time.time()) + secs)
                reply = f"🔕 Чат с {who}: уведомления выключены ({fmt_until(int(time.time()) + secs)})."
        else:
            set_muted(owner, chat.id, FOREVER)
            reply = f"🔕 Чат с {who}: уведомления выключены навсегда. Вернуть: <code>.размут</code>"
    elif cmd == "unmute":
        set_muted(owner, chat.id, 0)
        reply = f"🔔 Чат с {who}: уведомления включены."
    elif cmd == "ban":
        set_chat_banned(owner, chat.id, True)
        reply = (f"🚫 {who} заблокирован: его новые сообщения в этом чате будут удаляться.\n"
                 f"Нужно право удалять сообщения у бота в настройках бизнес-чатов.\n"
                 f"Снять: <code>.разбан</code>")
    elif cmd == "unban":
        set_chat_banned(owner, chat.id, False)
        reply = f"✅ {who} разблокирован."
    elif cmd == "info":
        cnt = db.execute(
            "SELECT COUNT(*) FROM msgs WHERE conn_id=? AND chat_id=?", (conn, chat.id)
        ).fetchone()[0]
        id_line = "скрыт" if is_hidden(chat.id) else f"<code>{chat.id}</code>"
        mu = mute_until(owner, chat.id)
        reply = (
            f"👤 <b>Собеседник:</b> {who}\nID: {id_line}\n"
            f"Сохранено сообщений: {cnt}\n"
            f"Уведомления: {('🔕 выкл, ' + fmt_until(mu)) if mu > time.time() else '🔔 вкл'}\n"
            f"Бан: {'🚫 да' if is_chat_banned(owner, chat.id) else 'нет'}"
        )
    elif cmd == "stats":
        saved = db.execute(
            "SELECT COUNT(*) FROM msgs WHERE conn_id IN (SELECT id FROM connections WHERE owner_chat_id=?)",
            (owner,),
        ).fetchone()[0]
        reply = (
            "📊 <b>Статистика</b>\n"
            f"Сохранено сообщений: {saved}\n"
            f"Поймано удалённых: {stat(owner, 'deleted')}\n"
            f"Поймано правок: {stat(owner, 'edited')}\n"
            f"Проверено ссылок: {stat(owner, 'links')}\n"
            f"Угроз найдено: {stat(owner, 'threats')}\n"
            f"Обычные ссылки: {'вкл' if get_setting(owner, 'info_all') else 'выкл'} · "
            f"Антискам: {'вкл' if get_setting(owner, 'scam') else 'выкл'}"
        )
    elif cmd == "clear":
        cur = db.execute("DELETE FROM msgs WHERE conn_id=? AND chat_id=?", (conn, chat.id))
        db.commit()
        reply = f"🧹 Сохранённые сообщения чата с {who} стёрты ({cur.rowcount})."
    elif cmd == "links":
        new = not get_setting(owner, "info_all")
        set_setting(owner, "info_all", new)
        reply = ("🔗 Буду сообщать о каждой ссылке и архиве." if new else
                 "🔗 Обычные ссылки и архивы — молчу. Подозрительное и опасное всё равно пришлю.")
    elif cmd == "scam":
        new = not get_setting(owner, "scam")
        set_setting(owner, "scam", new)
        reply = "🚨 Антискам включён." if new else "🚨 Антискам выключен."
    elif cmd == "ping":
        reply = "🏓 pong — бот работает."
    elif cmd == "help":
        reply = HELP_TEXT
    elif cmd == "save":
        r = m.reply_to_message
        if not r:
            reply = "Ответь командой <code>.save</code> на нужное сообщение."
        else:
            mtype, file_id = extract_media(r)
            text = r.text or r.caption or ""
            if not (mtype or text):
                reply = "Не удалось получить содержимое этого сообщения."
            else:
                try:
                    await deliver(owner, f"💾 Сохранено из чата с {who}:", text, mtype, file_id)
                except Exception:
                    log.exception(".save failed")
                    reply = "Не удалось отправить сохранённое сообщение."

    if reply:
        try:
            await bot.send_message(owner, reply, disable_web_page_preview=True)
        except Exception:
            log.exception("command reply failed")
    await try_delete(conn, [m.message_id])
    return True


# ============================== business ==============================
@dp.business_connection()
async def on_connection(c: BusinessConnection):
    upsert_user(c.user)
    if c.is_enabled:
        db.execute("INSERT OR REPLACE INTO connections VALUES (?, ?)", (c.id, c.user_chat_id))
        db.commit()
        if await notify_ok(c.user_chat_id):
            await bot.send_message(c.user_chat_id, CONNECTED_TEXT + "\n\nКоманды: .help")
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
    u = m.from_user

    if u and u.id == owner and m.text and m.text.startswith("."):
        if await handle_command(m, owner):
            return

    if u and u.id != owner:
        if is_chat_banned(owner, m.chat.id):
            await try_delete(m.business_connection_id, [m.message_id])
            return
        await scan_message(m, owner)

    mtype, file_id = extract_media(m)
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

    if u and u.id != owner:
        await scan_message(m, owner, old_text=row[0] if row else None)
        if not is_muted(owner, m.chat.id) and await notify_ok(owner):
            old = row[0] if row else None
            await bot.send_message(
                owner,
                f"✏️ {mention(u.id, u.username, u.full_name)} изменил сообщение:\n\n"
                f"Было:\n{quote(old) if old else '<i>не сохранено</i>'}\n"
                f"Стало:\n{quote(new_text)}",
            )
            bump(owner, "edited")

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
    if not owner or is_muted(owner, d.chat.id) or not await notify_ok(owner):
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
        try:
            await deliver(owner, f"🗑 {mention(uid, username, name)} удалил сообщение:", text, mtype, file_id)
            bump(owner, "deleted")
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


class IsRoot(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id == ROOT_ADMIN)


class A(StatesGroup):
    add_admin = State()
    del_admin = State()
    ban = State()
    unban = State()
    broadcast = State()
    bc_confirm = State()
    add_channel = State()


PROMPTS = {
    "add_admin": ("➕ Пришли ID, @username или перешли сообщение нового админа.", A.add_admin),
    "del_admin": ("➖ Пришли ID, @username или перешли сообщение админа, которого убрать.", A.del_admin),
    "ban": ("🚫 Пришли ID, @username или перешли сообщение пользователя для блокировки.", A.ban),
    "unban": ("✅ Пришли ID, @username или перешли сообщение пользователя для разблокировки.", A.unban),
    "add_channel": (
        "➕ Пришли @username канала, ссылку t.me/... или ID (-100...).\n"
        "Бот должен быть админом в этом канале.", A.add_channel),
}


def user_label(uid: int, viewer: int) -> str:
    r = db.execute("SELECT username, name FROM users WHERE id=?", (uid,)).fetchone()
    hid = is_hidden(uid)
    if hid and viewer != ROOT_ADMIN:
        return f"🕶 {html.escape(r[1]) if r and r[1] else 'Скрытый админ'}"
    name = html.escape(r[1]) if r and r[1] else "без имени"
    un = f" @{r[0]}" if r and r[0] else ""
    return f"{name}{un} <code>{uid}</code>" + (" 🕶" if hid else "") + (" 👑" if uid == ROOT_ADMIN else "")


def ref(uid: int, viewer: int) -> str:
    if is_hidden(uid) and viewer != ROOT_ADMIN:
        return "Пользователь"
    return f"<code>{uid}</code>"


def panel_text() -> str:
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    banned = db.execute("SELECT COUNT(*) FROM banned").fetchone()[0]
    chans = db.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    admins = db.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    return (
        "⚙️ <b>Админ-панель</b>\n\n"
        f"Статус: {'🟢 включён' if is_enabled() else '🔴 выключен'}\n"
        f"Пользователей: {users}\nАдминов: {admins}\n"
        f"Заблокировано: {banned}\nКаналов подписки: {chans}"
    )


def menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить админа", callback_data="adm:ask:add_admin")
    b.button(text="➖ Удалить админа", callback_data="adm:ask:del_admin")
    b.button(text="🚫 Заблокировать", callback_data="adm:ask:ban")
    b.button(text="✅ Разблокировать", callback_data="adm:ask:unban")
    b.button(text="📢 Рассылка", callback_data="adm:bc")
    b.button(text="📌 Обяз. подписка", callback_data="adm:subs")
    b.button(text="👥 Админы", callback_data="adm:admins")
    b.button(
        text="🔴 Выключить бота" if is_enabled() else "🟢 Включить бота",
        callback_data="adm:toggle",
    )
    b.adjust(2, 2, 2, 2)
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
            await ev.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest:
            pass
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def show_menu(ev):
    await show(ev, panel_text(), menu_kb())


async def send_menu(chat_id: int):
    await bot.send_message(chat_id, panel_text(), reply_markup=menu_kb())


async def show_admins(ev):
    viewer = ev.from_user.id
    ids = [r[0] for r in db.execute("SELECT id FROM admins")]
    lst = "\n".join(f"• {user_label(i, viewer)}" for i in ids)
    b = InlineKeyboardBuilder()
    text = f"👥 <b>Админы</b>\n\n{lst}"
    if viewer == ROOT_ADMIN:
        text += ("\n\n🕶 — у такого админа другие админы не видят ID и @username. "
                 "Кнопки ниже переключают это.")
        for i in ids:
            r = db.execute("SELECT name FROM users WHERE id=?", (i,)).fetchone()
            nm = (r[0] if r and r[0] else str(i))[:24]
            b.button(
                text=(f"👁 Показать: {nm}" if is_hidden(i) else f"🕶 Скрыть: {nm}"),
                callback_data=f"adm:hide:{i}",
            )
    b.button(text="⬅️ Назад", callback_data="adm:menu")
    b.adjust(1)
    await show(ev, text, b.as_markup())


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


BAD_INPUT = "Не понял. Пришли ID, @username или перешли сообщение. /cancel — отмена"


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


@dp.callback_query(IsAdmin(), F.data == "adm:admins")
async def cb_admins(cb: CallbackQuery):
    await show_admins(cb)


@dp.callback_query(IsRoot(), F.data.startswith("adm:hide:"))
async def cb_hide(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    if is_admin(uid):
        if is_hidden(uid):
            db.execute("DELETE FROM hidden WHERE id=?", (uid,))
        else:
            db.execute("INSERT OR IGNORE INTO hidden VALUES (?)", (uid,))
        db.commit()
    await show_admins(cb)


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


async def done(m: Message, state: FSMContext, text: str, to=None):
    await state.clear()
    await m.answer(text)
    await (to or show_menu)(m)


@dp.message(IsAdmin(), A.add_admin)
async def st_add_admin(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer(BAD_INPUT)
    db.execute("INSERT OR IGNORE INTO admins VALUES (?)", (uid,))
    db.commit()
    try:
        await bot.send_message(uid, "⚙️ Тебя назначили админом. Панель: /admin")
    except Exception:
        pass
    await done(m, state, f"✅ {ref(uid, m.from_user.id)} теперь админ.")


@dp.message(IsAdmin(), A.del_admin)
async def st_del_admin(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer(BAD_INPUT)
    if uid == ROOT_ADMIN:
        return await done(m, state, "⛔️ Главного админа удалить нельзя.")
    db.execute("DELETE FROM admins WHERE id=?", (uid,))
    db.execute("DELETE FROM hidden WHERE id=?", (uid,))
    db.commit()
    await done(m, state, f"✅ {ref(uid, m.from_user.id)} больше не админ.")


@dp.message(IsAdmin(), A.ban)
async def st_ban(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer(BAD_INPUT)
    if is_admin(uid):
        return await done(m, state, "⛔️ Админа заблокировать нельзя. Сначала убери из админов.")
    db.execute("INSERT OR IGNORE INTO banned VALUES (?)", (uid,))
    db.commit()
    await done(m, state, f"🚫 {ref(uid, m.from_user.id)} заблокирован.")


@dp.message(IsAdmin(), A.unban)
async def st_unban(m: Message, state: FSMContext):
    uid = get_target(m)
    if not uid:
        return await m.answer(BAD_INPUT)
    db.execute("DELETE FROM banned WHERE id=?", (uid,))
    db.commit()
    await done(m, state, f"✅ {ref(uid, m.from_user.id)} разблокирован.")


@dp.message(IsAdmin(), A.add_channel)
async def st_add_channel(m: Message, state: FSMContext):
    r = (m.text or "").strip()
    r = re.sub(r"^(https?://)?t\.me/", "@", r)
    if re.fullmatch(r"-?\d+", r):
        r = int(r)
    try:
        chat = await bot.get_chat(r)
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
    await done(m, state, f"✅ Канал «{html.escape(chat.title or '')}» добавлен.", to=show_subs)


# ---------- рассылка: кнопка → сообщение → превью → подтверждение ----------
def bc_recipients() -> list[int]:
    return [r[0] for r in db.execute("SELECT id FROM users WHERE id NOT IN (SELECT id FROM banned)")]


@dp.callback_query(IsAdmin(), F.data == "adm:bc")
async def cb_bc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(A.broadcast)
    try:
        await cb.message.edit_text(
            "📢 <b>Рассылка</b>\n\n"
            f"Получат: {len(bc_recipients())} чел. — все, кто запускал бота или подключал его "
            "(кроме заблокированных).\n\n"
            "1) Пришли мне сообщение для рассылки — любое: текст, фото, видео, с форматированием.\n"
            "2) Я покажу, как оно будет выглядеть.\n"
            "3) Нажмёшь «Отправить» — и оно уйдёт всем.\n\n"
            "/cancel — отмена"
        )
    except TelegramBadRequest:
        pass
    await cb.answer()


@dp.message(IsAdmin(), A.broadcast)
async def st_broadcast(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, message_id=m.message_id)
    await state.set_state(A.bc_confirm)
    await m.copy_to(m.chat.id)  # превью
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить", callback_data="adm:bc_send")
    b.button(text="❌ Отмена", callback_data="adm:menu")
    b.adjust(2)
    await m.answer(f"👆 Так увидят получатели. Отправить {len(bc_recipients())} чел.?", reply_markup=b.as_markup())


@dp.callback_query(IsAdmin(), A.bc_confirm, F.data == "adm:bc_send")
async def cb_bc_send(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    ids = bc_recipients()
    await cb.answer()
    try:
        await cb.message.edit_text(f"📢 Рассылка запущена ({len(ids)} получателей)...")
    except TelegramBadRequest:
        pass
    ok = fail = 0
    for uid in ids:
        for _ in range(2):
            try:
                await bot.copy_message(uid, data["chat_id"], data["message_id"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    await bot.send_message(cb.from_user.id, f"📢 Рассылка завершена.\nДоставлено: {ok}\nНе доставлено: {fail}")
    await send_menu(cb.from_user.id)


# ============================== запуск ==============================
async def on_startup(bot: Bot):
    url = f"{BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
    types = dp.resolve_used_update_types()
    await bot.set_webhook(url, secret_token=SECRET, allowed_updates=types, drop_pending_updates=True)
    log.info("Webhook set: %s | allowed: %s", url, types)


def main():
    if not BASE_URL:
        raise RuntimeError("Нет RENDER_EXTERNAL_URL / WEBHOOK_URL")
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
