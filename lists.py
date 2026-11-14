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


# ---------- тематические списки ----------
# Места — ники из шаблона: «St | Мэйвис», «Mn | Jacob Portman», строка кэпа «CAP : @ник» (кэп — такое же место, без 👑).
# Ищутся по приставкам (THEME_PREFIXES), по значению после «CAP :» или по пометке @ перед ником (только при создании
# командой +темсписок). Шаблон копируется как есть, с форматированием. При наборе бот ничего не разбирает: по 👍 берёт
# имя/юзернейм нажавшего и ставит его на место, чей ник в них содержится, ссылкой на профиль.
THEME_PREFIXES = ("St", "Mn")
_AT_RE = re.compile(r"@(?P<name>\w[\w .'’\-|]*)")
_AUTO_RE = re.compile(r"(?<![^\W\d_])(?P<name>(?:" + "|".join(THEME_PREFIXES) + r")\s*\|\s*\w[\w .'’\-]*)", re.I)
_CAPVAL = re.compile(r"\s*(?P<name>@?\w[\w .'’\-|]*)")
_TAILPFX = re.compile(r"(?:" + "|".join(THEME_PREFIXES) + r")\s*\|(.*)$", re.I)
_TAGRE = re.compile(r"<[^>]*>")
_USERLINK = re.compile(r'<a href="(?:tg://user\?id=\d+|https?://t\.me/[^"]+)">(.*?)</a>', re.S)
_CODETAG = re.compile(r"<(/?)(code|pre)(\s[^>]*)?>")
_CODESTRIP = re.compile(r"</?(?:code|pre)(?:\s[^>]*)?>")
_CAPWORDS = ("cap", "кэп", "кап", "captain", "капитан")
CAPTION_MAX = 1024
# сердечко рядом с ником (после него) — часть места: при вставке заменяется именем нажавшего, второго сердечка не будет
_HEARTS = "❤♥🤍🖤🤎💙💚💛🧡💜💗💖💕💞💓💘💝💟❣🩷🩵🩶💔"
_HEART_TAIL = re.compile(r"(?:\s*[" + _HEARTS + r"]\ufe0f?)+")
_BRACKET_RE = re.compile(r"\[(?P<inner>[^\[\]\n]{2,60})\]")  # явная пометка места: [St | Мэйвис 🤍]
_STOP = {"the", "miss", "mr", "mrs", "ms", "мисс", "мистер", "миссис", "of"}


def _norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold().replace("ё", "е")
    return "".join(ch for ch in s if ch.isalnum())


def _slot_key(label):
    return _norm(html.unescape(_TAGRE.sub("", label)).split("|")[-1])


def _visible(line):
    """Видимый текст строки без тегов, приведённый NFKC (𝐒𝐭 -> St), и карта: позиция -> индекс в исходной строке."""
    chars, idx, pos = [], [], 0
    for m in list(_TAGRE.finditer(line)) + [None]:
        end = m.start() if m else len(line)
        for i in range(pos, end):
            for ch in unicodedata.normalize("NFKC", line[i]):
                chars.append(ch)
                idx.append(i)
        if m:
            pos = m.end()
    return "".join(chars), idx


def _balance_code(lines):
    """Моно (<code>/<pre>), растянутое на несколько строк, делим по строкам: каждая строка парная сама по себе."""
    stack, out = [], []
    for ln in lines:
        pre = "".join(stack)
        for m in _CODETAG.finditer(ln):
            if m.group(1):
                for k in range(len(stack) - 1, -1, -1):
                    if stack[k].startswith("<" + m.group(2)):
                        del stack[k]
                        break
            else:
                stack.append(m.group(0))
        out.append(pre + ln + "".join("</" + re.match(r"<(\w+)", t).group(1) + ">" for t in reversed(stack)))
    return out


def _ext_heart(vis, b):
    """Конец места с учётом сердечек, стоящих сразу после ника."""
    m = _HEART_TAIL.match(vis, b)
    return m.end() if m else b


