"""AWS SigV4 signing for AWS IoT MQTT-over-WebSocket connections.

The AWSIoTPythonSDK used to hide this: connecting to AWS IoT over a WebSocket
requires a SigV4-signed ``wss://`` URL (service ``iotdevicegateway``). paho-mqtt
does not sign for us, so we build the presigned URL here.

This is the standard AWS SigV4 *query-string* signing flow, specialised for the
IoT data endpoint:

* method ``GET``, canonical URI ``/mqtt``, empty payload
* only the ``host`` header is signed
* the session token (temporary Cognito credentials) is appended **after**
  signing, exactly as AWS's reference implementation does.

Nothing here is Aiper-specific or reverse-engineered — it's plain AWS SigV4, so
it can be verified against AWS's published test vectors (see tests).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

_SERVICE = "iotdevicegateway"
_ALGORITHM = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def derive_signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key (the AWS ``getSignatureKey`` routine)."""
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def presign_iot_wss_url(
    endpoint: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
    *,
    amz_date: str | None = None,
    datestamp: str | None = None,
) -> str:
    """Return a SigV4-signed ``wss://<endpoint>/mqtt?...`` URL for AWS IoT.

    ``amz_date`` (``YYYYMMDDTHHMMSSZ``) and ``datestamp`` (``YYYYMMDD``) default
    to the current UTC time; they are injectable so tests are deterministic.
    """
    if amz_date is None or datestamp is None:
        now = time.gmtime()
        amz_date = amz_date or time.strftime("%Y%m%dT%H%M%SZ", now)
        datestamp = datestamp or time.strftime("%Y%m%d", now)

    credential_scope = f"{datestamp}/{region}/{_SERVICE}/aws4_request"

    # Canonical query string — params must be in sorted order. The four here are
    # already alphabetical (Algorithm, Credential, Date, SignedHeaders).
    canonical_querystring = (
        f"X-Amz-Algorithm={_ALGORITHM}"
        f"&X-Amz-Credential={quote(access_key + '/' + credential_scope, safe='')}"
        f"&X-Amz-Date={amz_date}"
        "&X-Amz-SignedHeaders=host"
    )

    canonical_headers = f"host:{endpoint}\n"
    signed_headers = "host"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(
        [
            "GET",
            "/mqtt",
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = derive_signing_key(secret_key, datestamp, region, _SERVICE)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    canonical_querystring += f"&X-Amz-Signature={signature}"
    # Temporary-credential session token is appended AFTER signing.
    if session_token:
        canonical_querystring += f"&X-Amz-Security-Token={quote(session_token, safe='')}"

    return f"wss://{endpoint}/mqtt?{canonical_querystring}"
