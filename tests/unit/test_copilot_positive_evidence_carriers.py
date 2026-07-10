from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.completion_output_grounding import (
    _boundary_delimited_present,
    grade_requested_output_criteria,
    page_evidence_prose_text,
)
from skyvern.forge.sdk.copilot.completion_verification import (
    CompletionVerificationResult,
    CriterionVerdict,
    EvidenceSourceKind,
    RunEvidenceSnapshot,
    grade_fallback_floor_reached_end_state_criteria,
)
from skyvern.forge.sdk.copilot.request_policy import _classifier_fallback_policy, build_classifier_fallback_floor
from skyvern.forge.sdk.copilot.runtime import (
    PreRunPageReference,
    RegisteredArtifactEntry,
    RegisteredArtifactEvidence,
)
from skyvern.forge.sdk.copilot.tools import completion as completion_module
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from tests.unit.copilot_test_helpers import (
    DISPATCHED_NAV_ONLY_HTML,
    make_completion_criterion,
)
from tests.unit.copilot_test_helpers import make_stub_artifact as _artifact
from tests.unit.copilot_test_helpers import make_stub_html_artifact as _html_artifact
from tests.unit.copilot_test_helpers import stub_artifact_app as _stub_app

_POST_RUN_LABEL = "post_run_page_observation"
_ARTIFACT_LABEL = "registered_artifact_observation"
_FLOOR_ID = "__copilot_fallback_floor__run"


class _GroundingCtx:
    def __init__(self) -> None:
        self.code_artifact_metadata: dict[str, object] = {}
        self.workflow_verification_evidence: SimpleNamespace | None = None
        self.last_workflow_yaml: str | None = None
        self.workflow_yaml: str | None = None


def _page_snapshot(post_run_text: str, pre_run_text: str | None) -> RunEvidenceSnapshot:
    return RunEvidenceSnapshot(
        block_outputs={_POST_RUN_LABEL: {"visible_text_excerpt": post_run_text}},
        block_output_sources={_POST_RUN_LABEL: "independent_page_evidence"},
        pre_run_page_reference_text=pre_run_text,
    )


def _artifact_snapshot(parsed_text: str) -> RunEvidenceSnapshot:
    return RunEvidenceSnapshot(
        block_outputs={_ARTIFACT_LABEL: {"parsed_text": parsed_text, "file_names": ["invoice.pdf"]}},
        block_output_sources={_ARTIFACT_LABEL: "registered_artifact_content"},
    )


def _requested_criterion(value: object) -> object:
    return make_completion_criterion(
        "c_out",
        "the request returns a confirmation number",
        output_path="output.confirmation_number",
        expected_output_value=value,
    )


def _grade(criterion: object, snapshot: RunEvidenceSnapshot) -> CriterionVerdict:
    verdicts = grade_requested_output_criteria(_GroundingCtx(), [criterion], snapshot)
    assert len(verdicts) == 1
    return verdicts[0]


def test_page_carrier_confirms_post_run_present_pre_run_absent() -> None:
    snapshot = _page_snapshot("Your confirmation number is WTR-1842-DEMO. Thank you.", "Submit your request below.")
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), snapshot)
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "evidence_confirms"
    assert verdict.evidence_source == "independent_page_evidence"


def test_page_carrier_is_fully_satisfied_without_self_emission() -> None:
    snapshot = _page_snapshot("Confirmation WTR-1842-DEMO issued.", "Start a new request.")
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), snapshot)
    result = CompletionVerificationResult(status="evaluated", criterion_ids=["c_out"], verdicts=[verdict])
    assert result.is_fully_satisfied() is True


