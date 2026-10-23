import re
from datetime import datetime

import config
import db
from core import PREFIXES, REG, canon, esc, mention, user_link
from core import cmd, resolve_target

VARS_HELP = """\
<b>Переменные:</b>
<code>{args}</code> — всё, что написано после команды
<code>{1}</code> <code>{2}</code> … — первое, второе слово после команды
<code>{имя_фамилия}</code> (или <code>{Name и фамилия}</code>) — текст после команды, а если его нет — имя автора/человека из ответа
<code>{name}</code> — имя автора, <code>{first}</code> — имя, <code>{last}</code> — фамилия
<code>{username}</code> — @ник автора, <code>{id}</code> — ID, <code>{mention}</code> — упоминание
<code>{reply}</code> — имя того, на чьё сообщение ответили (<code>{reply_username}</code>, <code>{reply_id}</code>, <code>{reply_mention}</code>)
<code>{points}</code> и <code>{total}</code> — баллы автора
<code>{team}</code> — команда, <code>{chat}</code> — чат, <code>{date}</code>, <code>{time}</code>"""


def _norm(k):
    return re.sub(r"[\s_]+", "", k.lower())


async def _vars(m, args):
    from economy import get_points, get_total
    u = m.from_user
    r = m.reply_to_message.from_user if m.reply_to_message and m.reply_to_message.from_user else None
    first, _, last = u.full_name.partition(" ")
    words = args.split()
    key = config.team_key(m.chat.id)
    from games import now
    full = args.strip() or (r.full_name if r else u.full_name)
    v = {
        "args": args.strip(), "текст": args.strip(),
        "name": u.full_name, "имя": first, "first": first, "last": last, "фамилия": last,
        "username": f"@{u.username}" if u.username else u.full_name, "id": str(u.id),
        "mention": mention(u.id, u.full_name),
        "reply": r.full_name if r else "", "reply_username": (f"@{r.username}" if r.username else r.full_name) if r else "",
        "replyid": str(r.id) if r else "", "replymention": mention(r.id, r.full_name) if r else "",
        "points": f"{await get_points(u.id, key):g}" if key else "0", "total": f"{await get_total(u.id):g}",
        "team": config.TEAMS[key]["title"] if key else "", "chat": m.chat.title or "",
        "date": now().strftime("%d.%m.%Y"), "time": now().strftime("%H:%M"),
        "nameифамилия": full, "имяифамилия": full, "имяфамилия": full,
        "full": full, "fullname": full,
    }
    for i, w in enumerate(words, 1):
        v[str(i)] = w
    return v


def _esc_vals(v, raw=("mention", "replymention")):
    return {k: (x if k in raw else esc(x)) for k, x in v.items()}


async def render(m, text, args):
    v = _esc_vals(await _vars(m, args))
    return re.sub(r"\{([^{}]+)\}", lambda mo: v.get(_norm(mo.group(1)), mo.group(0)), text)


_cache = {}


async def _rows(chat_id):
    if chat_id not in _cache:
        rows = await db.q("SELECT name,text,mtype,fid FROM custom WHERE chat_id=?", (chat_id,))
        _cache[chat_id] = sorted(rows, key=lambda r: -len(r["name"]))
    return _cache[chat_id]


def _bound(text, phrase):
    return text.startswith(phrase) and (len(text) == len(phrase) or text[len(phrase)].isspace())


async def run_custom(m, text, bot):
    """Ищет пользовательскую команду по началу сообщения; True, если нашлась и выполнена."""
    t = text.strip()
    low = t.lower()
    hit = None
    for r in await _rows(m.chat.id):
        trig = r["name"]
        if trig[0] in PREFIXES:                      # только с префиксом: .ник !ник ,ник /ник
            ph = trig[1:]
            if low[:1] in PREFIXES and _bound(low[1:].replace("_", ""), ph.replace("_", "")):
                hit = (r, 1 + len(ph))
        else:                                        # без префикса: «ник», «.ник», «кто тут»
            if _bound(low, trig):
                hit = (r, len(trig))
            elif low[:1] in PREFIXES and _bound(low[1:], trig):
                hit = (r, 1 + len(trig))
        if hit:
            break
    if not hit:
        return False
    r, n = hit
    args = t[n:].strip()
    out = await render(m, r["text"] or "", args) if r["text"] else ""
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


