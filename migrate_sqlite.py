"""Разовый перенос данных из локальной bot.db (SQLite) в онлайн-базу PostgreSQL.

Использование:
    1. В .env задайте DATABASE_URL (и STARLIGHT_CHAT_ID / MIDNIGHT_CHAT_ID, как для бота).
    2. Остановите бота.
    3. python migrate_sqlite.py bot.db

Скрипт безопасно запускать повторно: уже перенесённые строки не дублируются.
Баллы по командам (старые таблицы points/spent) сливаются в единый баланс.
"""
import asyncio
import sqlite3
import sys

import config
import db

COPY = ["users", "members", "settings", "meta", "lists", "games", "poll_tpl", "poll_sent",
        "shop", "custom", "warns", "history", "caps", "bank", "done_lists", "join_queue"]
SERIAL = {"games": "id", "shop": "id", "warns": "id", "history": "id", "poll_tpl": "id", "custom": "rid"}


def build_bank(src):
    """Единый баланс из старых points/spent. Основная команда: где человек состоит; если в обоих —
    где больше баллов, затем та, чья приставка в имени, затем где появился раньше."""
    def has(t):
        return src.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None
    pts, mem, spent = {}, {}, {}
    if has("points"):
        for uid, team, value, absent in src.execute("SELECT user_id,team,value,absent FROM points"):
            pts.setdefault(uid, {})[team] = (value or 0, absent or 0)
    for key, t in config.TEAMS.items():
        if t["chat_id"]:
            for uid, rid in src.execute("SELECT user_id, rowid FROM members WHERE chat_id=?", (t["chat_id"],)):
                mem.setdefault(uid, {})[key] = rid
    if has("spent"):
        for uid, value in src.execute("SELECT user_id,value FROM spent"):
            spent[uid] = value or 0
    names = {uid: n or "" for uid, n in src.execute("SELECT user_id,name FROM users")}
    prefixes = {}
    for key, t in config.TEAMS.items():
        r = src.execute("SELECT value FROM settings WHERE chat_id=? AND key='prefix'", (t["chat_id"],)).fetchone()
        prefixes[key] = "".join((r[0] if r else "").split()).lower()
    out = []
    for uid in set(pts) | set(mem):
        p, m = pts.get(uid, {}), mem.get(uid, {})
        cands = set(m) or set(p)
        if not cands:
            continue
        nm = "".join(names.get(uid, "").split()).lower()
        home = max(cands, key=lambda k: (p.get(k, (0, 0))[0], bool(prefixes.get(k)) and nm.startswith(prefixes[k]),
                                         -m.get(k, 10 ** 12)))
        value = sum(v for v, _ in p.values()) - spent.get(uid, 0)
        absent = 0 if home in m else p.get(home, (0, 0))[1]
        out.append((uid, home, float(value), int(absent)))
    return out


def coerce(v, typ):
    if v is None:
        return None
    if typ in ("bigint", "integer", "smallint"):
        return int(v)
    if typ in ("double precision", "real", "numeric"):
        return float(v)
    return str(v)


async def main(path, dsn=None):
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    await db.init(dsn)
    existing = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in COPY:
        if table not in existing:
            continue
        if table == "bank" and not src.execute("SELECT 1 FROM bank LIMIT 1").fetchone():
            continue
        src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
        types = {r["column_name"]: r["data_type"] for r in await db.q(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name=? AND table_schema=current_schema()", (table,))}
        cols = [c for c in src_cols if c in types]
        rows = src.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY rowid").fetchall()
        sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))}) ON CONFLICT DO NOTHING")
        for r in rows:
            await db.x(sql, [coerce(r[c], types[c]) for c in cols])
        print(f"{table}: {len(rows)}")
    # единый баланс из старых points/spent (если bank в SQLite ещё нет)
    if "points" in existing and not (("bank" in existing) and src.execute("SELECT 1 FROM bank LIMIT 1").fetchone()):
        bank = build_bank(src)
        for uid, home, value, absent in bank:
            await db.x("INSERT INTO bank(user_id,home,value,absent) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                       (uid, home, value, absent))
        print(f"bank (единый баланс): {len(bank)}")
    # счётчики автонумерации — после максимального перенесённого id
    for table, col in SERIAL.items():
        await db.x(f"SELECT setval(pg_get_serial_sequence('{table}','{col}'), COALESCE((SELECT MAX({col}) FROM {table}), 0) + 1, false)")
    await db.close()
    print("Готово. Теперь бот можно запускать: python bot.py")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Использование: python migrate_sqlite.py bot.db")
    asyncio.run(main(sys.argv[1]))
