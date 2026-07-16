"""Tests for the async (aiohttp) device-data REST path.

The migration keeps ONE wire builder (`_build_encrypted_request`) shared by the
sync and async senders, so these assert that the async path emits the same
encrypted envelope + headers and decodes the response correctly. A fake
aiohttp session captures the outbound request and returns canned responses
(plain JSON, which `decrypt_response` passes through unchanged).
"""
from __future__ import annotations

import json

import pytest

from custom_components.aiper_irrisense.api import IrrisenseApi


class _FakeResp:
    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self.status = status

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def text(self) -> str:
        return self._text


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in. Returns queued responses."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data}
        )
        text = self._texts.pop(0) if self._texts else "{}"
        return _FakeResp(text)


# --------------------------------------------------------------------------- #
# Shared wire builder
# --------------------------------------------------------------------------- #


def test_build_encrypted_request_shape() -> None:
    api = IrrisenseApi("u", "p", "eu")
    api._token = "TOK"
    enc, url, headers, data = api._build_encrypted_request(
        "/wr/x", {"sn": "S"}, base_url=None, token=None
    )
    assert url.endswith("/wr/x")
    assert headers["token"] == "TOK"
    assert "encryptKey" in headers
    # Body is the AES envelope {"data": "<b64>"}.
    assert list(json.loads(data).keys()) == ["data"]


def test_build_encrypted_request_none_body_has_no_data() -> None:
    api = IrrisenseApi("u", "p", "eu")
    _enc, _url, _headers, data = api._build_encrypted_request(
        "/x", None, base_url=None, token=None
    )
    assert data is None


def test_build_encrypted_request_explicit_token_overrides() -> None:
    api = IrrisenseApi("u", "p", "eu")
    api._token = "SESSION"
    _enc, _url, headers, _data = api._build_encrypted_request(
        "/x", {}, base_url=None, token="OVERRIDE"
    )
    assert headers["token"] == "OVERRIDE"


# --------------------------------------------------------------------------- #
# Async sender
# --------------------------------------------------------------------------- #


async def test_async_call_encrypted_wire_and_parse() -> None:
    api = IrrisenseApi("u", "p", "eu")
    api._token = "TOK"
    session = _FakeSession(['{"code":"0","data":{"ok":1}}'])
    api.attach_async_session(session)

    out = await api._async_call_encrypted("POST", "/wr/x", {"sn": "S"})

    assert out == {"code": "0", "data": {"ok": 1}}
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/wr/x")
    assert call["headers"]["token"] == "TOK"
    assert "encryptKey" in call["headers"]
    assert list(json.loads(call["data"]).keys()) == ["data"]


async def test_async_wr_returns_data_on_success() -> None:
    api = IrrisenseApi("u", "p", "eu")
    api.attach_async_session(_FakeSession(['{"code":"0","data":{"regions":[]}}']))
    result = await api._wr("/wr/getMapList", {"sn": "S"})
    assert result == {"regions": []}


async def test_async_wr_returns_none_on_failure_code() -> None:
    api = IrrisenseApi("u", "p", "eu")
    api.attach_async_session(_FakeSession(['{"code":"6002","msg":"missing fields"}']))
    result = await api._wr("/wr/getWateringRecordHistoryDataV2", {"sn": "S"})
    assert result is None


async def test_get_watering_history_caches_working_shape() -> None:
    api = IrrisenseApi("u", "p", "eu")
    # First body shape fails (6002), second succeeds — mirrors the brute-force.
    session = _FakeSession(
        ['{"code":"6002"}', '{"code":"0","data":{"list":[]}}']
    )
    api.attach_async_session(session)
    result = await api.get_watering_history("S")
    assert result == {"list": []}
    # The working shape index (1) is cached for next time.
    assert api._history_body_idx["S"] == 1


async def test_async_402_triggers_reauth_and_retries() -> None:
    """402 ('account used on another device') must re-login and retry, so HA
    recovers its REST session after the phone app invalidates the token."""
    api = IrrisenseApi("u", "p", "eu")
    # First response: 402. Second (after re-auth): success.
    api.attach_async_session(
        _FakeSession(
            ['{"code":"402","message":"used on another device"}',
             '{"code":"0","data":{"ok":1}}']
        )
    )
    calls = {"refresh": 0, "login": 0}

    def _refresh():
        calls["refresh"] += 1
        return False  # force fallback to login

    def _login():
        calls["login"] += 1
        api._token = "FRESH"
        return True

    api.refresh_token = _refresh
    api.login = _login

    out = await api._async_call_encrypted("POST", "/wr/x", {"sn": "S"})
    assert out == {"code": "0", "data": {"ok": 1}}
    assert calls["login"] == 1  # re-authenticated exactly once
    assert api._token == "FRESH"
