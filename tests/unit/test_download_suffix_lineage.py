"""SKY-12516 download_suffix lineage observability.

The naming freeze (all loop iterations' downloads named by iteration-0's account) is a post-render
runtime phenomenon: render is provably correct per iteration, yet the finalized artifact freezes.
These tests pin the diagnostic contract that lets the next production occurrence be attributed:

  (a) render         -> ``download_suffix_rendered``        (block label, current index, suffix fp)
  (c) finalize path  -> ``download_suffix_finalize_rename``  (task_block suffix vs context suffix vs desired)
  (c) at-save path   -> ``download_suffix_target_named``     (context suffix, context task id, desired)

plus a 4-case harness driving the REAL render/copy + REAL finalize + REAL contextvar naming to locate
exactly where an expected suffix A becomes a consumed suffix B. Account values here are synthetic.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter
from skyvern.webeye.actions.handler import _download_target_path
from tests.unit._fingerprint_expectations import (
    FINGERPRINT_TEST_SECRET_KEY,
    bare_sha256_fingerprint,
    expected_fingerprint,
)
from tests.unit.fake_workflow_run_context import FakeWorkflowRunContext

SUFFIX_TEMPLATE = "AllDataExport_UsageDetail_{{current_value.account_number}}"
# Synthetic, obviously-fake account numbers (never real customer values).
ACCOUNTS = ["ACCT_AAA_1001", "ACCT_BBB_2002", "ACCT_CCC_3003"]
SITE_FILENAME = "detail_report.csv"


@pytest.fixture(autouse=True)
def _keyed_fingerprint(fingerprint_secret_key: str) -> str:
    """Every test here asserts against the keyed fingerprint, so pin the test key module-wide."""
    return fingerprint_secret_key


def _rendered_suffix(account: str) -> str:
    return f"AllDataExport_UsageDetail_{account}"


def _make_output_param(label: str) -> OutputParameter:
    now = datetime.now(UTC)
    return OutputParameter(
        output_parameter_id=f"op_{label}",
        key=f"{label}_output",
        workflow_id="wf_test",
        created_at=now,
        modified_at=now,
    )


def _make_download_block() -> FileDownloadBlock:
    return FileDownloadBlock(
        label="bill_usage_download",
        url="https://billing.example.com/usage",
        navigation_goal="Select account {{current_value.account_number}} then export",
        download_suffix=SUFFIX_TEMPLATE,
        output_parameter=_make_output_param("bill_usage_download"),
    )


def _loop_context(block_label: str, index: int, account: str) -> FakeWorkflowRunContext:
    return FakeWorkflowRunContext(
        values={},
        block_metadata={
            block_label: {
                "current_index": index,
                "current_value": {"account_number": account},
                "current_item": {"account_number": account},
            }
        },
    )


def _events(cap: list[dict], name: str) -> list[dict]:
    return [e for e in cap if e.get("event") == name]


def _emit_artifact(case: str, records: list[dict]) -> None:
    """Print lineage as JSONL so a ``pytest -s`` runner can tee it to the Hermes artifact path."""
    for record in records:
        print(f"LINEAGE {case} {json.dumps(record, sort_keys=True, default=str)}")


def _make_task(task_id: str, workflow_run_id: str = "wr-harness") -> MagicMock:
    task = MagicMock()
    task.task_id = task_id
    task.organization_id = "org-harness"
    task.workflow_run_id = workflow_run_id
    task.browser_session_id = None
    return task


# --------------------------------------------------------------------------------------------------
# diagnostic_fingerprint keyed contract (must NOT be a bare/unsalted hash of low-entropy PII values)
# --------------------------------------------------------------------------------------------------


def test_diagnostic_fingerprint_none_and_empty() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    assert diagnostic_fingerprint(None) == "none"
    assert diagnostic_fingerprint("") == "empty:0"


def test_diagnostic_fingerprint_is_keyed_not_bare_sha256() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    value = _rendered_suffix(ACCOUNTS[0])
    fp = diagnostic_fingerprint(value)
    assert fp == expected_fingerprint(value)  # keyed HMAC of the value
    assert fp != bare_sha256_fingerprint(value)  # NOT the offline-brute-forceable unsalted sha256
    assert fp.endswith(f":{len(value)}")
    assert len(fp.split(":")[0]) == 12


def test_diagnostic_fingerprint_stable_for_same_key_and_value() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    value = _rendered_suffix(ACCOUNTS[1])
    assert diagnostic_fingerprint(value) == diagnostic_fingerprint(value)


def test_diagnostic_fingerprint_changes_with_key() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    value = _rendered_suffix(ACCOUNTS[0])
    with patch("skyvern.forge.sdk.core.hashing.settings.SECRET_KEY", "key-alpha"):
        fp_a = diagnostic_fingerprint(value)
    with patch("skyvern.forge.sdk.core.hashing.settings.SECRET_KEY", "key-beta"):
        fp_b = diagnostic_fingerprint(value)
    assert fp_a != fp_b  # keyed: a different secret yields a different tag
    assert fp_a == expected_fingerprint(value, key="key-alpha")
    assert fp_b == expected_fingerprint(value, key="key-beta")


def test_diagnostic_fingerprint_leaks_neither_value_nor_key() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    value = _rendered_suffix(ACCOUNTS[0])
    fp = diagnostic_fingerprint(value)
    assert value not in fp
    assert ACCOUNTS[0] not in fp
    assert FINGERPRINT_TEST_SECRET_KEY not in fp


def test_diagnostic_fingerprint_fails_closed_without_key() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    value = _rendered_suffix(ACCOUNTS[0])
    for missing_key in ("PLACEHOLDER", ""):
        with patch("skyvern.forge.sdk.core.hashing.settings.SECRET_KEY", missing_key):
            fp = diagnostic_fingerprint(value)
            assert fp == "unkeyed"  # fail closed: never emit a brute-forceable bare hash
            assert fp != bare_sha256_fingerprint(value)
            assert value not in fp


def test_diagnostic_fingerprint_handles_surrogate_filenames() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    # An invalid-UTF-8 on-disk filename surfaces via surrogateescape; the helper must not raise.
    surrogate = "invoice_\udc80.pdf"
    fp = diagnostic_fingerprint(surrogate)
    assert fp == expected_fingerprint(surrogate)
    assert fp.endswith(f":{len(surrogate)}")


def test_diagnostic_fingerprint_distinguishes_values() -> None:
    from skyvern.forge.sdk.core.hashing import diagnostic_fingerprint

    fps = {diagnostic_fingerprint(_rendered_suffix(a)) for a in ACCOUNTS}
    assert len(fps) == len(ACCOUNTS)


# --------------------------------------------------------------------------------------------------
# (a) render boundary
# --------------------------------------------------------------------------------------------------


def test_render_boundary_emits_lineage_per_iteration_and_leaves_template_untouched() -> None:
    block = _make_download_block()
    records: list[dict] = []
    for index, account in enumerate(ACCOUNTS):
        ctx = _loop_context(block.label, index, account)
        copy = block.model_copy(deep=True)
        with capture_logs() as cap:
            copy.format_potential_template_parameters(ctx)
        events = _events(cap, "download_suffix_rendered")
        assert len(events) == 1, f"expected one render lineage event for iteration {index}"
        event = events[0]
        assert event["block_label"] == "bill_usage_download"
        assert event["current_index"] == index
        assert event["download_suffix_fp"] == expected_fingerprint(_rendered_suffix(account))
        assert copy.download_suffix == _rendered_suffix(account)  # render is correct per iteration
        records.append(event)

    _emit_artifact("render", records)
    assert len({r["download_suffix_fp"] for r in records}) == len(ACCOUNTS)  # iterations distinguishable
    # The shared original template must never be mutated (freeze is not a render/mutate bug).
    assert block.download_suffix == SUFFIX_TEMPLATE


# --------------------------------------------------------------------------------------------------
# 4-case reproduction harness (real render/copy + real finalize + real contextvar naming)
# --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_case1_finalize_names_per_iteration_no_freeze(tmp_path: Path) -> None:
    """Deterministic path prod actually used: constant site filename, per-iteration task_block suffix.

    Finalize names each file by the ITERATION's own suffix -> three distinct account names, no freeze.
    """
    agent = ForgeAgent()
    block = _make_download_block()
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    records: list[dict] = []

    for index, account in enumerate(ACCOUNTS):
        ctx = _loop_context(block.label, index, account)
        copy = block.model_copy(deep=True)
        copy.format_potential_template_parameters(ctx)  # real per-iteration render
        before = sorted(str(p) for p in download_dir.iterdir())
        (download_dir / SITE_FILENAME).write_text(f"bytes-{account}")  # constant site name each iteration
        task = _make_task(task_id=f"task-{index}")
        with (
            patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=download_dir),
            patch("skyvern.forge.agent.skyvern_context.current", return_value=None),
            capture_logs() as cap,
        ):
            await agent._finalize_downloaded_files_for_task(
                task,
                organization_id=task.organization_id,
                download_suffix=copy.download_suffix,
                list_files_before=before,
                randomize_if_missing=False,
            )
        records.extend(_events(cap, "download_suffix_finalize_rename"))

    _emit_artifact("case1_finalize", records)
    names = {p.name for p in download_dir.iterdir()}
    assert names == {f"{_rendered_suffix(a)}.csv" for a in ACCOUNTS}, names  # 3 distinct -> NO freeze
    assert len({r["passed_download_suffix_fp"] for r in records}) == len(ACCOUNTS)


@pytest.mark.asyncio
async def test_harness_case2_frozen_contextvar_reproduces_freeze_on_atsave_path(tmp_path: Path) -> None:
    """At-save naming via a contextvar that was stamped at iteration 0 and never re-stamped.

    All three downloads collapse onto iteration-0's suffix (+ dedup) -> the observed freeze signature.
    """
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    frozen = SkyvernContext(task_id="task-0", download_suffix=_rendered_suffix(ACCOUNTS[0]))
    produced: list[str] = []
    records: list[dict] = []

    for account in ACCOUNTS:  # later iterations, but context never advanced
        with skyvern_context.scoped(frozen), capture_logs() as cap:
            target = _download_target_path(download_dir, SITE_FILENAME)
        target.write_text(f"bytes-{account}")
        produced.append(target.name)
        records.extend(_events(cap, "download_suffix_target_named"))

    _emit_artifact("case2_frozen_context", records)
    base = _rendered_suffix(ACCOUNTS[0])
    assert produced == [f"{base}.csv", f"{base}_1.csv", f"{base}_2.csv"], produced  # FREEZE reproduced
    assert len({r["context_download_suffix_fp"] for r in records}) == 1  # frozen suffix fp


@pytest.mark.asyncio
async def test_harness_case3_shared_page_callback_captures_stale_context(tmp_path: Path) -> None:
    """A download whose naming runs under a context captured at iteration 0 while the loop is on a later
    iteration. ``context_task_id`` in the lineage is the discriminator: it stays task-0 (stale)."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    captured_iter0_ctx = SkyvernContext(task_id="task-0", download_suffix=_rendered_suffix(ACCOUNTS[0]))
    records: list[dict] = []

    # Loop is on iteration 2 (task-2), but the late download names under the captured iteration-0 context.
    current_iteration_task_id = "task-2"
    with skyvern_context.scoped(captured_iter0_ctx), capture_logs() as cap:
        target = _download_target_path(download_dir, SITE_FILENAME)
    target.write_text("late-bytes")
    records.extend(_events(cap, "download_suffix_target_named"))

    _emit_artifact("case3_stale_capture", records)
    assert target.name == f"{_rendered_suffix(ACCOUNTS[0])}.csv"  # named by stale iteration-0 suffix
    assert len(records) == 1
    assert records[0]["context_task_id"] == "task-0"
    assert records[0]["context_task_id"] != current_iteration_task_id  # stale-context attribution caught


