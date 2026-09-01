from abc import ABC, abstractmethod
from enum import Enum


class EncryptMethod(Enum):
    AES = "aes"


class TokenDecryptionError(Exception):
    """No configured key decrypts this ciphertext.

    Terminal by construction: the candidate key set is fixed for the life of the
    process, so a retry re-derives the same keys and fails identically. Callers that
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
