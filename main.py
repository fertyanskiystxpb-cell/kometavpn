import asyncio
import logging
import os
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BufferedInputFile,
)

from dotenv import load_dotenv

load_dotenv()

# PostgreSQL на Render: при наличии DATABASE_URL используем database_pg
if os.getenv("DATABASE_URL"):
    from database_pg import (
        init_db,
        get_or_create_user,
        get_user_by_telegram_id,
        get_user_with_subscription,
        has_active_subscription,
        list_users_with_latest_subscription,
        create_active_subscription_for_telegram,
        list_used_keys,
        create_active_subscription_with_key,
        revoke_active_subscription_for_telegram,
        expire_outdated_subscriptions,
        reset_all_keys,
        reset_keys_by_keys,
        reset_keys_from_set,
        get_total_issued_keys_count,
        has_used_trial,
        mark_trial_used,
        get_all_telegram_ids,
        update_user_subscription_key,
        update_user,
        get_referral_stats,
        reset_trial_for_user,
        delete_user_completely,
        extend_active_subscription_by_days,
        set_user_api_client,
        list_expired_api_clients,
        clear_user_api_client,
        set_referrer_for_user,
    )
else:
    from database import (
        init_db,
        get_or_create_user,
        get_user_by_telegram_id,
        get_user_with_subscription,
        has_active_subscription,
        list_users_with_latest_subscription,
        create_active_subscription_for_telegram,
        list_used_keys,
        create_active_subscription_with_key,
        revoke_active_subscription_for_telegram,
        expire_outdated_subscriptions,
        reset_all_keys,
        reset_keys_by_keys,
        reset_keys_from_set,
        get_total_issued_keys_count,
        has_used_trial,
        mark_trial_used,
        get_all_telegram_ids,
        update_user_subscription_key,
        update_user,
        get_referral_stats,
        reset_trial_for_user,
        delete_user_completely,
        extend_active_subscription_by_days,
        set_user_api_client,
        list_expired_api_clients,
        clear_user_api_client,
        set_referrer_for_user,
    )

from xui_controller import XUIController, generate_vless_link


async def check_and_expire_subscriptions():
    """Проверяет и обновляет статус просроченных подписок в БД."""
    expired_count = await expire_outdated_subscriptions()
    if expired_count > 0:
        logger.info("expire_subscriptions: expired_count=%s, status=db_updated", expired_count)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8061067527:AAGc8Cz6yrQW5j5CD8HAxWleXO_vKj8Rw6Y")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "8516740130")
PAYMENT_URL = os.getenv("PAYMENT_URL", "https://example.com/checkout")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@kometavpn")

# Раздельные файлы ключей по длительности подписки
KEYS_FILE_3D = Path(__file__).with_name("keys_3d.txt")      # 3 дня (реферальные)
KEYS_FILE_7D = Path(__file__).with_name("keys_7d.txt")       # 7 дней
KEYS_FILE_30 = Path(__file__).with_name("keys.txt")          # 30 дней (старый файл)
KEYS_FILE_90 = Path(__file__).with_name("keys_90.txt")       # 90 дней
KEYS_FILE_180 = Path(__file__).with_name("keys_180.txt")     # 180 дней
KEYS_FILE_TRIAL = Path(__file__).with_name("keys_trial.txt")  # пробный период
KEYS_FILE = KEYS_FILE_30  # для /grant по умолчанию

# Для сброса ключей по файлу: callback suffix -> (Path, название)
RESET_KEYS_FILES = {
    "3d": (KEYS_FILE_3D, "keys_3d.txt (3 дн.)"),
    "7d": (KEYS_FILE_7D, "keys_7d.txt (7 дн.)"),
    "30": (KEYS_FILE_30, "keys.txt (30 дн.)"),
    "90": (KEYS_FILE_90, "keys_90.txt (90 дн.)"),
    "180": (KEYS_FILE_180, "keys_180.txt (180 дн.)"),
    "trial": (KEYS_FILE_TRIAL, "keys_trial.txt (пробный)"),
}

ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}

# 3x-ui панель
XUI_BASE_URL = os.getenv("XUI_BASE_URL", "https://193.109.69.12:2053/CtrJdwqTSwytSmlar1/")
XUI_USERNAME = os.getenv("XUI_USERNAME", "0SL0Yi6N66")
XUI_PASSWORD = os.getenv("XUI_PASSWORD", "WMpP9Mvjyg")
XUI_INBOUND_ID = int(os.getenv("XUI_INBOUND_ID", "1"))
xui: Optional[XUIController] = None


def get_xui() -> XUIController:
    """Ленивая инициализация контроллера XUI."""
    global xui
    if xui is None:
        xui = XUIController(
            base_url=XUI_BASE_URL,
            username=XUI_USERNAME,
            password=XUI_PASSWORD,
            inbound_id=XUI_INBOUND_ID,
        )
    return xui


async def issue_subscription_key(
    telegram_id: int,
    duration_days: int,
    keys_file: Optional[Path] = None,
) -> Optional[Tuple[str, bool]]:
    """
    Выдаёт подписку только через API панели (выдача из .txt отключена).
    Legacy-пользователи с уже выданными ключами из файлов не затрагиваются.
    Возвращает (vless_ссылка, True) или None при ошибке.
    """
    controller = get_xui()
    if not await controller.ensure_logged_in():
        logger.error("issue_subscription_key: user_id=%s, key_type=API, status=login_failed", telegram_id)
        return None
    uuid_str = await controller.add_user(telegram_id, duration_days)
    if not uuid_str:
        logger.error("issue_subscription_key: user_id=%s, key_type=API, status=addClient_failed", telegram_id)
        return None
    ok = await create_active_subscription_with_key(
        telegram_id=telegram_id,
        duration_days=duration_days,
        key=uuid_str,
    )
    if not ok:
        logger.error("issue_subscription_key: user_id=%s, key_type=API, status=db_create_failed", telegram_id)
        return None
    await set_user_api_client(telegram_id, uuid_str)
    logger.info("issue_subscription_key: user_id=%s, key_type=API, status=success, uuid=%s", telegram_id, uuid_str)
    return (generate_vless_link(uuid_str, f"Kometa-tg_{telegram_id}"), True)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Профиль")],
            [KeyboardButton(text="Купить подписку")],
            [KeyboardButton(text="Реферальная система")],
            [KeyboardButton(text="Поддержка")],
        ],
        resize_keyboard=True,
    )


async def ensure_subscribed(message: Message, pending_referrer_id: Optional[int] = None) -> bool:
    """
    Проверяет, подписан ли пользователь на обязательный канал.
    Если нет — отправляет ссылку на канал и возвращает False.
    Если pending_referrer_id указан и проверка успешна — начисляет реферальный бонус.
    """
    if not REQUIRED_CHANNEL:
        # Если канал не задан, не блокируем использование бота
        if pending_referrer_id:
            logger.info(f"Канал не задан, но есть pending_referrer_id={pending_referrer_id}, начисляем бонус")
            await process_referral_after_subscription(message.from_user.id, pending_referrer_id, message.bot)
        return True

    try:
        member = await message.bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL,
            user_id=message.from_user.id,
        )
        logger.info(f"Проверка подписки для {message.from_user.id}: статус = {member.status}")
    except Exception as e:
        # Если бот не может проверить (не админ в канале, приватный канал и т.п.) —
        # не блокируем пользователя. Реферал засчитываем, чтобы не терять бонусы.
        logger.warning(f"Не удалось проверить подписку на канал для {message.from_user.id}: {e}")
        if pending_referrer_id:
            logger.info(
                f"Не удалось проверить подписку, но есть pending_referrer_id={pending_referrer_id}, начисляем бонус вслепую"
            )
            await process_referral_after_subscription(message.from_user.id, pending_referrer_id, message.bot)
        return True

    # Проверяем, что пользователь подписан (статус не "left" и не "kicked")
    if member.status in ("left", "kicked"):
        logger.info(f"Пользователь {message.from_user.id} не подписан на канал (статус: {member.status})")
        if pending_referrer_id:
            cb = f"check_subscription_{pending_referrer_id}"
        else:
            cb = "check_subscription"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подписаться на канал",
                        url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Проверить подписку",
                        callback_data=cb,
                    )
                ],
            ]
        )
        await message.answer(
            "📢 Чтобы пользоваться ботом, нужно подписаться на наш канал.\n"
            f"📡 Канал: {REQUIRED_CHANNEL}\n"
            "✅ После подписки нажмите кнопку «Проверить подписку».",
            reply_markup=kb,
        )
        return False

    # Проверка подписки успешна — начисляем реферальный бонус если есть
    logger.info(f"Пользователь {message.from_user.id} подписан на канал (статус: {member.status})")
    if pending_referrer_id:
        logger.info(f"Начисление реферального бонуса: user_id={message.from_user.id}, referrer_id={pending_referrer_id}")
        await process_referral_after_subscription(message.from_user.id, pending_referrer_id, message.bot)
    
    return True

async def periodic_expiration_check():
    """Фоновая задача: истечение подписок в БД и удаление истёкших API-клиентов в панели."""
    while True:
        try:
            expired_count = await expire_outdated_subscriptions()
            if expired_count > 0:
                logger.info("periodic_expire: expired_count=%s, key_type=mixed, status=db_updated", expired_count)

            expired_api = await list_expired_api_clients()
            if expired_api:
                controller = get_xui()
                if await controller.ensure_logged_in():
                    for telegram_id, client_uuid in expired_api:
                        if await controller.delete_user(client_uuid):
                            await clear_user_api_client(telegram_id)
                            logger.info("periodic_expire: user_id=%s, key_type=API, status=success, panel=deleted, db=cleared, uuid=%s", telegram_id, client_uuid)
                        else:
                            logger.info("periodic_expire: user_id=%s, key_type=API, status=panel_delete_failed, uuid=%s", telegram_id, client_uuid)
                else:
                    logger.warning("periodic_expire: status=login_failed, pending_api_deletes=%s", len(expired_api))
        except Exception as e:
            logger.error("periodic_expire: status=error, error=%s", e)
        await asyncio.sleep(3600)

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment variables.")

    await init_db()
    
    # Запускаем фоновую задачу для проверки просроченных подписок   
    asyncio.create_task(periodic_expiration_check())

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

