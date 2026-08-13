from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from skyvern.browser_extension import broker_state as broker_state_module
from skyvern.browser_extension.broker_state import (
    STARTUP_LOG_LIMIT,
    BrokerPaths,
    BrokerState,
    OwnerFileLock,
    atomic_write_json,
    enable_broker_state,
    ensure_run_directory,
    initialize_empty_journal,
    matching_startup_failure,
    prepare_startup_log,
    process_identity_matches,
    publish_broker_state,
    read_broker_state,
    read_owner_json,
    read_readiness,
    record_startup_failure,
    run_directory_identity,
    validate_run_directory,
    write_readiness,
)
from skyvern.browser_extension.errors import BrowserExtensionBrokerError


@pytest.mark.skipif(os.name != "posix", reason="POSIX zombie semantics")
def test_process_identity_does_not_match_zombie_process() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        child = psutil.Process(process.pid)
        marker = f"{child.create_time():.6f}"
        deadline = time.monotonic() + 2.0
        while child.status() != psutil.STATUS_ZOMBIE:
            if time.monotonic() >= deadline:
                raise AssertionError("child process did not become a zombie")
            time.sleep(0.01)

        assert not process_identity_matches(process.pid, marker)
    finally:
        process.wait()


def test_run_directory_rejects_symlinks_and_unsafe_modes(tmp_path: Path) -> None:
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o700)
    symlink_base = tmp_path / "symlink-base"
    symlink_base.mkdir(mode=0o700)
    (symlink_base / "19777").symlink_to(safe)

    with pytest.raises(BrowserExtensionBrokerError, match="UNSAFE_PATH"):
        ensure_run_directory(19777, base_dir=symlink_base)

    mode_base = tmp_path / "mode-base"
    paths = ensure_run_directory(19777, base_dir=mode_base)
    paths.run_dir.chmod(0o755)
    with pytest.raises(BrowserExtensionBrokerError, match="0700"):
        ensure_run_directory(19777, base_dir=mode_base)


def test_run_directory_rejects_unsafe_existing_parent(tmp_path: Path) -> None:
    base = tmp_path / "unsafe-parent"
    base.mkdir(mode=0o755)

    with pytest.raises(BrowserExtensionBrokerError, match="0700"):
        ensure_run_directory(19777, base_dir=base)


def test_run_directory_identity_change_is_rejected(tmp_path: Path) -> None:
    base = tmp_path / "run"
    paths = ensure_run_directory(19777, base_dir=base)
    identity = run_directory_identity(paths)
    paths.run_dir.rename(base / "old")
    paths.run_dir.mkdir(mode=0o700)

    with pytest.raises(BrowserExtensionBrokerError, match="identity changed"):
        validate_run_directory(paths, expected_identity=identity)


def test_atomic_state_and_empty_journal_are_owner_only(tmp_path: Path) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    initialize_empty_journal(paths)
    state = BrokerState(
        schemaVersion=1,
        externalPort=19777,
        controlEndpoint=str(paths.control_socket),
        pid=os.getpid(),
        processStart="marker",
        bootId="boot",
        lifecycle="ready",
        cleanShutdown=False,
        protocolMin=1,
        protocolMax=1,
        features=("exclusive-client",),
        brokerGeneration=1,
        buildFingerprint="test",
    )

    publish_broker_state(paths, state)

    assert read_broker_state(paths) == state
    assert read_owner_json(paths.leases) == {"schemaVersion": 1, "leases": []}
    assert paths.state.stat().st_mode & 0o777 == 0o600
    assert paths.leases.stat().st_mode & 0o777 == 0o600

    atomic_write_json(paths.leases, {"schemaVersion": 1, "leases": [{"state": "active"}]})
    with pytest.raises(BrowserExtensionBrokerError, match="UNSAFE_STATE"):
        initialize_empty_journal(paths)


