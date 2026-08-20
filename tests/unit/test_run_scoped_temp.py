"""TEMP_PATH discipline: per-run scratch lives under ``runs/<org>/<run>``.

Every tenant that stages per-run files directly under TEMP_PATH invents its own path shape, and
cleanup then needs per-tenant knowledge — which is how the fleet ended up with files no reaper
could claim (SKY-14159). ``get_run_temp_dir`` is the one sanctioned way to stage per-run temp:
everything under it is deletable by run identity alone, by teardown and by the stale sweep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.api.files import RUN_TEMP_NAMESPACE, get_run_temp_dir


def test_run_temp_dir_is_namespaced_and_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path))

    run_dir = Path(get_run_temp_dir("o_1", "wr_1"))

    assert run_dir == tmp_path / RUN_TEMP_NAMESPACE / "o_1" / "wr_1"
    assert run_dir.is_dir()


@pytest.mark.parametrize("bad_component", ["..", ".", "a/b", "/etc", "./.."])
def test_run_temp_dir_rejects_non_single_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_component: str
) -> None:
    # The directory is fed to rmtree by run identity later; pathlib does not normalize dot
    # segments, so both components must be exactly one plain path element (see SKY-14138's guard).
    monkeypatch.setattr(settings, "TEMP_PATH", str(tmp_path))

    with pytest.raises(ValueError):
        get_run_temp_dir(bad_component, "wr_1")
    with pytest.raises(ValueError):
        get_run_temp_dir("o_1", bad_component)


def test_run_temp_dir_refuses_symlinked_ancestors_out_of_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Creation must not follow a symlinked runs/ or org ancestor outside TEMP_PATH — makedirs
    # would otherwise materialize run dirs on foreign filesystems (#15381 review P0).
    temp = tmp_path / "temp"
    outside = tmp_path / "outside"
    (temp / RUN_TEMP_NAMESPACE).mkdir(parents=True)
    outside.mkdir()
    (temp / RUN_TEMP_NAMESPACE / "o_linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(settings, "TEMP_PATH", str(temp))

    with pytest.raises(ValueError):
        get_run_temp_dir("o_linked", "wr_1")
    assert not (outside / "wr_1").exists()


def test_temp_path_tenant_allowlist() -> None:
    """New per-run temp MUST go through ``get_run_temp_dir``; direct TEMP_PATH tenants are frozen.

    This is the enforcement that keeps the structure from eroding: a new file calling
    ``get_skyvern_temp_dir()`` or ``make_temp_directory(`` fails here until it either uses the
    run-scoped helper or is deliberately added below with an owner and a lifecycle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    # cloud/ and workers/ exist only in the cloud repo; this test is OSS-synced, so scan and
    # expect only the roots actually present.
    present_roots = [root for root in ("skyvern", "cloud", "workers") if (repo_root / root).is_dir()]
    offenders: set[str] = set()
    for source_root in present_roots:
        for source_file in (repo_root / source_root).rglob("*.py"):
            text = source_file.read_text(errors="ignore")
            if "get_skyvern_temp_dir()" in text or "make_temp_directory(" in text:
                offenders.add(str(source_file.relative_to(repo_root)))

    allowed = {
        # The helpers themselves.
        "skyvern/forge/sdk/api/files.py",
        # Streaming frames: cross-service key contract (API local backend + S3 layout + display
        # capture all read/write <org>/<stream_key>); reaped by the publisher's owner registry.
        "skyvern/webeye/cdp_frame_publisher.py",
        "skyvern/forge/sdk/artifact/storage/local.py",
        "skyvern/forge/sdk/artifact/storage/s3.py",
        "skyvern/forge/sdk/artifact/storage/gcs.py",
        "skyvern/forge/sdk/artifact/storage/azure.py",
        # Pre-creates the org frame dir at activity start.
        "workers/temporal_v2_worker/activities.py",
        # X-display singleton state file (single-run pods only).
        "skyvern/utils/files.py",
        # Session-scoped recording staging; reaped by unlink-after-sync at session close.
        "cloud/webeye/vendor_recording.py",
        # Browser profile / user-data staging on launch lanes (single-run pods wipe TEMP_PATH).
        "skyvern/webeye/browser_factory.py",
        "skyvern/forge/sdk/routes/browser_profiles.py",
        "cloud/webeye/profile_cache.py",
        "cloud/browser_profile/banking.py",
    }
    allowed = {entry for entry in allowed if entry.split("/", 1)[0] in present_roots}
    assert offenders == allowed, (
        "TEMP_PATH tenant drift. New per-run temp must use get_run_temp_dir(); "
        f"unexpected: {sorted(offenders - allowed)}; stale allowlist entries: {sorted(allowed - offenders)}"
    )
