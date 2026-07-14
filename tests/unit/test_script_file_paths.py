from urllib.parse import quote

import pytest
from pydantic import ValidationError

from skyvern.schemas.scripts import FileEncoding, ScriptFileCreate
from skyvern.utils.script_file_paths import build_script_file_storage_uri, normalize_script_file_path


def _script_file(path: str) -> ScriptFileCreate:
    return ScriptFileCreate(path=path, content="ZA==", encoding=FileEncoding.BASE64)


@pytest.mark.parametrize(
    "file_path",
    [
        "",
        "../outside.py",
        "src/../outside.py",
        "/main.py",
        "\\main.py",
        "C:/main.py",
        "src//main.py",
        "src/./main.py",
        "src\\main.py",
        "main.py\x00.txt",
        "main.py%00.txt",
        "main.py%2500.txt",
        "%2e%2e/outside.py",
        "src/%2e%2e/outside.py",
        "%2Fmain.py",
        "src%5Cmain.py",
    ],
)
def test_script_file_create_rejects_unsafe_paths_at_input_time(file_path: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _script_file(file_path)

    assert "relative POSIX path" in str(exc.value)


def test_script_file_create_normalizes_encoded_storage_path() -> None:
    script_file = _script_file("src%2Fmain.py")

    assert script_file.path == "src/main.py"


def test_script_file_create_rejects_deeply_encoded_traversal_path() -> None:
    file_path = "%2e%2e/outside.py"
    for _ in range(6):
        file_path = quote(file_path, safe="")

    with pytest.raises(ValidationError) as exc:
        _script_file(file_path)

    assert "relative POSIX path" in str(exc.value)


def test_build_script_file_storage_uri_pins_normalized_path_under_base() -> None:
    uri = build_script_file_storage_uri(
        "file:///tmp/artifacts/local/org/",
        script_id="s_test",
        script_version=2,
        file_path="src%2Fmain.py",
    )

    assert uri == "file:///tmp/artifacts/local/org/scripts/s_test/2/src/main.py"


@pytest.mark.parametrize("file_path", ["../main.py", "%2e%2e/main.py", "main.py%00.txt", "C:/main.py"])
def test_build_script_file_storage_uri_rejects_unsafe_paths(file_path: str) -> None:
    with pytest.raises(ValueError):
        build_script_file_storage_uri(
            "s3://bucket/v1/local/org",
            script_id="s_test",
            script_version=2,
            file_path=file_path,
        )


def test_normalize_script_file_path_preserves_safe_posix_path() -> None:
    assert normalize_script_file_path("src/helpers/main.py") == "src/helpers/main.py"