def _media(msg):
    if not msg:
        return None, None
    for kind in ("photo", "video", "animation", "document", "audio", "voice", "sticker", "video_note"):
        v = getattr(msg, kind, None)
        if v:
            return kind, (v[-1].file_id if kind == "photo" else v.file_id)
    return None, None


def _split_trigger(args):
    """'"кто тут" ответ' -> ('кто тут','ответ'); 'ник текст' -> ('ник','текст')."""
    a = args.strip()
    if a and a[0] in "\"«“":
        close = {"\"": "\"", "«": "»", "“": "”"}[a[0]]
        j = a.find(close, 1)
        if j > 0:
            return a[1:j].strip().lower(), a[j + 1:].strip()
    p = a.split(None, 1)
    return (p[0].lower(), p[1] if len(p) > 1 else "") if p else ("", "")


@cmd("+кмд", "+команда", perm="settings")
async def c_add(m, args, bot):
    trig, text = _split_trigger(args)
    rep = m.reply_to_message
    mtype, fid = _media(rep)
    if rep and not text and not mtype:
        text = rep.text or rep.caption or ""
    if rep and mtype and not text:
        text = rep.caption or ""
    if not trig or not (text or mtype):
        return await m.reply(
            "Формат: <code>+кмд ник MD | {Name и фамилия}</code> — потом <code>.ник Иван Иванов</code> → <code>MD | Иван Иванов</code>\n"
            "• Триггер любой: слово, фраза в кавычках <code>+кмд \"кто тут\" Все тут</code>, с точкой (<code>.ник</code> — только с префиксом) или без.\n"
            "• Данные любые: текст, а также фото/гиф/видео/стикер/файл/голосовое — ответьте <code>+кмд триггер [подпись]</code> на сообщение с этим.\n\n" + VARS_HELP)
    base = trig[1:] if trig[0] in PREFIXES else trig
    if not base or trig[0] in "+-" or canon(base) in REG:
        return await m.reply("❗ Такое имя занято встроенной командой или некорректно.")
    await db.x("INSERT OR REPLACE INTO custom(chat_id,name,text,mtype,fid) VALUES(?,?,?,?,?)", (m.chat.id, trig, text, mtype, fid))
    _cache.pop(m.chat.id, None)
    await m.reply(f"✅ Триггер <code>{esc(trig)}</code> создан" + (f" ({mtype})" if mtype else "") + ".")


@cmd("-кмд", "-команда", perm="settings")
async def c_del(m, args, bot):
    trig, _ = _split_trigger(args)
    row = await db.one("SELECT 1 x FROM custom WHERE chat_id=? AND name=?", (m.chat.id, trig))
    if not row:
        return await m.reply("Такого триггера нет. Список: <code>.кмды</code>")
    await db.x("DELETE FROM custom WHERE chat_id=? AND name=?", (m.chat.id, trig))
    _cache.pop(m.chat.id, None)
    await m.reply(f"🗑 Триггер <code>{esc(trig)}</code> удалён.")


@cmd(".кмды", ".кастом")
async def c_list(m, args, bot):
    rows = await _rows(m.chat.id)
    if not rows:
        return await m.reply("Своих команд пока нет. Создать: <code>+кмд ник MD | {Name и фамилия}</code>\n\n" + VARS_HELP)
    await m.reply("🧩 <b>Свои команды:</b>\n" + "\n".join(
        f"• <code>{esc(r['name'])}</code> — " + (f"[{r['mtype']}] " if r["mtype"] else "") + esc((r["text"] or "")[:60]) for r in sorted(rows, key=lambda r: r["name"])))