async def cmd_start(message: Message, command: CommandObject) -> None:
    referrer_id = None
    is_new_user = False
    
    # Проверяем, новый ли пользователь (не был в БД ранее)
    existing_user = await get_user_by_telegram_id(message.from_user.id)
    if not existing_user:
        is_new_user = True
        # Обработка реферальной ссылки: /start REF123456789
        if command.args:
            try:
                referrer_id = int(command.args.strip())
                # Проверяем, что реферер существует и это не сам пользователь
                if referrer_id == message.from_user.id:
                    referrer_id = None
                else:
                    # Проверяем, что реферер существует в БД
                    referrer = await get_user_by_telegram_id(referrer_id)
                    if not referrer:
                        referrer_id = None
            except ValueError:
                pass
    
    # Создаём или получаем пользователя (без referrer_id пока)
    user = await get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        referrer_id=None,  # Не устанавливаем реферера сразу
    )
    logger.info("User started bot: %s", user)

    # Проверяем подписку на канал (с передачей referrer_id для обработки после успешной проверки)
    subscription_ok = await ensure_subscribed(message, referrer_id if is_new_user else None)
    
    if not subscription_ok:
        return

    # Получаем username бота для реферальной ссылки
    try:
        bot_info = await message.bot.get_me()
        bot_username = bot_info.username or "vpnkometa_bot"
    except Exception:
        bot_username = "vpnkometa_bot"
    
    referral_link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    text = (
        "✨ Добро пожаловать в <b>Kometa VPN</b>! ✨\n"
        "⚡ Быстрый и дешёвый VPN-сервис!\n\n"
        "📄 Пользовательское соглашение: https://telegra.ph/Polzovatelskoe-soglashenie-02-12-23\n\n"
        f"📢 Чтобы пользоваться ботом, убедитесь, что вы подписаны на канал: {REQUIRED_CHANNEL}\n\n"
        f"🎁 <b>Реферальная система</b>\n"
        f"Пригласи друга и получи <b>3 дня</b> подписки!\n"
        f"Твоя ссылка: <code>{referral_link}</code>"
    )
    
    if is_new_user and referrer_id:
        text += "\n\n✅ Ты зарегистрировался по реферальной ссылке!"
    
    # Добавляем информацию о реферальной системе в приветствие
    text += "\n\n💡 Используй кнопку «Реферальная система» в меню для получения своей реферальной ссылки!"
    
    await message.answer(text, reply_markup=main_menu_keyboard())


async def process_referral_after_subscription(user_id: int, referrer_id: int, bot: Bot) -> None:
    """
    Обрабатывает начисление реферального бонуса после успешной проверки подписки на канал.
    Устанавливает referrer_id пользователю и начисляет бонус рефереру.
    """
    logger.info(f"Обработка реферального бонуса: user_id={user_id}, referrer_id={referrer_id}")
    
    # Проверяем, что реферер существует в БД
    referrer = await get_user_by_telegram_id(referrer_id)
    if not referrer:
        logger.warning(f"Реферер {referrer_id} не найден в БД при обработке реферального бонуса для {user_id}")
        return
    
    # Проверяем, что у пользователя ещё нет реферера (защита от повторной установки)
    user = await get_user_by_telegram_id(user_id)
    if not user:
        logger.warning(f"Пользователь {user_id} не найден в БД при обработке реферального бонуса")
        return
    
    if user.get("referrer_id") is not None:
        logger.info(f"Пользователь {user_id} уже имеет реферера {user.get('referrer_id')}, пропускаем")
        return
    
    success = await set_referrer_for_user(user_id, referrer_id)
    if not success:
        logger.info("Пользователь %s уже имеет реферера или не найден, пропускаем начисление бонуса", user_id)
        return
    logger.info("Реферер установлен для user_id=%s, счетчик рефералов увеличен для referrer_id=%s", user_id, referrer_id)

    # Начисляем реферальный бонус
    logger.info(f"Начисление реферального бонуса для {referrer_id}")
    await process_referral_bonus(referrer_id, bot)


