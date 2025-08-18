from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx


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

    async def __aenter__(self) -> "X3UIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        if not (self.username and self.password):
            return
        try:
            resp = await self._client.post(
                "/login", data={"username": self.username, "password": self.password}
            )
            resp.raise_for_status()
        except Exception:
            try:
                resp = await self._client.post(
                    "/login", json={"username": self.username, "password": self.password}
                )
                resp.raise_for_status()
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
                "id": inbound_id,
                "settings": {
                    "clients": [
                        {
                            "id": client_uuid,
                            "email": email_note,
                            "enable": True,
                            "limitIp": 0,
                            "totalGB": total_gb_bytes or 0,
                            "expiryTime": expiry_ms,
                        }
                    ]
                },
            },
            {
                "inboundId": inbound_id,
                "client": {
                    "id": client_uuid,
                    "email": email_note,
                    "enable": True,
                    "limitIp": 0,
                    "totalGB": total_gb_bytes or 0,
                    "expiryTime": expiry_ms,
                },
            },
        ]

        endpoints = [
            "/panel/inbound/addClient",
            "/xui/inbound/addClient",
            "/panel/api/inbounds/addClient",
            "/api/inbounds/addClient",
        ]

        for endpoint in endpoints:
            for payload in payload_variants:
                try:
                    resp = await self._client.post(endpoint, json=payload)
                    if resp.status_code == 200 and "success" in resp.text.lower():
                        return X3UICreateClientResult(
                            uuid=client_uuid, note=email_note, config_url=None
                        )
                except Exception:
                    continue

        return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=None)
