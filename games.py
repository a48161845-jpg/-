import calendar
import io
import math
import os
import random
import json
import re
from datetime import datetime, timedelta
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

import config
import db
from core import cb, cmd, esc

try:
    TZ = ZoneInfo(config.TZ_NAME)
except ZoneInfoNotFoundError:
    # нет базы часовых поясов (например, Termux) — берём фиксированное смещение
    TZ = timezone(timedelta(hours=config.TZ_OFFSET_HOURS))
FMT = "%Y-%m-%d %H:%M"
KINDS = {"simple": "Простая", "bt": "ВТ", "mixed": "Смешанная"}
ALIASES = {"simple": "simple", "простая": "simple", "bt": "bt", "вт": "bt", "mixed": "mixed", "смеш": "mixed", "смешанная": "mixed"}
MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

PALETTE = {
    "Стандарт": None, "Красный": (235, 87, 87), "Оранжевый": (242, 153, 74), "Жёлтый": (242, 201, 76),
    "Зелёный": (39, 174, 96), "Голубой": (86, 204, 242), "Синий": (47, 128, 237),
    "Фиолетовый": (155, 81, 224), "Розовый": (240, 98, 146), "Серый": (110, 110, 120),
}
DEFAULT_RGB = {"simple": (66, 135, 245), "bt": (235, 87, 87), "mixed": (160, 95, 230), "past": (80, 80, 90)}


def now():
    return datetime.now(TZ).replace(tzinfo=None)


def parse_dt(d, t):
    try:
        day, mon = [int(x) for x in re.split(r"[./]", d)[:2]]
        hh, mm = [int(x) for x in t.split(":")[:2]]
        n = now()
        dt = datetime(n.year, mon, day, hh, mm)
        if dt < n - timedelta(days=1):
            dt = dt.replace(year=n.year + 1)
        return dt
    except Exception:
        return None


async def next_game(chat_id):
    r = await db.one("SELECT * FROM games WHERE chat_id=? AND dt>=? ORDER BY dt LIMIT 1", (chat_id, now().strftime(FMT)))
    return r


@cmd("+игра", perm="games")
async def c_add(m, args, bot):
    p = (args or "").split(None, 2)
    dt = parse_dt(p[0], p[1]) if len(p) >= 2 else None
    if not dt:
        return await m.reply("Формат: <code>+игра 17.10 20:00 описание</code>")
    descr = p[2] if len(p) > 2 else ""
    kind = await db.get_setting(m.chat.id, "game_kind", "simple")
    w = descr.split(None, 1)
    if w and w[0].lower() in ALIASES:
        kind = ALIASES[w[0].lower()]
        descr = w[1] if len(w) > 1 else ""
    await db.x("INSERT INTO games(chat_id,dt,descr,kind) VALUES(?,?,?,?)", (m.chat.id, dt.strftime(FMT), descr, kind))
    await m.reply(f"✅ Игра добавлена: <b>{dt:%d.%m %H:%M}</b> ({KINDS[kind]}) {esc(descr)}")


@cmd(".расп")
async def c_schedule(m, args, bot):
    rows = await db.q("SELECT * FROM games WHERE chat_id=? AND dt>=? ORDER BY dt", (m.chat.id, now().strftime(FMT)))
    if not rows:
        return await m.reply("Игр в расписании нет.")
    lines = ["📅 <b>Расписание:</b>"]
    for r in rows:
        dt = datetime.strptime(r["dt"], FMT)
        lines.append(f"• {dt:%d.%m %H:%M} — {esc(r['descr']) or '—'} ({KINDS.get(r['kind'], '')})")
    await m.reply("\n".join(lines))


@cmd(".отменить", perm="games")
async def c_cancel(m, args, bot):
    p = (args or "").split()
    dt = parse_dt(p[0], p[1]) if len(p) >= 2 else None
    if not dt:
        return await m.reply("Формат: <code>.отменить 17.10 20:00</code>")
    r = await db.one("SELECT id FROM games WHERE chat_id=? AND dt=?", (m.chat.id, dt.strftime(FMT)))
    if not r:
        return await m.reply("Такой игры нет.")
    await db.x("DELETE FROM games WHERE id=?", (r["id"],))
    await m.reply("🗑 Игра удалена.")


# ---------- календарь ----------
async def _color(chat_id, kind, default=None):
    name = await db.get_setting(chat_id, f"col_{kind}", "Стандарт")
    return PALETTE.get(name) or default or DEFAULT_RGB[kind]


FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def _font(size, bold=True):
    from PIL import ImageFont
    names = (["DejaVuSans-Bold.ttf"] if bold else []) + ["DejaVuSans.ttf"]
    paths = [os.path.join(FONT_DIR, n) for n in names] + [
        "/system/fonts/Roboto-Regular.ttf", "/system/fonts/DroidSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _month_range(year, month):
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), month % 12 + 1, 1)
    return start.strftime(FMT), end.strftime(FMT)


