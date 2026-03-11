from unittest.mock import MagicMock, patch

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.services.script_service import (
    _append_to_loop_output,
    _filter_downloaded_files_for_current_iteration,
    _to_downloaded_file_signature,
)


def _file(url: str, filename: str, checksum: str) -> FileInfo:
    return FileInfo(url=url, filename=filename, checksum=checksum)


def test_filter_downloaded_files_ignores_files_seen_before_iteration() -> None:
    before_iteration = [["a.pdf", "abc", "https://files/a.pdf"]]
    downloaded_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
        _file("https://files/b.pdf?sig=new", "b.pdf", "def"),
    ]

    current_iteration_files = _filter_downloaded_files_for_current_iteration(
        downloaded_files,
        {"downloaded_file_signatures_before_iteration": before_iteration},
    )

    assert [f.filename for f in current_iteration_files] == ["b.pdf"]


def test_filter_downloaded_files_preserves_duplicate_downloads_in_same_iteration() -> None:
    before_iteration = [["a.pdf", "abc", "https://files/a.pdf"]]
    downloaded_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
        _file("https://files/a.pdf?sig=new", "a.pdf", "abc"),
    ]

    current_iteration_files = _filter_downloaded_files_for_current_iteration(
        downloaded_files,
        {"downloaded_file_signatures_before_iteration": before_iteration},
    )

    assert len(current_iteration_files) == 1
    assert current_iteration_files[0].filename == "a.pdf"


def test_filter_downloaded_files_returns_all_when_loop_metadata_is_none() -> None:
    downloaded_files = [
        _file("https://files/a.pdf", "a.pdf", "abc"),
        _file("https://files/b.pdf", "b.pdf", "def"),
    ]
    result = _filter_downloaded_files_for_current_iteration(downloaded_files, None)
    assert result == downloaded_files


def test_filter_downloaded_files_returns_all_when_key_missing() -> None:
    """loop_metadata={} with no signatures key should return all files."""
    downloaded_files = [_file("https://files/a.pdf", "a.pdf", "abc")]
    result = _filter_downloaded_files_for_current_iteration(downloaded_files, {})
    assert result == downloaded_files


def test_filter_downloaded_files_returns_all_when_before_iteration_empty() -> None:
    """Empty before_iteration list should return all files."""
    downloaded_files = [_file("https://files/a.pdf", "a.pdf", "abc")]
    result = _filter_downloaded_files_for_current_iteration(
        downloaded_files,
        {"downloaded_file_signatures_before_iteration": []},
    )
    assert result == downloaded_files


def test_filter_downloaded_files_handles_none_checksum_and_filename() -> None:
    """Files with None checksum or filename should still be handled."""
    before_iteration = [[None, None, "https://files/a.pdf"]]
    downloaded_files = [
        FileInfo(url="https://files/a.pdf", filename=None, checksum=None),
        FileInfo(url="https://files/b.pdf", filename=None, checksum=None),
    ]
    result = _filter_downloaded_files_for_current_iteration(
        downloaded_files,
        {"downloaded_file_signatures_before_iteration": before_iteration},
    )
    assert len(result) == 1
    assert result[0].url == "https://files/b.pdf"


def test_filter_downloaded_files_handles_urls_without_query_strings() -> None:
    """URLs without query parameters should work correctly."""
    before_iteration = [["a.pdf", "abc", "https://files/a.pdf"]]
    downloaded_files = [
        _file("https://files/a.pdf", "a.pdf", "abc"),
        _file("https://files/b.pdf", "b.pdf", "def"),
    ]
    result = _filter_downloaded_files_for_current_iteration(
        downloaded_files,
        {"downloaded_file_signatures_before_iteration": before_iteration},
    )
    assert len(result) == 1
    assert result[0].filename == "b.pdf"


# --- _append_to_loop_output tests ---


def test_append_to_loop_output_merges_downloaded_files() -> None:
    """When loop_value is a dict and output has downloaded_files, current_value should include them."""
    mock_context = MagicMock()
    mock_context.loop_output_values = []
    mock_context.loop_metadata = {
        "current_index": 0,
        "current_value": {"name": "test"},
        "current_item": {"name": "test"},
    }

    with patch("skyvern.services.script_service.skyvern_context.current", return_value=mock_context):
        _append_to_loop_output(
            {"downloaded_files": ["/path/to/file.pdf"], "extracted_information": {"key": "val"}},
            label="my_block",
        )

    assert len(mock_context.loop_output_values) == 1
    entry = mock_context.loop_output_values[0]
    assert entry["current_value"]["name"] == "test"
    assert entry["current_value"]["downloaded_files"] == ["/path/to/file.pdf"]
    # extracted_information should NOT be copied into current_value — it already
    # lives in output_value and duplicating it causes _collect_extracted_information
    # to count it twice.
    assert "extracted_information" not in entry["current_value"]
    assert entry["output_value"]["extracted_information"] == {"key": "val"}
    assert entry["loop_value"] == {"name": "test"}
    assert entry["label"] == "my_block"


def test_append_to_loop_output_non_dict_loop_value() -> None:
    """When loop_value is not a dict, current_value should equal loop_value."""
    mock_context = MagicMock()
    mock_context.loop_output_values = []
    mock_context.loop_metadata = {
        "current_index": 0,
        "current_value": "just_a_string",
        "current_item": "just_a_string",
    }

    with patch("skyvern.services.script_service.skyvern_context.current", return_value=mock_context):
        _append_to_loop_output({"some": "output"}, label="block_1")

    assert len(mock_context.loop_output_values) == 1
    entry = mock_context.loop_output_values[0]
    assert entry["current_value"] == "just_a_string"
    assert entry["loop_value"] == "just_a_string"


def test_append_to_loop_output_does_not_mutate_loop_value() -> None:
    """Modifying current_value after append must not affect the original loop_value."""
    original_value = {"name": "test", "nested": {"key": "original"}}
    mock_context = MagicMock()
    mock_context.loop_output_values = []
    mock_context.loop_metadata = {
        "current_index": 0,
        "current_value": original_value,
        "current_item": original_value,
    }

    with patch("skyvern.services.script_service.skyvern_context.current", return_value=mock_context):
        _append_to_loop_output({"downloaded_files": ["file.pdf"]}, label="block_1")

    entry = mock_context.loop_output_values[0]
    # Mutate the copied current_value
    entry["current_value"]["nested"]["key"] = "mutated"
    entry["current_value"]["new_key"] = "added"

    # Original should be unaffected
    assert original_value["nested"]["key"] == "original"
    assert "new_key" not in original_value
    assert "downloaded_files" not in original_value


def test_append_to_loop_output_noop_when_no_context() -> None:
    """Should return without error when context is None."""
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=None):
        _append_to_loop_output({"output": "data"})  # Should not raise


# --- _to_downloaded_file_signature tests ---


def test_to_downloaded_file_signature_strips_fragment_without_query() -> None:
    """URL with fragment but no query string should have fragment stripped in signature."""
    file_info = FileInfo(url="https://files/doc.pdf#section2", filename="doc.pdf", checksum="xyz")
    signature = _to_downloaded_file_signature(file_info)
    assert signature == ("doc.pdf", "xyz", "https://files/doc.pdf")
