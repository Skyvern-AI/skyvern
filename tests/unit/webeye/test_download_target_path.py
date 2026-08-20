from __future__ import annotations

from pathlib import Path

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions.handler import _download_target_path
from skyvern.webeye.cdp_download_interceptor import download_filename_from_suffix

_SITE_UUID = "0faafbe5-fc0e-4cb6-9947-332fe1405073.pdf"


def test_suffix_without_extension_appends_source_extension() -> None:
    assert download_filename_from_suffix("REQ-1", ".pdf", set()) == "REQ-1.pdf"


def test_suffix_with_extension_is_used_verbatim() -> None:
    # A suffix that already carries an extension must not be double-suffixed.
    assert download_filename_from_suffix("report.pdf", ".pdf", set()) == "report.pdf"


def test_suffix_collisions_are_deduped() -> None:
    assert download_filename_from_suffix("REQ-1", ".pdf", {"REQ-1.pdf"}) == "REQ-1_1.pdf"
    assert download_filename_from_suffix("report.pdf", ".pdf", {"report.pdf"}) == "report_1.pdf"


def test_suffix_dedup_normalizes_full_path_existing_names() -> None:
    # A caller that passes absolute paths must not defeat dedup (would silently overwrite the first file).
    assert download_filename_from_suffix("REQ-1", ".pdf", {"/downloads/REQ-1.pdf"}) == "REQ-1_1.pdf"


def test_download_target_path_prefers_download_suffix(tmp_path: Path) -> None:
    with skyvern_context.scoped(SkyvernContext(download_suffix="REQ-1")):
        target = _download_target_path(tmp_path, _SITE_UUID)
    # The site's UUID name is replaced by the request-based name.
    assert target.name == "REQ-1.pdf"
    assert target.parent == tmp_path


def test_download_target_path_without_suffix_keeps_site_stem(tmp_path: Path) -> None:
    with skyvern_context.scoped(SkyvernContext(download_suffix=None)):
        target = _download_target_path(tmp_path, "invoice.pdf")
    assert target.name.endswith("-invoice.pdf")
    assert target.name != "invoice.pdf"


def test_download_target_path_dedupes_same_suffix_in_dir(tmp_path: Path) -> None:
    (tmp_path / "REQ-1.pdf").write_bytes(b"x")
    with skyvern_context.scoped(SkyvernContext(download_suffix="REQ-1")):
        target = _download_target_path(tmp_path, "site-uuid.pdf")
    assert target.name == "REQ-1_1.pdf"
