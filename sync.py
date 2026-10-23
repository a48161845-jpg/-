import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

import config
import db
from core import cmd, esc, mention, user_name
from games import FMT, now, run_auto_polls

log = logging.getLogger("sync")


async def team_list_ids(chat_id):
    ids = set()
    for r in await db.q("SELECT state FROM lists WHERE chat_id=?", (chat_id,)):
        st = json.loads(r["state"])
        ids |= {p["id"] for p in st.get("players", [])}
        if st.get("captain"):
            ids.add(st["captain"]["id"])
    return ids


async def window_active(chat_id):
    window = int(await db.get_setting(chat_id, "kick_window", "60") or 60) * 60
    n = now()
    for g in await db.q("SELECT dt FROM games WHERE chat_id=?", (chat_id,)):
        left = (datetime.strptime(g["dt"], FMT) - n).total_seconds()
        if -90 * 60 <= left <= window:
            return True
    return False


async def enforce_team(bot, chat_id, force=False):
    """Записан в список этой команды, но состоит в чате другой — выписываем оттуда (перед игрой или по force).
    Возвращает (кикнутые, ошибки)."""
    kicked, errors = [], []
    key = config.team_key(chat_id)
    if not key or not (force or await window_active(chat_id)):
        return kicked, errors
    other = config.TEAMS[config.other_team(key)]
    if not other["chat_id"]:
        return kicked, errors
    for uid in await team_list_ids(chat_id):
        try:
            mem = await bot.get_chat_member(other["chat_id"], uid)
        except Exception:
            continue
        if mem.status not in ("member", "restricted"):
            continue
        try:
            await bot.ban_chat_member(other["chat_id"], uid)
            await bot.unban_chat_member(other["chat_id"], uid, only_if_banned=True)
        except Exception as e:
            log.warning("kick failed %s: %s", uid, e)
            errors.append(f"{await user_name(uid)}: {e}")
            continue
        kicked.append(uid)
        try:
            await bot.send_message(
                other["chat_id"],
                f"🚪 {mention(uid, await user_name(uid))} записан(а) в список {config.TEAMS[key]['title']} "
                f"и исключён(а) из этого чата.\nСсылка на другую команду: <code>.сс</code>")
        except Exception:
            pass
    return kicked, errors


@cmd(".выписать", perm="lists")
async def c_evict(m, args, bot):
    """Принудительная проверка: всех из списков этой команды выписать из чата другой команды."""
    if not config.team_key(m.chat.id):
        return await m.reply("Команда работает только в чатах команд.")
    kicked, errors = await enforce_team(bot, m.chat.id, force=True)
    text = f"🚪 Исключено из другой команды: <b>{len(kicked)}</b>" if kicked else "Некого выписывать: из списков никто не состоит в чате другой команды."
    if errors:
        text += "\n❗ Не вышло (у бота нужны права админа с банами в том чате):\n" + "\n".join(esc(e) for e in errors[:5])
    await m.reply(text)


DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def last_reset_moment(dow, hh, mm):
    """Последний (не позже сейчас) момент еженедельного сброса."""
    n = now()
    t = n.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=(n.weekday() - dow) % 7)
    if t > n:
        t -= timedelta(days=7)
    return t


async def reset_lists(bot, chat_id):
    """Очищает все списки чата (шаблоны и картинки остаются), убирает кнопки со старых сообщений."""
    from lists import empty_state
    for r in await db.q("SELECT num,msg_id FROM lists WHERE chat_id=?", (chat_id,)):
        if r["msg_id"]:
            try:
                await bot.edit_message_reply_markup(chat_id=chat_id, message_id=r["msg_id"], reply_markup=None)
            except Exception:
                pass
        await db.x("UPDATE lists SET state=?, msg_id=NULL WHERE chat_id=? AND num=?", (empty_state(), chat_id, r["num"]))


async def weekly_reset(bot):
    for t in config.TEAMS.values():
        cid = t["chat_id"]
        if not cid:
            continue
        dow = int(await db.get_setting(cid, "reset_dow", "4") or 4)
        hhmm = await db.get_setting(cid, "reset_time", "00:00") or "00:00"
        if hhmm == "off":
            continue
        hh, mm = (int(x) for x in hhmm.split(":"))
        stamp = last_reset_moment(dow, hh, mm).strftime("%Y-%m-%d %H:%M")
        prev = await db.get_meta(f"lists_reset_{cid}")
        if not prev:
            await db.set_meta(f"lists_reset_{cid}", stamp)  # первый запуск: просто запоминаем
        elif prev != stamp:
            await db.set_meta(f"lists_reset_{cid}", stamp)
            await reset_lists(bot, cid)
            try:
                await bot.send_message(cid, "🔄 Списки сброшены на новую неделю. Теперь можно записываться заново (в том числе в другой команде).")
            except Exception:
                pass


@cmd(".сброссписков", perm="lists")
async def c_reset_lists(m, args, bot):
    await reset_lists(bot, m.chat.id)
    await m.reply("🔄 Все списки очищены. Вызовите нужный: <code>.список 1</code>")


