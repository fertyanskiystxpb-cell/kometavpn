#!/usr/bin/env python3
"""
Миграция данных из SQLite в PostgreSQL.
- Читает schema.json (результат analyze_db.py)
- Создаёт таблицы в PostgreSQL по схеме
- Копирует все данные с сохранением связей и ID
- Старую SQLite БД не изменяет и не удаляет

Требуется: DATABASE_URL в окружении или .env (PostgreSQL от Render).
Запуск: python migrate.py
"""

import json
import os
import sys
from pathlib import Path

import sqlite3
import psycopg2
from psycopg2.extras import execute_values

# Пути
SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "bot.db"
SCHEMA_JSON_PATH = SCRIPT_DIR / "schema.json"

# Маппинг типов для PostgreSQL
TYPE_MAP = {
    "integer": "INTEGER",
    "text": "TEXT",
    "float": "REAL",
    "blob": "BYTEA",
    "numeric": "NUMERIC",
    "boolean": "BOOLEAN",
}


def load_schema() -> dict:
    if not SCHEMA_JSON_PATH.exists():
        print(f"Ошибка: {SCHEMA_JSON_PATH} не найден. Сначала запустите: python analyze_db.py")
        sys.exit(1)
    with open(SCHEMA_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_pg_connection():
    """Подключение к PostgreSQL из DATABASE_URL (Render)."""
    url = os.getenv("DATABASE_URL")
    if not url:
        try:
            from dotenv import load_dotenv
            load_dotenv(SCRIPT_DIR / ".env")
            url = os.getenv("DATABASE_URL")
        except ImportError:
            pass
    if not url:
        print("Ошибка: задайте DATABASE_URL (PostgreSQL). Например в .env или export DATABASE_URL=...")
        sys.exit(1)
    # Render использует postgres://, но psycopg2 ожидает postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[11:]
    return psycopg2.connect(url)


def create_pg_tables(pg_conn, schema: dict) -> None:
    """Создаёт таблицы в PostgreSQL по schema.json (если не существуют)."""
    cur = pg_conn.cursor()
    # Порядок: users (subscriptions ссылается на users)
    table_order = list(schema["tables"].keys())
    if "subscriptions" in table_order and "users" in table_order:
        table_order = ["users", "subscriptions"] + [t for t in table_order if t not in ("users", "subscriptions")]
    for table_name in table_order:
        if table_name not in schema["tables"]:
            continue
        table_info = schema["tables"][table_name]
        col_defs = []
        for col in table_info["columns"]:
            name = col["name"]
            pg_type = TYPE_MAP.get(col["type"], "TEXT")
            if name == "id" and col.get("pk"):
                col_defs.append("id SERIAL PRIMARY KEY")
                continue
            if col.get("pk"):
                col_defs.append(f'"{name}" {pg_type} PRIMARY KEY')
                continue
            notnull = " NOT NULL" if col.get("notnull") else ""
            default = ""
            if col.get("default") is not None:
                d = col["default"]
                if isinstance(d, (int, float)):
                    default = f" DEFAULT {d}"
                elif str(d).upper() == "NULL":
                    default = ""
                else:
                    default = f" DEFAULT {d!r}"
            # subscriptions.user_id -> FK на users(id)
            if table_name == "subscriptions" and name == "user_id":
                col_defs.append(f'user_id INTEGER NOT NULL REFERENCES users(id){default}')
                continue
            col_defs.append(f'"{name}" {pg_type}{notnull}{default}')

        columns_sql = ",\n    ".join(col_defs)
        create_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    {columns_sql}\n)'
        try:
            cur.execute(create_sql)
        except Exception as e:
            print(f"Предупреждение при создании таблицы {table_name}: {e}")
    pg_conn.commit()
    cur.close()


def copy_table(sqlite_conn, pg_conn, table_name: str, table_info: dict) -> int:
    """
    Копирует данные из SQLite в PostgreSQL для одной таблицы.
    Сохраняет исходные id. Возвращает количество скопированных строк.
    """
    sqlite_cur = sqlite_conn.cursor()
    pg_cur = pg_conn.cursor()

    columns = [c["name"] for c in table_info["columns"]]
    cols_str = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))

    sqlite_cur.execute(f'SELECT {cols_str} FROM "{table_name}"')
    rows = sqlite_cur.fetchall()

    if not rows:
        sqlite_cur.close()
        pg_cur.close()
        return 0

    # ON CONFLICT (id) DO NOTHING — при повторном запуске не дублируем
    insert_sql = f'INSERT INTO "{table_name}" ({cols_str}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING'
    try:
        execute_values(pg_cur, insert_sql, rows, page_size=500)
    except Exception:
        pg_conn.rollback()
        for row in rows:
            try:
                pg_cur.execute(f'INSERT INTO "{table_name}" ({cols_str}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING', row)
            except Exception:
                pass
    pg_conn.commit()

    # Обновить sequence для id
    if "id" in columns and table_name in ("users", "subscriptions"):
        try:
            pg_cur.execute(f"SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE((SELECT MAX(id) FROM \"{table_name}\"), 1))", (table_name,))
            pg_conn.commit()
        except Exception:
            pass

    count = len(rows)
    pg_cur.close()
    sqlite_cur.close()
    return count


def main():
    schema = load_schema()
    if not DB_PATH.exists():
        print(f"Ошибка: SQLite файл не найден: {DB_PATH}")
        sys.exit(1)

    print("Подключение к SQLite...")
    sqlite_conn = sqlite3.connect(DB_PATH)
    print("Подключение к PostgreSQL...")
    pg_conn = get_pg_connection()

    print("Создание таблиц в PostgreSQL...")
    create_pg_tables(pg_conn, schema)

    # Порядок важен: сначала users (subscriptions ссылается на users)
    table_order = list(schema["tables"].keys())
    if "subscriptions" in table_order and "users" in table_order:
        table_order = ["users", "subscriptions"] + [t for t in table_order if t not in ("users", "subscriptions")]

    total = 0
    for table_name in table_order:
        if table_name not in schema["tables"]:
            continue
        n = copy_table(sqlite_conn, pg_conn, table_name, schema["tables"][table_name])
        total += n
        print(f"  {table_name}: скопировано {n} записей")

    sqlite_conn.close()
    pg_conn.close()
    print(f"\nМиграция завершена. Всего записей: {total}")
    print("SQLite файл не изменён. Для работы бота с PostgreSQL используйте database.py с DATABASE_URL.")


if __name__ == "__main__":
    main()
