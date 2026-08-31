from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.services.script_service import (
    _append_to_loop_output,
    _filter_downloaded_files_for_current_iteration,
    _to_downloaded_file_signature,
    load_scripts,
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
#
# Pin the legacy webhook contract: cached for-loop output is
# List[List[{loop_value, output_parameter, output_value}]] — list-of-lists,
# per-item keyed by output_parameter (key ends in _output), matching
# ForLoopBlock.execute's outputs_with_loop_values shape.


def _mock_context(loop_output_values: list[list[dict]] | None, loop_value: object = "item") -> MagicMock:
    ctx = MagicMock()
    ctx.loop_output_values = loop_output_values
    ctx.loop_metadata = {
        "current_index": 0,
        "current_value": loop_value,
        "current_item": loop_value,
    }
    ctx.workflow_id = "wf_test"
    return ctx


def test_append_to_loop_output_emits_legacy_per_item_shape() -> None:
    ctx = _mock_context([[]], loop_value={"name": "test"})
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx):
        _append_to_loop_output({"extracted_information": {"key": "val"}}, label="my_block")

    assert len(ctx.loop_output_values) == 1
    iteration = ctx.loop_output_values[0]
    assert len(iteration) == 1
    entry = iteration[0]
    # Per-item legacy shape: loop_value + output_parameter + output_value, nothing else.
    assert set(entry.keys()) == {"loop_value", "output_parameter", "output_value"}
    assert entry["loop_value"] == {"name": "test"}
    assert entry["output_value"] == {"extracted_information": {"key": "val"}}
    # output_parameter.key must carry the legacy _output suffix.
    assert entry["output_parameter"].key == "my_block_output"
    assert entry["output_parameter"].workflow_id == "wf_test"


def test_append_to_loop_output_groups_items_per_iteration() -> None:
    """Items from the same iteration land in the same sub-list; new sub-list = new iteration."""
    ctx = _mock_context([[]])
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx):
        _append_to_loop_output({"k": 1}, label="navigate")
        _append_to_loop_output({"k": 2}, label="extract")

        # Simulate the loop generator opening a new iteration.
        ctx.loop_output_values.append([])
        _append_to_loop_output({"k": 3}, label="navigate")

    assert len(ctx.loop_output_values) == 2
    assert len(ctx.loop_output_values[0]) == 2
    assert len(ctx.loop_output_values[1]) == 1
    assert [e["output_parameter"].key for e in ctx.loop_output_values[0]] == [
        "navigate_output",
        "extract_output",
    ]
    assert ctx.loop_output_values[1][0]["output_parameter"].key == "navigate_output"


def test_append_to_loop_output_initializes_sublist_when_empty() -> None:
    """Defensive fallback: if the generator never appended a sub-list, one gets created."""
    ctx = _mock_context([])
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx):
        _append_to_loop_output({"k": "v"}, label="block_1")

    assert len(ctx.loop_output_values) == 1
    assert len(ctx.loop_output_values[0]) == 1


def test_append_to_loop_output_non_dict_loop_value() -> None:
    ctx = _mock_context([[]], loop_value="just_a_string")
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx):
        _append_to_loop_output({"some": "output"}, label="block_1")

    entry = ctx.loop_output_values[0][0]
    assert entry["loop_value"] == "just_a_string"


def test_append_to_loop_output_noop_when_no_context() -> None:
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=None):
        _append_to_loop_output({"output": "data"})  # Should not raise


def test_append_to_loop_output_noop_when_label_missing() -> None:
    """No label means no output_parameter.key to route on; entry is dropped and the drop is logged (not silent)."""
    ctx = _mock_context([[]])
    with (
        patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx),
        patch("skyvern.services.script_service.LOG") as mock_log,
    ):
        _append_to_loop_output({"output": "data"}, label=None)
        _append_to_loop_output({"output": "data2"})  # default label=None
        _append_to_loop_output({"output": "data3"}, label="")

    assert ctx.loop_output_values == [[]]
    # All three drops should have logged — empty-string callers don't get silently dropped.
    assert mock_log.warning.call_count == 3


def test_append_to_loop_output_preserves_passed_output_parameter() -> None:
    """When the caller hands us the real OutputParameter, we preserve its ID (don't synthesize a fresh UUID)."""
    real_op = OutputParameter(
        output_parameter_id="op_real_123",
        key="my_block_output",
        workflow_id="wf_test",
        created_at=datetime(2026, 1, 1),
        modified_at=datetime(2026, 1, 1),
        parameter_type=ParameterType.OUTPUT,
    )
    ctx = _mock_context([[]])
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx):
        _append_to_loop_output({"k": "v"}, label="my_block", output_parameter=real_op)

    entry = ctx.loop_output_values[0][0]
    assert entry["output_parameter"] is real_op
    assert entry["output_parameter"].output_parameter_id == "op_real_123"


