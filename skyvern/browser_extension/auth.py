from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.protocol import LEGACY_PROTOCOL_VERSION

_TOKEN_ENV = "SKYVERN_BROWSER_EXTENSION_TOKEN"


def load_or_create_pairing_token() -> str:
    environment_token = os.environ.get(_TOKEN_ENV)
    if environment_token is not None:
        environment_token = environment_token.strip()
        if environment_token:
            return environment_token

    token_dir = Path.home() / ".skyvern"
    token_path = token_dir / "browser_extension_token"
    existing_token = _read_token_file(token_path)
    if existing_token:
        return existing_token
    if existing_token == "":
        token_path.unlink()

    token_dir_existed = token_dir.exists()
    token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not token_dir_existed:
        token_dir.chmod(0o700)

    token = secrets.token_urlsafe(32)
    return _publish_token(token_path, token)


def _read_token_file(token_path: Path) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(token_path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BrowserExtensionError("Pairing token path must be a regular file owned by the current user") from exc

    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise BrowserExtensionError("Pairing token path must be a regular file")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise BrowserExtensionError("Pairing token file must be owned by the current user")
        if stat.S_IMODE(file_stat.st_mode) & 0o177:
            os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "r", encoding="utf-8") as token_file:
            file_descriptor = -1
            return token_file.read().strip()
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def _publish_token(token_path: Path, token: str) -> str:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=token_path.parent,
        prefix=f".{token_path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as token_file:
            token_file.write(token)
            token_file.flush()
            os.fsync(token_file.fileno())
        try:
            os.link(temporary_path, token_path)
        except FileExistsError:
            try:
                existing_token = _read_token_file(token_path)
            except BrowserExtensionError:
                existing_token = None
            if existing_token:
                return existing_token
            try:
                token_path.unlink(missing_ok=True)
                os.link(temporary_path, token_path)
            except OSError as exc:
                raise BrowserExtensionError("Failed to publish a valid pairing token after a creation race") from exc
        except OSError as exc:
            raise BrowserExtensionError("Failed to publish pairing token") from exc
        return token
    finally:
        temporary_path.unlink(missing_ok=True)


def build_challenge() -> tuple[str, str]:
    server_nonce = _b64url(secrets.token_bytes(32))
    challenge = json.dumps(
        {"v": LEGACY_PROTOCOL_VERSION, "type": "auth.challenge", "serverNonce": server_nonce}, separators=(",", ":")
    )
    return server_nonce, challenge


def compute_ext_proof(token: str, server_nonce: str, client_nonce: str) -> str:
    message = f"skyvern-ext-v1|{server_nonce}|{client_nonce}"
    return _compute_proof(token, message)


def compute_server_proof(token: str, client_nonce: str, server_nonce: str) -> str:
    message = f"skyvern-srv-v1|{client_nonce}|{server_nonce}"
    return _compute_proof(token, message)


def verify_ext_proof(token: str, server_nonce: str, client_nonce: str, proof: str) -> bool:
    expected_proof = compute_ext_proof(token, server_nonce, client_nonce)
    try:
        return hmac.compare_digest(expected_proof, proof)
    except TypeError:
        return False


def hash_recovery_secret(recovery_secret: str) -> str:
    """Return the non-credential journal representation from spec-v3.md lines 57-63."""
    return hashlib.sha256(f"skyvern-recovery-v1|{recovery_secret}".encode()).hexdigest()


def compute_client_proof(
    recovery_secret: str,
    server_nonce: str,
    client_nonce: str,
    client_id: str,
    broker_generation: int,
) -> str:
    message = f"skyvern-client-v1|{server_nonce}|{client_nonce}|{client_id}|{broker_generation}"
    return _compute_proof(recovery_secret, message)


def compute_broker_proof(
    recovery_secret: str,
    client_nonce: str,
    server_nonce: str,
    client_id: str,
    broker_generation: int,
) -> str:
    message = f"skyvern-broker-v1|{client_nonce}|{server_nonce}|{client_id}|{broker_generation}"
    return _compute_proof(recovery_secret, message)


def verify_client_proof(
    recovery_secret: str,
    server_nonce: str,
    client_nonce: str,
    client_id: str,
    broker_generation: int,
    proof: str,
) -> bool:
    expected = compute_client_proof(
        recovery_secret,
        server_nonce,
        client_nonce,
        client_id,
        broker_generation,
    )
    try:
        return hmac.compare_digest(expected, proof)
    except TypeError:
        return False


def _compute_proof(token: str, message: str) -> str:
    digest = hmac.new(token.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
