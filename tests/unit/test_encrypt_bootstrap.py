import base64
import traceback
from pathlib import Path
from typing import Iterator

import pytest
from pydantic import SecretStr, ValidationError

from skyvern.config import Settings, settings
from skyvern.forge.sdk.encrypt import VERSIONED_CIPHERTEXT_PREFIX, encryptor
from skyvern.forge.sdk.encrypt import files as file_crypto
from skyvern.forge.sdk.encrypt.aes_gcm import AESGCMEncryptor
from skyvern.forge.sdk.encrypt.base import DEFAULT_ENCRYPT_METHOD, EncryptMethod, TokenDecryptionError
from skyvern.forge.sdk.encrypt.bootstrap import (
    _SALT_DERIVATION_LABEL,
    PLACEHOLDER_SECRET_KEY,
    _derive_from_secret_key,
    register_aes_encryptor,
)
from tests.unit.forge.sdk.artifact.storage.test_helpers import artifact_root

SECRET_KEY = "self-hosted-secret-key-000"
V2_SECRET_KEY = "self-hosted-v2-secret-key-000"
V2_SALT = "self-hosted-v2-salt-000"
# "refresh-token" as the retired CBC writer stored it under SECRET_KEY with each kind of salt/IV a deployment
# could hold.
LEGACY_PUBLIC_DEFAULT_CIPHERTEXT = "TmO1Ph6RwR1dVoSl7iLZ9g=="
LEGACY_EXPLICIT_CIPHERTEXT = "wPXE7jBaF8hk9AZgVPP6rQ=="
# Written under the salt/IV derived from SECRET_KEY while ENCRYPTOR_AES_SALT / ENCRYPTOR_AES_IV were unset. Only the
# derivation reads it (no fallback does), so a derivation change fails here.
LEGACY_DERIVED_CIPHERTEXT = "HiAexXnV+9rKTiIGzNrrig=="


@pytest.fixture(autouse=True)
def isolated_encrypt_methods() -> Iterator[None]:
    original = dict(encryptor._methods)
    encryptor._methods = {}
    try:
        yield
    finally:
        encryptor._methods = original


@pytest.fixture
def v2_fixture_encryptor() -> AESGCMEncryptor:
    return AESGCMEncryptor(secret_key=V2_SECRET_KEY, salt=V2_SALT, method=EncryptMethod.AES_GCM_V2)


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        ({"secret_key": ""}, "ENCRYPTOR_AES_SECRET_KEY"),
        ({"secret_key": PLACEHOLDER_SECRET_KEY}, "ENCRYPTOR_AES_SECRET_KEY"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": V2_SECRET_KEY}, "configured together"),
        ({"secret_key": SECRET_KEY, "v2_salt": V2_SALT}, "configured together"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": "", "v2_salt": ""}, "configured together"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": PLACEHOLDER_SECRET_KEY, "v2_salt": V2_SALT}, "placeholder"),
        ({"secret_key": SECRET_KEY, "v2_secret_key": SECRET_KEY, "v2_salt": V2_SALT}, "must differ from v1"),
        (
            {"secret_key": SECRET_KEY, "salt": V2_SALT, "v2_secret_key": V2_SECRET_KEY, "v2_salt": V2_SALT},
            "must differ from v1",
        ),
        (
            {
                "secret_key": SECRET_KEY,
                "v2_secret_key": V2_SECRET_KEY,
                "v2_salt": _derive_from_secret_key(SECRET_KEY, _SALT_DERIVATION_LABEL),
            },
            "must differ from v1",
        ),
        ({"secret_key": SECRET_KEY, "write_version": "v2"}, "WRITE_VERSION=v1"),
    ],
)
def test_register_aes_encryptor_rejects_unusable_configuration(configuration: dict[str, str], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        register_aes_encryptor(
            secret_key=configuration["secret_key"],
            salt=configuration.get("salt"),
            v2_secret_key=configuration.get("v2_secret_key"),
            v2_salt=configuration.get("v2_salt"),
            write_version=configuration.get("write_version", "v1"),
        )

    assert not encryptor._methods


@pytest.mark.asyncio
async def test_default_method_round_trips_and_other_versions_are_read_only(
    v2_fixture_encryptor: AESGCMEncryptor,
) -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, v2_secret_key=V2_SECRET_KEY, v2_salt=V2_SALT)

    assert DEFAULT_ENCRYPT_METHOD is EncryptMethod.AES_GCM
    ciphertext = await encryptor.encrypt("refresh-token", DEFAULT_ENCRYPT_METHOD)
    v2_ciphertext = await v2_fixture_encryptor.encrypt("refresh-token-v2")
    assert ciphertext != "refresh-token"
    assert await encryptor.decrypt(ciphertext, DEFAULT_ENCRYPT_METHOD) == "refresh-token"
    assert await encryptor.decrypt(v2_ciphertext, EncryptMethod.AES_GCM_V2) == "refresh-token-v2"
    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(v2_ciphertext, EncryptMethod.AES_GCM)

    with pytest.raises(NotImplementedError, match="AES-GCM"):
        await encryptor.encrypt("refresh-token", EncryptMethod.AES)
    with pytest.raises(NotImplementedError, match="AES v2 writes"):
        await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM_V2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "salt", "iv", "ciphertext", "readable"),
    [
        (SECRET_KEY, None, None, LEGACY_PUBLIC_DEFAULT_CIPHERTEXT, True),
        (SECRET_KEY, "legacy-salt", None, "HWfEfJWYbO0JqAYUam2rwQ==", True),
        (SECRET_KEY, None, "legacy-iv", "GdJIuflCrG26sExLEulc+g==", True),
        (SECRET_KEY, "explicit-salt", "explicit-iv", LEGACY_EXPLICIT_CIPHERTEXT, True),
        (SECRET_KEY, None, None, LEGACY_DERIVED_CIPHERTEXT, True),
        (SECRET_KEY, None, None, LEGACY_EXPLICIT_CIPHERTEXT, False),
        (f"{SECRET_KEY}-other", None, None, LEGACY_PUBLIC_DEFAULT_CIPHERTEXT, False),
    ],
)
async def test_legacy_parameters(key, salt, iv, ciphertext, readable):
    register_aes_encryptor(secret_key=key, salt=salt, iv=iv)
    if readable:
        assert await encryptor.decrypt(ciphertext, EncryptMethod.AES) == "refresh-token"
    else:
        with pytest.raises(TokenDecryptionError):
            await encryptor.decrypt(ciphertext, EncryptMethod.AES)


