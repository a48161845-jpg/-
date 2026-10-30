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


async def sync_chat(chat_id):
    """Заносит всех участников чата в базу. Возвращает их количество."""
    if not _client:
        return 0
    n = 0
    try:
        async for u in _client.iter_participants(chat_id):
            if getattr(u, "bot", False) or getattr(u, "deleted", False):
                continue
            await db.touch_user(u.id, _name(u), u.username)
            await db.add_member(chat_id, u.id)
            n += 1
    except Exception as e:
        # чаще всего: чат ещё не «виден» клиенту — напишите в нём любое сообщение — повтор будет при ежедневной синхронизации в 00:00 МСК
        log.warning("sync_chat %s failed: %s", chat_id, e)
    return n
