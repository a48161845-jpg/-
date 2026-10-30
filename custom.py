"""Свои команды (триггеры): слово или фраза -> ответ (текст или медиа).

Как в обычных ботах: триггер пишется как есть, регистр не важен, префикс (. ! / ,) необязателен.
Режимы срабатывания: «начало» (сообщение начинается с триггера, остальное — аргументы),
«точно» (сообщение целиком равно триггеру), «слово» (триггер встречается в тексте).
"""
import re
import time

import config
import db
from core import PREFIXES, REG, canon, cmd, esc, mention

MODES = {"start": "в начале", "exact": "точно", "word": "в тексте"}
MODE_WORDS = {"начало": "start", "точно": "exact", "слово": "word"}
MODE_ORDER = ["start", "exact", "word"]

VARS_HELP = """\
<b>Переменные в ответе:</b>
<code>{args}</code> — текст после триггера, <code>{1}</code> <code>{2}</code> — первое/второе слово
<code>{имя_фамилия}</code> — текст после триггера, а если его нет — имя автора (или того, на кого ответили)
<code>{name}</code> <code>{first}</code> <code>{last}</code> — имя автора, <code>{username}</code>, <code>{id}</code>, <code>{mention}</code>
<code>{reply}</code> <code>{reply_mention}</code> — тот, на чьё сообщение ответили
<code>{points}</code> — баланс, <code>{team}</code> — команда, <code>{chat}</code>, <code>{date}</code>, <code>{time}</code>"""

_cache = {}
_last = {}


def norm_trig(s):
    """Триггер в каноничном виде: нижний регистр, без префикса в начале, одинарные пробелы."""
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    return s.lstrip(PREFIXES).strip()


def _norm_var(k):
    return re.sub(r"[\s_]+", "", k.lower())


async def _vars(m, args):
    from economy import get_balance, get_home
    from games import now
    u = m.from_user
    r = m.reply_to_message.from_user if m.reply_to_message and m.reply_to_message.from_user else None
    first, _, last = u.full_name.partition(" ")
    words = args.split()
    home = await get_home(u.id)
    full = args.strip() or (r.full_name if r else u.full_name)
    bal = f"{await get_balance(u.id):g}"
    v = {
        "args": args.strip(), "текст": args.strip(),
        "name": u.full_name, "имя": first, "first": first, "last": last, "фамилия": last,
        "username": f"@{u.username}" if u.username and u.id not in config.OWNER_IDS else u.full_name, "id": str(u.id),
        "mention": mention(u.id, u.full_name),
        "reply": r.full_name if r else "", "reply_username": (f"@{r.username}" if r.username and r.id not in config.OWNER_IDS else r.full_name) if r else "",
        "replyid": str(r.id) if r else "", "replymention": mention(r.id, r.full_name) if r else "",
        "points": bal, "total": bal, "баллы": bal,
        "team": config.TEAMS[home]["title"] if home in config.TEAMS else "", "chat": m.chat.title or "",
        "date": now().strftime("%d.%m.%Y"), "time": now().strftime("%H:%M"),
        "nameифамилия": full, "имяифамилия": full, "имяфамилия": full, "full": full, "fullname": full,
    }
    for i, w in enumerate(words, 1):
        v[str(i)] = w
    return v


async def render(m, text, args):
    raw = ("mention", "replymention")
    v = {k: (x if k in raw else esc(x)) for k, x in (await _vars(m, args)).items()}
    return re.sub(r"\{([^{}]+)\}", lambda mo: v.get(_norm_var(mo.group(1)), mo.group(0)), text)


# ---------- хранилище ----------
def invalidate(chat_id):
    _cache.pop(chat_id, None)


def _compile(trig, mode):
    words = r"\s+".join(re.escape(w) for w in trig.split())
    pre = "[" + re.escape(PREFIXES) + "]*"
    if mode == "exact":
        return re.compile(rf"^{pre}{words}$", re.I)
    if mode == "word":
        return re.compile(rf"(?<!\w){words}(?!\w)", re.I)
    return re.compile(rf"^{pre}{words}(?!\w)", re.I)


