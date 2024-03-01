import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any, Union

from jose import jwt

from skyvern.forge.sdk.settings_manager import SettingsManager

ALGORITHM = "HS256"


def create_access_token(
    subject: Union[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(
            minutes=SettingsManager.get_settings().ACCESS_TOKEN_EXPIRE_MINUTES,
        )
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(to_encode, SettingsManager.get_settings().SECRET_KEY, algorithm=ALGORITHM)
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
