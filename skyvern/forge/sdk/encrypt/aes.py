import base64
import hashlib
from collections.abc import Iterable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod

# md5 normalizes a string to 16 bytes here and is not asked to supply a security
# property. Every input is either a fixed public constant below or a 256-bit
# HMAC-SHA256 digest from bootstrap.py, so what makes a derived salt/IV unguessable
# is the secrecy of that input, not md5's collision resistance.
#
# The IV path does carry a real weakness, and it is not the hash: the IV is
# deterministic — one per deployment rather than one per message — which costs
# AES-CBC its semantic security. Fixing that needs random IVs carried in a
# versioned payload, and it is staged rather than done here because every running
# instance must be able to *read* the new format before any instance starts
# *writing* it. This change is that read step.
default_iv = hashlib.md5(b"deterministic_iv_0123456789", usedforsecurity=False).digest()
default_salt = hashlib.md5(b"deterministic_salt_0123456789", usedforsecurity=False).digest()


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
        self.salt, self.iv = self._encryption_params(salt, iv)
        self._fallback_decrypt_params = self._decrypt_fallbacks((self.salt, self.iv), salt, iv, fallback_decrypt_keys)

    def method(self) -> EncryptMethod:
        return EncryptMethod.AES

    @staticmethod
    def _encryption_params(salt: str | None, iv: str | None) -> tuple[bytes, bytes]:
        return (
            hashlib.md5(salt.encode("utf-8"), usedforsecurity=False).digest() if salt else default_salt,
            hashlib.md5(iv.encode("utf-8"), usedforsecurity=False).digest() if iv else default_iv,
        )

    @staticmethod
    def _sha256_params(salt: str | None, iv: str | None) -> tuple[bytes, bytes]:
        # Decrypt-only. Open-source releases normalized salt/IV with sha256 for a
        # window, so a deployment upgrading from one of those has stored ciphertext
        # only these parameters can open.
        return (
            hashlib.sha256(salt.encode("utf-8")).digest() if salt else default_salt,
            hashlib.sha256(iv.encode("utf-8")).digest()[:16] if iv else default_iv,
        )

    @classmethod
    def _decrypt_fallbacks(
        cls,
        primary: tuple[bytes, bytes],
        salt: str | None,
        iv: str | None,
        fallback_decrypt_keys: Iterable[tuple[str | None, str | None]] | None,
    ) -> list[tuple[bytes, bytes]]:
        candidates: list[tuple[bytes, bytes]] = []
        for pair_salt, pair_iv in [(salt, iv), *(fallback_decrypt_keys or [])]:
            for params in (cls._encryption_params(pair_salt, pair_iv), cls._sha256_params(pair_salt, pair_iv)):
                if params != primary and params not in candidates:
                    candidates.append(params)
        return candidates

    def _derive_key(self, salt: bytes | None = None) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt if salt is not None else self.salt,
            iterations=100000,
        )
        return kdf.derive(self.secret_key)

    async def encrypt(self, plaintext: str) -> str:
        try:
            key = self._derive_key()
            cipher = Cipher(algorithms.AES(key), modes.CBC(self.iv))
            encryptor = cipher.encryptor()
            padded_plaintext = self._pad(plaintext.encode("utf-8"))
            ciphertext = encryptor.update(padded_plaintext) + encryptor.finalize()
            return base64.b64encode(ciphertext).decode("utf-8")
        except Exception as e:
            raise Exception("Failed to encrypt token") from e

    async def decrypt(self, ciphertext: str) -> str:
        try:
            encrypted_data = base64.b64decode(ciphertext.encode("utf-8"))
        except Exception as e:
            raise Exception("Failed to decrypt token") from e

        candidates: list[tuple[bytes, bytes]] = [(self.salt, self.iv), *self._fallback_decrypt_params]
        last_error: Exception | None = None
        for salt, iv in candidates:
            try:
                key = self._derive_key(salt=salt)
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                padded_plaintext = decryptor.update(encrypted_data) + decryptor.finalize()
                plaintext = self._unpad(padded_plaintext)
                return plaintext.decode("utf-8")
            except Exception as e:
                last_error = e
                continue

        raise Exception("Failed to decrypt token") from last_error

    def _pad(self, data: bytes) -> bytes:
        block_size = 16
        padding_length = block_size - (len(data) % block_size)
        padding = bytes([padding_length] * padding_length)
        return data + padding

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
