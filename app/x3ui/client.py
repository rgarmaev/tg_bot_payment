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
        paths = self._candidates(["login", "x3ui/login"])  # try /login and explicit /x3ui/login
        try:
            cookie_names = ", ".join(self._client.cookies.keys()) if hasattr(self._client, "cookies") else "<none>"
        except Exception:
            cookie_names = "<error>"
        self._log.info("Stage:login start; candidates=%s; cookies(before)=%s", paths, cookie_names)
        for p in paths:
            try:
                full_url = f"{self.base_url}{p}"
                self._log.debug("Stage:login POST form %s", full_url)
                resp = await self._client.post(full_url, data={"username": self.username, "password": self.password})
                if 200 <= resp.status_code < 400:
                    try:
                        cookie_names = ", ".join(self._client.cookies.keys()) if hasattr(self._client, "cookies") else "<none>"
                    except Exception:
                        cookie_names = "<error>"
                    self._log.info("Stage:login success via %s; cookies=%s", full_url, cookie_names)
                    return
            except Exception as e:
                self._log.debug("Stage:login failed via %s (form): %s", full_url, e)
                pass
            try:
                full_url = f"{self.base_url}{p}"
                self._log.debug("Stage:login POST json %s", full_url)
                resp = await self._client.post(full_url, json={"username": self.username, "password": self.password})
                if 200 <= resp.status_code < 400:
                    try:
                        cookie_names = ", ".join(self._client.cookies.keys()) if hasattr(self._client, "cookies") else "<none>"
                    except Exception:
                        cookie_names = "<error>"
                    self._log.info("Stage:login success via %s; cookies=%s", full_url, cookie_names)
                    return
            except Exception as e:
                self._log.debug("Stage:login failed via %s (json): %s", full_url, e)
                pass

    async def add_client(
        self,
        inbound_id: int,
        days: int,
        traffic_gb: Optional[int],
        email_note: str,
    ) -> X3UICreateClientResult:
        await self.login()
        try:
            cookie_names = ", ".join(self._client.cookies.keys()) if hasattr(self._client, "cookies") else "<none>"
        except Exception:
            cookie_names = "<error>"
        self._log.info("Stage:add_client start; cookies=%s", cookie_names)
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
                # Some forks expect these optional fields present even if empty/zero
                "flow": "",
                "subId": "",
                "reset": 0,
            },
        }

        # Try multiple endpoint subpaths (with and without base path) and both payload formats
        subpaths = [
            "panel/api/inbounds/addClient",
            "api/inbounds/addClient",
            "panel/inbound/addClient",
            "xui/inbound/addClient",
        ]
        endpoints = self._candidates(subpaths)
        payloads = [
            ("v1", payload),
            (
                "v2_obj",
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
            ),
            (
                "v2_str",
                {
                    "id": inbound_id,
                    # Some panels require settings to be a JSON-encoded string
                    "settings": json.dumps(
                        {
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
                        }
                    ),
                },
            ),
        ]

        for ep in endpoints:
            full_url = f"{self.base_url}{ep}"
            for pf_name, pf_payload in payloads:
                try:
                    self._log.info("Stage:add_client try %s with payload %s", full_url, pf_name)
                    self._log.info("Payload: %s", json.dumps(pf_payload, indent=2))
                    # Try JSON first
                    headers_json = {
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"{self.base_url}/",
                    }
                    resp = await self._client.post(full_url, json=pf_payload, headers=headers_json)
                    body = resp.text
                    self._log.info("Stage:add_client resp %s -> %s %s", full_url, resp.status_code, body[:400])
                    # If server complains about JSON parse, retry with raw JSON body
                    if resp.status_code == 200 and body and "unexpected end of json input" in body.lower():
                        try:
                            raw = json.dumps(pf_payload)
                            headers_raw = {
                                "Accept": "application/json",
                                "Content-Type": "application/json; charset=utf-8",
                                "X-Requested-With": "XMLHttpRequest",
                                "Referer": f"{self.base_url}/",
                            }
                            self._log.info("Stage:add_client retry(raw-json) %s", full_url)
                            resp = await self._client.post(full_url, content=raw, headers=headers_raw)
                            body = resp.text
                            self._log.info("Stage:add_client resp(raw-json) %s -> %s %s", full_url, resp.status_code, body[:400])
                        except Exception as e:
                            self._log.debug("Stage:add_client raw-json retry failed: %s", e)
                    # If still not ok, try form-encoded as a last resort
                    if resp.status_code == 200 and (not body or body.strip() == ""):
                        try:
                            from urllib.parse import urlencode as _urlencode
                            # For v2_str, send id/settings fields directly; for others, wrap in json field
                            if pf_name == "v2_str" and isinstance(pf_payload, dict) and "id" in pf_payload and "settings" in pf_payload:
                                form = _urlencode({"id": str(pf_payload["id"]), "settings": pf_payload["settings"]})
                            else:
                                form = _urlencode({"json": json.dumps(pf_payload)})
                            headers_form = {
                                "Accept": "application/json",
                                "Content-Type": "application/x-www-form-urlencoded",
                                "X-Requested-With": "XMLHttpRequest",
                                "Referer": f"{self.base_url}/",
                            }
                            self._log.info("Stage:add_client retry(form) %s", full_url)
                            resp = await self._client.post(full_url, content=form, headers=headers_form)
                            body = resp.text
                            self._log.info("Stage:add_client resp(form) %s -> %s %s", full_url, resp.status_code, body[:400])
                        except Exception as e:
                            self._log.debug("Stage:add_client form retry failed: %s", e)
                    if resp.status_code == 200:
                        # Parse success and extract link
                        config_url: Optional[str] = None
                        is_success = False
                        try:
                            data = resp.json()
                            self._log.debug("Stage:add_client parsed JSON: %s", data)
                            if isinstance(data, dict):
                                success_flag = data.get("success")
                                ok_flag = data.get("ok")
                                status_val = str(data.get("status")).lower() if data.get("status") is not None else None
                                code_val = data.get("code")
                                is_success = (
                                    success_flag is True
                                    or ok_flag is True
                                    or status_val in {"success", "ok"}
                                    or code_val == 0
                                )
                                candidates: list[str] = []
                                for key in ["link", "url", "config", "configUrl", "vless", "vmess", "trojan"]:
                                    val = data.get(key)
                                    if isinstance(val, str):
                                        candidates.append(val)
                                for container_key in ["obj", "data", "result", "client"]:
                                    container = data.get(container_key)
                                    if isinstance(container, dict):
                                        for key in ["link", "url", "config", "configUrl", "vless", "vmess", "trojan"]:
                                            val = container.get(key)
                                            if isinstance(val, str):
                                                candidates.append(val)
                                    if isinstance(container, list):
                                        for it in container:
                                            if isinstance(it, dict):
                                                for key in ["link", "url", "config", "configUrl", "vless", "vmess", "trojan"]:
                                                    val = it.get(key)
                                                    if isinstance(val, str):
                                                        candidates.append(val)
                                for c in candidates:
                                    if isinstance(c, str) and (c.startswith("vless://") or c.startswith("vmess://") or c.startswith("trojan://")):
                                        config_url = c
                                        break
                        except Exception:
                            pass

                        if is_success:
                            if config_url:
                                self._log.info("Stage:add_client success using %s; config URL: %s", full_url, config_url)
                            else:
                                self._log.info("Stage:add_client success using %s; no link provided", full_url)
                            return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=config_url)
                        else:
                            self._log.warning("Stage:add_client body indicates failure on %s: %s", full_url, body)
                    else:
                        self._log.warning("Stage:add_client HTTP %s on %s: %s", resp.status_code, full_url, body)
                except Exception as e:
                    self._log.warning("Stage:add_client error on %s: %s", full_url, e)
                    continue

        self._log.error("Stage:add_client failed after all endpoints")
        # Do not generate the link locally; return without config URL
        return X3UICreateClientResult(uuid=client_uuid, note=email_note, config_url=None)

    async def get_inbound(self, inbound_id: int) -> Optional[dict]:
        await self.login()
        self._log.info("Stage:get_inbound start id=%s", inbound_id)
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
                self._log.debug("Stage:get_inbound request %s", full_url)
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
                self._log.debug("Stage:get_inbound %s error: %s", ep, e)
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