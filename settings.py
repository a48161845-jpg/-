import asyncio
import json
import time

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as KB

import db
from core import cb, cmd, esc, fmt, has_perm, mention, parse_num
from games import KINDS, PALETTE

PENDING = {}  # (chat, user) -> (action, arg, ts)
TTL = 180

# ключ -> (тип, подсказка)
SIMPLE = {
    "prefix": ("str", "Отправьте новую приставку (например: Midnight |)"),
    "norm": ("num", "Отправьте норму баллов (число)"),
    "warn_limit": ("int", "Отправьте лимит варнов (число)"),
    "kick_window": ("int", "За сколько минут до игры кикать из чата второй команды (число)"),
    "tag_limit": ("int", "Сколько упоминаний в одном сообщении (число)"),
    "tag_delay": ("num", "Задержка между сообщениями с тегами, секунд (число)"),
}


def kb(rows):
    return KB(inline_keyboard=rows)


def onoff(v):
    return "✅ Вкл" if v else "❌ Выкл"


async def S(chat, key, default=""):
    return await db.get_setting(chat, key, default)


# ---------- экраны ----------
def grid(btns, n=2):
    return [btns[i:i + n] for i in range(0, len(btns), n)]


async def main_screen(chat):
    return "⚙️ <b>Настройки чата:</b>\n\nВыберите раздел:", kb(grid([
        B(text="💰 Экономика", callback_data="st:menu:eco"), B(text="📅 Календарь", callback_data="st:menu:cal"),
        B(text="📊 Опросы", callback_data="st:menu:polls"), B(text="🏷 Теги", callback_data="st:menu:tags"),
        B(text="📝 Тексты", callback_data="st:menu:texts"), B(text="✖️ Закрыть", callback_data="st:close")]))


async def eco_screen(chat):
    tr = await S(chat, 'transfer', '0') == '1'
    aa = await S(chat, 'auto_approve', '1') == '1'
    t = (f"💰 <b>Экономика и модерация</b>\n\n• Норма баллов: {fmt(float(await S(chat, 'norm', '0') or 0))}\n"
         f"• Приставка: {esc(await S(chat, 'prefix', '—') or '—')}\n"
         f"• Перевод баллов: {onoff(tr)}\n"
         f"• Лимит варнов: {await S(chat, 'warn_limit', '3')}\n"
         f"• Кик из чата 2-й команды за: {await S(chat, 'kick_window', '60')} мин до игры\n"
         f"• Заявки на вступление: {'авто-принятие ✅' if aa else 'вручную ❌'}")
    return t, kb(grid([
        B(text="✏️ Приставка", callback_data="st:ask:prefix"), B(text="✏️ Норма", callback_data="st:ask:norm"),
        B(text=f"Перевод: {'✅' if tr else '❌'}", callback_data="st:tog:transfer"),
        B(text=f"Варны: {await S(chat, 'warn_limit', '3')}", callback_data="st:ask:warn_limit"),
        B(text="⏱ Окно кика", callback_data="st:ask:kick_window"),
        B(text=f"Заявки: {'авто ✅' if aa else 'вручную'}", callback_data="st:tog:auto_approve"),
        B(text="⬅️ Назад", callback_data="st:menu:main")]))


async def cal_screen(chat):
    kind = await S(chat, "game_kind", "simple")
    cols = {k: await S(chat, f"col_{k}", "Стандарт") for k in ("simple", "bt", "mixed", "past")}
    t = (f"📅 <b>Календарь и игры</b>\n\n• Тип игры по умолч.: {KINDS[kind]}\n• Цвет простых: {cols['simple']}\n"
         f"• Цвет ВТ: {cols['bt']}\n• Цвет смешанных: {cols['mixed']}\n• Цвет прошедших: {cols['past']}")
    return t, kb(grid([
        B(text=f"Тип: {KINDS[kind]}", callback_data="st:kind"), B(text="🎨 Простые", callback_data="st:col:simple"),
        B(text="🎨 ВТ", callback_data="st:col:bt"), B(text="🎨 Смеш.", callback_data="st:col:mixed"),
        B(text="🎨 Прошл.", callback_data="st:col:past"), B(text="⬅️ Назад", callback_data="st:menu:main")]))


