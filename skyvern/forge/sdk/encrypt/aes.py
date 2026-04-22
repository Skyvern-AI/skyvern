import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod

# key/salt path: md5 output is fed into PBKDF2HMAC-SHA256 (100k iterations) inside
# _derive_key(); md5 is only a string->16-byte normalizer, so usedforsecurity=False is
# honest. Changing the hash would invalidate every stored ciphertext, so the
# normalizer itself is locked-in until a re-key migration.
#
# iv path (default_iv / self.iv below): md5 output becomes the AES-CBC IV directly,
# so md5 *is* being used in a security-sensitive position there. But the real issue
# is architectural — the IV is deterministic (same input string -> same IV every
# time), which breaks AES-CBC semantic security. Swapping md5 for SHA-256 wouldn't
# fix that. Fix requires random per-encryption IVs stored alongside ciphertext,
# which is a breaking migration tracked separately. Those two md5() calls are left
# without usedforsecurity=False on purpose so the alert keeps surfacing as debt.
default_iv = hashlib.md5(b"deterministic_iv_0123456789").digest()
default_salt = hashlib.md5(b"deterministic_salt_0123456789", usedforsecurity=False).digest()


class AES(BaseEncryptor):
    def __init__(self, *, secret_key: str, salt: str | None = None, iv: str | None = None) -> None:
        self.secret_key = hashlib.md5(secret_key.encode("utf-8"), usedforsecurity=False).digest()
        self.salt = hashlib.md5(salt.encode("utf-8"), usedforsecurity=False).digest() if salt else default_salt
        self.iv = hashlib.md5(iv.encode("utf-8")).digest() if iv else default_iv

    def method(self) -> EncryptMethod:
        return EncryptMethod.AES

    def _derive_key(self) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.salt,
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
            key = self._derive_key()
            cipher = Cipher(algorithms.AES(key), modes.CBC(self.iv))
            decryptor = cipher.decryptor()
            padded_plaintext = decryptor.update(encrypted_data) + decryptor.finalize()
            plaintext = self._unpad(padded_plaintext)
            return plaintext.decode("utf-8")
        except Exception as e:
            raise Exception("Failed to decrypt token") from e

    def _pad(self, data: bytes) -> bytes:
        block_size = 16
        padding_length = block_size - (len(data) % block_size)
        padding = bytes([padding_length] * padding_length)
        return data + padding

    def _unpad(self, data: bytes) -> bytes:
        padding_length = data[-1]
        return data[:-padding_length]
