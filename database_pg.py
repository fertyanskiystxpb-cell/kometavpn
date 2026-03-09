"""
Асинхронный слой БД для бота: PostgreSQL через SQLAlchemy 2.0 + asyncpg.
Используется при наличии DATABASE_URL (например на Render.com).
Модели и API совместимы с прежней SQLite-реализацией.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, BigInteger, String, Text,
    select, update, delete, func,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship
from urllib.parse import urlparse

# DATABASE_URL от Render: postgres://... или postgresql://...
# asyncpg ожидает postgresql+asyncpg://
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL:
    orig = DATABASE_URL
    if DATABASE_URL.startswith("postgres://"):
        # Старый короткий префикс, используемый Heroku/Render
        DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[11:]
    elif DATABASE_URL.startswith("postgresql://"):
        # Полный sync‑драйвер, меняем на asyncpg
        DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL[len("postgresql://") :]
    elif not DATABASE_URL.startswith("postgresql+asyncpg://"):
        # На всякий случай — если протокол какой‑то другой, но совместимый
        DATABASE_URL = "postgresql+asyncpg://" + DATABASE_URL.split("://", 1)[-1]

    # Логируем безопасную версию URL, чтобы видеть, что реально используется
    try:
        parsed = urlparse(DATABASE_URL)
        safe_url = f"{parsed.scheme}://{parsed.username or ''}:***@{parsed.hostname or ''}"
        if parsed.port:
            safe_url += f":{parsed.port}"
        if parsed.path:
            safe_url += parsed.path
        print(f"[DB] DATABASE_URL orig={orig!r} used={safe_url}")
    except Exception as e:
        print(f"[DB] failed to parse DATABASE_URL={DATABASE_URL!r}: {e}")

# Для обратной совместиости (main.py может импортировать DB_PATH — не используется при PG)
DB_PATH = Path(__file__).resolve().parent / "bot.db"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255))
    first_name = Column(String(255))
    created_at = Column(DateTime(timezone=False), nullable=False, default=datetime.utcnow)
    trial_used = Column(Integer, nullable=False, default=0)
    referrer_id = Column(Integer)
    referral_count = Column(Integer, nullable=False, default=0)
    v2ray_uuid = Column(Text)
    is_api_user = Column(Integer, nullable=False, default=0)

    subscriptions = relationship("Subscription", back_populates="user", order_by="Subscription.id.desc()")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(32), nullable=False)
    started_at = Column(DateTime(timezone=False))
    expires_at = Column(DateTime(timezone=False))
    payment_reference = Column(Text)

    user = relationship("User", back_populates="subscriptions")


# Движок и сессии (инициализируются в init_db)
_engine = None
_async_session = None


def _get_engine():
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан. Задайте переменную окружения для PostgreSQL.")
        _engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def _get_session_maker():
    global _async_session
    if _async_session is None:
        _async_session = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session


async def _get_session() -> AsyncSession:
    return _get_session_maker()()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Преобразует строку ORM или Row в словарь с ключами-колонками."""
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    if hasattr(row, "__table__"):
        return {c.key: getattr(row, c.key) for c in row.__table__.columns}
    return dict(row)


def _serialize_value(v: Any) -> Any:
    """Даты в ISO-строку для совместимости с прежним API."""
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row_to_dict_serialized(row: Any) -> Dict[str, Any]:
    d = _row_to_dict(row)
    return {k: _serialize_value(v) for k, v in d.items()}


async def init_db() -> None:
    """Создаёт таблицы в PostgreSQL при отсутствии."""
    async with _get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    referrer_id: Optional[int] = None,
) -> Dict[str, Any]:
    async with _get_session_maker()() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user:
            if username is not None or first_name is not None:
                if username is not None:
                    user.username = username
                if first_name is not None:
                    user.first_name = first_name
                await session.commit()
                await session.refresh(user)
            return _row_to_dict_serialized(user)

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            created_at=datetime.utcnow(),
            referrer_id=referrer_id,
        )
        session.add(user)
        try:
            await session.flush()
            if referrer_id:
                await session.execute(
                    update(User).where(User.telegram_id == referrer_id).values(referral_count=User.referral_count + 1)
                )
            await session.commit()
            await session.refresh(user)
            return _row_to_dict_serialized(user)
        except IntegrityError:
            await session.rollback()
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            existing = result.scalar_one_or_none()
            if existing:
                return _row_to_dict_serialized(existing)
            raise


async def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    async with _get_session_maker()() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        return _row_to_dict_serialized(user) if user else None


