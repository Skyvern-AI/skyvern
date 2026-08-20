import os
import time
from pathlib import Path

from skyvern.config import settings
from skyvern.forge.sdk.artifact.storage.local import LocalStorage
from skyvern.services.cleanup_service import start_temp_artifact_sweep, sweep_stale_temp_artifacts


class _RemoteStorage:
    """Stand-in for any non-local backend (S3/Azure/GCS) that uploads downloads off-disk."""


def _set_backend(monkeypatch, storage: object) -> None:
    monkeypatch.setattr("skyvern.services.cleanup_service.StorageFactory.get_storage", lambda: storage)


def _make_entry(base: Path, name: str, *, age_hours: float, is_dir: bool = True) -> Path:
    entry = base / name
    if is_dir:
        entry.mkdir(parents=True)
        (entry / "payload.bin").write_bytes(b"x" * 16)
    else:
        entry.write_bytes(b"x" * 16)
    stamp = time.time() - age_hours * 3600
    os.utime(entry, (stamp, stamp))
    return entry


def _patch_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    temp_dir = tmp_path / "temp"
    log_dir = tmp_path / "log"
    download_dir = tmp_path / "downloads"
    for directory in (temp_dir, log_dir, download_dir):
        directory.mkdir()
    monkeypatch.setattr(settings, "TEMP_PATH", str(temp_dir))
    monkeypatch.setattr(settings, "LOG_PATH", str(log_dir))
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(download_dir))
    return temp_dir, log_dir, download_dir


def test_sweep_removes_aged_log_and_download_entries(tmp_path, monkeypatch):
    _, log_dir, download_dir = _patch_paths(monkeypatch, tmp_path)
    _set_backend(monkeypatch, _RemoteStorage())

    stale_log_day = _make_entry(log_dir, "2026-07-01", age_hours=72)
    fresh_log_day = _make_entry(log_dir, "2026-07-15", age_hours=1)
    stale_download = _make_entry(download_dir, "wr_123", age_hours=72)
    fresh_download = _make_entry(download_dir, "wr_456", age_hours=1)

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 2
    assert not stale_log_day.exists()
    assert not stale_download.exists()
    assert fresh_log_day.exists()
    assert fresh_download.exists()


def test_sweep_removes_aged_video_and_har_day_dirs(tmp_path, monkeypatch):
    # Multi-activity workers no longer wipe these roots at teardown (SKY-14139), so the sweep is
    # their only reaper. Both are per-day dirs like LOG_PATH: an aged day is genuinely finished.
    _patch_paths(monkeypatch, tmp_path)
    video_dir = tmp_path / "video"
    har_dir = tmp_path / "har"
    for directory in (video_dir, har_dir):
        directory.mkdir()
    monkeypatch.setattr(settings, "VIDEO_PATH", str(video_dir))
    monkeypatch.setattr(settings, "HAR_PATH", str(har_dir))
    _set_backend(monkeypatch, _RemoteStorage())

    stale_video_day = _make_entry(video_dir, "2026-07-01", age_hours=72)
    fresh_video_day = _make_entry(video_dir, "2026-07-15", age_hours=1)
    stale_har_day = _make_entry(har_dir, "2026-07-01", age_hours=72)
    fresh_har_day = _make_entry(har_dir, "2026-07-15", age_hours=1)

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 2
    assert not stale_video_day.exists()
    assert not stale_har_day.exists()
    assert fresh_video_day.exists()
    assert fresh_har_day.exists()


def test_sweep_skips_unconfigured_video_and_har_paths(tmp_path, monkeypatch):
    # An empty path setting must be skipped outright: Path("") is the working directory, and
    # sweeping it would eat whatever the process happens to be running in.
    _, log_dir, _ = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "VIDEO_PATH", "")
    monkeypatch.setattr(settings, "HAR_PATH", "")
    _set_backend(monkeypatch, _RemoteStorage())

    stale_log_day = _make_entry(log_dir, "2026-07-01", age_hours=72)

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 1
    assert not stale_log_day.exists()


def test_sweep_spares_download_path_on_local_backend(tmp_path, monkeypatch):
    # On the local storage backend DOWNLOAD_PATH/<run_id> is the run's permanent artifact record
    # (served via file://, never uploaded), so the sweep must leave it alone while still clearing LOG_PATH.
    _, log_dir, download_dir = _patch_paths(monkeypatch, tmp_path)
    _set_backend(monkeypatch, LocalStorage())

    stale_log_day = _make_entry(log_dir, "2026-07-01", age_hours=72)
    stale_download = _make_entry(download_dir, "wr_123", age_hours=72)

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 1
    assert not stale_log_day.exists()
    assert stale_download.exists()


def test_sweep_never_touches_temp_path(tmp_path, monkeypatch):
    # TEMP_PATH mixes reused script caches and browser-session profile dirs whose mtime is
    # frozen while in use, so the age-gated sweep must leave TEMP_PATH alone entirely.
    temp_dir, _, _ = _patch_paths(monkeypatch, tmp_path)

    script_cache = _make_entry(temp_dir, "s_abc123", age_hours=720)
    profile_dir = _make_entry(temp_dir, "skyvern_browser_profile_xyz", age_hours=720)
    loose_file = _make_entry(temp_dir, "orphan.zip", age_hours=720, is_dir=False)

    assert sweep_stale_temp_artifacts(max_age_hours=48) == 0
    assert script_cache.exists()
    assert profile_dir.exists()
    assert loose_file.exists()


