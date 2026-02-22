# Миграция SQLite → PostgreSQL (Render.com)

## Шаги

### 1. Локально: анализ текущей SQLite БД

```bash
python analyze_db.py
```

- Подключается к `bot.db`
- Выводит в консоль список таблиц, колонки, типы, примеры записей
- Сохраняет схему в **schema.json**

### 2. Локально: перенос данных в PostgreSQL

Убедитесь, что у вас есть строка подключения PostgreSQL (например из Render: Internal Database URL).

```bash
# В .env или export
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
# или для Render часто выдают postgres:// — скрипт сам подставит postgresql://

python migrate.py
```

- Читает **schema.json**
- Создаёт таблицы в PostgreSQL (если их ещё нет)
- Копирует все данные из SQLite, сохраняя `id` и связи
- **SQLite не изменяется и не удаляется**

### 3. Render: переменная окружения

В Render в сервисе бота добавьте переменную окружения:

- **Key:** `DATABASE_URL`
- **Value:** Internal Database URL (из вашего PostgreSQL сервиса на Render)

После деплоя бот будет использовать **database_pg.py** (PostgreSQL). Без `DATABASE_URL` используется прежняя SQLite (**database.py**).

### 4. Зависимости

В **requirements.txt** уже добавлено:

- `sqlalchemy[asyncio]>=2.0`
- `asyncpg>=0.29.0`
- `psycopg2-binary>=2.9.0` (для скрипта миграции)

## Файлы

| Файл | Назначение |
|------|------------|
| **analyze_db.py** | Анализ SQLite, создание schema.json |
| **schema.json** | Описание таблиц и колонок (создаётся analyze_db.py) |
| **migrate.py** | Перенос данных SQLite → PostgreSQL |
| **database_pg.py** | Асинхронный слой БД для PostgreSQL (SQLAlchemy + asyncpg) |
| **database.py** | Текущий слой для SQLite (без изменений логики, добавлен set_referrer_for_user) |

В **main.py** при наличии `DATABASE_URL` импорт идёт из **database_pg**, иначе из **database**.