async def rows(chat_id):
    """Команды чата (с id для кнопок), самые длинные триггеры первыми."""
    if chat_id not in _cache:
        out = []
        for r in await db.q("SELECT rid, name, text, mtype, fid, mode FROM custom WHERE chat_id=?", (chat_id,)):
            trig = norm_trig(r["name"])
            mode = r["mode"] if r["mode"] in MODES else "start"
            if trig:
                out.append({"rid": r["rid"], "raw": r["name"], "trig": trig, "text": r["text"] or "", "mtype": r["mtype"],
                            "fid": r["fid"], "mode": mode, "rx": _compile(trig, mode)})
        _cache[chat_id] = sorted(out, key=lambda x: -len(x["trig"]))
    return _cache[chat_id]


async def by_rid(chat_id, rid):
    return next((r for r in await rows(chat_id) if r["rid"] == rid), None)


def check_trigger(trig):
    """None, если триггер годится, иначе текст ошибки."""
    if not trig:
        return "Пустой триггер."
    if trig[0] in "+-":
        return "Триггер не может начинаться с + или -."
    if canon(trig.split()[0]) in REG:
        return "Это имя занято встроенной командой."
    if len(trig) > 60:
        return "Слишком длинный триггер (до 60 символов)."
    return None


async def save(chat_id, trig, text, mtype, fid, mode="start"):
    """Создаёт или обновляет команду. True, если обновили существующую."""
    old = next((r for r in await rows(chat_id) if r["trig"] == trig), None)
    if old:
        await db.x("DELETE FROM custom WHERE chat_id=? AND name=?", (chat_id, old["raw"]))
    await db.x("INSERT INTO custom(chat_id,name,text,mtype,fid,mode) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(chat_id,name) DO UPDATE SET text=excluded.text, mtype=excluded.mtype, fid=excluded.fid, mode=excluded.mode",
               (chat_id, trig, text, mtype, fid, mode))
    invalidate(chat_id)
    return bool(old)


async def delete(chat_id, rid):
    r = await by_rid(chat_id, rid)
    if r:
        await db.x("DELETE FROM custom WHERE chat_id=? AND name=?", (chat_id, r["raw"]))
        invalidate(chat_id)
    return r


async def set_mode(chat_id, rid, mode):
    r = await by_rid(chat_id, rid)
    if r:
        await db.x("UPDATE custom SET mode=? WHERE chat_id=? AND name=?", (mode, chat_id, r["raw"]))
        invalidate(chat_id)


# ---------- срабатывание ----------
async def run_custom(m, text, bot):
    """Ищет подходящую команду в сообщении; True, если нашлась и выполнена."""
    t = text.strip()
    hit = None
    for r in await rows(m.chat.id):
        mo = r["rx"].search(t)
        if mo:
            hit = (r, t[mo.end():].strip() if r["mode"] != "exact" else "")
            break
    if not hit:
        return False
    r, args = hit
    if r["mode"] == "word":  # реакция на слово в обычном тексте — не чаще раза в 5 секунд
        k = (m.chat.id, r["trig"])
        if time.time() - _last.get(k, 0) < 5:
            return False
        _last[k] = time.time()
    out = await render(m, r["text"], args) if r["text"] else ""
    kw = {"reply_to_message_id": m.reply_to_message.message_id} if m.reply_to_message else {}
    try:
        if r["mtype"]:
            send = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation,
                    "document": bot.send_document, "audio": bot.send_audio, "voice": bot.send_voice,
                    "sticker": bot.send_sticker, "video_note": bot.send_video_note}[r["mtype"]]
            if r["mtype"] in ("sticker", "video_note"):
                await send(m.chat.id, r["fid"], **kw)
                if out:
                    await bot.send_message(m.chat.id, out, **kw)
            else:
                await send(m.chat.id, r["fid"], caption=out or None, **kw)
        else:
            await bot.send_message(m.chat.id, out, **kw)
    except Exception:
        await bot.send_message(m.chat.id, esc(out) or "…")
    return True


