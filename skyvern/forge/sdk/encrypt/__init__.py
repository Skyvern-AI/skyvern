from pydantic import BaseModel

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod, TokenDecryptionError


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

    async def decrypt_unlabeled(self, ciphertext: str) -> str:
        """Decrypt a value stored without its method, trying GCM first: its tag rejects CBC data, not vice versa."""
        if EncryptMethod.AES_GCM in self._methods:
            try:
                return await self._methods[EncryptMethod.AES_GCM].decrypt(ciphertext)
            except TokenDecryptionError:
                if EncryptMethod.AES not in self._methods:
                    raise
        return await self.decrypt(ciphertext, EncryptMethod.AES)


encryptor = Encryptor()
