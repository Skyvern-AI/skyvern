import hashlib
import hmac

from skyvern.config import settings
from skyvern.forge.sdk.encrypt import VERSIONED_CIPHERTEXT_PREFIX, encryptor
from skyvern.forge.sdk.encrypt.base import DEFAULT_ENCRYPT_METHOD

_TOTP_CODE_DIGEST_DOMAIN = b"totp-code-digest-v1"


async def encrypt_totp_field(value: str) -> str:
    ciphertext = await encryptor.encrypt(value, DEFAULT_ENCRYPT_METHOD)
    return f"{VERSIONED_CIPHERTEXT_PREFIX}{DEFAULT_ENCRYPT_METHOD.value}:{ciphertext}"


async def decrypt_totp_field(value: str) -> str:
    return await encryptor.decrypt_unlabeled(value)


def totp_code_digest(organization_id: str, code: str) -> str:
    key = hmac.new(settings.SECRET_KEY.encode("utf-8"), _TOTP_CODE_DIGEST_DOMAIN, hashlib.sha256).digest()
    return hmac.new(key, f"{organization_id}|{code}".encode(), hashlib.sha256).hexdigest()
