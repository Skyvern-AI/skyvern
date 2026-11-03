from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from skyvern.forge.sdk.api.files import recover_download_extension
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions.handler import _download_target_path
from skyvern.webeye.cdp_download_interceptor import (
    ORIGINAL_FILENAME_MARKER,
    download_filename_from_suffix,
    unique_download_filename,
)

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


def test_original_filename_marker_prefixes_the_site_name() -> None:
    assert (
        download_filename_from_suffix(
            f"Fund-A_{ORIGINAL_FILENAME_MARKER}", ".pdf", set(), original_filename="Q4 Report.pdf"
        )
        == "Fund-A_Q4 Report.pdf"
    )


def test_original_filename_marker_keeps_source_extension_after_dotted_prefix() -> None:
    assert (
        download_filename_from_suffix(
            f"Acme Capital L.P._{ORIGINAL_FILENAME_MARKER}",
            ".pdf",
            set(),
            original_filename="Q4 Report.pdf",
        )
        == "Acme Capital L.P._Q4 Report.pdf"
    )


def test_original_filename_marker_alone_keeps_the_site_name() -> None:
    assert (
        download_filename_from_suffix(ORIGINAL_FILENAME_MARKER, ".pdf", set(), original_filename="Q4 Report.pdf")
        == "Q4 Report.pdf"
    )


def test_original_filename_marker_uses_the_source_extension_when_the_site_name_has_none() -> None:
    assert (
        download_filename_from_suffix(f"ACME_{ORIGINAL_FILENAME_MARKER}", ".xlsx", set(), original_filename="export")
        == "ACME_export.xlsx"
    )


def test_original_filename_marker_as_suffix_does_not_lose_the_extension() -> None:
    assert (
        download_filename_from_suffix(
            f"{ORIGINAL_FILENAME_MARKER}_archived", ".pdf", set(), original_filename="report.pdf"
        )
        == "report_archived.pdf"
    )


def test_original_filename_marker_sanitizes_a_traversing_site_name() -> None:
    # The site picks this name, and a Windows-style traversal survives Path().name on POSIX.
    assert (
        download_filename_from_suffix(
            f"ACME_{ORIGINAL_FILENAME_MARKER}", ".pdf", set(), original_filename="..\\..\\secret.pdf"
        )
        == "ACME_secret.pdf"
    )


def test_original_filename_marker_falls_back_when_the_site_names_nothing() -> None:
    assert (
        download_filename_from_suffix(ORIGINAL_FILENAME_MARKER, ".pdf", set(), original_filename=None) == "download.pdf"
    )


def test_original_filename_marker_bounds_a_site_name_the_filesystem_would_reject() -> None:
    # The site controls this part of the name; a basename over ~255 bytes fails the rename outright.
    name = download_filename_from_suffix(
        f"ACME_{ORIGINAL_FILENAME_MARKER}", ".pdf", set(), original_filename="z" * 400 + ".pdf"
    )
    assert len(name.encode()) <= 255
    assert name.startswith("ACME_z") and name.endswith(".pdf")


def test_original_filename_marker_bounds_multibyte_stem_with_prefix_and_extension() -> None:
    name = download_filename_from_suffix(
        f"ACME_{ORIGINAL_FILENAME_MARKER}", ".pdf", set(), original_filename="界" * 150 + ".pdf"
    )
    assert len(name.encode("utf-8")) <= 255
    assert name.startswith("ACME_界") and name.endswith(".pdf")


def test_original_filename_marker_collisions_are_deduped() -> None:
    assert (
        download_filename_from_suffix(
            f"ACME_{ORIGINAL_FILENAME_MARKER}", ".pdf", {"ACME_report.pdf"}, original_filename="report.pdf"
        )
        == "ACME_report_1.pdf"
    )


def test_original_filename_marker_dedupes_after_filename_sanitization() -> None:
    assert (
        download_filename_from_suffix(
            f"ACME_{ORIGINAL_FILENAME_MARKER}", ".pdf", {"ACME_a#.pdf"}, original_filename="a.pdf"
        )
        == "ACME_a_1.pdf"
    )


def test_raw_jinja_original_filename_resolves_for_an_unrendered_suffix() -> None:
    # Cached-script mode bakes the author's template into generated code as an unrendered literal.
    assert (
        download_filename_from_suffix("ACME_{{ original_filename }}", ".pdf", set(), original_filename="report.pdf")
        == "ACME_report.pdf"
    )


def test_already_named_file_keeps_recovered_extension() -> None:
    from skyvern.webeye.cdp_download_interceptor import append_download_extension_if_missing

    assert append_download_extension_if_missing("ACME_export", ".xlsx") == "ACME_export.xlsx"
    assert append_download_extension_if_missing("ACME_export.xlsx", ".xlsx") == "ACME_export.xlsx"
    assert unique_download_filename("ACME_export", ".xlsx", set()) == "ACME_export.xlsx"
    assert unique_download_filename("ACME_export", ".xlsx", {"ACME_export.xlsx"}) == "ACME_export_1.xlsx"


def test_suffix_dedup_keeps_a_numeric_template_suffix() -> None:
    suffix = f"{ORIGINAL_FILENAME_MARKER}_2024"
    assert (
        download_filename_from_suffix(suffix, ".pdf", {"Q4 Report_2024.pdf"}, original_filename="Q4 Report.pdf")
        == "Q4 Report_2024_1.pdf"
    )


def test_recover_extension_ignores_dotted_template_prefix_before_original_filename(tmp_path: Path) -> None:
    with patch("skyvern.forge.sdk.api.files.guess_extension_from_file", return_value=".xlsx"):
        assert (
            recover_download_extension(tmp_path / "extensionless", f"Acme Capital L.P._{ORIGINAL_FILENAME_MARKER}")
            == ".xlsx"
        )


def test_download_target_path_prefixes_the_site_name(tmp_path: Path) -> None:
    context = SkyvernContext(download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}")
    with skyvern_context.scoped(context):
        target = _download_target_path(tmp_path, "Q4 Report.pdf")
    assert target.name == "ACME_Q4 Report.pdf"
    assert target.parent == tmp_path
    assert context.download_suffix_applied_files == {target.name: ("Q4 Report.pdf", f"ACME_{ORIGINAL_FILENAME_MARKER}")}


def test_download_target_path_does_not_infer_prior_naming_from_site_affixes(tmp_path: Path) -> None:
    context = SkyvernContext(download_suffix=f"ACME_{ORIGINAL_FILENAME_MARKER}")
    with skyvern_context.scoped(context):
        target = _download_target_path(tmp_path, "ACME_report.pdf")
    assert target.name == "ACME_ACME_report.pdf"
    assert context.download_suffix_applied_files == {
        target.name: ("ACME_report.pdf", f"ACME_{ORIGINAL_FILENAME_MARKER}")
    }
