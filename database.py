import asyncio
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = Path(__file__).with_name("bot.db")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


async def init_db() -> None:
    def _init() -> None:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    expires_at TEXT,
                    payment_reference TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
            conn.commit()

            # Добавляем колонки для реферальной системы и пробного периода, если их ещё нет
            cur.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cur.fetchall()]
            if "trial_used" not in columns:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0"
                )
                conn.commit()
            if "referrer_id" not in columns:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN referrer_id INTEGER"
                )
                conn.commit()
            if "referral_count" not in columns:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN referral_count INTEGER NOT NULL DEFAULT 0"
                )
                conn.commit()
            if "v2ray_uuid" not in columns:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN v2ray_uuid TEXT"
                )
                conn.commit()
            if "is_api_user" not in columns:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN is_api_user INTEGER NOT NULL DEFAULT 0"
                )
                conn.commit()
        finally:
            conn.close()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _init)


async def get_or_create_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    referrer_id: Optional[int] = None,
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()

    def _op() -> Dict[str, Any]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            # Сначала пытаемся получить существующего пользователя
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cur.fetchone()
            
            if row:
                user_dict = dict(row)
                # Если пользователь существует, обновляем username и first_name если они изменились
                if username is not None or first_name is not None:
                    updates = []
                    params = []
                    if username is not None:
                        updates.append("username = ?")
                        params.append(username)
                    if first_name is not None:
                        updates.append("first_name = ?")
                        params.append(first_name)
                    if updates:
                        params.append(telegram_id)
                        cur.execute(
                            f"UPDATE users SET {', '.join(updates)} WHERE telegram_id = ?",
                            params,
                        )
                        conn.commit()
                        # Получаем обновлённую запись
                        cur.execute(
                            "SELECT * FROM users WHERE telegram_id = ?",
                            (telegram_id,),
                        )
                        user_dict = dict(cur.fetchone())
                return user_dict

            # Пользователь не существует - создаём нового
            now = dt.datetime.utcnow().isoformat()
            try:
                cur.execute(
                    """
                    INSERT INTO users (telegram_id, username, first_name, created_at, referrer_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (telegram_id, username, first_name, now, referrer_id),
                )
                conn.commit()
                user_id = cur.lastrowid
            except sqlite3.IntegrityError:
                # Если произошла ошибка уникальности (race condition), получаем существующего пользователя
                conn.rollback()
                cur.execute(
                    "SELECT * FROM users WHERE telegram_id = ?",
                    (telegram_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
                raise  # Если пользователь всё равно не найден, пробрасываем ошибку
            
            if referrer_id:
                # Увеличиваем счётчик рефералов у реферера
                cur.execute(
                    "UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id = ?",
                    (referrer_id,),
                )
                conn.commit()
            
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return dict(cur.fetchone())
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    loop = asyncio.get_running_loop()

    def _op() -> Optional[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def get_latest_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    loop = asyncio.get_running_loop()

    def _op() -> Optional[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT * FROM subscriptions
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def get_user_with_subscription(
    telegram_id: int,
) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        return None
    sub = await get_latest_subscription(user["id"])
    return user, sub


async def set_referrer_for_user(user_telegram_id: int, referrer_telegram_id: int) -> bool:
    """
    Устанавливает referrer_id пользователю и увеличивает referral_count у реферера.
    Возвращает True, если обновление выполнено (у пользователя ещё не было реферера).
    """
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET referrer_id = ? WHERE telegram_id = ? AND referrer_id IS NULL",
                (referrer_telegram_id, user_telegram_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return False
            cur.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id = ?",
                (referrer_telegram_id,),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def create_pending_subscription(
    user_id: int,
    payment_reference: str,
) -> None:
    loop = asyncio.get_running_loop()

    def _op() -> None:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO subscriptions (user_id, status, started_at, expires_at, payment_reference)
                VALUES (?, 'pending', NULL, NULL, ?)
                """,
                (user_id, payment_reference),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _op)


