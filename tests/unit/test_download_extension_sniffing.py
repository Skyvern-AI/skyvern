from skyvern.forge.sdk.api.files import guess_extension_from_file, recover_download_extension

_PDF_BYTES = b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n1 0 obj\n<</Title (statement)>>\nrest of the document"


def test_guess_extension_from_file_detects_pdf_without_extension(tmp_path) -> None:
    extensionless = tmp_path / "download-20260617114038544470-ABCD"
    extensionless.write_bytes(_PDF_BYTES)

    assert guess_extension_from_file(extensionless) == ".pdf"


def test_guess_extension_from_file_returns_empty_for_unknown_content(tmp_path) -> None:
    unknown = tmp_path / "mystery"
    unknown.write_bytes(b"this header matches nothing")

    assert guess_extension_from_file(unknown) == ""


def test_guess_extension_from_file_returns_empty_for_missing_file(tmp_path) -> None:
    assert guess_extension_from_file(tmp_path / "does-not-exist") == ""


def test_recover_download_extension_sniffs_when_no_download_suffix(tmp_path) -> None:
    downloaded = tmp_path / "download-xyz"
    downloaded.write_bytes(_PDF_BYTES)

    assert recover_download_extension(downloaded, download_suffix=None) == ".pdf"


def test_recover_download_extension_sniffs_when_download_suffix_lacks_extension(tmp_path) -> None:
    downloaded = tmp_path / "download-xyz"
    downloaded.write_bytes(_PDF_BYTES)

    assert recover_download_extension(downloaded, download_suffix="invoice") == ".pdf"


def test_recover_download_extension_skips_when_download_suffix_already_has_extension(tmp_path) -> None:
    # Guard against invoice.pdf + .pdf -> invoice.pdf.pdf
    downloaded = tmp_path / "download-xyz"
    downloaded.write_bytes(_PDF_BYTES)

    assert recover_download_extension(downloaded, download_suffix="invoice.pdf") == ""


def test_recover_download_extension_empty_for_unrecognized_content(tmp_path) -> None:
    downloaded = tmp_path / "download-xyz"
    downloaded.write_bytes(b"not a recognizable file")

    assert recover_download_extension(downloaded, download_suffix=None) == ""
