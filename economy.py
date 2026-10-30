import asyncio
import hashlib
import json
import logging
import re

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from core import (cb, cmd, esc, fmt, has_perm, lookup_user, mention, parse_num,
                  resolve_target, user_link, user_name)

log = logging.getLogger("economy")
_lock = asyncio.Lock()
LINE_RE = re.compile(r"^\s*(@\w{3,}|\d{5,})(.*)$")


def parse_value(rest):
    """'- 56' и ': 5' -> разделитель; '-56' (без пробела после минуса) -> отрицательное число."""
    rest = rest.strip()
    m = re.match(r"^[-–—:=]\s+(.+)$", rest) or re.match(r"^[:=]\s*(.+)$", rest)
    if m:
        rest = m.group(1)
    return parse_num(rest)


def parse_grant_lines(text):
    ok, bad = [], []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = LINE_RE.match(line)
        val = parse_value(m.group(2)) if m else None
        if m and val is not None:
            ok.append((m.group(1), val))
        else:
            bad.append(line.strip())
    return ok, bad


async def add_points(uid, delta, home):
    """Единый баланс. home — команда, в чьём чате идёт выдача (используется только если человека ещё нет в базе)."""
    await db.x(
        "INSERT INTO bank(user_id,home,value,absent) VALUES(?,?,?,0) "
        "ON CONFLICT(user_id) DO UPDATE SET value=bank.value+excluded.value",
        (uid, home, delta),
    )


async def get_balance(uid):
    r = await db.one("SELECT value FROM bank WHERE user_id=?", (uid,))
    return r["value"] if r else 0.0


async def get_home(uid):
    """Основная команда человека (где он был изначально) или None."""
    r = await db.one("SELECT home FROM bank WHERE user_id=?", (uid,))
    return r["home"] if r else None


async def spend_points(uid, amount):
    await db.x("UPDATE bank SET value=value-? WHERE user_id=?", (amount, uid))


def _clip(text, n=32):
    text = (text or "—").replace("\n", " ").strip() or "—"
    return text if len(text) <= n else text[: n - 1] + "…"


async def build_blocks():
    """[(команда, [строки])] — все, кто закреплён за командой (основная команда), по убыванию баланса."""
    blocks = []
    for key, t in config.TEAMS.items():
        cid = t["chat_id"]
        rows = await db.q(
            "SELECT b.user_id AS user_id, u.name AS name, u.username AS username, b.value AS value, b.absent AS absent "
            "FROM bank b LEFT JOIN users u ON u.user_id=b.user_id WHERE b.home=? "
            "ORDER BY b.absent ASC, b.value DESC, LOWER(u.name) ASC", (key,))
        norm = await get_norm(cid) if cid else 0
        lines = [f"{t['title']} — {len(rows)}" + (f" · норма {fmt(norm)}" if norm else ""), ""]
        for i, r in enumerate(rows, 1):
            un = f"@{r['username']}" if r["username"] else "—"
            line = f"{i}. {_clip(r['name'])} | {un} | {r['user_id']} - {fmt(r['value'])}"
            line += " 🚪" if r["absent"] else ""
            lines.append(line)
        blocks.append((key, lines))
    return blocks


def chunk(lines, limit=3800):
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur)
    return out


async def _tg(fn, *a, **kw):
    """Вызов Telegram с одной повторной попыткой при flood-лимите."""
    for attempt in range(2):
        try:
            return await fn(*a, **kw)
        except TelegramRetryAfter as e:
            if attempt:
                raise
            await asyncio.sleep(e.retry_after + 1)