async def polls_screen(chat):
    ch = await S(chat, "poll_channel", "")
    auto, queue = await S(chat, "auto_polls", "1") == "1", await S(chat, "queue", "0") == "1"
    t = (f"📢 <b>Канал и опросы</b>\n\n• Привязанный канал: {ch or 'не привязан'}\n• Авто-опросы: {onoff(auto)}\n"
         f"• Режим очереди: {onoff(queue)}\n\nОпросы шлются за N часов до игры по таймерам.")
    return t, kb(grid([
        B(text="🔗 Канал", callback_data="st:ask:poll_channel"), B(text="⏰ Таймеры", callback_data="st:timers"),
        B(text="📝 Шаблоны", callback_data="st:tpls"), B(text=f"Авто: {'✅' if auto else '❌'}", callback_data="st:tog:auto_polls"),
        B(text=f"Очередь: {'✅' if queue else '❌'}", callback_data="st:tog:queue"),
        B(text="⬅️ Назад", callback_data="st:menu:main")]))


async def tpls_screen(chat):
    rows = await db.q("SELECT name FROM poll_tpl WHERE chat_id=? ORDER BY rowid", (chat,))
    btns = [B(text=r["name"], callback_data=f"st:tpl:{r['name']}") for r in rows]
    btns += [B(text="➕ Новый", callback_data="st:ask:tpl_new"), B(text="⬅️ Назад", callback_data="st:menu:polls")]
    return "📝 <b>Редактор шаблонов:</b>\nВыберите шаблон для изменения:", kb(grid(btns))


async def tpl_screen(chat, name):
    r = await db.one("SELECT * FROM poll_tpl WHERE chat_id=? AND name=?", (chat, name))
    if not r:
        return await tpls_screen(chat)
    opts = json.loads(r["options"])
    t = (f"✏️ <b>{esc(name)}</b>\n❓ Вопрос: {esc(r['question'])}\nТип: {'🎯 Викторина' if r['quiz'] else '📊 Обычный'}\n"
         f"Anon: {'✅' if r['anon'] else '❌'} | Multi: {'✅' if r['multi'] else '❌'}\n"
         + "\n".join(f"{i + 1}. {esc(o)}" for i, o in enumerate(opts)))
    n = name
    return t, kb(grid([
        B(text="Имя", callback_data=f"st:ask:tplname:{n}"), B(text="Вопрос", callback_data=f"st:ask:tplq:{n}"),
        B(text="📊 Обычный" if r["quiz"] else "🎯 Викторина", callback_data=f"st:tq:{n}"),
        B(text="📋 Варианты", callback_data=f"st:ask:tplo:{n}"),
        B(text=f"Anon: {'✅' if r['anon'] else '❌'}", callback_data=f"st:ta:{n}"),
        B(text=f"Multi: {'✅' if r['multi'] else '❌'}", callback_data=f"st:tm:{n}"),
        B(text="🗑 Удалить", callback_data=f"st:tdel:{n}"), B(text="⬅️ Назад", callback_data="st:tpls")]))


async def timers_screen(chat):
    tm = [x for x in (await S(chat, "timers", "")).split(",") if x.strip()]
    btns = [B(text=f"❌ {x} ч", callback_data=f"st:tmdel:{i}") for i, x in enumerate(tm)]
    btns += [B(text="➕ Добавить", callback_data="st:ask:timer_add"), B(text="🔴 Сбросить", callback_data="st:tmclear"),
             B(text="⬅️ Назад", callback_data="st:menu:polls")]
    return "⏰ <b>Таймеры опросов</b>\n" + ("\n".join(f"📌 За {x} ч" for x in tm) or "Таймеров нет."), kb(grid(btns))