def test_atomic_publication_rejects_existing_symlink(tmp_path: Path) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    paths.state.symlink_to(victim)

    with pytest.raises(BrowserExtensionBrokerError, match="UNSAFE_PATH"):
        atomic_write_json(paths.state, {"schemaVersion": 1})

    assert victim.read_text() == "unchanged"


def test_startup_failure_backoff_progresses_across_child_fingerprints(tmp_path: Path) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    failures = [
        record_startup_failure(
            paths,
            code="STARTUP_FAILED",
            port=19777,
            observed_state_fingerprint=f"child-{attempt}",
            now=100.0 + attempt,
        )
        for attempt in range(7)
    ]

    assert [failure.attemptCount for failure in failures] == [1, 2, 3, 4, 5, 6, 7]
    assert [failure.retryAfter - failure.lastFailure for failure in failures] == [1, 2, 4, 8, 16, 30, 30]
    assert failures[-1].firstFailure == failures[0].firstFailure
    assert matching_startup_failure(paths, observed_state_fingerprint="child-6", now=120.0) == failures[-1]
    assert matching_startup_failure(paths, observed_state_fingerprint="child-5", now=120.0) is None


def test_enable_clears_startup_failure_while_holding_spawn_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    record_startup_failure(
        paths,
        code="BROKER_NOT_ENABLED",
        port=19777,
        observed_state_fingerprint="missing",
    )
    original_clear = broker_state_module.clear_startup_failure
    clear_observed = False

    def clear_while_locked(locked_paths: BrokerPaths) -> None:
        nonlocal clear_observed
        assert locked_paths.run_dir == paths.run_dir
        contender = OwnerFileLock(paths.spawn_lock)
        acquired = contender.acquire(blocking=False)
        if acquired:
            contender.release()
        assert not acquired
        original_clear(paths)
        clear_observed = True

    monkeypatch.setattr(broker_state_module, "clear_startup_failure", clear_while_locked)

    enabled_paths, source = enable_broker_state(19777, base_dir=tmp_path / "run")

    assert enabled_paths.run_dir == paths.run_dir
    assert enabled_paths.extension_secret == paths.extension_secret
    assert source == "created"
    assert clear_observed
    assert not paths.startup_failure.exists()


def test_spawn_lock_descriptor_handoff_remains_exclusive(tmp_path: Path) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    parent_lock = OwnerFileLock(paths.spawn_lock)
    assert parent_lock.acquire()
    assert parent_lock.fd is not None
    inherited_fd = os.dup(parent_lock.fd)
    parent_lock.handoff_to_child()
    child_lock = OwnerFileLock.adopt_inherited(paths.spawn_lock, inherited_fd)
    contender = OwnerFileLock(paths.spawn_lock)

    assert not contender.acquire(blocking=False)
    child_lock.release()
    assert contender.acquire(blocking=False)
    contender.release()


def test_enable_retains_matching_legacy_credential_for_seamless_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir(mode=0o700)
    legacy_path = token_dir / "browser_extension_token"
    legacy_path.write_text("legacy-secret")
    legacy_path.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    paths, source = enable_broker_state(19777, base_dir=tmp_path / "run")

    assert source == "copied"
    assert paths.extension_secret.read_text() == "legacy-secret"
    assert legacy_path.read_text() == "legacy-secret"
    assert paths.extension_secret.stat().st_mode & 0o777 == 0o600
    assert legacy_path.stat().st_mode & 0o777 == 0o600


def test_enable_creates_matching_legacy_credential_when_no_pairing_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    paths, source = enable_broker_state(19777, base_dir=tmp_path / "run")
    legacy_path = home / ".skyvern" / "browser_extension_token"

    assert source == "created"
    assert paths.extension_secret.read_text() == legacy_path.read_text()
    assert paths.extension_secret.stat().st_mode & 0o777 == 0o600
    assert legacy_path.stat().st_mode & 0o777 == 0o600
    assert legacy_path.parent.stat().st_mode & 0o777 == 0o700


