"""Regression tests for the Aiper AES-CBC + RSA request envelope.

These pin the *wire* behaviour of ``crypto.AiperEncryption`` byte-for-byte:
the compact JSON serialisation, the zero padding, the AES-CBC scheme, the
printable-byte key/IV alphabet, and the RSA-PKCS1v15 ``encryptKey`` header.
The whole envelope was reverse-engineered from the Aiper mobile app, so any
future refactor that changes these bytes changes what Aiper's cloud receives
— and must fail loudly here first.
"""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

from custom_components.aiper_irrisense import crypto
from custom_components.aiper_irrisense.crypto import AiperEncryption

# The four-char nonce alphabet, copied verbatim from the module so the test
# fails if the production set is ever narrowed/widened.
_NONCE_ALPHABET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()-_=+[]{}"
)


def _aes_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    return dec.update(ciphertext) + dec.finalize()


# --------------------------------------------------------------------------- #
# Public key constant — wire-critical, must never drift silently
# --------------------------------------------------------------------------- #


def test_public_key_constant_is_pinned() -> None:
    # The exact DER-base64 the mobile app uses. If Aiper rotates their key we
    # WANT this to fail so the change is a conscious one.
    assert crypto.PUBLIC_KEY_STRING == (
        "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCIKoKPqwq1f60hm/2lpHDF/DT4J9YaptuTq78nsxdgnSBAvkIZ3E8d"
        "qbEBT/VETjJ9Yr28QtHX13E8QGByYxLzYPldHNXChgOWfSemTEC3TxPvlaSuM9eFUuhqSeGbgoKG7JJNlgjvsPO2cH"
        "EhPXJE4qWtKEZVOZBxEeCgAaLZxwIDAQAB"
    )


def test_public_key_is_rsa_1024() -> None:
    pub = load_der_public_key(base64.b64decode(crypto.PUBLIC_KEY_STRING))
    assert pub.key_size == 1024


# --------------------------------------------------------------------------- #
# Key / IV generation
# --------------------------------------------------------------------------- #


def test_key_and_iv_are_16_printable_bytes() -> None:
    enc = AiperEncryption()
    assert len(enc.aes_key) == 16
    assert len(enc.iv) == 16
    # The Android app samples printable-ish bytes in [40, 127).
    assert all(40 <= b < 127 for b in enc.aes_key)
    assert all(40 <= b < 127 for b in enc.iv)


def test_nonce_shape() -> None:
    for _ in range(50):
        nonce = AiperEncryption._nonce()
        assert len(nonce) == 4
        assert set(nonce) <= _NONCE_ALPHABET


# --------------------------------------------------------------------------- #
# encryptKey RSA header
# --------------------------------------------------------------------------- #


def test_encrypt_key_header_is_rsa1024_block() -> None:
    enc = AiperEncryption()
    raw = base64.b64decode(enc.encrypt_key_header)
    # RSA-1024 PKCS1v15 encryption of a short payload => one 128-byte block.
    assert len(raw) == 128


def test_encrypt_key_header_randomised_per_call() -> None:
    # PKCS1v15 padding is randomised, so two instances (different key/iv AND
    # different padding) must not collide.
    assert AiperEncryption().encrypt_key_header != AiperEncryption().encrypt_key_header


# --------------------------------------------------------------------------- #
# Zero padding
# --------------------------------------------------------------------------- #


def test_zero_pad_leaves_block_multiple_untouched() -> None:
    data = b"x" * 16
    assert AiperEncryption._zero_pad(data) == data
    assert AiperEncryption._zero_pad(b"x" * 32) == b"x" * 32


def test_zero_pad_fills_to_next_block_with_nulls() -> None:
    assert AiperEncryption._zero_pad(b"x" * 15) == b"x" * 15 + b"\x00"
    assert AiperEncryption._zero_pad(b"x" * 17) == b"x" * 17 + b"\x00" * 15


def test_zero_unpad_strips_trailing_nulls_only() -> None:
    assert AiperEncryption._zero_unpad(b"payload\x00\x00\x00") == b"payload"
    # Interior nulls are preserved; only the trailing run is stripped.
    assert AiperEncryption._zero_unpad(b"a\x00b\x00") == b"a\x00b"


# --------------------------------------------------------------------------- #
# encrypt_request — byte-for-byte wire format
# --------------------------------------------------------------------------- #


