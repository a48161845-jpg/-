"""Тихая команда «анрег» (только для создателя): пока режим включён, любые упоминания создателя
(@ник, тег ссылкой, ответ на его сообщение) от любого человека или бота бот сразу удаляет.
Команда ничего никуда не пишет: сообщение с «анрег» просто удаляется. Повторное «анрег» выключает режим."""
import logging
import re

from aiogram import BaseMiddleware
from aiogram.types import Message

import config
import db

log = logging.getLogger("unreg")
_ON = None  # кэш режима


async def is_on():
    global _ON
    if _ON is None:
        _ON = (await db.get_meta("unreg", "0")) == "1"
    return _ON


async def set_on(v):
    global _ON
    _ON = bool(v)
    await db.set_meta("unreg", "1" if v else "0")


async def _owner_names():
    names = set(config.OWNER_USERNAMES)
    for uid in config.OWNER_IDS:
        r = await db.one("SELECT username FROM users WHERE user_id=?", (uid,))
        if r and r["username"]:
            names.add(r["username"].lower())
    return names


def _entities(m):
    return (m.entities or []) + (m.caption_entities or [])


async def mentions_owner(m: Message):
    """Есть ли в сообщении упоминание создателя: @ник, text_mention/ссылка tg://user?id=, ответ на его сообщение."""
    if m.from_user and m.from_user.id in config.OWNER_IDS:
        return False
    rep = m.reply_to_message
    if rep and rep.from_user and rep.from_user.id in config.OWNER_IDS:
        return True
    text = m.text or m.caption or ""
    names = await _owner_names()
    for e in _entities(m):
        if e.type == "mention" and text[e.offset:e.offset + e.length].lstrip("@").lower() in names:
            return True
        if e.type == "text_mention" and e.user and e.user.id in config.OWNER_IDS:
            return True
        if e.type == "text_link" and e.url:
            mm = re.search(r"tg://user\?id=(\d+)", e.url)
            if mm and int(mm.group(1)) in config.OWNER_IDS:
                return True
    if names and re.search(r"@(" + "|".join(map(re.escape, names)) + r")(?!\w)", text, re.I):
        return True
    return False


def _is_unreg_cmd(text):
    t = (text or "").strip().lower()
    if t and t[0] in ".!/,":
        t = t[1:]
    parts = t.split()
    return parts[0] if parts else "", parts[1:]


class UnregMiddleware(BaseMiddleware):
    """Стоит перед остальной обработкой сообщений."""

    async def __call__(self, handler, event: Message, data):
        try:
            if event.from_user and event.from_user.id in config.UNREG_IDS and event.chat.type != "channel":
                word, rest = _is_unreg_cmd(event.text)
                if word == "анрег" and not event.edit_date and (not rest or (len(rest) == 1 and rest[0] in ("вкл", "выкл", "on", "off"))):
                    if rest and rest[0] in ("выкл", "off"):
                        await set_on(False)
                    elif rest and rest[0] in ("вкл", "on"):
                        await set_on(True)
                    else:
                        await set_on(not await is_on())
                    try:
                        await event.delete()  # тихо: никаких ответов, сама команда тоже исчезает
                    except Exception:
                        pass
                    return None
            if await is_on() and await mentions_owner(event):
                try:
                    await event.delete()
                except Exception as e:
                    log.info("delete failed: %s", e)
                return None
        except Exception:
            log.exception("unreg middleware")
        return await handler(event, data)