def test_existing_broker_credential_restores_legacy_file_in_owner_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    token_dir = home / ".skyvern"
    token_dir.mkdir(parents=True, mode=0o755)
    token_dir.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    broker_state_module.atomic_write_secret(paths.extension_secret, "broker-secret")

    enabled_paths, source = enable_broker_state(19777, base_dir=tmp_path / "run")
    legacy_path = token_dir / "browser_extension_token"

    assert enabled_paths.run_dir == paths.run_dir
    assert source == "existing"
    assert legacy_path.read_text() == "broker-secret"
    assert legacy_path.stat().st_mode & 0o777 == 0o600


def test_readiness_success_error_and_timeout() -> None:
    read_fd, write_fd = os.pipe()
    try:
        write_readiness(write_fd, "READY", port=19777)
        os.close(write_fd)
        write_fd = -1
        assert read_readiness(read_fd, timeout=0.1) == {"status": "READY", "port": 19777}
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    read_fd, write_fd = os.pipe()
    try:
        write_readiness(write_fd, "ERROR", code="PORT_IN_USE")
        os.close(write_fd)
        write_fd = -1
        assert read_readiness(read_fd, timeout=0.1) == {"status": "ERROR", "code": "PORT_IN_USE"}
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    read_fd, write_fd = os.pipe()
    try:
        started = time.monotonic()
        with pytest.raises(BrowserExtensionBrokerError, match="STARTUP_TIMEOUT"):
            read_readiness(read_fd, timeout=0.01)
        assert time.monotonic() - started < 0.5
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_startup_log_is_bounded_while_child_is_writing(tmp_path: Path) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    write_fd, drain_thread = prepare_startup_log(paths)
    payload = b"x" * (STARTUP_LOG_LIMIT * 3) + b"terminal-marker"
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(write_fd, view) :]
    finally:
        os.close(write_fd)
    drain_thread.join(timeout=1.0)

    assert not drain_thread.is_alive()
    assert paths.startup_log.stat().st_size <= STARTUP_LOG_LIMIT
    assert paths.startup_log.read_bytes().endswith(b"terminal-marker")


def test_oversized_startup_log_is_recovered_before_next_spawn(tmp_path: Path) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    paths.startup_log.write_bytes(b"old" * STARTUP_LOG_LIMIT)
    paths.startup_log.chmod(0o600)

    write_fd, drain_thread = prepare_startup_log(paths)
    os.close(write_fd)
    drain_thread.join(timeout=1.0)

    assert not drain_thread.is_alive()
    assert paths.startup_log.stat().st_size <= STARTUP_LOG_LIMIT // 2


def test_startup_log_fd_is_closed_when_pipe_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    read_fd, log_fd = os.pipe()
    monkeypatch.setattr(broker_state_module, "_prepare_startup_log_file", lambda _paths: (log_fd, b""))
    monkeypatch.setattr(broker_state_module.os, "pipe", lambda: (_ for _ in ()).throw(OSError("fd pressure")))
    try:
        with pytest.raises(OSError, match="fd pressure"):
            prepare_startup_log(paths)
        with pytest.raises(OSError):
            os.fstat(log_fd)
    finally:
        os.close(read_fd)


def test_failed_enable_preserves_working_legacy_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    token_dir = tmp_path / ".skyvern"
    token_dir.mkdir(mode=0o700)
    legacy_path = token_dir / "browser_extension_token"
    legacy_path.write_text("legacy-secret")
    legacy_path.chmod(0o600)
    paths = ensure_run_directory(19777, base_dir=tmp_path / "run")
    paths.leases.write_text("not-json")
    paths.leases.chmod(0o600)

    with pytest.raises(BrowserExtensionBrokerError, match="UNSAFE_STATE"):
        enable_broker_state(19777, base_dir=tmp_path / "run")

    assert legacy_path.read_text() == "legacy-secret"
    assert not paths.extension_secret.exists()


def test_paths_never_place_non_socket_artifacts_outside_port_directory(tmp_path: Path) -> None:
    paths = ensure_run_directory(23456, base_dir=tmp_path)

    for field in paths.__dataclass_fields__:
        if field not in {"run_dir", "control_socket"}:
            assert getattr(paths, field).parent == tmp_path / "23456"
    assert paths.control_socket.parent.stat().st_mode & 0o777 == 0o700


