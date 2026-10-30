"""Хранилище: онлайн PostgreSQL (asyncpg). Адрес базы — переменная DATABASE_URL в .env."""
import asyncpg

import config

_pool = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id BIGINT PRIMARY KEY, name TEXT, username TEXT);
CREATE INDEX IF NOT EXISTS ix_users_un ON users(username);
CREATE TABLE IF NOT EXISTS members(chat_id BIGINT, user_id BIGINT, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS settings(chat_id BIGINT, key TEXT, value TEXT, PRIMARY KEY(chat_id,key));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS lists(chat_id BIGINT, num INTEGER, template TEXT, state TEXT,
    msg_id BIGINT, mode TEXT DEFAULT 'name', photo TEXT, PRIMARY KEY(chat_id,num));
CREATE TABLE IF NOT EXISTS games(id BIGSERIAL PRIMARY KEY, chat_id BIGINT, dt TEXT, descr TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS poll_tpl(id BIGSERIAL, chat_id BIGINT, name TEXT, question TEXT, options TEXT,
    anon INTEGER DEFAULT 0, multi INTEGER DEFAULT 0, quiz INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
    once INTEGER DEFAULT 0, PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS poll_sent(game_id BIGINT, tpl TEXT, hours DOUBLE PRECISION, PRIMARY KEY(game_id,tpl,hours));
CREATE TABLE IF NOT EXISTS shop(id BIGSERIAL PRIMARY KEY, chat_id BIGINT, kind TEXT, price DOUBLE PRECISION,
    name TEXT, descr TEXT);
CREATE TABLE IF NOT EXISTS custom(rid BIGSERIAL, chat_id BIGINT, name TEXT, text TEXT, mtype TEXT, fid TEXT,
    mode TEXT DEFAULT 'start', PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS warns(id BIGSERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, reason TEXT,
    until DOUBLE PRECISION, created DOUBLE PRECISION, by_id BIGINT);
CREATE TABLE IF NOT EXISTS history(id BIGSERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, action TEXT,
    reason TEXT, until DOUBLE PRECISION, created DOUBLE PRECISION, by_id BIGINT);
CREATE TABLE IF NOT EXISTS caps(chat_id BIGINT, user_id BIGINT, perms TEXT, PRIMARY KEY(chat_id,user_id));
-- единый баланс: каждый закреплён за основной командой (home), баллы одни на всех
CREATE TABLE IF NOT EXISTS bank(user_id BIGINT PRIMARY KEY, home TEXT NOT NULL, value DOUBLE PRECISION NOT NULL DEFAULT 0,
    absent INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_bank_home ON bank(home);
-- набранные списки: хранятся до конца игры, чтобы работали проверки (кик, запрет записи в две команды)
CREATE TABLE IF NOT EXISTS done_lists(chat_id BIGINT, num INTEGER, ids TEXT, until DOUBLE PRECISION, PRIMARY KEY(chat_id,num));
-- заявки, пришедшие во время закрытого входа (20:00-21:00), бот примет их после 21:00
CREATE TABLE IF NOT EXISTS join_queue(chat_id BIGINT, user_id BIGINT, ts DOUBLE PRECISION, PRIMARY KEY(chat_id,user_id));
"""


def _pg(sql):
    """Плейсхолдеры ? -> $1, $2, ... (в запросах нет строковых литералов с «?»)."""
    out, n = [], 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


async def init(dsn=None):
    global _pool
    dsn = dsn or config.DATABASE_URL
    if not dsn:
        raise RuntimeError("Не задан DATABASE_URL (адрес онлайн-базы PostgreSQL) в .env")
    # statement_cache_size=0 — чтобы работало и через pgbouncer (Supabase, Neon и т.п.)
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, statement_cache_size=0)
    async with _pool.acquire() as c:
        await c.execute(SCHEMA)


async def close():
    if _pool:
        await _pool.close()


async def q(sql, args=()):
    async with _pool.acquire() as c:
        return await c.fetch(_pg(sql), *args)


async def one(sql, args=()):
    async with _pool.acquire() as c:
        return await c.fetchrow(_pg(sql), *args)


async def x(sql, args=()):
    """Выполняет запрос; возвращает число затронутых строк."""
    async with _pool.acquire() as c:
        status = await c.execute(_pg(sql), *args)
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def get_setting(chat_id, key, default=""):
    r = await one("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
    return r["value"] if r and r["value"] is not None else default


async def set_setting(chat_id, key, value):
    await x(
        "INSERT INTO settings(chat_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value",
        (chat_id, key, str(value)),
    )


async def del_setting(chat_id, key):
    await x("DELETE FROM settings WHERE chat_id=? AND key=?", (chat_id, key))


async def get_meta(key, default=""):
    r = await one("SELECT value FROM meta WHERE key=?", (key,))
    return r["value"] if r else default


async def set_meta(key, value):
    await x(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


_seen = {}
_mem = set()


async def touch_user(uid, name, username):
    """Пишет в базу только если имя/ник изменились (не чаще, чем нужно)."""
    key = (name, username)
    if _seen.get(uid) == key:
        return
    _seen[uid] = key
    await x(
        "INSERT INTO users(user_id,name,username) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username",
        (uid, name, username.lower() if username else None),
    )


async def add_member(chat_id, uid):
    """True, если человека раньше не знали в этом чате."""
    if (chat_id, uid) in _mem:
        return False
    _mem.add((chat_id, uid))
    new = await x("INSERT INTO members(chat_id,user_id) VALUES(?,?) ON CONFLICT DO NOTHING", (chat_id, uid))
    key = config.team_key(chat_id)
    if key:  # первая команда, где человека увидели, становится его основной
        await x("INSERT INTO bank(user_id,home,value,absent) VALUES(?,?,0,0) ON CONFLICT DO NOTHING", (uid, key))
    return new > 0


async def remove_member(chat_id, uid):
    _mem.discard((chat_id, uid))
    await x("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid))
