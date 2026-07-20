from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from _thread import LockType
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import psutil
import structlog

LOG = structlog.get_logger(__name__)

PROFILE_ROOT_PREFIX = "skyvern-local-browsers-"
PROFILE_DIR_PREFIX = "profile-"
PROFILE_LOCK_FILENAME = ".skyvern-profile.lock"
PROFILE_ROOT_MODE = 0o700
PROFILE_DIR_MODE = 0o700
PROFILE_LOCK_MODE = 0o600
PROCESS_KILL_TIMEOUT_SECONDS = 2.0
PROFILE_DELETE_TIMEOUT_SECONDS = 1.0
# One orphan with a live browser can cost ~3s (kill wait + delete) — reclamation converges across startups.
PROFILE_SWEEP_STARTUP_BUDGET_SECONDS = 1.0
PROFILE_PROCESS_STOP_RESERVE_SECONDS = 0.1

_sweep_triggered = False
_sweep_trigger_lock = threading.Lock()

_Identity = tuple[int, int]


class _ProfileIdentityChangedError(RuntimeError):
    pass


def _identity(result: os.stat_result) -> _Identity:
    return result.st_dev, result.st_ino


def _validate_directory(path: Path, uid: int, mode: int) -> os.stat_result:
    result = os.lstat(path)
    if not stat.S_ISDIR(result.st_mode) or result.st_uid != uid or stat.S_IMODE(result.st_mode) != mode:
        raise RuntimeError(f"Unsafe local browser profile directory: {path}")
    return result


def _validate_lock_path(path: Path, fd: int, uid: int) -> os.stat_result:
    path_result = os.lstat(path)
    fd_result = os.fstat(fd)
    if (
        not stat.S_ISREG(path_result.st_mode)
        or not stat.S_ISREG(fd_result.st_mode)
        or path_result.st_uid != uid
        or fd_result.st_uid != uid
        or stat.S_IMODE(path_result.st_mode) != PROFILE_LOCK_MODE
        or stat.S_IMODE(fd_result.st_mode) != PROFILE_LOCK_MODE
        or _identity(path_result) != _identity(fd_result)
    ):
        raise RuntimeError(f"Unsafe local browser profile lock: {path}")
    return fd_result


def _lock_exclusive_nonblocking(fd: int) -> None:
    import fcntl  # noqa: PLC0415

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    import fcntl  # noqa: PLC0415

    fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_open_flags(*, create: bool) -> int:
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _directory_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _matches_directory(
    result: os.stat_result,
    *,
    uid: int,
    mode: int,
    expected_identity: _Identity,
) -> bool:
    return (
        stat.S_ISDIR(result.st_mode)
        and result.st_uid == uid
        and stat.S_IMODE(result.st_mode) == mode
        and _identity(result) == expected_identity
    )


def _profile_root(uid: int) -> Path:
    return Path(tempfile.gettempdir()) / f"{PROFILE_ROOT_PREFIX}{uid}"


def _prepare_profile_root() -> tuple[Path, int, os.stat_result]:
    uid = os.getuid()
    root = _profile_root(uid)
    try:
        root.mkdir(mode=PROFILE_ROOT_MODE)
    except FileExistsError:
        pass
    else:
        root.chmod(PROFILE_ROOT_MODE)
    return root, uid, _validate_directory(root, uid, PROFILE_ROOT_MODE)


@dataclass(slots=True)
class LocalBrowserProfile:
    path: Path
    _root: Path
    _uid: int
    _root_identity: _Identity
    _profile_identity: _Identity
    _lock_identity: _Identity
    _lock_fd: int | None
    _state_lock: LockType = field(default_factory=threading.Lock, repr=False)
    _cleanup_started: bool = field(default=False, repr=False)

    def revalidate(self) -> bool:
        with self._state_lock:
            fd = self._lock_fd
        if fd is None or self.path.parent != self._root:
            return False
        try:
            root_result = _validate_directory(self._root, self._uid, PROFILE_ROOT_MODE)
            profile_result = _validate_directory(self.path, self._uid, PROFILE_DIR_MODE)
            lock_result = _validate_lock_path(self.path / PROFILE_LOCK_FILENAME, fd, self._uid)
            _lock_exclusive_nonblocking(fd)
        except (OSError, RuntimeError):
            return False
        return (
            _identity(root_result) == self._root_identity
            and _identity(profile_result) == self._profile_identity
            and _identity(lock_result) == self._lock_identity
        )

    def release(self) -> None:
        with self._state_lock:
            fd = self._lock_fd
            self._lock_fd = None
        if fd is None:
            return
        try:
            try:
                _unlock(fd)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _begin_cleanup(self) -> bool:
        with self._state_lock:
            if self._cleanup_started:
                return False
            self._cleanup_started = True
            return True


