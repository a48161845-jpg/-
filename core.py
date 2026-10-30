import html
import json
import re

import config
import db

REG = {}  # команда -> (функция, право)
CB = {}   # префикс callback -> функция


BOT = None  # выставляется в bot.py, нужен для поиска пользователей по @нику
PREFIXES = ".!/,"


def canon(tok):
    """Имя команды без префикса (. ! / ,) и без подчёркиваний: '!о_баллы' == '.обаллы'."""
    tok = (tok or "").lower()
    if tok and tok[0] in PREFIXES:
        tok = tok[1:]
    return tok.replace("_", "")


def cmd(*names, perm=None):
    def deco(fn):
        for n in names:
            REG[canon(n)] = (fn, perm)
        return fn
    return deco


def cb(prefix):
    def deco(fn):
        CB[prefix] = fn
        return fn
    return deco


PERMS = {
    "warn": "Варн", "mute": "Мут", "kick": "Кик", "ban": "Бан",
    "economy": "Экономика", "lists": "Списки", "games": "Игры",
    "shop": "Магазин", "settings": "Настройки",
}
# отдельные галочки в правах кэпа (не права на команды, по умолчанию выключены)
FLAGS = {"shoptag": "Тег при выдаче товаров"}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def fmt(v):
    return f"{round(float(v), 2):g}"


def parse_num(s):
    try:
        return float(str(s).strip().replace(",", ".").replace("−", "-"))
    except ValueError:
        return None


def mention(uid, name):
    if uid in config.OWNER_IDS:  # создателя бот нигде не тегает — просто имя, без уведомления
        return esc(name)
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


async def user_name(uid):
    r = await db.one("SELECT name FROM users WHERE user_id=?", (uid,))
    return r["name"] if r and r["name"] else str(uid)


async def user_link(uid):
    return mention(uid, await user_name(uid))


def is_owner(uid):
    return uid in config.OWNER_IDS


async def get_caps(chat_id, uid):
    r = await db.one("SELECT perms FROM caps WHERE chat_id=? AND user_id=?", (chat_id, uid))
    return set(json.loads(r["perms"])) if r else None


async def has_perm(chat_id, uid, perm):
    if is_owner(uid):
        return True
    if perm == "owner":
        return False
    caps = await get_caps(chat_id, uid)
    return caps is not None and perm in caps


async def is_creator(bot, chat_id, uid):
    try:
        mem = await bot.get_chat_member(chat_id, uid)
        return mem.status == "creator"
    except Exception:
        return False


DUR_RE = re.compile(r"^(\d+)\s*([мmчhдdнw])$", re.I)
UNITS = {"м": 60, "m": 60, "ч": 3600, "h": 3600, "д": 86400, "d": 86400, "н": 604800, "w": 604800}


def parse_duration(tok):
    m = DUR_RE.match(tok or "")
    if not m:
        return None
    return int(m.group(1)) * UNITS[m.group(2).lower()]


def fmt_dur(sec):
    sec = int(sec)
    parts = []
    for name, n in (("д", 86400), ("ч", 3600), ("м", 60)):
        if sec >= n:
            parts.append(f"{sec // n}{name}")
            sec %= n
    return " ".join(parts) or "0м"


async def _resolve_remote(token):
    """Ник не найден в базе — спрашиваем Telegram (Bot API, затем MTProto), писать в чат человеку не нужно."""
    if BOT:
        try:
            ch = await BOT.get_chat(token)
            if ch.type == "private":
                await db.touch_user(ch.id, ch.full_name or token, ch.username)
                return ch.id
        except Exception:
            pass
    try:
        import mtproto
        return await mtproto.resolve_username(token)
    except Exception:
        return None


async def lookup_user(token):
    token = (token or "").strip()
    if token.startswith("@"):
        r = await db.one("SELECT user_id FROM users WHERE username=?", (token[1:].lower(),))
        if r:
            return r["user_id"]
        return await _resolve_remote(token)
    if token.isdigit():
        return int(token)
    return None


async def resolve_target(message, args):
    """Цель: реплей, @username или ID. Возвращает (user_id|None, остаток аргументов)."""
    rep = message.reply_to_message
    if rep and rep.from_user:
        u = rep.from_user
        await db.touch_user(u.id, u.full_name, u.username)
        return u.id, args.strip()
    parts = args.split(None, 1)
    if not parts:
        return None, ""
    uid = await lookup_user(parts[0])
    if uid:
        return uid, parts[1] if len(parts) > 1 else ""
    return None, args
