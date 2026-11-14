import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# @ники создателя, которых бот никогда не выводит тегом (через запятую, без @)
OWNER_USERNAMES = {x.lower().lstrip("@") for x in os.getenv("OWNER_USERNAMES", "n0irxs").replace(" ", "").split(",") if x}
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}

# Канал, где кэпы вручную выдают баллы (формат строк: Ник (id) - баллы); бот только читает его
POINTS_CHANNEL_ID = int(os.getenv("POINTS_CHANNEL_ID", "-1003957141651"))
# Общий список баллов бот ведёт в том же канале (https://t.me/hungergamesbm): висит закреплённым, кэпы правят значения прямо в нём
LIST_CHANNEL_ID = int(os.getenv("LIST_CHANNEL_ID", str(POINTS_CHANNEL_ID)))
# Номер сообщения со списком больше НЕ зашит в коде: баллы берутся из всех сообщений канала, а сообщение списка бот
# запоминает сам (при первом запуске — закреплённое сообщение канала). 0 = определять автоматически (рекомендуется).
LIST_MESSAGE_ID = int(os.getenv("LIST_MESSAGE_ID", "0") or 0)
LIST_CHANNEL_LINK = os.getenv("LIST_CHANNEL_LINK", "https://t.me/hungergamesbm")

# Необязательно: MTProto (Telethon) — чтобы бот видел всех участников и находил их по @нику
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")

# Онлайн-база PostgreSQL, например postgresql://user:password@host:5432/dbname?sslmode=require
DATABASE_URL = os.environ.get("DATABASE_URL", "")

TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))  # запасной вариант без tzdata
# Ежедневная синхронизация участников — в 00:00 по МСК
DAILY_CHECK_HOUR = 0

# Голодные игры: с 20:00 до 21:00 (МСК) заход в команды закрыт, после 21:00 бот принимает заявки
JOIN_CLOSE_FROM = 20 * 60
JOIN_CLOSE_TO = 21 * 60
GAME_LENGTH_MIN = 60  # игра идёт час: после 21:00 ограничения «перед игрой» снимаются

TEAMS = {
    "starlight": {
        "chat_id": int(os.getenv("STARLIGHT_CHAT_ID", "0")),
        "title": "𝐒𝐭𝐚𝐫𝐥𝐢𝐠𝐡𝐭 𝐓𝐞𝐚𝐦 🤍",
    },
    "midnight": {
        "chat_id": int(os.getenv("MIDNIGHT_CHAT_ID", "0")),
        "title": "𝐌𝐢𝐝𝐧𝐢𝐠𝐡𝐭 𝐓𝐞𝐚𝐦 🖤",
    },
}


def team_key(chat_id: int):
    for k, t in TEAMS.items():
        if t["chat_id"] and t["chat_id"] == chat_id:
            return k
    return None


def other_team(key: str) -> str:
    return "midnight" if key == "starlight" else "starlight"