async def process_referral_bonus(referrer_id: int, bot: Bot) -> None:
    """
    Реферальный бонус: тип ключа пригласившего определяет логику.
    - Legacy (.txt): только уведомление, подписку не продлеваем.
    - API: при активной подписке — +3 дня в панели и в БД; при отсутствии — выдача ключа через API.
    """
    logger.info("process_referral_bonus: referrer_id=%s", referrer_id)

    referrer = await get_user_by_telegram_id(referrer_id)
    if not referrer:
        logger.error("process_referral_bonus: referrer_id=%s, status=referrer_not_found", referrer_id)
        return

    is_api_referrer = bool(referrer.get("is_api_user"))
    has_active = await has_active_subscription(referrer_id)
    logger.info("process_referral_bonus: referrer_id=%s, key_type=%s, has_active=%s", referrer_id, "API" if is_api_referrer else "Legacy", has_active)

    if has_active:
        if is_api_referrer:
            # API-ключ: продлеваем в панели и в БД на 3 дня
            uuid_str = referrer.get("v2ray_uuid")
            if not uuid_str:
                logger.error("process_referral_bonus: referrer_id=%s, key_type=API, status=no_uuid", referrer_id)
                return
            controller = get_xui()
            if not await controller.ensure_logged_in():
                logger.error("process_referral_bonus: referrer_id=%s, key_type=API, status=login_failed", referrer_id)
                return
            panel_ok = await controller.extend_client_expiry_by_days(uuid_str, 3)
            if not panel_ok:
                logger.warning("process_referral_bonus: referrer_id=%s, key_type=API, status=panel_extend_failed", referrer_id)
            extended = await extend_active_subscription_by_days(referrer_id, 3)
            if not extended:
                logger.error("process_referral_bonus: referrer_id=%s, key_type=API, status=db_extend_failed", referrer_id)
                return
            logger.info("process_referral_bonus: referrer_id=%s, key_type=API, status=extended_3d, panel_ok=%s", referrer_id, panel_ok)
            expires_at = extended.get("expires_at") or ""
            expires_display = expires_at.split("T")[0] if expires_at and "T" in expires_at else (expires_at[:10] if expires_at else "—")
            username = f"@{referrer.get('username')}" if referrer.get('username') else "нет username"
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=(
                            "🎁 <b>Реферальный бонус (+3 дня в профиль)</b>\n\n"
                            f"👤 Кто пригласил: {referrer.get('first_name', '')} {username}\n"
                            f"🆔 TG ID: <code>{referrer_id}</code> (API-ключ)\n"
                            f"📅 Подписка продлена до: {expires_display}\n\n"
                            "Подписка продлена в панели и в БД."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.error("Не удалось отправить сообщение админу %s: %s", admin_id, e)
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        "🎉 <b>Реферальный бонус!</b>\n\n"
                        "Твой друг зарегистрировался по твоей ссылке.\n"
                        "В твой профиль начислено <b>+3 дня</b> подписки.\n\n"
                        "Ключ не меняется — проверь раздел «Профиль»."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("Не удалось уведомить реферера %s: %s", referrer_id, e)
        else:
            # Legacy-ключ: продлеваем только в БД на 3 дня
            extended = await extend_active_subscription_by_days(referrer_id, 3)
            if not extended:
                logger.error(
                    "process_referral_bonus: referrer_id=%s, key_type=Legacy, status=db_extend_failed",
                    referrer_id,
                )
                return
            expires_at = extended.get("expires_at") or ""
            expires_display = (
                expires_at.split("T")[0] if expires_at and "T" in expires_at else (expires_at[:10] if expires_at else "—")
            )
            logger.info(
                "process_referral_bonus: referrer_id=%s, key_type=Legacy, status=extended_3d", referrer_id
            )
            try:
                await bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        "🎉 <b>Реферальный бонус!</b>\n\n"
                        "Твой друг зарегистрировался по твоей ссылке.\n"
                        "В твой профиль начислено <b>+3 дня</b> подписки.\n\n"
                        f"📅 Новая дата окончания: <b>{expires_display}</b>\n\n"
                        "Твой ключ не меняется — проверь раздел «Профиль»."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.warning("Не удалось уведомить реферера (Legacy) %s: %s", referrer_id, e)
        return

    # Активной подписки нет — выдаём ключ только через API
    result = await issue_subscription_key(referrer_id, 3, None)
    if not result:
        logger.error("process_referral_bonus: referrer_id=%s, key_type=API, status=issue_failed", referrer_id)
        return

    key_or_link, _ = result
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text=(
                "🎉 <b>Реферальный бонус!</b>\n\n"
                "Твой друг зарегистрировался по твоей ссылке.\n"
                "Тебе начислено <b>3 дня</b> подписки!\n\n"
                f"🔑 Твоя ссылка для подключения:\n<code>{key_or_link}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("process_referral_bonus: не удалось отправить ключ рефереру %s: %s", referrer_id, e)
        username = f"@{referrer.get('username')}" if referrer.get('username') else "нет username"
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "🎁 <b>Реферальный бонус (3 дня)</b>\n\n"
                        f"👤 Реферер: {referrer.get('first_name', '')} {username}\n"
                        f"🆔 TG ID: <code>{referrer_id}</code>\n"
                        f"🔑 Ссылка: <code>{key_or_link[:80]}...</code>\n\n"
                        f"⚠️ Не удалось отправить сообщение рефереру. Ошибка: {e}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as admin_error:
                logger.error("Не удалось отправить реферальный бонус админу %s: %s", admin_id, admin_error)


async def cmd_help(message: Message) -> None:
    if not await ensure_subscribed(message):
        return
    
    # Расширенный help для админа: все команды с примерами
    if is_admin(message.from_user.id):
        await message.answer(
            "📚 <b>Команды бота</b>\n\n"
            "👥 <b>Для всех пользователей</b>\n"
            "➡️ /start – главное меню\n"
            "ℹ️ /help – список команд\n"
            "👤 /profile – ваш профиль и статус подписки (там же кнопка «Заменить ключ»)\n"
            "💳 /buy – купить подписку\n"
            "🎁 /referral – ваша реферальная ссылка и правила\n\n"
            "🛠 <b>Админ-команды</b>\n"
            "📊 /admin – отчёт по пользователям в .txt\n\n"
            "🎫 /grant – меню выдачи подписки по Telegram ID\n"
            "   (выберите длительность и затем отправьте TELEGRAM_ID одним сообщением)\n\n"
            "⛔ /revoke TELEGRAM_ID – отозвать активную подписку\n"
            "   Пример: <code>/revoke 123456789</code>\n\n"
            "👥 /referrals – статистика рефералов\n\n"
            "🔁 /reset_trial TELEGRAM_ID – сбросить возможность взять пробный период\n"
            "   Пример: <code>/reset_trial 123456789</code>\n\n"
            "🧹 /reset_keys – меню сброса ключей\n"
            "🧹 /reset_keys_count 30 [N] – сбросить N ключей из файла (30/90/180/trial)\n"
            "   Пример: <code>/reset_keys_count 30 5</code>\n"
            "🧹 /reset_key КЛЮЧ – сбросить один конкретный ключ\n"
            "   Пример: <code>/reset_key abc123-ключ</code>\n\n"
            "➕ /admin_add TELEGRAM_ID [username] [имя] – добавить/обновить пользователя в БД\n"
            "   Пример: <code>/admin_add 123456789 nick Ivan</code>\n\n"
            "🔑 /admin_setkey TELEGRAM_ID НОВЫЙ_КЛЮЧ – изменить ключ подписки\n"
            "   Пример: <code>/admin_setkey 123456789 my-new-key-123</code>\n\n"
            "✏️ /admin_setuser TELEGRAM_ID username|first_name ЗНАЧЕНИЕ – изменить ник/имя\n"
            "   Примеры:\n"
            "   <code>/admin_setuser 123456789 username newnick</code>\n"
            "   <code>/admin_setuser 123456789 first_name Иван Иванов</code>\n\n"
            "🗑 /delete_user TELEGRAM_ID – полностью удалить пользователя из БД\n"
            "   Пример: <code>/delete_user 123456789</code>\n\n"
            "📢 /broadcast Текст – рассылка всем пользователям\n"
            "   Пример: <code>/broadcast Всем привет!</code>\n\n"
            "✉️ /send TELEGRAM_ID Текст – сообщение одному пользователю\n"
            "   Пример: <code>/send 123456789 Ваш ключ готов!</code>\n"
        )
        return

    # Обычный help для пользователя
    await message.answer(
        "📚 <b>Команды бота</b>:\n"
        "➡️ /start – главное меню\n"
        "ℹ️ /help – этот список\n"
        "👤 /profile – ваш профиль и ключ\n"
        "💳 /buy – купить подписку\n"
        "🎁 /referral – ваша реферальная ссылка\n"
        "\nТакже вы можете пользоваться кнопками в главном меню."
    )


async def show_profile(message: Message) -> None:
    if not await ensure_subscribed(message):
        return

    # Проверяем и аннулируем просроченные подписки перед показом профиля
    await check_and_expire_subscriptions()

    user_with_sub = await get_user_with_subscription(message.from_user.id)
    if not user_with_sub:
        # Пользователь ещё не существует в БД (например, после миграции) — создаём без подписки
        user = await get_or_create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            referrer_id=None,
        )
        sub = None
    else:
        user, sub = user_with_sub
    show_replace_button = False
    if sub is None:
        sub_status = "Нет подписки"
        display_status = "Нет подписки"
        extra_lines: list[str] = []
    else:
        sub_status = sub.get("status", "unknown")
        expires = sub.get("expires_at")
        key = sub.get("payment_reference")
        extra_lines = []
        
        # Форматируем статус подписки для отображения
        if sub_status == "active":
            display_status = "Активна"
            if expires:
                # Форматируем дату в ГГГГ-ММ-ДД
                try:
                    # Пробуем распарсить ISO формат и взять только дату
                    expires_date = expires.split('T')[0] if 'T' in expires else expires[:10]
                    extra_lines.append(f"Истекает: {expires_date}")
                except:
                    extra_lines.append(f"Истекает: {expires}")
            if key:
                if user.get("is_api_user") and user.get("v2ray_uuid"):
                    display_key = generate_vless_link(
                        user["v2ray_uuid"],
                        f"Kometa-tg_{user.get('telegram_id', '')}",
                    )
                    extra_lines.append(f"Ваша ссылка для подключения:\n<code>{display_key}</code>")
                else:
                    extra_lines.append(f"Ваш ключ: {key}")
                # Кнопка «Заменить ключ» доступна при любой активной подписке с ключом
                show_replace_button = True
        elif sub_status == "expired":
            display_status = "Истекла"
            extra_lines.append("Срок действия подписки истек")
        elif sub_status == "revoked":
            display_status = "Отозвана"
            extra_lines.append("Подписка отозвана администратором")
            # Не показываем ключ для отозванной подписки
            key = None
        elif sub_status == "pending":
            display_status = "Ожидает подтверждения"
        else:
            display_status = sub_status

    extra = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    if user.get("username"):
        header = (
            "👤 <b>Ваш профиль</b>:\n"
            f"Имя: {user.get('first_name')}\n"
            f"Юзернейм: @{user.get('username')}"
        )
    else:
        header = f"👤 <b>Ваш профиль</b>:\nИмя: {user.get('first_name')}\n"

    text = header + f"\n💼 Статус подписки: {display_status}{extra}"
    if show_replace_button:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔁 Заменить ключ",
                        callback_data="replace_key",
                    )
                ],
            ]
        )
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text)


async def handle_replace_key_callback(callback: CallbackQuery) -> None:
    """
    Заменяет текущий ключ пользователя на новый API-ключ, сохраняя оставшийся срок подписки.
    Работает как для legacy-ключей, так и для уже выданных API-ключей.
    """
    if not callback.from_user or not callback.message:
        await callback.answer()
        return

    user_with_sub = await get_user_with_subscription(callback.from_user.id)
    if not user_with_sub:
        await callback.answer("Профиль не найден. Нажмите /start.", show_alert=True)
        return

    user, sub = user_with_sub
    if not sub or sub.get("status") != "active":
        await callback.answer("У вас нет активной подписки для замены ключа.", show_alert=True)
        return

    expires = sub.get("expires_at")
    if not expires:
        await callback.answer("Не удалось определить срок действия подписки.", show_alert=True)
        return

    # Считаем оставшееся количество дней, чтобы выдать новый ключ на тот же срок
    try:
        if "T" in expires:
            expires_dt = dt.datetime.fromisoformat(expires)
        else:
            expires_dt = dt.datetime.strptime(expires[:10], "%Y-%m-%d")
        now = dt.datetime.utcnow()
        remaining_seconds = (expires_dt - now).total_seconds()
        if remaining_seconds <= 0:
            await callback.answer("Срок действия вашей подписки истёк. Ключ заменить нельзя.", show_alert=True)
            return
        remaining_days = max(1, int(remaining_seconds // 86400))
    except Exception as e:
        logger.error(
            "replace_key: failed to parse expires_at for user_id=%s, expires=%s, error=%s",
            callback.from_user.id,
            expires,
            e,
        )
        await callback.answer("Произошла ошибка при обработке срока действия подписки.", show_alert=True)
        return

    controller = get_xui()
    if not await controller.ensure_logged_in():
        logger.error("replace_key: login_failed user_id=%s", callback.from_user.id)
        await callback.answer("Не удалось подключиться к VPN-серверу. Попробуйте позже.", show_alert=True)
        return

    # Если был старый API-ключ — удаляем клиента в панели и очищаем привязку
    old_uuid = user.get("v2ray_uuid")
    if user.get("is_api_user") and old_uuid:
        deleted = await controller.delete_user(old_uuid)
        if not deleted:
            logger.warning("replace_key: delete_old_failed user_id=%s uuid=%s", callback.from_user.id, old_uuid)
            await callback.answer(
                "Не удалось удалить старый ключ. Попробуйте позже или обратитесь в поддержку.",
                show_alert=True,
            )
            return
        await clear_user_api_client(callback.from_user.id)

    # Создаём нового клиента в панели на оставшийся срок
    new_uuid = await controller.add_user(callback.from_user.id, remaining_days)
    if not new_uuid:
        logger.error("replace_key: add_user_failed user_id=%s", callback.from_user.id)
        await callback.answer("Не удалось выдать новый ключ. Попробуйте позже.", show_alert=True)
        return

    # Обновляем ключ в последней подписке и помечаем пользователя как API-пользователя
    updated = await update_user_subscription_key(telegram_id=callback.from_user.id, new_key=new_uuid)
    if not updated:
        logger.error("replace_key: update_user_subscription_key_failed user_id=%s", callback.from_user.id)
        await callback.answer("Не удалось обновить ваш ключ в базе данных.", show_alert=True)
        return

    await set_user_api_client(callback.from_user.id, new_uuid)

    key_or_link = generate_vless_link(new_uuid, f"Kometa-tg_{user.get('telegram_id', '')}")

    await callback.message.answer(
        "🔁 <b>Ключ заменён</b>\n\n"
        "Старый ключ был удалён. Вот ваш новый ключ для подключения:\n"
        f"<code>{key_or_link}</code>\n\n"
        "Если VPN уже был настроен, просто замените старый ключ на этот.",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("Новый ключ выдан.")


async def show_buy_info(message: Message) -> None:
    if not await ensure_subscribed(message):
        return

    text = (
        "💳 <b>Выберите длительность подписки</b>:\n\n"
        "🆓 Пробный период (1 день) — бесплатно, можно получить только один раз\n"
        "📅 30 дней — 60 ₽\n"
        "💰 90 дней — 150 ₽\n"
        "👑 180 дней — 280 ₽\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆓 Пробный период (1 день)", callback_data="sub_trial")],
            [InlineKeyboardButton(text="📅 30 дней", callback_data="sub_30")],
            [InlineKeyboardButton(text="💰 90 дней", callback_data="sub_90")],
            [InlineKeyboardButton(text="👑 180 дней", callback_data="sub_180")],
        ]
    )

    await message.answer(text, reply_markup=kb)