def test_page_carrier_pre_run_present_does_not_confirm() -> None:
    snapshot = _page_snapshot(
        "Your confirmation number is WTR-1842-DEMO.", "Prior page already showed WTR-1842-DEMO earlier."
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_page_carrier_missing_pre_run_pin_abstains() -> None:
    snapshot = _page_snapshot("Your confirmation number is WTR-1842-DEMO.", None)
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_artifact_carrier_confirms_without_absence_proof() -> None:
    snapshot = _artifact_snapshot("Invoice INV-4820-XZ total 512.00 USD paid.")
    verdict = _grade(_requested_criterion("INV-4820-XZ"), snapshot)
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "evidence_confirms"
    assert verdict.evidence_source == "registered_artifact_content"


def test_structured_contradiction_masks_confirmation() -> None:
    # Production packet shape: prose in visible_text_excerpt (no agent-only evidence_text), and a
    # structured value at the requested output_path that contradicts the expected scalar.
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            _POST_RUN_LABEL: {
                "visible_text_excerpt": "confirmation WTR-1842-DEMO shown",
                "output": {"confirmation_number": "OTHER-0000-XX"},
            }
        },
        block_output_sources={_POST_RUN_LABEL: "independent_page_evidence"},
        pre_run_page_reference_text="a blank request form",
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), snapshot)
    assert verdict.reason_code != "evidence_confirms"


