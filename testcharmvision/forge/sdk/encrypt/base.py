from abc import ABC, abstractmethod
from enum import Enum


class EncryptMethod(Enum):
    AES = "aes"


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
