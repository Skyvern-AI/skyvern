from pydantic import BaseModel

from skyvern.forge.sdk.encrypt.base import BaseEncryptor, EncryptMethod, TokenDecryptionError

VERSIONED_CIPHERTEXT_PREFIX = "skyvern_aes:"


class Encryptor(BaseModel):
    def __init__(self) -> None:
        self._methods: dict[EncryptMethod, BaseEncryptor] = {}

    def add_encrypt_method(self, encrypt_method: BaseEncryptor) -> None:
        self._methods[encrypt_method.method()] = encrypt_method

    async def encrypt(self, plaintext: str, method: EncryptMethod) -> str:
        if method is EncryptMethod.AES_GCM_V2:
            raise NotImplementedError("AES v2 writes are not supported")
        if method not in self._methods:
            raise ValueError(f"encrypt method not registered: {method}")

        return await self._methods[method].encrypt(plaintext)

    async def decrypt(self, ciphertext: str, method: EncryptMethod) -> str:
        if method not in self._methods:
            if method is EncryptMethod.AES_GCM_V2:
                raise TokenDecryptionError("Failed to decrypt token")
            raise ValueError(f"encrypt method not registered: {method}")

        return await self._methods[method].decrypt(ciphertext)

    async def decrypt_unlabeled(self, ciphertext: str) -> str:
        """Decrypt a value stored without its method, trying GCM first: its tag rejects CBC data, not vice versa."""
        if ciphertext.startswith(VERSIONED_CIPHERTEXT_PREFIX):
            method_value, separator, ciphertext = ciphertext[len(VERSIONED_CIPHERTEXT_PREFIX) :].partition(":")
            try:
                method = EncryptMethod(method_value)
            except ValueError:
                raise TokenDecryptionError("Failed to decrypt token") from None
            if not separator or not ciphertext or method not in (EncryptMethod.AES_GCM, EncryptMethod.AES_GCM_V2):
                raise TokenDecryptionError("Failed to decrypt token")
            return await self.decrypt(ciphertext, method)

        if EncryptMethod.AES_GCM in self._methods:
            try:
                return await self._methods[EncryptMethod.AES_GCM].decrypt(ciphertext)
            except TokenDecryptionError:
                if EncryptMethod.AES not in self._methods:
                    raise
        return await self.decrypt(ciphertext, EncryptMethod.AES)


encryptor = Encryptor()
