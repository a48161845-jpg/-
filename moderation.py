import json
import time
from datetime import timedelta

from aiogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup

import db
from core import (PERMS, cb, cmd, esc, fmt_dur, get_caps, is_creator, is_owner, mention,
                  parse_duration, resolve_target, user_link, user_name)

FULL = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)
MUTED = ChatPermissions(can_send_messages=False)


async def _log(chat, uid, action, reason, until, by):
    await db.x("INSERT INTO history(chat_id,user_id,action,reason,until,created,by_id) VALUES(?,?,?,?,?,?,?)",
               (chat, uid, action, reason, until, time.time(), by))


async def _parse(m, args, bot, with_dur=True):
    uid, rest = await resolve_target(m, args)
    if uid is None:
        await m.reply("Укажите цель: ответом на сообщение, @ником или ID.")
        return None
    if is_owner(uid) or uid == bot.id:
        await m.reply("Этого пользователя трогать нельзя.")
        return None
    dur = None
    if with_dur:
        t = rest.split(None, 1)
        if t and parse_duration(t[0]):
            dur = parse_duration(t[0])
            rest = t[1] if len(t) > 1 else ""
    return uid, dur, rest.strip()


def _why(reason):
    return f"\nПричина: {esc(reason)}" if reason else ""


ACTIONS = {"warn": "⚠️ варн", "unwarn": "✅ снят варн", "autokick": "🚪 автокик", "mute": "🔇 мут", "unmute": "🔊 размут",
           "kick": "🚪 кик", "ban": "⛔ бан", "unban": "✅ разбан"}


async def _kick(bot, chat, uid):
    await bot.ban_chat_member(chat, uid)
    await bot.unban_chat_member(chat, uid, only_if_banned=True)


async def _active_warns(chat, uid):
    r = await db.one("SELECT COUNT(*) c FROM warns WHERE chat_id=? AND user_id=? AND (until IS NULL OR until>?)",
                     (chat, uid, time.time()))
    return r["c"]