THEMES = {
    "winter": {"name": "Зима", "emoji": "❄️", "bg": ((8, 18, 44), (52, 100, 150)), "cell": (255, 255, 255, 30),
               "text": (228, 240, 255), "weekend": (255, 150, 182), "accent": (170, 215, 255),
               "kinds": {"simple": (74, 168, 255), "bt": (255, 105, 150), "mixed": (170, 135, 255), "past": (96, 112, 145)}},
    "spring": {"name": "Весна", "emoji": "🌸", "bg": ((18, 50, 42), (104, 168, 116)), "cell": (255, 255, 255, 36),
               "text": (242, 252, 238), "weekend": (255, 150, 190), "accent": (255, 192, 216),
               "kinds": {"simple": (52, 186, 118), "bt": (255, 105, 158), "mixed": (190, 128, 240), "past": (100, 132, 110)}},
    "summer": {"name": "Лето", "emoji": "☀️", "bg": ((12, 84, 132), (255, 172, 92)), "cell": (0, 40, 72, 96),
               "text": (255, 255, 255), "weekend": (255, 228, 120), "accent": (255, 224, 128),
               "kinds": {"simple": (0, 168, 232), "bt": (255, 88, 78), "mixed": (170, 90, 232), "past": (84, 104, 124)}},
    "autumn": {"name": "Осень", "emoji": "🍂", "bg": ((38, 18, 12), (160, 76, 30)), "cell": (255, 255, 255, 26),
               "text": (255, 240, 218), "weekend": (255, 172, 112), "accent": (255, 192, 104),
               "kinds": {"simple": (242, 152, 52), "bt": (222, 72, 52), "mixed": (176, 92, 164), "past": (122, 92, 74)}},
}


def season_of(month):
    return "winter" if month in (12, 1, 2) else "spring" if month in (3, 4, 5) else "summer" if month in (6, 7, 8) else "autumn"


def _lens(cx, cy, length, width, ang, n=14):
    """Листик/лепесток: линза длиной length, повёрнутая на угол ang."""
    pts = [(length * i / n, width * math.sin(math.pi * i / n)) for i in range(n + 1)]
    pts += [(length * i / n, -width * math.sin(math.pi * i / n)) for i in range(n, -1, -1)]
    ca, sa = math.cos(ang), math.sin(ang)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts]


def _decor(season, W, H, seed):
    """Прозрачный слой с сезонным декором: снежинки / лепестки / солнце и блики / листья."""
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if season == "winter":
        for _ in range(80):
            x, y, r = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(2, 6)
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rnd.randint(60, 150)))
        for _ in range(10):
            x, y, r, base = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(16, 32), rnd.uniform(0, 1)
            col = (255, 255, 255, rnd.randint(100, 180))
            for k in range(3):
                a = base + math.pi * k / 3
                dx, dy = math.cos(a) * r, math.sin(a) * r
                d.line((x - dx, y - dy, x + dx, y + dy), fill=col, width=3)
                for s in (-1, 1):
                    d.ellipse((x + s * dx - 3, y + s * dy - 3, x + s * dx + 3, y + s * dy + 3), fill=col)
    elif season == "spring":
        for _ in range(46):
            x, y = rnd.uniform(0, W), rnd.uniform(0, H)
            col = rnd.choice([(255, 183, 208), (255, 214, 226), (255, 255, 255), (250, 160, 190)]) + (rnd.randint(110, 200),)
            d.polygon(_lens(x, y, rnd.uniform(22, 40), rnd.uniform(8, 14), rnd.uniform(0, 6.28)), fill=col)
        for _ in range(6):
            x, y = rnd.uniform(40, W - 40), rnd.uniform(20, H - 20)
            for k in range(5):
                a = 2 * math.pi * k / 5
                d.polygon(_lens(x, y, 24, 10, a), fill=(255, 205, 225, 190))
            d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(255, 220, 90, 230))
    elif season == "summer":
        for _ in range(22):
            x, y, r = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(14, 62)
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 240, 190, rnd.randint(18, 50)))
        sx, sy = W - 112, 70
        for k in range(12):
            a = 2 * math.pi * k / 12
            d.line((sx + math.cos(a) * 52, sy + math.sin(a) * 52, sx + math.cos(a) * 74, sy + math.sin(a) * 74),
                   fill=(255, 226, 120, 215), width=6)
        d.ellipse((sx - 40, sy - 40, sx + 40, sy + 40), fill=(255, 226, 120, 240))
    else:
        pal = [(214, 90, 36), (240, 152, 50), (182, 60, 40), (232, 192, 72), (150, 72, 30)]
        for _ in range(40):
            x, y, ln = rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(32, 54)
            ang = rnd.uniform(0, 6.28)
            d.polygon(_lens(x, y, ln, rnd.uniform(11, 18), ang), fill=rnd.choice(pal) + (rnd.randint(130, 210),))
            d.line((x, y, x + math.cos(ang) * ln, y + math.sin(ang) * ln), fill=(90, 40, 20, 150), width=2)
    return layer