def parse_themed(body):
    """Шаблон -> (template с ⟦n⟧ на месте каждого ника (вместе с сердечком рядом), [{label,key}])."""
    body = _USERLINK.sub(r"\1", body.strip("\n"))  # готовое сообщение со ссылками-упоминаниями -> обычный текст
    out, slots = [], []
    for line in _balance_code(body.split("\n")):
        vis, idx = _visible(line)
        found = []  # (начало в строке, конец в строке, начало в vis, конец в vis)

        def add(o_a, o_b, v1, v2):
            found.append((idx[o_a], idx[o_b - 1] + 1, v1, v2))

        cut = next((i for i, ch in enumerate(vis) if ch in ":➘"), -1)
        if cut != -1 and _norm(vis[:cut]) in _CAPWORDS:  # «CAP :», «C A P :», «Кэп ➘» — значение после двоеточия это место
            m = _CAPVAL.match(vis, cut + 1)
            if m:
                name = m.group("name").rstrip(" -|")
                a, b = m.start("name"), m.start("name") + len(name)
                if _slot_key(name):
                    e = _ext_heart(vis, b)
                    add(a, e, a, e)
        else:
            for m in _BRACKET_RE.finditer(vis):  # 1) [ник] — явная пометка, скобки уходят вместе с местом
                if _slot_key(re.sub("[" + _HEARTS + "\ufe0f]", "", m.group("inner"))):
                    add(m.start(), m.end(), m.start() + 1, m.end() - 1)
            if not found:
                for rx in (_AUTO_RE, _AT_RE):  # 2) «St | Имя», 3) если таких нет в строке — места с @
                    for m in rx.finditer(vis):
                        name = m.group("name").rstrip(" -|")
                        a = m.start("name") if rx is _AUTO_RE else m.start()
                        b = m.start("name") + len(name)
                        if _slot_key(name):
                            e = _ext_heart(vis, b)
                            add(a, e, a if rx is _AUTO_RE else a + 1, e)
                    if found:
                        break
        labels = [_TAGRE.sub("", vis[v1:v2]).strip() for _, _, v1, v2 in found]
        base = len(slots)
        for k in reversed(range(len(found))):
            o1, o2, v1, v2 = found[k]
            inner = line[o1:o2]
            # место помечаем его номером ⟦n⟧; теги, попавшие внутрь, возвращаем следом — разметка остаётся парной
            line = line[:o1] + f"⟦{base + k}⟧" + "".join(_TAGRE.findall(inner)) + line[o2:]
        line = re.sub(r"<(b|i|u|s|em|strong|code)></\1>|<tg-emoji[^>]*></tg-emoji>", "", line)  # пустые пары после вырезания
        slots.extend({"label": lb, "key": _slot_key(re.sub("[" + _HEARTS + "\ufe0f]", "", lb))} for lb in labels)
        out.append(line)
    return "\n".join(out), slots


def _words(s):
    s = unicodedata.normalize("NFKC", s or "").casefold().replace("ё", "е")
    return [w for w in re.findall(r"[^\W_]+", s) if len(w) >= 3 and w not in _STOP]


def find_slot(slots, taken, full_name, username=""):
    """Место для нажавшего (имя и фамилия считаются, порядок слов не важен).
    1) ник места целиком есть в имени/юзернейме — самое длинное совпадение;
    2) иначе по словам: имя или фамилия места есть среди слов в имени нажавшего — побеждает место с наибольшим числом
       совпавших слов, если оно одно (или одно свободное);
    3) иначе хвост после «St |» / «Mn |» входит ровно в одно место."""
    n = _norm(full_name) + "|" + _norm(username)
    hits = [i for i, s in enumerate(slots) if s["key"] and s["key"] in n]
    if hits:
        top = max(len(slots[i]["key"]) for i in hits)
        hits = [i for i in hits if len(slots[i]["key"]) == top]
        return next((i for i in hits if i not in taken), hits[0])
    uw = set(_words(full_name) + _words(username))
    score = {i: sum(1 for w in set(_words(s["label"].split("|")[-1])) if w in uw) for i, s in enumerate(slots)}
    score = {i: v for i, v in score.items() if v}
    if score:
        top = max(score.values())
        best = [i for i, v in score.items() if v == top]
        if len(best) == 1:
            return best[0]
        free = [i for i in best if i not in taken]
        if len(free) == 1:
            return free[0]
        return None
    m = _TAILPFX.search(unicodedata.normalize("NFKC", full_name or ""))
    tail = _norm(m.group(1)) if m else ""
    if len(tail) >= 3:
        c = [i for i, s in enumerate(slots) if s["key"] and tail in s["key"]]
        if len(c) == 1:
            return c[0]
        free = [i for i in c if i not in taken]
        if len(free) == 1:
            return free[0]
    return None


def link_label(p):
    """Нажавший встаёт на место ссылкой на профиль: есть @юзернейм — t.me/ник, нет — tg://user?id. Текст ссылки — его имя."""
    name = p["name"] if len(p["name"]) <= 40 else p["name"][:39] + "…"
    if p.get("username") and p["id"] not in config.OWNER_IDS:
        return f'<a href="https://t.me/{p["username"]}">{esc(name)}</a>'
    return mention(p["id"], name)


