"""Outbound webhook delivery.

Every outbound call goes through `post_json`, which keeps TLS verification on,
sets a timeout, and never logs the signing secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

import requests

log = logging.getLogger(__name__)

WEBHOOK_TIMEOUT_S = 10


def _signature(payload: bytes) -> str:
    secret = os.environ["WEBHOOK_SIGNING_SECRET"].encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def post_json(url: str, payload: dict[str, Any]) -> int:
    body = json.dumps(payload, sort_keys=True).encode()
    response = requests.post(
        url,
        data=body,
        headers={"content-type": "application/json", "x-signature": _signature(body)},
        timeout=WEBHOOK_TIMEOUT_S,
    )
    log.info("webhook delivered url=%s status=%s", url, response.status_code)
    return response.status_code


def notify_report_submitted(url: str, report_id: int) -> int:
    return post_json(url, {"event": "report.submitted", "report_id": report_id})
