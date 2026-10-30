import asyncio
import html
import json
import re
import time
import unicodedata
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from core import cb, cmd, esc, has_perm, mention, resolve_target, user_link
from economy import get_balance

PH = "———"
CAPMARK = "⁣"  # невидимая метка: эта строка — место капитана
KEYCAP = re.compile(r"((?:[0-9]️?⃣)+)")
TAG = re.compile(r"<[^>]+>")
PLACE = re.compile(r"@[A-Za-z0-9_]{2,}|[—–\-]{2,}|—")
_locks = {}


def plain(s):
    return TAG.sub("", s)


def is_cap(line):
    if CAPMARK in line:
        return True
    t = unicodedata.normalize("NFKC", plain(line)).lower()
    return "captain" in t or "капитан" in t


def normalize(tpl):
    """Приводит шаблон к виду, где каждое место = '———'.
    Понимает {cap} (капитан) и {user} (игрок), а также старый формат с 1️⃣ @ник / ———."""
    out = []
    for line in tpl.strip("\n").split("\n"):
        if "{cap}" in line:
            line = line.replace("{cap}", PH, 1).replace("{user}", PH) + CAPMARK
        elif "{user}" in line:
            line = line.replace("{user}", PH)
        elif is_cap(line):
            idx = line.find("➘")
            if idx == -1:
                idx = line.find(":")
            if idx != -1:
                before, after = line[:idx + 1], line[idx + 1:]
                j = after.rfind("⭐")
                tail = after[j:] if j != -1 else ""
                line = before + "  " + PH + "   " + tail
        else:
            m = KEYCAP.search(line)
            if m:
                tail = line[m.end():]
                n = PLACE.search(tail)
                tail = tail[:n.start()] + PH + tail[n.end():] if n else " " + PH + tail
                line = line[:m.end()] + tail
        out.append(line)
    return "\n".join(out)


def count_slots(tpl):
    return sum(l.count(PH) for l in tpl.split("\n") if PH in l and not is_cap(l))


def has_cap(tpl):
    return any(is_cap(l) and PH in l for l in tpl.split("\n"))


def label(u, mode):
    if mode == "username" and u.get("username") and u["id"] not in config.OWNER_IDS:
        return "@" + u["username"]
    return mention(u["id"], u["name"])


def render(tpl, state, mode):
    """Игроки заполняют места по порядку."""
    players = state.get("players", [])
    cap = state.get("captain")
    out, si = [], 0
    for line in tpl.split("\n"):
        if PH in line:
            if is_cap(line):
                line = line.replace(PH, label(cap, mode) if cap else PH, 1)
            else:
                parts = line.split(PH)
                line = parts[0]
                for tail in parts[1:]:
                    line += (label(players[si], mode) if si < len(players) else PH) + tail
                    si += 1
        out.append(line)
    return "\n".join(out)


async def sorted_state(chat_id, state):
    """Выполнившие норму (баланс не ниже нормы) стоят в списке выше."""
    norm = float(await db.get_setting(chat_id, "norm", "0") or 0)
    players = list(state["players"])
    if config.team_key(chat_id) and norm > 0:
        met = {p["id"]: await get_balance(p["id"]) >= norm for p in players}
        players.sort(key=lambda p: not met[p["id"]])
    return {"captain": state.get("captain"), "players": players}


async def full_text(chat_id, row, state):
    return render(row["template"], await sorted_state(chat_id, state), row["mode"])


def is_full(row, state):
    slots = count_slots(row["template"])
    cap_ok = state.get("captain") is not None or not has_cap(row["template"])
    return slots > 0 and len(state["players"]) >= slots and cap_ok


def list_kb(num):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍", callback_data=f"ls:like:{num}"),
        InlineKeyboardButton(text="👎", callback_data=f"ls:dis:{num}"),
        InlineKeyboardButton(text="👑", callback_data=f"ls:cap:{num}"),
    ]])


def setup_kb(num, mode):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("✅ " if mode == "name" else "") + "Имя (ID)", callback_data=f"ls:mn:{num}"),
         InlineKeyboardButton(text=("✅ " if mode == "username" else "") + "@Username", callback_data=f"ls:mu:{num}")],
        [InlineKeyboardButton(text="🆕 Сброс и запуск", callback_data=f"ls:go:{num}")],
    ])


