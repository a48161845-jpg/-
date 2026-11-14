"""ЧС команд.

• `.чс` (ответом / @ником / ID) — человек попадает в чёрный список команды этого чата: бот сохраняет данные
  (id, имя, ник, причина, кто добавил, дата), банит его в чате (вход закрыт навсегда) и пишет ему в личку,
  что он в ЧС команды.
• `.массчс` — то же сразу в обе команды.
• `-чс` — убрать из ЧС этой команды (и разбанить).
• Если ЧС получила его ОСНОВНАЯ команда и он состоит в другой, то через 7 дней другая команда автоматически
  становится основной (баллы и место в списке в канале переезжают вместе с ней).
• Если после ЧС человек не состоит ни в одной команде — бот предлагает вступить в команду, где у него нет ЧС.
• Заявки на вступление и повторный вход людей из ЧС бот отклоняет сам.
"""
import asyncio
import logging
import time

from aiogram.types import LinkPreviewOptions

import config
import db
from core import cmd, esc, get_caps, hide_owner_tags, is_owner, resolve_target, user_link

log = logging.getLogger("blacklist")

WEEK = 7 * 86400
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _teams():
    return [k for k, t in config.TEAMS.items() if t["chat_id"]]


def _title(key):
    return config.TEAMS[key]["title"]


async def is_blacklisted(key, uid):
    return bool(await db.one("SELECT 1 FROM blacklist WHERE team=? AND user_id=?", (key, uid)))


async def blacklisted_keys(uid):
    return [k for k in _teams() if await is_blacklisted(k, uid)]


async def _present(bot, chat, uid):
    """True/False — состоит ли человек в чате; None — выяснить не удалось."""
    try:
        mem = await bot.get_chat_member(chat, uid)
    except Exception:
        return None
    if mem.status in ("left", "kicked"):
        return False
    if mem.status == "restricted" and not getattr(mem, "is_member", True):
        return False
    return True


async def _dm(bot, uid, text):
    try:
        await bot.send_message(uid, text, link_preview_options=NO_PREVIEW)
        return True
    except Exception:
        return False


def notice(keys):
    if len(keys) > 1:
        names = " и ".join(_title(k) for k in keys)
        return f"🚫 Вы находитесь в чёрном списке команд {names}. Вход в их чаты для вас закрыт навсегда."
    return f"🚫 Вы находитесь в чёрном списке команды {_title(keys[0])}. Вход в этот чат для вас закрыт навсегда."


async def _offer(bot, uid):
    """Команда, где у человека нет ЧС, и ссылка на неё: (key, link) или None."""
    from sync import team_link
    for key in _teams():
        if await is_blacklisted(key, uid):
            continue
        try:
            link = await team_link(bot, key)
        except Exception:
            continue
        if link:
            return key, link
    return None


def _offer_text(key, link):
    return (f"Вы можете вступить в команду {_title(key)}: {link}\n"
            "Вход по заявке. С 20:00 до 21:00 (МСК) вход закрыт — заявку бот примет после 21:00.")


