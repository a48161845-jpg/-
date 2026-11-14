import asyncio

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import cb, cmd, mention

_busy = set()  # сообщения, где таймер уже идёт


@cmd(".снос")
async def c_snos(m, args, bot):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💣 Снести всё", callback_data="sn:go")]])
    await m.reply("🏚 <b>Снос</b>\n\nНажмите, чтобы снести всё.", reply_markup=kb)


@cb("sn")
async def on_snos(call, parts, bot):
    msg = call.message
    key = (msg.chat.id, msg.message_id)
    if key in _busy:
        return await call.answer("Уже готовимся…")
    _busy.add(key)
    await call.answer()
    who = mention(call.from_user.id, call.from_user.full_name)
    try:
        for n in (3, 2, 1):
            await msg.edit_text(f"💣 Снос начнётся через <b>{n}</b>… Готовность!")
            await asyncio.sleep(1)
        await msg.edit_text(f"{who}, ты дебильный, сносить нельзя 🤡")
    finally:
        _busy.discard(key)
