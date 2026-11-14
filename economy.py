import asyncio
import collections
import hashlib
import json
import logging
import re
import time

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from core import (cb, cmd, esc, fmt, has_perm, hide_owner_tags, lookup_user, mention, parse_num,
                  points_word, resolve_target, user_link, user_name)

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


# ---------- баллы из канала ----------
# Кэпы вручную пишут в канал строки «Ник (id) - баллы». Бот только читает канал:
# значение из сообщения прибавляется к балансу, а правка/замена строки меняет баланс на разницу.
# Покупки в магазине бот снимает сам, и они сохраняются при любых правках списка.
CH_LINE_RE = re.compile(r"^(?P<name>.*)\((?P<id>\d{5,})\)\s*(?P<rest>.*)$")      # Ник (id) - 5
CH_PIPE_RE = re.compile(r"^(?P<name>.*?)\s*\|\s*(?P<id>\d{5,})\s*(?P<rest>.*)$")     # 1. @ник | id - 5
_ch_lock = asyncio.Lock()


def parse_channel_text(text):
    """-> (ok: [(uid, имя, баллы)], bad: [строка]). Форматы: «1. @ник | id - 5», «Ник (id) - 5»; «-5» без пробела = минус 5."""
    ok, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        m = CH_PIPE_RE.match(line) or CH_LINE_RE.match(line)
        if not m:
            continue  # заголовки и пустые строки пропускаем
        val = parse_value(m.group("rest"))
        if val is None:
            bad.append(line)
        else:
            name = re.sub(r"^\s*\d+[.)]\s*", "", m.group("name")).strip(" |-–—:")
            ok.append((int(m.group("id")), name or str(m.group("id")), val))
    return ok, bad


async def _remember_list_user(uid, label):
    """Человек из списка/канала: сохраняем его @ник (для поиска по нику), а настоящие данные Telegram подтянутся сами.
    Ник из списка не перезаписывает уже известный и не отбирается у другого человека."""
    un = label[1:].lower() if re.fullmatch(r"@\w{3,}", label or "") else None
    r = await db.one("SELECT username FROM users WHERE user_id=?", (uid,))
    if r is None:
        await db.x("INSERT INTO users(user_id,name,username) VALUES(?,?,?) ON CONFLICT(user_id) DO NOTHING", (uid, label, un))
    elif un and not r["username"] and not await db.one("SELECT 1 FROM users WHERE username=? AND user_id<>?", (un, uid)):
        await db.x("UPDATE users SET username=? WHERE user_id=? AND (username IS NULL OR username='')", (un, uid))