def media_of(msg):
    if not msg:
        return None, None
    for kind in ("photo", "video", "animation", "document", "audio", "voice", "sticker", "video_note"):
        v = getattr(msg, kind, None)
        if v:
            return kind, (v[-1].file_id if kind == "photo" else v.file_id)
    return None, None


def split_args(args):
    """'[режим] триггер ответ' -> (режим, триггер, ответ). Фраза-триггер — в кавычках."""
    a = (args or "").strip()
    mode = "start"
    w = a.split(None, 1)
    if len(w) == 2 and w[0].lower() in MODE_WORDS:
        mode, a = MODE_WORDS[w[0].lower()], w[1].strip()
    if a and a[0] in "\"«“":
        close = {"\"": "\"", "«": "»", "“": "”"}[a[0]]
        j = a.find(close, 1)
        if j > 0:
            return mode, norm_trig(a[1:j]), a[j + 1:].strip()
    p = a.split(None, 1)
    return (mode, norm_trig(p[0]), p[1] if len(p) > 1 else "") if p else (mode, "", "")


HOWTO = ("Формат: <code>+кмд триггер ответ</code> или ответом на сообщение: <code>+кмд триггер</code> (подойдёт и фото, гиф, стикер, файл, голосовое).\n"
         "Фраза — в кавычках: <code>+кмд \"кто тут\" Все тут</code>.\n"
         "Режим первым словом: <code>+кмд точно привет Привет!</code> · <code>начало</code> (по умолчанию) · <code>точно</code> · <code>слово</code>.")


@cmd("+кмд", "+команда", perm="settings")
async def c_add(m, args, bot):
    mode, trig, text = split_args(args)
    rep = m.reply_to_message
    mtype, fid = media_of(rep)
    if text:
        text = esc(text)  # набранное в команде — обычный текст
    elif rep and (rep.text or rep.caption):
        text = rep.html_text or ""  # из ответа берём с форматированием
    err = check_trigger(trig)
    if err or not (text or mtype):
        return await m.reply((f"❗ {err}\n\n" if err else "") + HOWTO + "\n\n" + VARS_HELP)
    upd = await save(m.chat.id, trig, text, mtype, fid, mode)
    await m.reply(f"✅ Команда <code>{esc(trig)}</code> {'обновлена' if upd else 'создана'} (срабатывает: {MODES[mode]}"
                  + (f", {mtype}" if mtype else "") + ").")


@cmd("-кмд", "-команда", perm="settings")
async def c_del(m, args, bot):
    a = (args or "").strip()
    items = sorted(await rows(m.chat.id), key=lambda r: r["trig"])
    if a.isdigit():
        r = items[int(a) - 1] if 1 <= int(a) <= len(items) else None
    else:
        _, trig, _ = split_args(a) if a[:1] in "\"«“" else ("start", norm_trig(a), "")
        r = next((x for x in items if x["trig"] == trig), None)
    if not r:
        return await m.reply("Такой команды нет. Список: <code>.кмды</code>")
    await delete(m.chat.id, r["rid"])
    await m.reply(f"🗑 Команда <code>{esc(r['trig'])}</code> удалена.")


@cmd(".кмды", ".кастом")
async def c_list(m, args, bot):
    items = sorted(await rows(m.chat.id), key=lambda r: r["trig"])
    if not items:
        return await m.reply("Своих команд пока нет.\n\n" + HOWTO)
    await m.reply("🧩 <b>Свои команды:</b>\n" + "\n".join(
        f"{i}. <code>{esc(r['trig'])}</code> · {MODES[r['mode']]} — " + (f"[{r['mtype']}] " if r["mtype"] else "")
        + esc(re.sub(r"<[^>]+>", "", r["text"])[:50]) for i, r in enumerate(items, 1))
        + "\n\nУдалить: <code>-кмд номер</code>. Всё остальное — в <code>.настройки</code> → Команды.")