async def list_pending_subscriptions() -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()

    def _op() -> List[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM subscriptions WHERE status = 'pending' ORDER BY id ASC"
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def create_active_subscription_for_telegram(
    telegram_id: int,
    duration_days: int,
) -> bool:
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                return False
            user_id = user_row["id"]

            started_at = dt.datetime.utcnow()
            expires_at = started_at + dt.timedelta(days=duration_days)
            cur.execute(
                """
                INSERT INTO subscriptions (user_id, status, started_at, expires_at, payment_reference)
                VALUES (?, 'active', ?, ?, NULL)
                """,
                (
                    user_id,
                    started_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def list_used_keys() -> List[str]:
    loop = asyncio.get_running_loop()

    def _op() -> List[str]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT DISTINCT payment_reference
                FROM subscriptions
                WHERE payment_reference IS NOT NULL
                """
            )
            rows = cur.fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def create_active_subscription_with_key(
    telegram_id: int,
    duration_days: int,
    key: str,
) -> bool:
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                return False
            user_id = user_row["id"]

            started_at = dt.datetime.utcnow()
            expires_at = started_at + dt.timedelta(days=duration_days) if duration_days > 0 else None
            cur.execute(
                """
                INSERT INTO subscriptions (user_id, status, started_at, expires_at, payment_reference)
                VALUES (?, 'active', ?, ?, ?)
                """,
                (
                    user_id,
                    started_at.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    key,
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def has_active_subscription(telegram_id: int) -> bool:
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                return False
            user_id = user_row["id"]

            now = dt.datetime.utcnow().isoformat()
            cur.execute(
                """
                SELECT 1 FROM subscriptions
                WHERE user_id = ?
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, now),
            )
            row = cur.fetchone()
            return bool(row)
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def revoke_active_subscription_for_telegram(telegram_id: int) -> bool:
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                return False
            user_id = user_row["id"]

            now = dt.datetime.utcnow().isoformat()
            cur.execute(
                """
                UPDATE subscriptions
                SET status = 'revoked',
                    expires_at = ?
                WHERE user_id = ? AND status = 'active'
                """,
                (now, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def extend_active_subscription_by_days(
    telegram_id: int, days: int
) -> Optional[Dict[str, Any]]:
    """
    Продлевает активную подписку пользователя на указанное количество дней.
    Возвращает обновлённую подписку (с полями payment_reference, expires_at и т.д.) или None.
    """
    loop = asyncio.get_running_loop()

    def _op() -> Optional[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                return None
            user_id = user_row["id"]

            now = dt.datetime.utcnow()
            cur.execute(
                """
                SELECT id, expires_at, payment_reference
                FROM subscriptions
                WHERE user_id = ?
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, now.isoformat()),
            )
            sub_row = cur.fetchone()
            if not sub_row:
                return None

            sub_id = sub_row["id"]
            expires_at = sub_row["expires_at"]
            if expires_at:
                try:
                    normalized = str(expires_at).replace("Z", "+00:00")
                    current_end = dt.datetime.fromisoformat(normalized)
                    if current_end.tzinfo is not None:
                        current_end = current_end.astimezone(dt.timezone.utc).replace(tzinfo=None)
                    if current_end > now:
                        # Subscription is still active, extend from current expiry date
                        base_date = current_end
                    else:
                        # Subscription is expired, extend from current time
                        base_date = now
                    new_expires = (base_date + dt.timedelta(days=days)).isoformat()
                except (ValueError, TypeError):
                    new_expires = (now + dt.timedelta(days=days)).isoformat()
            else:
                new_expires = (now + dt.timedelta(days=days)).isoformat()

            cur.execute(
                "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
                (new_expires, sub_id),
            )
            conn.commit()

            cur.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def set_user_api_client(telegram_id: int, v2ray_uuid: str) -> bool:
    """Помечает пользователя как API-клиента и сохраняет его UUID."""
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET v2ray_uuid = ?, is_api_user = 1 WHERE telegram_id = ?",
                (v2ray_uuid, telegram_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def list_expired_api_clients() -> List[Tuple[int, str]]:
    """
    Возвращает список (telegram_id, v2ray_uuid) для API-пользователей,
    у которых нет активной подписки (все истекли/отозваны).
    """
    loop = asyncio.get_running_loop()

    def _op() -> List[Tuple[int, str]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            now = dt.datetime.utcnow().isoformat()
            cur.execute(
                """
                SELECT u.telegram_id, u.v2ray_uuid
                FROM users u
                WHERE u.is_api_user = 1
                  AND u.v2ray_uuid IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM subscriptions s
                    WHERE s.user_id = u.id
                      AND s.status = 'active'
                      AND (s.expires_at IS NULL OR s.expires_at > ?)
                  )
                """,
                (now,),
            )
            rows = cur.fetchall()
            return [(row[0], row[1]) for row in rows if row[1]]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def clear_user_api_client(telegram_id: int) -> bool:
    """Сбрасывает флаг API-клиента и UUID после удаления в панели."""
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET v2ray_uuid = NULL, is_api_user = 0 WHERE telegram_id = ?",
                (telegram_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def expire_outdated_subscriptions() -> int:
    loop = asyncio.get_running_loop()
    
    def _op() -> int:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            now = dt.datetime.utcnow().isoformat()
            cur.execute(
                """
                UPDATE subscriptions
                SET status = 'expired'
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                """,
                (now,)
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    
    return await loop.run_in_executor(None, _op)


async def reset_all_keys() -> bool:
    loop = asyncio.get_running_loop()
    
    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE subscriptions SET payment_reference = NULL WHERE payment_reference IS NOT NULL"
            )
            conn.commit()
            return True
        except Exception as e:
            print(f"Ошибка при сбросе ключей: {e}")
            return False
        finally:
            conn.close()
    
    return await loop.run_in_executor(None, _op)


async def reset_keys_by_keys(key_strings: List[str]) -> int:
    """
    Сбрасывает ключи у подписок, у которых payment_reference входит в список key_strings.
    Возвращает количество обновлённых записей.
    """
    if not key_strings:
        return 0
    loop = asyncio.get_running_loop()

    def _op() -> int:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            placeholders = ",".join("?" * len(key_strings))
            cur.execute(
                f"UPDATE subscriptions SET payment_reference = NULL WHERE payment_reference IN ({placeholders})",
                key_strings,
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def reset_keys_from_set(key_strings: List[str], limit: Optional[int] = None) -> int:
    """
    Сбрасывает ключи у подписок, у которых payment_reference входит в key_strings.
    Если limit задан — сбрасываются только у последних (по id подписки) limit подписок.
    Возвращает количество обновлённых записей.
    """
    if not key_strings:
        return 0
    loop = asyncio.get_running_loop()

    def _op() -> int:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            placeholders = ",".join("?" * len(key_strings))
            if limit is not None and limit > 0:
                cur.execute(
                    f"""
                    SELECT id FROM subscriptions
                    WHERE payment_reference IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (*key_strings, limit),
                )
                ids = [row[0] for row in cur.fetchall()]
                if not ids:
                    return 0
                id_placeholders = ",".join("?" * len(ids))
                cur.execute(
                    f"UPDATE subscriptions SET payment_reference = NULL WHERE id IN ({id_placeholders})",
                    ids,
                )
            else:
                cur.execute(
                    f"UPDATE subscriptions SET payment_reference = NULL WHERE payment_reference IN ({placeholders})",
                    key_strings,
                )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def get_total_issued_keys_count() -> int:
    loop = asyncio.get_running_loop()
    
    def _op() -> int:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(DISTINCT payment_reference)
                FROM subscriptions
                WHERE payment_reference IS NOT NULL
                """
            )
            row = cur.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    
    return await loop.run_in_executor(None, _op)


async def list_users_with_latest_subscription() -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()

    def _op() -> List[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    u.id AS user_id,
                    u.telegram_id,
                    u.username,
                    u.first_name,
                    u.created_at,
                    s.status AS subscription_status,
                    s.expires_at AS subscription_expires_at,
                    s.payment_reference AS subscription_key
                FROM users u
                LEFT JOIN subscriptions s
                    ON s.id = (
                        SELECT id
                        FROM subscriptions
                        WHERE user_id = u.id
                        ORDER BY id DESC
                        LIMIT 1
                    )
                ORDER BY u.id ASC
                """
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def has_used_trial(telegram_id: int) -> bool:
    """
    Проверяет, использовал ли пользователь пробный период (7 дней).
    """
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT trial_used FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            value = row[0]
            return bool(value)
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def mark_trial_used(telegram_id: int) -> None:
    """
    Помечает, что пользователь уже использовал пробный период.
    """
    loop = asyncio.get_running_loop()

    def _op() -> None:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET trial_used = 1 WHERE telegram_id = ?",
                (telegram_id,),
            )
            conn.commit()
        finally:
            conn.close()

    await loop.run_in_executor(None, _op)


async def get_all_telegram_ids() -> List[int]:
    """Возвращает список всех telegram_id пользователей (для рассылки)."""
    loop = asyncio.get_running_loop()

    def _op() -> List[int]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users")
            return [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def update_user_subscription_key(telegram_id: int, new_key: str) -> bool:
    """
    Обновляет ключ (payment_reference) у последней подписки пользователя.
    Возвращает True при успехе, False если пользователь или подписка не найдены.
    """
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return False
            user_id = row[0]
            cur.execute(
                """
                SELECT id FROM subscriptions
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            )
            sub_row = cur.fetchone()
            if not sub_row:
                return False
            cur.execute(
                "UPDATE subscriptions SET payment_reference = ? WHERE id = ?",
                (new_key, sub_row[0]),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def update_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> bool:
    """
    Обновляет username и/или first_name пользователя.
    Передавать только те поля, которые нужно изменить.
    """
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            if username is not None and first_name is not None:
                cur.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                    (username, first_name, telegram_id),
                )
            elif username is not None:
                cur.execute(
                    "UPDATE users SET username = ? WHERE telegram_id = ?",
                    (username, telegram_id),
                )
            elif first_name is not None:
                cur.execute(
                    "UPDATE users SET first_name = ? WHERE telegram_id = ?",
                    (first_name, telegram_id),
                )
            else:
                return False
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def get_referral_stats() -> List[Dict[str, Any]]:
    """Возвращает статистику рефералов: кто сколько пригласил."""
    loop = asyncio.get_running_loop()

    def _op() -> List[Dict[str, Any]]:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT telegram_id, username, first_name, referral_count
                FROM users
                WHERE referral_count > 0
                ORDER BY referral_count DESC
                """
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def reset_trial_for_user(telegram_id: int) -> bool:
    """Сбрасывает флаг использования пробного периода для пользователя."""
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET trial_used = 0 WHERE telegram_id = ?",
                (telegram_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)


async def delete_user_completely(telegram_id: int) -> bool:
    """
    Полностью удаляет пользователя из базы данных:
    - Удаляет все его подписки
    - Уменьшает referral_count у реферера (если был)
    - Удаляет пользователя из таблицы users
    
    Возвращает True если пользователь был найден и удален, False если не найден.
    """
    loop = asyncio.get_running_loop()

    def _op() -> bool:
        conn = _get_connection()
        try:
            cur = conn.cursor()
            # Получаем информацию о пользователе
            cur.execute(
                "SELECT id, referrer_id FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                return False
            
            user_id = user_row["id"]
            referrer_id = user_row["referrer_id"]
            
            # Удаляем все подписки пользователя
            cur.execute(
                "DELETE FROM subscriptions WHERE user_id = ?",
                (user_id,),
            )
            
            # Если у пользователя был реферер, уменьшаем его referral_count
            if referrer_id is not None:
                cur.execute(
                    "UPDATE users SET referral_count = referral_count - 1 WHERE telegram_id = ? AND referral_count > 0",
                    (referrer_id,),
                )
            
            # Удаляем пользователя
            cur.execute(
                "DELETE FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            
            conn.commit()
            return True
        finally:
            conn.close()

    return await loop.run_in_executor(None, _op)