@pytest.mark.xfail(reason="SKY-11868: free-text-only contradiction is unmaskable without a substring door", strict=True)
def test_free_text_contradiction_masks_confirmation() -> None:
    # The contradicting value lives only in prose, not at a resolvable structured path; the
    # snapshot-absence carrier cannot tell a confirming appearance from a contradicting one in
    # free text without reintroducing the withdrawn substring door.
    snapshot = _page_snapshot("Requested WTR-1842-DEMO but the confirmed number is OTHER-0000-XX.", "empty form")
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_boolean_expected_value_never_confirms() -> None:
    snapshot = _page_snapshot("The submission state is true now.", "empty form")
    verdict = _grade(_requested_criterion(True), snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_judgment_boolean_criterion_yields_no_carrier_confirmation() -> None:
    criterion = make_completion_criterion(
        "c_out",
        "the run judged the goal reached",
        output_path="output.goal_reached",
        expected_output_shape="goal_judgment_boolean",
    )
    snapshot = _page_snapshot("Goal reached and confirmed complete.", "empty form")
    verdict = _grade(criterion, snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_substring_inside_word_does_not_confirm() -> None:
    snapshot = _page_snapshot("please DEMONSTRATE the workflow now", "empty form")
    verdict = _grade(_requested_criterion("DEMO"), snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_sub_four_char_value_excluded() -> None:
    snapshot = _page_snapshot("Selected state: CA. Continue.", "no state selected")
    verdict = _grade(_requested_criterion("CA"), snapshot)
    assert verdict.reason_code != "evidence_confirms"


def test_punctuation_adjacent_value_confirms() -> None:
    snapshot = _page_snapshot("Reference code: [REF-5521]! saved.", "empty form")
    verdict = _grade(_requested_criterion("REF-5521"), snapshot)
    assert verdict.state == "satisfied"
    assert verdict.evidence_source == "independent_page_evidence"


def test_no_authored_contract_confirms_with_carrier_and_stays_unproducible_without() -> None:
    criterion = _requested_criterion("WTR-1842-DEMO")
    with_carrier = _grade(criterion, _page_snapshot("Confirmation WTR-1842-DEMO issued.", "empty form"))
    assert with_carrier.reason_code == "evidence_confirms"

    without_carrier = _grade(criterion, RunEvidenceSnapshot())
    assert without_carrier.reason_code == "unproducible"


def test_boundary_predicate_rejects_alnum_neighbors() -> None:
    assert _boundary_delimited_present("demo", "the demo runs") is True
    assert _boundary_delimited_present("demo", "demonstrate") is False
    assert _boundary_delimited_present("1842", "order #1842.") is True
    assert _boundary_delimited_present("1842", "18429") is False


def test_page_evidence_prose_text_skips_stamp_keys_and_booleans() -> None:
    text = page_evidence_prose_text(
        {
            "workflow_run_id": "wr_secret",
            "observed_after_workflow_run": True,
            "screenshot_used": True,
            "visible_text_excerpt": "confirmation WTR-1842-DEMO",
        }
    )
    assert "wr_secret" not in text
    assert "True" not in text
    assert "WTR-1842-DEMO" in text


def _carrier_verdict(source: str) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id="c_out",
        state="satisfied",
        reason_code="evidence_confirms",
        evidence_ref=f"block_outputs:{_POST_RUN_LABEL}",
        evidence_source=source,  # type: ignore[arg-type]
    )


def test_fallback_floor_threads_carrier_verdict() -> None:
    floor = build_classifier_fallback_floor([])
    snapshot = RunEvidenceSnapshot(block_outputs={"other": {"note": "no terminal record here"}})
    verdicts = grade_fallback_floor_reached_end_state_criteria(
        floor, snapshot, carrier_verdicts=(_carrier_verdict("independent_page_evidence"),)
    )
    assert [v.criterion_id for v in verdicts] == [_FLOOR_ID]
    assert verdicts[0].state == "satisfied"


def test_fallback_floor_without_carrier_verdict_stays_empty() -> None:
    floor = build_classifier_fallback_floor([])
    snapshot = RunEvidenceSnapshot(block_outputs={"other": {"note": "no terminal record here"}})
    assert grade_fallback_floor_reached_end_state_criteria(floor, snapshot) == []


def test_fallback_floor_ignores_non_independent_carrier_source() -> None:
    floor = build_classifier_fallback_floor([])
    snapshot = RunEvidenceSnapshot(block_outputs={"other": {"note": "no terminal record here"}})
    assert (
        grade_fallback_floor_reached_end_state_criteria(
            floor, snapshot, carrier_verdicts=(_carrier_verdict("runtime_output"),)
        )
        == []
    )


def test_fallback_floor_carrier_masked_by_poisoned_record() -> None:
    floor = build_classifier_fallback_floor([])
    snapshot = RunEvidenceSnapshot(block_outputs={"submit": {"error": "submission failed with a blocking challenge"}})
    assert (
        grade_fallback_floor_reached_end_state_criteria(
            floor, snapshot, carrier_verdicts=(_carrier_verdict("independent_page_evidence"),)
        )
        == []
    )


def test_carrier_floor_verdicts_filter() -> None:
    verdicts = [
        _carrier_verdict("independent_page_evidence"),
        _carrier_verdict("runtime_output"),
        CriterionVerdict(criterion_id="x", state="unsatisfied", reason_code="unproducible"),
    ]
    filtered = completion_module._carrier_floor_verdicts(verdicts)
    assert len(filtered) == 1
    assert filtered[0].evidence_source == "independent_page_evidence"


def test_floor_call_site_parity_across_seams() -> None:
    floor = build_classifier_fallback_floor([])
    snapshot = RunEvidenceSnapshot(block_outputs={"other": {"note": "no terminal record"}})
    carrier = (_carrier_verdict("independent_page_evidence"),)

    main = grade_fallback_floor_reached_end_state_criteria(floor, snapshot, carrier_verdicts=carrier)
    assert main and main[0].satisfied

    deterministic, _ = completion_module._deterministic_run_verification_result(
        floor, snapshot, carrier_verdicts=carrier
    )
    assert deterministic is not None
    assert any(v.criterion_id == _FLOOR_ID and v.satisfied for v in deterministic.verdicts)

    seeded = CompletionVerificationResult(
        status="evaluated",
        criterion_ids=[_FLOOR_ID],
        verdicts=[CriterionVerdict(criterion_id=_FLOOR_ID, state="unsatisfied", reason_code="no_evidence")],
    )
    upgraded = completion_module._apply_present_value_upgrades(
        seeded, floor, snapshot, include_terminal_goal_records=True, carrier_verdicts=carrier
    )
    assert any(v.criterion_id == _FLOOR_ID and v.satisfied for v in upgraded.verdicts)


@pytest.mark.parametrize(
    "message",
    [
        "Read the receipt and return the confirmation number, which is CONF-7712045.",
        "Extract the confirmation number CONF-7712045 from the page and return it as a record.",
        "Capture the order status and confirmation number CONF-7712045 for the entity, grouped per location, with status.",
    ],
)
def test_classifier_fallback_mints_no_typed_requested_output_value(message: str) -> None:
    policy = _classifier_fallback_policy([], raw_secret_present=False, failure_kind="timeout", user_message=message)
    assert policy.classifier_status == "fallback"
    requested = [c for c in policy.completion_criteria if c.id.startswith("__copilot_requested_output__")]
    assert requested
    for criterion in requested:
        assert criterion.expected_output_value is None
        assert criterion.requested_output_evidence_source == "runtime_output"


def test_registered_artifact_bind_requires_stamp_match() -> None:
    evidence = RegisteredArtifactEvidence(
        entries=(RegisteredArtifactEntry(artifact_id="art_1", file_name="a.txt", parsed_text="INV-4820-XZ"),),
        workflow_run_id="wr_match",
    )
    matched_outputs: dict[str, object] = {}
    matched_sources: dict[str, str] = {}
    completion_module._bind_registered_artifact_evidence(evidence, "wr_match", matched_outputs, matched_sources)  # type: ignore[arg-type]
    assert matched_sources.get(_ARTIFACT_LABEL) == "registered_artifact_content"

    stale_outputs: dict[str, object] = {}
    stale_sources: dict[str, str] = {}
    completion_module._bind_registered_artifact_evidence(evidence, "wr_other", stale_outputs, stale_sources)  # type: ignore[arg-type]
    assert _ARTIFACT_LABEL not in stale_outputs


def test_registered_artifact_bind_does_not_overwrite_existing_label() -> None:
    evidence = RegisteredArtifactEvidence(
        entries=(RegisteredArtifactEntry(artifact_id="art_1", file_name="a.txt", parsed_text="INV-4820-XZ"),),
        workflow_run_id="wr_match",
    )
    outputs: dict[str, object] = {_ARTIFACT_LABEL: {"parsed_text": "runtime"}}
    sources: dict[str, str] = {_ARTIFACT_LABEL: "runtime_output"}
    completion_module._bind_registered_artifact_evidence(evidence, "wr_match", outputs, sources)  # type: ignore[arg-type]
    assert sources[_ARTIFACT_LABEL] == "runtime_output"


def test_pre_run_reference_text_stamp_gated() -> None:
    reference = PreRunPageReference(text="prior page text", workflow_run_id="wr_match")
    assert completion_module._pre_run_page_reference_text(reference, "wr_match") == "prior page text"
    assert completion_module._pre_run_page_reference_text(reference, "wr_other") is None


def test_parse_registered_artifact_text_txt_decode() -> None:
    parsed = run_execution_module._parse_registered_artifact_text("notes.txt", b"INV-4820-XZ total")
    assert parsed == "INV-4820-XZ total"


def test_parse_registered_artifact_text_unsupported_extension() -> None:
    assert run_execution_module._parse_registered_artifact_text("image.png", b"\x89PNG") is None


def test_parse_registered_artifact_text_pdf_cleans_temp_file(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_extract(path: str, *, file_identifier: str) -> str:
        captured["path"] = path
        assert os.path.exists(path)
        return "extracted INV-4820-XZ"

    monkeypatch.setattr(run_execution_module, "extract_pdf_file", fake_extract)
    parsed = run_execution_module._parse_registered_artifact_text("statement.pdf", b"%PDF-1.4 bytes")
    assert parsed == "extracted INV-4820-XZ"
    assert not os.path.exists(captured["path"])


def test_parse_registered_artifact_text_pdf_cleans_temp_file_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_extract(path: str, *, file_identifier: str) -> str:
        captured["path"] = path
        raise ValueError("bad pdf")

    monkeypatch.setattr(run_execution_module, "extract_pdf_file", fake_extract)
    assert run_execution_module._parse_registered_artifact_text("statement.pdf", b"%PDF") is None
    assert not os.path.exists(captured["path"])


@pytest.mark.asyncio
async def test_artifact_producer_binds_parsed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_artifact("art_1", "receipt.txt", 20)]
    _stub_app(monkeypatch, artifacts, {"art_1": b"INV-4820-XZ paid"})
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(ctx, run_id="wr_1", organization_id="o_1")
    assert ctx.registered_artifact_evidence is not None
    assert ctx.registered_artifact_evidence.workflow_run_id == "wr_1"
    assert ctx.registered_artifact_evidence.entries[0].parsed_text == "INV-4820-XZ paid"


@pytest.mark.asyncio
async def test_artifact_producer_skips_oversize_before_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    oversize = run_execution_module._MAX_REGISTERED_ARTIFACT_BYTES + 1
    artifacts = [_artifact("art_big", "big.csv", oversize)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_big": b"x"})
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(ctx, run_id="wr_1", organization_id="o_1")
    assert retrieved_ids == []
    assert ctx.registered_artifact_evidence is None


@pytest.mark.asyncio
async def test_artifact_producer_caps_artifact_count(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_artifact(f"art_{i}", f"f{i}.txt", 10) for i in range(5)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {f"art_{i}": b"INV-4820-XZ" for i in range(5)})
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(ctx, run_id="wr_1", organization_id="o_1")
    assert len(retrieved_ids) == run_execution_module._MAX_REGISTERED_ARTIFACTS


@pytest.mark.asyncio
async def test_artifact_producer_skips_unsupported_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_artifact("art_png", "screenshot.png", 10)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_png": b"binary"})
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(ctx, run_id="wr_1", organization_id="o_1")
    assert retrieved_ids == []
    assert ctx.registered_artifact_evidence is None


def test_collect_downloaded_artifact_ids_dedupes_across_blocks() -> None:
    block_outputs = {
        "download": {"downloaded_file_artifact_ids": ["art_1", "art_2"]},
        "report": {"downloaded_file_artifact_ids": ["art_2", "art_3"]},
        "noise": {"value": "no ids"},
        "bad": {"downloaded_file_artifact_ids": "not-a-list"},
    }
    assert run_execution_module._collect_downloaded_artifact_ids(block_outputs) == ["art_1", "art_2", "art_3"]


@pytest.mark.asyncio
async def test_artifact_producer_binds_from_downloaded_artifact_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    graded = [_artifact("art_graded", "receipt.txt", 30)]
    _stub_app(monkeypatch, artifacts=[], retrieved={"art_graded": b"CONF-7712045 paid"}, by_ids=graded)
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(
        ctx, run_id="wr_graded", organization_id="o_1", downloaded_artifact_ids=["art_graded"]
    )
    assert ctx.registered_artifact_evidence is not None
    assert ctx.registered_artifact_evidence.workflow_run_id == "wr_graded"
    assert ctx.registered_artifact_evidence.entries[0].parsed_text == "CONF-7712045 paid"


@pytest.mark.asyncio
async def test_artifact_producer_binds_graded_run_across_repair_iteration_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graded = [_artifact("art_iter2", "receipt.txt", 30)]
    stale_run_scan = [_artifact("art_iter1", "receipt.txt", 30)]
    _stub_app(
        monkeypatch,
        artifacts=stale_run_scan,
        retrieved={"art_iter2": b"CONF-7712045 paid"},
        by_ids=graded,
    )
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(
        ctx, run_id="wr_iter2", organization_id="o_1", downloaded_artifact_ids=["art_iter2"]
    )
    assert ctx.registered_artifact_evidence is not None
    assert ctx.registered_artifact_evidence.workflow_run_id == "wr_iter2"
    assert ctx.registered_artifact_evidence.entries[0].artifact_id == "art_iter2"


@pytest.mark.asyncio
async def test_artifact_producer_id_path_filters_non_download_type(monkeypatch: pytest.MonkeyPatch) -> None:
    non_download = [_artifact("art_screenshot", "page.txt", 30, artifact_type=ArtifactType.SCREENSHOT_LLM)]
    retrieved_ids = _stub_app(
        monkeypatch, artifacts=[], retrieved={"art_screenshot": b"CONF-7712045"}, by_ids=non_download
    )
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(
        ctx, run_id="wr_1", organization_id="o_1", downloaded_artifact_ids=["art_screenshot"]
    )
    assert retrieved_ids == []
    assert ctx.registered_artifact_evidence is None


@pytest.mark.asyncio
async def test_artifact_producer_id_path_skips_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    oversize = run_execution_module._MAX_REGISTERED_ARTIFACT_BYTES + 1
    graded = [_artifact("art_big", "big.csv", oversize)]
    retrieved_ids = _stub_app(monkeypatch, artifacts=[], retrieved={"art_big": b"x"}, by_ids=graded)
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(
        ctx, run_id="wr_1", organization_id="o_1", downloaded_artifact_ids=["art_big"]
    )
    assert retrieved_ids == []
    assert ctx.registered_artifact_evidence is None


@pytest.mark.asyncio
async def test_artifact_producer_falls_back_to_run_scan_without_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_artifact("art_1", "receipt.txt", 20)]
    _stub_app(monkeypatch, artifacts, {"art_1": b"INV-4820-XZ paid"}, by_ids=[])
    ctx = SimpleNamespace(registered_artifact_evidence=None)
    await run_execution_module._capture_registered_artifact_evidence(ctx, run_id="wr_1", organization_id="o_1")
    assert ctx.registered_artifact_evidence is not None
    assert ctx.registered_artifact_evidence.entries[0].parsed_text == "INV-4820-XZ paid"


_HTML_WITH_VALUE = (
    "<html><head><title>Done</title></head><body><main><h1>Request complete</h1>"
    "<p>Your confirmation number is WTR-1842-DEMO. Thank you.</p></main></body></html>"
)
_HTML_NO_VALUE = (
    "<html><body><main><h1>Submit your request</h1><p>Fill the form below to begin.</p></main></body></html>"
)
_HTML_SCRAPE_PREACTION = "<html><body><main><p>SCRAPEONLYTOKEN loading form</p></main></body></html>"


def _producer_ctx(pre_run_prose: str | None = "Submit your request below.") -> SimpleNamespace:
    baseline = {"visible_text_excerpt": pre_run_prose} if pre_run_prose is not None else None
    return SimpleNamespace(
        composition_page_evidence=baseline,
        pre_run_page_reference=None,
        workflow_verification_evidence=SimpleNamespace(),
    )


def _snapshot_from_ctx(ctx: SimpleNamespace, run_id: str) -> RunEvidenceSnapshot:
    block_outputs: dict[str, object] = {}
    block_output_sources: dict[str, EvidenceSourceKind] = {}
    completion_module._bind_independent_post_run_page_evidence(ctx, run_id, block_outputs, block_output_sources)
    return RunEvidenceSnapshot(
        block_outputs=block_outputs,
        block_output_sources=block_output_sources,
        pre_run_page_reference_text=completion_module._pre_run_page_reference_text(ctx.pre_run_page_reference, run_id),
    )


def test_pre_run_baseline_provenance_valid_for_scout_evidence() -> None:
    assert run_execution_module._pre_run_baseline_is_provenance_valid({"visible_text_excerpt": "a form"}) is True


def test_pre_run_baseline_provenance_rejects_post_run_stamp() -> None:
    stale = {"visible_text_excerpt": "a page", "observed_after_workflow_run": True}
    assert run_execution_module._pre_run_baseline_is_provenance_valid(stale) is False


def test_pre_run_baseline_provenance_rejects_foreign_run_id() -> None:
    stale = {"visible_text_excerpt": "a page", "workflow_run_id": "wr_prior"}
    assert run_execution_module._pre_run_baseline_is_provenance_valid(stale) is False


def test_pre_run_baseline_provenance_rejects_non_mapping() -> None:
    assert run_execution_module._pre_run_baseline_is_provenance_valid(None) is False


@pytest.mark.asyncio
async def test_dispatched_fetch_returns_none_without_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_app(monkeypatch, artifacts=[], retrieved={})
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatched_fetch_skips_oversize_before_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    oversize = run_execution_module._MAX_REGISTERED_ARTIFACT_BYTES + 1
    artifacts = [_html_artifact("art_big", ArtifactType.HTML_ACTION, file_size=oversize)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_big": _HTML_WITH_VALUE.encode()})
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert result is None
    assert retrieved_ids == []


@pytest.mark.asyncio
async def test_dispatched_fetch_rejects_bundled_zip_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_zip", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_zip": b"PK\x03\x04 whole zip archive bytes"})
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatched_fetch_rejects_empty_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_empty", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_empty": b""})
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatched_fetch_parses_terminal_html(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert result is not None
    assert "WTR-1842-DEMO" in page_evidence_prose_text(result)


@pytest.mark.asyncio
async def test_dispatched_producer_confirms_value_only_post_run(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert ctx.composition_page_evidence["observed_after_workflow_run"] is True
    assert ctx.composition_page_evidence["workflow_run_id"] == "wr_disp"
    assert ctx.pre_run_page_reference is not None
    assert ctx.pre_run_page_reference.workflow_run_id == "wr_disp"
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "evidence_confirms"
    assert verdict.evidence_source == "independent_page_evidence"


def test_select_terminal_prefers_html_action_over_later_scrape() -> None:
    early = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc)
    artifacts = [
        _html_artifact("art_action", ArtifactType.HTML_ACTION, created_at=early),
        _html_artifact("art_scrape", ArtifactType.HTML_SCRAPE, created_at=late),
    ]
    selected = run_execution_module._select_terminal_page_artifact(artifacts)
    assert selected is not None
    assert selected.artifact_id == "art_action"


def test_select_terminal_tiebreak_by_artifact_id_on_equal_created_at() -> None:
    tie = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    artifacts = [
        _html_artifact("art_action_z", ArtifactType.HTML_ACTION, created_at=tie),
        _html_artifact("art_action_a", ArtifactType.HTML_ACTION, created_at=tie),
    ]
    selected = run_execution_module._select_terminal_page_artifact(artifacts)
    assert selected is not None
    assert selected.artifact_id == "art_action_z"


@pytest.mark.asyncio
async def test_dispatched_producer_selects_terminal_html_action(monkeypatch: pytest.MonkeyPatch) -> None:
    tie = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    later = datetime(2026, 7, 9, 11, 0, tzinfo=timezone.utc)
    artifacts = [
        _html_artifact("art_action_a", ArtifactType.HTML_ACTION, created_at=tie),
        _html_artifact("art_action_z", ArtifactType.HTML_ACTION, created_at=tie),
        _html_artifact("art_scrape", ArtifactType.HTML_SCRAPE, created_at=later),
    ]
    _stub_app(
        monkeypatch,
        artifacts,
        {
            "art_action_a": _HTML_NO_VALUE.encode(),
            "art_action_z": _HTML_WITH_VALUE.encode(),
            "art_scrape": _HTML_SCRAPE_PREACTION.encode(),
        },
    )
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    prose = page_evidence_prose_text(ctx.composition_page_evidence)
    assert "WTR-1842-DEMO" in prose
    assert "SCRAPEONLYTOKEN" not in prose


@pytest.mark.asyncio
async def test_dispatched_producer_confirms_from_html_scrape_when_only_family(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_scrape", ArtifactType.HTML_SCRAPE)]
    _stub_app(monkeypatch, artifacts, {"art_scrape": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.evidence_source == "independent_page_evidence"


@pytest.mark.asyncio
async def test_dispatched_producer_negative_control_value_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_NO_VALUE.encode()})
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.reason_code != "evidence_confirms"


@pytest.mark.asyncio
async def test_dispatched_producer_value_in_baseline_does_not_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx(pre_run_prose="Prior page already showed WTR-1842-DEMO earlier.")
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.reason_code != "evidence_confirms"


@pytest.mark.asyncio
async def test_dispatched_producer_stale_baseline_not_pinned_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx(pre_run_prose=None)
    ctx.composition_page_evidence = {
        "visible_text_excerpt": "stale page from a prior turn",
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_prior",
    }
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert ctx.pre_run_page_reference is None
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.reason_code != "evidence_confirms"


@pytest.mark.asyncio
async def test_dispatched_producer_abstains_without_terminal_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_app(monkeypatch, artifacts=[], retrieved={})
    ctx = _producer_ctx(pre_run_prose=None)
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert ctx.composition_page_evidence is None


_HTML_FORM_AND_RESULTS = (
    "<html><head><title>Find a provider</title></head><body><main>"
    '<form id="finder" action="/find" method="get">'
    '<label for="zip">ZIP code</label>'
    '<input id="zip" name="zip" type="text" required />'
    '<input id="account-password" name="password" type="password" />'
    '<button type="submit">Search</button>'
    "</form>"
    '<table id="provider-results"><tbody>'
    "<tr><td>Example Fiber</td><td>up to 500 Mbps</td></tr>"
    "<tr><td>Example Cable</td><td>up to 300 Mbps</td></tr>"
    "</tbody></table></main></body></html>"
)
_HTML_DISABLED_SUBMIT = (
    "<html><body><main>"
    '<form id="apply" action="/apply" method="post">'
    '<input id="account-email" name="email" type="email" />'
    '<input id="account-password" name="password" type="password" />'
    '<button type="submit" disabled>Submit</button>'
    "</form></main></body></html>"
)


async def _dispatched_packet(monkeypatch: pytest.MonkeyPatch, html: str) -> dict[str, object]:
    _stub_app(monkeypatch, [_html_artifact("art_page", ArtifactType.HTML_ACTION)], {"art_page": html.encode()})
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", organization_id="o_1", current_url=""
    )
    assert ctx.composition_page_evidence is not None
    return ctx.composition_page_evidence


@pytest.mark.asyncio
async def test_dispatched_packet_carries_forms_and_result_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = await _dispatched_packet(monkeypatch, _HTML_FORM_AND_RESULTS)
    forms = packet["forms"]
    assert forms
    assert any(field.get("type") == "password" for field in forms[0]["fields"])
    assert any(control.get("type") == "submit" for control in forms[0]["submit_controls"])
    containers = packet["result_containers"]
    assert containers
    assert any(container.get("selector") for container in containers)


@pytest.mark.asyncio
async def test_dispatched_packet_drops_navigation_targets_without_current_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dispatched captures carry no current URL, so the same-origin link filter drops every target.
    packet = await _dispatched_packet(monkeypatch, DISPATCHED_NAV_ONLY_HTML)
    assert packet["navigation_targets"] == []
    assert packet["forms"] == []
    assert packet["result_containers"] == []


@pytest.mark.asyncio
async def test_dispatched_packet_carries_static_disabled_submit_control(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = await _dispatched_packet(monkeypatch, _HTML_DISABLED_SUBMIT)
    controls = packet["forms"][0]["submit_controls"]
    assert controls
    assert controls[0]["disabled"] is True
    # Without challenge indicators the static parse never claims challenge gating; the
    # literal disabled attribute above is the only gating signal a dispatched packet carries.
    challenge_state = packet["challenge_state"]
    assert challenge_state["gates_submit_controls"] is False
    assert challenge_state["gated_submit_controls"] == []
