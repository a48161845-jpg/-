"""
ПРЕМИУМ-ЭМОДЗИ ДЛЯ ВСЕГО БОТА
=============================
Впишите ID премиум-эмодзи напротив обычного эмодзи — и оно заменится во ВСЕХ сообщениях бота
(команды, настройки, магазин, списки и т.д.) и на ВСЕХ кнопках. Текст остаётся на месте:
в сообщении эмодзи просто становится премиумным, на кнопке премиум-эмодзи стоит иконкой слева от надписи.

Как заполнять:  "💎": "5368324170671202286",
Пустая строка ("") — эмодзи остаётся обычным. Менять код больше нигде не нужно, после правки перезапустите бота.
Цвета кнопок настраиваются ниже, в разделе «ЦВЕТА КНОПОК».
Можно добавлять свои строки (любой эмодзи, который бот где-то пишет).
Узнать ID: перешлите сообщение с нужным премиум-эмодзи в любого бота-определителя ID (он покажет custom_emoji_id).
Важно: Telegram показывает премиум-эмодзи от бота только если у бота есть такая возможность
(например, у бота куплен дополнительный юзернейм на Fragment); иначе вместо них будут обычные эмодзи.
"""
import re

PREMIUM_EMOJI = {
    # --- баллы / магазин ---
    "💎": "",
    "💰": "",
    "🛒": "",
    "🏆": "",
    "🥇": "",
    "🥈": "",
    "🥉": "",
    "📊": "",
    "👑": "",
    # --- статусы ---
    "✅": "",
    "❌": "",
    "⚠️": "",
    "❗": "",
    "❓": "",
    "⛔": "",
    "🆕": "",
    "✖️": "",
    "👍": "",
    "👎": "",
    # --- кнопки и действия ---
    "🗑": "",
    "🚪": "",
    "➕": "",
    "➖": "",
    "🔄": "",
    "🔁": "",
    "🔗": "",
    "💾": "",
    "🧹": "",
    "✏️": "",
    "✍️": "",
    "⚙️": "",
    "🔧": "",
    "📌": "",
    # --- разделы ---
    "🧩": "",
    "📝": "",
    "📅": "",
    "🏷": "",
    "🎨": "",
    "📋": "",
    "🎯": "",
    "🖼": "",
    "⏰": "",
    "⏱": "",
    "📜": "",
    "📖": "",
    "📢": "",
    "💬": "",
    "🎮": "",
    # --- модерация / игры ---
    "🔇": "",
    "🔊": "",
    "🔨": "",
    "💣": "",
    "🖐": "",
    "🤖": "",
    "🙅": "",
    "🔴": "",
    "🏚": "",
    # --- команды и прочее ---
    "🤍": "",
    "🖤": "",
    "⭐": "",
    "👋": "",
    "😎": "",
    "🤡": "",
    "❄️": "",
    "🌸": "",
    "☀️": "",
    "🍂": "",
}


# ====================== ЦВЕТА КНОПОК ======================
# Telegram умеет красить inline-кнопки: "success" — зелёная, "danger" — красная, "primary" — синяя.
# Если слово/эмодзи есть в надписи кнопки — она красится (правила проверяются сверху вниз, побеждает первое).
# Можно править списки и добавлять свои. COLORS_ENABLED = False — отключить цвета совсем.
COLORS_ENABLED = True
BUTTON_COLORS = [
    ("danger", ["сбросить", "удалить", "отмена", "закрыть", "снести", "🗑", "❌", "⛔", "💣", "🔴", "👎", "✖️"]),
    ("success", ["✅", "👍", "💾", "🆕", "➕", "готово", "добавить", "новый", "новая"]),
    ("primary", ["💰", "📅", "📊", "🏷", "📝", "🧩", "👑", "🎨", "⏱", "🔗", "⏰", "📋", "🎯", "✏️", "💬", "👋", "📜", "🛒"]),
]
# кнопки без эмодзи (например, товары магазина) красятся по началу callback_data
COLOR_BY_CALLBACK = {"sh:buy": "primary", "ls:m": "primary"}
# цвет всех остальных кнопок (например, «Назад», «Лимит»); None — оставлять обычными
DEFAULT_COLOR = "primary"

# ====================== дальше код, его трогать не нужно ======================

_VS = "️"


def _base(e):
    return e.replace(_VS, "")


def _ids():
    return {_base(k): v.strip() for k, v in PREMIUM_EMOJI.items() if v and v.strip().isdigit()}