@pytest.mark.asyncio
async def test_register_aes_encryptor_adds_gcm_with_a_fresh_nonce_per_encryption() -> None:
    register_aes_encryptor(secret_key=SECRET_KEY)

    assert EncryptMethod.AES_GCM_V2 not in encryptor._methods
    first = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)
    second = await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM)

    with pytest.raises(TokenDecryptionError):
        await AESGCMEncryptor(secret_key=SECRET_KEY, salt=None).decrypt(first)
    assert first != second
    assert await encryptor.decrypt(first, EncryptMethod.AES_GCM) == "refresh-token"
    assert await encryptor.decrypt(second, EncryptMethod.AES_GCM) == "refresh-token"


def _assert_artifact_file_encryption(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, sealed, restored = (tmp_path / name for name in ("source", "sealed", "restored"))
    identity = {"organization_id": "org_test", "purpose": "binary-test", "object_id": "file_test"}
    root_a, root_b = artifact_root(), artifact_root(66)
    monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (root_a,))
    monkeypatch.setattr(settings, "SKYVERN_ENCRYPT_BROWSER_PROFILES", False)
    if case == "wrong-root-shape":
        marker = "synthetic-root-that-must-not-appear"
        with pytest.raises(ValidationError, match="SKYVERN_ARTIFACT_ENCRYPTION_KEYS") as error:
            Settings(_env_file=None, SKYVERN_ARTIFACT_ENCRYPTION_KEYS={"unexpected": marker})
        assert marker not in str(error.value)
        assert marker not in "".join(traceback.format_exception(error.value))
        return
    if case.startswith("writer-"):
        keys = {
            "writer-missing": (),
            "writer-malformed": (SecretStr("!"),),
            "writer-short": (SecretStr(base64.b64encode(b"a").decode()),),
            "writer-valid": (root_a, root_b),
        }[case]
        if case == "writer-valid":
            writer = Settings(
                _env_file=None, SKYVERN_ENCRYPT_BROWSER_PROFILES=True, SKYVERN_ARTIFACT_ENCRYPTION_KEYS=keys
            )
            assert writer.artifact_encryption_roots() == (b"A" * 32, b"B" * 32)
        else:
            with pytest.raises(RuntimeError, match="SKYVERN_ARTIFACT_ENCRYPTION_KEYS"):
                Settings(_env_file=None, SKYVERN_ENCRYPT_BROWSER_PROFILES=True, SKYVERN_ARTIFACT_ENCRYPTION_KEYS=keys)
        return
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SECRET_KEY", SECRET_KEY)
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SALT", "credential-fixture-salt")
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SCOPED_SECRET_KEY", "credential-scoped-fixture-key")
    payload = bytes(range(256)) * (65537 if case == "multi-buffer" else 1)
    source.write_bytes(b"" if case == "empty" else payload)

    file_crypto.encrypt_file(source, sealed, **identity)
    original = sealed.read_bytes()
    assert original != source.read_bytes()
    restored.write_bytes(b"previous output")
    if case == "scoped":
        # Readers receive B first, then writers change the active root. Credential settings can rotate
        # or disappear on a browser-only worker without stranding either artifact.
        monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (root_a, root_b))
        file_crypto.decrypt_file(sealed, source, **identity)
        monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (root_b, root_a))
        monkeypatch.setattr(settings, "ENCRYPTOR_AES_SECRET_KEY", "rotated-credential-key")
        file_crypto.encrypt_file(source, restored, **identity)
        second = restored.read_bytes()
        assert original[9:41] != second[9:41]
        monkeypatch.setattr(settings, "ENCRYPTOR_AES_SECRET_KEY", PLACEHOLDER_SECRET_KEY)
        monkeypatch.setattr(settings, "ENCRYPTOR_AES_SALT", None)
        monkeypatch.setattr(settings, "ENCRYPTOR_AES_SCOPED_SECRET_KEY", None)
        for encrypted in (sealed, restored):
            file_crypto.decrypt_file(encrypted, source, **identity)
            assert source.read_bytes() == payload
        monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (root_b,))
        with pytest.raises(file_crypto.ArtifactDecryptionError, match="No configured artifact root"):
            file_crypto.decrypt_file(sealed, source, **identity)
        assert source.read_bytes() == payload
        file_crypto.decrypt_file(restored, source, **identity)
        assert source.read_bytes() == payload
        return
    if case in {"empty", "binary", "multi-buffer"}:
        file_crypto.decrypt_file(sealed, restored, **identity)
        assert restored.read_bytes() == source.read_bytes()
        return
    if case == "size-limit":
        assert file_crypto.MAX_PLAINTEXT_BYTES == (1 << 36) - 32
        monkeypatch.setattr(file_crypto, "MAX_PLAINTEXT_BYTES", len(payload))
        file_crypto.encrypt_file(source, sealed, **identity)
        original = sealed.read_bytes()
        monkeypatch.setattr(file_crypto, "MAX_PLAINTEXT_BYTES", len(payload) - 1)
        with pytest.raises(ValueError, match="size limit"):
            file_crypto.encrypt_file(source, sealed, **identity)
        assert sealed.read_bytes() == original
    elif case == "missing-key":
        monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", ())
        with pytest.raises(RuntimeError, match="SKYVERN_ARTIFACT_ENCRYPTION_KEYS"):
            file_crypto.encrypt_file(source, sealed, **identity)
        assert sealed.read_bytes() == original
        return
    elif case == "wrong-key":
        monkeypatch.setattr(settings, "SKYVERN_ARTIFACT_ENCRYPTION_KEYS", (root_b,))
    elif case in {"organization_id", "purpose", "object_id"}:
        identity[case] += "-other"
    elif case in {"header", "tag", "ciphertext"}:
        tampered = bytearray(original)
        tampered[{"header": len(file_crypto.FILE_MAGIC), "tag": -1, "ciphertext": len(tampered) // 2}[case]] ^= 1
        sealed.write_bytes(tampered)
    elif case == "truncated":
        sealed.write_bytes(original[:-1])
    else:
        raise AssertionError(f"Unknown file encryption test case {case}")

    with pytest.raises(file_crypto.ArtifactDecryptionError):
        file_crypto.decrypt_file(sealed, restored, **identity)
    assert restored.read_bytes() == b"previous output"
    assert set(tmp_path.iterdir()) == {source, sealed, restored}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "token",
        "empty",
        "binary",
        "multi-buffer",
        "size-limit",
        "missing-key",
        "wrong-key",
        "organization_id",
        "purpose",
        "object_id",
        "header",
        "tag",
        "ciphertext",
        "truncated",
        "scoped",
        "writer-missing",
        "writer-malformed",
        "writer-short",
        "writer-valid",
        "wrong-root-shape",
    ],
)
async def test_gcm_rejects_tampered_ciphertext_and_other_deployment_keys(
    v2_fixture_encryptor: AESGCMEncryptor, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case != "token":
        _assert_artifact_file_encryption(case, tmp_path, monkeypatch)
        return
    register_aes_encryptor(secret_key=SECRET_KEY, v2_secret_key=V2_SECRET_KEY, v2_salt=V2_SALT)
    ciphertext_by_method = {
        EncryptMethod.AES_GCM: await encryptor.encrypt("refresh-token", EncryptMethod.AES_GCM),
        EncryptMethod.AES_GCM_V2: await v2_fixture_encryptor.encrypt("refresh-token"),
    }

    for method, ciphertext in ciphertext_by_method.items():
        raw = bytearray(base64.b64decode(ciphertext))
        raw[-1] ^= 1
        with pytest.raises(TokenDecryptionError):
            await encryptor.decrypt(base64.b64encode(bytes(raw)).decode(), method)

    register_aes_encryptor(
        secret_key=f"{SECRET_KEY}-other",
        v2_secret_key=f"{V2_SECRET_KEY}-other",
        v2_salt=V2_SALT,
    )
    for method, ciphertext in ciphertext_by_method.items():
        with pytest.raises(TokenDecryptionError):
            await encryptor.decrypt(ciphertext, method)


@pytest.mark.asyncio
async def test_decrypt_unlabeled_reads_gcm_and_legacy_cbc_values(v2_fixture_encryptor: AESGCMEncryptor) -> None:
    register_aes_encryptor(secret_key=SECRET_KEY, v2_secret_key=V2_SECRET_KEY, v2_salt=V2_SALT)
    legacy = "59tEbedVMkasA7GzdJMm1w=="  # "ws://legacy" under the public default salt/IV
    current = await encryptor.encrypt("ws://current", EncryptMethod.AES_GCM)
    v2_ciphertext = await v2_fixture_encryptor.encrypt("ws://v2")
    versioned_v2 = f"{VERSIONED_CIPHERTEXT_PREFIX}{EncryptMethod.AES_GCM_V2.value}:{v2_ciphertext}"

    assert await encryptor.decrypt_unlabeled(legacy) == "ws://legacy"
    assert await encryptor.decrypt_unlabeled(current) == "ws://current"
    assert await encryptor.decrypt_unlabeled(versioned_v2) == "ws://v2"

    for invalid in (
        f"{VERSIONED_CIPHERTEXT_PREFIX}aes_gcm_v3:{current}",
        f"{VERSIONED_CIPHERTEXT_PREFIX}{EncryptMethod.AES_GCM_V2.value}:",
    ):
        with pytest.raises(TokenDecryptionError, match="Failed to decrypt token"):
            await encryptor.decrypt_unlabeled(invalid)

    register_aes_encryptor(secret_key=SECRET_KEY)
    with pytest.raises(TokenDecryptionError, match="Failed to decrypt token"):
        await encryptor.decrypt(v2_ciphertext, EncryptMethod.AES_GCM_V2)
    with pytest.raises(TokenDecryptionError, match="Failed to decrypt token"):
        await encryptor.decrypt_unlabeled(versioned_v2)
    assert await encryptor.decrypt_unlabeled(current) == "ws://current"
    assert await encryptor.decrypt_unlabeled(legacy) == "ws://legacy"


@pytest.mark.asyncio
@pytest.mark.parametrize("write_scoped", [False, True])
async def test_scoped_reader_prefers_shared_key_with_either_writer(monkeypatch, write_scoped):
    salt = "production-fixture-salt"
    shared = AESGCMEncryptor(secret_key=SECRET_KEY, salt=salt)
    scoped = AESGCMEncryptor(secret_key=V2_SECRET_KEY, salt=salt)
    register_aes_encryptor(secret_key=SECRET_KEY, salt=salt, scoped_secret_key=V2_SECRET_KEY, write_scoped=write_scoped)
    written = await encryptor.encrypt("new", DEFAULT_ENCRYPT_METHOD)
    writer, other = (scoped, shared) if write_scoped else (shared, scoped)
    assert await writer.decrypt(written) == "new"
    with pytest.raises(TokenDecryptionError):
        await other.decrypt(written)
    shared_value = await shared.encrypt("old")
    scoped_value = await scoped.encrypt("candidate")
    calls = []
    original = AESGCMEncryptor.decrypt

    async def recording_decrypt(reader, ciphertext):
        calls.append(reader)
        return await original(reader, ciphertext)

    monkeypatch.setattr(AESGCMEncryptor, "decrypt", recording_decrypt)
    pair = encryptor._methods[DEFAULT_ENCRYPT_METHOD]
    assert await encryptor.decrypt(shared_value, DEFAULT_ENCRYPT_METHOD) == "old"
    assert calls == [pair.shared]
    calls.clear()
    assert await encryptor.decrypt_unlabeled(scoped_value) == "candidate"
    assert calls == [pair.shared, pair.scoped]
    register_aes_encryptor(secret_key=SECRET_KEY, salt=salt)
    with pytest.raises(TokenDecryptionError):
        await encryptor.decrypt(scoped_value, DEFAULT_ENCRYPT_METHOD)
    with pytest.raises(RuntimeError, match="SCOPED_SECRET_KEY"):
        register_aes_encryptor(secret_key=SECRET_KEY, write_scoped=True)