def sweep_local_browser_profiles_once_in_background() -> threading.Thread | None:
    if sys.platform == "win32":
        return None

    global _sweep_triggered
    with _sweep_trigger_lock:
        if _sweep_triggered:
            return None
        _sweep_triggered = True

    thread = threading.Thread(
        target=sweep_local_browser_profiles_with_budget,
        daemon=True,
        name="skyvern-profile-sweep",
    )
    thread.start()
    return thread


def create_local_browser_profile() -> LocalBrowserProfile | None:
    if sys.platform == "win32":
        return None

    sweep_local_browser_profiles_once_in_background()

    # A concurrent startup sweep can win the race in the mkdtemp->flock window: reaping the
    # still-lockless dir (FileNotFoundError) or acquiring the just-published lock first
    # (BlockingIOError — nobody else can hold a lock we created O_EXCL). Retry with a fresh dir.
    last_error: OSError | None = None
    for _ in range(3):
        try:
            return _create_local_browser_profile_once()
        except (FileNotFoundError, BlockingIOError) as exc:
            last_error = exc
    raise RuntimeError("Local browser profile creation kept losing its directory") from last_error


def _create_local_browser_profile_once() -> LocalBrowserProfile:
    root, uid, root_result = _prepare_profile_root()
    profile_path = Path(tempfile.mkdtemp(prefix=PROFILE_DIR_PREFIX, dir=root))
    lock_fd: int | None = None
    lock_acquired = False
    try:
        profile_path.chmod(PROFILE_DIR_MODE)
        profile_result = _validate_directory(profile_path, uid, PROFILE_DIR_MODE)
        lock_path = profile_path / PROFILE_LOCK_FILENAME
        lock_fd = os.open(lock_path, _lock_open_flags(create=True), PROFILE_LOCK_MODE)
        os.fchmod(lock_fd, PROFILE_LOCK_MODE)
        lock_result = _validate_lock_path(lock_path, lock_fd, uid)
        _lock_exclusive_nonblocking(lock_fd)
        lock_acquired = True
        profile = LocalBrowserProfile(
            path=profile_path,
            _root=root,
            _uid=uid,
            _root_identity=_identity(root_result),
            _profile_identity=_identity(profile_result),
            _lock_identity=_identity(lock_result),
            _lock_fd=lock_fd,
        )
        lock_fd = None
        if not profile.revalidate():
            profile.release()
            raise _ProfileIdentityChangedError("Local browser profile identity changed during creation")
        return profile
    except BlockingIOError:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    except _ProfileIdentityChangedError:
        raise
    except Exception:
        if lock_fd is not None:
            try:
                if lock_acquired:
                    _unlock(lock_fd)
            finally:
                os.close(lock_fd)
        try:
            _remove_profile_directory_bounded(profile_path)
        except Exception:
            LOG.warning("local_browser_profile_creation_rollback_failed", path=str(profile_path), exc_info=True)
        raise


def _matching_processes(user_data_dir: Path) -> list[psutil.Process] | None:
    user_data_arg = f"--user-data-dir={user_data_dir}"
    matches: dict[int, psutil.Process] = {}
    try:
        for process in psutil.process_iter(["pid", "cmdline"]):
            info = process.info
            cmdline = None if info is None else info.get("cmdline")
            if not cmdline:
                # Our Chromium is an ordinary same-uid child, so its args are readable; blank same-uid entries are
                # zombies or arg-restricted binaries, while other users cannot traverse the 0700 profile root.
                continue
            if user_data_arg not in cmdline:
                continue
            matches[process.pid] = process
            for child in process.children(recursive=True):
                matches[child.pid] = child
    except Exception:
        return None
    return list(matches.values())


def terminate_local_browser_processes(user_data_dir: str | Path) -> bool:
    path = Path(user_data_dir)
    scanned = _matching_processes(path)
    if scanned is None:
        return False
    processes = {process.pid: process for process in scanned}
    for process in processes.values():
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except Exception:
            return False
    if not processes:
        return True
    try:
        _gone, alive = psutil.wait_procs(list(processes.values()), timeout=PROCESS_KILL_TIMEOUT_SECONDS)
    except Exception:
        return False
    return not alive