async def handle_subscription_duration_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.message:
        await callback.answer()
        return

    # Проверка подписки на канал для callback.
    # Если не можем проверить (ошибка от Telegram) — не блокируем.
    try:
        member = await callback.message.bot.get_chat_member(
            chat_id=REQUIRED_CHANNEL,
            user_id=callback.from_user.id,
        )
        if member.status in ("left", "kicked"):
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Подписаться на канал",
                            url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Проверить подписку",
                            callback_data="check_subscription",
                        )
                    ],
                ]
            )
            await callback.message.answer(
                "Чтобы пользоваться ботом, нужно подписаться на наш канал.\n"
                f"Канал: {REQUIRED_CHANNEL}\n"
                "После подписки нажмите кнопку «Проверить подписку».",
                reply_markup=kb,
            )
            await callback.answer()
            return
    except Exception as e:
        logger.warning("Не удалось проверить подписку (callback): %s", e)

    data = callback.data or ""
    # Пробный период обрабатываем отдельно
    if data == "sub_trial":
        # Проверяем, не использовал ли уже пробный период
        if await has_used_trial(callback.from_user.id):
            await callback.message.answer(
                "⚠️ Вы уже использовали пробный период. Доступны только платные подписки."
            )
            await callback.answer()
            return

        days = 1
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Получить пробный период",
                        callback_data=f"pay_{days}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Закрыть",
                        callback_data="buy_close",
                    )
                ],
            ]
        )

        text = (
            "🆓 Вы выбрали пробный период на 1 день.\n\n"
            "ℹ️ Пробный период выдается бесплатно и только один раз.\n"
            "👇 Нажмите «Получить пробный период», чтобы получить ключ.\n"
        )
    else:
        # Разбираем длительность из callback_data вида sub_30 / sub_90 / sub_180
        if not data.startswith("sub_"):
            await callback.answer()
            return

        try:
            days = int(data.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка выбора тарифа.")
            return

        # Показываем информацию и две кнопки: Получить / Закрыть.
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Получить",
                        callback_data=f"pay_{days}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Закрыть",
                        callback_data="buy_close",
                    )
                ],
            ]
        )

        # Определяем цену в зависимости от выбранной длительности
        if days == 30:
            price = 60
        elif days == 90:
            price = 150
        elif days == 180:
            price = 280
        else:
            price = 0  # защита от некорректных значений

        text = (
            f"🛒 Вы выбрали подписку на <b>{days}</b> дней.\n\n"
            f"💳 Для покупки VPN отправьте <b>{price} ₽</b> на данные реквизиты:\n\n"
            "💳 2200 7021 0425 4771\n\n"
            "✅ После оплаты нажмите на кнопку «Получить».\n"
        )

    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


