from pydantic import BaseModel

from skyvern.config import settings
from skyvern.forge.sdk.encrypt.aes import AES
from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod


class Encryptor(BaseModel):
    def __init__(self) -> None:
        self._methods: dict[EncryptMethod, BaseEncryptor] = {}

    def add_encrypt_method(self, encrypt_method: BaseEncryptor) -> None:
        self._methods[encrypt_method.method()] = encrypt_method

    async def encrypt(self, plaintext: str, method: EncryptMethod) -> str:
        if method not in self._methods:
            raise ValueError(f"encrypt method not registered: {method}")

        return await self._methods[method].encrypt(plaintext)

    async def decrypt(self, ciphertext: str, method: EncryptMethod) -> str:
        if method not in self._methods:
            raise ValueError(f"encrypt method not registered: {method}")

        return await self._methods[method].decrypt(ciphertext)


encryptor = Encryptor()

encryptor.add_encrypt_method(
    AES(secret_key=settings.ENCRYPTOR_AES_SECRET_KEY, iv=settings.ENCRYPTOR_AES_IV, salt=settings.ENCRYPTOR_AES_SALT)
)