def test_long_socket_path_uses_unpredictable_recorded_owner_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o1777)
    monkeypatch.setattr(broker_state_module, "_shared_temporary_root", lambda: shared_root)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    predictable = shared_root / f"skyvern-browser-extension-{uid}"
    predictable.mkdir(mode=0o700)
    base_dir = tmp_path / ("long-run-directory-" * 8)

    paths = ensure_run_directory(23456, base_dir=base_dir)

    assert paths.control_socket.parent != predictable
    assert paths.control_socket.parent.parent == shared_root
    assert paths.control_socket.parent.name.startswith(f"skyvern-browser-extension-{uid}-")
    assert paths.control_socket.parent.stat().st_mode & 0o777 == 0o700
    state = BrokerState(
        schemaVersion=1,
        externalPort=23456,
        controlEndpoint=str(paths.control_socket),
        pid=os.getpid(),
        processStart="marker",
        bootId="boot",
        lifecycle="ready",
        cleanShutdown=False,
        protocolMin=1,
        protocolMax=1,
        features=("exclusive-client",),
        brokerGeneration=1,
        buildFingerprint="test",
    )
    publish_broker_state(paths, state)

    resolved = ensure_run_directory(23456, base_dir=base_dir)

    assert resolved.control_socket == paths.control_socket


def test_long_socket_path_skips_preexisting_random_name_without_wedging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o1777)
    monkeypatch.setattr(broker_state_module, "_shared_temporary_root", lambda: shared_root)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    collision_token = "attacker-controlled-name"
    selected_token = "fresh-unpredictable-name"
    tokens = iter((collision_token, selected_token))
    monkeypatch.setattr(broker_state_module.secrets, "token_urlsafe", lambda _size: next(tokens))
    collision = shared_root / f"skyvern-browser-extension-{uid}-{collision_token}"
    collision.mkdir(mode=0o755)
    base_dir = tmp_path / ("long-run-directory-" * 8)

    paths = ensure_run_directory(23456, base_dir=base_dir)

    assert paths.control_socket.parent != collision
    assert paths.control_socket.parent == shared_root / f"skyvern-browser-extension-{uid}-{selected_token}"
    assert paths.control_socket.parent.stat().st_mode & 0o777 == 0o700


def test_long_socket_path_replaces_unsafe_recorded_directory_without_wedging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_root = tmp_path / "shared"
    shared_root.mkdir(mode=0o1777)
    monkeypatch.setattr(broker_state_module, "_shared_temporary_root", lambda: shared_root)
    monkeypatch.setattr(broker_state_module.secrets, "token_urlsafe", lambda _size: "fresh-unpredictable-name")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    unsafe_directory = shared_root / f"skyvern-browser-extension-{uid}-attacker-controlled-name"
    unsafe_directory.mkdir(mode=0o755)
    base_dir = tmp_path / ("long-run-directory-" * 8)
    initial = ensure_run_directory(23456, base_dir=base_dir, prepare_control_endpoint=False)
    publish_broker_state(
        initial,
        BrokerState(
            schemaVersion=1,
            externalPort=23456,
            controlEndpoint=str(unsafe_directory / "control.sock"),
            pid=os.getpid(),
            processStart="stale-marker",
            bootId="boot",
            lifecycle="ready",
            cleanShutdown=False,
            protocolMin=1,
            protocolMax=1,
            features=("exclusive-client",),
            brokerGeneration=1,
            buildFingerprint="test",
        ),
    )

    paths = ensure_run_directory(23456, base_dir=base_dir)

    assert paths.control_socket.parent != unsafe_directory
    assert paths.control_socket.parent == shared_root / f"skyvern-browser-extension-{uid}-fresh-unpredictable-name"
    assert paths.control_socket.parent.stat().st_mode & 0o777 == 0o700
