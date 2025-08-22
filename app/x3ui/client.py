from __future__ import annotations

import uuid
from dataclasses import dataclass
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from urllib.parse import urlsplit
import json


@dataclass
class X3UICreateClientResult:
    uuid: str
    note: str
    config_url: Optional[str] = None


class X3UIClient:
    def __init__(self, base_url: str, username: Optional[str], password: Optional[str]):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15)
        self._log = logging.getLogger("x3ui")
        try:
            parsed = urlsplit(self.base_url)
            self._base_path = parsed.path.strip("/")
        except Exception:
            self._base_path = ""

    async def __aenter__(self) -> "X3UIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.aclose()

    def _candidates(self, subpaths: list[str]) -> list[str]:
        candidates: list[str] = []
        prefixes = ["", self._base_path] if self._base_path else [""]
        for prefix in prefixes:
            for sp in subpaths:
                sp_clean = sp.lstrip("/")
                if prefix:
                    candidates.append(f"/{prefix}/{sp_clean}")
                else:
                    candidates.append(f"/{sp_clean}")
        # de-duplicate preserving order
        seen = set()
        uniq: list[str] = []
        for p in candidates:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    async def login(self) -> None:
        if not (self.username and self.password):
            return
        paths = self._candidates(["login"])  # e.g., /login and /x3ui/login
        for p in paths:
            try:
                resp = await self._client.post(p, data={"username": self.username, "password": self.password})
                if 200 <= resp.status_code < 400:
                    return
            except Exception:
                pass
            try:
                resp = await self._client.post(p, json={"username": self.username, "password": self.password})
                if 200 <= resp.status_code < 400:
                    return
            except Exception:
                pass

    async def add_client(
        self,
        inbound_id: int,
        days: int,
        traffic_gb: Optional[int],
        email_note: str,
    ) -> X3UICreateClientResult:
        await self.login()
        client_uuid = str(uuid.uuid4())
        expiry_ms = int((datetime.utcnow() + timedelta(days=days)).timestamp() * 1000)
        total_gb_bytes = None
        if traffic_gb is not None:
            total_gb_bytes = int(traffic_gb) * 1024 * 1024 * 1024

        payload_variants = [
            {
                "inboundId": inbound_id,
                "client": {
                    "id": client_uuid,
                    "email": email_note,
                    "subId": email_note,
                    "enable": True,
                    "limitIp": 0,
                    "totalGB": total_gb_bytes or 0,
                    "expiryTime": expiry_ms,
                },
            },
            {
                "id": inbound_id,
                "settings": {
                    "clients": [
                        {
                            "id": client_uuid,
                            "email": email_note,
                            "subId": email_note,
                            "enable": True,
                            "limitIp": 0,
                            "totalGB": total_gb_bytes or 0,
                            "expiryTime": expiry_ms,
                        }
                    ]
                },
            },
            # Некоторые форки ожидают строковый settings
            {
                "id": inbound_id,
                "settings": json.dumps({
                    "clients": [
                        {
                            "id": client_uuid,
                            "email": email_note,
                            "subId": email_note,
                            "enable": True,
                            "limitIp": 0,
                            "totalGB": total_gb_bytes or 0,
                            "expiryTime": expiry_ms,
                        }
                    ]
                })
            },
        ]

        subpaths = [
            "panel/api/inbounds/addClient",  # 3x-ui v2.6.2
            "api/inbounds/addClient",
            "panel/inbound/addClient",
            "xui/inbound/addClient",
        ]
        endpoints = self._candidates(subpaths)

        for endpoint in endpoints:
            for payload in payload_variants:
                try:
                    self._log.debug("x3-ui addClient try %s payload=%s", endpoint, "client" if "client" in payload else "settings")
                    resp = await self._client.post(endpoint, json=payload)
                    body = resp.text
                    self._log.debug("x3-ui addClient %s -> %s %s", endpoint, resp.status_code, body[:400])
                    if resp.status_code == 200:
                        # Некоторые сборки возвращают пустое тело при успехе
                        if not body or not body.strip():
                            return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=None)
                        lower = body.lower()
                        if "success" in lower or '"ok":true' in lower or '"status":"success"' in lower:
                            return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=None)
                except Exception as e:
                    self._log.warning("x3-ui addClient error on %s: %s", endpoint, e)
                    continue

        return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=None)

    async def get_inbound(self, inbound_id: int) -> Optional[dict]:
        await self.login()
        subpaths = [
            f"panel/api/inbounds/get/{inbound_id}",
            "panel/api/inbounds/list",
            f"api/inbounds/get/{inbound_id}",
            "api/inbounds/list",
        ]
        paths = self._candidates(subpaths)
        for ep in paths:
            try:
                resp = await self._client.get(ep)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        obj = data.get("obj") if isinstance(data.get("obj"), (dict, list)) else None
                        if obj is None:
                            obj = data.get("data") if isinstance(data.get("data"), (dict, list)) else None
                        if isinstance(obj, dict):
                            if obj.get("id") == inbound_id or obj.get("port"):
                                return obj
                        if isinstance(obj, list):
                            for it in obj:
                                if it.get("id") == inbound_id:
                                    return it
            except Exception as e:
                self._log.debug("get_inbound %s error: %s", ep, e)
                continue
        return None

    def build_vless_url(self, inbound: dict, client_uuid: str, note: str) -> Optional[str]:
        try:
            protocol = (inbound.get("protocol") or "vless").lower()
            if protocol != "vless":
                return None
            port = inbound.get("port")
            stream = inbound.get("streamSettings", {})
            network = (stream.get("network") or "tcp").lower()
            security = (stream.get("security") or "none").lower()

            host = None
            path = None
            sni = None

            if network == "ws":
                ws = stream.get("wsSettings", {})
                path = ws.get("path") or "/"
                headers = ws.get("headers") or {}
                host = headers.get("Host") or headers.get("host")
            if security in ("tls", "reality"):
                tls = stream.get("tlsSettings") or stream.get("realitySettings") or {}
                sni = tls.get("serverName") or (tls.get("serverNames")[0] if isinstance(tls.get("serverNames"), list) and tls.get("serverNames") else None)

            # Derive server host from PUBLIC_BASE_URL if not present
            from ..config import settings as app_settings
            public_host = None
            if app_settings.public_base_url:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(app_settings.public_base_url)
                    if parsed.hostname:
                        public_host = parsed.hostname
                except Exception:
                    pass

            # Build query params
            params: dict[str, str] = {"encryption": "none"}

            if security == "reality":
                params["security"] = "reality"
                reality = stream.get("realitySettings", {})
                pbk = reality.get("publicKey")
                sid = None
                short_id = reality.get("shortId")
                if isinstance(short_id, list) and short_id:
                    sid = short_id[0]
                elif isinstance(short_id, str):
                    sid = short_id
                spx = reality.get("spiderX") or "/"
                fp = reality.get("fingerprint") or "chrome"
                if pbk:
                    params["pbk"] = pbk
                if sid:
                    params["sid"] = sid
                if sni:
                    params["sni"] = sni
                if fp:
                    params["fp"] = fp
                if spx:
                    params["spx"] = spx
                # type for tcp
                params["type"] = network
            else:
                if security and security != "none":
                    params["security"] = security
                if network == "ws":
                    params["type"] = "ws"
                    if path:
                        params["path"] = path
                    if host:
                        params["host"] = host

            # Choose server host
            server = public_host or host or sni
            if not server or not port:
                return None

            from urllib.parse import urlencode, quote
            qs = urlencode(params)
            tag = quote(note)
            return f"vless://{client_uuid}@{server}:{port}?{qs}#{tag}"
        except Exception:
            return None