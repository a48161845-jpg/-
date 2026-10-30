import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message

import config
import db
import custom, economy, games, helpcmd, lists, moderation, settings, shop, sync  # noqa: F401  (регистрируют команды)
import core
import mtproto
from core import CB, PREFIXES, REG, canon, has_perm, mention

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()


class Track(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        u = event.from_user
        if u and not u.is_bot:
            await db.touch_user(u.id, u.full_name, u.username)
            if event.chat.type in ("group", "supergroup"):
                if await db.add_member(event.chat.id, u.id) and config.team_key(event.chat.id):
                    economy.schedule_refresh(event.bot)
        rep = event.reply_to_message
        if rep and rep.from_user and not rep.from_user.is_bot:
            await db.touch_user(rep.from_user.id, rep.from_user.full_name, rep.from_user.username)
        return await handler(event, data)


dp.message.outer_middleware(Track())


@dp.message()
async def on_message(m: Message, bot: Bot):
    for u in m.new_chat_members or []:
        if u.is_bot:
            continue
        await db.touch_user(u.id, u.full_name, u.username)
        await db.add_member(m.chat.id, u.id)
        t = await db.get_setting(m.chat.id, "greet", "")
        if t:
            await m.answer(t.replace("{name}", mention(u.id, u.full_name)))
    if m.left_chat_member and not m.left_chat_member.is_bot:
        u = m.left_chat_member
        await db.remove_member(m.chat.id, u.id)
        t = await db.get_setting(m.chat.id, "bye", "")
        if t:
            await m.answer(t.replace("{name}", mention(u.id, u.full_name)))

    text = m.text or m.caption
    if not m.from_user:
        return
    if not text:
        await settings.handle_pending(m)  # медиа-ответ при создании своей команды
        return
    token = text.split(None, 1)[0].lower()
    if token.startswith("/"):
        token = token.split("@")[0]
    # команда должна начинаться с префикса (. ! / ,) либо с + или -
    entry = REG.get(canon(token)) if token[0] in PREFIXES + "+-" else None
    if not entry:
        if await settings.handle_pending(m):  # ждём ответ на вопрос из настроек — он важнее триггеров
            return
        await custom.run_custom(m, text, bot)
        return
    settings.PENDING.pop((m.chat.id, m.from_user.id), None)
    fn, perm = entry
    if perm and not await has_perm(m.chat.id, m.from_user.id, perm):
        return
    parts = text.split(None, 1)
    try:
        await fn(m, parts[1] if len(parts) > 1 else "", bot)
    except Exception as e:
        logging.exception("command %s failed", token)
        await m.reply(f"❗ Ошибка: {e}")


@dp.callback_query()
async def on_callback(call: CallbackQuery, bot: Bot):
    parts = (call.data or "").split(":")
    fn = CB.get(parts[0])
    if not fn or not call.message:
        return await call.answer()
    try:
        await fn(call, parts, bot)
    except Exception as e:
        logging.exception("callback failed")
        await call.answer(f"Ошибка: {e}"[:150], show_alert=True)


@dp.chat_join_request()
async def join_req(req: ChatJoinRequest, bot: Bot):
    await sync.on_join_request(req, bot)


@dp.chat_member()
async def chat_member(upd: ChatMemberUpdated, bot: Bot):
    await sync.on_chat_member(upd, bot)


async def main():
    await db.init()
    await db.x("DELETE FROM poll_tpl WHERE name='приз' AND once=1")  # убираем автошаблон из прошлой версии
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    core.BOT = bot
    try:
        if await mtproto.start():
            logging.info("MTProto (Telethon) подключён")
    except Exception:
        logging.exception("MTProto не запустился — работаем без него")
    asyncio.create_task(sync.scheduler(bot))
    asyncio.create_task(sync.sync_members(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
