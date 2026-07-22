import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Union
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import jwt

from skyvern.config import settings


def _normalize_numbers(x: Any) -> Any:
    if isinstance(x, float):
        return int(x) if x.is_integer() else x
    if isinstance(x, dict):
        return {k: _normalize_numbers(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_normalize_numbers(v) for v in x]
    return x


def _normalize_json_dumps(payload: dict) -> str:
    return json.dumps(_normalize_numbers(payload), separators=(",", ":"), ensure_ascii=False)


def create_access_token(
    subject: Union[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        )
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.SIGNATURE_ALGORITHM,
    )
    return encoded_jwt


def generate_skyvern_signature(
    payload: str,
    api_key: str,
) -> str:
    """
    Generate Skyvern signature.

    :param payload: the request body
    :param api_key: the Skyvern api key

    :return: the Skyvern signature
    """
    hash_obj = hmac.new(api_key.encode("utf-8"), msg=payload.encode("utf-8"), digestmod=hashlib.sha256)
    return hash_obj.hexdigest()


MAX_WEBHOOK_PAYLOAD_LOG_SIZE = 8000  # ~8KB – keeps Datadog log entries manageable

# Any absolute http(s) URL token, up to the next whitespace/quote/angle bracket
# (a JSON string can't contain a raw " or \), so an embedded URL is matched too,
# not only a whole-value URL. Case-insensitive to catch uppercase schemes.
_URL_TOKEN_RE = re.compile(r"https?://[^\s\"\\<>]+", re.IGNORECASE)
# Query-param names whose presence marks a URL as credentialed: Skyvern HMAC
# signing (sig), and storage presigns (S3 X-Amz-Signature/Credential/Security-Token,
# GCS X-Goog-Signature, Azure SAS sig). Any match strips the whole query.
_URL_SIGNATURE_PARAMS = frozenset(
    {"sig", "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-goog-signature"}
)


def _strip_credentialed_url_query(match: "re.Match[str]") -> str:
    url = match.group(0)
    try:
        parsed = urlsplit(url)
    except ValueError:
        # Fail closed: an unparseable URL keeps only its pre-query/fragment prefix.
        return re.split(r"[?#]", url, maxsplit=1)[0]
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if not query_keys & _URL_SIGNATURE_PARAMS:
        return url
    netloc = parsed.netloc.rsplit("@", 1)[-1]  # drop any basic-auth userinfo
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def redact_credentialed_urls(text: str) -> str:
    """Strip signing/presign query params from any credentialed URL in a string.

    Covers Skyvern signed content URLs and S3/Azure/GCS presigns, including URLs embedded in a
    larger value; non-credentialed URLs and surrounding text are untouched. For log-only copies.
    """
    return _URL_TOKEN_RE.sub(_strip_credentialed_url_query, text)


@dataclass
class WebhookSignature:
    timestamp: str
    signature: str
    signed_payload: str
    headers: dict[str, str]
    # URL-credential-redacted, truncated copy of the payload — safe for logging.
    payload_for_log: str


def generate_skyvern_webhook_signature(payload: dict, api_key: str) -> WebhookSignature:
    payload_str = _normalize_json_dumps(payload)
    signature = generate_skyvern_signature(payload=payload_str, api_key=api_key)
    timestamp = str(int(datetime.utcnow().timestamp()))
    # Redact signed/presigned URL params before truncating so the log copy never
    # carries replayable credentials. signed_payload stays raw so delivery and the
    # HMAC signature above are unaffected.
    log_payload = redact_credentialed_urls(payload_str)
    if len(log_payload) > MAX_WEBHOOK_PAYLOAD_LOG_SIZE:
        payload_for_log = (
            log_payload[:MAX_WEBHOOK_PAYLOAD_LOG_SIZE] + f"... (truncated, original size: {len(payload_str)})"
        )
    else:
        payload_for_log = log_payload
    return WebhookSignature(
        timestamp=timestamp,
        signature=signature,
        signed_payload=payload_str,
        headers={
            "x-skyvern-timestamp": timestamp,
            "x-skyvern-signature": signature,
            "Content-Type": "application/json",
        },
        payload_for_log=payload_for_log,
    )
