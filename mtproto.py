"""Необязательный MTProto-клиент (Telethon, вход под токеном бота).

Зачем: Bot API не умеет получать список участников группы и находить человека по @нику,
если тот ни разу не писал боту. Через MTProto бот-админ видит всех участников.
Включается, если в .env заданы API_ID и API_HASH (https://my.telegram.org) и установлен telethon.
"""
import logging

import config
import db

log = logging.getLogger("mtproto")
_client = None


def available():
    return _client is not None


async def start():
    global _client
    if not (config.API_ID and config.API_HASH):
        log.info("API_ID/API_HASH не заданы — MTProto выключен")
        return False
    try:
        from telethon import TelegramClient
    except ImportError:
        log.warning("telethon не установлен (pip install telethon) — MTProto выключен")
        return False
    from telethon.sessions import StringSession
    # сессия хранится в онлайн-базе (meta), а не в файле на диске
    saved = await db.get_meta("mt_session", "")
    _client = TelegramClient(StringSession(saved or None), config.API_ID, config.API_HASH)
    await _client.start(bot_token=config.BOT_TOKEN)
    new = _client.session.save()
    if new != saved:
        await db.set_meta("mt_session", new)
    return True


def _name(ent, fallback=""):
    parts = [getattr(ent, "first_name", None), getattr(ent, "last_name", None)]
    return " ".join(p for p in parts if p) or fallback or str(ent.id)


async def resolve_username(username):
    """@ник -> user_id (и запись в базу). None, если не найден."""
    if not _client:
        return None
    try:
        ent = await _client.get_entity(username.lstrip("@"))
    except Exception:
        return None
    if type(ent).__name__ != "User":
        return None
    await db.touch_user(ent.id, _name(ent, username), getattr(ent, "username", None))
    return ent.id


async def _find_chat(chat_id, link):
    """Бот-клиент «видит» чат только по access_hash. Если его нет в кэше сессии — берём из своей пригласительной ссылки."""
    try:
        return await _client.get_input_entity(chat_id)
    except Exception:
        pass
    if link:
        from telethon.tl.functions.messages import CheckChatInviteRequest
        h = link.rstrip("/").split("/")[-1].lstrip("+")
        res = await _client(CheckChatInviteRequest(h))
        chat = getattr(res, "chat", None)  # ChatInviteAlready: бот уже в чате
        if chat is not None:
            return await _client.get_input_entity(chat)
    raise ValueError("клиент не видит чат: напишите в нём любое сообщение и убедитесь, что бот — админ")


async def _save_session():
    try:
        new = _client.session.save()
        if new != await db.get_meta("mt_session", ""):
            await db.set_meta("mt_session", new)
    except Exception:
        pass


async def sync_chat(chat_id, link=None):
    """Заносит всех участников чата в базу, убирает ушедших. Возвращает (сколько нашли, текст ошибки)."""
    if not _client:
        return 0, "MTProto выключен"
    seen = set()
    try:
        peer = await _find_chat(chat_id, link)
        async for u in _client.iter_participants(peer):
            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                continue
            await db.touch_user(u.id, _name(u), u.username)
            await db.add_member(chat_id, u.id)
            seen.add(u.id)
    except Exception as e:
        log.warning("sync_chat %s failed: %s", chat_id, e)
        return len(seen), str(e)[:150]
    finally:
        await _save_session()
    if seen:  # полный список получен — тех, кого в нём нет, в чате уже нет
        for r in await db.q("SELECT user_id FROM members WHERE chat_id=?", (chat_id,)):
            if r["user_id"] not in seen:
                await db.remove_member(chat_id, r["user_id"])
    return len(seen), ""