async def blacklist_user(bot, uid, keys, by_id, reason):
    """Кладёт человека в ЧС команд keys. Возвращает строки отчёта для чата."""
    from lists import remove_from_lists
    from economy import schedule_refresh

    u = await db.one("SELECT name, username FROM users WHERE user_id=?", (uid,))
    name = (u["name"] if u and u["name"] else "") or ""
    uname = u["username"] if u else None
    b = await db.one("SELECT home FROM bank WHERE user_id=?", (uid,))
    home = b["home"] if b else ""

    out = []
    for key in keys:
        chat = config.TEAMS[key]["chat_id"]
        had = await is_blacklisted(key, uid)
        await db.x(
            "INSERT INTO blacklist(team,user_id,name,username,reason,by_id,created,was_home,moved) "
            "VALUES(?,?,?,?,?,?,?,?,0) ON CONFLICT(team,user_id) DO UPDATE SET "
            "reason=CASE WHEN excluded.reason<>'' THEN excluded.reason ELSE blacklist.reason END, "
            "name=COALESCE(NULLIF(excluded.name,''), blacklist.name), "
            "username=COALESCE(excluded.username, blacklist.username)",
            (key, uid, name, uname, reason or "", by_id, time.time(), 1 if home == key else 0))
        try:
            await bot.ban_chat_member(chat, uid)  # бан навсегда: по ссылке в чат уже не зайти
            out.append(f"⛔ {_title(key)}: вход закрыт навсегда" + (" (уже был в ЧС — данные обновлены)" if had else ""))
        except Exception as e:
            out.append(f"❗ {_title(key)}: не удалось забанить ({esc(e)}), но в ЧС записан — при заявке/входе бот отклонит")
        await db.remove_member(chat, uid)
        if home == key:
            await db.x("UPDATE bank SET absent=1 WHERE user_id=? AND home=?", (uid, key))
        try:
            await remove_from_lists(bot, chat, uid)
        except Exception:
            log.exception("remove_from_lists failed")

    all_bl = await blacklisted_keys(uid)
    text = notice(all_bl)
    remaining = [k for k in _teams() if k not in all_bl]
    present_in = None
    for k in remaining:
        if await _present(bot, config.TEAMS[k]["chat_id"], uid):
            present_in = k
            break

    offer = None
    if present_in and any(home == k for k in keys):
        out.append(f"🔁 Основная команда была {_title(home)} — через 7 дней основной автоматически станет "
                   f"{_title(present_in)} (там он уже состоит).")
    elif remaining and present_in is None:
        offer = await _offer(bot, uid)
        if offer:
            text += "\n\n" + _offer_text(*offer)

    if await _dm(bot, uid, text):
        out.append("✉️ В личку отправлено: он в ЧС команды" + (f"; предложено вступить в {_title(offer[0])}" if offer else ""))
    else:
        out.append("✉️ В личку не доставить (человек не запускал бота)"
                   + (f" — передайте ему ссылку на {_title(offer[0])}: {offer[1]}" if offer else ""))

    schedule_refresh(bot)
    return out


# ---------- команды ----------
def _here(m):
    return config.team_key(m.chat.id)


async def _target(m, args, bot):
    uid, rest = await resolve_target(m, args)
    if uid is None:
        await m.reply("Укажите цель: ответом на сообщение, @ником или ID.")
        return None
    if is_owner(uid) or uid == bot.id or uid == m.from_user.id:
        await m.reply("Создателя, бота и себя в ЧС добавлять нельзя 😎")
        return None
    for k in _teams():
        if await get_caps(config.TEAMS[k]["chat_id"], uid) is not None:
            await m.reply("Это кэп команды. Сначала снимите кэпа: <code>-кэп</code>.")
            return None
    return uid, rest.strip()


def _why(reason):
    return f"\nПричина: {hide_owner_tags(esc(reason))}" if reason else ""


async def _show(m, key):
    rows = await db.q("SELECT * FROM blacklist WHERE team=? ORDER BY created DESC LIMIT 50", (key,))
    if not rows:
        return await m.reply(f"ЧС {_title(key)} пуст.")
    lines = [f"🚫 <b>ЧС {_title(key)}</b> ({len(rows)})"]
    for r in rows:
        who = esc(r["name"] or "без имени") + (f" (@{esc(r['username'])})" if r["username"] else "")
        d = time.strftime("%d.%m.%Y", time.localtime(r["created"]))
        lines.append(f"• {who} — <code>{r['user_id']}</code>, {d}" + (f" — {hide_owner_tags(esc(r['reason']))}" if r["reason"] else ""))
    await m.reply("\n".join(lines))


@cmd(".чс", perm="ban")
async def c_bl(m, args, bot):
    key = _here(m)
    if not key:
        return await m.reply("ЧС работает только в чатах команд.")
    if not (args or "").strip() and not m.reply_to_message:
        return await _show(m, key)
    p = await _target(m, args, bot)
    if not p:
        return
    uid, reason = p
    lines = await blacklist_user(bot, uid, [key], m.from_user.id, reason)
    await m.reply(f"🚫 {await user_link(uid)} добавлен в ЧС команды {_title(key)}.\n"
                  f"Данные сохранены: ID <code>{uid}</code>, имя, ник, причина, кто добавил, дата.{_why(reason)}\n"
                  + "\n".join(lines))


