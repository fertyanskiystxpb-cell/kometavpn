#!/usr/bin/env python3
"""
Скрипт анализа существующей SQLite базы данных.
- Подключается к bot.db
- Выводит список таблиц, колонки, типы, примеры записей
- Сохраняет схему в schema.json для последующей миграции
Запуск: python analyze_db.py
"""

import json
import sqlite3
import sys
from pathlib import Path

# Путь к SQLite файлу (рядом со скриптом)
DB_PATH = Path(__file__).resolve().parent / "bot.db"
SCHEMA_JSON_PATH = Path(__file__).resolve().parent / "schema.json"

# Соответствие типов SQLite -> описание для миграции
SQLITE_TYPE_MAP = {
    "INTEGER": "integer",
    "TEXT": "text",
    "REAL": "float",
    "BLOB": "blob",
    "NUMERIC": "numeric",
    "BOOLEAN": "boolean",
}


def get_sqlite_type(decl_type: str) -> str:
    """Нормализует объявленный тип SQLite."""
    if not decl_type:
        return "text"
    decl = decl_type.upper().split("(")[0].strip()
    return SQLITE_TYPE_MAP.get(decl, "text")


def analyze_sqlite(db_path: Path) -> dict:
    """
    Анализирует SQLite БД и возвращает структуру для schema.json.
    """
    if not db_path.exists():
        print(f"Ошибка: файл {db_path} не найден.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Список таблиц
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]

    schema = {
        "source": str(db_path),
        "tables": {},
    }

    for table in tables:
        # Информация о колонках (PRAGMA table_info)
        cur.execute(f"PRAGMA table_info({table})")
        columns_info = cur.fetchall()
        columns = []
        for col in columns_info:
            cid, name, decl_type, notnull, default, pk = col
            columns.append({
                "name": name,
                "type": get_sqlite_type(decl_type),
                "sqlite_type": decl_type or "TEXT",
                "notnull": bool(notnull),
                "default": default,
                "pk": bool(pk),
            })

        # Примеры записей (первые 3)
        cur.execute(f"SELECT * FROM {table} LIMIT 3")
        rows = cur.fetchall()
        sample_rows = [dict(row) for row in rows]

        schema["tables"][table] = {
            "columns": columns,
            "sample_rows": sample_rows,
            "sample_count": len(sample_rows),
        }

        # Вывод в консоль
        print(f"\n{'='*60}")
        print(f"Таблица: {table}")
        print("Колонки:")
        for c in columns:
            pk_mark = " (PK)" if c["pk"] else ""
            print(f"  - {c['name']}: {c['sqlite_type']}{pk_mark}")
        print("Примеры записей (до 3):")
        for i, row in enumerate(sample_rows, 1):
            print(f"  [{i}] {row}")

    conn.close()
    return schema


def main():
    print(f"Анализ SQLite: {DB_PATH}")
    schema = analyze_sqlite(DB_PATH)

    with open(SCHEMA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)

    print(f"\nСхема сохранена в: {SCHEMA_JSON_PATH}")
    print("Таблицы:", list(schema["tables"].keys()))


if __name__ == "__main__":
    main()