async def tags_screen(chat):
    t = f"🏷 <b>Теги</b>\n\n• Лимит упоминаний: {await S(chat, 'tag_limit', '5')}\n• Задержка: {await S(chat, 'tag_delay', '2')} с\n\nКоманда: <code>.тег текст</code>"
    return t, kb(grid([B(text="Лимит", callback_data="st:ask:tag_limit"), B(text="Задержка", callback_data="st:ask:tag_delay"),
                       B(text="⬅️ Назад", callback_data="st:menu:main")]))


async def texts_screen(chat):
    return ("📝 <b>Настройка текстов</b>\n\nВыберите категорию.\nВ тексте можно использовать {name}.",
            kb(grid([B(text="💬 Приветствие", callback_data="st:ask:text_greet"), B(text="👋 Прощание", callback_data="st:ask:text_bye"),
                     B(text="📜 Правила", callback_data="st:ask:text_rules"), B(text="⬅️ Назад", callback_data="st:menu:main")])))


SCREENS = {"main": main_screen, "eco": eco_screen, "cal": cal_screen, "polls": polls_screen,
           "tags": tags_screen, "texts": texts_screen}


@cmd(".настройки", "/settings", perm="settings")
async def c_settings(m, args, bot):
    t, k = await main_screen(m.chat.id)
    await m.answer(t, reply_markup=k)


async def _show(call, screen):
    t, k = screen
    try:
        await call.message.edit_text(t, reply_markup=k)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


@cb("st")
async def on_cb(call, parts, bot):
    chat, uid = call.message.chat.id, call.from_user.id
    if not await has_perm(chat, uid, "settings"):
        return await call.answer("Нет прав", show_alert=True)
    a = parts[1]
    if a == "close":
        await call.message.delete()
        return await call.answer()
    if a == "menu":
        await _show(call, await SCREENS[parts[2]](chat))
    elif a == "tog":
        key = parts[2]
        dflt = "1" if key in ("auto_polls", "auto_approve") else "0"
        await db.set_setting(chat, key, "0" if await S(chat, key, dflt) == "1" else "1")
        await _show(call, await (polls_screen if key in ("auto_polls", "queue") else eco_screen)(chat))
    elif a == "kind":
        order = list(KINDS)
        cur = await S(chat, "game_kind", "simple")
        await db.set_setting(chat, "game_kind", order[(order.index(cur) + 1) % len(order)])
        await _show(call, await cal_screen(chat))
    elif a == "col":
        names = list(PALETTE)
        cur = await S(chat, f"col_{parts[2]}", "Стандарт")
        await db.set_setting(chat, f"col_{parts[2]}", names[(names.index(cur) + 1) % len(names)])
        await _show(call, await cal_screen(chat))
    elif a == "tpls":
        await _show(call, await tpls_screen(chat))
    elif a == "tpl":
        await _show(call, await tpl_screen(chat, parts[2]))
    elif a in ("ta", "tm", "tq"):
        col = {"ta": "anon", "tm": "multi", "tq": "quiz"}[a]
        await db.x(f"UPDATE poll_tpl SET {col}=1-{col} WHERE chat_id=? AND name=?", (chat, parts[2]))
        if a == "tq":
            r = await db.one("SELECT quiz FROM poll_tpl WHERE chat_id=? AND name=?", (chat, parts[2]))
            if r and r["quiz"]:
                PENDING[(chat, uid)] = ("tplc", parts[2], time.time())
                await call.message.answer("🎯 Викторина: отправьте номер правильного варианта (1, 2, ...).")
        await _show(call, await tpl_screen(chat, parts[2]))
    elif a == "tdel":
        await db.x("DELETE FROM poll_tpl WHERE chat_id=? AND name=?", (chat, parts[2]))
        await _show(call, await tpls_screen(chat))
    elif a == "timers":
        await _show(call, await timers_screen(chat))
    elif a == "tmdel":
        tm = [x for x in (await S(chat, "timers", "")).split(",") if x.strip()]
        i = int(parts[2])
        if i < len(tm):
            tm.pop(i)
        await db.set_setting(chat, "timers", ",".join(tm))
        await _show(call, await timers_screen(chat))
    elif a == "tmclear":
        await db.set_setting(chat, "timers", "")
        await _show(call, await timers_screen(chat))
    elif a == "ask":
        action = parts[2]
        arg = parts[3] if len(parts) > 3 else None
        hints = {k: v[1] for k, v in SIMPLE.items()}
        hints.update({
            "poll_channel": "Перешлите сообщение из канала, либо отправьте его ID (-100…) или @username. Бот должен быть админом канала.",
            "tpl_new": "Отправьте название нового шаблона (до 20 символов).",
            "tplname": "Отправьте новое название шаблона.",
            "tplq": "Отправьте вопрос. Можно {time}, {date}, {desc}.",
            "tplo": "Отправьте варианты ответа, каждый с новой строки (2–10).",
            "timer_add": "За сколько часов до игры слать опрос? (число, например 10 или 1,5)",
            "text_greet": "Отправьте текст приветствия ({name} — имя участника).",
            "text_bye": "Отправьте текст прощания ({name} — имя участника).",
            "text_rules": "Отправьте текст правил."})
        PENDING[(chat, uid)] = (action, arg, time.time())
        await call.message.answer(f"✍️ {hints.get(action, 'Отправьте значение')}\n<i>Для отмены напишите: отмена</i>")
    await call.answer()


