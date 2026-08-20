import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod

# The public defaults and fallback parameters retain the legacy MD5 normalization so
# existing ciphertext remains decryptable. New ciphertext configured with an explicit
# salt and IV uses SHA-256 normalization instead.
#
# The SHA-derived IV remains deterministic, matching the persisted ciphertext format.
# A future authenticated-encryption migration can introduce random nonces stored with
# each ciphertext; that requires a versioned payload and is separate from removing MD5
# from the primary encryption path here.
#
# Fallback candidates are decrypt-only. They let deployments read values written before
# this normalization change and naturally age out as those values are rewritten through
# the primary path.
default_iv = hashlib.md5(b"deterministic_iv_0123456789").digest()
default_salt = hashlib.md5(b"deterministic_salt_0123456789", usedforsecurity=False).digest()


class AES(BaseEncryptor):
    def __init__(
        self,
        *,
        secret_key: str,
        salt: str | None = None,
        iv: str | None = None,
        fallback_decrypt_keys: list[tuple[str | None, str | None]] | None = None,
    ) -> None:
        self.secret_key = hashlib.md5(secret_key.encode("utf-8"), usedforsecurity=False).digest()
        self.salt = hashlib.sha256(salt.encode("utf-8")).digest() if salt else default_salt
        self.iv = hashlib.sha256(iv.encode("utf-8")).digest()[:16] if iv else default_iv
        self._fallback_decrypt_params: list[tuple[bytes, bytes]] = [
            (
                hashlib.sha256(fb_salt.encode("utf-8")).digest() if fb_salt else default_salt,
                hashlib.sha256(fb_iv.encode("utf-8")).digest()[:16] if fb_iv else default_iv,
            )
            for fb_salt, fb_iv in (fallback_decrypt_keys or [])
        ]
        self._fallback_decrypt_params.extend(
            (
                hashlib.md5(fb_salt.encode("utf-8"), usedforsecurity=False).digest() if fb_salt else default_salt,
                hashlib.md5(fb_iv.encode("utf-8")).digest() if fb_iv else default_iv,
            )
            for fb_salt, fb_iv in (fallback_decrypt_keys or [])
        )

    def method(self) -> EncryptMethod:
        return EncryptMethod.AES

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