async def refresh_channel(bot):
    """Обновляет список баллов в канале с минимумом запросов:
    у каждой команды свои сообщения, неизменившиеся (по хэшу) не трогаются, лишние удаляются."""
    async with _lock:
        ch = config.POINTS_CHANNEL_ID
        legacy = json.loads(await db.get_meta("points_msgs", "[]"))  # сообщения старой версии бота
        for mid in legacy:
            try:
                await bot.delete_message(ch, mid)
            except Exception:
                pass
        if legacy:
            await db.set_meta("points_msgs", "[]")

        state = json.loads(await db.get_meta("points_state", "{}"))
        for key, lines in await build_blocks():
            texts = chunk(lines)
            cur = state.get(key, [])
            new = []
            for i, text in enumerate(texts):
                h = hashlib.md5(text.encode("utf-8")).hexdigest()
                if i < len(cur):
                    if cur[i]["h"] == h:
                        new.append(cur[i])
                        continue
                    try:
                        await _tg(bot.edit_message_text, text, chat_id=ch, message_id=cur[i]["id"], parse_mode=None)
                        new.append({"id": cur[i]["id"], "h": h})
                        continue
                    except TelegramBadRequest as e:
                        if "not modified" in str(e):
                            new.append({"id": cur[i]["id"], "h": h})
                            continue
                        log.info("edit failed (%s), отправляю заново", e)
                m = await _tg(bot.send_message, ch, text, parse_mode=None)
                new.append({"id": m.message_id, "h": h})
            for old in cur[len(texts):]:
                try:
                    await bot.delete_message(ch, old["id"])
                except Exception:
                    pass
            state[key] = new
            await db.set_meta("points_state", json.dumps(state))
        for stale in [k for k in state if k not in config.TEAMS]:  # сообщения от старых версий
            for old in state.pop(stale):
                try:
                    await bot.delete_message(ch, old["id"])
                except Exception:
                    pass
            await db.set_meta("points_state", json.dumps(state))


_pending = None


def schedule_refresh(bot, delay=3.0):
    """Откладывает обновление канала и склеивает серии событий (вход/выход людей) в один запрос."""
    global _pending
    if _pending is not None and not _pending.done():
        return

    async def run():
        await asyncio.sleep(delay)
        try:
            await refresh_channel(bot)
        except Exception:
            log.exception("refresh_channel failed")

    _pending = asyncio.create_task(run())


async def get_norm(chat_id):
    return float(await db.get_setting(chat_id, "norm", "0") or 0)


@cmd(".баллы")
async def c_points(m, args, bot):
    uid, _ = await resolve_target(m, args)
    if uid is None:
        if args.strip():
            return await m.reply("Пользователь не найден (он должен хотя бы раз написать в чате).")
        uid = m.from_user.id
    bal = await get_balance(uid)
    home = await get_home(uid)
    lines = [f"💰 {await user_link(uid)}", f"Баланс: <b>{fmt(bal)}</b>"]
    if home in config.TEAMS:
        t = config.TEAMS[home]
        line = f"Команда: {t['title']}"
        norm = await get_norm(t["chat_id"]) if t["chat_id"] else 0
        if norm:
            line += f" · норма {fmt(norm)} {'✅' if bal >= norm else '❌'}"
        lines.append(line)
    await m.reply("\n".join(lines))


@cmd("+с_баллы", "+c_баллы", perm="economy")
async def c_grant_list(m, args, bot):
    key = config.team_key(m.chat.id)
    if not key:
        return await m.reply("Команда работает только в чате команды.")
    rep = m.reply_to_message
    if not rep or not (rep.text or rep.caption):
        return await m.reply("Ответьте этой командой на сообщение со списком:\n<code>7233257134 - 56\n@n0irxs -56</code>")
    ok, bad = parse_grant_lines(rep.text or rep.caption)
    done, missing = [], []
    for tok, val in ok:
        uid = await lookup_user(tok)
        if uid is None:
            missing.append(tok)
            continue
        await add_points(uid, val, key)
        done.append(f"{await user_name(uid)} ({uid}): {'+' if val > 0 else ''}{fmt(val)}")
    await refresh_channel(bot)
    if done:
        try:
            await bot.send_message(
                config.POINTS_CHANNEL_ID,
                f"📥 Выдача баллов — {config.TEAMS[key]['title']}\n" + "\n".join(done), parse_mode=None)
        except Exception:
            pass
    text = f"✅ Начислено: {len(done)}"
    if missing:
        text += ("\n⚠️ Не найдены: " + ", ".join(esc(x) for x in missing)
                 + "\nПроверьте ник — бот обновляет участников каждый день в 00:00 МСК (для поиска по @нику нужен Telethon).")
    if bad:
        text += "\n⚠️ Не распознаны строки: " + "; ".join(esc(x) for x in bad[:10])
    await m.reply(text)