async def handle_pay_callback(callback: CallbackQuery) -> None:
    """
    Обработка нажатия кнопки «Получить».
    Для платных тарифов отправляет заявку админу,
    для пробного периода сразу выдаёт ключ.
    """
    if not callback.from_user or not callback.message:
        await callback.answer()
        return

    data = callback.data or ""
    if not data.startswith("pay_"):
        await callback.answer()
        return

    try:
        days = int(data.split("_", maxsplit=1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка параметров оплаты.")
        return

    user = callback.from_user
    username = f"@{user.username}" if user.username else "нет username"

    # ===== Пробный период (1 день) =====
    if days == 1:
        if await has_used_trial(user.id):
            await callback.message.answer(
                "⚠️ Вы уже использовали пробный период. Выберите платную подписку."
            )
            await callback.answer()
            return

        has_active = await has_active_subscription(user.id)

        # Если подписка уже есть, просто добавляем 1 день к текущему сроку (без нового ключа)
        if has_active:
            db_user = await get_user_by_telegram_id(user.id)
            is_api = bool(db_user and db_user.get("is_api_user") and db_user.get("v2ray_uuid"))

            if is_api:
                controller = get_xui()
                if await controller.ensure_logged_in():
                    panel_ok = await controller.extend_client_expiry_by_days(db_user["v2ray_uuid"], days)
                    if not panel_ok:
                        logger.warning(
                            "trial_extend: user_id=%s, key_type=API, status=panel_extend_failed", user.id
                        )

            extended = await extend_active_subscription_by_days(user.id, days)
            if not extended:
                await callback.message.answer(
                    "❌ Не удалось продлить вашу подписку на пробный день. Попробуйте позже."
                )
                await callback.answer()
                return

            await mark_trial_used(user.id)
            expires_at = extended.get("expires_at") or ""
            expires_date = (
                expires_at.split("T")[0] if expires_at and "T" in expires_at else (expires_at[:10] if expires_at else "—")
            )
            text = (
                "🆓 <b>Пробный период активирован</b>\n\n"
                "У вас уже была подписка, поэтому к текущему сроку добавлен <b>+1 день</b>.\n\n"
                f"📅 Новая дата окончания: <b>{expires_date}</b>\n\n"
                "Ключ не меняется — проверьте раздел «Профиль»."
            )
            await callback.message.answer(text, parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        # Активной подписки нет — выдаём новый API-ключ на 1 день
        result = await issue_subscription_key(user.id, 1, None)
        if not result:
            await callback.message.answer(
                "❌ Не удалось оформить пробную подписку (ошибка API). Попробуйте позже или обратитесь к администратору."
            )
            await callback.answer()
            return

        key_or_link, _ = result
        await mark_trial_used(user.id)
        expires_date = (dt.datetime.utcnow() + dt.timedelta(days=days)).strftime("%Y-%m-%d")
        text = (
            "🆓 Вам выдан <b>пробный период на 1 день</b>.\n\n"
            f"📅 Активен до: <b>{expires_date}</b>\n\n"
            f"🔑 Ваша ссылка для подключения:\n<code>{key_or_link}</code>\n\n"
            "📘 Инструкция по установке: https://telegra.ph/Kak-podklyuchit-VPN-za-1-minutu-02-13\n\n"
            "👤 Ключ также доступен в разделе «Профиль»."
        )
        await callback.message.answer(text, parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    # ===== Платные тарифы — логика с подтверждением админом =====

    # Сообщаем пользователю, что заявка отправлена администратору
    await callback.message.answer(
        "⌛️ Заявка на оплату отправлена администратору.\n"
        "✅ После проверки оплаты бот выдаст вам ключ.",
    )

    # Формируем клавиатуру для админа
    admin_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить оплату и выдать ключ",
                    callback_data=f"admin_confirm_{user.id}_{days}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить заявку",
                    callback_data=f"admin_decline_{user.id}_{days}",
                )
            ],
        ]
    )

    # Отправляем сообщение всем администраторам
    for admin_id in ADMIN_IDS:
        try:
            await callback.message.bot.send_message(
                chat_id=admin_id,
                text=(
                    "🆕 <b>Новая заявка на оплату</b>\n\n"
                    f"👤 Пользователь: {user.first_name or ''} {username}\n"
                    f"🆔 TG ID: <code>{user.id}</code>\n"
                    f"📅 Тариф: <b>{days}</b> дней\n\n"
                    "👇 Нажмите кнопку ниже, чтобы подтвердить оплату и выдать ключ "
                    "или отклонить заявку."
                ),
                reply_markup=admin_kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error(f"Не удалось отправить заявку админу {admin_id}: {e}")

    await callback.answer()


async def handle_admin_payment_callback(callback: CallbackQuery) -> None:
    """
    Обработка нажатия админских кнопок подтверждения / отклонения оплаты.
    """
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    data = callback.data or ""
    if not (data.startswith("admin_confirm_") or data.startswith("admin_decline_")):
        await callback.answer()
        return

    parts = data.split("_")
    if len(parts) != 4:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    action = parts[1]  # confirm / decline
    try:
        user_tg_id = int(parts[2])
        days = int(parts[3])
    except ValueError:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    if action == "decline":
        # Уведомляем пользователя об отклонении заявки
        try:
            await callback.message.bot.send_message(
                chat_id=user_tg_id,
                text=(
                    "❌ Ваша заявка на оплату была отклонена администратором.\n"
                    "Если вы считаете, что это ошибка, свяжитесь с поддержкой."
                ),
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_tg_id} об отклонении: {e}")

        try:
            await callback.message.edit_text(
                f"❌ Заявка пользователя {user_tg_id} на {days} дней отклонена."
            )
        except Exception:
            pass

        await callback.answer("❌ Заявка отклонена.")
        return

    # ===== Подтверждение оплаты и выдача ключа =====

    if days == 30:
        key_file = KEYS_FILE_30
    elif days == 90:
        key_file = KEYS_FILE_90
    elif days == 180:
        key_file = KEYS_FILE_180
    else:
        key_file = KEYS_FILE_30

    result = await issue_subscription_key(user_tg_id, days, key_file)
    if not result:
        await callback.answer(
            "❌ Не удалось выдать подписку (нет ключей или ошибка API).",
            show_alert=True,
        )
        return

    key_or_link, is_api = result
    issued_count = len(await list_used_keys())
    expires_date = (dt.datetime.utcnow() + dt.timedelta(days=days)).strftime("%Y-%m-%d")
    key_label = "ссылка для подключения" if is_api else "ключ доступа"

    try:
        await callback.message.bot.send_message(
            chat_id=user_tg_id,
            text=(
                f"🎉 Поздравляю с покупкой!\n"
                f"✅ Вам выдана подписка на <b>{days}</b> дней.\n\n"
                f"📅 Активна до: <b>{expires_date}</b>\n\n"
                f"🔑 Ваш {key_label}:\n<code>{key_or_link}</code>\n\n"
                "📘 Инструкция по установке: https://telegra.ph/Kak-podklyuchit-VPN-za-1-minutu-02-13\n\n"
                "👤 Ключ также можно посмотреть в разделе «Профиль»."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить ключ пользователю {user_tg_id}: {e}")

    try:
        display_key = key_or_link if len(key_or_link) <= 60 else key_or_link[:57] + "..."
        await callback.message.edit_text(
            "✅ Оплата подтверждена и ключ выдан.\n\n"
            f"👤 Пользователь: <code>{user_tg_id}</code>\n"
            f"📅 Тариф: {days} дней\n"
            f"🔑 Ключ/ссылка: <code>{display_key}</code>\n"
            f"📦 Всего выдано ключей: {issued_count}.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    await callback.answer("✅ Оплата подтверждена и ключ выдан.")


async def handle_buy_close_callback(callback: CallbackQuery) -> None:
    """
    Кнопка «Закрыть» под формой покупки.
    Просто убирает сообщение или клавиатуру.
    """
    if not callback.message:
        await callback.answer()
        return

    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    await callback.answer("Ок ✅")


async def handle_check_subscription_callback(callback: CallbackQuery) -> None:
    """
    Обработчик кнопки «Проверить подписку».
    Ещё раз проверяет, подписан ли пользователь на канал.
    Обрабатывает реферальный бонус если был передан referrer_id в callback_data.
    """
    if not callback.from_user:
        await callback.answer()
        return

    # Извлекаем referrer_id из callback_data если есть: check_subscription_123456789
    pending_referrer_id = None
    data = callback.data or ""
    if data.startswith("check_subscription_") and len(data) > 19:
        try:
            ref_id_str = data.replace("check_subscription_", "")
            if ref_id_str:
                pending_referrer_id = int(ref_id_str)
                logger.info(f"Извлечен referrer_id из callback: {pending_referrer_id}")
        except ValueError:
            logger.warning(f"Не удалось извлечь referrer_id из callback_data: {data}")

    try:
        member = await callback.message.bot.get_chat_member(  # type: ignore[union-attr]
            chat_id=REQUIRED_CHANNEL,
            user_id=callback.from_user.id,
        )
        logger.info(f"Проверка подписки через кнопку для {callback.from_user.id}: статус = {member.status}")
        if member.status in ("left", "kicked"):
            logger.info(f"Пользователь {callback.from_user.id} всё ещё не подписан (статус: {member.status})")
            await callback.answer("Вы всё ещё не подписаны на канал.", show_alert=True)
            return
    except Exception as e:
        # Если не удалось проверить — НЕ засчитываем реферал (защита от накрутки).
        logger.error(f"Не удалось проверить подписку (check button) для {callback.from_user.id}: {e}")
        await callback.answer("Не удалось проверить подписку. Попробуйте позже.", show_alert=True)
        return

    # Проверка подписки успешна — начисляем реферальный бонус если есть
    logger.info(f"Пользователь {callback.from_user.id} подписан на канал (статус: {member.status})")
    if pending_referrer_id:
        logger.info(f"Начисление реферального бонуса через кнопку: user_id={callback.from_user.id}, referrer_id={pending_referrer_id}")
        await process_referral_after_subscription(
            callback.from_user.id, pending_referrer_id, callback.message.bot
        )

    await callback.answer("✅ Подписка найдена!", show_alert=True)
    await callback.message.answer(  # type: ignore[union-attr]
        "✅ Подписка на канал подтверждена.\n"
        "Теперь вы можете пользоваться ботом. Нажмите /start или используйте меню."
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def cmd_admin_panel(message: Message) -> None:
    """
    Доступна только администраторам.
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к админ-панели.")
        return
    
    # Проверяем и аннулируем просроченные подписки
    expired_count = await expire_outdated_subscriptions()
    if expired_count > 0:
        logger.info(f"Автоматически аннулировано {expired_count} просроченных подписок")

    users = await list_users_with_latest_subscription()
    total_users = len(users)

    # Количество выданных ключей (по всем подпискам с payment_reference)
    used_keys = await list_used_keys()
    total_keys = len(used_keys)
    
    # Подсчет активных подписок
    active_subs = 0
    expired_subs = 0
    revoked_subs = 0
    pending_subs = 0

    # Формируем содержимое для .txt файла (без HTML)
    file_lines = [
        "Админ-панель Kometa VPN",
        "=" * 40,
        f"Всего пользователей: {total_users}",
        f"Всего выданных ключей: {total_keys}",
        "",
    ]

    if not users:
        file_lines.append("Пользователей пока нет.")
    else:
        for u in users:
            sub_status = u.get("subscription_status")
            if sub_status == "active":
                active_subs += 1
            elif sub_status == "expired":
                expired_subs += 1
            elif sub_status == "revoked":
                revoked_subs += 1
            elif sub_status == "pending":
                pending_subs += 1

        file_lines.append(f"Активных подписок: {active_subs}")
        file_lines.append(f"Истекших подписок: {expired_subs}")
        file_lines.append(f"Отозванных подписок: {revoked_subs}")
        file_lines.append(f"Ожидающих: {pending_subs}")
        file_lines.append("")
        file_lines.append("Пользователи:")
        file_lines.append("-" * 40)

        for idx, u in enumerate(users, start=1):
            username = f"@{u['username']}" if u.get("username") else "—"
            first_name = u.get("first_name") or "—"
            sub_status = u.get("subscription_status")
            expires_at = u.get("subscription_expires_at")
            sub_key = u.get("subscription_key")

            if sub_status is None:
                sub_text = "Нет подписки"
            elif sub_status == "active":
                if expires_at:
                    expires_date = expires_at.split('T')[0] if 'T' in expires_at else expires_at[:10]
                    sub_text = f"Активна до: {expires_date}"
                    if sub_key:
                        sub_text += f", Ключ: {sub_key}"
                else:
                    sub_text = "Активна"
            elif sub_status == "expired":
                sub_text = "Истекла"
            elif sub_status == "revoked":
                sub_text = "Отозвана"
            elif sub_status == "pending":
                sub_text = "Ожидает подтверждения"
            else:
                sub_text = sub_status

            file_lines.append(
                f"{idx}. user_id={u['user_id']}, telegram_id={u['telegram_id']}"
            )
            file_lines.append(f"   Имя: {first_name}, Ник: {username}")
            file_lines.append(f"   Подписка: {sub_text}")
            file_lines.append("")

    file_lines.append("Команды админа:")
    file_lines.append("/admin          — этот отчёт (БД в .txt)")
    file_lines.append("/admin_add TELEGRAM_ID [username] [имя] — добавить пользователя в БД")
    file_lines.append("/admin_setkey TELEGRAM_ID КЛЮЧ — изменить ключ подписки у пользователя")
    file_lines.append("/admin_setuser TELEGRAM_ID username|first_name ЗНАЧЕНИЕ — изменить ник/имя")
    file_lines.append("/grant TELEGRAM_ID ДНИ  — выдать подписку")
    file_lines.append("/revoke TELEGRAM_ID     — отозвать подписку")
    file_lines.append("/delete_user TELEGRAM_ID — полностью удалить пользователя из БД")
    file_lines.append("/broadcast Текст        — сообщение всем пользователям")
    file_lines.append("/send TELEGRAM_ID Текст — сообщение одному пользователю")

    file_content = "\n".join(file_lines)
    doc = BufferedInputFile(
        file_content.encode("utf-8"),
        filename="admin_users.txt",
    )
    await message.answer_document(
        document=doc,
        caption="🛠 <b>Админ-панель Kometa VPN</b>\n\n"
        f"👥 Пользователей: {total_users} | 🔑 Выдано ключей: {total_keys}\n\n"
        "📎 Полный отчёт во вложении.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_grant_subscription(message: Message) -> None:
    """
    Выдача подписки пользователю через админ-панель с выбором длительности кнопками.
    /grant
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 дня", callback_data="grant_3"),
                InlineKeyboardButton(text="7 дней", callback_data="grant_7"),
            ],
            [
                InlineKeyboardButton(text="30 дней", callback_data="grant_30"),
                InlineKeyboardButton(text="90 дней", callback_data="grant_90"),
            ],
            [
                InlineKeyboardButton(text="180 дней", callback_data="grant_180"),
                InlineKeyboardButton(text="∞ Бесконечность", callback_data="grant_inf"),
            ],
            [
                InlineKeyboardButton(text="📆 Другое количество дней", callback_data="grant_custom"),
            ],
        ]
    )
    await message.answer(
        "💳 <b>Выдача подписки</b>\n\n"
        "Выберите длительность подписки, затем введите Telegram ID пользователя.",
        reply_markup=kb,
    )


async def handle_grant_callback(callback: CallbackQuery) -> None:
    """Обработчик выбора длительности для /grant."""
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    
    data = callback.data or ""
    if not data.startswith("grant_"):
        await callback.answer()
        return
    
    duration_key = data.replace("grant_", "")
    if duration_key == "custom":
        # Сохраняем состояние: ждём ввода количества дней от админа
        if not hasattr(handle_grant_callback, "_pending"):
            handle_grant_callback._pending = {}
        handle_grant_callback._pending[callback.from_user.id] = ("await_days", None, None)
        await callback.message.edit_text(
            "✏️ Введите количество дней подписки (целое число, например 45)."
        )
        await callback.answer()
        return
    duration_map = {
        "3": (3, KEYS_FILE_3D, "keys_3d.txt"),
        "7": (7, KEYS_FILE_7D, "keys_7d.txt"),
        "30": (30, KEYS_FILE_30, "keys.txt"),
        "90": (90, KEYS_FILE_90, "keys_90.txt"),
        "180": (180, KEYS_FILE_180, "keys_180.txt"),
        "inf": (0, KEYS_FILE_30, "keys.txt"),  # бесконечность = 0 дней
    }
    
    if duration_key not in duration_map:
        await callback.answer("Неверная длительность")
        return
    
    days, key_file, file_name = duration_map[duration_key]
    days_text = "бесконечность" if days == 0 else f"{days} дней"
    
    await callback.message.edit_text(
        f"📅 Выбрано: <b>{days_text}</b>\n\n"
        f"Отправьте Telegram ID пользователя одним сообщением (только число).",
    )
    await callback.answer()
    
    # Сохраняем выбранную длительность в состоянии (простое решение через глобальную переменную или лучше через FSM)
    # Для простоты используем временное хранилище в памяти
    if not hasattr(handle_grant_callback, "_pending"):
        handle_grant_callback._pending = {}
    handle_grant_callback._pending[callback.from_user.id] = (days, key_file, file_name)


async def handle_grant_user_id(message: Message) -> None:
    """Обрабатывает ввод Telegram ID после выбора длительности."""
    if not is_admin(message.from_user.id):
        return
    
    if not hasattr(handle_grant_callback, "_pending"):
        return
    
    pending = handle_grant_callback._pending.get(message.from_user.id)
    if not pending:
        return
    
    days, key_file, file_name = pending
    del handle_grant_callback._pending[message.from_user.id]
    
    try:
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Telegram ID должен быть числом.")
        return
    
    result = await issue_subscription_key(telegram_id, days, key_file)
    if not result:
        await message.answer(
            f"❌ Не удалось выдать подписку (нет ключей в {file_name} или ошибка API)."
        )
        return

    key_or_link, is_api = result
    issued_count = len(await list_used_keys())
    days_text = "бесконечность" if days == 0 else f"{days} дней"
    if days > 0:
        expires_date = (dt.datetime.utcnow() + dt.timedelta(days=days)).strftime("%Y-%m-%d")
        expires_text = f"📅 Активна до: <b>{expires_date}</b>\n"
    else:
        expires_text = "📅 Активна: <b>бессрочно</b>\n"
    key_label = "ссылка" if is_api else "ключ"

    try:
        await message.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"✅ Вам выдана подписка на {days_text}.\n\n"
                f"{expires_text}\n"
                f"🔑 Ваш {key_label} доступа:\n<code>{key_or_link}</code>\n\n"
                "Этот ключ также можно посмотреть в разделе «Профиль»."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {telegram_id}: {e}")

    display_key = key_or_link if len(key_or_link) <= 60 else key_or_link[:57] + "..."
    await message.answer(
        f"✅ Подписка на {days_text} выдана пользователю <code>{telegram_id}</code>.\n"
        f"{expires_text}"
        f"🔑 {key_label.capitalize()}: <code>{display_key}</code>\n"
        f"📦 Всего выдано ключей: {issued_count}."
    )


async def cmd_revoke_subscription(message: Message, command: CommandObject) -> None:
    """
    Удаление (отзыв) подписки пользователя.
    /revoke TELEGRAM_ID
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return

    if not command.args or not command.args.strip().isdigit():
        await message.answer(
            "ℹ️ Использование: /revoke TELEGRAM_ID\n"
            "Пример: <code>/revoke 123456789</code>"
        )
        return

    telegram_id = int(command.args.strip())

    ok = await revoke_active_subscription_for_telegram(telegram_id=telegram_id)

    if not ok:
        logger.info("revoke: user_id=%s, status=no_active_subscription_or_user_not_found", telegram_id)
        await message.answer(
            "❌ Не найдено активной подписки для этого пользователя "
            "или пользователь отсутствует в базе."
        )
        return

    user = await get_user_by_telegram_id(telegram_id)
    key_type = "API" if (user and user.get("is_api_user")) else "Legacy"

    if user and user.get("is_api_user") and user.get("v2ray_uuid"):
        controller = get_xui()
        if await controller.ensure_logged_in():
            if await controller.delete_user(user["v2ray_uuid"]):
                await clear_user_api_client(telegram_id)
                logger.info("revoke: user_id=%s, key_type=%s, status=success, panel=deleted, db=revoked", telegram_id, key_type)
            else:
                logger.info("revoke: user_id=%s, key_type=%s, status=db_revoked_panel_failed, uuid=%s", telegram_id, key_type, user["v2ray_uuid"])
        else:
            logger.info("revoke: user_id=%s, key_type=%s, status=db_revoked_panel_login_failed", telegram_id, key_type)
    else:
        logger.info("revoke: user_id=%s, key_type=%s, status=success, panel=not_applicable, db=revoked", telegram_id, key_type)

    # Пытаемся уведомить пользователя
    try:
        await message.bot.send_message(
            chat_id=telegram_id,
            text=(
                "⛔ Ваша подписка была отозвана администратором.\n"
                "Если вы считаете, что это ошибка, свяжитесь с поддержкой."
            ),
        )
    except Exception:
        # Игнорируем, если не удалось отправить
        pass

    await message.answer(f"✅ Подписка пользователя <code>{telegram_id}</code> успешно отозвана.")


async def cmd_admin_add_user(message: Message, command: CommandObject) -> None:
    """
    Добавить пользователя в БД (по telegram_id). Если уже есть — обновит запись.
    /admin_add TELEGRAM_ID [username] [имя]
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not command.args or not command.args.strip():
        await message.answer(
            "ℹ️ Использование: /admin_add TELEGRAM_ID [username] [имя]\n"
            "Пример: /admin_add 123456789\n"
            "Пример: /admin_add 123456789 nick Ivan"
        )
        return
    parts = command.args.strip().split(maxsplit=2)
    try:
        telegram_id = int(parts[0])
    except ValueError:
        await message.answer("❌ TELEGRAM_ID должен быть числом.")
        return
    username = parts[1].lstrip("@") if len(parts) > 1 and parts[1] else None
    first_name = parts[2] if len(parts) > 2 else None
    user = await get_or_create_user(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
    )
    await message.answer(
        f"✅ Пользователь добавлен/обновлён в БД:\n"
        f"🆔 telegram_id: <code>{user['telegram_id']}</code>\n"
        f"👤 username: @{user.get('username') or '—'}\n"
        f"📝 имя: {user.get('first_name') or '—'}"
    )


async def cmd_admin_setkey(message: Message, command: CommandObject) -> None:
    """
    Изменить ключ подписки у пользователя (у последней подписки).
    /admin_setkey TELEGRAM_ID НОВЫЙ_КЛЮЧ
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not command.args or not command.args.strip():
        await message.answer(
            "ℹ️ Использование: /admin_setkey TELEGRAM_ID НОВЫЙ_КЛЮЧ\n"
            "Пример: /admin_setkey 123456789 my-new-key-123"
        )
        return
    parts = command.args.strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Укажите TELEGRAM_ID и новый ключ.")
        return
    try:
        telegram_id = int(parts[0])
    except ValueError:
        await message.answer("❌ TELEGRAM_ID должен быть числом.")
        return
    new_key = parts[1].strip()
    ok = await update_user_subscription_key(telegram_id=telegram_id, new_key=new_key)
    if not ok:
        await message.answer(
            "❌ Не удалось обновить ключ: пользователь не найден или у него нет подписки.\n"
            "Сначала добавьте пользователя и/или выдайте подписку через /grant."
        )
        return
    await message.answer(
        f"✅ Ключ подписки для <code>{telegram_id}</code> обновлён на: <code>{new_key}</code>"
    )


async def cmd_admin_setuser(message: Message, command: CommandObject) -> None:
    """
    Изменить username или first_name пользователя.
    /admin_setuser TELEGRAM_ID username НОВОЕ_ЗНАЧЕНИЕ
    /admin_setuser TELEGRAM_ID first_name Новое Имя
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not command.args or not command.args.strip():
        await message.answer(
            "ℹ️ Использование:\n"
            "/admin_setuser TELEGRAM_ID username новый_ник\n"
            "/admin_setuser TELEGRAM_ID first_name Имя Фамилия"
        )
        return
    parts = command.args.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("❌ Укажите TELEGRAM_ID, поле (username или first_name) и значение.")
        return
    try:
        telegram_id = int(parts[0])
    except ValueError:
        await message.answer("❌ TELEGRAM_ID должен быть числом.")
        return
    field = parts[1].lower()
    value = parts[2].strip()
    if field == "username":
        value = value.lstrip("@")
        ok = await update_user(telegram_id=telegram_id, username=value)
    elif field == "first_name":
        ok = await update_user(telegram_id=telegram_id, first_name=value)
    else:
        await message.answer("❌ Поле должно быть <code>username</code> или <code>first_name</code>.")
        return
    if not ok:
        await message.answer("❌ Пользователь с таким TELEGRAM_ID не найден в БД.")
        return
    await message.answer(f"✅ Данные пользователя <code>{telegram_id}</code> обновлены.")


async def cmd_broadcast(message: Message, command: CommandObject) -> None:
    """
    Отправить сообщение всем пользователям из БД.
    /broadcast Текст сообщения
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer("ℹ️ Использование: /broadcast Текст сообщения")
        return
    tg_ids = await get_all_telegram_ids()
    if not tg_ids:
        await message.answer("📭 В БД нет пользователей.")
        return
    sent = 0
    failed = 0
    for uid in tg_ids:
        try:
            await message.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            logger.warning("Broadcast to %s failed: %s", uid, e)
            failed += 1
    await message.answer(
        f"📤 Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Не доставлено: {failed}"
    )


async def cmd_send_to_user(message: Message, command: CommandObject) -> None:
    """
    Отправить сообщение конкретному пользователю по telegram_id.
    /send TELEGRAM_ID Текст сообщения
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    if not command.args or not command.args.strip():
        await message.answer(
            "ℹ️ Использование: /send TELEGRAM_ID Текст сообщения\n"
            "Пример: /send 123456789 Привет, ваш ключ готов!"
        )
        return
    parts = command.args.strip().split(maxsplit=1)
    try:
        telegram_id = int(parts[0])
    except ValueError:
        await message.answer("❌ TELEGRAM_ID должен быть числом.")
        return
    text = parts[1] if len(parts) > 1 else ""
    if not text:
        await message.answer("❌ Укажите текст сообщения.")
        return
    try:
        await message.bot.send_message(chat_id=telegram_id, text=text)
        await message.answer(f"✅ Сообщение отправлено пользователю <code>{telegram_id}</code>.")
    except Exception as e:
        await message.answer(
            f"❌ Не удалось отправить: пользователь не найден или заблокировал бота.\n"
            f"Ошибка: {e}"
        )


def _read_keys_from_file(path: Path) -> list[str]:
    """Читает ключи из файла (по одному на строку)."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


async def cmd_reset_keys(message: Message) -> None:
    """
    Меню сброса ключей: все, по файлу (с выбором файла и количества), или один ключ.
    /reset_keys — меню
    /reset_keys_count 30 [N] — сбросить N ключей из файла (30|90|180|trial), N не указано = все из файла
    /reset_key КЛЮЧ — сбросить один конкретный ключ
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Сбросить ВСЕ ключи", callback_data="rk_all"),
            ],
            [
                InlineKeyboardButton(text="📁 Сбросить по файлу", callback_data="rk_file"),
            ],
        ]
    )
    await message.answer(
        "🔑 <b>Сброс ключей</b>\n\n"
        "• <b>Сбросить ВСЕ</b> — все ключи из всех файлов снова станут доступны.\n"
        "• <b>По файлу</b> — выбрать файл (30/90/180/пробный) и сбросить все ключи из него или указать количество.\n\n"
        "Или используйте команды:\n"
        "• <code>/reset_keys_count 30</code> — сбросить все ключи из keys.txt\n"
        "• <code>/reset_keys_count 30 5</code> — сбросить 5 ключей из keys.txt\n"
        "• <code>/reset_key ключ_строка</code> — сбросить один ключ",
        reply_markup=kb,
    )


