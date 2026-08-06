from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

import skyvern.browser_extension.auth as auth_module
from skyvern.browser_extension.auth import (
    build_challenge,
    compute_ext_proof,
    compute_server_proof,
    load_or_create_pairing_token,
    verify_ext_proof,
)
from skyvern.browser_extension.errors import BrowserExtensionError


def test_extension_proof_round_trip_and_tampering() -> None:
    token = "pairing-token"
    server_nonce = "server-nonce"
    client_nonce = "client-nonce"
    proof = compute_ext_proof(token, server_nonce, client_nonce)

    assert verify_ext_proof(token, server_nonce, client_nonce, proof)
    assert not verify_ext_proof(token, "tampered-server", client_nonce, proof)
    assert not verify_ext_proof(token, server_nonce, "tampered-client", proof)
    assert not verify_ext_proof(token, server_nonce, client_nonce, f"{proof}x")
    assert not verify_ext_proof(token, server_nonce, client_nonce, "not-ascii-\N{SNOWMAN}")


def test_server_proof_uses_distinct_domain_separator() -> None:
    token = "pairing-token"
    server_nonce = "server-nonce"
    client_nonce = "client-nonce"

    assert compute_server_proof(token, client_nonce, server_nonce) != compute_ext_proof(
        token, server_nonce, client_nonce
    )


def test_build_challenge_contains_a_32_byte_b64url_nonce() -> None:
    server_nonce, raw_challenge = build_challenge()
    padding = "=" * (-len(server_nonce) % 4)

    assert len(base64.urlsafe_b64decode(server_nonce + padding)) == 32
    assert "=" not in server_nonce
    assert json.loads(raw_challenge) == {"v": 1, "type": "auth.challenge", "serverNonce": server_nonce}


def test_environment_token_wins_over_existing_file(tmp_path: Path, monkeypatch) -> None:
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir()
    (token_dir / "browser_extension_token").write_text("file-token")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_TOKEN", "  environment-token\n")

    assert load_or_create_pairing_token() == "environment-token"


def test_token_file_is_created_securely_and_reused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)

    first_token = load_or_create_pairing_token()
    token_dir = tmp_path / ".skyvern"
    token_file = token_dir / "browser_extension_token"

    assert token_file.read_text() == first_token
    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    monkeypatch.setattr("skyvern.browser_extension.auth.secrets.token_urlsafe", lambda _: "different-token")
    assert load_or_create_pairing_token() == first_token


def test_existing_token_file_is_trimmed_and_hardened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir()
    token_file = token_dir / "browser_extension_token"
    token_file.write_text("  file-token\n")
    token_file.chmod(0o644)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)

    assert load_or_create_pairing_token() == "file-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_empty_environment_and_file_tokens_are_regenerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir()
    token_file = token_dir / "browser_extension_token"
    token_file.write_text(" \n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SKYVERN_BROWSER_EXTENSION_TOKEN", " \n")
    monkeypatch.setattr("skyvern.browser_extension.auth.secrets.token_urlsafe", lambda _: "regenerated-token")

    assert load_or_create_pairing_token() == "regenerated-token"
    assert token_file.read_text() == "regenerated-token"


def test_token_path_must_be_a_regular_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir()
    (token_dir / "browser_extension_token").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)

    with pytest.raises(BrowserExtensionError, match="regular file"):
        load_or_create_pairing_token()


def test_token_file_must_be_owned_by_current_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir()
    (token_dir / "browser_extension_token").write_text("file-token")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("SKYVERN_BROWSER_EXTENSION_TOKEN", raising=False)
    actual_user_id = os.getuid()
    monkeypatch.setattr("skyvern.browser_extension.auth.os.getuid", lambda: actual_user_id + 1)

    with pytest.raises(BrowserExtensionError, match="owned"):
        load_or_create_pairing_token()


def test_publish_token_writes_secure_file_and_returns_new_token(tmp_path: Path) -> None:
    token_path = tmp_path / "browser_extension_token"

    assert auth_module._publish_token(token_path, "new-token") == "new-token"
    assert token_path.read_text() == "new-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_publish_token_collision_adopts_existing_token(tmp_path: Path) -> None:
    token_path = tmp_path / "browser_extension_token"
    token_path.write_text("winning-token")
    token_path.chmod(0o600)

    assert auth_module._publish_token(token_path, "losing-token") == "winning-token"
    assert token_path.read_text() == "winning-token"


def test_publish_token_replaces_corrupt_collision_and_retries_link_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "browser_extension_token"
    real_link = os.link
    link_calls = 0

    def collide_once(source: str | Path, destination: str | Path) -> None:
        nonlocal link_calls
        link_calls += 1
        if link_calls == 1:
            token_path.write_text("\n")
            token_path.chmod(0o600)
            raise FileExistsError
        real_link(source, destination)

    monkeypatch.setattr(auth_module.os, "link", collide_once)

    assert auth_module._publish_token(token_path, "new-token") == "new-token"
    assert token_path.read_text() == "new-token"
    assert link_calls == 2