async def _guess_home(uid):
    """Основная команда для нового человека из канала: где его видели; иначе пусто — проставится при первом сообщении в чате команды."""
    for key, t in config.TEAMS.items():
        if t["chat_id"] and await db.one("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (t["chat_id"], uid)):
            return key
    return ""


async def apply_channel_post(bot, chat_id, msg_id, text):
    """Обрабатывает новое/изменённое сообщение канала баллов. Возвращает (применено строк, предупреждения)."""
    ok, bad = parse_channel_text(text)
    new, names, warns = {}, {}, []
    for uid, name, val in ok:
        if uid in new:
            warns.append(f"id {uid} встречается в сообщении дважды — взята последняя строка")
        new[uid], names[uid] = val, name
    for line in bad:
        warns.append(f"не понял баллы в строке: {line[:80]}")
    async with _ch_lock:
        old = {r["user_id"]: r["value"] for r in await db.q(
            "SELECT user_id, value FROM ch_points WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))}
        for uid in set(old) | set(new):
            delta = new.get(uid, 0.0) - old.get(uid, 0.0)
            if uid in new:
                await _remember_list_user(uid, names[uid])  # @ник из строки — для поиска; всё остальное по id
                await db.x("INSERT INTO ch_points(chat_id,msg_id,user_id,value) VALUES(?,?,?,?) "
                           "ON CONFLICT(chat_id,msg_id,user_id) DO UPDATE SET value=excluded.value",
                           (chat_id, msg_id, uid, new[uid]))
            else:
                await db.x("DELETE FROM ch_points WHERE chat_id=? AND msg_id=? AND user_id=?", (chat_id, msg_id, uid))
            if delta:
                await add_points(uid, delta, await _guess_home(uid))
    return len(new), warns


async def apply_list_edit(bot, text):
    """Кэп поправил значения прямо в общем списке бота: баланс человека становится равным написанному (по id)."""
    ok, bad = parse_channel_text(text)
    warns, seen = [f"не понял баллы в строке: {line[:80]}" for line in bad], set()
    async with _ch_lock:
        for uid, name, val in ok:
            if uid in seen:
                warns.append(f"id {uid} встречается в списке дважды — взята последняя строка")
            seen.add(uid)
            r = await db.one("SELECT value FROM bank WHERE user_id=?", (uid,))
            await _remember_list_user(uid, name)
            if r is None:
                await add_points(uid, val, await _guess_home(uid))
            elif abs(r["value"] - val) > 1e-9:
                await db.x("UPDATE bank SET value=? WHERE user_id=?", (val, uid))
    return len(ok), warns


async def on_channel_post(bot, post):
    if post.chat.id != config.POINTS_CHANNEL_ID:
        return
    text = post.text or post.caption or ""
    if not text.strip():
        return
    h = _h(text)
    if h in _own_hashes:  # это эхо сообщения, которое бот сам только что отправил/отредактировал
        _own_hashes.remove(h)
        return
    try:
        state = json.loads(await db.get_meta("points_state", "{}"))
        list_ids = {e["id"] for e in state.get("all", [])} if state.get("ch") == post.chat.id else set()
        if post.message_id in list_ids:
            n, warns = await apply_list_edit(bot, text)
            if not state.get("imported"):  # первое прочтение готового сообщения — по правке кэпа
                state["imported"] = True
                await db.set_meta("points_state", json.dumps(state))
        else:
            n, warns = await apply_channel_post(bot, post.chat.id, post.message_id, text)
    except Exception:
        log.exception("channel post failed")
        return
    schedule_refresh(bot)  # перерисовать общий список баллов
    if warns:  # проверка списков: о проблемах сообщаем создателю в личку
        link = f"\nСообщение №{post.message_id} в канале баллов."
        for uid in config.OWNER_IDS:
            try:
                await bot.send_message(uid, "⚠️ Проверка списка баллов:\n" + "\n".join(warns[:15]) + link, parse_mode=None)
            except Exception:
                pass


def _clip(text, n=32):
    text = str(text if text is not None else "—").replace("\n", " ").strip() or "—"
    return text if len(text) <= n else text[: n - 1] + "…"


LABEL_TTL = 23 * 3600  # ник в списке меняется примерно раз в сутки (с запасом, чтобы попасть в ежедневную синхронизацию); человек определяется только по id


async def build_lines():
    """Общий список баллов одним списком: «N. @ник | id - баллы», по убыванию баланса.
    Подпись (@ник или имя) запоминается и пересматривается раз в 24 часа — если человек сменил ник, он обновится."""
    rows = await db.q(
        "SELECT b.user_id AS user_id, u.name AS name, u.username AS username, b.value AS value, "
        "l.label AS label, l.ts AS ts FROM bank b LEFT JOIN users u ON u.user_id=b.user_id "
        "LEFT JOIN list_names l ON l.user_id=b.user_id "
        "ORDER BY b.value DESC, b.user_id ASC")
    now_ts = time.time()
    lines = []
    for i, r in enumerate(rows, 1):
        label = r["label"]
        if label is None or now_ts - (r["ts"] or 0) >= LABEL_TTL:
            if r["username"] and r["user_id"] not in config.OWNER_IDS:
                label = f"@{r['username']}"
            else:  # без ника и создатель/овнеры (их не тегаем) — просто имя
                label = _clip(r["name"] or r["user_id"])
            await db.x("INSERT INTO list_names(user_id,label,ts) VALUES(?,?,?) "
                       "ON CONFLICT(user_id) DO UPDATE SET label=excluded.label, ts=excluded.ts", (r["user_id"], label, now_ts))
        lines.append(hide_owner_tags(f"{i}. {label} | {r['user_id']} - {fmt(r['value'])}"))
    return lines


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


async def _pin(bot, ch, mid):
    """Закрепляет сообщение без уведомления; без права закреплять у бота — тихо пропускает."""
    try:
        await bot.pin_chat_message(ch, mid, disable_notification=True)
    except Exception as e:
        log.info("pin failed: %s", e)


_own_hashes = collections.deque(maxlen=64)  # тексты, которые бот сам только что отправил (чтобы не принять своё эхо за правку кэпа)


def _h(text):
    return hashlib.md5((text or "").strip().encode("utf-8")).hexdigest()


def _gone(e):
    t = str(e).lower()
    return "message to edit not found" in t or "message_id_invalid" in t or "message can't be edited" in t


async def _notify_once(bot, key, text):
    if await db.get_meta(key, "") == "1":
        return
    await db.set_meta(key, "1")
    for uid in config.OWNER_IDS:
        try:
            await bot.send_message(uid, text, parse_mode=None)
        except Exception:
            pass


async def init_list(bot):
    """Работа с готовым сообщением списка (LIST_MESSAGE_ID): при первом запуске бот читает его текущее содержимое
    (пересылает себе в личку создателя и тут же удаляет пересылку), забирает баллы по id и дальше правит это же сообщение.
    True — можно обновлять список; False — содержимое ещё не прочитано (чтобы не затереть список)."""
    mid, ch = config.LIST_MESSAGE_ID, config.LIST_CHANNEL_ID
    state = json.loads(await db.get_meta("points_state", "{}"))
    if not mid:
        # Номер сообщения не задан: бот сам помнит, где его список (state["all"]), и от номера не зависит.
        if state.get("ch") == ch and state.get("all"):
            return True
        # первый запуск: если в канале закреплён список в формате «Ник | id - баллы», забираем его, иначе бот создаст свой
        try:
            pinned = (await bot.get_chat(ch)).pinned_message
            text = (pinned.text or pinned.caption) if pinned else None
            if text and parse_channel_text(text)[0]:
                await apply_list_edit(bot, text)
                state = {"ch": ch, "all": [{"id": pinned.message_id, "h": ""}], "imported": True}
                await db.set_meta("points_state", json.dumps(state))
        except Exception as e:
            log.info("pinned list read failed: %s", e)
        return True
    if state.get("ch") == ch and state.get("src") == mid:
        return bool(state.get("imported"))
    state = {"ch": ch, "src": mid, "all": [{"id": mid, "h": ""}], "imported": False}
    text = None
    for uid in config.OWNER_IDS:
        try:
            fwd = await bot.forward_message(uid, ch, mid)
            text = fwd.text or fwd.caption
            try:
                await bot.delete_message(uid, fwd.message_id)
            except Exception:
                pass
            if text:
                break
        except Exception as e:
            log.info("forward list failed for %s: %s", uid, e)
    if text:
        await apply_list_edit(bot, text)
        state["imported"] = True
    await db.set_meta("points_state", json.dumps(state))
    if not state["imported"]:
        await _notify_once(bot, "list_import_warned",
                           f"⚠️ Не смог прочитать сообщение №{mid} в канале баллов. Откройте личку с ботом (/start), "
                           "чтобы он мог его прочитать, или просто отредактируйте это сообщение — бот возьмёт значения из правки. "
                           "Пока список не тронут.")
    return state["imported"]


async def refresh_channel(bot):
    """Общий список баллов в канале висит всегда: сообщения правятся на месте, заново не отправляются
    (новое сообщение — только если списка ещё нет, он вырос на ещё одно сообщение или старое удалено)."""
    async with _lock:
        ch = config.LIST_CHANNEL_ID
        if not await init_list(bot):
            return
        state = json.loads(await db.get_meta("points_state", "{}"))
        if state.get("ch") != ch:  # канал сменился (или списка ещё не было): старые id сообщений к новому каналу не относятся
            state = {"ch": ch}
            await db.set_meta("points_pinned", "")
        if "all" not in state:  # переход со старой версии: берём уже висящие сообщения команд, ничего не пересылая
            state["all"] = [e for k in ("starlight", "midnight") for e in state.get(k, [])]
            for k in ("starlight", "midnight"):
                state.pop(k, None)
        for stale in [k for k in state if k not in ("all", "ch", "src", "imported")]:
            state.pop(stale)
        texts = chunk(await build_lines()) or ["Список баллов пока пуст."]
        cur = state["all"]
        new = []
        for i, text in enumerate(texts):
            h = _h(text)
            if i < len(cur):
                if cur[i].get("h") == h:
                    new.append(cur[i])
                    continue
                try:
                    _own_hashes.append(h)
                    await _tg(bot.edit_message_text, text, chat_id=ch, message_id=cur[i]["id"], parse_mode=None)
                    new.append({"id": cur[i]["id"], "h": h})
                    if i == 0:
                        await db.set_meta("list_edit_warned", "")
                    continue
                except TelegramBadRequest as e:
                    if "not modified" in str(e):
                        new.append({"id": cur[i]["id"], "h": h})
                        continue
                    if i == 0 and config.LIST_MESSAGE_ID:  # готовое сообщение: новых вместо него не шлём
                        log.warning("edit of list message failed: %s", e)
                        await _notify_once(bot, "list_edit_warned",
                                           f"⚠️ Не могу отредактировать сообщение №{config.LIST_MESSAGE_ID} в канале баллов: {e}. "
                                           "Дайте боту право редактировать чужие сообщения (админ канала).")
                        new.append(cur[i])
                        continue
                    if not _gone(e):
                        log.warning("edit failed: %s", e)
                        new.append(cur[i])  # оставляем как есть, не шлём заново
                        continue
                    log.info("сообщение списка удалено, отправляю заново")
            _own_hashes.append(h)
            m = await _tg(bot.send_message, ch, text, parse_mode=None)
            new.append({"id": m.message_id, "h": h})
            if i == 0:
                await _pin(bot, ch, m.message_id)
        for old in cur[len(texts):]:  # список стал короче — лишние сообщения убираем
            try:
                await bot.delete_message(ch, old["id"])
            except Exception:
                pass
        state["all"] = new
        state["ch"] = ch
        await db.set_meta("points_state", json.dumps(state))
        if new and await db.get_meta("points_pinned", "") != "1":  # один раз закрепляем уже существующий список
            await _pin(bot, ch, new[0]["id"])
            await db.set_meta("points_pinned", "1")


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


MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def _place(value):
    """Место в списке (одинаковые баллы — одно место) и всего людей."""
    place = (await db.one("SELECT COUNT(*) AS c FROM bank WHERE value>?", (value,)))["c"] + 1
    total = (await db.one("SELECT COUNT(*) AS c FROM bank"))["c"]
    return place, total


@cmd(".баллы")
async def c_points(m, args, bot):
    """.баллы — свой баланс и место; .баллы @ник / id / ответом — чужой; .баллы топ [N] — лидеры."""
    a = args.strip()
    first = a.split()[0].lower() if a else ""
    if first in ("топ", "top", "лидеры"):
        rest = a.split()[1:]
        n = max(1, min(int(rest[0]) if rest and rest[0].isdigit() else 10, 30))
        rows = await db.q(
            "SELECT b.user_id AS user_id, u.name AS name, b.value AS value FROM bank b "
            "LEFT JOIN users u ON u.user_id=b.user_id ORDER BY b.value DESC, b.user_id ASC LIMIT ?", (n,))
        if not rows:
            return await m.reply("Список баллов пока пуст.")
        total = (await db.one("SELECT COUNT(*) AS c FROM bank"))["c"]
        lines = [f"🏆 <b>Топ по баллам</b> · {len(rows)} из {total}", ""]
        prev, place = None, 0
        for i, r in enumerate(rows, 1):
            if r["value"] != prev:
                place, prev = i, r["value"]
            lines.append(f"{MEDALS.get(place, f'{place}.')} {hide_owner_tags(esc(r['name'] or r['user_id']))} — <b>{fmt(r['value'])}</b>")
        me = await db.one("SELECT value FROM bank WHERE user_id=?", (m.from_user.id,))
        if me is not None and all(r["user_id"] != m.from_user.id for r in rows):
            p, _ = await _place(me["value"])
            lines += ["", f"Вы: {p} место — <b>{fmt(me['value'])}</b>"]
        return await m.reply("\n".join(lines))

    uid, _ = await resolve_target(m, args)
    if uid is None:
        if a:
            return await m.reply("Не нашёл такого человека. Укажите @ник или id (ищу и по списку баллов), либо ответьте на его сообщение.")
        uid = m.from_user.id
    name = hide_owner_tags(esc(await user_name(uid)))
    bal = await get_balance(uid)
    await m.reply(f"💎 Баланс {name}: {fmt(bal)} {points_word(bal)}")


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


async def channel_link(bot):
    if config.LIST_CHANNEL_LINK:
        return config.LIST_CHANNEL_LINK
    link = await db.get_meta("points_link")
    if not link:
        try:
            link = (await bot.create_chat_invite_link(config.LIST_CHANNEL_ID, name="баллы")).invite_link
            await db.set_meta("points_link", link)
        except Exception:
            return ""
    return link


@cmd("!о_баллы")
async def c_link(m, args, bot):
    link = await channel_link(bot)
    await m.reply(f"📊 Баллы: {link}" if link else "Ссылка недоступна: добавьте бота админом в канал.")


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