async def handle_reset_keys_callback(callback: CallbackQuery) -> None:
    """Обработчик кнопок сброса ключей."""
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return

    data = callback.data or ""

    # Меню "Сбросить ВСЕ" — запрос подтверждения
    if data == "rk_all":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да, сбросить все", callback_data="rk_all_ok"),
                ],
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data="rk_cancel"),
                ],
            ]
        )
        await callback.message.edit_text(
            "⚠️ Сбросить <b>все</b> выданные ключи во всех файлах?\n"
            "Подписки пользователей не удалятся, но привязка ключей к ним пропадёт.",
            reply_markup=kb,
        )
        await callback.answer()
        return

    if data == "rk_all_ok":
        success = await reset_all_keys()
        if success:
            await callback.message.edit_text("✅ Все ключи успешно сброшены.")
        else:
            await callback.message.edit_text("❌ Ошибка при сбросе ключей.")
        await callback.answer()
        return

    # Выбор файла
    if data == "rk_file":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📄 keys_3d.txt", callback_data="rk_f_3d"),
                    InlineKeyboardButton(text="📄 keys_7d.txt", callback_data="rk_f_7d"),
                ],
                [
                    InlineKeyboardButton(text="📄 keys.txt (30 дн.)", callback_data="rk_f_30"),
                    InlineKeyboardButton(text="📄 keys_90.txt", callback_data="rk_f_90"),
                ],
                [
                    InlineKeyboardButton(text="📄 keys_180.txt", callback_data="rk_f_180"),
                    InlineKeyboardButton(text="📄 keys_trial.txt", callback_data="rk_f_trial"),
                ],
                [InlineKeyboardButton(text="❌ Назад", callback_data="rk_cancel")],
            ]
        )
        await callback.message.edit_text(
            "📁 Выберите файл, ключи из которого нужно сбросить:",
            reply_markup=kb,
        )
        await callback.answer()
        return

    # Выбран конкретный файл — показать подтверждение
    if data in ("rk_f_30", "rk_f_90", "rk_f_180", "rk_f_trial"):
        file_key = data.replace("rk_f_", "")
        if file_key not in RESET_KEYS_FILES:
            await callback.answer()
            return
        path, label = RESET_KEYS_FILES[file_key]
        file_keys = _read_keys_from_file(path)
        used = set(await list_used_keys())
        used_from_file = [k for k in file_keys if k in used]
        count = len(used_from_file)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ Сбросить все из файла ({count} шт.)",
                        callback_data=f"rk_f_{file_key}_ok",
                    )
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="rk_cancel")],
            ]
        )
        await callback.message.edit_text(
            f"📄 <b>{label}</b>\n\n"
            f"Использовано ключей из этого файла: <b>{count}</b>\n\n"
            "Сбросить все эти ключи? Или укажите количество командой:\n"
            f"<code>/reset_keys_count {file_key} 5</code> — сбросить 5 ключей",
            reply_markup=kb,
        )
        await callback.answer()
        return

    # Подтверждение сброса по файлу
    if data.startswith("rk_f_") and data.endswith("_ok"):
        file_key = data[5:-3]  # rk_f_30_ok -> 30
        if file_key not in RESET_KEYS_FILES:
            await callback.answer()
            return
        path, label = RESET_KEYS_FILES[file_key]
        file_keys = _read_keys_from_file(path)
        used = set(await list_used_keys())
        to_reset = [k for k in file_keys if k in used]
        if not to_reset:
            await callback.message.edit_text(f"В файле {label} нет использованных ключей для сброса.")
            await callback.answer()
            return
        n = await reset_keys_from_set(to_reset, limit=None)
        await callback.message.edit_text(
            f"✅ Сброшено ключей из {label}: <b>{n}</b>"
        )
        await callback.answer()
        return

    if data == "rk_cancel":
        await callback.message.edit_text("❌ Сброс ключей отменён.")
        await callback.answer()
        return

    await callback.answer()


