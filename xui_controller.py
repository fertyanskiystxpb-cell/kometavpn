"""
Контроллер API панели 3x-ui для управления клиентами (VLESS/Reality).
Использует aiohttp с сохранением сессии (куки) и передаёт settings как JSON-строку.
"""

import asyncio
import json
import logging
import os
import ssl
import uuid as uuid_lib
from typing import Optional
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)

# Параметры Reality для генерации vless:// (как в панели)
VLESS_ADDRESS = os.getenv("VLESS_ADDRESS", "151.241.215.71")
VLESS_PORT = int(os.getenv("VLESS_PORT", "443"))
VLESS_SNI = os.getenv("VLESS_SNI", "swcdn.apple.com")
VLESS_PBK = os.getenv("VLESS_PBK", "V6zkalrAPp-Hc6m6tSw4OMclfaxOJSdGMxNwVU3kOgA")
# Short ID: в ссылке используется один короткий sid (первый до запятой, если несколько)
VLESS_SID = os.getenv("VLESS_SID", "3db9a12c")
VLESS_FLOW = os.getenv("VLESS_FLOW", "xtls-rprx-vision")
VLESS_SECURITY = os.getenv("VLESS_SECURITY", "reality")
VLESS_FP = os.getenv("VLESS_FP", "chrome")  # fingerprint
VLESS_SPX = os.getenv("VLESS_SPX", "%2F")   # spx (path), уже закодировано или /


def generate_vless_link(uuid: str, remark: Optional[str] = None) -> str:
    """
    Собирает vless:// как в панели 3x-ui (Reality).
    Порядок и набор параметров должны совпадать с панелью.
    remark: комментарий после # (например Kometa-tg_8516740130).
    """
    # Один короткий sid для ссылки (панель использует один)
    sid = VLESS_SID.split(",")[0].strip() if VLESS_SID else ""
    params = [
        "type=tcp",
        "encryption=none",
        f"security={VLESS_SECURITY}",
        f"pbk={VLESS_PBK}" if VLESS_PBK else "",
        f"fp={VLESS_FP}" if VLESS_FP else "",
        f"sni={VLESS_SNI}",
        f"sid={sid}" if sid else "",
        f"spx={VLESS_SPX}" if VLESS_SPX else "",
        f"flow={VLESS_FLOW}",
    ]
    query = "&".join(p for p in params if p)
    fragment = f"#{remark}" if remark else "#KometaVPN"
    return f"vless://{uuid}@{VLESS_ADDRESS}:{VLESS_PORT}?{query}{fragment}"


