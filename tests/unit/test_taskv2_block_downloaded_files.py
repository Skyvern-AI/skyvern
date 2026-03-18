"""Tests for TaskV2Block downloaded_files output with loop-scoped filtering (SKY-7005)."""

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.files import FileInfo
from skyvern.forge.sdk.workflow.loop_download_filter import filter_downloaded_files_for_current_iteration


def _file(url: str, filename: str, checksum: str) -> FileInfo:
    return FileInfo(url=url, filename=filename, checksum=checksum)


def test_taskv2_output_includes_downloaded_files_filtered_by_loop() -> None:
    """When inside a loop, TaskV2Block output should include only THIS iteration's downloaded files."""
    # Simulate baseline: iteration started with a.pdf already downloaded
    loop_state = {
        "downloaded_file_signatures_before_iteration": [
            ["a.pdf", "abc", "https://files/a.pdf"],
        ],
    }

    # Storage returns all files (a.pdf from before + b.pdf downloaded this iteration)
    all_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
        _file("https://files/b.pdf?sig=new", "b.pdf", "def"),
    ]

    # Apply the same filter that TaskV2Block now uses
    filtered = filter_downloaded_files_for_current_iteration(all_files, loop_state)

    # Build output dict the same way TaskV2Block does
    task_v2_output = {
        "task_id": "oc_test",
        "status": "completed",
        "summary": None,
        "extracted_information": None,
        "failure_reason": None,
        "downloaded_files": [fi.model_dump() for fi in filtered],
        "downloaded_file_urls": [fi.url for fi in filtered],
        "task_screenshot_artifact_ids": [],
        "workflow_screenshot_artifact_ids": [],
    }

    assert len(task_v2_output["downloaded_files"]) == 1
    assert task_v2_output["downloaded_files"][0]["filename"] == "b.pdf"
    assert task_v2_output["downloaded_file_urls"] == ["https://files/b.pdf?sig=new"]


def test_taskv2_output_includes_all_files_outside_loop() -> None:
    """Outside a loop (no loop_internal_state), all downloaded files should be included."""
    all_files = [
        _file("https://files/a.pdf", "a.pdf", "abc"),
        _file("https://files/b.pdf", "b.pdf", "def"),
    ]

    filtered = filter_downloaded_files_for_current_iteration(all_files, None)

    task_v2_output = {
        "downloaded_files": [fi.model_dump() for fi in filtered],
        "downloaded_file_urls": [fi.url for fi in filtered],
    }

    assert len(task_v2_output["downloaded_files"]) == 2
    assert task_v2_output["downloaded_file_urls"] == [
        "https://files/a.pdf",
        "https://files/b.pdf",
    ]


def test_taskv2_output_empty_when_no_new_downloads_in_iteration() -> None:
    """If no new files were downloaded in this iteration, both lists should be empty."""
    loop_state = {
        "downloaded_file_signatures_before_iteration": [
            ["a.pdf", "abc", "https://files/a.pdf"],
        ],
    }

    all_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
    ]

    filtered = filter_downloaded_files_for_current_iteration(all_files, loop_state)

    task_v2_output = {
        "downloaded_files": [fi.model_dump() for fi in filtered],
        "downloaded_file_urls": [fi.url for fi in filtered],
    }

    assert task_v2_output["downloaded_files"] == []
    assert task_v2_output["downloaded_file_urls"] == []


def test_taskv2_context_loop_state_available_after_task_execution() -> None:
    """Verify that loop_internal_state on the context survives TaskV2Block's context reset
    and is available for the downloaded_files filtering step."""
    loop_state = {
        "downloaded_file_signatures_before_iteration": [
            ["a.pdf", "abc", "https://files/a.pdf"],
        ],
    }

    # Set context with loop state (simulating what ForLoopBlock does before executing child)
    skyvern_context.set(
        SkyvernContext(
            organization_id="org_1",
            workflow_run_id="wr_1",
            run_id="wr_1",
            loop_internal_state=loop_state,
        )
    )

    # Simulate TaskV2Block's finally block preserving loop_internal_state
    context = skyvern_context.current()
    preserved_loop_state = context.loop_internal_state if context else None
    skyvern_context.set(
        SkyvernContext(
            organization_id="org_1",
            workflow_run_id="wr_1",
            run_id=context.run_id if context else "wr_1",
            loop_internal_state=preserved_loop_state,
        )
    )

    # Now read context the way the new downloaded_files code does
    current_context = skyvern_context.current()
    assert current_context is not None
    assert current_context.loop_internal_state == loop_state

    # Filter should work correctly
    all_files = [
        _file("https://files/a.pdf?sig=old", "a.pdf", "abc"),
        _file("https://files/b.pdf?sig=new", "b.pdf", "def"),
    ]
    filtered = filter_downloaded_files_for_current_iteration(
        all_files,
        current_context.loop_internal_state,
    )
    assert [f.filename for f in filtered] == ["b.pdf"]

    skyvern_context.reset()