@pytest.mark.asyncio
async def test_harness_case4_finalize_taskblock_path_diverges_from_contextvar_path(tmp_path: Path) -> None:
    """Same downloaded file, two naming paths. finalize uses the PASSED task_block suffix (current
    iteration); the at-save path uses the contextvar (possibly stale). The lineage exposes the divergence,
    which is the field that will attribute the next production occurrence to one path or the other."""
    agent = ForgeAgent()
    current_account, stale_account = ACCOUNTS[2], ACCOUNTS[0]

    finalize_dir = tmp_path / "finalize"
    finalize_dir.mkdir()
    (finalize_dir / SITE_FILENAME).write_text("bytes")
    task = _make_task(task_id="task-2")
    with (
        patch("skyvern.forge.agent.get_path_for_workflow_download_directory", return_value=finalize_dir),
        patch(
            "skyvern.forge.agent.skyvern_context.current",
            return_value=SkyvernContext(task_id="task-0", download_suffix=_rendered_suffix(stale_account)),
        ),
        capture_logs() as cap_finalize,
    ):
        await agent._finalize_downloaded_files_for_task(
            task,
            organization_id=task.organization_id,
            download_suffix=_rendered_suffix(current_account),  # task_block-derived, current iteration
            list_files_before=[],
            randomize_if_missing=False,
        )
    finalize_events = _events(cap_finalize, "download_suffix_finalize_rename")

    atsave_dir = tmp_path / "atsave"
    atsave_dir.mkdir()
    with (
        skyvern_context.scoped(SkyvernContext(task_id="task-0", download_suffix=_rendered_suffix(stale_account))),
        capture_logs() as cap_atsave,
    ):
        atsave_target = _download_target_path(atsave_dir, SITE_FILENAME)
    atsave_events = _events(cap_atsave, "download_suffix_target_named")

    _emit_artifact("case4_finalize", finalize_events)
    _emit_artifact("case4_atsave", atsave_events)

    assert {p.name for p in finalize_dir.iterdir()} == {f"{_rendered_suffix(current_account)}.csv"}
    assert atsave_target.name == f"{_rendered_suffix(stale_account)}.csv"

    assert len(finalize_events) == 1
    finalize_event = finalize_events[0]
    # finalize path: names by task_block (current), and the lineage still records the divergent context.
    assert finalize_event["passed_download_suffix_fp"] == expected_fingerprint(_rendered_suffix(current_account))
    assert finalize_event["context_download_suffix_fp"] == expected_fingerprint(_rendered_suffix(stale_account))
    assert finalize_event["desired_name_fp"] == expected_fingerprint(f"{_rendered_suffix(current_account)}.csv")
    assert finalize_event["context_task_id"] == "task-0"
    assert finalize_event["finalize_task_id"] == "task-2"

    assert len(atsave_events) == 1
    assert atsave_events[0]["context_download_suffix_fp"] == expected_fingerprint(_rendered_suffix(stale_account))