def empty_state():
    return json.dumps({"captain": None, "players": []})


def _has_prefix(name, prefix):
    a = re.sub(r"\s+", "", name or "").lower()
    b = re.sub(r"\s+", "", prefix or "").lower()
    return a.startswith(b)


def _photo_id(msg):
    if msg is not None and msg.photo:
        return msg.photo[-1].file_id
    return None


@cmd("+список", perm="lists")
async def c_create(m, args, bot):
    mt = re.match(r"\s*(\d+)", args or "")
    if not mt:
        return await m.reply("Формат: <code>+список 1</code> и шаблон с новой строки (или ответом на сообщение с шаблоном). "
                             "Можно с картинкой — бот её запомнит.")
    num = int(mt.group(1))
    full = m.html_text or ""
    body = full.split("\n", 1)[1] if "\n" in full else ""
    if not body.strip() and m.reply_to_message:
        body = m.reply_to_message.html_text or ""
    if not body.strip():
        return await m.reply("Нет шаблона: напишите его со следующей строки или ответьте на сообщение с ним.")
    tpl = normalize(body)
    slots = count_slots(tpl)
    if slots == 0:
        return await m.reply("Не нашёл мест в шаблоне. Используйте {user} для игроков и {cap} для капитана.")
    photo = _photo_id(m) or _photo_id(m.reply_to_message)
    await db.x(
        "INSERT INTO lists(chat_id,num,template,state,msg_id,mode,photo) VALUES(?,?,?,?,NULL,'name',?) "
        "ON CONFLICT(chat_id,num) DO UPDATE SET template=excluded.template,state=excluded.state,msg_id=NULL,"
        "photo=COALESCE(excluded.photo, lists.photo)",
        (m.chat.id, num, tpl, empty_state(), photo))
    row = await db.one("SELECT photo FROM lists WHERE chat_id=? AND num=?", (m.chat.id, num))
    await m.reply(f"✅ Список №{num} создан (мест: {slots}, кэп: {'да' if has_cap(tpl) else 'нет'}"
                  f"{', 🖼 картинка сохранена' if row and row['photo'] else ''}).\nВызвать: <code>.список {num}</code>")


@cmd(".список", perm="lists")
async def c_call(m, args, bot):
    mt = re.match(r"\s*(\d+)", args or "")
    if not mt:
        return await m.reply("Формат: <code>.список 1</code>")
    num = int(mt.group(1))
    row = await db.one("SELECT mode FROM lists WHERE chat_id=? AND num=?", (m.chat.id, num))
    if not row:
        return await m.reply("Такого списка нет.")
    await db.x("UPDATE lists SET state=? WHERE chat_id=? AND num=?", (empty_state(), m.chat.id, num))
    await m.answer(f"Настройка списка №{num} (список очищен):", reply_markup=setup_kb(num, row["mode"]))


@cmd("-список", perm="lists")
async def c_delete(m, args, bot):
    mt = re.match(r"\s*(\d+)", args or "")
    if not mt:
        return await m.reply("Формат: <code>-список 1</code>")
    await db.x("DELETE FROM lists WHERE chat_id=? AND num=?", (m.chat.id, int(mt.group(1))))
    await m.reply("🗑 Список удалён.")


@cmd(".лсписок", ".списки", perm="lists")
async def c_all(m, args, bot):
    rows = await db.q("SELECT num, photo FROM lists WHERE chat_id=? ORDER BY num", (m.chat.id,))
    await m.reply("📋 Номера списков: " + (", ".join(f"{r['num']}{'🖼' if r['photo'] else ''}" for r in rows) if rows else "пока нет"))