async def _plus_minus(m, args, bot, sign):
    key = config.team_key(m.chat.id)
    if not key:
        return await m.reply("Команда работает только в чате команды.")
    uid, rest = await resolve_target(m, args)
    amount = parse_num(rest.split()[0]) if rest.split() else None
    if uid is None or amount is None:
        return await m.reply("Формат: <code>+баллы @username 5</code>, <code>+баллы ID 1,5</code> или ответом: <code>+баллы 5</code>")
    await add_points(uid, sign * abs(amount), key)
    await refresh_channel(bot)
    await m.reply(f"{'➕' if sign > 0 else '➖'} {await user_link(uid)}: {'+' if sign > 0 else '-'}{fmt(abs(amount))}. "
                  f"Баланс: <b>{fmt(await get_balance(uid))}</b>")


@cmd("+баллы", perm="economy")
async def c_plus(m, args, bot):
    await _plus_minus(m, args, bot, 1)


@cmd("-баллы", perm="economy")
async def c_minus(m, args, bot):
    await _plus_minus(m, args, bot, -1)


@cmd(".сброс_баллов", perm="economy")
async def c_reset(m, args, bot):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, сбросить", callback_data="eco:reset:y"),
        InlineKeyboardButton(text="Отмена", callback_data="eco:reset:n")]])
    await m.reply("⚠️ Обнулить баланс <b>всех участников</b>?", reply_markup=kb)


@cb("eco")
async def on_cb(call, parts, bot):
    if parts[1] == "reset":
        if not await has_perm(call.message.chat.id, call.from_user.id, "economy"):
            return await call.answer("Нет прав", show_alert=True)
        if parts[2] == "y":
            await db.x("UPDATE bank SET value=0")
            await refresh_channel(bot)
            await call.message.edit_text("🧹 Баллы сброшены.")
        else:
            await call.message.edit_text("Сброс отменён.")
        await call.answer()


@cmd(".норма", perm="economy")
async def c_norm(m, args, bot):
    if not config.team_key(m.chat.id):
        return await m.reply("Команда работает только в чате команды.")
    if not args.strip():
        return await m.reply(f"Текущая норма: <b>{fmt(await get_norm(m.chat.id))}</b>")
    val = parse_num(args.split()[0])
    if val is None:
        return await m.reply("Формат: <code>.норма 2</code>")
    await db.set_setting(m.chat.id, "norm", val)
    await m.reply(f"✅ Норма для этой команды: <b>{fmt(val)}</b>")


async def channel_link(bot):
    if config.POINTS_CHANNEL_LINK:
        return config.POINTS_CHANNEL_LINK
    link = await db.get_meta("points_link")
    if not link:
        try:
            link = (await bot.create_chat_invite_link(config.POINTS_CHANNEL_ID, name="баллы")).invite_link
            await db.set_meta("points_link", link)
        except Exception:
            return ""
    return link


@cmd("!о_баллы")
async def c_link(m, args, bot):
    link = await channel_link(bot)
    await m.reply(f"📊 Баллы команд: {link}" if link else "Ссылка недоступна: добавьте бота админом в канал.")


@cmd(".передать")
async def c_transfer(m, args, bot):
    key = config.team_key(m.chat.id)
    if not key or await db.get_setting(m.chat.id, "transfer", "0") != "1":
        return await m.reply("Передача баллов выключена.")
    uid, rest = await resolve_target(m, args)
    amount = parse_num(rest.split()[0]) if rest.split() else None
    if uid is None or not amount or amount <= 0 or uid == m.from_user.id:
        return await m.reply("Формат: <code>.передать @username 3</code>")
    if await get_balance(m.from_user.id) < amount:
        return await m.reply("Недостаточно баллов.")
    await spend_points(m.from_user.id, amount)
    await add_points(uid, amount, key)
    await refresh_channel(bot)
    await m.reply(f"🔁 {mention(m.from_user.id, m.from_user.full_name)} → {await user_link(uid)}: {fmt(amount)}")