_cache = {"sig": None, "ids": {}, "rx": None}


def _state():
    ids = _ids()
    sig = tuple(sorted(ids.items()))
    if sig != _cache["sig"]:
        _cache["sig"], _cache["ids"] = sig, ids
        keys = sorted(ids, key=len, reverse=True)
        _cache["rx"] = re.compile("(" + "|".join(re.escape(k) for k in keys) + ")" + _VS + "?") if keys else None
    return _cache["ids"], _cache["rx"]


_TAG = re.compile(r"(<tg-emoji\b[^>]*>.*?</tg-emoji>|<[^>]*>)", re.S)


def apply_html(text):
    """В HTML-тексте заменяет эмодзи из словаря на <tg-emoji>. Теги, код и уже готовые tg-emoji не трогает."""
    if not text:
        return text
    ids, rx = _state()
    if not rx:
        return text
    out, in_code = [], 0
    for part in _TAG.split(text):
        if part.startswith("<"):
            low = part.lower()
            if low.startswith(("<code", "<pre")):
                in_code += 1
            elif low.startswith(("</code", "</pre")):
                in_code = max(0, in_code - 1)
            out.append(part)
        elif in_code:
            out.append(part)
        else:
            out.append(rx.sub(lambda m: f'<tg-emoji emoji-id="{ids[m.group(1)]}">{m.group(0)}</tg-emoji>', part))
    return "".join(out)


def split_leading(text):
    """Кнопка: если текст начинается с эмодзи из словаря -> (id, текст без него), иначе (None, текст)."""
    ids, rx = _state()
    if not rx or not text:
        return None, text
    m = rx.match(text)
    if not m:
        return None, text
    return ids[m.group(1)], text[m.end():].lstrip()


def pick_style(text, data):
    """Цвет кнопки по правилам BUTTON_COLORS (первое совпадение). Нет совпадения — DEFAULT_COLOR (по умолчанию синяя), так что красятся все кнопки."""
    low = (text or "").lower()
    for style, words in BUTTON_COLORS:
        if any(w.lower() in low for w in words):
            return style
    for prefix in COLOR_BY_CALLBACK:
        if (data or "").startswith(prefix):
            return COLOR_BY_CALLBACK[prefix]
    return DEFAULT_COLOR


def apply_markup(markup):
    """Новая inline-клавиатура: цвет кнопки и премиум-эмодзи-иконка вместо эмодзи в начале надписи (текст остаётся)."""
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return markup
    changed, new_rows = False, []
    for row in rows:
        new_row = []
        for b in row:
            upd = {}
            text = getattr(b, "text", "")
            eid, rest = split_leading(text)
            if eid and rest and not getattr(b, "icon_custom_emoji_id", None):
                upd.update(text=rest, icon_custom_emoji_id=eid)
            if COLORS_ENABLED and not getattr(b, "style", None):
                st = pick_style(text, getattr(b, "callback_data", None))
                if st:
                    upd["style"] = st
            if upd:
                data = b.model_dump(exclude_none=True)
                data.update(upd)
                b = type(b).model_validate(data)
                changed = True
            new_row.append(b)
        new_rows.append(new_row)
    return type(markup)(inline_keyboard=new_rows) if changed else markup


def install(bot):
    """Подключает замену к боту: действует на все отправки и правки сообщений."""
    from aiogram.client.session.middlewares.base import BaseRequestMiddleware

    class _Mw(BaseRequestMiddleware):
        async def __call__(self, make_request, b, method):
            try:
                pm = getattr(method, "parse_mode", "HTML")
                if type(pm).__name__ == "Default":  # Default("parse_mode") -> значение по умолчанию бота
                    pm = b.default.parse_mode
                if str(getattr(pm, "value", pm)).upper() == "HTML" and not getattr(method, "entities", None) \
                        and not getattr(method, "caption_entities", None):
                    for f in ("text", "caption"):
                        v = getattr(method, f, None)
                        if isinstance(v, str):
                            setattr(method, f, apply_html(v))
                mk = getattr(method, "reply_markup", None)
                if mk is not None and getattr(mk, "inline_keyboard", None):
                    method.reply_markup = apply_markup(mk)
            except Exception:  # замена эмодзи не должна ломать отправку
                import logging
                logging.getLogger("premium_emoji").exception("emoji replace failed")
            return await make_request(b, method)

    bot.session.middleware(_Mw())
