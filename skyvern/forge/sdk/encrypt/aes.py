"""Decrypt-only reader for ciphertext stored under ``EncryptMethod.AES`` (AES-256-CBC).

CBC writes are retired: every new value is encrypted by ``AESGCMEncryptor``. This class stays so deployments that still
hold rows written before the AES-GCM migration can read them.
"""

import base64
import hashlib
from collections.abc import Iterable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod, TokenDecryptionError

# Rows written while ENCRYPTOR_AES_SALT / ENCRYPTOR_AES_IV were unset, before salt/IV were derived from the secret
# key, used these public values. md5 is only the retired writer's string-to-16-byte normalizer; changing any derivation
# here strands every stored CBC row.
_LEGACY_DEFAULT_SALT = hashlib.md5(b"deterministic_salt_0123456789", usedforsecurity=False).digest()
_LEGACY_DEFAULT_IV = hashlib.md5(b"deterministic_iv_0123456789", usedforsecurity=False).digest()


def _legacy_cbc_salt(salt: str | None) -> bytes:
    return hashlib.md5(salt.encode("utf-8"), usedforsecurity=False).digest() if salt else _LEGACY_DEFAULT_SALT


def _legacy_cbc_iv(iv: str | None) -> bytes:
    # Read-only compatibility: rows written before the GCM migration were encrypted under this fixed IV, so reading
    # them needs the same bytes. Nothing encrypts with it.
    return hashlib.md5(iv.encode("utf-8"), usedforsecurity=False).digest() if iv else _LEGACY_DEFAULT_IV


class AES(BaseEncryptor):
    def __init__(
        self,
        *,
        secret_key: str,
        salt: str | None = None,
        iv: str | None = None,
        fallback_decrypt_keys: Iterable[tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.secret_key = hashlib.md5(secret_key.encode("utf-8"), usedforsecurity=False).digest()
        self._decrypt_parameters: list[tuple[str | None, str | None]] = [(salt, iv), *(fallback_decrypt_keys or [])]
        # PBKDF2 blocks the event loop for tens of ms and its output depends only on secret and salt, so derive once
        # per salt. Filled on first use rather than here so processes that never decrypt don't pay for it at boot.
        self._derived_keys: dict[bytes, bytes] = {}

    def method(self) -> EncryptMethod:
        return EncryptMethod.AES

    def _derive_key(self, salt: bytes) -> bytes:
        key = self._derived_keys.get(salt)
        if key is None:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = kdf.derive(self.secret_key)
            self._derived_keys[salt] = key
        return key

    async def encrypt(self, plaintext: str) -> str:
        raise NotImplementedError("AES-CBC writes are retired; use AES-GCM")

    async def decrypt(self, ciphertext: str) -> str:
        try:
            encrypted_data = base64.b64decode(ciphertext.encode("utf-8"), validate=True)
        except Exception:
            raise TokenDecryptionError("Failed to decrypt token") from None

        for salt, iv in self._decrypt_parameters:
            try:
                key = self._derive_key(_legacy_cbc_salt(salt))
                cipher = Cipher(algorithms.AES(key), modes.CBC(_legacy_cbc_iv(iv)))
                decryptor = cipher.decryptor()
                padded_plaintext = decryptor.update(encrypted_data) + decryptor.finalize()
                plaintext = self._unpad(padded_plaintext)
                return plaintext.decode("utf-8")
            except Exception:
                continue

        # Never chain the candidate failure: padding and key errors must not distinguish one cause from another.
        raise TokenDecryptionError("Failed to decrypt token") from None

    def _unpad(self, data: bytes) -> bytes:
        # Strict PKCS#7 validation. Rejecting malformed trailers is what lets the
        # multi-key decrypt loop in ``decrypt`` distinguish wrong-key garbage from
        # a legitimate plaintext when no AEAD/HMAC is in play.
        block_size = 16
        if not data:
            raise ValueError("invalid padding: empty data")
        padding_length = data[-1]
        if padding_length < 1 or padding_length > block_size or padding_length > len(data):
            raise ValueError("invalid padding: length out of range")
        if data[-padding_length:] != bytes([padding_length] * padding_length):
            raise ValueError("invalid padding: trailer mismatch")
        return data[:-padding_length]
