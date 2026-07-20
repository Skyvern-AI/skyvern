from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest
from structlog.testing import capture_logs

from skyvern.library import local_browser_profile


@pytest.fixture(autouse=True)
def _skip_unrequested_background_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_browser_profile, "_sweep_triggered", True)


def _managed_root(tmp_path: Path) -> Path:
    return tmp_path / f"skyvern-local-browsers-{os.getuid()}"


def _lock_path(profile_path: Path) -> Path:
    entries = list(profile_path.iterdir())
    assert len(entries) == 1
    return entries[0]


def _stat_with_uid(result: os.stat_result, uid: int) -> os.stat_result:
    values = list(result)
    values[4] = uid
    return os.stat_result(values)


def _patch_wrong_uid(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real_lstat = os.lstat

    def wrong_uid_lstat(candidate: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
        result = real_lstat(candidate, *args, **kwargs)
        return _stat_with_uid(result, os.getuid() + 1) if Path(candidate) == path else result

    monkeypatch.setattr(os, "lstat", wrong_uid_lstat)


def _matching_process(profile_path: Path) -> MagicMock:
    process = MagicMock()
    process.pid = 43210
    process.info = {"pid": process.pid, "cmdline": ["chromium", f"--user-data-dir={profile_path}"]}
    process.cmdline.return_value = process.info["cmdline"]
    process.children.return_value = []
    return process


def _blank_cmdline_process(pid: int) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    process.info = {"pid": pid, "cmdline": None}
    return process


def _run_deletion_child(path: Path, identity: tuple[int, int]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "skyvern.library.local_browser_profile",
            "--delete-profile-tree",
            str(path),
            str(identity[0]),
            str(identity[1]),
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


def test_create_uses_secure_per_uid_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    profile = local_browser_profile.create_local_browser_profile()
    try:
        root_stat = profile.path.parent.lstat()
        profile_stat = profile.path.lstat()
        assert profile.path.parent == _managed_root(tmp_path)
        assert stat.S_IMODE(root_stat.st_mode) == 0o700
        assert root_stat.st_uid == os.getuid()
        assert stat.S_IMODE(profile_stat.st_mode) == 0o700
        assert profile_stat.st_uid == os.getuid()
    finally:
        profile.release()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_create_triggers_background_sweep_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(local_browser_profile, "_sweep_triggered", False)
    sweep = MagicMock()
    monkeypatch.setattr(local_browser_profile, "sweep_local_browser_profiles_with_budget", sweep)
    trigger = local_browser_profile.sweep_local_browser_profiles_once_in_background
    threads: list[threading.Thread] = []

    def capture_thread() -> threading.Thread | None:
        thread = trigger()
        if thread is not None:
            threads.append(thread)
        return thread

    monkeypatch.setattr(local_browser_profile, "sweep_local_browser_profiles_once_in_background", capture_thread)

    first = local_browser_profile.create_local_browser_profile()
    second = local_browser_profile.create_local_browser_profile()
    try:
        assert len(threads) == 1
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
        assert sweep.call_count == 1
    finally:
        first.release()
        second.release()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_budgeted_sweep_prevents_subsequent_background_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_browser_profile, "_sweep_triggered", False)
    monkeypatch.setattr(local_browser_profile.subprocess, "Popen", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(local_browser_profile, "_wait_for_process_with_budget", MagicMock(return_value=0))

    local_browser_profile.sweep_local_browser_profiles_with_budget()

    assert local_browser_profile._sweep_triggered is True
    assert local_browser_profile.sweep_local_browser_profiles_once_in_background() is None


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_create_background_sweep_reaps_orphan_and_preserves_new_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(local_browser_profile, "_sweep_triggered", False)
    root = _managed_root(tmp_path)
    root.mkdir(mode=local_browser_profile.PROFILE_ROOT_MODE)
    root.chmod(local_browser_profile.PROFILE_ROOT_MODE)
    orphan = root / f"{local_browser_profile.PROFILE_DIR_PREFIX}orphan"
    orphan.mkdir(mode=local_browser_profile.PROFILE_DIR_MODE)
    orphan.chmod(local_browser_profile.PROFILE_DIR_MODE)
    lock_path = orphan / local_browser_profile.PROFILE_LOCK_FILENAME
    lock_path.touch(mode=local_browser_profile.PROFILE_LOCK_MODE)
    lock_path.chmod(local_browser_profile.PROFILE_LOCK_MODE)
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", lambda _path: True)
    monkeypatch.setattr(
        local_browser_profile,
        "sweep_local_browser_profiles_with_budget",
        local_browser_profile.sweep_orphaned_local_browser_profiles,
    )
    trigger = local_browser_profile.sweep_local_browser_profiles_once_in_background
    threads: list[threading.Thread] = []

    def capture_thread() -> threading.Thread | None:
        thread = trigger()
        if thread is not None:
            threads.append(thread)
        return thread

    monkeypatch.setattr(local_browser_profile, "sweep_local_browser_profiles_once_in_background", capture_thread)

    profile = local_browser_profile.create_local_browser_profile()
    try:
        assert len(threads) == 1
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
        assert not orphan.exists()
        assert profile.path.exists()
        assert profile.revalidate()
    finally:
        profile.release()


def test_windows_background_sweep_and_create_bypass_posix_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_browser_profile, "_sweep_triggered", False)
    getuid = MagicMock(side_effect=AssertionError("os.getuid must not run on Windows"))
    sweep = MagicMock(side_effect=AssertionError("profile sweep must not run on Windows"))
    thread = MagicMock(side_effect=AssertionError("profile sweep thread must not start on Windows"))
    monkeypatch.setattr(local_browser_profile.sys, "platform", "win32")
    monkeypatch.setattr(local_browser_profile.os, "getuid", getuid)
    monkeypatch.setattr(local_browser_profile, "sweep_local_browser_profiles_with_budget", sweep)
    monkeypatch.setattr(local_browser_profile.threading, "Thread", thread)

    assert local_browser_profile.sweep_local_browser_profiles_once_in_background() is None
    assert local_browser_profile.create_local_browser_profile() is None
    assert local_browser_profile._sweep_triggered is False
    getuid.assert_not_called()
    sweep.assert_not_called()
    thread.assert_not_called()


@pytest.mark.parametrize("unsafe_root", ["mode", "owner", "symlink"])
def test_create_rejects_unsafe_root(
    unsafe_root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = _managed_root(tmp_path)
    if unsafe_root == "symlink":
        target = tmp_path / "root-target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if unsafe_root == "mode":
            root.chmod(0o755)
        else:
            _patch_wrong_uid(monkeypatch, root)

    with pytest.raises((OSError, RuntimeError)):
        local_browser_profile.create_local_browser_profile()


@pytest.mark.parametrize("unsafe_root", ["mode", "owner", "symlink"])
def test_sweep_rejects_unsafe_root(
    unsafe_root: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    profile.release()
    root = profile.path.parent
    if unsafe_root == "mode":
        root.chmod(0o755)
    elif unsafe_root == "owner":
        _patch_wrong_uid(monkeypatch, root)
    else:
        target = tmp_path / "root-target"
        root.rename(target)
        root.symlink_to(target, target_is_directory=True)

    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)

    local_browser_profile.sweep_orphaned_local_browser_profiles()

    assert profile.path.exists()
    cleanup.assert_not_called()


@pytest.mark.parametrize("lock_state", ["directory", "symlink", "wrong_mode"])
def test_sweep_skips_malformed_lock_without_removing_the_candidate(
    lock_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    profile.release()
    lock_path = _lock_path(profile.path)
    lock_path.unlink()
    if lock_state == "directory":
        lock_path.mkdir()
    elif lock_state == "symlink":
        external_lock = tmp_path / "external-lock"
        external_lock.touch()
        lock_path.symlink_to(external_lock)
    else:
        lock_path.touch(mode=0o644)

    terminate = MagicMock(return_value=True)
    rmtree = MagicMock()
    rmdir = MagicMock(side_effect=AssertionError("malformed locks must not trigger recovery"))
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", terminate)
    monkeypatch.setattr(local_browser_profile.shutil, "rmtree", rmtree)
    monkeypatch.setattr(local_browser_profile.os, "rmdir", rmdir)

    local_browser_profile.sweep_orphaned_local_browser_profiles()

    assert profile.path.exists()
    terminate.assert_not_called()
    rmtree.assert_not_called()
    rmdir.assert_not_called()


def test_sweep_removes_lockless_empty_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    profile.release()
    _lock_path(profile.path).unlink()

    assert local_browser_profile.sweep_orphaned_local_browser_profiles() == 1
    assert not profile.path.exists()


def test_sweep_leaves_lockless_nonempty_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    profile.release()
    _lock_path(profile.path).unlink()
    sentinel = profile.path / "sentinel"
    sentinel.write_text("live data")

    assert local_browser_profile.sweep_orphaned_local_browser_profiles() == 0
    assert sentinel.read_text() == "live data"


def test_sweep_rejects_candidate_owned_by_another_uid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    profile.release()
    _patch_wrong_uid(monkeypatch, profile.path)
    cleanup = MagicMock(return_value=True)
    monkeypatch.setattr(local_browser_profile, "cleanup_local_browser_profile", cleanup)

    local_browser_profile.sweep_orphaned_local_browser_profiles()

    assert profile.path.exists()
    cleanup.assert_not_called()


@pytest.mark.parametrize("replaced_identity", ["directory", "lock"])
def test_creator_revalidates_directory_and_lock_identity(
    replaced_identity: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    assert profile.revalidate() is True

    if replaced_identity == "directory":
        original_path = profile.path.with_name(f"{profile.path.name}-original")
        profile.path.rename(original_path)
        profile.path.mkdir(mode=0o700)
    else:
        lock_path = _lock_path(profile.path)
        lock_path.rename(lock_path.with_name(f"{lock_path.name}-original"))
        lock_path.touch(mode=0o600)

    try:
        assert profile.revalidate() is False
    finally:
        profile.release()


@pytest.mark.skipif(os.name == "nt", reason="flock is POSIX-only")
def test_real_child_lock_preserves_live_profile_then_sweep_reaps_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    lock_path = _lock_path(profile.path)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys; f=open(sys.argv[1], 'rb'); "
                "\ntry: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)"
                "\nexcept BlockingIOError: raise SystemExit(0)"
                "\nraise SystemExit(1)"
            ),
            str(lock_path),
        ],
        check=False,
        timeout=5,
    )
    assert probe.returncode == 0
    profile.release()

    ready_path = tmp_path / "child-ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; f=open(sys.argv[1], 'rb'); "
                "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); "
                "pathlib.Path(sys.argv[2]).touch(); time.sleep(60)"
            ),
            str(lock_path),
            str(ready_path),
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists()
        monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", lambda _path: True)

        local_browser_profile.sweep_orphaned_local_browser_profiles()

        assert profile.path.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)

    local_browser_profile.sweep_orphaned_local_browser_profiles()

    assert not profile.path.exists()


