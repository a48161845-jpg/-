import aiosqlite

_db = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, username TEXT);
CREATE INDEX IF NOT EXISTS ix_users_un ON users(username);
CREATE TABLE IF NOT EXISTS members(chat_id INTEGER, user_id INTEGER, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS points(user_id INTEGER, team TEXT, value REAL NOT NULL DEFAULT 0,
    absent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,team));
CREATE INDEX IF NOT EXISTS ix_points_team ON points(team);
CREATE TABLE IF NOT EXISTS spent(user_id INTEGER PRIMARY KEY, value REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id,key));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS lists(chat_id INTEGER, num INTEGER, template TEXT, state TEXT,
    msg_id INTEGER, mode TEXT DEFAULT 'name', PRIMARY KEY(chat_id,num));
CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, dt TEXT,
    descr TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS poll_tpl(chat_id INTEGER, name TEXT, question TEXT, options TEXT,
    anon INTEGER DEFAULT 0, multi INTEGER DEFAULT 0, quiz INTEGER DEFAULT 0, correct INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS poll_sent(game_id INTEGER, tpl TEXT, hours REAL, PRIMARY KEY(game_id,tpl,hours));
CREATE TABLE IF NOT EXISTS shop(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, kind TEXT,
    price REAL, name TEXT, descr TEXT);
CREATE TABLE IF NOT EXISTS custom(chat_id INTEGER, name TEXT, text TEXT, PRIMARY KEY(chat_id,name));
CREATE TABLE IF NOT EXISTS warns(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
    reason TEXT, until REAL, created REAL, by_id INTEGER);
CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER,
    action TEXT, reason TEXT, until REAL, created REAL, by_id INTEGER);
CREATE TABLE IF NOT EXISTS caps(chat_id INTEGER, user_id INTEGER, perms TEXT, PRIMARY KEY(chat_id,user_id));
CREATE TABLE IF NOT EXISTS twins(chat_id INTEGER, user_id INTEGER, PRIMARY KEY(chat_id,user_id));
"""


async def init(path="bot.db"):
    global _db
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    for stmt in ("ALTER TABLE lists ADD COLUMN photo TEXT",
                 "ALTER TABLE poll_tpl ADD COLUMN once INTEGER DEFAULT 0",
                 "ALTER TABLE custom ADD COLUMN mtype TEXT",
                 "ALTER TABLE custom ADD COLUMN fid TEXT"):  # миграция старых баз
        try:
            await _db.execute(stmt)
        except Exception:
            pass
    await _db.commit()


async def q(sql, args=()):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def one(sql, args=()):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def x(sql, args=()):
    cur = await _db.execute(sql, args)
    await _db.commit()
    return cur.lastrowid


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
    cur = await _db.execute("INSERT OR IGNORE INTO members(chat_id,user_id) VALUES(?,?)", (chat_id, uid))
    await _db.commit()
    return cur.rowcount > 0


async def remove_member(chat_id, uid):
    _mem.discard((chat_id, uid))
    await x("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid))