_MARK = re.compile(r"⟦(\d+)⟧")


def _upgrade(tpl):
    """Списки, созданные прошлыми версиями (места без номеров, кэп отдельной строкой), приводим к новому виду."""
    if "⟦" in tpl:
        return tpl
    out, n = [], 0
    for line in tpl.split("\n"):
        if CAPMARK in line:
            line = line.replace(PH, "").replace(CAPMARK, "")
        else:
            while PH in line:
                line = line.replace(PH, f"⟦{n}⟧", 1)
                n += 1
        out.append(line)
    return "\n".join(out)


def render_themed(tpl, slots, state, mode=None):
    by_slot = {p.get("slot"): p for p in state.get("players", [])}
    out = []
    for line in _upgrade(tpl).split("\n"):
        ids = [int(i) for i in _MARK.findall(line)]
        if ids:
            if any(by_slot.get(i) for i in ids):
                line = _CODESTRIP.sub("", line)  # на занятом месте моно убираем — ссылка в нём не кликается

            def put(m):
                i = int(m.group(1))
                if by_slot.get(i):
                    return link_label(by_slot[i])
                return slots[i]["label"] if i < len(slots) else ""
            line = _MARK.sub(put, line)
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
    return {"captain": state.get("captain"), "players": list(state["players"])}


async def full_text(chat_id, row, state):
    st = await sorted_state(chat_id, state)
    if row["kind"] == "themed":
        return render_themed(row["template"], json.loads(row["slots"] or "[]"), st, row["mode"])
    return render(row["template"], st, row["mode"])


def is_full(row, state):
    if row["kind"] == "themed":
        n = len(json.loads(row["slots"] or "[]"))
        return n > 0 and len(state["players"]) >= n
    slots = count_slots(row["template"])
    cap_ok = state.get("captain") is not None or not has_cap(row["template"])
    return slots > 0 and len(state["players"]) >= slots and cap_ok


def list_kb(num, themed=False):
    btns = [InlineKeyboardButton(text="👍", callback_data=f"ls:like:{num}"),
            InlineKeyboardButton(text="👎", callback_data=f"ls:dis:{num}")]
    if not themed:  # в тематических списках кэп — обычное место, короны нет
        btns.append(InlineKeyboardButton(text="👑", callback_data=f"ls:cap:{num}"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])


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
        "photo=COALESCE(excluded.photo, lists.photo), kind='normal', slots=NULL",
        (m.chat.id, num, tpl, empty_state(), photo))
    row = await db.one("SELECT photo FROM lists WHERE chat_id=? AND num=?", (m.chat.id, num))
    await m.reply(f"✅ Список №{num} создан (мест: {slots}, кэп: {'да' if has_cap(tpl) else 'нет'}"
                  f"{', 🖼 картинка сохранена' if row and row['photo'] else ''}).\nВызвать: <code>.список {num}</code>")


@cmd("+темсписок", "+тем", "+тематический", perm="lists")
async def c_create_themed(m, args, bot):
    mt = re.match(r"\s*(\d+)", args or "")
    if not mt:
        return await m.reply("Формат: <code>+темсписок 1</code> и шаблон с новой строки (или ответом на сообщение с шаблоном). "
                             "Места — ники персонажей: <code>St | Мэйвис</code> / <code>Mn | Jacob Portman</code> "
                             "или любые, помеченные собакой: <code>@Мэйвис</code>. Кэп: строка <code>CAP : @ник</code> — это такое же место.")
    num = int(mt.group(1))
    full = m.html_text or ""
    body = full.split("\n", 1)[1] if "\n" in full else ""
    if not body.strip() and m.reply_to_message:
        body = m.reply_to_message.html_text or ""
    if not body.strip():
        return await m.reply("Нет шаблона: напишите его со следующей строки или ответьте на сообщение с ним.")
    tpl, slots = parse_themed(body)
    if not slots:
        return await m.reply("Не нашёл ников-мест. Пишите их как <code>St | Имя</code> / <code>Mn | Имя</code> "
                             "или пометьте собакой: <code>@Имя персонажа</code> (собака нужна только здесь, в шаблоне).")
    photo = _photo_id(m) or _photo_id(m.reply_to_message)
    await db.x(
        "INSERT INTO lists(chat_id,num,template,state,msg_id,mode,photo,kind,slots) VALUES(?,?,?,?,NULL,'name',?,'themed',?) "
        "ON CONFLICT(chat_id,num) DO UPDATE SET template=excluded.template,state=excluded.state,msg_id=NULL,"
        "photo=COALESCE(excluded.photo, lists.photo), kind='themed', slots=excluded.slots",
        (m.chat.id, num, tpl, empty_state(), photo, json.dumps(slots, ensure_ascii=False)))
    names = ", ".join(esc(s["label"]) for s in slots)
    await m.reply(f"✅ Тематический список №{num} создан (мест: {len(slots)}).\n"
                  f"Ники-места: {names}\nВызвать: <code>.список {num}</code>. "
                  "При наборе бот сам ставит нажавшего 👍 на место, чей ник есть в его имени.")