@pytest.mark.parametrize("failure", ["incomplete_scan", "denied_kill", "wait_error", "alive_survivor"])
def test_kill_failure_prevents_profile_deletion(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    process = _matching_process(profile_path)
    if failure == "incomplete_scan":
        monkeypatch.setattr(
            local_browser_profile.psutil,
            "process_iter",
            MagicMock(side_effect=psutil.AccessDenied(pid=process.pid)),
        )
    else:
        monkeypatch.setattr(local_browser_profile.psutil, "process_iter", MagicMock(return_value=[process]))
    if failure == "denied_kill":
        process.kill.side_effect = psutil.AccessDenied(pid=process.pid)
    elif failure == "wait_error":
        monkeypatch.setattr(local_browser_profile.psutil, "wait_procs", MagicMock(side_effect=RuntimeError("wait")))
    elif failure == "alive_survivor":
        monkeypatch.setattr(local_browser_profile.psutil, "wait_procs", MagicMock(return_value=([], [process])))

    rmtree = MagicMock()
    monkeypatch.setattr(local_browser_profile.shutil, "rmtree", rmtree)

    assert local_browser_profile.terminate_local_browser_processes(profile_path) is False
    assert local_browser_profile.cleanup_local_browser_profile(profile_path) is False
    rmtree.assert_not_called()


def test_blank_cmdline_bystander_does_not_hide_matching_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    matching = _matching_process(profile_path)
    blank = _blank_cmdline_process(43211)
    monkeypatch.setattr(local_browser_profile.psutil, "process_iter", MagicMock(return_value=[blank, matching]))
    monkeypatch.setattr(local_browser_profile.psutil, "wait_procs", MagicMock(return_value=([matching], [])))

    assert local_browser_profile.cleanup_local_browser_profile(profile_path) is True
    matching.kill.assert_called_once_with()
    blank.kill.assert_not_called()
    assert not profile_path.exists()


def test_blank_cmdline_bystanders_do_not_block_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    blank = _blank_cmdline_process(43211)
    monkeypatch.setattr(local_browser_profile.psutil, "process_iter", MagicMock(return_value=[blank]))

    assert local_browser_profile.cleanup_local_browser_profile(profile_path) is True
    blank.kill.assert_not_called()
    assert not profile_path.exists()


def test_unknown_cleanup_failure_skips_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    monkeypatch.setattr(
        local_browser_profile,
        "terminate_local_browser_processes",
        MagicMock(side_effect=RuntimeError("unknown")),
    )
    rmtree = MagicMock()
    monkeypatch.setattr(local_browser_profile.shutil, "rmtree", rmtree)

    assert local_browser_profile.cleanup_local_browser_profile(profile_path) is False
    rmtree.assert_not_called()


def test_cleanup_spawn_failure_returns_false_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    lock_fd = profile._lock_fd
    assert lock_fd is not None
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", lambda _path: True)
    monkeypatch.setattr(
        local_browser_profile.subprocess,
        "Popen",
        MagicMock(side_effect=OSError("cleanup spawn failed")),
    )

    with capture_logs() as logs:
        assert local_browser_profile.cleanup_local_browser_profile(profile) is False

    assert profile._lock_fd is None
    with pytest.raises(OSError):
        os.fstat(lock_fd)
    assert any(log.get("event") == "local_browser_profile_cleanup_failed" for log in logs)


def test_creation_rollback_failure_preserves_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(local_browser_profile.os, "open", MagicMock(side_effect=PermissionError("lock open failed")))
    monkeypatch.setattr(
        local_browser_profile,
        "_remove_profile_directory_bounded",
        MagicMock(side_effect=OSError("rollback cleanup failed")),
    )

    with pytest.raises(PermissionError, match="lock open failed"):
        local_browser_profile.create_local_browser_profile()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_sweep_cannot_reach_protected_sibling_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    protected_dirs = [tmp_path / "skyvern-browser", tmp_path / "skyvern-browser-profiles"]
    for protected_dir in protected_dirs:
        protected_dir.mkdir()
        (protected_dir / "sentinel").write_text("live data")

    profile = local_browser_profile.create_local_browser_profile()
    profile.release()
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", lambda _path: True)

    assert local_browser_profile.sweep_orphaned_local_browser_profiles() == 1
    assert not profile.path.exists()
    for protected_dir in protected_dirs:
        assert protected_dir.is_dir()
        assert (protected_dir / "sentinel").read_text() == "live data"


@pytest.mark.skipif(os.name == "nt", reason="identity-checked deletion is POSIX-only")
def test_deletion_child_rejects_symlink_replacement(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    sentinel = protected / "sentinel"
    sentinel.write_text("live data")
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    profile_result = profile_path.lstat()
    profile_path.rmdir()
    profile_path.symlink_to(protected, target_is_directory=True)

    result = _run_deletion_child(profile_path, (profile_result.st_dev, profile_result.st_ino))

    assert result.returncode == 1
    assert sentinel.read_text() == "live data"


@pytest.mark.skipif(os.name == "nt", reason="identity-checked deletion is POSIX-only")
def test_deletion_child_rejects_same_name_directory_replacement(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    profile_result = profile_path.lstat()
    profile_path.rename(tmp_path / "original-profile")
    profile_path.mkdir()
    sentinel = profile_path / "sentinel"
    sentinel.write_text("replacement data")

    result = _run_deletion_child(profile_path, (profile_result.st_dev, profile_result.st_ino))

    assert result.returncode == 1
    assert sentinel.read_text() == "replacement data"


def test_timed_out_deletion_stops_before_cleanup_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "profile"
    profile_path.mkdir()
    late_mutation = tmp_path / "late-mutation"
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def start_blocking_deleter(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(
            [
                sys.executable,
                "-c",
                (
                    "import pathlib, signal, sys, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "time.sleep(0.5); pathlib.Path(sys.argv[1]).touch()"
                ),
                str(late_mutation),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        return child

    monkeypatch.setattr(local_browser_profile, "PROFILE_DELETE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", lambda _path: True)
    monkeypatch.setattr(local_browser_profile.subprocess, "Popen", start_blocking_deleter)

    started_at = time.monotonic()
    try:
        assert local_browser_profile.cleanup_local_browser_profile(profile_path) is False
        assert time.monotonic() - started_at <= 0.2
        assert children[0].poll() is not None
        time.sleep(0.55)
        assert not late_mutation.exists()
        assert profile_path.exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_interrupted_finalization_is_recovered_by_next_sweep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    lock_path = _lock_path(profile.path)
    real_rmdir = os.rmdir
    failed_once = False

    def fail_first_profile_rmdir(path: os.PathLike[str] | str, *, dir_fd: int | None = None) -> None:
        nonlocal failed_once
        if str(path).startswith(".reap-") and dir_fd is not None and not failed_once:
            failed_once = True
            raise OSError("forced finalization failure")
        real_rmdir(path, dir_fd=dir_fd)

    monkeypatch.setattr(local_browser_profile.os, "rmdir", fail_first_profile_rmdir)
    monkeypatch.setattr(local_browser_profile, "terminate_local_browser_processes", lambda _path: True)

    assert local_browser_profile.cleanup_local_browser_profile(profile) is False
    assert profile.path.is_dir()
    assert not lock_path.exists()
    assert not list(profile._root.glob(".reap-*"))

    assert local_browser_profile.sweep_orphaned_local_browser_profiles() == 1
    assert not profile.path.exists()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_sweep_reclaims_leftover_empty_tombstone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root, _uid, _root_result = local_browser_profile._prepare_profile_root()
    tombstone = root / ".reap-deadbeef"
    tombstone.mkdir(mode=0o700)
    hidden = root / ".hidden"
    hidden.mkdir(mode=0o700)

    assert local_browser_profile.sweep_orphaned_local_browser_profiles() == 1
    assert not tombstone.exists()
    assert hidden.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_finalization_removes_verified_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    try:
        assert local_browser_profile._finalize_managed_profile_removal(profile) is True
        assert not profile.path.exists()
    finally:
        profile.release()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_finalization_rejects_same_name_swap_at_tombstone_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    original_name = f"{profile.path.name}-original"
    original_path = profile._root / original_name
    real_rename = os.rename
    swapped = False

    def swap_before_tombstone_rename(
        src: os.PathLike[str] | str,
        dst: os.PathLike[str] | str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if (
            src == profile.path.name
            and str(dst).startswith(".reap-")
            and src_dir_fd is not None
            and dst_dir_fd is not None
            and not swapped
        ):
            swapped = True
            real_rename(src, original_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            os.mkdir(src, mode=0o700, dir_fd=src_dir_fd)
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(local_browser_profile.os, "rename", swap_before_tombstone_rename)
    try:
        assert local_browser_profile._finalize_managed_profile_removal(profile) is False
        assert not profile.path.exists()
        assert original_path.is_dir()
        assert not list(profile._root.glob(".reap-*"))
    finally:
        profile.release()

    assert local_browser_profile.sweep_orphaned_local_browser_profiles() == 1
    assert not original_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_finalization_rejects_profile_symlink_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    profile = local_browser_profile.create_local_browser_profile()
    original_profile_path = profile.path.with_name(f"{profile.path.name}-original")
    protected = tmp_path / "protected"
    protected.mkdir()
    sentinel = protected / "sentinel"
    sentinel.write_text("live data")
    real_open = os.open
    swapped = False

    def swap_before_finalize_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if Path(path) == profile._root and dir_fd is None and not swapped:
            swapped = True
            profile.path.rename(original_profile_path)
            profile.path.symlink_to(protected, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(local_browser_profile.os, "open", swap_before_finalize_open)
    try:
        assert local_browser_profile._finalize_managed_profile_removal(profile) is False
        assert sentinel.read_text() == "live data"
    finally:
        profile.release()


@pytest.mark.skipif(os.name == "nt", reason="managed profile sweep is POSIX-only")
def test_sweep_surfaces_real_child_warning_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    unsafe_root = _managed_root(tmp_path)
    unsafe_root.mkdir(mode=0o700)
    unsafe_root.chmod(0o755)

    with capture_logs() as logs:
        local_browser_profile.sweep_local_browser_profiles_with_budget()

    assert any(
        log.get("return_code") == 0 and "Unsafe local browser profile directory" in log.get("output_tail", "")
        for log in logs
    )


@pytest.mark.skipif(os.name == "nt", reason="identity-checked deletion is POSIX-only")
def test_tombstone_removal_detects_swap_at_terminal_rmdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    victim = root / "profile-victim"
    victim.mkdir(mode=0o700)
    expected = local_browser_profile._identity(os.lstat(victim))

    real_rmdir = os.rmdir

    def swapping_rmdir(name: str, *, dir_fd: int | None = None) -> None:
        os.rename(name, ".swapped-aside", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.mkdir(name, mode=0o700, dir_fd=dir_fd)
        real_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", swapping_rmdir)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        removed = local_browser_profile._remove_verified_directory_via_tombstone(
            root_fd,
            victim.name,
            expected_identity=expected,
            uid=os.getuid(),
        )
    finally:
        os.close(root_fd)

    assert removed is False
    survivor = root / ".swapped-aside"
    assert survivor.exists()
    assert local_browser_profile._identity(os.lstat(survivor)) == expected


@pytest.mark.skipif(os.name == "nt", reason="flock is POSIX-only")
def test_create_retries_when_directory_reaped_mid_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    calls = {"count": 0}
    real_mkdtemp = tempfile.mkdtemp

    def reaped_first_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            os.rmdir(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", reaped_first_mkdtemp)
    profile = local_browser_profile.create_local_browser_profile()
    try:
        assert profile is not None
        assert calls["count"] == 2
        assert profile.path.exists()
        assert profile.revalidate()
    finally:
        if profile is not None:
            profile.release()


@pytest.mark.skipif(os.name == "nt", reason="flock is POSIX-only")
def test_create_retries_when_sweep_wins_published_lock_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    real_lock = local_browser_profile._lock_exclusive_nonblocking
    calls = {"count": 0}

    def sweep_wins_first(fd: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise BlockingIOError
        real_lock(fd)

    monkeypatch.setattr(local_browser_profile, "_lock_exclusive_nonblocking", sweep_wins_first)
    profile = local_browser_profile.create_local_browser_profile()
    try:
        assert profile is not None
        assert profile.path.exists()
        assert calls["count"] >= 2
    finally:
        if profile is not None:
            profile.release()
