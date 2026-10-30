import json
import time

import config
import db
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import cb, cmd, esc, fmt, mention, parse_num, user_name
from economy import get_balance, refresh_channel, spend_points


def _team(chat_id):
    return config.team_key(chat_id)


def is_auto(name):
    """Автоматически бот выдаёт только «снять варн», всё остальное выдают вручную."""
    return "варн" in (name or "").lower()


async def _items(chat_id):
    """Товары чата по порядку. Номер товара = место в списке, поэтому нумерация всегда идёт с 1."""
    return await db.q("SELECT * FROM shop WHERE chat_id=? ORDER BY id", (chat_id,))


@cmd(".магазин")
async def c_shop(m, args, bot):
    items = await _items(m.chat.id)
    if not items:
        return await m.reply("🛒 Магазин пуст.")
    lines = ["🛒 <b>Магазин:</b>", ""]
    btns = []
    for n, it in enumerate(items, 1):
        lines.append(f"<b>{n}. {esc(it['name'])}</b> — {fmt(it['price'])} б. {'🤖' if is_auto(it['name']) else '🖐'}")
        if it["descr"]:
            lines.append(f"   {esc(it['descr'])}")
        btns.append(InlineKeyboardButton(text=f"{n}. {it['name']} · {fmt(it['price'])}"[:60], callback_data=f"sh:buy:{it['id']}"))
    await m.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[btns[i:i + 2] for i in range(0, len(btns), 2)]))


@cmd(".+товар", perm="shop")
async def c_add(m, args, bot):
    head, _, descr = (args or "").partition("|")
    p = head.split(None, 1)
    if p and p[0].lower() in ("авто", "ручной"):  # старый формат: тип больше не нужен
        p = head.split(None, 2)[1:]
    price = parse_num(p[0]) if len(p) >= 2 else None
    if price is None or price <= 0:
        return await m.reply("Формат: <code>.+товар 15 Снять варн | описание</code>\n"
                             "Автоматически бот выдаёт только «снять варн», остальные товары выдают вручную.")
    await db.x("INSERT INTO shop(chat_id,kind,price,name,descr) VALUES(?,?,?,?,?)",
               (m.chat.id, "auto" if is_auto(p[1]) else "manual", price, p[1].strip(), descr.strip()))
    await m.reply(f"✅ Товар №{len(await _items(m.chat.id))} добавлен.")


@cmd(".-товар", perm="shop")
async def c_del(m, args, bot):
    key = (args or "").strip()
    if not key:
        return await m.reply("Формат: <code>.-товар номер или название</code>")
    items = await _items(m.chat.id)
    if key.isdigit():
        n = int(key)
        target = items[n - 1] if 1 <= n <= len(items) else None
    else:
        target = next((it for it in items if it["name"].lower() == key.lower()), None)
    if not target:
        return await m.reply("Такого товара нет. Номера смотрите в <code>.магазин</code>")
    await db.x("DELETE FROM shop WHERE id=?", (target["id"],))
    await m.reply(f"🗑 Товар «{esc(target['name'])}» удалён. Остальные пронумерованы заново.")


async def _unwarn(chat_id, uid):
    r = await db.one("SELECT id FROM warns WHERE chat_id=? AND user_id=? AND (until IS NULL OR until>?) "
                     "ORDER BY id DESC LIMIT 1", (chat_id, uid, time.time()))
    if not r:
        return False
    await db.x("DELETE FROM warns WHERE id=?", (r["id"],))
    return True


async def _shop_tags(chat_id):
    """Кого тегать при ручной выдаче: только кэпов с включённым «Тег при выдаче» в правах."""
    out = []
    for c in await db.q("SELECT user_id, perms FROM caps WHERE chat_id=?", (chat_id,)):
        if "shoptag" in json.loads(c["perms"]) and c["user_id"] not in config.OWNER_IDS:
            out.append(mention(c["user_id"], await user_name(c["user_id"])))
    return out


@cb("sh")
async def on_cb(call, parts, bot):
    chat, u = call.message.chat.id, call.from_user
    it = await db.one("SELECT * FROM shop WHERE id=? AND chat_id=?", (int(parts[2]), chat))
    key = _team(chat)
    if not it or not key:
        return await call.answer("Товар недоступен", show_alert=True)
    if await get_balance(u.id) < it["price"]:
        return await call.answer("Недостаточно баллов", show_alert=True)
    who = mention(u.id, u.full_name)
    if is_auto(it["name"]):
        if not await _unwarn(chat, u.id):
            return await call.answer("У вас нет активных варнов.", show_alert=True)
        await spend_points(u.id, it["price"])
        await call.message.answer(f"🛒 {who} купил(а) «{esc(it['name'])}» за {fmt(it['price'])}. Варн снят.")
    else:
        await spend_points(u.id, it["price"])
        tags = await _shop_tags(chat)
        await call.message.answer(f"🛒 {who} купил(а) «{esc(it['name'])}» за {fmt(it['price'])}.\n"
                                  f"🖐 Выдать вручную: {' '.join(tags) if tags else 'админ'}")
    await refresh_channel(bot)
    await call.answer("Покупка совершена ✅")