def test_append_to_loop_output_synthesizes_when_output_parameter_missing() -> None:
    """Fallback path: no OutputParameter passed -> synthesize one from the label."""
    ctx = _mock_context([[]])
    with patch("skyvern.services.script_service.skyvern_context.current", return_value=ctx):
        _append_to_loop_output({"k": "v"}, label="my_block")

    entry = ctx.loop_output_values[0][0]
    assert entry["output_parameter"].key == "my_block_output"
    # Synthesized fallback gets a fresh UUID, never the empty string.
    assert entry["output_parameter"].output_parameter_id
    assert entry["output_parameter"].output_parameter_id != "op_real_123"


# --- _to_downloaded_file_signature tests ---


def test_to_downloaded_file_signature_strips_fragment_without_query() -> None:
    """URL with fragment but no query string should have fragment stripped in signature."""
    file_info = FileInfo(url="https://files/doc.pdf#section2", filename="doc.pdf", checksum="xyz")
    signature = _to_downloaded_file_signature(file_info)
    assert signature == ("doc.pdf", "xyz", "https://files/doc.pdf")


@pytest.mark.asyncio
async def test_load_scripts_omits_a_path_it_could_not_write(tmp_path: Path) -> None:
    """The caller gates on this set, so empty content must not be reported as written -- otherwise a
    stale copy from an earlier run gets loaded as if it were this revision's code."""
    script = MagicMock(script_id="s_1", organization_id="o_1")
    script_file = MagicMock(artifact_id="a_1", file_path="main.py", mime_type="text/x-python")
    stale = tmp_path / "s_1" / "main.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("print('code the retention sweep already deleted')")

    with (
        patch("skyvern.services.script_service.settings.TEMP_PATH", str(tmp_path)),
        patch("skyvern.services.script_service.app") as mock_app,
    ):
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=MagicMock())
        mock_app.ARTIFACT_MANAGER.retrieve_artifact = AsyncMock(return_value=b"")
        written = await load_scripts(script, [script_file])

    assert str(stale) not in written


@pytest.mark.asyncio
async def test_load_scripts_leaves_an_existing_local_copy_alone_when_the_fetch_returns_nothing(
    tmp_path: Path,
) -> None:
    """retrieve_artifact returns None for an exhausted-retry S3 failure as well as for a confirmed
    deletion -- download_file returns None on both paths -- so this layer cannot tell them apart.
    Gating on the written set rather than deleting means a transient failure costs this run its
    cached code without destroying the copy for the next one."""
    script = MagicMock(script_id="s_1", organization_id="o_1")
    script_file = MagicMock(artifact_id="a_1", file_path="main.py", mime_type="text/x-python")
    cached = tmp_path / "s_1" / "main.py"
    cached.parent.mkdir(parents=True)
    cached.write_text("print('a perfectly good cached script')")

    with (
        patch("skyvern.services.script_service.settings.TEMP_PATH", str(tmp_path)),
        patch("skyvern.services.script_service.app") as mock_app,
    ):
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=MagicMock())
        mock_app.ARTIFACT_MANAGER.retrieve_artifact = AsyncMock(return_value=None)
        written = await load_scripts(script, [script_file])

    assert cached.exists(), "a transient fetch failure must not destroy a working local cache"
    assert str(cached) not in written


@pytest.mark.asyncio
async def test_load_scripts_reports_a_path_it_did_write(tmp_path: Path) -> None:
    script = MagicMock(script_id="s_2", organization_id="o_1")
    script_file = MagicMock(artifact_id="a_1", file_path="main.py", mime_type="text/x-python")

    with (
        patch("skyvern.services.script_service.settings.TEMP_PATH", str(tmp_path)),
        patch("skyvern.services.script_service.app") as mock_app,
    ):
        mock_app.DATABASE.artifacts.get_artifact_by_id = AsyncMock(return_value=MagicMock())
        mock_app.ARTIFACT_MANAGER.retrieve_artifact = AsyncMock(return_value=b"print('fresh')")
        written = await load_scripts(script, [script_file])

    target = tmp_path / "s_2" / "main.py"
    assert str(target) in written and target.read_text() == "print('fresh')"