def test_encrypt_request_exact_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the exact ciphertext independently and assert equality.

    This is the core regression: it pins the compact ``(",", ":")`` JSON
    separators, the field order (body keys, then ``nonce``, then
    ``timestamp``), the zero padding, and the AES-CBC scheme all at once.
    """
    enc = AiperEncryption()
    # Freeze the two sources of non-determinism inside encrypt_request.
    monkeypatch.setattr(AiperEncryption, "_nonce", staticmethod(lambda: "ABCD"))
    monkeypatch.setattr(crypto.time, "time", lambda: 1_700_000_000.0)

    body = {"email": "user@example.com", "password": "s3cr3t"}
    out = enc.encrypt_request(body)

    # Independently rebuild what should have gone on the wire.
    expected_plain = json.dumps(
        {**body, "nonce": "ABCD", "timestamp": 1_700_000_000_000},
        separators=(",", ":"),
    ).encode("utf-8")
    pad_len = 16 - (len(expected_plain) % 16)
    if pad_len != 16:
        expected_plain += b"\x00" * pad_len
    cipher = Cipher(algorithms.AES(enc.aes_key), modes.CBC(enc.iv))
    e = cipher.encryptor()
    expected_ct = e.update(expected_plain) + e.finalize()
    expected = json.dumps({"data": base64.b64encode(expected_ct).decode("utf-8")})

    assert out == expected


def test_encrypt_request_envelope_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    enc = AiperEncryption()
    monkeypatch.setattr(AiperEncryption, "_nonce", staticmethod(lambda: "ABCD"))
    monkeypatch.setattr(crypto.time, "time", lambda: 1_700_000_000.0)

    envelope = json.loads(enc.encrypt_request({"k": "v"}))
    # Only a single "data" key goes on the wire.
    assert list(envelope.keys()) == ["data"]
    ct = base64.b64decode(envelope["data"])
    # AES-CBC ciphertext is always a whole number of 16-byte blocks.
    assert len(ct) % 16 == 0


def test_encrypt_request_adds_nonce_and_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    enc = AiperEncryption()
    monkeypatch.setattr(AiperEncryption, "_nonce", staticmethod(lambda: "WXYZ"))
    monkeypatch.setattr(crypto.time, "time", lambda: 1_700_000_000.5)

    envelope = json.loads(enc.encrypt_request({"email": "a"}))
    plain = _aes_decrypt(enc.aes_key, enc.iv, base64.b64decode(envelope["data"]))
    recovered = json.loads(plain.rstrip(b"\x00"))
    assert recovered["email"] == "a"
    assert recovered["nonce"] == "WXYZ"
    # int(time.time() * 1000): fractional seconds truncate toward zero.
    assert recovered["timestamp"] == 1_700_000_000_500


def test_encrypt_request_does_not_mutate_caller_body() -> None:
    enc = AiperEncryption()
    body = {"email": "a"}
    enc.encrypt_request(body)
    assert body == {"email": "a"}  # nonce/timestamp added to a copy only


# --------------------------------------------------------------------------- #
# decrypt_response
# --------------------------------------------------------------------------- #


def test_decrypt_response_passthrough_for_plain_json() -> None:
    enc = AiperEncryption()
    payload = '{"code":"0","msg":"ok"}'
    assert enc.decrypt_response(payload) == payload


def test_decrypt_response_passthrough_for_empty() -> None:
    enc = AiperEncryption()
    assert enc.decrypt_response("") == ""


def test_decrypt_response_decrypts_ciphertext_roundtrip() -> None:
    enc = AiperEncryption()
    plaintext = '{"data":{"token":"abc"},"code":"0"}'
    padded = enc._zero_pad(plaintext.encode("utf-8"))
    cipher = Cipher(algorithms.AES(enc.aes_key), modes.CBC(enc.iv))
    e = cipher.encryptor()
    ct = e.update(padded) + e.finalize()
    b64 = base64.b64encode(ct).decode("utf-8")

    assert enc.decrypt_response(b64) == plaintext


def test_full_request_response_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """encrypt_request → (inner ciphertext) → decrypt_response recovers body."""
    enc = AiperEncryption()
    monkeypatch.setattr(AiperEncryption, "_nonce", staticmethod(lambda: "ABCD"))
    monkeypatch.setattr(crypto.time, "time", lambda: 1_700_000_000.0)

    body = {"email": "user@example.com", "password": "p"}
    inner_b64 = json.loads(enc.encrypt_request(body))["data"]
    recovered = json.loads(enc.decrypt_response(inner_b64))
    assert recovered == {**body, "nonce": "ABCD", "timestamp": 1_700_000_000_000}