async def handle_pending(m):
    key = (m.chat.id, m.from_user.id)
    p = PENDING.get(key)
    if not p:
        return False
    action, arg, ts = p
    if time.time() - ts > TTL:
        PENDING.pop(key, None)
        return False
    text = (m.text or m.caption or "").strip()
    PENDING.pop(key, None)
    chat = m.chat.id
    if text.lower() in ("отмена", "cancel"):
        await m.reply("Отменено.")
        return True
    if action in SIMPLE:
        typ = SIMPLE[action][0]
        val = text
        if typ in ("num", "int"):
            n = parse_num(text)
            if n is None or n < 0:
                await m.reply("Нужно число. Откройте .настройки и попробуйте снова.")
                return True
            val = int(n) if typ == "int" else n
        await db.set_setting(chat, action, val)
        await m.reply("✅ Сохранено.")
    elif action == "poll_channel":
        cid = None
        fo = getattr(m, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None):
            cid = fo.chat.id
        elif text.lstrip("-").isdigit():
            cid = int(text)
        elif text.startswith("@"):
            try:
                cid = (await m.bot.get_chat(text)).id
            except Exception:
                cid = None
        if cid is None:
            await m.reply("Не удалось определить канал.")
        else:
            await db.set_setting(chat, "poll_channel", cid)
            await m.reply(f"✅ Канал привязан: {cid}")
    elif action == "tpl_new":
        name = text[:20]
        if not name or await db.one("SELECT 1 FROM poll_tpl WHERE chat_id=? AND name=?", (chat, name)):
            await m.reply("Пустое имя или такой шаблон уже есть.")
        else:
            await db.x("INSERT INTO poll_tpl(chat_id,name,question,options) VALUES(?,?,?,?)",
                       (chat, name, "{time} Starlight Team vs Midnight Team", json.dumps(["✅ Буду", "❌ Не буду"], ensure_ascii=False)))
            t, k = await tpl_screen(chat, name)
            await m.answer(t, reply_markup=k)
    elif action == "tplname":
        await db.x("UPDATE poll_tpl SET name=? WHERE chat_id=? AND name=?", (text[:20], chat, arg))
        await m.reply("✅ Переименовано.")
    elif action == "tplq":
        await db.x("UPDATE poll_tpl SET question=? WHERE chat_id=? AND name=?", (text[:300], chat, arg))
        await m.reply("✅ Вопрос сохранён.")
    elif action == "tplo":
        opts = [o.strip()[:100] for o in text.splitlines() if o.strip()][:10]
        if len(opts) < 2:
            await m.reply("Нужно минимум 2 варианта.")
        else:
            await db.x("UPDATE poll_tpl SET options=? WHERE chat_id=? AND name=?", (json.dumps(opts, ensure_ascii=False), chat, arg))
            await m.reply("✅ Варианты сохранены.")
    elif action == "tplc":
        n = parse_num(text)
        await db.x("UPDATE poll_tpl SET correct=? WHERE chat_id=? AND name=?", (max(int(n or 1) - 1, 0), chat, arg))
        await m.reply("✅ Правильный вариант сохранён.")
    elif action == "timer_add":
        n = parse_num(text)
        if not n or n <= 0:
            await m.reply("Нужно положительное число.")
        else:
            tm = [x for x in (await S(chat, "timers", "")).split(",") if x.strip()]
            tm.append(f"{n:g}")
            await db.set_setting(chat, "timers", ",".join(tm))
            await m.reply(f"✅ Таймер: за {n:g} ч до игры.")
    elif action.startswith("text_"):
        await db.set_setting(chat, action[5:], m.html_text)
        await m.reply("✅ Текст сохранён.")
    return True