def _make_aged_run_dir(runs_org: Path, run_id: str, *, age_hours: float) -> Path:
    """A run dir whose CHILDREN are aged too — deep staleness, not just the root's mtime."""
    run_dir = _make_entry(runs_org, run_id, age_hours=age_hours)
    stamp = time.time() - age_hours * 3600
    for child in run_dir.rglob("*"):
        os.utime(child, (stamp, stamp))
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def test_sweep_reaps_aged_run_scoped_temp_dirs_only(tmp_path, monkeypatch):
    # TEMP_PATH stays excluded from the sweep EXCEPT runs/<org>/<run>: that namespace is
    # single-tenant and keyed by run identity, so an aged entry is a crash-path orphan whose
    # teardown never ran (SKY-14159). Everything else under TEMP_PATH keeps the mtime exemption.
    temp_dir, log_dir, _ = _patch_paths(monkeypatch, tmp_path)
    _set_backend(monkeypatch, _RemoteStorage())

    runs_org = temp_dir / "runs" / "o_1"
    runs_org.mkdir(parents=True)
    stale_run = _make_aged_run_dir(runs_org, "wr_dead", age_hours=72)
    fresh_run = _make_entry(runs_org, "wr_live", age_hours=1)
    untouched_cache = _make_entry(temp_dir, "s_script_cache", age_hours=720)

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 1
    assert not stale_run.exists()
    assert fresh_run.exists()
    assert untouched_cache.exists()


def test_sweep_keeps_an_old_run_root_with_a_fresh_child(tmp_path, monkeypatch):
    # Writes into an existing staging child do not refresh the run root's mtime, and download
    # timeouts have no upper bound — a >48h run can still be mid-download. Root age alone must
    # never delete a directory whose subtree is fresh (#15381 review P1).
    temp_dir, _, _ = _patch_paths(monkeypatch, tmp_path)
    _set_backend(monkeypatch, _RemoteStorage())

    runs_org = temp_dir / "runs" / "o_1"
    runs_org.mkdir(parents=True)
    old_root = runs_org / "wr_long"
    in_flight = old_root / "xhr_staging_ab" / "partial.bin"
    in_flight.parent.mkdir(parents=True)
    in_flight.write_bytes(b"placeholder")
    stamp = time.time() - 72 * 3600
    for path in (in_flight, in_flight.parent, old_root):
        os.utime(path, (stamp, stamp))
    # A write to an EXISTING child refreshes only the file, never the ancestors' mtimes.
    in_flight.write_bytes(b"still-downloading")

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 0
    assert in_flight.read_bytes() == b"still-downloading"


def test_sweep_never_follows_symlinked_run_namespace_ancestors(tmp_path, monkeypatch):
    # runs/ or an org dir being a symlink must not let the sweep resolve outside TEMP_PATH and
    # delete foreign directories (#15381 review P0).
    temp_dir, _, _ = _patch_paths(monkeypatch, tmp_path)
    _set_backend(monkeypatch, _RemoteStorage())

    outside = tmp_path / "outside" / "runs_target"
    outside.mkdir(parents=True)
    victim = _make_aged_run_dir(outside, "wr_victim", age_hours=72)

    (temp_dir / "runs").mkdir()
    (temp_dir / "runs" / "o_linked").symlink_to(outside, target_is_directory=True)

    removed = sweep_stale_temp_artifacts(max_age_hours=48)

    assert removed == 0
    assert victim.exists()


def test_sweep_disabled_when_gate_nonpositive(tmp_path, monkeypatch):
    _, log_dir, _ = _patch_paths(monkeypatch, tmp_path)
    stale = _make_entry(log_dir, "2026-07-01", age_hours=72)

    assert sweep_stale_temp_artifacts(max_age_hours=0) == 0
    assert stale.exists()


def test_sweep_tolerates_missing_base_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOG_PATH", str(tmp_path / "nope_log"))
    monkeypatch.setattr(settings, "DOWNLOAD_PATH", str(tmp_path / "nope_dl"))

    assert sweep_stale_temp_artifacts(max_age_hours=48) == 0


def test_sweep_default_gate_comes_from_settings(tmp_path, monkeypatch):
    _, log_dir, _ = _patch_paths(monkeypatch, tmp_path)
    stale = _make_entry(log_dir, "2026-07-01", age_hours=72)
    monkeypatch.setattr(settings, "TEMP_ARTIFACT_SWEEP_MAX_AGE_HOURS", 48.0)

    assert sweep_stale_temp_artifacts() == 1
    assert not stale.exists()


def test_start_sweep_disabled_by_nonpositive_gate(monkeypatch):
    monkeypatch.setattr(settings, "TEMP_ARTIFACT_SWEEP_MAX_AGE_HOURS", 0.0)
    assert start_temp_artifact_sweep() is None
