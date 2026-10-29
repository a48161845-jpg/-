import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}

# Канал, где хранится общий список баллов
POINTS_CHANNEL_ID = int(os.getenv("POINTS_CHANNEL_ID", "-1004327950960"))
POINTS_CHANNEL_LINK = os.getenv("POINTS_CHANNEL_LINK", "")

# Необязательно: MTProto (Telethon) — чтобы бот видел всех участников и находил их по @нику
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")

TZ_NAME = os.getenv("TZ_NAME", "Europe/Moscow")
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))  # запасной вариант без tzdata
# Ежедневная синхронизация участников — в 00:00 по МСК
DAILY_CHECK_HOUR = 0

# Голодные игры: с 20:00 до 21:00 (МСК) заход в команды закрыт, после 21:00 бот принимает заявки
JOIN_CLOSE_FROM = 20 * 60
JOIN_CLOSE_TO = 21 * 60

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