@cmd(".варн", perm="warn")
async def c_warn(m, args, bot):
    p = await _parse(m, args, bot)
    if not p:
        return
    uid, dur, reason = p
    until = time.time() + dur if dur else None
    await db.x("INSERT INTO warns(chat_id,user_id,reason,until,created,by_id) VALUES(?,?,?,?,?,?)",
               (m.chat.id, uid, reason, until, time.time(), m.from_user.id))
    await _log(m.chat.id, uid, "warn", reason, until, m.from_user.id)
    limit = int(await db.get_setting(m.chat.id, "warn_limit", "3") or 3)
    n = await _active_warns(m.chat.id, uid)
    text = f"⚠️ {await user_link(uid)} получает варн ({n}/{limit})" + (f" на {fmt_dur(dur)}" if dur else "") + _why(reason)
    if n >= limit:
        try:
            await _kick(bot, m.chat.id, uid)
            await db.x("DELETE FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
            await _log(m.chat.id, uid, "autokick", "лимит варнов", None, m.from_user.id)
            text += "\n🚪 Лимит варнов — исключён из чата."
        except Exception as e:
            text += f"\n❗ Не удалось кикнуть: {esc(e)}"
    await m.reply(text)


@cmd(".сварн", perm="warn")
async def c_unwarn(m, args, bot):
    p = await _parse(m, args, bot, with_dur=False)
    if not p:
        return
    uid = p[0]
    r = await db.one("SELECT id FROM warns WHERE chat_id=? AND user_id=? AND (until IS NULL OR until>?) ORDER BY id DESC LIMIT 1",
                     (m.chat.id, uid, time.time()))
    if not r:
        return await m.reply("Активных варнов нет.")
    await db.x("DELETE FROM warns WHERE id=?", (r["id"],))
    await _log(m.chat.id, uid, "unwarn", "", None, m.from_user.id)
    await m.reply(f"✅ Последний варн снят с {await user_link(uid)}.")


@cmd(".мут", perm="mute")
async def c_mute(m, args, bot):
    p = await _parse(m, args, bot)
    if not p:
        return
    uid, dur, reason = p
    try:
        await bot.restrict_chat_member(m.chat.id, uid, MUTED, until_date=timedelta(seconds=dur) if dur else None)
    except Exception as e:
        return await m.reply(f"❗ Не удалось: {esc(e)}")
    await _log(m.chat.id, uid, "mute", reason, time.time() + dur if dur else None, m.from_user.id)
    await m.reply(f"🔇 {await user_link(uid)} в муте {'на ' + fmt_dur(dur) if dur else 'навсегда'}.{_why(reason)}")


@cmd(".размут", perm="mute")
async def c_unmute(m, args, bot):
    p = await _parse(m, args, bot, with_dur=False)
    if not p:
        return
    try:
        await bot.restrict_chat_member(m.chat.id, p[0], FULL)
    except Exception as e:
        return await m.reply(f"❗ Не удалось: {esc(e)}")
    await _log(m.chat.id, p[0], "unmute", "", None, m.from_user.id)
    await m.reply(f"🔊 Мут снят с {await user_link(p[0])}.")


@cmd(".кик", perm="kick")
async def c_kick(m, args, bot):
    p = await _parse(m, args, bot, with_dur=False)
    if not p:
        return
    try:
        await _kick(bot, m.chat.id, p[0])
    except Exception as e:
        return await m.reply(f"❗ Не удалось: {esc(e)}")
    await _log(m.chat.id, p[0], "kick", p[2], None, m.from_user.id)
    await m.reply(f"🚪 {await user_link(p[0])} исключён.{_why(p[2])}")


@cmd(".бан", perm="ban")
async def c_ban(m, args, bot):
    p = await _parse(m, args, bot)
    if not p:
        return
    uid, dur, reason = p
    try:
        await bot.ban_chat_member(m.chat.id, uid, until_date=timedelta(seconds=dur) if dur else None)
    except Exception as e:
        return await m.reply(f"❗ Не удалось: {esc(e)}")
    await _log(m.chat.id, uid, "ban", reason, time.time() + dur if dur else None, m.from_user.id)
    await m.reply(f"⛔ {await user_link(uid)} забанен {'на ' + fmt_dur(dur) if dur else 'навсегда'}.{_why(reason)}")


@cmd(".разбан", perm="ban")
async def c_unban(m, args, bot):
    p = await _parse(m, args, bot, with_dur=False)
    if not p:
        return
    try:
        await bot.unban_chat_member(m.chat.id, p[0], only_if_banned=True)
    except Exception as e:
        return await m.reply(f"❗ Не удалось: {esc(e)}")
    await _log(m.chat.id, p[0], "unban", "", None, m.from_user.id)
    await m.reply(f"✅ {await user_link(p[0])} разбанен.")


@cmd(".история", perm="warn")
async def c_history(m, args, bot):
    uid, _ = await resolve_target(m, args)
    if uid is None:
        return await m.reply("Укажите цель: ответом, @ником или ID.")
    rows = await db.q("SELECT * FROM history WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 20", (m.chat.id, uid))
    if not rows:
        return await m.reply("История чистая.")
    lines = [f"📜 История: {await user_link(uid)}"]
    for r in rows:
        t = time.strftime("%d.%m %H:%M", time.localtime(r["created"]))
        lines.append(f"• {t} — {ACTIONS.get(r['action'], r['action'])} {esc(r['reason'] or '')}".rstrip())
    await m.reply("\n".join(lines))


# ---------- кэпы ----------
async def _can_manage(bot, m):
    return is_owner(m.from_user.id) or await is_creator(bot, m.chat.id, m.from_user.id)


@cmd("+кэп")
async def c_add_cap(m, args, bot):
    if not await _can_manage(bot, m):
        return await m.reply("⚠️ Только владелец бота или создатель чата.")
    uid, _ = await resolve_target(m, args)
    if uid is None:
        return await m.reply("Укажите цель: ответом, @ником или ID.")
    await db.x("INSERT OR REPLACE INTO caps(chat_id,user_id,perms) VALUES(?,?,?)", (m.chat.id, uid, json.dumps(list(PERMS))))
    await m.reply(f"👑 {await user_link(uid)} назначен кэпом. Права: <code>.права {uid}</code>")


@cmd("-кэп")
async def c_del_cap(m, args, bot):
    if not await _can_manage(bot, m):
        return await m.reply("⚠️ Только владелец бота или создатель чата.")
    uid, _ = await resolve_target(m, args)
    if uid is None:
        return await m.reply("Укажите цель.")
    await db.x("DELETE FROM caps WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
    await m.reply("✅ Кэп снят.")


@cmd(".кэпы")
async def c_caps(m, args, bot):
    rows = await db.q("SELECT * FROM caps WHERE chat_id=?", (m.chat.id,))
    if not rows:
        return await m.reply("Кэпов нет.")
    lines = ["👑 <b>Кэпы:</b>"]
    for r in rows:
        perms = json.loads(r["perms"])
        lines.append(f"• {await user_link(r['user_id'])}: " + ", ".join(PERMS[p] for p in perms if p in PERMS))
    await m.reply("\n".join(lines))


async def _perm_kb(chat, uid):
    caps = await get_caps(chat, uid) or set()
    btns = [InlineKeyboardButton(text=("✅ " if p in caps else "❌ ") + t, callback_data=f"cp:t:{uid}:{p}")
            for p, t in PERMS.items()]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="💾 Готово", callback_data="cp:ok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@cmd(".права")
async def c_perms(m, args, bot):
    if not await _can_manage(bot, m):
        return await m.reply("⚠️ Только владелец бота или создатель чата.")
    uid, _ = await resolve_target(m, args)
    if uid is None or await get_caps(m.chat.id, uid) is None:
        return await m.reply("Это не кэп. Сначала: <code>+кэп @ник</code>")
    await m.reply(f"🔧 Права кэпа {await user_link(uid)}:", reply_markup=await _perm_kb(m.chat.id, uid))


@cb("cp")
async def on_cp(call, parts, bot):
    chat = call.message.chat.id
    if not (is_owner(call.from_user.id) or await is_creator(bot, chat, call.from_user.id)):
        return await call.answer("Нет прав", show_alert=True)
    if parts[1] == "ok":
        await call.message.edit_text("💾 Права сохранены.")
        return await call.answer()
    uid, perm = int(parts[2]), parts[3]
    caps = await get_caps(chat, uid)
    if caps is None:
        return await call.answer("Кэп не найден", show_alert=True)
    caps ^= {perm}
    await db.x("UPDATE caps SET perms=? WHERE chat_id=? AND user_id=?", (json.dumps(sorted(caps)), chat, uid))
    await call.message.edit_reply_markup(reply_markup=await _perm_kb(chat, uid))
    await call.answer()


@cmd(".сброскэпов")
async def c_reset_caps(m, args, bot):
    if not await _can_manage(bot, m):
        return await m.reply("⚠️ Только владелец бота или создатель чата.")
    await db.x("DELETE FROM caps WHERE chat_id=?", (m.chat.id,))
    await m.reply("🧹 Все кэпы этого чата удалены.")
