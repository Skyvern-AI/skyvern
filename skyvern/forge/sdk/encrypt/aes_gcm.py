import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod, TokenDecryptionError

# Changing the label re-derives the key and invalidates every stored aes_gcm ciphertext.
_KEY_DERIVATION_INFO = b"skyvern.encryptor.aes_gcm.v1"
_NONCE_BYTES = 12


class AESGCMEncryptor(BaseEncryptor):
    """AES-256-GCM with a random 96-bit nonce per encryption, stored as base64(nonce || ciphertext || tag)."""

    def __init__(self, *, secret_key: str, salt: str | None) -> None:
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt.encode("utf-8") if salt else None,
            info=_KEY_DERIVATION_INFO,
        ).derive(secret_key.encode("utf-8"))

    def method(self) -> EncryptMethod:
        return EncryptMethod.AES_GCM

    async def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ciphertext).decode("utf-8")

    async def decrypt(self, ciphertext: str) -> str:
        try:
            data = base64.b64decode(ciphertext.encode("utf-8"), validate=True)
            nonce, sealed = data[:_NONCE_BYTES], data[_NONCE_BYTES:]
            return AESGCM(self._key).decrypt(nonce, sealed, None).decode("utf-8")
        except Exception as e:
            raise TokenDecryptionError("Failed to decrypt token") from e