async def cmd_reset_keys_count(message: Message, command: CommandObject) -> None:
    """
    Сбросить ключи из указанного файла: все или N штук.
    /reset_keys_count 30       — все ключи из keys.txt
    /reset_keys_count 30 5     — 5 ключей из keys.txt
    /reset_keys_count 90|180|trial [N]
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    args = (command.args or "").strip().split()
    if not args or args[0] not in RESET_KEYS_FILES:
        await message.answer(
            "ℹ️ Использование: /reset_keys_count 30 [N]\n"
            "Файлы: <code>30</code> (keys.txt), <code>90</code>, <code>180</code>, <code>trial</code>.\n"
            "N — сколько ключей сбросить (если не указано — все из файла)."
        )
        return
    file_key = args[0]
    limit = None
    if len(args) >= 2:
        try:
            limit = int(args[1])
            if limit <= 0:
                await message.answer("❌ Количество должно быть больше 0.")
                return
        except ValueError:
            await message.answer("❌ Укажите число ключей для сброса.")
            return
    path, label = RESET_KEYS_FILES[file_key]
    file_keys = _read_keys_from_file(path)
    if not file_keys:
        await message.answer(f"⚠️ Файл {path.name} пуст или не найден.")
        return
    used = set(await list_used_keys())
    to_reset = [k for k in file_keys if k in used]
    if not to_reset:
        await message.answer(f"В файле {label} нет использованных ключей.")
        return
    n = await reset_keys_from_set(to_reset, limit=limit)
    await message.answer(f"✅ Сброшено ключей из {label}: <b>{n}</b>")


async def cmd_reset_key(message: Message, command: CommandObject) -> None:
    """
    Сбросить один ключ по точному значению.
    /reset_key ключ_строка
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    key = (command.args or "").strip()
    if not key:
        await message.answer(
            "ℹ️ Использование: /reset_key КЛЮЧ\n"
            "Пример: /reset_key abc123-ключ"
        )
        return
    n = await reset_keys_by_keys([key])
    if n == 0:
        await message.answer("❌ Такой ключ не найден в выданных подписках (или уже сброшен).")
        return
    await message.answer(f"✅ Ключ сброшен (обновлено записей: {n}).")

