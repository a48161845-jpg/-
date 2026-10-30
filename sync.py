import asyncio
import json
import logging
import time
from datetime import datetime

import config
import db
from core import cmd, esc, mention, user_name
from games import FMT, now, run_auto_polls

log = logging.getLogger("sync")


async def team_list_ids(chat_id):
    from lists import list_ids
    return await list_ids(chat_id)


async def window_active(chat_id):
    window = int(await db.get_setting(chat_id, "kick_window", "60") or 60) * 60
    n = now()
    for g in await db.q("SELECT dt FROM games WHERE chat_id=?", (chat_id,)):
        left = (datetime.strptime(g["dt"], FMT) - n).total_seconds()
        if -60 * 60 <= left <= window:
            return True
    return False


def join_closed():
    """С 20:00 до 21:00 (МСК) идут Голодные игры — вход в команды закрыт."""
    n = now()
    return config.JOIN_CLOSE_FROM <= n.hour * 60 + n.minute < config.JOIN_CLOSE_TO


async def belongs_to_other(chat_id, uid):
    """Человек состоит в противоположной команде: закреплён за ней (основная команда),
    а если основной команды ещё нет — просто состоит в её чате."""
    key = config.team_key(chat_id)
    other_key = config.other_team(key)
    b = await db.one("SELECT home FROM bank WHERE user_id=?", (uid,))
    if b:
        return b["home"] == other_key
    oc = config.TEAMS[other_key]["chat_id"]
    return bool(oc and await db.one("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (oc, uid)))


async def enforce_team(bot, chat_id):
    """Перед игрой и во время неё: из чата выгоняем всех, кто состоит в противоположной команде
    и не записан в списки этой команды. Возвращает (кикнутые, ошибки)."""
    kicked, errors = [], []
    key = config.team_key(chat_id)
    if not key or not await window_active(chat_id):
        return kicked, errors
    other_key = config.other_team(key)
    other = config.TEAMS[other_key]
    listed = await team_list_ids(chat_id)
    rows = await db.q(
        "SELECT m.user_id FROM members m WHERE m.chat_id=? AND (m.user_id IN (SELECT user_id FROM bank WHERE home=?) "
        "OR (m.user_id NOT IN (SELECT user_id FROM bank) AND m.user_id IN (SELECT user_id FROM members WHERE chat_id=?)))",
        (chat_id, other_key, other["chat_id"] or 0))
    safe = set(config.OWNER_IDS) | {bot.id} | {r["user_id"] for r in await db.q("SELECT user_id FROM caps WHERE chat_id=?", (chat_id,))}
    candidates = [r["user_id"] for r in rows if r["user_id"] not in listed and r["user_id"] not in safe]
    if not candidates:
        return kicked, errors
    try:
        admins = {a.user.id for a in await bot.get_chat_administrators(chat_id)}
    except Exception:
        admins = set()
    for uid in candidates:
        if uid in admins:
            continue
        try:
            mem = await bot.get_chat_member(chat_id, uid)
        except Exception:
            continue
        if mem.status not in ("member", "restricted"):
            continue
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
        except Exception as e:
            log.warning("kick failed %s: %s", uid, e)
            errors.append(f"{await user_name(uid)}: {e}")
            continue
        kicked.append(uid)
        try:
            await bot.send_message(
                chat_id,
                f"🚪 {mention(uid, await user_name(uid))} состоит в команде {other['title']} и не записан(а) в список "
                f"{config.TEAMS[key]['title']} — исключён(а) на время игры.")
        except Exception:
            pass
    return kicked, errors


async def team_link(bot, target_key):
    """Ссылка-заявка в чат команды target_key (создаётся один раз и запоминается)."""
    target = config.TEAMS[target_key]
    if not target["chat_id"]:
        return None
    meta = f"cc_link_{target['chat_id']}"
    link = await db.get_meta(meta)
    if not link:
        link = (await bot.create_chat_invite_link(target["chat_id"], name="сс", creates_join_request=True)).invite_link
        await db.set_meta(meta, link)
    return link


async def _link_cmd(m, bot, target_key):
    here = config.team_key(m.chat.id)
    if not here:
        return await m.reply("Эта команда работает только в чатах команд.")
    if here == target_key:
        return await m.reply(f"Вы уже в чате {config.TEAMS[here]['title']}. "
                             f"Ссылка на другую команду: <code>{'.стар' if here == 'midnight' else '.мид'}</code>")
    try:
        link = await team_link(bot, target_key)
    except Exception as e:
        return await m.reply(f"❗ Не удалось создать ссылку (бот должен быть админом с правом приглашений): {esc(e)}")
    if not link:
        return await m.reply("Чат этой команды не настроен.")
    await m.reply(f"🔗 {config.TEAMS[target_key]['title']}\nВступление по заявке: {link}\n"
                  "С 20:00 до 21:00 (МСК) вход закрыт — идут Голодные игры, заявку бот примет после 21:00.")


@cmd(".мид")
async def c_mid(m, args, bot):
    """Выдача ссылки на Midnight (пишется в чате Starlight)."""
    await _link_cmd(m, bot, "midnight")


@cmd(".стар")
async def c_star(m, args, bot):
    """Выдача ссылки на Starlight (пишется в чате Midnight)."""
    await _link_cmd(m, bot, "starlight")


async def on_join_request(req, bot):
    key = config.team_key(req.chat.id)
    if not key:
        return
    # трогаем только заявки по ссылке, созданной самим ботом (.мид / .стар); чужие ссылки не обрабатываем
    inv = getattr(req, "invite_link", None)
    creator = getattr(inv, "creator", None)
    if not inv or not creator or creator.id != bot.id:
        return
    other = config.TEAMS[config.other_team(key)]["chat_id"]
    uid = req.from_user.id
    dm = getattr(req, "user_chat_id", None) or uid
    if uid in config.OWNER_IDS:  # создатель заходит всегда: без закрытого входа и проверок
        try:
            await bot.approve_chat_join_request(req.chat.id, uid)
        except Exception as e:
            log.warning("approve owner failed: %s", e)
        return
    await db.touch_user(uid, req.from_user.full_name, req.from_user.username)
    if other and uid in await team_list_ids(other) and await window_active(other):
        try:
            await bot.decline_chat_join_request(req.chat.id, uid)
            await bot.send_message(dm, "Вы записаны в список другой команды на ближайшую игру — вступить сюда сейчас нельзя.")
        except Exception:
            pass
        return
    if await window_active(req.chat.id) and uid not in await team_list_ids(req.chat.id) and await belongs_to_other(req.chat.id, uid):
        try:
            await bot.decline_chat_join_request(req.chat.id, uid)
            await bot.send_message(dm, "Вы состоите в другой команде и не записаны в список этой — вступить сюда перед игрой нельзя.")
        except Exception:
            pass
        return
    # Автопринятие — только по ссылке бота, иначе заявку рассматривают админы.
    if await db.get_setting(req.chat.id, "auto_approve", "1") != "1":
        return
    if join_closed():  # идут Голодные игры: заявка ждёт до 21:00
        await db.x("INSERT INTO join_queue(chat_id,user_id,ts) VALUES(?,?,?) "
                   "ON CONFLICT(chat_id,user_id) DO UPDATE SET ts=excluded.ts", (req.chat.id, uid, time.time()))
        try:
            await bot.send_message(dm, "Сейчас идут Голодные игры (20:00–21:00 МСК), вход закрыт. "
                                       "Заявка сохранена — после 21:00 бот примет вас автоматически.")
        except Exception:
            pass
        return
    try:
        await bot.approve_chat_join_request(req.chat.id, uid)
    except Exception as e:
        log.warning("approve failed: %s", e)


async def flush_join_queue(bot):
    """После 21:00 принимает заявки, пришедшие во время закрытого входа."""
    for r in await db.q("SELECT chat_id,user_id FROM join_queue"):
        await db.x("DELETE FROM join_queue WHERE chat_id=? AND user_id=?", (r["chat_id"], r["user_id"]))
        if await window_active(r["chat_id"]) and r["user_id"] not in await team_list_ids(r["chat_id"]) \
                and await belongs_to_other(r["chat_id"], r["user_id"]):
            try:
                await bot.decline_chat_join_request(r["chat_id"], r["user_id"])
            except Exception:
                pass
            continue
        try:
            await bot.approve_chat_join_request(r["chat_id"], r["user_id"])
        except Exception as e:  # заявка могла быть отозвана или принята админом
            log.info("queued approve failed %s: %s", r["user_id"], e)
        await asyncio.sleep(0.3)


async def sync_members(bot):
    """Собирает участников чатов команд в базу (админы через Bot API, всех — через Telethon, если подключён)."""
    import mtproto
    total = 0
    for t in config.TEAMS.values():
        cid = t["chat_id"]
        if not cid:
            continue
        try:
            for a in await bot.get_chat_administrators(cid):
                if not a.user.is_bot:
                    await db.touch_user(a.user.id, a.user.full_name, a.user.username)
                    await db.add_member(cid, a.user.id)
                    total += 1
        except Exception as e:
            log.warning("admins sync failed %s: %s", cid, e)
        total += await mtproto.sync_chat(cid)
    return total


async def daily_check(bot):
    """Каждый день в 00:00 МСК: собираем участников чатов и проверяем, состоят ли люди в своей основной команде;
    ушедшие уходят вниз списка баллов."""
    from economy import refresh_channel
    await sync_members(bot)
    for key, t in config.TEAMS.items():
        if not t["chat_id"]:
            continue
        for r in await db.q("SELECT user_id FROM bank WHERE home=?", (key,)):
            try:
                mem = await bot.get_chat_member(t["chat_id"], r["user_id"])
                absent = int(mem.status in ("left", "kicked"))
            except Exception:
                absent = 1
            await db.x("UPDATE bank SET absent=? WHERE user_id=?", (absent, r["user_id"]))
            await asyncio.sleep(0.05)
    await refresh_channel(bot)


async def on_chat_member(upd, bot):
    """Вход/выход участника: обновляем базу, списки игр и список баллов в канале."""
    key = config.team_key(upd.chat.id)
    uid = upd.new_chat_member.user.id
    st = upd.new_chat_member.status
    if st in ("member", "administrator", "creator", "restricted"):
        await db.touch_user(uid, upd.new_chat_member.user.full_name, upd.new_chat_member.user.username)
        await db.add_member(upd.chat.id, uid)
        if key:
            await db.x("UPDATE bank SET absent=0 WHERE user_id=? AND home=?", (uid, key))
    elif st in ("left", "kicked"):
        await db.remove_member(upd.chat.id, uid)
        if key:
            await db.x("UPDATE bank SET absent=1 WHERE user_id=? AND home=?", (uid, key))
            from lists import remove_from_lists
            await remove_from_lists(bot, upd.chat.id, uid)
    if key:
        from economy import schedule_refresh
        schedule_refresh(bot)


async def scheduler(bot):
    while True:
        try:
            for t in config.TEAMS.values():
                if t["chat_id"]:
                    await enforce_team(bot, t["chat_id"])
            await run_auto_polls(bot)
            if not join_closed():
                await flush_join_queue(bot)
            await db.x("DELETE FROM warns WHERE until IS NOT NULL AND until<?", (time.time(),))
            await db.x("DELETE FROM done_lists WHERE until<?", (time.time(),))
            today = now().strftime("%Y-%m-%d")
            if now().hour >= config.DAILY_CHECK_HOUR and await db.get_meta("last_daily") != today:
                await db.set_meta("last_daily", today)
                await daily_check(bot)
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(60)
