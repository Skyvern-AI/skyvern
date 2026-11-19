import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod, TokenDecryptionError

# Changing either label re-derives the key and invalidates ciphertext stored under that version.
_KEY_DERIVATION_INFO = {
    EncryptMethod.AES_GCM: b"skyvern.encryptor.aes_gcm.v1",
    EncryptMethod.AES_GCM_V2: b"skyvern.encryptor.aes_gcm.v2",
}
_NONCE_BYTES = 12


class AESGCMEncryptor(BaseEncryptor):
    """AES-256-GCM with a random 96-bit nonce per encryption, stored as base64(nonce || ciphertext || tag)."""

    def __init__(
        self,
        *,
        secret_key: str,
        salt: str | None,
        method: EncryptMethod = EncryptMethod.AES_GCM,
    ) -> None:
        if method not in _KEY_DERIVATION_INFO:
            raise ValueError(f"unsupported AES-GCM method: {method}")
        self._method = method
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt.encode("utf-8") if salt else None,
            info=_KEY_DERIVATION_INFO[method],
        ).derive(secret_key.encode("utf-8"))

    def method(self) -> EncryptMethod:
        return self._method

    async def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ciphertext).decode("utf-8")

    async def decrypt(self, ciphertext: str) -> str:
        try:
            data = base64.b64decode(ciphertext.encode("utf-8"), validate=True)
            nonce, sealed = data[:_NONCE_BYTES], data[_NONCE_BYTES:]
            return AESGCM(self._key).decrypt(nonce, sealed, None).decode("utf-8")
        except Exception:
            raise TokenDecryptionError("Failed to decrypt token") from None


class ScopedAESGCMEncryptor(BaseEncryptor):
    """Keep shared-first reads while writers switch independently during the environment split."""

    def __init__(self, shared: AESGCMEncryptor, scoped: AESGCMEncryptor, *, write_scoped: bool) -> None:
        self.shared = shared
        self.scoped = scoped
        self.writer = scoped if write_scoped else shared

    def method(self) -> EncryptMethod:
        return EncryptMethod.AES_GCM

    async def encrypt(self, plaintext: str) -> str:
        return await self.writer.encrypt(plaintext)

    async def decrypt(self, ciphertext: str) -> str:
        try:
            return await self.shared.decrypt(ciphertext)
        except TokenDecryptionError:
            pass
        return await self.scoped.decrypt(ciphertext)