async def cmd_referrals(message: Message) -> None:
    """Показывает статистику рефералов."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    
    stats = await get_referral_stats()
    if not stats:
        await message.answer("📊 Пока нет рефералов.")
        return
    
    lines = ["📊 <b>Статистика рефералов</b>\n"]
    for idx, s in enumerate(stats, 1):
        username = f"@{s.get('username')}" if s.get('username') else "нет username"
        first_name = s.get('first_name') or ""
        count = s.get('referral_count', 0)
        tg_id = s.get('telegram_id')
        lines.append(
            f"{idx}. {first_name} {username}\n"
            f"   🆔 ID: <code>{tg_id}</code> | 👥 Приглашено: <b>{count}</b>"
        )
    
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_reset_trial(message: Message, command: CommandObject) -> None:
    """Возобновить возможность использовать пробный период для пользователя."""
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    
    if not command.args or not command.args.strip().isdigit():
        await message.answer(
            "ℹ️ Использование: /reset_trial TELEGRAM_ID\n"
            "Пример: <code>/reset_trial 123456789</code>"
        )
        return
    
    telegram_id = int(command.args.strip())
    ok = await reset_trial_for_user(telegram_id)
    
    if not ok:
        await message.answer("❌ Пользователь с таким Telegram ID не найден.")
        return
    
    await message.answer(f"✅ Пробный период возобновлён для пользователя <code>{telegram_id}</code>.")


async def cmd_delete_user(message: Message, command: CommandObject) -> None:
    """
    Полностью удаляет пользователя из базы данных.
    Удаляет все его подписки и сам пользователь.
    После удаления повторный переход по реферальной ссылке будет работать.
    /delete_user TELEGRAM_ID
    """
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У тебя нет доступа к этой команде.")
        return

    if not command.args or not command.args.strip().isdigit():
        await message.answer(
            "ℹ️ Использование: /delete_user TELEGRAM_ID\n"
            "Пример: <code>/delete_user 123456789</code>\n\n"
            "⚠️ Внимание: это полностью удалит пользователя из БД, включая все его подписки.\n"
            "После удаления пользователь сможет зарегистрироваться заново по реферальной ссылке."
        )
        return

    telegram_id = int(command.args.strip())
    
    # Проверяем, существует ли пользователь перед удалением
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Пользователь с таким Telegram ID не найден в базе данных.")
        return
    
    ok = await delete_user_completely(telegram_id)

    if not ok:
        await message.answer("❌ Не удалось удалить пользователя.")
        return

    await message.answer(
        f"✅ Пользователь <code>{telegram_id}</code> полностью удалён из базы данных.\n\n"
        "🗑️ Удалено:\n"
        "• Все подписки пользователя\n"
        "• Запись пользователя из БД\n"
        "• Обновлён счётчик рефералов у реферера (если был)\n\n"
        "🔄 Теперь пользователь может зарегистрироваться заново по реферальной ссылке."
    )


async def show_referral_system(message: Message) -> None:
    """Показывает реферальную систему с индивидуальной ссылкой."""
    if not await ensure_subscribed(message):
        return

    # Получаем username бота для реферальной ссылки
    try:
        bot_info = await message.bot.get_me()
        bot_username = bot_info.username or "vpnkometa_bot"
    except Exception:
        bot_username = "vpnkometa_bot"
    
    referral_link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    
    # Получаем информацию о пользователе для статистики рефералов
    user = await get_user_by_telegram_id(message.from_user.id)
    referral_count = user.get("referral_count", 0) if user else 0
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Копировать ссылку",
                    callback_data="copy_referral_link",
                )
            ],
        ]
    )
    
    text = (
        "🎁 <b>Реферальная система</b>\n\n"
        "💎 <b>Как получить 3 дня подписки?</b>\n"
        "1️⃣ Скопируй свою реферальную ссылку\n"
        "2️⃣ Отправь её другу\n"
        "3️⃣ Друг должен перейти по ссылке и подписаться на канал\n"
        "4️⃣ После успешной регистрации друга тебе автоматически начисляется <b>3 дня</b> подписки!\n\n"
        "📊 <b>Твоя статистика:</b>\n"
        f"👥 Приглашено друзей: <b>{referral_count}</b>\n\n"
        f"🔗 <b>Твоя реферальная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        "💡 <i>Нажми кнопку ниже, чтобы скопировать ссылку</i>"
    )
    
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def handle_copy_referral_link(callback: CallbackQuery) -> None:
    """Обработчик кнопки копирования реферальной ссылки."""
    if not callback.from_user:
        await callback.answer()
        return
    
    # Получаем username бота для реферальной ссылки
    try:
        bot_info = await callback.message.bot.get_me()
        bot_username = bot_info.username or "vpnkometa_bot"
    except Exception:
        bot_username = "vpnkometa_bot"
    
    referral_link = f"https://t.me/{bot_username}?start={callback.from_user.id}"
    
    # В Telegram нельзя программно скопировать текст, но можно показать ссылку отдельным сообщением
    await callback.message.answer(
        f"🔗 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{referral_link}</code>\n\n"
        "💡 Нажми на ссылку выше, чтобы скопировать её",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("✅ Ссылка отправлена отдельным сообщением для удобного копирования!")


async def show_support(message: Message) -> None:
    if not await ensure_subscribed(message):
        return

    await message.answer(
        "🆘 <b>Поддержка</b>\n\n"
        "Если вы столкнулись с проблемами, сбоями VPN или есть вопросы — пишите:\n"
        "👤 @r5net или 👤 @juckmyass"
    )


async def on_message_text(message: Message) -> None:
    if not await ensure_subscribed(message):
        return

    # Поддерживаем как варианты без эмодзи, так и с ними (на всякий случай)
    if message.text in ("Профиль", "👤 Профиль"):
        await show_profile(message)
    elif message.text in ("Купить подписку", "💳 Купить подписку"):
        await show_buy_info(message)
    elif message.text in ("Реферальная система", "🎁 Реферальная система"):
        await show_referral_system(message)
    elif message.text in ("Поддержка", "🆘 Поддержка"):
        await show_support(message)
    else:
        await message.answer(
            "❓ Неизвестная команда.\n"
            "Используйте кнопки меню или команду /help."
        )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment variables.")

    await init_db()

    # Запускаем фоновую задачу для проверки просроченных подписок
    asyncio.create_task(periodic_expiration_check())

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(show_referral_system, Command("referral"))
    dp.message.register(show_profile, Command("profile"))
    dp.message.register(show_buy_info, Command("buy"))
    dp.message.register(cmd_admin_panel, Command("admin"))
    dp.message.register(cmd_grant_subscription, Command("grant"))
    dp.message.register(cmd_revoke_subscription, Command("revoke"))
    dp.message.register(cmd_referrals, Command("referrals"))
    dp.message.register(cmd_reset_trial, Command("reset_trial"))
    dp.message.register(cmd_reset_keys, Command("reset_keys"))
    dp.message.register(cmd_reset_keys_count, Command("reset_keys_count"))
    dp.message.register(cmd_reset_key, Command("reset_key"))
    dp.message.register(cmd_admin_add_user, Command("admin_add"))
    dp.message.register(cmd_admin_setkey, Command("admin_setkey"))
    dp.message.register(cmd_admin_setuser, Command("admin_setuser"))
    dp.message.register(cmd_delete_user, Command("delete_user"))
    dp.message.register(cmd_broadcast, Command("broadcast"))
    dp.message.register(cmd_send_to_user, Command("send"))

    dp.callback_query.register(handle_subscription_duration_callback, F.data.startswith("sub_"))
    dp.callback_query.register(handle_replace_key_callback, F.data == "replace_key")
    dp.callback_query.register(handle_pay_callback, F.data.startswith("pay_"))
    dp.callback_query.register(handle_buy_close_callback, F.data == "buy_close")
    dp.callback_query.register(handle_check_subscription_callback, F.data.startswith("check_subscription"))
    dp.callback_query.register(handle_reset_keys_callback, F.data.startswith("rk_"))
    dp.callback_query.register(handle_grant_callback, F.data.startswith("grant_"))
    dp.callback_query.register(handle_copy_referral_link, F.data == "copy_referral_link")
    dp.callback_query.register(
        handle_admin_payment_callback,
        (F.data.startswith("admin_confirm_") | F.data.startswith("admin_decline_")),
    )

    async def admin_text_handler(message: Message) -> None:
        if is_admin(message.from_user.id) and message.text and message.text.strip().isdigit():
            # Если для этого админа ожидаем ввод количества дней для /grant
            pending_store = getattr(handle_grant_callback, "_pending", None)
            if pending_store is not None:
                pending = pending_store.get(message.from_user.id)
                if pending and isinstance(pending, tuple) and pending[0] == "await_days":
                    days = int(message.text.strip())
                    if days <= 0 or days > 365:
                        await message.answer("❌ Введите количество дней от 1 до 365.")
                        return

                    # Для API-выдачи keys_file не используется, поэтому передаём None и понятное имя
                    pending_store[message.from_user.id] = (days, None, "панель API")
                    days_text = f"{days} дней"
                    await message.answer(
                        f"📅 Выбрано: <b>{days_text}</b>\n\n"
                        "Теперь отправьте Telegram ID пользователя одним сообщением (только число)."
                    )
                    return

            await handle_grant_user_id(message)
        else:
            await on_message_text(message)
    
    dp.message.register(admin_text_handler, F.text)

    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
