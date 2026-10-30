"""Перенос данных бота из локальной bot.db на онлайн-базу PostgreSQL (Supabase, Neon, Railway, Render…).

Запуск (бот должен быть остановлен):
    python transfer_db.py                      # спросит файл базы и адрес онлайн-базы
    python transfer_db.py bot.db "postgresql://user:pass@host:5432/db?sslmode=require"

Адрес берётся из аргумента, из DATABASE_URL в .env или вводится вручную.
Скрипт сам создаёт таблицы, переносит данные, собирает единый баланс баллов и в конце сверяет
число строк в локальной и онлайн-базе. Запускать можно повторно — дубли не создаются.
"""
import asyncio
import os
import sqlite3
import sys

import config
import db
import migrate_sqlite as mig


def ask(prompt, default=""):
    v = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return v or default


async def verify(path):
    """Сверка: в онлайн-базе должно быть не меньше строк, чем в локальной (по каждой таблице)."""
    src = sqlite3.connect(path)
    have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ok = True
    print("\nСверка:")
    for t in mig.COPY:
        if t not in have or t == "bank":
            continue
        local = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        online = (await db.one(f"SELECT COUNT(*) AS c FROM {t}"))["c"]
        mark = "✅" if online >= local else "❌"
        ok &= online >= local
        print(f"  {mark} {t}: было {local}, в онлайн-базе {online}")
    bank = (await db.one("SELECT COUNT(*) AS c, COALESCE(SUM(value),0) AS s FROM bank"))
    print(f"  💰 bank (единый баланс): людей {bank['c']}, сумма баллов {bank['s']:g}")
    return ok


async def run(path, dsn):
    if not os.path.exists(path):
        sys.exit(f"Файл {path} не найден.")
    if not dsn:
        sys.exit("Нужен адрес онлайн-базы (DATABASE_URL).")
    print("Подключаюсь к онлайн-базе…")
    try:
        await db.init(dsn)
        await db.close()
    except Exception as e:
        sys.exit(f"Не удалось подключиться: {e}\nПроверьте адрес, пароль и что в строке есть ?sslmode=require.")
    print("Подключение есть, переношу данные:")
    await mig.main(path, dsn)
    await db.init(dsn)
    ok = await verify(path)
    await db.close()
    print("\n" + ("Всё перенесено ✅. Впишите тот же адрес в .env как DATABASE_URL и запускайте бота."
                  if ok else "Есть расхождения ❌ — запустите скрипт ещё раз или пришлите вывод."))


if __name__ == "__main__":
    a = sys.argv[1:]
    path = a[0] if a else ask("Файл локальной базы", "bot.db")
    dsn = a[1] if len(a) > 1 else (config.DATABASE_URL or ask("Адрес онлайн-базы (postgresql://…)"))
    asyncio.run(run(path, dsn))