class XUIController:
    """
    Управление панелью 3x-ui: авторизация, добавление/удаление клиентов.
    Сессия aiohttp сохраняет куки между запросами.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        inbound_id: int = 1,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.inbound_id = inbound_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=aiohttp.TCPConnector(ssl=self._ssl_context),
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def login(self) -> bool:
        """
        Авторизация в панели. Куки сохраняются в сессии.
        """
        session = await self._get_session()
        url = urljoin(self.base_url, "login")
        payload = {
            "username": self.username,
            "password": self.password,
        }
        try:
            async with session.post(url, data=payload) as resp:
                if resp.status == 200:
                    body = await resp.text()
                    # Успех: часто редирект или {"success": true}
                    if "success" in body.lower() or resp.status == 200:
                        logger.info("XUI login successful")
                        return True
                    # Некоторые панели возвращают 200 с ошибкой в теле
                    logger.warning("XUI login response: %s", body[:200])
                    return True  # Всё равно считаем успехом при 200
                logger.warning("XUI login failed: status %s", resp.status)
                return False
        except Exception as e:
            logger.exception("XUI login error: %s", e)
            return False

    async def _get_client_uuid_by_email(self, email: str) -> Optional[str]:
        """
        Получает inbound из панели и находит UUID клиента по email.
        Панель может создавать своего UUID при addClient, поэтому реальный UUID берём отсюда.
        """
        session = await self._get_session()
        # Варианты: GET .../get/1 или GET .../get?id=1
        urls_to_try = [
            urljoin(self.base_url, f"panel/api/inbounds/get/{self.inbound_id}"),
            urljoin(self.base_url, "panel/api/inbounds/get") + f"?id={self.inbound_id}",
        ]
        obj = None
        try:
            for url in urls_to_try:
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        if data and data.get("success") is not False:
                            obj = data.get("obj") or data
                            break
                except Exception:
                    continue
            if not obj:
                return None
            settings_raw = obj.get("settings")
            if not settings_raw:
                return None
            if isinstance(settings_raw, str):
                settings = json.loads(settings_raw)
            else:
                settings = settings_raw
            clients = settings.get("clients") or []
            for c in clients:
                if c.get("email") == email:
                    return c.get("id")
            return None
        except Exception as e:
            logger.warning("XUI _get_client_uuid_by_email error: %s", e)
            return None

    async def add_user(
        self,
        telegram_id: int,
        duration_days: int,
        client_uuid: Optional[str] = None,
    ) -> Optional[str]:
        """
        Создаёт клиента во inbound. Email = tg_{telegram_id}.
        expiryTime в миллисекундах Unix.
        Возвращает реальный UUID клиента из панели (после создания запрашиваем get и берём id по email).
        """
        from datetime import datetime, timedelta

        if client_uuid is None:
            client_uuid = str(uuid_lib.uuid4())

        if duration_days <= 0:
            expiry_time_ms = 0  # без срока в 3x-ui
        else:
            expire_dt = datetime.utcnow() + timedelta(days=duration_days)
            expiry_time_ms = int(expire_dt.timestamp() * 1000)

        email = f"tg_{telegram_id}"
        clients_payload = [
            {
                "id": client_uuid,
                "flow": VLESS_FLOW,
                "email": email,
                "limitIp": 0,
                "totalGB": 0,
                "expiryTime": expiry_time_ms,
                "enable": True,
                "tgId": str(telegram_id),
                "subId": "",
                "comment": email,
                "reset": 0,
            }
        ]
        settings_dict = {"clients": clients_payload}
        settings_str = json.dumps(settings_dict)

        body = {
            "id": self.inbound_id,
            "settings": settings_str,
        }

        session = await self._get_session()
        url = urljoin(self.base_url, "panel/api/inbounds/addClient")
        try:
            async with session.post(url, json=body) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning("XUI addClient failed: status=%s body=%s", resp.status, text[:300])
                    return None
        except Exception as e:
            logger.exception("XUI add_user error: %s", e)
            return None

        # Панель может присвоить клиенту свой UUID — получаем реальный из панели
        await asyncio.sleep(0.5)  # даём панели обновиться
        real_uuid = await self._get_client_uuid_by_email(email)
        if real_uuid:
            logger.info("XUI addClient success for tg_%s, real uuid=%s", telegram_id, real_uuid)
            return real_uuid
        logger.warning("XUI addClient: не удалось получить UUID по email %s, используем переданный", email)
        return client_uuid

    async def delete_user(self, client_uuid: str) -> bool:
        """
        Удаляет клиента из inbound через /panel/api/inbounds/{id}/delClient/{uuid}
        """
        session = await self._get_session()
        path = f"panel/api/inbounds/{self.inbound_id}/delClient/{client_uuid}"
        url = urljoin(self.base_url, path)
        try:
            async with session.post(url) as resp:
                text = await resp.text()
                if resp.status == 200:
                    logger.info("XUI delClient success for uuid=%s", client_uuid)
                    return True
                logger.warning("XUI delClient failed: status=%s body=%s", resp.status, text[:200])
                return False
        except Exception as e:
            logger.exception("XUI delete_user error: %s", e)
            return False

    async def ensure_logged_in(self) -> bool:
        """Вызывает login() при необходимости (можно расширить проверку куки)."""
        return await self.login()