async def _push(bot, chat, row, state):
    """Обновляет сообщение со списком (подпись или текст), кнопки пропадают у заполненного."""
    text = await full_text(chat, row, state)
    kb = None if is_full(row, state) else list_kb(row["num"])
    order = ("caption", "text") if row["photo"] else ("text", "caption")
    for kind in order:
        try:
            if kind == "caption":
                await bot.edit_message_caption(chat_id=chat, message_id=row["msg_id"], caption=text, reply_markup=kb)
            else:
                await bot.edit_message_text(text, chat_id=chat, message_id=row["msg_id"], reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return


async def refresh_lists(bot, chat):
    """Перерисовывает запущенные списки чата."""
    for row in await db.q("SELECT * FROM lists WHERE chat_id=? AND msg_id IS NOT NULL", (chat,)):
        await _push(bot, chat, row, json.loads(row["state"]))


async def remove_from_lists(bot, chat, uid):
    """Человек вышел из чата — убираем его из списков игр."""
    for row in await db.q("SELECT * FROM lists WHERE chat_id=? AND msg_id IS NOT NULL", (chat,)):
        st = json.loads(row["state"])
        cap = st.get("captain")
        pl = [p for p in st["players"] if p["id"] != uid]
        if len(pl) == len(st["players"]) and not (cap and cap["id"] == uid):
            continue
        st = {"captain": None if cap and cap["id"] == uid else cap, "players": pl}
        await db.x("UPDATE lists SET state=? WHERE chat_id=? AND num=?", (json.dumps(st), chat, row["num"]))
        await _push(bot, chat, row, st)
    for r in await db.q("SELECT num, ids FROM done_lists WHERE chat_id=?", (chat,)):
        ids = [i for i in json.loads(r["ids"]) if i != uid]
        await db.x("UPDATE done_lists SET ids=? WHERE chat_id=? AND num=?", (json.dumps(ids), chat, r["num"]))


async def enforce_prefix(bot, chat):
    """Применяет приставку чата к уже запущенным спискам. Возвращает число убранных людей."""
    prefix = (await db.get_setting(chat, "prefix", "")).strip()
    if not prefix:
        return 0
    removed = 0
    for row in await db.q("SELECT * FROM lists WHERE chat_id=? AND msg_id IS NOT NULL", (chat,)):
        st = json.loads(row["state"])
        cap = st.get("captain")
        pl = [p for p in st["players"] if p["id"] in config.OWNER_IDS or _has_prefix(p["name"], prefix)]
        new_cap = cap if cap and (cap["id"] in config.OWNER_IDS or _has_prefix(cap["name"], prefix)) else None
        n = len(st["players"]) - len(pl) + (1 if cap and not new_cap else 0)
        if not n:
            continue
        removed += n
        st = {"captain": new_cap, "players": pl}
        await db.x("UPDATE lists SET state=? WHERE chat_id=? AND num=?", (json.dumps(st), chat, row["num"]))
        await _push(bot, chat, row, st)
    return removed


async def list_ids(chat_id):
    """Все, кто записан в списки чата: идущие сейчас + набранные, пока не прошла игра."""
    ids = set()
    for r in await db.q("SELECT state FROM lists WHERE chat_id=?", (chat_id,)):
        st = json.loads(r["state"])
        ids |= {p["id"] for p in st.get("players", [])}
        if st.get("captain"):
            ids.add(st["captain"]["id"])
    for r in await db.q("SELECT ids FROM done_lists WHERE chat_id=? AND until>?", (chat_id, time.time())):
        ids |= set(json.loads(r["ids"]))
    return ids


async def in_other_team_list(chat_id, uid):
    key = config.team_key(chat_id)
    if not key:
        return False
    other = config.TEAMS[config.other_team(key)]["chat_id"]
    return bool(other) and uid in await list_ids(other)


async def finish_list(chat, num, st):
    """Список набран: запоминаем состав до конца игры и сбрасываем сам список (шаблон остаётся)."""
    from games import FMT, now
    ids = [p["id"] for p in st["players"]] + ([st["captain"]["id"]] if st.get("captain") else [])
    g = await db.one("SELECT dt FROM games WHERE chat_id=? AND dt>=? ORDER BY dt LIMIT 1", (chat, now().strftime(FMT)))
    if g:
        until = (datetime.strptime(g["dt"], FMT) - now()).total_seconds() + 90 * 60 + time.time()
    else:
        until = time.time() + 12 * 3600
    await db.x("INSERT INTO done_lists(chat_id,num,ids,until) VALUES(?,?,?,?) "
               "ON CONFLICT(chat_id,num) DO UPDATE SET ids=excluded.ids, until=excluded.until",
               (chat, num, json.dumps(ids), until))
    await db.x("UPDATE lists SET state=?, msg_id=NULL WHERE chat_id=? AND num=?", (empty_state(), chat, num))


async def _edit(call, text, kb):
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


async def _post_list(bot, chat, num, row, state):
    text = await full_text(chat, row, state)
    kb = list_kb(num)
    photo = row["photo"]
    if photo:
        # подпись к фото ограничена 1024 символами — если список длинный, шлём фото отдельно
        est = len(html.unescape(plain(text))) + 20 * (count_slots(row["template"]) + 6)
        if est <= 1000:
            return await bot.send_photo(chat, photo, caption=text, reply_markup=kb)
        await bot.send_photo(chat, photo)
    return await bot.send_message(chat, text, reply_markup=kb)


@cb("ls")
async def on_cb(call, parts, bot):
    action, num = parts[1], int(parts[2])
    chat, u = call.message.chat.id, call.from_user
    lock = _locks.setdefault((chat, num), asyncio.Lock())
    async with lock:
        row = await db.one("SELECT * FROM lists WHERE chat_id=? AND num=?", (chat, num))
        if not row:
            return await call.answer("Список удалён", show_alert=True)

        if action in ("mn", "mu", "go"):
            if not await has_perm(chat, u.id, "lists"):
                return await call.answer("Нет прав", show_alert=True)
            if action in ("mn", "mu"):
                mode = "name" if action == "mn" else "username"
                await db.x("UPDATE lists SET mode=? WHERE chat_id=? AND num=?", (mode, chat, num))
                await _edit(call, f"Настройка списка №{num} (список очищен):", setup_kb(num, mode))
                return await call.answer()
            state = json.loads(empty_state())
            msg = await _post_list(bot, chat, num, row, state)
            await db.x("UPDATE lists SET state=?, msg_id=? WHERE chat_id=? AND num=?",
                       (json.dumps(state), msg.message_id, chat, num))
            await _edit(call, f"✅ Список №{num} запущен.", None)
            return await call.answer()

        if not row["msg_id"] or row["msg_id"] != call.message.message_id:
            return await call.answer("Этот список неактивен", show_alert=True)
        st = json.loads(row["state"])
        slots = count_slots(row["template"])
        players, cap = st["players"], st.get("captain")
        me = {"id": u.id, "name": u.full_name, "username": u.username}
        in_pl = any(p["id"] == u.id for p in players)
        is_c = bool(cap and cap["id"] == u.id)

        if action == "dis":
            if not (in_pl or is_c):
                return await call.answer("Вас нет в списке")
            players = [p for p in players if p["id"] != u.id]
            if is_c:
                cap = None
        else:
            prefix = (await db.get_setting(chat, "prefix", "")).strip()
            if prefix and u.id not in config.OWNER_IDS and not _has_prefix(u.full_name, prefix):
                return await call.answer(f"Без приставки встать в список нельзя. Добавьте в имя: {prefix}", show_alert=True)
            if await in_other_team_list(chat, u.id):
                return await call.answer("Вы уже записаны в списке другой команды 🙅", show_alert=True)
            if action == "like":
                if in_pl or is_c:
                    return await call.answer("Вы уже в списке")
                if len(players) >= slots:
                    return await call.answer("Мест нет", show_alert=True)
                players.append(me)
            else:
                if not has_cap(row["template"]):
                    return await call.answer("В списке нет места кэпа")
                if cap and cap["id"] != u.id:
                    return await call.answer("Место кэпа занято", show_alert=True)
                if is_c:
                    cap = None
                else:
                    players = [p for p in players if p["id"] != u.id]
                    cap = me
        st = {"captain": cap, "players": players}
        await db.x("UPDATE lists SET state=? WHERE chat_id=? AND num=?", (json.dumps(st), chat, num))
        full = is_full(row, st)
        try:
            await _edit(call, await full_text(chat, row, st), None if full else list_kb(num))
        except TelegramBadRequest as e:
            if "too long" in str(e).lower():
                await call.answer("Список не помещается в подпись к картинке", show_alert=True)
            else:
                raise
        if full:
            await finish_list(chat, num, st)
        await call.answer()
    try:
        from sync import enforce_team
        asyncio.create_task(enforce_team(bot, chat))
    except Exception:
        pass
