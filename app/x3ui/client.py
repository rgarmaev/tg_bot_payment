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
        from ..config import settings as app_settings
        verify = app_settings.x3ui_verify_tls
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15, follow_redirects=True, verify=verify)
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
                full_url = f"{self.base_url}{p}"
                resp = await self._client.post(full_url, data={"username": self.username, "password": self.password})
                if 200 <= resp.status_code < 400:
                    self._log.info("Login successful via %s", full_url)
                    return
            except Exception as e:
                self._log.debug("Login failed via %s: %s", full_url, e)
                pass
            try:
                full_url = f"{self.base_url}{p}"
                resp = await self._client.post(full_url, json={"username": self.username, "password": self.password})
                if 200 <= resp.status_code < 400:
                    self._log.info("Login successful via %s", full_url)
                    return
            except Exception as e:
                self._log.debug("Login failed via %s: %s", full_url, e)
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

        self._log.info("Adding client: inbound_id=%s, days=%s, traffic_gb=%s, email=%s, uuid=%s", 
                      inbound_id, days, traffic_gb, email_note, client_uuid)

        # Correct format according to 3x-ui API documentation
        payload = {
            "inboundId": inbound_id,
            "client": {
                "id": client_uuid,
                "email": email_note,
                "enable": True,
                "limitIp": 0,
                "totalGB": total_gb_bytes or 0,
                "expiryTime": expiry_ms,
            },
        }

        # Try the correct endpoint first
        endpoint = f"{self.base_url}/panel/api/inbounds/addClient"
        try:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            self._log.info("x3-ui addClient trying %s with correct payload", endpoint)
            self._log.info("Payload: %s", json.dumps(payload, indent=2))
            
            resp = await self._client.post(endpoint, json=payload, headers=headers)
            body = resp.text
            self._log.info("x3-ui addClient %s -> %s %s", endpoint, resp.status_code, body[:400])
            self._log.info("Response headers: %s", dict(resp.headers))
            
            if resp.status_code == 200:
                # Check if response indicates success
                if not body or not body.strip():
                    self._log.info("Empty response body, treating as success")
                    inbound = await self.get_inbound(inbound_id)
                    config_url = self.build_vless_url(inbound or {}, client_uuid, email_note)
                    self._log.info("Generated config_url: %s", config_url)
                    return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=config_url)
                
                lower = body.lower()
                if "success" in lower or '"ok":true' in lower or '"status":"success"' in lower:
                    self._log.info("Success response detected")
                    inbound = await self.get_inbound(inbound_id)
                    config_url = self.build_vless_url(inbound or {}, client_uuid, email_note)
                    self._log.info("Generated config_url: %s", config_url)
                    return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=config_url)
                else:
                    self._log.warning("Response indicates failure: %s", body)
            else:
                self._log.warning("HTTP %s response: %s", resp.status_code, body)
        except Exception as e:
            self._log.warning("x3-ui addClient error on %s: %s", endpoint, e)

        # Fallback: try alternative endpoints with different payload formats
        fallback_payloads = [
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
        ]

        fallback_endpoints = [
            f"{self.base_url}/api/inbounds/addClient",
            f"{self.base_url}/panel/inbound/addClient",
            f"{self.base_url}/xui/inbound/addClient",
        ]

        for endpoint in fallback_endpoints:
            for payload in fallback_payloads:
                try:
                    headers = {"Accept": "application/json", "Content-Type": "application/json"}
                    self._log.info("x3-ui addClient fallback try %s", endpoint)
                    self._log.info("Payload: %s", json.dumps(payload, indent=2))
                    
                    resp = await self._client.post(endpoint, json=payload, headers=headers)
                    body = resp.text
                    self._log.info("x3-ui addClient %s -> %s %s", endpoint, resp.status_code, body[:400])
                    
                    if resp.status_code == 200:
                        lower = body.lower()
                        if "success" in lower or '"ok":true' in lower or '"status":"success"' in lower:
                            self._log.info("Success response detected")
                            inbound = await self.get_inbound(inbound_id)
                            config_url = self.build_vless_url(inbound or {}, client_uuid, email_note)
                            self._log.info("Generated config_url: %s", config_url)
                            return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=config_url)
                        else:
                            self._log.warning("Response indicates failure: %s", body)
                    else:
                        self._log.warning("HTTP %s response: %s", resp.status_code, body)
                except Exception as e:
                    self._log.warning("x3-ui addClient fallback error on %s: %s", endpoint, e)
                    continue

        self._log.error("Failed to add client after trying all endpoints")
        # Even if client creation failed, try to generate config URL for manual creation
        try:
            inbound = await self.get_inbound(inbound_id)
            config_url = self.build_vless_url(inbound or {}, client_uuid, email_note)
            self._log.info("Generated config_url despite client creation failure: %s", config_url)
            return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=config_url)
        except Exception as e:
            self._log.error("Failed to generate config URL: %s", e)
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
                full_url = f"{self.base_url}{ep}"
                resp = await self._client.get(full_url)
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
            self._log.debug("build_vless_url called with inbound keys: %s", list(inbound.keys()))
            protocol = (inbound.get("protocol") or "vless").lower()
            self._log.debug("Protocol: %s", protocol)
            if protocol != "vless":
                self._log.debug("Protocol is not vless, returning None")
                return None
            port = inbound.get("port")
            self._log.debug("Port: %s", port)
            stream_raw = inbound.get("streamSettings", {})
            self._log.debug("StreamSettings raw: %s", stream_raw[:200] if isinstance(stream_raw, str) else stream_raw)
            stream = stream_raw if isinstance(stream_raw, dict) else {}
            try:
                if isinstance(stream_raw, str):
                    import json as _json
                    stream = _json.loads(stream_raw)
                    self._log.debug("Parsed stream: %s", stream)
            except Exception as e:
                self._log.debug("Failed to parse streamSettings: %s", e)
                stream = {}
            network = (stream.get("network") or "tcp").lower()
            security = (stream.get("security") or "none").lower()
            self._log.debug("Network: %s, Security: %s", network, security)

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
                sni = tls.get("serverName")

            # Derive server host from PUBLIC_BASE_URL or X3UI_BASE_URL if not present
            from ..config import settings as app_settings
            public_host = None
            if app_settings.public_base_url:
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(app_settings.public_base_url)
                    if parsed.hostname:
                        public_host = parsed.hostname
                        self._log.debug("Public host from PUBLIC_BASE_URL: %s", public_host)
                except Exception as e:
                    self._log.debug("Failed to parse PUBLIC_BASE_URL: %s", e)
                    pass
            base_host = None
            try:
                from urllib.parse import urlparse as _urlparse
                parsed_base = _urlparse(self.base_url)
                if parsed_base.hostname:
                    base_host = parsed_base.hostname
                    self._log.debug("Base host from X3UI_BASE_URL: %s", base_host)
            except Exception as e:
                self._log.debug("Failed to parse X3UI_BASE_URL: %s", e)
                base_host = None

            # Build query params
            params: dict[str, str] = {"encryption": "none"}

            if security == "reality":
                self._log.debug("Building Reality protocol params")
                params["security"] = "reality"
                reality = stream.get("realitySettings", {})
                # Handle both dict and string formats
                if isinstance(reality, str):
                    try:
                        import json as _json
                        reality = _json.loads(reality)
                        self._log.debug("Parsed reality from string: %s", reality)
                    except Exception as e:
                        self._log.debug("Failed to parse reality string: %s", e)
                        reality = {}
                reality_inner = reality.get("settings", {}) if isinstance(reality.get("settings"), dict) else {}
                # public key
                pbk = reality_inner.get("publicKey") or reality.get("publicKey")
                self._log.debug("Public key: %s", pbk)
                # short id(s)
                sid = None
                if isinstance(reality.get("shortIds"), list) and reality.get("shortIds"):
                    sid = reality.get("shortIds")[0]
                elif isinstance(reality.get("shortId"), list) and reality.get("shortId"):
                    sid = reality.get("shortId")[0]
                elif isinstance(reality.get("shortId"), str):
                    sid = reality.get("shortId")
                self._log.debug("Short ID: %s", sid)
                # sni/serverName
                sni_candidate = reality_inner.get("serverName") or sni
                self._log.debug("SNI candidate: %s", sni_candidate)
                # spiderX and fingerprint
                spx = reality_inner.get("spiderX") or reality.get("spiderX") or "/"
                fp = reality_inner.get("fingerprint") or reality.get("fingerprint") or "chrome"
                self._log.debug("SpiderX: %s, Fingerprint: %s", spx, fp)
                if pbk:
                    params["pbk"] = pbk
                if sid:
                    params["sid"] = sid
                if sni_candidate:
                    params["sni"] = sni_candidate
                if fp:
                    params["fp"] = fp
                if spx:
                    params["spx"] = spx
                # network type (tcp/ws)
                params["type"] = network
                self._log.debug("Reality params: %s", params)
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
            server = public_host or host or sni or base_host
            self._log.debug("Final server host: %s (public_host=%s, host=%s, sni=%s, base_host=%s)", 
                          server, public_host, host, sni, base_host)
            if not server or not port:
                self._log.debug("Missing server (%s) or port (%s), returning None", server, port)
                return None

            from urllib.parse import urlencode, quote
            qs = urlencode(params)
            tag = quote(note)
            result = f"vless://{client_uuid}@{server}:{port}?{qs}#{tag}"
            self._log.debug("Generated VLESS URL: %s", result)
            return result
        except Exception as e:
            self._log.debug("Exception in build_vless_url: %s", e)
            return None