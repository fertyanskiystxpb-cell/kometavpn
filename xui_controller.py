

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
VLESS_ADDRESS = os.getenv("VLESS_ADDRESS", "193.109.69.12")
VLESS_PORT = int(os.getenv("VLESS_PORT", "443"))
VLESS_SNI = os.getenv("VLESS_SNI", "swcdn.apple.com")
VLESS_PBK = os.getenv("VLESS_PBK", "1CIbi5YWlOdS-cE-mo0n-mPZq5_evzh_-hA3ngLiBEY")
VLESS_SID = os.getenv("VLESS_SID", "fd464a9f7c5243")
VLESS_FLOW = os.getenv("VLESS_FLOW", "xtls-rprx-vision")
VLESS_SECURITY = os.getenv("VLESS_SECURITY", "reality")
VLESS_FP = os.getenv("VLESS_FP", "chrome") 
VLESS_SPX = os.getenv("VLESS_SPX", "%2F") 


def generate_vless_link(uuid: str, remark: Optional[str] = None) -> str:
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
                    if "success" in body.lower() or resp.status == 200:
                        logger.info("XUI login: status=success")
                        return True
                    logger.warning("XUI login: status=unknown_response, body=%s", body[:200])
                    return True
                logger.info("XUI login: status=failed, http_status=%s", resp.status)
                return False
        except Exception as e:
            logger.exception("XUI login: status=error, error=%s", e)
            return False

    async def _get_client_uuid_by_email(self, email: str) -> Optional[str]:
        session = await self._get_session()
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
        from datetime import datetime, timedelta

        if client_uuid is None:
            client_uuid = str(uuid_lib.uuid4())

        if duration_days <= 0:
            expiry_time_ms = 0 
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

        await asyncio.sleep(0.5)  
        real_uuid = await self._get_client_uuid_by_email(email)
        if real_uuid:
            logger.info("XUI addClient success for tg_%s, real uuid=%s", telegram_id, real_uuid)
            return real_uuid
        logger.warning("XUI addClient: не удалось получить UUID по email %s, используем переданный", email)
        return client_uuid

    async def _get_inbound_obj(self) -> Optional[dict]:
        """Возвращает полный объект inbound из панели (для обновления срока клиента)."""
        session = await self._get_session()
        for url in (
            urljoin(self.base_url, f"panel/api/inbounds/get/{self.inbound_id}"),
            urljoin(self.base_url, "panel/api/inbounds/get") + f"?id={self.inbound_id}",
        ):
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if data and data.get("success") is not False:
                        return data.get("obj") or data
            except Exception:
                continue
        return None

    async def extend_client_expiry_by_days(self, client_uuid: str, days: int) -> bool:
        from datetime import datetime, timedelta

        obj = await self._get_inbound_obj()
        if not obj:
            logger.warning("XUI extend_client_expiry: inbound not found")
            return False
        settings_raw = obj.get("settings")
        if not settings_raw:
            return False
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
        clients = settings.get("clients") or []
        found = None
        for c in clients:
            if c.get("id") == client_uuid:
                found = c
                break
        if not found:
            logger.warning("XUI extend_client_expiry: client uuid=%s not found", client_uuid)
            return False
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        current_expiry = found.get("expiryTime") or 0
        if current_expiry <= 0:
            new_expiry_ms = int((datetime.utcnow() + timedelta(days=days)).timestamp() * 1000)
        else:
            try:
                current_dt = datetime.utcfromtimestamp(current_expiry / 1000.0)
                new_expiry_ms = int((current_dt + timedelta(days=days)).timestamp() * 1000)
            except Exception:
                new_expiry_ms = int((datetime.utcnow() + timedelta(days=days)).timestamp() * 1000)
        found["expiryTime"] = new_expiry_ms
        settings_str = json.dumps(settings)
        update_body = {"id": self.inbound_id, "settings": settings_str}
        session = await self._get_session()
        url = urljoin(self.base_url, "panel/api/inbounds/update/" + str(self.inbound_id))
        try:
            async with session.post(url, json=update_body) as resp:
                text = await resp.text()
                if resp.status == 200:
                    logger.info("XUI extend_client_expiry: uuid=%s, days=%s, status=success", client_uuid, days)
                    return True
                logger.warning("XUI extend_client_expiry: status=failed, http=%s body=%s", resp.status, text[:200])
                return False
        except Exception as e:
            logger.exception("XUI extend_client_expiry: status=error, error=%s", e)
            return False

    async def delete_user(self, client_uuid: str) -> bool:
        session = await self._get_session()
        path = f"panel/api/inbounds/{self.inbound_id}/delClient/{client_uuid}"
        url = urljoin(self.base_url, path)
        try:
            async with session.post(url) as resp:
                text = await resp.text()
                if resp.status == 200:
                    logger.info("XUI delClient: uuid=%s, status=success", client_uuid)
                    return True
                logger.info("XUI delClient: uuid=%s, status=failed, http=%s body=%s", client_uuid, resp.status, text[:200])
                return False
        except Exception as e:
            logger.exception("XUI delete_user: uuid=%s, status=error, error=%s", client_uuid, e)
            return False

    async def ensure_logged_in(self) -> bool:
        return await self.login()