def _wait_for_process_with_budget(
    process: subprocess.Popen[bytes],
    *,
    started_at: float,
    budget: float,
    process_group: bool = False,
) -> int | None:
    deadline = started_at + max(budget, 0.0)
    stop_reserve = min(PROFILE_PROCESS_STOP_RESERVE_SECONDS, max(budget / 2, 0.0))
    work_timeout = max(0.0, deadline - time.monotonic() - stop_reserve)
    try:
        return process.wait(timeout=work_timeout)
    except subprocess.TimeoutExpired:
        pass

    try:
        if process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return None

    terminate_timeout = min(stop_reserve / 2, max(0.0, deadline - time.monotonic()))
    try:
        process.wait(timeout=terminate_timeout)
    except subprocess.TimeoutExpired:
        pass

    if process_group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return None
    elif process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            return None

    if process.poll() is None:
        try:
            # SIGKILL is asynchronous. Keep the work deadline bounded, but allow the
            # configured stop reserve to reap the child before returning to the caller.
            process.wait(timeout=max(stop_reserve, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return None
    return None


def _remove_verified_directory_via_tombstone(
    root_fd: int,
    name: str,
    *,
    expected_identity: _Identity,
    uid: int,
) -> bool:
    tombstone = f".reap-{uuid4().hex}"
    try:
        os.rename(name, tombstone, src_dir_fd=root_fd, dst_dir_fd=root_fd)
    except OSError:
        return False

    def restore_original_name() -> None:
        try:
            os.rename(tombstone, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        except OSError:
            pass

    try:
        tombstone_fd = os.open(tombstone, _directory_open_flags(), dir_fd=root_fd)
    except OSError:
        restore_original_name()
        return False
    try:
        try:
            result = os.fstat(tombstone_fd)
        except OSError:
            restore_original_name()
            return False

        if _identity(result) == expected_identity:
            try:
                os.rmdir(tombstone, dir_fd=root_fd)
            except OSError:
                restore_original_name()
                return False
            # APFS keeps st_nlink on the held fd after rmdir, so confirm removal by absence:
            # the verified inode still existing under any root entry means a same-name swap
            # won the race and rmdir removed the impostor instead. This scans the whole root
            # per removal — fine at local-dev profile counts; revisit if the root grows large.
            try:
                for entry in os.listdir(root_fd):
                    if _identity(os.lstat(entry, dir_fd=root_fd)) == expected_identity:
                        return False
            except OSError:
                return False
            return True

        if stat.S_ISDIR(result.st_mode) and result.st_uid == uid:
            try:
                os.rmdir(tombstone, dir_fd=root_fd)
            except OSError:
                restore_original_name()
            return False

        restore_original_name()
        return False
    finally:
        os.close(tombstone_fd)


def _delete_profile_path(path: Path, *, preserve_lock: bool, expected_identity: _Identity | None = None) -> bool:
    if sys.platform != "win32":
        if expected_identity is None or not shutil.rmtree.avoids_symlink_attacks:
            return False
        flags = _directory_open_flags()
        try:
            parent_fd = os.open(path.parent, flags)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            try:
                profile_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            try:
                if _identity(os.fstat(profile_fd)) != expected_identity:
                    return False
                for name in os.listdir(profile_fd):
                    if preserve_lock and name == PROFILE_LOCK_FILENAME:
                        continue
                    try:
                        result = os.lstat(name, dir_fd=profile_fd)
                        if stat.S_ISDIR(result.st_mode):
                            shutil.rmtree(name, dir_fd=profile_fd)
                        else:
                            os.unlink(name, dir_fd=profile_fd)
                    except FileNotFoundError:
                        continue
                if preserve_lock:
                    return True
                return _remove_verified_directory_via_tombstone(
                    parent_fd,
                    path.name,
                    expected_identity=expected_identity,
                    uid=os.getuid(),
                )
            except OSError:
                return False
            finally:
                os.close(profile_fd)
        finally:
            os.close(parent_fd)

    try:
        for entry in path.iterdir():
            if preserve_lock and entry.name == PROFILE_LOCK_FILENAME:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except FileNotFoundError:
                continue
        if not preserve_lock:
            path.rmdir()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _remove_profile_directory_bounded(
    path: Path,
    *,
    preserve_lock: bool = False,
    expected_identity: _Identity | None = None,
) -> bool:
    started_at = time.monotonic()
    mode = "--delete-profile-contents" if preserve_lock else "--delete-profile-tree"
    command = [sys.executable, "-m", "skyvern.library.local_browser_profile", mode, str(path)]
    if sys.platform != "win32":
        if expected_identity is None:
            try:
                expected_identity = _identity(os.lstat(path))
            except FileNotFoundError:
                return True
            except OSError:
                return False
        command.extend((str(expected_identity[0]), str(expected_identity[1])))
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return_code = _wait_for_process_with_budget(
        process,
        started_at=started_at,
        budget=PROFILE_DELETE_TIMEOUT_SECONDS,
    )
    return return_code == 0 and (path.exists() if preserve_lock else not path.exists())


def _finalize_managed_profile_removal(profile: LocalBrowserProfile) -> bool:
    if not profile.revalidate():
        return False
    flags = _directory_open_flags()
    try:
        root_fd = os.open(profile._root, flags)
    except OSError:
        return False
    try:
        if not _matches_directory(
            os.fstat(root_fd),
            uid=profile._uid,
            mode=PROFILE_ROOT_MODE,
            expected_identity=profile._root_identity,
        ):
            return False
        try:
            profile_fd = os.open(profile.path.name, flags, dir_fd=root_fd)
        except OSError:
            return False
        try:
            if not _matches_directory(
                os.fstat(profile_fd),
                uid=profile._uid,
                mode=PROFILE_DIR_MODE,
                expected_identity=profile._profile_identity,
            ):
                return False
            try:
                os.unlink(PROFILE_LOCK_FILENAME, dir_fd=profile_fd)
            except OSError:
                return False
        finally:
            os.close(profile_fd)

        return _remove_verified_directory_via_tombstone(
            root_fd,
            profile.path.name,
            expected_identity=profile._profile_identity,
            uid=profile._uid,
        )
    finally:
        os.close(root_fd)


def cleanup_local_browser_profile(profile: LocalBrowserProfile | str | Path) -> bool:
    managed = profile if isinstance(profile, LocalBrowserProfile) else None
    path = managed.path if managed is not None else Path(profile)
    if managed is not None and not managed._begin_cleanup():
        return not path.exists()
    try:
        if managed is not None and not managed.revalidate():
            return False
        terminated = terminate_local_browser_processes(path)
        if not terminated:
            return False
        if managed is not None and not managed.revalidate():
            return False
        if managed is None:
            return _remove_profile_directory_bounded(path)
        if not _remove_profile_directory_bounded(
            path,
            preserve_lock=True,
            expected_identity=managed._profile_identity,
        ):
            return False
        return _finalize_managed_profile_removal(managed)
    except Exception:
        LOG.warning("local_browser_profile_cleanup_failed", path=str(path), exc_info=True)
        return False
    finally:
        if managed is not None:
            managed.release()


def _remove_lockless_empty_sweep_candidate(
    root: Path,
    uid: int,
    root_result: os.stat_result,
    path: Path,
    profile_result: os.stat_result,
) -> bool:
    flags = _directory_open_flags()
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return False
    try:
        if not _matches_directory(
            os.fstat(root_fd),
            uid=uid,
            mode=PROFILE_ROOT_MODE,
            expected_identity=_identity(root_result),
        ):
            return False
        return _remove_verified_directory_via_tombstone(
            root_fd,
            path.name,
            expected_identity=_identity(profile_result),
            uid=uid,
        )
    except OSError:
        return False
    finally:
        os.close(root_fd)


def _remove_leftover_tombstone(
    root: Path,
    uid: int,
    root_result: os.stat_result,
    path: Path,
) -> bool:
    flags = _directory_open_flags()
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return False
    try:
        if not _matches_directory(
            os.fstat(root_fd),
            uid=uid,
            mode=PROFILE_ROOT_MODE,
            expected_identity=_identity(root_result),
        ):
            return False
        tombstone_fd = os.open(path.name, flags, dir_fd=root_fd)
        try:
            result = os.fstat(tombstone_fd)
            if not stat.S_ISDIR(result.st_mode) or result.st_uid != uid:
                return False
        finally:
            os.close(tombstone_fd)
        os.rmdir(path.name, dir_fd=root_fd)
        return True
    except OSError:
        return False
    finally:
        os.close(root_fd)


def _open_sweep_candidate(
    root: Path,
    uid: int,
    root_result: os.stat_result,
    path: Path,
) -> tuple[LocalBrowserProfile | None, bool]:
    lock_fd: int | None = None
    try:
        profile_result = _validate_directory(path, uid, PROFILE_DIR_MODE)
        lock_path = path / PROFILE_LOCK_FILENAME
        try:
            lock_fd = os.open(lock_path, _lock_open_flags(create=False))
        except FileNotFoundError:
            return None, _remove_lockless_empty_sweep_candidate(root, uid, root_result, path, profile_result)
        lock_result = _validate_lock_path(lock_path, lock_fd, uid)
        try:
            _lock_exclusive_nonblocking(lock_fd)
        except BlockingIOError:
            os.close(lock_fd)
            return None, False
        profile = LocalBrowserProfile(
            path=path,
            _root=root,
            _uid=uid,
            _root_identity=_identity(root_result),
            _profile_identity=_identity(profile_result),
            _lock_identity=_identity(lock_result),
            _lock_fd=lock_fd,
        )
        if not profile.revalidate():
            profile.release()
            return None, False
        return profile, False
    except (OSError, RuntimeError):
        if lock_fd is not None:
            os.close(lock_fd)
        return None, False


def sweep_orphaned_local_browser_profiles() -> int:
    if sys.platform == "win32":
        return 0
    try:
        root, uid, root_result = _prepare_profile_root()
        candidates = list(root.iterdir())
    except (OSError, RuntimeError):
        LOG.warning("local_browser_profile_sweep_root_unsafe", exc_info=True)
        return 0

    removed = 0
    for path in candidates:
        if path.name.startswith("."):
            if path.name.startswith(".reap-") and _remove_leftover_tombstone(root, uid, root_result, path):
                removed += 1
            continue
        profile, recovered = _open_sweep_candidate(root, uid, root_result, path)
        if recovered or (profile is not None and cleanup_local_browser_profile(profile)):
            removed += 1
    return removed


def sweep_local_browser_profiles_with_budget() -> None:
    if sys.platform == "win32":
        return

    global _sweep_triggered
    with _sweep_trigger_lock:
        _sweep_triggered = True

    with tempfile.TemporaryFile() as capture_file:
        started_at = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-m", "skyvern.library.local_browser_profile", "--sweep"],
            stdin=subprocess.DEVNULL,
            stdout=capture_file,
            stderr=capture_file,
            start_new_session=True,
        )
        return_code = _wait_for_process_with_budget(
            process,
            started_at=started_at,
            budget=PROFILE_SWEEP_STARTUP_BUDGET_SECONDS,
            process_group=True,
        )
        capture_file.seek(0, os.SEEK_END)
        capture_file.seek(max(capture_file.tell() - 2048, 0))
        output_tail = capture_file.read(2048).decode(errors="replace").strip()

    if return_code is None:
        LOG.warning("local_browser_profile_sweep_timed_out", output_tail=output_tail)
        return

    if return_code != 0:
        LOG.warning(
            "local_browser_profile_sweep_failed",
            return_code=return_code,
            output_tail=output_tail,
        )
    elif output_tail:
        LOG.warning("local_browser_profile_sweep_output", return_code=return_code, output_tail=output_tail)


if __name__ == "__main__":
    if sys.argv[1:] == ["--sweep"]:
        sweep_orphaned_local_browser_profiles()
    elif (
        sys.platform == "win32"
        and len(sys.argv) == 3
        and sys.argv[1] in {"--delete-profile-contents", "--delete-profile-tree"}
    ):
        deleted = _delete_profile_path(
            Path(sys.argv[2]),
            preserve_lock=sys.argv[1] == "--delete-profile-contents",
        )
        raise SystemExit(0 if deleted else 1)
    elif (
        sys.platform != "win32"
        and len(sys.argv) == 5
        and sys.argv[1] in {"--delete-profile-contents", "--delete-profile-tree"}
    ):
        try:
            expected_identity = int(sys.argv[3]), int(sys.argv[4])
        except ValueError:
            raise SystemExit(2) from None
        deleted = _delete_profile_path(
            Path(sys.argv[2]),
            preserve_lock=sys.argv[1] == "--delete-profile-contents",
            expected_identity=expected_identity,
        )
        raise SystemExit(0 if deleted else 1)
    else:
        raise SystemExit(2)
