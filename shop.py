import json
import time

import config
import db
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import cb, cmd, esc, fmt, mention, parse_num
from economy import get_total, refresh_channel, spend_points


def _team(chat_id):
    return config.team_key(chat_id)


@cmd(".магазин")
async def c_shop(m, args, bot):
    items = await db.q("SELECT * FROM shop WHERE chat_id=? ORDER BY id", (m.chat.id,))
    if not items:
        return await m.reply("🛒 Магазин пуст.")
    lines = ["🛒 <b>Магазин:</b>", ""]
    btns = []
    for it in items:
        lines.append(f"<b>{it['id']}. {esc(it['name'])}</b> — {fmt(it['price'])} б. {'🤖' if it['kind'] == 'auto' else '🖐'}")
        if it["descr"]:
            lines.append(f"   {esc(it['descr'])}")
        btns.append(InlineKeyboardButton(text=f"🛒 {it['name']} · {fmt(it['price'])}", callback_data=f"sh:buy:{it['id']}"))
    await m.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)]))


@cmd(".+товар", perm="shop")
async def c_add(m, args, bot):
    head, _, descr = (args or "").partition("|")
    p = head.split(None, 2)
    kinds = {"авто": "auto", "ручной": "manual"}
    price = parse_num(p[1]) if len(p) >= 3 else None
    if len(p) < 3 or p[0].lower() not in kinds or price is None:
        return await m.reply("Формат: <code>.+товар авто 15 Снять варн | описание</code>\n(или <code>ручной</code>)")
    await db.x("INSERT INTO shop(chat_id,kind,price,name,descr) VALUES(?,?,?,?,?)",
               (m.chat.id, kinds[p[0].lower()], price, p[2].strip(), descr.strip()))
    await m.reply("✅ Товар добавлен.")


@cmd(".-товар", perm="shop")
async def c_del(m, args, bot):
    key = (args or "").strip()
    if not key:
        return await m.reply("Формат: <code>.-товар ID или название</code>")
    if key.isdigit():
        await db.x("DELETE FROM shop WHERE chat_id=? AND id=?", (m.chat.id, int(key)))
    else:
        await db.x("DELETE FROM shop WHERE chat_id=? AND lower(name)=lower(?)", (m.chat.id, key))
    await m.reply("🗑 Готово (если товар существовал).")


async def _auto_action(bot, chat_id, uid, name):
    low = name.lower()
    if "варн" in low:
        r = await db.one("SELECT id FROM warns WHERE chat_id=? AND user_id=? AND (until IS NULL OR until>?) "
                         "ORDER BY id DESC LIMIT 1", (chat_id, uid, time.time()))
        if not r:
            return False, "У вас нет активных варнов."
        await db.x("DELETE FROM warns WHERE id=?", (r["id"],))
        return True, "Варн снят."
    if "мут" in low:
        from moderation import FULL
        await bot.restrict_chat_member(chat_id, uid, FULL)
        return True, "Мут снят."
    return None, ""


@cb("sh")
async def on_cb(call, parts, bot):
    chat, u = call.message.chat.id, call.from_user
    it = await db.one("SELECT * FROM shop WHERE id=? AND chat_id=?", (int(parts[2]), chat))
    key = _team(chat)
    if not it or not key:
        return await call.answer("Товар недоступен", show_alert=True)
    if await get_total(u.id) < it["price"]:
        return await call.answer("Недостаточно баллов", show_alert=True)
    who = mention(u.id, u.full_name)
    if it["kind"] == "auto":
        ok, msg = await _auto_action(bot, chat, u.id, it["name"])
        if ok is False:
            return await call.answer(msg, show_alert=True)
        if ok is None:
            note = "Автодействие не распознано — выдаст админ."
        else:
            note = msg
        await spend_points(u.id, key, it["price"])
        await call.message.answer(f"🛒 {who} купил(а) «{esc(it['name'])}» за {fmt(it['price'])}. {note}")
    else:
        await spend_points(u.id, key, it["price"])
        caps = await db.q("SELECT user_id FROM caps WHERE chat_id=?", (chat,))
        admins = " ".join(mention(c["user_id"], "кэп") for c in caps[:5])
        await call.message.answer(f"🛒 {who} купил(а) «{esc(it['name'])}» за {fmt(it['price'])}.\n"
                                  f"🖐 Выдать вручную: {admins or 'админ'}")
    await refresh_channel(bot)
    await call.answer("Покупка совершена ✅")
