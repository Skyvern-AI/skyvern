"""Authenticated binary files sealed with dedicated artifact roots."""

from __future__ import annotations

import os
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from skyvern.config import settings

# SP 800-38D limits a single GCM plaintext to 2**39 - 256 bits.
MAX_PLAINTEXT_BYTES = (1 << 36) - 32
FILE_MAGIC = b"SKYVFILE"
_VERSION = 2
_HEADER = struct.Struct("!8sB32s12s")
_TAG_BYTES = 16
FILE_ENVELOPE_OVERHEAD = _HEADER.size + _TAG_BYTES
_BUFFER_BYTES = 1024 * 1024


class ArtifactDecryptionError(ValueError):
    """An invalid envelope, ciphertext, or authenticated identity."""


def _identity(organization_id: str, purpose: str, object_id: str) -> bytes:
    fields = [value.encode("utf-8") for value in (organization_id, purpose, object_id)]
    return b"".join(struct.pack("!I", len(field)) + field for field in fields)


def _artifact_key(root: bytes, identity: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"skyvern.artifact.file.v2\x00" + identity,
    ).derive(root)


@contextmanager
def _atomic_output(destination: str | Path) -> Iterator[BinaryIO]:
    target = Path(destination)
    staged = tempfile.NamedTemporaryFile(dir=target.parent, prefix=".skyvern-file-", delete=False)
    try:
        with staged:
            yield cast(BinaryIO, staged)
        os.replace(staged.name, target)
    finally:
        Path(staged.name).unlink(missing_ok=True)


def encrypt_file(
    source: str | Path, destination: str | Path, *, organization_id: str, purpose: str, object_id: str
) -> None:
    """Seal a binary file and atomically publish it with mode 0600 on POSIX."""
    root = settings.artifact_encryption_roots()[0]
    identity = _identity(organization_id, purpose, object_id)
    nonce = os.urandom(12)
    header = _HEADER.pack(FILE_MAGIC, _VERSION, sha256(root).digest(), nonce)
    encryptor = Cipher(algorithms.AES(_artifact_key(root, identity)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header + identity)
    with _atomic_output(destination) as output, open(source, "rb") as plaintext:
        size = os.fstat(plaintext.fileno()).st_size
        if size > MAX_PLAINTEXT_BYTES:
            raise ValueError("Artifact exceeds the AES-GCM file size limit")
        output.write(header)
        written = 0
        while chunk := plaintext.read(_BUFFER_BYTES):
            written += len(chunk)
            if written > size:
                raise ValueError("Artifact grew during encryption")
            output.write(encryptor.update(chunk))
        if written != size:
            raise ValueError("Artifact shrank during encryption")
        output.write(encryptor.finalize())
        output.write(encryptor.tag)


def decrypt_file(
    source: str | Path, destination: str | Path, *, organization_id: str, purpose: str, object_id: str
) -> None:
    """Authenticate a binary file and atomically publish the plaintext only after tag verification."""
    identity = _identity(organization_id, purpose, object_id)
    with _atomic_output(destination) as output, open(source, "rb") as ciphertext:
        size = os.fstat(ciphertext.fileno()).st_size - _HEADER.size - _TAG_BYTES
        if not 0 <= size <= MAX_PLAINTEXT_BYTES:
            raise ArtifactDecryptionError("Invalid encrypted artifact size")
        header = ciphertext.read(_HEADER.size)
        if len(header) != _HEADER.size:
            raise ArtifactDecryptionError("Truncated encrypted artifact header")
        magic, version, key_id, nonce = _HEADER.unpack(header)
        if magic != FILE_MAGIC or version != _VERSION:
            raise ArtifactDecryptionError("Invalid encrypted artifact header")
        try:
            roots = settings.artifact_encryption_roots()
        except RuntimeError as exc:
            raise ArtifactDecryptionError("Invalid artifact root configuration") from exc
        root = next((root for root in roots if sha256(root).digest() == key_id), None)
        if root is None:
            raise ArtifactDecryptionError("No configured artifact root matches the encrypted artifact")
        ciphertext.seek(_HEADER.size + size)
        tag = ciphertext.read(_TAG_BYTES)
        if len(tag) != _TAG_BYTES:
            raise ArtifactDecryptionError("Truncated encrypted artifact tag")
        ciphertext.seek(_HEADER.size)
        decryptor = Cipher(algorithms.AES(_artifact_key(root, identity)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header + identity)
        remaining = size
        while remaining:
            chunk = ciphertext.read(min(remaining, _BUFFER_BYTES))
            if not chunk:
                raise ArtifactDecryptionError("Truncated encrypted artifact")
            remaining -= len(chunk)
            output.write(decryptor.update(chunk))
        if ciphertext.read(_TAG_BYTES + 1) != tag:
            raise ArtifactDecryptionError("Encrypted artifact changed during decryption")
        try:
            output.write(decryptor.finalize())
        except InvalidTag:
            raise ArtifactDecryptionError("Encrypted artifact authentication failed") from None
