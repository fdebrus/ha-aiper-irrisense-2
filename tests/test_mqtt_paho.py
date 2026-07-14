"""Tests for the paho-mqtt + SigV4 MQTT transport.

A live AWS IoT connection can't be exercised in CI, so these cover everything
up to (and including) the paho client wiring using a fake client:

* the SigV4 WebSocket path assembly (`_build_ws_path`),
* reason-code interpretation (`_rc_is_success`),
* the on_disconnect -> reconnect decision, and
* connect_mqtt building a paho VERSION2 websockets client with a signed ws
  path + TLS, driving it to "connected" via a simulated on_connect.
"""
from __future__ import annotations

import time
import types

import paho.mqtt.client as _paho
import pytest

from custom_components.aiper_irrisense.api import IrrisenseApi

# paho-mqtt 2.x introduced CallbackAPIVersion. On Python 3.11 the HA test pin
# drags in paho 1.6.x (see requirements_test.txt), where connect_mqtt's
# VERSION2 client can't be built — so skip just that wiring test there. Python
# 3.12/3.13 CI runs it against real paho 2.x.
requires_paho2 = pytest.mark.skipif(
    not hasattr(_paho, "CallbackAPIVersion"),
    reason="needs paho-mqtt 2.x (CallbackAPIVersion)",
)


def _api_with_creds() -> IrrisenseApi:
    api = IrrisenseApi("u", "p", "eu")
    api._identity_id = "id-123"
    api._openid_token = "tok"
    api._openid_token_exp = None
    api._iot_endpoint = "abc-ats.iot.eu-central-1.amazonaws.com"
    api._aws_region = "eu-central-1"
    api._aws_credentials = {
        "AccessKeyId": "AKIDEXAMPLE",
        "SecretKey": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "SessionToken": "SESSION+TOKEN==",
    }
    api._aws_credentials_exp = time.time() + 9999
    return api


# --------------------------------------------------------------------------- #
# SigV4 ws path
# --------------------------------------------------------------------------- #


def test_build_ws_path() -> None:
    api = _api_with_creds()
    path = api._build_ws_path(api._aws_credentials)
    assert path.startswith("/mqtt?")
    assert "X-Amz-Signature=" in path
    assert "X-Amz-Security-Token=" in path
    # Only the path is returned — no scheme/host.
    assert "wss://" not in path
    assert api._iot_endpoint not in path


def test_aws_iot_region_from_endpoint() -> None:
    api = IrrisenseApi("u", "p", "eu")
    api._aws_region = None
    api._iot_endpoint = "abc-ats.iot.us-east-1.amazonaws.com"
    assert api._aws_iot_region() == "us-east-1"


# --------------------------------------------------------------------------- #
# reason-code handling
# --------------------------------------------------------------------------- #


def test_rc_is_success_variants() -> None:
    assert IrrisenseApi._rc_is_success(0) is True
    assert IrrisenseApi._rc_is_success(None) is True
    assert IrrisenseApi._rc_is_success(1) is False
    # paho 2.x ReasonCode-like objects expose `is_failure`.
    ok = types.SimpleNamespace(is_failure=False)
    bad = types.SimpleNamespace(is_failure=True)
    assert IrrisenseApi._rc_is_success(ok) is True
    assert IrrisenseApi._rc_is_success(bad) is False


# --------------------------------------------------------------------------- #
# on_disconnect -> reconnect decision
# --------------------------------------------------------------------------- #


def test_on_disconnect_schedules_reconnect_when_unexpected(monkeypatch) -> None:
    api = IrrisenseApi("u", "p", "eu")
    called = []
    monkeypatch.setattr(api, "_schedule_reconnect", lambda: called.append(True))
    api._intentional_disconnect = False
    api._mqtt_connected = True
    api._on_disconnect(None, None, None, 1, None)
    assert api._mqtt_connected is False
    assert called == [True]


def test_on_disconnect_silent_when_intentional(monkeypatch) -> None:
    api = IrrisenseApi("u", "p", "eu")
    called = []
    monkeypatch.setattr(api, "_schedule_reconnect", lambda: called.append(True))
    api._intentional_disconnect = True
    api._on_disconnect(None, None, None, 0, None)
    assert called == []


# --------------------------------------------------------------------------- #
# connect_mqtt wiring (fake paho client)
# --------------------------------------------------------------------------- #


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, callback_api_version=None, client_id=None, transport=None):
        self.callback_api_version = callback_api_version
        self.client_id = client_id
        self.transport = transport
        self.reconnect_on_failure = True
        self.on_connect = None
        self.on_disconnect = None
        self.ws_path = None
        self.tls_set_called = False
        self.connected_args = None
        self.loop_started = False
        _FakeClient.instances.append(self)

    def enable_logger(self, logger=None):
        pass

    def ws_set_options(self, path=None):
        self.ws_path = path

    def tls_set(self, ca_certs=None):
        self.tls_set_called = True

    def connect(self, host, port, keepalive=60):
        self.connected_args = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True
        # Simulate the broker accepting the connection.
        self.on_connect(self, None, {}, 0, None)


@requires_paho2
def test_connect_mqtt_builds_signed_paho_client(monkeypatch) -> None:
    import paho.mqtt.client as mqtt

    _FakeClient.instances.clear()
    monkeypatch.setattr(mqtt, "Client", _FakeClient)

    api = _api_with_creds()
    assert api.connect_mqtt() is True
    assert api.is_mqtt_connected() is True

    client = _FakeClient.instances[-1]
    assert client.callback_api_version == mqtt.CallbackAPIVersion.VERSION2
    assert client.transport == "websockets"
    assert client.client_id == "id-123"
    assert client.reconnect_on_failure is False  # we drive reconnection
    assert client.ws_path.startswith("/mqtt?")
    assert client.tls_set_called is True
    assert client.connected_args == ("abc-ats.iot.eu-central-1.amazonaws.com", 443, 60)
    assert client.loop_started is True


def test_connect_mqtt_returns_false_without_credentials(monkeypatch) -> None:
    import paho.mqtt.client as mqtt

    monkeypatch.setattr(mqtt, "Client", _FakeClient)
    api = IrrisenseApi("u", "p", "eu")
    api._identity_id = "id"
    api._iot_endpoint = "abc-ats.iot.eu-central-1.amazonaws.com"
    # No openid token/credentials -> _get_aws_credentials returns None.
    assert api.connect_mqtt() is False
