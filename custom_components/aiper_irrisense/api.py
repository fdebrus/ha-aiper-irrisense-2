"""Aiper Irrisense 2 API client — REST + AWS IoT MQTT.

Most of the auth/MQTT plumbing is lifted directly from ha-aiper (pool cleaner),
since the Irrisense 2 shares the same identity/IoT infrastructure. The
Irrisense-specific additions are:

* `wr/*` REST endpoints (read + write)
* `setWorkMode` / `WrControl` MQTT publishes on the `aiper/things/{sn}/downChan`
  topic — **plain JSON** (no XOR envelope), QoS 1
* Fetch + parse the S3-hosted zone map JSON

Command wire format reverse-engineered from the decompiled Android APK.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections import defaultdict
from typing import Any, Callable

import aiohttp
import requests

from .const import (
    API_ENDPOINTS,
    APP_OS,
    APP_VERSION,
    CMD_SET_WORK_MODE,
    CMD_WORK_INFO,
    CMD_WR_CONTROL,
    IRRISENSE_SERIAL_PREFIXES,
    MODE_PESTICIDE,
    MODE_WATERING,
    REGION_TYPE_AREA,
    REGION_TYPE_POINT,
    REQUEST_ID_KEY,
    STATUS_RUNNING,
    STATUS_STOPPED,
    TOPIC_CLOUD_REPORT,
    TOPIC_READ,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_REQUEST,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
    TOPIC_SHADOW_UPDATE_DELTA,
    TOPIC_SHADOW_UPDATE_DOCUMENTS,
    TOPIC_WRITE,
    WATER_YIELD_LOW,
)
from .crypto import AiperEncryption
from .exceptions import InvalidAuth

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# MQTT transport
# --------------------------------------------------------------------------- #
# We connect to AWS IoT with paho-mqtt over a SigV4-signed WebSocket (see
# aws_sigv4.py). paho 2.x surfaces socket teardown cleanly via its
# ``on_disconnect`` callback, so — unlike the old AWSIoTPythonSDK path, whose
# loop thread died with an unhandled AttributeError on eviction — no
# process-wide ``threading.excepthook`` crash shield is required. Reconnection
# (with a freshly re-signed URL each attempt) is handled by a small supervisor
# thread started from ``on_disconnect``.
# --------------------------------------------------------------------------- #


def _find_map_url(obj: Any) -> str | None:
    """Recursively scan a dict/list for the first http(s) URL value."""
    if isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            return obj
        return None
    if isinstance(obj, dict):
        # Prefer common URL keys first
        for key in ("url", "mapUrl", "fileUrl", "downloadUrl", "mapFileUrl"):
            val = obj.get(key)
            if isinstance(val, str) and (val.startswith("http://") or val.startswith("https://")):
                return val
        for val in obj.values():
            found = _find_map_url(val)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _find_map_url(item)
            if found:
                return found
    return None


class IrrisenseApi:
    """REST + MQTT client for the Aiper Irrisense 2."""

    def __init__(self, username: str, password: str, region: str = "eu") -> None:
        self.username = username
        self.password = password
        self.region = region
        self.base_url = API_ENDPOINTS.get(region, API_ENDPOINTS["eu"])

        # Auth state
        self._token: str | None = None
        self._user_id: str | None = None
        self._token_expires: int = 0

        # AWS IoT state
        self._identity_id: str | None = None
        self._identity_pool_id: str | None = None
        self._developer_provider_name: str | None = None
        self._openid_token: str | None = None
        self._openid_token_exp: float | None = None
        self._aws_credentials: dict[str, Any] | None = None
        self._aws_credentials_exp: float | None = None
        self._iot_endpoint: str | None = None
        self._aws_region: str | None = None
        self._mqtt_client: Any = None
        self._mqtt_connected = False
        self.mqtt_debug = False

        # Device cache
        self._devices: dict[str, dict] = {}
        self._device_zone_id_by_sn: dict[str, str] = {}
        # Which of the get_watering_history body shapes the backend accepted
        # for a given SN, so we don't re-brute-force all four every refresh.
        self._history_body_idx: dict[str, int] = {}

        # MQTT subscription callbacks
        self._shadow_callbacks: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()

        # Serialize publishes per device SN
        self._cmd_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

        # ACK watchdog: {(sn, cmd_type): published_at_ts}. Cleared by
        # the coordinator on matching upChan frames; a background timer
        # logs a WARNING if still present after `_ack_timeout` seconds.
        self._pending_ack: dict[tuple[str, str], float] = {}
        self._ack_timeout: float = 3.0

        # Track SNs we've subscribed to (plus the subscribing callback) so we
        # can replay the subscription after any reconnect (paho fires
        # `on_connect` again on each reconnect).
        self._subscribed: dict[str, Callable] = {}
        # True while the reconnect supervisor thread is running.
        self._reconnecting = False
        # Set when we deliberately tear down (disconnect / unload) so the
        # on_disconnect handler doesn't schedule a reconnect.
        self._intentional_disconnect = False

        # REST session. The synchronous `requests` session backs the auth /
        # AWS-credential chain, which must be callable from the (non-event-loop)
        # MQTT bootstrap + reconnect threads. Device-data calls use aiohttp via
        # `_async_call_encrypted` and the shared async session below.
        self._session = requests.Session()
        self._rest_lock = threading.Lock()
        self._rest_min_interval = 0.8
        self._rest_next_allowed = 0.0
        # aiohttp session for async device-data calls. Attached by the
        # coordinator (Home Assistant's shared session) via
        # `attach_async_session`; falls back to a self-owned session for
        # non-HA callers. Both the sync and async paths share the same
        # `_rest_next_allowed` spacing window.
        self._async_session: aiohttp.ClientSession | None = None
        self._owns_async_session = False
        self._async_rest_lock = asyncio.Lock()
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "version": APP_VERSION,
                "os": APP_OS,
                "charset": "UTF-8",
                "Accept-Language": "en",
                "zoneId": "Europe/London",
                "requestidkey": REQUEST_ID_KEY,
                "token": "",
            }
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_success(payload: dict) -> bool:
        code = payload.get("code") if isinstance(payload, dict) else None
        successful = payload.get("successful") if isinstance(payload, dict) else None
        return str(code) in ("0", "200") or successful is True

    def _rest_wait(self) -> None:
        with self._rest_lock:
            now = time.time()
            if now < self._rest_next_allowed:
                time.sleep(self._rest_next_allowed - now)
            self._rest_next_allowed = time.time() + self._rest_min_interval

    def _request_with_backoff(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        json_body: dict | None = None,
        data: Any = None,
        timeout: int = 30,
    ):
        max_attempts = 4
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self._rest_wait()
            try:
                resp = self._session.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=json_body,
                    data=data,
                    timeout=timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise Exception(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp
            except Exception as err:
                last_exc = err
                msg = str(err).lower()
                transient = any(
                    k in msg
                    for k in (
                        "429", "500", "502", "503", "504",
                        "timeout", "tempor", "connection", "reset", "refused",
                    )
                )
                if attempt >= max_attempts or not transient:
                    break
                time.sleep(delay + random.uniform(0, 0.3))
                delay = min(delay * 2.0, 8.0)
        raise last_exc if last_exc else Exception("Request failed")

    def _build_encrypted_request(
        self,
        path: str,
        body: dict | None,
        *,
        base_url: str | None,
        token: str | None,
    ) -> tuple[AiperEncryption, str, dict, str | None]:
        """Build the wire-critical parts of an encrypted REST call.

        SINGLE source of truth for what goes on the wire — headers (including
        the RSA ``encryptKey`` and current ``token``), URL, and the AES-CBC
        encrypted body — shared verbatim by both the synchronous
        (:meth:`_call_encrypted`) and asynchronous
        (:meth:`_async_call_encrypted`) senders so the two paths can never
        drift. The reverse-engineered envelope lives entirely here + in
        :mod:`.crypto`.
        """
        enc = AiperEncryption()
        headers = dict(self._session.headers)
        headers["encryptKey"] = enc.encrypt_key_header
        headers["token"] = token or (self._token or "")

        url_base = (base_url or self.base_url).rstrip("/")
        url = f"{url_base}{path}"

        data = enc.encrypt_request(body) if body is not None else None
        return enc, url, headers, data

    @staticmethod
    def _decode_encrypted_response(enc: AiperEncryption, path: str, text: str) -> dict:
        """Decrypt + JSON-parse a REST response. Shared by both senders."""
        decrypted = enc.decrypt_response(text)
        try:
            return json.loads(decrypted)
        except Exception as err:
            raise Exception(
                f"Failed to parse decrypted response from {path}: {decrypted[:200]}"
            ) from err

    def _call_encrypted(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int = 30,
        retry_login: bool = True,
    ) -> dict:
        """Synchronous encrypted REST call (auth / AWS-credential chain)."""
        enc, url, headers, data = self._build_encrypted_request(
            path, body, base_url=base_url, token=token
        )

        resp = self._request_with_backoff(method, url, headers=headers, data=data, timeout=timeout)
        payload = self._decode_encrypted_response(enc, path, resp.text)

        if retry_login and str(payload.get("code")) in ("401", "403"):
            _LOGGER.info("Token expired; refreshing")
            if self.refresh_token() or self.login():
                return self._call_encrypted(
                    method, path, body,
                    base_url=base_url, token=self._token,
                    timeout=timeout, retry_login=False,
                )

        return payload

    # ------------------------------------------------------------------ #
    # Async device-data REST (aiohttp)
    # ------------------------------------------------------------------ #

    def attach_async_session(self, session: aiohttp.ClientSession) -> None:
        """Attach the aiohttp session used for async device-data calls.

        The coordinator passes Home Assistant's shared session here so we don't
        create (and leak) our own.
        """
        self._async_session = session
        self._owns_async_session = False

    def _get_async_session(self) -> aiohttp.ClientSession:
        if self._async_session is None:
            # Fallback for non-HA callers / tests: self-owned session, closed
            # in disconnect().
            self._async_session = aiohttp.ClientSession()
            self._owns_async_session = True
        return self._async_session

    async def _async_rest_wait(self) -> None:
        """Async twin of :meth:`_rest_wait` — same ``_rest_min_interval``
        spacing, sharing the ``_rest_next_allowed`` window with the sync path.
        """
        async with self._async_rest_lock:
            now = time.time()
            if now < self._rest_next_allowed:
                await asyncio.sleep(self._rest_next_allowed - now)
            self._rest_next_allowed = time.time() + self._rest_min_interval

    async def _async_request_with_backoff(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        data: Any = None,
        timeout: int = 30,
    ) -> str:
        """Async twin of :meth:`_request_with_backoff`. Returns the response
        text. Same retry policy (4 attempts, exp backoff, transient-only)."""
        max_attempts = 4
        delay = 1.0
        last_exc: Exception | None = None
        session = self._get_async_session()
        for attempt in range(1, max_attempts + 1):
            await self._async_rest_wait()
            try:
                async with session.request(
                    method.upper(),
                    url,
                    headers=headers,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status in (429, 500, 502, 503, 504):
                        raise Exception(f"HTTP {resp.status}")
                    resp.raise_for_status()
                    return await resp.text()
            except Exception as err:
                last_exc = err
                msg = str(err).lower()
                transient = any(
                    k in msg
                    for k in (
                        "429", "500", "502", "503", "504",
                        "timeout", "tempor", "connection", "reset", "refused",
                    )
                )
                if attempt >= max_attempts or not transient:
                    break
                await asyncio.sleep(delay + random.uniform(0, 0.3))
                delay = min(delay * 2.0, 8.0)
        raise last_exc if last_exc else Exception("Request failed")

    async def _async_call_encrypted(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: int = 30,
        retry_login: bool = True,
    ) -> dict:
        """Asynchronous encrypted REST call (device-data endpoints).

        Uses the SAME wire builder + response decoder as the sync path. Token
        refresh on 401/403 hops to the executor because the auth chain
        (refresh_token / login) is synchronous.
        """
        enc, url, headers, data = self._build_encrypted_request(
            path, body, base_url=base_url, token=token
        )

        text = await self._async_request_with_backoff(
            method, url, headers=headers, data=data, timeout=timeout
        )
        payload = self._decode_encrypted_response(enc, path, text)

        if retry_login and str(payload.get("code")) in ("401", "403"):
            _LOGGER.info("Token expired; refreshing (async path)")
            loop = asyncio.get_running_loop()
            refreshed = await loop.run_in_executor(
                None, lambda: self.refresh_token() or self.login()
            )
            if refreshed:
                return await self._async_call_encrypted(
                    method, path, body,
                    base_url=base_url, token=self._token,
                    timeout=timeout, retry_login=False,
                )

        return payload

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        _LOGGER.debug("Irrisense login for %s", self.username)
        login_data = {"email": self.username, "password": self.password}

        payload = self._call_encrypted("POST", "/login", login_data, token="")
        if not self._is_success(payload):
            msg = payload.get("msg") or payload.get("message") or "Unknown error"
            raise InvalidAuth(f"Login failed: {msg}")

        result = payload.get("data", {}) or {}
        self._token = result.get("token")
        self._user_id = result.get("serialNumber")
        self._token_expires = result.get("tokenExpires", 0)

        domains = result.get("domain") or []
        if domains:
            self.base_url = str(domains[0]).rstrip("/")

        if not self._token:
            raise InvalidAuth(f"No token in login response: {result}")

        self._session.headers["token"] = self._token
        _LOGGER.info("Irrisense login OK (base_url=%s)", self.base_url)

        self._get_openid_token()
        return True

    def refresh_token(self) -> bool:
        try:
            payload = self._call_encrypted("POST", "/users/token/refresh", {}, retry_login=False)
            if self._is_success(payload):
                new_token = (payload.get("data") or {}).get("token")
                if new_token:
                    self._token = new_token
                    self._session.headers["token"] = new_token
                    return True
        except Exception as err:
            _LOGGER.debug("Token refresh failed: %s", err)
        return False

    def _get_openid_token(self) -> None:
        try:
            payload = self._call_encrypted("POST", "/users/getOpenIdToken", {})
            if not self._is_success(payload):
                _LOGGER.warning("OpenID token fetch failed code=%s", payload.get("code"))
                return
            data = payload.get("data", {}) or {}
            self._developer_provider_name = data.get("developerProviderName")
            self._identity_id = data.get("identityId")
            self._identity_pool_id = data.get("identityPoolId")
            self._iot_endpoint = data.get("iotEndpoint")
            self._aws_region = data.get("region")
            self._openid_token = data.get("token")
            dur = data.get("tokenDuration")
            if dur:
                self._openid_token_exp = time.time() + float(dur)
        except Exception as err:
            _LOGGER.warning("OpenID token error: %s", err)

    def _get_aws_credentials(self) -> dict[str, Any] | None:
        if not self._identity_id or not self._openid_token:
            return None

        if self._openid_token_exp and (self._openid_token_exp - time.time()) < 120:
            self._get_openid_token()

        if self._aws_credentials_exp and (self._aws_credentials_exp - time.time()) > 120:
            return self._aws_credentials

        region = self._aws_region
        if not region and self._iot_endpoint and ".iot." in self._iot_endpoint:
            try:
                region = self._iot_endpoint.split(".iot.", 1)[1].split(".", 1)[0]
            except Exception:
                region = None
        region = region or "eu-central-1"

        url = f"https://cognito-identity.{region}.amazonaws.com/"
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
        }
        body = {
            "IdentityId": self._identity_id,
            "Logins": {"cognito-identity.amazonaws.com": self._openid_token},
        }
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        out = resp.json()
        creds = out.get("Credentials") or {}
        if not creds.get("AccessKeyId"):
            _LOGGER.warning("Unexpected Cognito response: %s", out)
            return None
        self._aws_credentials = creds
        self._aws_credentials_exp = time.time() + 3300
        return creds

    # ------------------------------------------------------------------ #
    # Device discovery / shared endpoints
    # ------------------------------------------------------------------ #

    async def get_devices(self) -> list[dict]:
        """List all devices on the account and filter for Irrisense units."""
        try:
            payload = await self._async_call_encrypted("POST", "/equipment/getEquipment", {})
            if not self._is_success(payload):
                _LOGGER.warning("get_devices failed: %s", payload.get("code"))
                return []

            devices = payload.get("data", []) or []
            if isinstance(devices, dict):
                devices = devices.get("list", devices.get("equipments", []))

            # Diagnostic: surface how many devices the backend returned and
            # their serial prefixes, so a "no devices found" can be told apart
            # from a serial-prefix filter miss. Serials are truncated.
            _LOGGER.debug(
                "getEquipment (async) returned code=%s, %d device(s); serials=%s",
                payload.get("code") if isinstance(payload, dict) else None,
                len(devices) if isinstance(devices, list) else -1,
                [str(d.get("sn"))[:6] + "…" for d in devices if isinstance(d, dict)]
                if isinstance(devices, list) else devices,
            )

            # If the async (aiohttp) path saw zero, compare with the sync
            # (requests) path — this pinpoints whether the aiohttp migration is
            # at fault. Runs on the executor so it doesn't block the loop.
            if isinstance(devices, list) and not devices:
                try:
                    loop = asyncio.get_running_loop()
                    sync_payload = await loop.run_in_executor(
                        None, self._call_encrypted, "POST", "/equipment/getEquipment", {}
                    )
                    sync_data = sync_payload.get("data") or [] if isinstance(sync_payload, dict) else []
                    if isinstance(sync_data, dict):
                        sync_data = sync_data.get("list", sync_data.get("equipments", []))
                    _LOGGER.debug(
                        "getEquipment (sync compare) code=%s → %s device(s)",
                        sync_payload.get("code") if isinstance(sync_payload, dict) else None,
                        len(sync_data) if isinstance(sync_data, list) else sync_data,
                    )
                except Exception as cmp_err:  # noqa: BLE001 - diagnostic only
                    _LOGGER.debug("getEquipment sync compare failed: %s", cmp_err)

            out: list[dict] = []
            for device in devices:
                sn = device.get("sn")
                if not sn:
                    continue
                # Filter: only Irrisense serials (WRX / WGX prefix). Leave other
                # aiper devices to the sibling ha-aiper integration.
                if not sn.upper().startswith(IRRISENSE_SERIAL_PREFIXES):
                    continue
                self._devices[sn] = device
                zid = device.get("zoneId") or device.get("zone_id")
                if isinstance(zid, str) and zid:
                    self._device_zone_id_by_sn[sn] = zid
                out.append(device)
            return out
        except Exception as err:
            _LOGGER.error("Failed to get devices: %s", err)
            return []

    def get_equipment_info(self, sn: str) -> dict | None:
        """Shared `/equipment/getEquipmentInfo` — generic device metadata."""
        try:
            payload = self._call_encrypted(
                "POST", "/equipment/getEquipmentInfo", {"sn": sn}
            )
            if self._is_success(payload):
                return payload.get("data")
        except Exception as err:
            _LOGGER.error("get_equipment_info(%s) failed: %s", sn, err)
        return None

    def check_equipment_online(self, sn: str) -> dict | None:
        try:
            payload = self._call_encrypted(
                "POST", "/equipment/checkEquipmentOnlineStatus", {"sn": sn}
            )
            if self._is_success(payload):
                return payload.get("data")
        except Exception as err:
            _LOGGER.debug("check_equipment_online(%s): %s", sn, err)
        return None

    # ------------------------------------------------------------------ #
    # Irrisense-specific REST endpoints (all under /wr/)
    # ------------------------------------------------------------------ #

    async def _wr(self, path: str, body: dict | None = None) -> dict | None:
        """Helper: POST to a /wr/... endpoint and return data payload on success."""
        try:
            payload = await self._async_call_encrypted("POST", path, body or {})
            if self._is_success(payload):
                return payload.get("data") if isinstance(payload, dict) else None
            _LOGGER.debug(
                "WR %s not successful: code=%s msg=%s body=%s full=%s",
                path,
                payload.get("code") if isinstance(payload, dict) else None,
                payload.get("msg") or payload.get("message") if isinstance(payload, dict) else None,
                body,
                payload,
            )
        except Exception as err:
            _LOGGER.debug("WR call %s error: %s", path, err)
        return None

    # -- Reads --
    async def get_wr_equipment_info(self, sn: str) -> dict | None:
        """Irrisense-specific status (firmware, battery, active zone, etc.)."""
        return await self._wr("/wr/getEquipmentInfo", {"sn": sn})

    async def get_map_list(self, sn: str) -> dict | None:
        """Returns the S3 URL for the zone map JSON."""
        return await self._wr("/wr/getMapList", {"sn": sn})

    async def get_watering_task_list(self, sn: str) -> dict | None:
        return await self._wr("/wr/getWateringTaskListV2", {"sn": sn})

    async def get_watering_setting(self, sn: str) -> dict | None:
        return await self._wr("/wr/getWateringSettingV2", {"sn": sn})

    async def get_nozzle_type_setting(self, sn: str) -> dict | None:
        return await self._wr("/wr/getNozzleTypeSetting", {"sn": sn})

    async def get_reminder_setting(self, sn: str) -> dict | None:
        return await self._wr("/wr/getReminderSetting", {"sn": sn})

    async def get_watering_statistics(self, sn: str) -> dict | None:
        return await self._wr("/wr/wateringRecordStatisticsV2", {"sn": sn})

    async def get_watering_history(self, sn: str, page: int = 1, size: int = 20) -> dict | None:
        # Backend returns code=6002 when required fields are missing. Try a
        # couple of known body shapes; the mobile app sends date windows.
        now_ms = int(time.time() * 1000)
        month_ms = 30 * 24 * 3600 * 1000
        bodies: list[dict] = [
            {"sn": sn, "pageNo": page, "pageSize": size},
            {"sn": sn, "pageNum": page, "pageSize": size},
            {"sn": sn, "pageNo": page, "pageSize": size,
             "startTime": now_ms - month_ms, "endTime": now_ms},
            {"sn": sn, "startTime": now_ms - month_ms, "endTime": now_ms,
             "pageNo": page, "pageSize": size, "type": 0},
        ]

        # Try the shape that worked last time for this SN first; fall back to
        # the full sweep (in the original order) on a cache miss. The bodies
        # themselves are unchanged, and the first-ever call for an SN — before
        # anything is cached — still tries them in the exact original order.
        cached = self._history_body_idx.get(sn)
        order = list(range(len(bodies)))
        if cached is not None and 0 <= cached < len(bodies):
            order = [cached] + [i for i in order if i != cached]

        for i in order:
            result = await self._wr("/wr/getWateringRecordHistoryDataV2", bodies[i])
            if result is not None:
                self._history_body_idx[sn] = i
                return result
        # Nothing worked this pass — drop any stale cache so the next refresh
        # sweeps fresh rather than re-trying a shape the backend now rejects.
        self._history_body_idx.pop(sn, None)
        return None

    async def get_drainage_reminder(self, sn: str) -> dict | None:
        return await self._wr("/wr/getDrainageReminderPopup", {"sn": sn})

    async def get_map_pesticide_usage(self, sn: str) -> dict | None:
        return await self._wr("/wr/getMapPesticideUsage", {"sn": sn})

    async def get_skip_history(self, sn: str) -> dict | None:
        return await self._wr("/wr/getWateringTaskSkipRecordHistoryDataV2", {"sn": sn})

    @staticmethod
    def _parse_regions(zmap: dict | None) -> list[dict[str, Any]]:
        """Slim the zone-map JSON down to just what the coordinator needs.

        The raw S3 JSON carries per-region `points[]` arrays (each with rotate,
        valve, waterpress, etc.) that bloat memory and we never read. Keep the
        trip-planning fields only.
        """
        if not isinstance(zmap, dict):
            return []
        raw = zmap.get("regions")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            rid = r.get("id")
            if not isinstance(rid, int):
                continue
            out.append(
                {
                    "id": rid,
                    "name": r.get("name") if isinstance(r.get("name"), str) else f"Zone {rid}",
                    "type": int(r.get("type") or 0),
                    "sort": int(r.get("sort") or 0),
                    "waterYield": float(r.get("waterYield") or WATER_YIELD_LOW),
                    "pointTime": int(r.get("pointTime") or 1),
                    "n_points": len(r.get("points") or []),
                    "usageStatus": r.get("usageStatus"),
                }
            )
        return out

    def fetch_zone_map(self, sn: str) -> dict | None:
        """Blocking zone-map fetch using ``requests``. Prefer
        :py:meth:`async_fetch_zone_map` — ``requests`` goes through urllib3's
        header parser, which chokes on Aiper's bogus
        ``Content-Type: multipart/form-data; charset=utf-8`` (no boundary).

        Left in place for diagnostics / non-HA callers. Returns the raw JSON
        dict on success.
        """
        # Sync path: the async get_map_list can't be awaited here, so issue the
        # encrypted getMapList directly via the synchronous sender.
        try:
            payload = self._call_encrypted("POST", "/wr/getMapList", {"sn": sn})
            info = payload.get("data") if self._is_success(payload) else None
        except Exception as err:  # noqa: BLE001 - diagnostic helper
            _LOGGER.warning("getMapList (sync) failed for %s: %s", sn, err)
            info = None
        if info is None:
            return None

        _LOGGER.debug("getMapList raw response for %s: %s", sn, info)

        url = _find_map_url(info)
        if not url:
            _LOGGER.debug("No map URL found in getMapList response for %s: %s", sn, info)
            return None

        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as err:
            _LOGGER.warning("Failed to fetch zone map for %s: %s", sn, err)
            return None

    async def async_fetch_zone_map(
        self,
        session: aiohttp.ClientSession,
        sn: str,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> dict | None:
        """Async zone-map fetch using aiohttp.

        Why not ``requests``: the S3 pre-signed URL is served with
        ``Content-Type: multipart/form-data; charset=utf-8`` with **no
        boundary parameter**. urllib3's email/header parser raises
        ``NoBoundaryInMultipartDefect`` → ``HeaderParsingError`` before the
        body is returned. aiohttp does not invoke that parser, so the plain
        JSON body comes through intact.

        ``get_map_list`` is now an async encrypted REST call (awaited
        directly); the S3 GET uses the passed-in aiohttp session.
        """
        info = await self.get_map_list(sn)
        if info is None:
            return None

        _LOGGER.debug("getMapList raw response for %s: %s", sn, info)

        url = _find_map_url(info)
        if not url:
            _LOGGER.debug("No map URL found in getMapList response for %s: %s", sn, info)
            return None

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                # Force text→json decode ourselves so aiohttp doesn't trip on
                # the bogus Content-Type either (it would refuse json() by
                # default since the header isn't application/json).
                text = await resp.text()
            try:
                return json.loads(text)
            except Exception as parse_err:
                _LOGGER.warning(
                    "Zone map JSON parse failed for %s: %s (body=%s)",
                    sn, parse_err, text[:200],
                )
                return None
        except Exception as err:
            _LOGGER.warning("Failed to fetch zone map for %s: %s", sn, err)
            return None

    # -- Writes --
    async def set_schedule_enabled(self, sn: str, task_ids: list[int], enabled: bool) -> bool:
        body = {"sn": sn, "taskIds": task_ids, "enabled": 1 if enabled else 0}
        return await self._wr("/wr/batchUpdateWrWateringTaskEnabledV2", body) is not None

    async def set_watering_setting(self, sn: str, settings: dict[str, Any]) -> bool:
        body = {"sn": sn, **settings}
        return await self._wr("/wr/updateWateringSetting", body) is not None

    async def set_nozzle_type(self, sn: str, nozzle_type: int) -> bool:
        # The `/wr/updateNozzleTypeSetting` REST endpoint uses a 1-indexed
        # mapping: 1 = Standard, 2 = Jet. Our device-side representation
        # (matches the iOS UI + MQTT shadow) is 0 = Standard, 1 = Jet — see
        # decompiled `NozzleViewModel.updateNozzleSettingByServer`:
        #     updateNozzleTypeSetting(sn, value == 1 ? 2 : 1)
        server_value = 2 if int(nozzle_type) == 1 else 1
        body = {"sn": sn, "nozzleType": server_value}
        return await self._wr("/wr/updateNozzleTypeSetting", body) is not None

    async def set_water_shortage_reminder(self, sn: str, enabled: bool) -> bool:
        body = {"sn": sn, "waterShortageReminder": 1 if enabled else 0}
        return await self._wr("/wr/updateWaterShortageReminderSetting", body) is not None

    async def set_task_reminder(self, sn: str, enabled: bool) -> bool:
        body = {"sn": sn, "taskReminder": 1 if enabled else 0}
        return await self._wr("/wr/updateTaskReminderSetting", body) is not None

    async def set_pesticide_reminder(self, sn: str, enabled: bool) -> bool:
        body = {"sn": sn, "pesticideReminder": 1 if enabled else 0}
        return await self._wr("/wr/updatePesticideReminderSetting", body) is not None

    async def set_drainage_reminder(self, sn: str, enabled: bool) -> bool:
        """Drainage reminder lives under `updateTaskReminderSetting` in the
        mobile app (shared endpoint toggles all four), but we also try a
        dedicated endpoint first in case the backend exposes one.
        """
        body = {"sn": sn, "drainageReminder": 1 if enabled else 0}
        # No known dedicated endpoint yet — send as a field update on the
        # generic reminder setter if it exists; otherwise fall back to the
        # watering-setting path (some backends accept reminder keys there).
        result = await self._wr("/wr/updateDrainageReminderSetting", body)
        if result is None:
            result = await self._wr("/wr/updateTaskReminderSetting", body)
        return result is not None

    # ------------------------------------------------------------------ #
    # MQTT — AWS IoT WebSocket (SigV4)
    # ------------------------------------------------------------------ #

    def _aws_iot_region(self) -> str:
        region = self._aws_region
        if not region and self._iot_endpoint and ".iot." in self._iot_endpoint:
            region = self._iot_endpoint.split(".iot.", 1)[1].split(".", 1)[0]
        return region or "eu-central-1"

    def _build_ws_path(self, creds: dict[str, Any]) -> str:
        """Return the SigV4-signed WebSocket request path (``/mqtt?...``)."""
        from .aws_sigv4 import presign_iot_wss_url  # noqa: WPS433

        url = presign_iot_wss_url(
            self._iot_endpoint,
            self._aws_iot_region(),
            creds["AccessKeyId"],
            creds["SecretKey"],
            creds.get("SessionToken"),
        )
        # paho's ws_set_options wants everything after the host: "/mqtt?...".
        return url.split(self._iot_endpoint, 1)[1]

    def connect_mqtt(self) -> bool:
        if not self._identity_id or not self._iot_endpoint:
            _LOGGER.error("No IoT identity/endpoint available")
            return False
        try:
            import paho.mqtt.client as mqtt  # noqa: WPS433
            import certifi  # noqa: WPS433

            creds = self._get_aws_credentials()
            if not creds:
                _LOGGER.error("Unable to obtain AWS credentials for MQTT")
                return False

            # ClientId must equal the Cognito identity_id verbatim (exact-match
            # AWS IoT policy). Co-existence with the iOS Aiper app is
            # impossible; last CONNECT wins — whenever the phone app opens, the
            # broker evicts us. paho 2.x reports this cleanly via on_disconnect
            # (no thread crash), and our reconnect supervisor re-signs + retries.
            client_id = self._identity_id
            _LOGGER.info("Irrisense MQTT client_id=%s", client_id)

            self._intentional_disconnect = False
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                transport="websockets",
            )
            # We drive reconnection ourselves so each attempt gets a freshly
            # re-signed SigV4 URL (paho would blindly reuse the stale one).
            client.reconnect_on_failure = False
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            if self.mqtt_debug:
                try:
                    client.enable_logger(logging.getLogger("paho.mqtt.aiper"))
                except Exception:  # noqa: BLE001 - diagnostic only
                    pass

            client.ws_set_options(path=self._build_ws_path(creds))
            client.tls_set(ca_certs=certifi.where())

            self._mqtt_client = client
            client.connect(self._iot_endpoint, 443, keepalive=60)
            client.loop_start()

            # Wait briefly for CONNACK (on_connect flips _mqtt_connected).
            for _ in range(100):
                if self._mqtt_connected:
                    _LOGGER.info("Connected to AWS IoT MQTT (Irrisense)")
                    return True
                time.sleep(0.1)
            _LOGGER.warning("MQTT connect timed out waiting for CONNACK")
            return False
        except ImportError:
            _LOGGER.error("paho-mqtt not installed")
            return False
        except Exception as err:
            _LOGGER.exception("MQTT connection failed: %r", err)
            return False

    def is_mqtt_connected(self) -> bool:
        return bool(self._mqtt_connected and self._mqtt_client)

    # ------------------------------------------------------------------ #
    # paho callbacks + reconnect supervisor
    # ------------------------------------------------------------------ #

    @staticmethod
    def _rc_is_success(reason_code: Any) -> bool:
        """True if a paho reason code / int indicates success."""
        if reason_code is None:
            return True
        if isinstance(reason_code, int):
            return reason_code == 0
        # paho 2.x ReasonCode object
        is_failure = getattr(reason_code, "is_failure", None)
        if is_failure is not None:
            return not is_failure
        return int(getattr(reason_code, "value", 1)) == 0

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Fires on initial connect AND after every reconnect."""
        if not self._rc_is_success(reason_code):
            _LOGGER.warning("Irrisense MQTT connect refused: %s", reason_code)
            return
        self._mqtt_connected = True
        _LOGGER.info(
            "Irrisense MQTT ONLINE. Replaying %d subscription(s).",
            len(self._subscribed),
        )
        # Replay every remembered subscription — paho does not restore them
        # across a fresh connect.
        for sn, cb in list(self._subscribed.items()):
            try:
                self.subscribe_device(sn, cb)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Failed to replay subscription for %s: %s", sn, err)

    def _on_disconnect(self, client, userdata, *args) -> None:
        """Fires when the socket drops. paho 2.x passes
        ``(client, userdata, disconnect_flags, reason_code, properties)``;
        accept ``*args`` so minor signature differences don't break us.
        """
        self._mqtt_connected = False
        reason_code = None
        for a in args:
            # reason_code is the arg that looks like an int / ReasonCode.
            if isinstance(a, int) or hasattr(a, "is_failure"):
                reason_code = a
        if self._intentional_disconnect:
            _LOGGER.debug("Irrisense MQTT disconnected (intentional).")
            return
        _LOGGER.warning(
            "Irrisense MQTT OFFLINE (rc=%s). Most common cause: the Aiper phone "
            "app connected with the same Cognito identityId and AWS IoT evicted "
            "us. Scheduling reconnect.",
            reason_code,
        )
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Start the reconnect supervisor (one at a time). Each attempt
        re-signs a fresh SigV4 URL — temporary credentials rotate, so the
        previous URL cannot simply be reused.
        """
        with self._lock:
            if self._reconnecting:
                return
            self._reconnecting = True

        def _worker() -> None:
            delay = 1.0
            try:
                while not self._intentional_disconnect and not self._mqtt_connected:
                    time.sleep(delay)
                    delay = min(delay * 2.0, 8.0)
                    if self._mqtt_client is None:
                        return
                    try:
                        creds = self._get_aws_credentials()
                        if not creds:
                            _LOGGER.debug("Reconnect: no AWS credentials yet")
                            continue
                        self._mqtt_client.ws_set_options(
                            path=self._build_ws_path(creds)
                        )
                        self._mqtt_client.reconnect()
                        # Give CONNACK a moment; _on_connect flips the flag.
                        for _ in range(50):
                            if self._mqtt_connected or self._intentional_disconnect:
                                break
                            time.sleep(0.1)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("MQTT reconnect attempt failed: %s", err)
                if self._mqtt_connected:
                    _LOGGER.info("Irrisense MQTT reconnected.")
            finally:
                with self._lock:
                    self._reconnecting = False

        threading.Thread(
            target=_worker, name="irrisense-mqtt-reconnect", daemon=True
        ).start()

    def request_shadow(self, sn: str) -> bool:
        if not self.is_mqtt_connected():
            return False
        try:
            topic = TOPIC_SHADOW_GET_REQUEST.format(sn=sn)
            self._mqtt_client.publish(topic, "", 1)
            return True
        except Exception as err:
            _LOGGER.debug("request_shadow(%s) failed: %s", sn, err)
            return False

    def subscribe_device(self, sn: str, callback: Callable[[str, dict], None]) -> bool:
        """Subscribe to all device-relevant MQTT topics.

        Callbacks are invoked with `(sn, parsed_dict)`. Topics subscribed:
          - aiper/things/{sn}/upChan           (command responses)
          - aiper/things/{sn}/WR/cloud/report  (plain-JSON heartbeats + alarms)
          - $aws/things/{sn}/shadow/...        (NetStat/OpInfo only — no watering state)
        """
        if not self.is_mqtt_connected():
            _LOGGER.warning("MQTT not connected, cannot subscribe")
            return False

        with self._lock:
            # Idempotent: avoid stacking duplicate callbacks on reconnect-driven replays.
            callbacks = self._shadow_callbacks.setdefault(sn, [])
            if callback not in callbacks:
                callbacks.append(callback)
            # Remember the last-subscriber so the crash shield can replay after
            # a forced recreate (keyed by sn so each device replays once).
            self._subscribed[sn] = callback

        decoder = json.JSONDecoder()

        def _iter_json_objects(text: str) -> list[Any]:
            """Parse zero or more back-to-back JSON objects from ``text``.

            The ``/WR/cloud/report`` topic occasionally emits multiple JSON
            frames concatenated in a single MQTT packet (e.g. ``{...}{...}``
            or with interleaving whitespace/newlines). Plain ``json.loads``
            rejects anything after the first object. Use ``raw_decode`` in a
            loop so every frame is delivered to the callback.
            """
            out: list[Any] = []
            pos = 0
            length = len(text)
            while pos < length:
                # Skip whitespace between objects
                while pos < length and text[pos].isspace():
                    pos += 1
                if pos >= length:
                    break
                try:
                    obj, end = decoder.raw_decode(text, pos)
                except ValueError:
                    # Couldn't parse from here — bail; caller will log a
                    # warning for the remaining tail.
                    break
                out.append(obj)
                pos = end
            return out

        def on_message(client, userdata, message):  # noqa: ARG001
            try:
                raw = message.payload
                text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)

                objects = _iter_json_objects(text)
                if not objects:
                    _LOGGER.debug(
                        "Non-JSON MQTT message on %s: %s",
                        message.topic, text[:200],
                    )
                    return

                if self.mqtt_debug:
                    if len(objects) == 1:
                        _LOGGER.debug(
                            "MQTT %s: %s",
                            getattr(message, "topic", "?"), text[:400],
                        )
                    else:
                        _LOGGER.debug(
                            "MQTT %s (%d concatenated frames): %s",
                            getattr(message, "topic", "?"),
                            len(objects), text[:400],
                        )

                for data in objects:
                    if isinstance(data, dict):
                        data.setdefault("_sn", sn)
                        try:
                            data["_topic"] = getattr(message, "topic", "")
                        except Exception:
                            pass

                    with self._lock:
                        callbacks = list(self._shadow_callbacks.get(sn, []))
                    for cb in callbacks:
                        try:
                            cb(sn, data)
                        except TypeError:
                            try:
                                cb(data)
                            except Exception as err:
                                _LOGGER.error("Callback error: %s", err)
                        except Exception as err:
                            _LOGGER.error("Callback error: %s", err)
            except Exception as err:
                _LOGGER.error("Failed to process MQTT message: %s", err)

        try:
            topics = [
                TOPIC_READ.format(sn=sn),
                TOPIC_CLOUD_REPORT.format(sn=sn),
                TOPIC_SHADOW_GET.format(sn=sn),
                TOPIC_SHADOW_UPDATE_ACCEPTED.format(sn=sn),
                TOPIC_SHADOW_UPDATE_DELTA.format(sn=sn),
                TOPIC_SHADOW_UPDATE_DOCUMENTS.format(sn=sn),
            ]
            for t in topics:
                # paho routes messages to a per-topic callback registered via
                # message_callback_add; subscribe() only installs the filter.
                self._mqtt_client.message_callback_add(t, on_message)
                self._mqtt_client.subscribe(t, 1)
                _LOGGER.debug("Subscribed to %s", t)
            return True
        except Exception as err:
            _LOGGER.error("subscribe_device(%s) failed: %s", sn, err)
            return False

    # ------------------------------------------------------------------ #
    # Irrisense MQTT commands — plain JSON {<cmd>: {...}}, QoS 1
    # ------------------------------------------------------------------ #

    def _publish_cmd(self, sn: str, cmd_type: str, data: dict) -> bool:
        """Publish a downChan command. Plain JSON, no XOR, QoS 1.

        Envelope: ``{"<cmd_name>": {...data}}`` — the command name is the
        **top-level key** and the payload is its direct value. There is no
        ``type`` / ``data`` wrapper. Ground truth captured via frida-gadget
        hook on the Android app (``AWSIotMqttManager.publishString`` +
        ``com.aiper.device.common.utils.MqttManager.publishToTopic``) —
        every publish the official app sends uses the unwrapped form,
        e.g. ``{"setWorkMode":{"mode":0,"waterYield":0.1,"map_id":1,"status":1}}``.

        Logging is at INFO with the exact bytes recorded so ship logs can be
        diff-compared with a phone MQTT capture. Also records an outbound-
        timestamp per (sn, cmd_type) that :meth:`_ack_watchdog` consumes to
        decide whether an expected upChan response arrived.
        """
        if not self.is_mqtt_connected():
            _LOGGER.warning("MQTT not connected; cannot publish %s to %s", cmd_type, sn)
            return False

        payload = {cmd_type: dict(data)}
        message = json.dumps(payload, separators=(",", ":"))
        topic = TOPIC_WRITE.format(sn=sn)

        try:
            with self._cmd_locks[sn]:
                self._mqtt_client.publish(topic, message, 1)
            # Byte-for-byte publish record. High-volume on the always-on
            # command path (every start/stop), so keep it at INFO only when
            # mqtt_debug is enabled; otherwise emit at DEBUG so it stays
            # reachable via logger config without spamming shipping logs.
            if self.mqtt_debug:
                _LOGGER.info("MQTT PUB → %s  (%d bytes)  %s", topic, len(message), message)
            else:
                _LOGGER.debug("MQTT PUB → %s  (%d bytes)  %s", topic, len(message), message)

            # Record outbound timestamp so the ACK watchdog can detect a
            # silent drop. We intentionally only watch command types that
            # are expected to echo back on upChan (setWorkMode, WrControl).
            if cmd_type in ("setWorkMode", "WrControl"):
                with self._lock:
                    self._pending_ack[(sn, cmd_type)] = time.time()
                self._schedule_ack_watchdog(sn, cmd_type, message)
            return True
        except Exception as err:
            _LOGGER.error("Publish %s failed: %s", cmd_type, err)
            return False

    # ------------------------------------------------------------------ #
    # ACK watchdog
    # ------------------------------------------------------------------ #

    def _schedule_ack_watchdog(self, sn: str, cmd_type: str, sent_bytes: str) -> None:
        """Start a one-shot timer that warns if no up_<cmd_type> / <cmd_type>
        response is observed on upChan within :attr:`_ack_timeout` seconds.

        The watchdog runs in a daemon thread so it can't block the MQTT
        callback loop. When an ACK is seen, :meth:`note_upchan_ack` clears
        the pending entry and the timer silently no-ops.
        """
        timeout = self._ack_timeout

        def _wait():
            time.sleep(timeout)
            with self._lock:
                ts = self._pending_ack.pop((sn, cmd_type), None)
            if ts is None:
                # ACK cleared it — healthy path
                return
            _LOGGER.warning(
                "MQTT ACK TIMEOUT: %s %s → no up_%s on upChan within %.1fs. "
                "Broker accepted the publish but the device did not respond. "
                "Sent bytes: %s",
                sn, cmd_type, cmd_type, timeout, sent_bytes,
            )

        threading.Thread(target=_wait, name=f"irrisense-ack-{cmd_type}", daemon=True).start()

    def note_upchan_ack(self, sn: str, cmd_type: str) -> None:
        """Coordinator calls this whenever an upChan frame with a matching
        ``type`` (or ``up_<type>``) is observed, so the watchdog can clear.
        """
        with self._lock:
            self._pending_ack.pop((sn, cmd_type), None)

    def start_zone(
        self,
        sn: str,
        map_id: int,
        *,
        region_type: int = 0,
        water_yield: float = WATER_YIELD_LOW,
        point_time: int | None = None,
        pesticide: bool = False,
        pesticides_sn: str | None = None,
        used_amount: float | None = None,
    ) -> bool:
        """Start watering on a zone (region).

        Source: WrPanelWorkInfoViewModel.startWork (APK :1833-1858); payload
        shape cross-confirmed via frida capture of the live Android app.

        Wire payload the firmware accepts:
          * Area/line zones → ``{mode, waterYield, map_id, status:1}``
          * Point zones    → ``{mode, point_time, map_id, status:1}``
          * No ``region_type`` on the wire — the device infers it from the
            zone map itself. ``region_type`` here is only used to decide
            which dose field to send.

        Preset rules:
          * ``waterYield`` legal presets 0.1 / 0.25 / 0.5 (app labels
            "3 / 6 / 13 mm"). Off-preset values are silently dropped by
            the firmware; the coordinator snaps before calling this.
          * ``point_time`` legal presets 1 / 5 / 10 minutes (APK :1844).

        Pesticide mode (area zones only, per APK ``startWork$start``):
          ``mode=0``, ``waterYield=WATER_YIELD_LOW``, plus
          ``pesticides_sn`` + ``used_amount``.
        """
        mode = MODE_PESTICIDE if pesticide else MODE_WATERING
        body: dict[str, Any] = {
            "map_id": int(map_id),
            "status": STATUS_RUNNING,
            "mode": mode,
        }

        if region_type == REGION_TYPE_POINT:
            # point_time is in MINUTES (APK :1844).
            body["point_time"] = int(point_time if point_time is not None else 1)
        else:
            body["waterYield"] = float(water_yield)

        if pesticide and region_type == REGION_TYPE_AREA:
            # Pesticide path (area regions only — APK `startWork$start`
            # only adds pesticides_sn / used_amount when region.type == 0).
            # Drop point_time (shouldn't be set in this branch anyway),
            # pin yield to the low preset, and attach pesticide info.
            body.pop("point_time", None)
            body["waterYield"] = WATER_YIELD_LOW
            if pesticides_sn:
                body["pesticides_sn"] = str(pesticides_sn)
            if used_amount is not None:
                body["used_amount"] = float(used_amount)

        return self._publish_cmd(sn, CMD_SET_WORK_MODE, body)

    def stop_zone(self, sn: str, map_id: int) -> bool:
        """Stop watering on a zone.

        Source: WrPanelWorkInfoViewModel.stopWork (APK :2139-2142).
        """
        body = {"mode": 0, "map_id": int(map_id), "status": STATUS_STOPPED}
        return self._publish_cmd(sn, CMD_SET_WORK_MODE, body)

    def query_work_info(self, sn: str) -> bool:
        """Ask the device to publish its current work snapshot.

        Source: CmdManager.sendIgnoreResponse("workInfo", null).
        """
        return self._publish_cmd(sn, CMD_WORK_INFO, {})

    def wr_control(self, sn: str, cmd: int) -> bool:
        """Send WrControl — manual valve / reset command.

        `cmd=1` starts manual mode, `cmd=0` resets/exits.
        """
        return self._publish_cmd(sn, CMD_WR_CONTROL, {"cmd": int(cmd)})

    # ------------------------------------------------------------------ #
    # Diagnostic: raw publish
    # ------------------------------------------------------------------ #

    def debug_publish(self, topic: str, payload: str, qos: int = 1) -> bool:
        """Publish an arbitrary payload to an arbitrary topic.

        Intended as a troubleshooting hatch — lets us experiment with
        payload shapes (e.g. boxed longs, camelCase variants, extra fields)
        without redeploying. Logged at INFO.
        """
        if not self.is_mqtt_connected():
            _LOGGER.warning("MQTT not connected; cannot debug_publish to %s", topic)
            return False
        try:
            self._mqtt_client.publish(topic, payload, int(qos))
            _LOGGER.info(
                "MQTT PUB (debug) → %s  qos=%d  (%d bytes)  %s",
                topic, qos, len(payload), payload,
            )
            return True
        except Exception as err:
            _LOGGER.error("debug_publish(%s) failed: %s", topic, err)
            return False

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def disconnect(self) -> None:
        # Signal the on_disconnect handler + reconnect supervisor to stand down.
        self._intentional_disconnect = True
        if self._mqtt_client:
            try:
                self._mqtt_client.disconnect()
            except Exception:
                pass
            try:
                # Stop paho's network loop thread started by loop_start().
                self._mqtt_client.loop_stop()
            except Exception:
                pass
            self._mqtt_connected = False
        try:
            self._session.close()
        except Exception:
            pass
        # Only close a self-owned aiohttp session; Home Assistant's shared
        # session (attached via attach_async_session) must never be closed here.
        if self._owns_async_session and self._async_session is not None:
            session, self._async_session = self._async_session, None
            self._owns_async_session = False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and not session.closed:
                loop.create_task(session.close())
        _LOGGER.info("Irrisense API disconnected")