@cmd(".массчс", ".масчс", ".мчс", perm="ban")
async def c_mass_bl(m, args, bot):
    if not _here(m):
        return await m.reply("ЧС работает только в чатах команд.")
    p = await _target(m, args, bot)
    if not p:
        return
    uid, reason = p
    keys = _teams()
    lines = await blacklist_user(bot, uid, keys, m.from_user.id, reason)
    await m.reply(f"🚫 {await user_link(uid)} добавлен в ЧС обеих команд.\n"
                  f"Данные сохранены: ID <code>{uid}</code>, имя, ник, причина, кто добавил, дата.{_why(reason)}\n"
                  + "\n".join(lines))


@cmd(".масс", perm="ban")
async def c_mass(m, args, bot):
    """`.масс чс @ник` = `.массчс @ник`; остальное отдаём своим командам (триггер «масс»)."""
    parts = (args or "").split(None, 1)
    if parts and parts[0].lower() == "чс":
        return await c_mass_bl(m, parts[1] if len(parts) > 1 else "", bot)
    import custom
    await custom.run_custom(m, m.text or m.caption or "", bot)


@cmd("-чс", perm="ban")
async def c_unbl(m, args, bot):
    key = _here(m)
    if not key:
        return await m.reply("ЧС работает только в чатах команд.")
    uid, _ = await resolve_target(m, args)
    if uid is None:
        return await m.reply("Укажите цель: ответом на сообщение, @ником или ID.")
    if not await is_blacklisted(key, uid):
        return await m.reply("Этого человека нет в ЧС этой команды.")
    await db.x("DELETE FROM blacklist WHERE team=? AND user_id=?", (key, uid))
    try:
        await bot.unban_chat_member(m.chat.id, uid, only_if_banned=True)
    except Exception as e:
        return await m.reply(f"✅ Убран из ЧС, но разбанить не вышло: {esc(e)}")
    await m.reply(f"✅ {await user_link(uid)} убран из ЧС команды {_title(key)} и разбанен.")


# ---------- хуки ----------
async def block_join(bot, key, uid, dm):
    """Заявка на вступление от человека из ЧС: отклоняем, пишем в личку. True — заявка обработана."""
    if not await is_blacklisted(key, uid):
        return False
    try:
        await bot.decline_chat_join_request(config.TEAMS[key]["chat_id"], uid)
    except Exception:
        pass
    text = notice(await blacklisted_keys(uid))
    off = await _offer(bot, uid)
    if off:
        text += "\n\n" + _offer_text(*off)
    await _dm(bot, dm, text)
    return True


async def reban(bot, key, uid):
    """Человек из ЧС всё-таки оказался в чате (разбанили вручную / добавили админом) — выгоняем снова."""
    if not await is_blacklisted(key, uid):
        return False
    try:
        await bot.ban_chat_member(config.TEAMS[key]["chat_id"], uid)
    except Exception as e:
        log.warning("reban failed %s: %s", uid, e)
        return False
    await _dm(bot, uid, notice(await blacklisted_keys(uid)))
    return True


async def process_week(bot):
    """Раз в час: если основная команда дала ЧС больше 7 дней назад и человек состоит в другой — она становится основной."""
    if time.time() - float(await db.get_meta("bl_last", "0") or 0) < 3600:
        return
    await db.set_meta("bl_last", time.time())
    from economy import schedule_refresh
    from sync import notify_owners
    changed = False
    for r in await db.q("SELECT * FROM blacklist WHERE was_home=1 AND moved=0 AND created<=?", (time.time() - WEEK,)):
        uid, key = r["user_id"], r["team"]
        b = await db.one("SELECT home FROM bank WHERE user_id=?", (uid,))
        other = config.other_team(key)
        oc = config.TEAMS[other]["chat_id"]
        if not b or b["home"] != key or not oc or await is_blacklisted(other, uid):
            await db.x("UPDATE blacklist SET moved=1 WHERE team=? AND user_id=?", (key, uid))
            continue
        if await _present(bot, oc, uid):  # состоит в другой команде — она становится основной
            await db.x("UPDATE bank SET home=?, absent=0 WHERE user_id=?", (other, uid))
            await db.x("UPDATE blacklist SET moved=1 WHERE team=? AND user_id=?", (key, uid))
            await _dm(bot, uid, f"🔁 Прошла неделя после ЧС: ваша основная команда теперь {_title(other)}.")
            await notify_owners(bot, f"🔁 {r['name'] or uid} ({uid}): основная команда {_title(key)} → {_title(other)} (неделя после ЧС).")
            changed = True
        await asyncio.sleep(0.2)
    if changed:
        schedule_refresh(bot)