async def calendar_image(chat_id, year, month):
    """Календарь текущего месяца в стиле времени года: дни с играми светятся, под числом — время игры."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    th = THEMES[season_of(month)]
    start, end = _month_range(year, month)
    rows = await db.q("SELECT dt,kind FROM games WHERE chat_id=? AND dt>=? AND dt<? ORDER BY dt", (chat_id, start, end))
    days = {}
    for r in rows:
        d0 = datetime.strptime(r["dt"], FMT)
        days.setdefault(d0.day, (r["kind"], d0.strftime("%H:%M")))
    colors = {k: await _color(chat_id, k, th["kinds"][k]) for k in ("simple", "bt", "mixed", "past")}

    weeks = calendar.monthcalendar(year, month)
    cell, pad, head = 132, 36, 206
    W, H = pad * 2 + cell * 7, head + cell * len(weeks) + pad + 16
    today = now().date()

    def box(wi, di):
        x0, y0 = pad + di * cell + 7, head + wi * cell + 7
        return (x0, y0, x0 + cell - 14, y0 + cell - 14)

    def game_col(day):
        is_past = datetime(year, month, day).date() < today
        col = colors["past"] if is_past else colors[days[day][0]]
        return tuple(int(c * (0.55 if is_past else 1.0)) for c in col), is_past

    # фон: вертикальный градиент + сезонный декор
    top, bottom = [Image.new("RGB", (W, H), c) for c in th["bg"]]
    img = Image.composite(bottom, top, Image.linear_gradient("L").resize((W, H))).convert("RGBA")
    decor = _decor(season_of(month), W, H, year * 100 + month)
    alpha = decor.split()[3]
    grid_box = (pad, head, W - pad, H - pad)  # внутри сетки декор приглушаем, чтобы цифры читались
    alpha.paste(alpha.crop(grid_box).point(lambda v: int(v * 0.3)), grid_box)
    decor.putalpha(alpha)
    img = Image.alpha_composite(img, decor).convert("RGB")

    # свечение дней с играми
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            if day in days:
                col, is_past = game_col(day)
                gd.rounded_rectangle(box(wi, di), radius=26, fill=col if is_past else tuple(min(255, int(c * 1.1)) for c in col))
    img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(22)))

    # «стеклянные» плашки дней
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(over)
    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            if not day:
                continue
            b = box(wi, di)
            is_today = datetime(year, month, day).date() == today
            ol = (255, 255, 255, 255) if is_today else None
            if day in days:
                col, _ = game_col(day)
                od.rounded_rectangle(b, radius=26, fill=col + (255,), outline=ol, width=4)
                od.rounded_rectangle((b[0] + 3, b[1] + 3, b[2] - 3, b[1] + (b[3] - b[1]) // 2), radius=22, fill=(255, 255, 255, 16))
            else:
                od.rounded_rectangle(b, radius=26, fill=th["cell"], outline=ol or (255, 255, 255, 46), width=4 if is_today else 1)
    img = Image.alpha_composite(img.convert("RGBA"), over).convert("RGB")
    d = ImageDraw.Draw(img)

    # шапка
    d.text((W // 2 + 3, 67), f"{MONTHS[month]} {year}", fill=(0, 0, 0), font=_font(62), anchor="mm")
    d.text((W // 2, 64), f"{MONTHS[month]} {year}", fill=(255, 255, 255), font=_font(62), anchor="mm")
    d.text((W // 2, 118), f"—  {th['name']}  —", fill=th["accent"], font=_font(28, bold=False), anchor="mm")
    d.line((pad + 60, 148, W - pad - 60, 148), fill=th["accent"], width=2)
    for i, wd in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]):
        d.text((pad + i * cell + cell // 2, 180), wd, fill=th["weekend"] if i >= 5 else th["accent"], font=_font(30), anchor="mm")

    for wi, week in enumerate(weeks):
        for di, day in enumerate(week):
            if not day:
                continue
            b = box(wi, di)
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            if day in days:
                d.text((cx, cy - 14), str(day), fill=(255, 255, 255), font=_font(44), anchor="mm")
                d.text((cx, cy + 30), days[day][1], fill=(255, 255, 255), font=_font(24, bold=False), anchor="mm")
            else:
                d.text((cx, cy), str(day), fill=th["weekend"] if di >= 5 else th["text"], font=_font(40), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@cmd(".календарь")
async def c_calendar(m, args, bot):
    n = now()
    img = await calendar_image(m.chat.id, n.year, n.month)
    start, end = _month_range(n.year, n.month)
    rows = await db.q("SELECT dt,descr,kind FROM games WHERE chat_id=? AND dt>=? AND dt<? ORDER BY dt", (m.chat.id, start, end))
    lines = [f"{THEMES[season_of(n.month)]['emoji']} <b>{MONTHS[n.month]} {n.year}</b>"]
    for r in rows:
        dt = datetime.strptime(r["dt"], FMT)
        lines.append(f"• {dt:%d.%m %H:%M} — {esc(r['descr']) or KINDS.get(r['kind'], '')}")
    if not rows:
        lines.append("Игр в этом месяце нет.")
    await m.answer_photo(BufferedInputFile(img, "calendar.png"), caption="\n".join(lines)[:1000])


# ---------- опросы ----------
class SafeDict(dict):
    def __missing__(self, k):
        return "{" + k + "}"


async def send_template(bot, chat_id, tpl, game=None):
    target = chat_id
    ch = await db.get_setting(chat_id, "poll_channel", "")
    if ch:
        target = int(ch)
    info = SafeDict(time="", date="", desc="")
    if game:
        dt = datetime.strptime(game["dt"], FMT)
        info = SafeDict(time=f"{dt:%H:%M}", date=f"{dt:%d.%m}", desc=game["descr"] or "")
    question = tpl["question"].format_map(info)[:300]
    options = json.loads(tpl["options"])
    kw = dict(is_anonymous=bool(tpl["anon"]))
    if tpl["quiz"]:
        kw.update(type="quiz", correct_option_id=min(tpl["correct"], len(options) - 1))
    else:
        kw.update(allows_multiple_answers=bool(tpl["multi"]))
    msg = await bot.send_poll(target, question, options, **kw)
    if await db.get_setting(chat_id, "auto_pin", "1") == "1":
        await pin(bot, target, msg.message_id)
    return msg


async def pin(bot, chat_id, message_id):
    """Закрепляет сообщение; если у бота нет права закреплять — молча пропускает."""
    try:
        await bot.pin_chat_message(chat_id, message_id)
    except Exception:
        pass


@cmd(".опрос", perm="games")
async def c_poll(m, args, bot):
    name = (args or "").strip()
    tpl = await db.one("SELECT * FROM poll_tpl WHERE chat_id=? AND name=?", (m.chat.id, name))
    if not tpl:
        rows = await db.q("SELECT name FROM poll_tpl WHERE chat_id=?", (m.chat.id,))
        names = ", ".join(r["name"] for r in rows) or "нет шаблонов (создайте в .настройки → Опросы)"
        return await m.reply(f"Формат: <code>.опрос имя_шаблона</code>\nШаблоны: {esc(names)}")
    await send_template(bot, m.chat.id, tpl, await next_game(m.chat.id))


async def run_auto_polls(bot):
    n = now()
    chats = await db.q("SELECT DISTINCT chat_id FROM games WHERE dt>=?", (n.strftime(FMT),))
    for c in chats:
        chat = c["chat_id"]
        if await db.get_setting(chat, "auto_polls", "1") != "1":
            continue
        timers = sorted({float(x) for x in (await db.get_setting(chat, "timers", "")).split(",") if x.strip()}, reverse=True)
        tpls = await db.q("SELECT * FROM poll_tpl WHERE chat_id=? ORDER BY id", (chat,))
        if not timers or not tpls:
            continue
        queue = await db.get_setting(chat, "queue", "0") == "1"
        for g in await db.q("SELECT * FROM games WHERE chat_id=? AND dt>=?", (chat, n.strftime(FMT))):
            left = (datetime.strptime(g["dt"], FMT) - n).total_seconds() / 3600
            for idx, h in enumerate(timers):
                if not (h - 0.5 < left <= h):
                    continue
                chosen = [tpls[idx % len(tpls)]] if queue else tpls
                for t in chosen:
                    if await db.one("SELECT 1 FROM poll_sent WHERE game_id=? AND tpl=? AND hours=?", (g["id"], t["name"], h)):
                        continue
                    await db.x("INSERT INTO poll_sent VALUES(?,?,?)", (g["id"], t["name"], h))
                    try:
                        await send_template(bot, chat, t, g)
                    except Exception:
                        pass


# ---------- опрос на приз ----------
PRIZE_QUESTION = "🏆 Выберите приз за победу"
PRIZE_OPTIONS = ["Антитаргет", "400 евро", "Преф"]


@cmd(".приз", ".призы", perm="games")
async def c_prize(m, args, bot):
    """Отправляет в чат опрос на приз с тремя вариантами."""
    msg = await bot.send_poll(m.chat.id, PRIZE_QUESTION, PRIZE_OPTIONS, is_anonymous=False, allows_multiple_answers=False)
    await pin(bot, m.chat.id, msg.message_id)  # опрос на приз закрепляется всегда
