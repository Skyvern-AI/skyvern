from skyvern.forge.sdk.db.utils import downloaded_file_count_from_output


def test_counts_downloaded_files() -> None:
    output = {"downloaded_files": [{"url": "a"}, {"url": "b"}, {"url": "c"}]}
    assert downloaded_file_count_from_output(output) == 3


def test_zero_when_no_files_downloaded() -> None:
    assert downloaded_file_count_from_output({"downloaded_files": []}) == 0


def test_none_when_key_absent() -> None:
    # Non-download blocks (e.g. an extraction block output) carry no downloaded_files key.
    assert downloaded_file_count_from_output({"extracted_information": {"k": "v"}}) is None


def test_none_when_downloaded_files_is_null() -> None:
    assert downloaded_file_count_from_output({"downloaded_files": None}) is None


def test_none_for_non_dict_output() -> None:
    assert downloaded_file_count_from_output(None) is None
    assert downloaded_file_count_from_output("a string value") is None
    assert downloaded_file_count_from_output(["a", "b"]) is None