async def get_latest_subscription(user_id: int) -> Optional[Dict[str, Any]]:
    async with _get_session_maker()() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id.desc()).limit(1)
        )
        sub = result.scalar_one_or_none()
        return _row_to_dict_serialized(sub) if sub else None


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
    Возвращает True, если обновление выполнено.
    """
    async with _get_session_maker()() as session:
        result = await session.execute(
            update(User).where(User.telegram_id == user_telegram_id, User.referrer_id.is_(None)).values(
                referrer_id=referrer_telegram_id
            )
        )
        if result.rowcount == 0:
            return False
        await session.execute(
            update(User).where(User.telegram_id == referrer_telegram_id).values(
                referral_count=User.referral_count + 1
            )
        )
        await session.commit()
        return True


async def create_pending_subscription(user_id: int, payment_reference: str) -> None:
    async with _get_session_maker()() as session:
        session.add(
            Subscription(user_id=user_id, status="pending", payment_reference=payment_reference)
        )
        await session.commit()


async def list_pending_subscriptions() -> List[Dict[str, Any]]:
    async with _get_session_maker()() as session:
        result = await session.execute(select(Subscription).where(Subscription.status == "pending").order_by(Subscription.id))
        rows = result.scalars().all()
        return [_row_to_dict_serialized(r) for r in rows]


async def create_active_subscription_for_telegram(telegram_id: int, duration_days: int) -> bool:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return False
        started = datetime.utcnow()
        expires = started + timedelta(days=duration_days)
        session.add(
            Subscription(
                user_id=user.id,
                status="active",
                started_at=started,
                expires_at=expires,
            )
        )
        await session.commit()
        return True


async def list_used_keys() -> List[str]:
    async with _get_session_maker()() as session:
        result = await session.execute(
            select(Subscription.payment_reference).where(Subscription.payment_reference.isnot(None)).distinct()
        )
        return [row[0] for row in result.fetchall() if row[0]]


async def create_active_subscription_with_key(
    telegram_id: int, duration_days: int, key: str
) -> bool:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return False
        started = datetime.utcnow()
        expires = (started + timedelta(days=duration_days)) if duration_days > 0 else None
        session.add(
            Subscription(
                user_id=user.id,
                status="active",
                started_at=started,
                expires_at=expires,
                payment_reference=key,
            )
        )
        await session.commit()
        return True


async def has_active_subscription(telegram_id: int) -> bool:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return False
        now = datetime.utcnow()
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
                (Subscription.expires_at.is_(None)) | (Subscription.expires_at > now),
            ).order_by(Subscription.id.desc()).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def revoke_active_subscription_for_telegram(telegram_id: int) -> bool:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return False
        now = datetime.utcnow()
        result = await session.execute(
            update(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
            ).values(status="revoked", expires_at=now)
        )
        await session.commit()
        return result.rowcount > 0


async def extend_active_subscription_by_days(telegram_id: int, days: int) -> Optional[Dict[str, Any]]:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return None
        now = datetime.utcnow()
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id == user.id,
                Subscription.status == "active",
                (Subscription.expires_at.is_(None)) | (Subscription.expires_at > now),
            ).order_by(Subscription.id.desc()).limit(1)
        )
        sub = result.scalar_one_or_none()
        if not sub:
            return None
        if sub.expires_at:
            sub.expires_at = sub.expires_at + timedelta(days=days)
        else:
            sub.expires_at = now + timedelta(days=days)
        await session.commit()
        await session.refresh(sub)
        return _row_to_dict_serialized(sub)


async def set_user_api_client(telegram_id: int, v2ray_uuid: str) -> bool:
    async with _get_session_maker()() as session:
        result = await session.execute(
            update(User).where(User.telegram_id == telegram_id).values(v2ray_uuid=v2ray_uuid, is_api_user=1)
        )
        await session.commit()
        return result.rowcount > 0


async def list_expired_api_clients() -> List[Tuple[int, str]]:
    now = datetime.utcnow()
    async with _get_session_maker()() as session:
        # Пользователи с is_api_user=1, v2ray_uuid не пустой и без активной подписки
        subq = (
            select(Subscription.user_id).where(
                Subscription.status == "active",
                (Subscription.expires_at.is_(None)) | (Subscription.expires_at > now),
            ).distinct()
        )
        result = await session.execute(
            select(User.telegram_id, User.v2ray_uuid).where(
                User.is_api_user == 1,
                User.v2ray_uuid.isnot(None),
                ~User.id.in_(subq),
            )
        )
        return [(row[0], row[1]) for row in result.fetchall() if row[1]]


async def clear_user_api_client(telegram_id: int) -> bool:
    async with _get_session_maker()() as session:
        result = await session.execute(
            update(User).where(User.telegram_id == telegram_id).values(v2ray_uuid=None, is_api_user=0)
        )
        await session.commit()
        return result.rowcount > 0


async def expire_outdated_subscriptions() -> int:
    now = datetime.utcnow()
    async with _get_session_maker()() as session:
        result = await session.execute(
            update(Subscription).where(
                Subscription.status == "active",
                Subscription.expires_at.isnot(None),
                Subscription.expires_at <= now,
            ).values(status="expired")
        )
        await session.commit()
        return result.rowcount


async def reset_all_keys() -> bool:
    async with _get_session_maker()() as session:
        await session.execute(update(Subscription).where(Subscription.payment_reference.isnot(None)).values(payment_reference=None))
        await session.commit()
        return True


async def reset_keys_by_keys(key_strings: List[str]) -> int:
    if not key_strings:
        return 0
    async with _get_session_maker()() as session:
        result = await session.execute(
            update(Subscription).where(Subscription.payment_reference.in_(key_strings)).values(payment_reference=None)
        )
        await session.commit()
        return result.rowcount


async def reset_keys_from_set(key_strings: List[str], limit: Optional[int] = None) -> int:
    if not key_strings:
        return 0
    async with _get_session_maker()() as session:
        if limit:
            subq = (
                select(Subscription.id).where(Subscription.payment_reference.in_(key_strings)).order_by(Subscription.id.desc()).limit(limit)
            )
            result = await session.execute(update(Subscription).where(Subscription.id.in_(subq)).values(payment_reference=None))
        else:
            result = await session.execute(
                update(Subscription).where(Subscription.payment_reference.in_(key_strings)).values(payment_reference=None)
            )
        await session.commit()
        return result.rowcount


async def get_total_issued_keys_count() -> int:
    async with _get_session_maker()() as session:
        result = await session.execute(
            select(func.count(func.distinct(Subscription.payment_reference))).where(
                Subscription.payment_reference.isnot(None)
            )
        )
        return result.scalar() or 0


async def list_users_with_latest_subscription() -> List[Dict[str, Any]]:
    async with _get_session_maker()() as session:
        # Подзапрос: id последней подписки для каждого user_id
        subq = (
            select(Subscription.id)
            .where(Subscription.user_id == User.id)
            .order_by(Subscription.id.desc())
            .limit(1)
            .correlate(User)
            .scalar_subquery()
        )
        stmt = (
            select(
                User.id.label("user_id"),
                User.telegram_id,
                User.username,
                User.first_name,
                User.created_at,
                Subscription.status.label("subscription_status"),
                Subscription.expires_at.label("subscription_expires_at"),
                Subscription.payment_reference.label("subscription_key"),
            )
            .select_from(User)
            .outerjoin(Subscription, Subscription.id == subq)
            .order_by(User.id)
        )
        result = await session.execute(stmt)
        rows = result.fetchall()
        return [_row_to_dict_serialized(row) for row in rows]


async def has_used_trial(telegram_id: int) -> bool:
    async with _get_session_maker()() as session:
        result = await session.execute(select(User.trial_used).where(User.telegram_id == telegram_id))
        row = result.one_or_none()
        return bool(row[0]) if row else False


async def mark_trial_used(telegram_id: int) -> None:
    async with _get_session_maker()() as session:
        await session.execute(update(User).where(User.telegram_id == telegram_id).values(trial_used=1))
        await session.commit()


async def get_all_telegram_ids() -> List[int]:
    async with _get_session_maker()() as session:
        result = await session.execute(select(User.telegram_id))
        return [row[0] for row in result.fetchall()]


async def update_user_subscription_key(telegram_id: int, new_key: str) -> bool:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return False
        res = await session.execute(
            select(Subscription.id).where(Subscription.user_id == user.id).order_by(Subscription.id.desc()).limit(1)
        )
        sub_id_row = res.one_or_none()
        if not sub_id_row:
            return False
        sub_id = sub_id_row[0]
        await session.execute(update(Subscription).where(Subscription.id == sub_id).values(payment_reference=new_key))
        await session.commit()
        return True


async def update_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> bool:
    async with _get_session_maker()() as session:
        if username is None and first_name is None:
            return False
        values = {}
        if username is not None:
            values["username"] = username
        if first_name is not None:
            values["first_name"] = first_name
        result = await session.execute(update(User).where(User.telegram_id == telegram_id).values(**values))
        await session.commit()
        return result.rowcount > 0


async def get_referral_stats() -> List[Dict[str, Any]]:
    async with _get_session_maker()() as session:
        result = await session.execute(
            select(User.telegram_id, User.username, User.first_name, User.referral_count).where(
                User.referral_count > 0
            ).order_by(User.referral_count.desc())
        )
        return [_row_to_dict_serialized(row) for row in result.fetchall()]


async def reset_trial_for_user(telegram_id: int) -> bool:
    async with _get_session_maker()() as session:
        result = await session.execute(update(User).where(User.telegram_id == telegram_id).values(trial_used=0))
        await session.commit()
        return result.rowcount > 0


async def delete_user_completely(telegram_id: int) -> bool:
    async with _get_session_maker()() as session:
        r = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = r.scalar_one_or_none()
        if not user:
            return False
        referrer_id = user.referrer_id
        if referrer_id is not None:
            await session.execute(
                update(User).where(User.telegram_id == referrer_id, User.referral_count > 0).values(
                    referral_count=User.referral_count - 1
                )
            )
        await session.execute(delete(Subscription).where(Subscription.user_id == user.id))
        await session.execute(delete(User).where(User.telegram_id == telegram_id))
        await session.commit()
        return True
