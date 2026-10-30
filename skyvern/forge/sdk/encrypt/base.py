from abc import ABC, abstractmethod
from enum import Enum


class EncryptMethod(Enum):
    # AES-CBC with a fixed per-deployment IV; kept for decrypting rows written before AES_GCM.
    AES = "aes"
    AES_GCM = "aes_gcm"


DEFAULT_ENCRYPT_METHOD = EncryptMethod.AES_GCM


class TokenDecryptionError(Exception):
    """No configured key decrypts this ciphertext.

    Terminal by construction: the candidate key set is fixed for the life of the
    process, so a retry uses the same keys and fails identically. Callers that
    poll must stop rather than re-dial.
    """


class BaseEncryptor(ABC):
    @abstractmethod
    def method(self) -> EncryptMethod:
        pass

    @abstractmethod
    async def encrypt(self, plaintext: str) -> str:
        pass

    @abstractmethod
    async def decrypt(self, ciphertext: str) -> str:
        pass
