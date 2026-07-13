"""Tests for the typed authentication exceptions.

api.login must raise InvalidAuth (not a bare Exception) so callers can
discriminate a permanent auth failure from a transient connectivity problem
without matching on message text.
"""
from __future__ import annotations

import pytest

from custom_components.aiper_irrisense.api import IrrisenseApi
from custom_components.aiper_irrisense.exceptions import (
    AiperError,
    CannotConnect,
    InvalidAuth,
)


def test_exception_hierarchy() -> None:
    assert issubclass(InvalidAuth, AiperError)
    assert issubclass(CannotConnect, AiperError)


def test_login_rejected_credentials_raise_invalid_auth(monkeypatch) -> None:
    api = IrrisenseApi("user@example.com", "pw", region="eu")
    # Cloud rejects the login (non-success code).
    monkeypatch.setattr(
        api, "_call_encrypted", lambda *a, **k: {"code": "1", "msg": "bad creds"}
    )
    with pytest.raises(InvalidAuth):
        api.login()


def test_login_missing_token_raises_invalid_auth(monkeypatch) -> None:
    api = IrrisenseApi("user@example.com", "pw", region="eu")
    # Success code but no token in the payload — still a permanent auth problem.
    monkeypatch.setattr(
        api, "_call_encrypted", lambda *a, **k: {"code": "0", "data": {}}
    )
    with pytest.raises(InvalidAuth):
        api.login()
