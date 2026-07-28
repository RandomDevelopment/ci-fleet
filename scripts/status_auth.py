#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
from typing import Mapping

AUTH_SCHEME = "CI-Fleet-HMAC-SHA256"


def canonical_request(controller: str, timestamp: int, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"POST\n/v1/status\n{controller}\n{timestamp}\n{nonce}\n{digest}".encode()


def sign_headers(controller: str, body: bytes, key: bytes, *, timestamp: int, nonce: str) -> dict[str, str]:
    signature = hmac.new(key, canonical_request(controller, timestamp, nonce, body), hashlib.sha256).hexdigest()
    return {
        "Authorization": f"{AUTH_SCHEME} {signature}",
        "Content-Type": "application/json",
        "X-CI-Fleet-Controller": controller,
        "X-CI-Fleet-Timestamp": str(timestamp),
        "X-CI-Fleet-Nonce": nonce,
    }


def verify_headers(headers: Mapping[str, str], body: bytes, key: bytes) -> tuple[str, int, str]:
    values = {name.lower(): value for name, value in headers.items()}
    controller = values.get("x-ci-fleet-controller", "")
    nonce = values.get("x-ci-fleet-nonce", "")
    try:
        timestamp = int(values.get("x-ci-fleet-timestamp", ""))
    except ValueError as error:
        raise ValueError("invalid authentication timestamp") from error
    authorization = values.get("authorization", "")
    prefix = f"{AUTH_SCHEME} "
    if not authorization.startswith(prefix):
        raise ValueError("missing authentication signature")
    expected = hmac.new(key, canonical_request(controller, timestamp, nonce, body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(authorization[len(prefix):], expected):
        raise ValueError("invalid authentication signature")
    return controller, timestamp, nonce