@cmd(".темтест", perm="lists")
async def c_theme_test(m, args, bot):
    """.темтест 1 St | Мэйвис — на какое место встанет человек с таким именем."""
    mt = re.match(r"\s*(\d+)\s+(.+)", args or "", re.S)
    if not mt:
        return await m.reply("Формат: <code>.темтест 1 St | Мэйвис</code>")
    row = await db.one("SELECT * FROM lists WHERE chat_id=? AND num=?", (m.chat.id, int(mt.group(1))))
    if not row or row["kind"] != "themed":
        return await m.reply("Такого тематического списка нет.")
    sl = json.loads(row["slots"] or "[]")
    i = find_slot(sl, set(), mt.group(2), "")
    await m.reply(f"✅ Встанет на место: {esc(sl[i]['label'])}" if i is not None else "❌ Ни одно место не подошло.")


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
    rows = await db.q("SELECT num, photo, kind FROM lists WHERE chat_id=? ORDER BY num", (m.chat.id,))
    await m.reply("📋 Номера списков: " + (", ".join(f"{r['num']}{'🖼' if r['photo'] else ''}{'🎭' if r['kind'] == 'themed' else ''}" for r in rows) if rows else "пока нет"))


async def _push(bot, chat, row, state):
    """Обновляет сообщение со списком (подпись или текст), кнопки пропадают у заполненного."""
    text = await full_text(chat, row, state)
    kb = None if is_full(row, state) else list_kb(row["num"], row["kind"] == "themed")
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
        if row["kind"] == "themed":
            continue  # приставка тематических списков не касается
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
    themed = row["kind"] == "themed"
    kb = list_kb(num, themed)
    photo = row["photo"]
    if photo:
        # подпись к фото ограничена 1024 символами — если список длинный, шлём фото отдельно
        shown = html.unescape(plain(text))
        if themed:  # тематический: длина известна точно (имена в подписи урезаются до 40 знаков), картинка идёт вместе со списком
            fits = len(shown.encode("utf-16-le")) // 2 <= CAPTION_MAX
        else:
            fits = len(shown) + 20 * (count_slots(row["template"]) + 6) <= 1000
        if fits:
            try:
                return await bot.send_photo(chat, photo, caption=text, reply_markup=kb)
            except TelegramBadRequest as e:
                if "too long" not in str(e).lower():
                    raise
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
            if action == "cap" and row["kind"] == "themed":
                return await call.answer("В этом списке нет места кэпа")
            prefix = "" if row["kind"] == "themed" else (await db.get_setting(chat, "prefix", "")).strip()  # в тематических приставка не нужна: смотрим только на ник
            if prefix and u.id not in config.OWNER_IDS and not _has_prefix(u.full_name, prefix):
                return await call.answer(f"Без приставки встать в список нельзя. Добавьте в имя: {prefix}", show_alert=True)
            if await in_other_team_list(chat, u.id):
                return await call.answer("Вы уже записаны в списке другой команды 🙅", show_alert=True)
            if action == "like":
                if in_pl or is_c:
                    return await call.answer("Вы уже в списке")
                if row["kind"] == "themed":
                    tslots = json.loads(row["slots"] or "[]")
                    taken = {p.get("slot") for p in players}
                    idx = find_slot(tslots, taken, u.full_name, u.username or "")
                    if idx is None:
                        free = ", ".join(s["label"] for i, s in enumerate(tslots) if i not in taken)
                        nm = (u.full_name or "")[:22]
                        return await call.answer(f"В вашем имени «{nm}» нет ника ни одного места. Свободные: {free}"[:195], show_alert=True)
                    if idx in taken:
                        return await call.answer("Это место уже занято", show_alert=True)
                    me["slot"] = idx
                    players.append(me)
                else:
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
            await _edit(call, await full_text(chat, row, st), None if full else list_kb(num, row["kind"] == "themed"))
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