# ---------- тексты ----------
async def _show_text(m, key, title):
    t = await S(m.chat.id, key, "")
    await m.answer(t.replace("{name}", mention(m.from_user.id, m.from_user.full_name)) if t else f"{title} не настроено (.настройки → Тексты).")


@cmd(".правила")
async def c_rules(m, args, bot):
    await _show_text(m, "rules", "Правила")


@cmd(".приветствие")
async def c_greet(m, args, bot):
    await _show_text(m, "greet", "Приветствие")


@cmd(".прощание")
async def c_bye(m, args, bot):
    await _show_text(m, "bye", "Прощание")


async def _del_text(m, key):
    await db.del_setting(m.chat.id, key)
    await m.reply("🗑 Текст удалён.")


@cmd("-правила", perm="settings")
async def d_rules(m, args, bot):
    await _del_text(m, "rules")


@cmd("-приветствие", perm="settings")
async def d_greet(m, args, bot):
    await _del_text(m, "greet")


@cmd("-прощание", perm="settings")
async def d_bye(m, args, bot):
    await _del_text(m, "bye")


# ---------- теги ----------
@cmd(".тег", perm="settings")
async def c_tag(m, args, bot):
    limit = max(int(float(await S(m.chat.id, "tag_limit", "5"))), 1)
    delay = float(await S(m.chat.id, "tag_delay", "2"))
    rows = await db.q("SELECT m.user_id, u.name FROM members m LEFT JOIN users u ON u.user_id=m.user_id WHERE m.chat_id=?", (m.chat.id,))
    people = [r for r in rows if r["user_id"] != bot.id]
    if not people:
        return await m.reply("Пока никого не знаю в этом чате.")
    text = esc(args or "Общий сбор!")
    for i in range(0, len(people), limit):
        part = " ".join(mention(r["user_id"], r["name"] or "·") for r in people[i:i + limit])
        await m.answer(f"{text}\n{part}")
        await asyncio.sleep(delay)


# ---------- приставка ----------
@cmd(".приставка")
async def c_prefix(m, args, bot):
    a = (args or "").strip()
    cur = (await db.get_setting(m.chat.id, "prefix", "")).strip()
    if not a:
        if cur:
            return await m.reply(f"🏷 Приставка этого чата: <b>{esc(cur)}</b>\nБез неё в имени встать в список нельзя.")
        return await m.reply("Приставка не задана — встать в список может любой.\nЗадать: <code>.приставка Midnight |</code>")
    if not await has_perm(m.chat.id, m.from_user.id, "settings"):
        return await m.reply("Менять приставку могут только кэпы и владелец.")
    if a.lower() in ("-", "сброс", "нет", "выкл", "удалить"):
        await db.del_setting(m.chat.id, "prefix")
        return await m.reply("🗑 Приставка снята — ограничение на вход в список выключено.")
    await db.set_setting(m.chat.id, "prefix", a[:30])
    await m.reply(f"✅ Приставка: <b>{esc(a[:30])}</b>\nТеперь в список встанут только те, чьё имя в Telegram начинается с неё.")


@cmd("-приставка", perm="settings")
async def d_prefix(m, args, bot):
    await db.del_setting(m.chat.id, "prefix")
    await m.reply("🗑 Приставка снята.")