@pytest.mark.asyncio
async def test_harness_case5_async_delayed_callback_reads_context_at_fire_time(tmp_path: Path) -> None:
    """A download callback registered during iteration 0 but firing (async) during iteration 1, against a
    single run-scoped context mutated in place (the re-stamp at agent.py: ``context.download_suffix = ...``).

    The late callback names under the context value AT FIRE TIME -> the LATEST suffix, not iteration 0's.
    That is the opposite of the observed earliest-freeze, so this rules out "naive in-place shared-context
    mutation + late callback" as the mechanism and points root cause at a captured iteration-0 snapshot or a
    frozen task_block. The lineage's ``context_task_id`` is what tells the two apart in production.
    """
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    shared = SkyvernContext(task_id="task-0", download_suffix=_rendered_suffix(ACCOUNTS[0]))
    produced: list[str] = []
    records: list[dict] = []
    gate = asyncio.Event()

    async def deferred_download() -> None:
        await gate.wait()  # the download event lands only after the loop advanced to iteration 1
        with skyvern_context.scoped(shared), capture_logs() as cap:
            target = _download_target_path(download_dir, SITE_FILENAME)
        target.write_text("late-bytes")
        produced.append(target.name)
        records.extend(_events(cap, "download_suffix_target_named"))

    pending = asyncio.create_task(deferred_download())
    # Loop advances to iteration 1 and re-stamps the SAME run-scoped context in place.
    shared.task_id = "task-1"
    shared.download_suffix = _rendered_suffix(ACCOUNTS[1])
    gate.set()
    await pending

    _emit_artifact("case5_async_delay", records)
    assert produced == [f"{_rendered_suffix(ACCOUNTS[1])}.csv"]  # latest-wins, NOT iteration-0 freeze
    assert len(records) == 1
    assert records[0]["context_task_id"] == "task-1"
    assert records[0]["context_download_suffix_fp"] == expected_fingerprint(_rendered_suffix(ACCOUNTS[1]))