@cmd(".автосброс", perm="settings")
async def c_autoreset(m, args, bot):
    a = args.lower().split()
    if a and a[0] in ("выкл", "off", "нет"):
        await db.set_setting(m.chat.id, "reset_time", "off")
        return await m.reply("Автосброс списков выключен.")
    if a:
        try:
            dow = DAYS.index(a[0][:2])
            hh, mm = (int(x) for x in a[1].split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            return await m.reply("Формат: <code>.автосброс пт 00:00</code> или <code>.автосброс выкл</code>")
        await db.set_setting(m.chat.id, "reset_dow", str(dow))
        await db.set_setting(m.chat.id, "reset_time", f"{hh:02d}:{mm:02d}")
        await db.set_meta(f"lists_reset_{m.chat.id}", last_reset_moment(dow, hh, mm).strftime("%Y-%m-%d %H:%M"))
    dow = int(await db.get_setting(m.chat.id, "reset_dow", "4") or 4)
    t = await db.get_setting(m.chat.id, "reset_time", "00:00")
    await m.reply("🔄 Автосброс списков: " + ("выключен" if t == "off" else f"каждый(ую) <b>{DAYS[dow]} в {t}</b> (МСК)")
                  + "\nИзменить: <code>.автосброс пт 22:00</code>")


async def other_link(bot, chat_id):
    key = config.team_key(chat_id)
    if not key:
        return None
    other = config.TEAMS[config.other_team(key)]
    if not other["chat_id"]:
        return None
    meta = f"cc_link_{other['chat_id']}"
    link = await db.get_meta(meta)
    if not link:
        link = (await bot.create_chat_invite_link(other["chat_id"], name="сс", creates_join_request=True)).invite_link
        await db.set_meta(meta, link)
    return link


@cmd(".сс")
async def c_cc(m, args, bot):
    try:
        link = await other_link(bot, m.chat.id)
    except Exception as e:
        return await m.reply(f"❗ Не удалось создать ссылку (бот должен быть админом с правом приглашений): {esc(e)}")
    if not link:
        return await m.reply("Эта команда работает только в чатах команд.")
    key = config.team_key(m.chat.id)
    other = config.TEAMS[config.other_team(key)]["title"]
    await m.reply(f"🔗 Можно сыграть и в другой команде: {other}\nВступление по заявке: {link}")


async def on_join_request(req, bot):
    key = config.team_key(req.chat.id)
    if not key:
        return
    # трогаем только заявки по ссылке, созданной самим ботом (.сс); чужие ссылки не обрабатываем
    inv = getattr(req, "invite_link", None)
    creator = getattr(inv, "creator", None)
    if not inv or not creator or creator.id != bot.id:
        return
    other = config.TEAMS[config.other_team(key)]["chat_id"]
    uid = req.from_user.id
    await db.touch_user(uid, req.from_user.full_name, req.from_user.username)
    if other and uid in await team_list_ids(other) and await window_active(other):
        try:
            await bot.decline_chat_join_request(req.chat.id, uid)
            await bot.send_message(uid, "Вы записаны в список другой команды на ближайшую игру — вступить сюда сейчас нельзя.")
        except Exception:
            pass
        return
    # Автопринятие — только по ссылке бота (.сс), чужие ссылки бот не трогает; иначе заявку рассматривают админы.
    if await db.get_setting(req.chat.id, "auto_approve", "1") == "1":
        try:
            await bot.approve_chat_join_request(req.chat.id, uid)
        except Exception as e:
            log.warning("approve failed: %s", e)


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


@cmd(".синхр", ".сбор", perm="settings")
async def c_sync(m, args, bot):
    import mtproto
    n = await sync_members(bot)
    try:
        from economy import refresh_channel
        await refresh_channel(bot)
    except Exception as e:
        log.warning("refresh after sync failed: %s", e)
    note = "" if mtproto.available() else "\nℹ️ Полный список участников доступен только с Telethon (см. README: API_ID/API_HASH)."
    await m.reply(f"🔄 Участников собрано: {n}{note}")


async def daily_check(bot):
    """Проверка, состоят ли люди из списка баллов в чатах команд; отсутствующие уходят вниз списка."""
    from economy import refresh_channel
    await sync_members(bot)
    for key, t in config.TEAMS.items():
        if not t["chat_id"]:
            continue
        for r in await db.q("SELECT user_id FROM points WHERE team=?", (key,)):
            try:
                mem = await bot.get_chat_member(t["chat_id"], r["user_id"])
                absent = int(mem.status in ("left", "kicked"))
            except Exception:
                absent = 1
            await db.x("UPDATE points SET absent=? WHERE user_id=? AND team=?", (absent, r["user_id"], key))
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
            await db.x("UPDATE points SET absent=0 WHERE user_id=? AND team=?", (uid, key))
    elif st in ("left", "kicked"):
        await db.remove_member(upd.chat.id, uid)
        if key:
            await db.x("UPDATE points SET absent=1 WHERE user_id=? AND team=?", (uid, key))
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
            await weekly_reset(bot)
            await db.x("DELETE FROM warns WHERE until IS NOT NULL AND until<?", (time.time(),))
            today = now().strftime("%Y-%m-%d")
            if now().hour >= config.DAILY_CHECK_HOUR and await db.get_meta("last_daily") != today:
                await db.set_meta("last_daily", today)
                await daily_check(bot)
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(60)
