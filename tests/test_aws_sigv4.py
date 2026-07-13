"""Tests for the AWS SigV4 presigner used for MQTT-over-WebSocket.

The signing-key derivation is checked against AWS's own published vector (the
"deriving the signing key" example from the SigV4 documentation), which anchors
the crypto core to a known-good external value. The URL assembly is then checked
structurally and for determinism.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from custom_components.aiper_irrisense.aws_sigv4 import (
    derive_signing_key,
    presign_iot_wss_url,
)


def test_derive_signing_key_matches_aws_published_vector() -> None:
    # AWS SigV4 docs, "Deriving the signing key" worked example.
    key = derive_signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "20120215",
        "us-east-1",
        "iam",
    )
    assert key.hex() == (
        "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
    )


def _sign(**kw):
    return presign_iot_wss_url(
        endpoint="abc123-ats.iot.eu-central-1.amazonaws.com",
        region="eu-central-1",
        access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        amz_date="20240101T000000Z",
        datestamp="20240101",
        **kw,
    )


def test_presign_url_structure() -> None:
    url = _sign(session_token="SESSION/TOKEN+withspecials==")
    parsed = urlparse(url)
    assert parsed.scheme == "wss"
    assert parsed.hostname == "abc123-ats.iot.eu-central-1.amazonaws.com"
    assert parsed.path == "/mqtt"

    q = parse_qs(parsed.query)
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-SignedHeaders"] == ["host"]
    assert q["X-Amz-Date"] == ["20240101T000000Z"]
    # Credential is scoped to the iotdevicegateway service.
    assert q["X-Amz-Credential"][0].endswith(
        "/20240101/eu-central-1/iotdevicegateway/aws4_request"
    )
    # Signature is a 64-char hex digest.
    sig = q["X-Amz-Signature"][0]
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
    # Session token is present and url-decoded back to the original.
    assert q["X-Amz-Security-Token"] == ["SESSION/TOKEN+withspecials=="]


def test_presign_without_session_token_omits_it() -> None:
    url = _sign()
    assert "X-Amz-Security-Token" not in url
    # Security token must never appear in the signed portion.
    assert url.index("X-Amz-Signature") > url.index("X-Amz-SignedHeaders")


def test_presign_is_deterministic_for_fixed_inputs() -> None:
    assert _sign(session_token="T") == _sign(session_token="T")


def test_signature_changes_with_credentials() -> None:
    a = _sign()
    b = presign_iot_wss_url(
        endpoint="abc123-ats.iot.eu-central-1.amazonaws.com",
        region="eu-central-1",
        access_key="AKIDEXAMPLE",
        secret_key="a-different-secret-key",
        amz_date="20240101T000000Z",
        datestamp="20240101",
    )
    assert a != b
