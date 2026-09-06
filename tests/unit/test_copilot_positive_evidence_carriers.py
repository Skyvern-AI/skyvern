from __future__ import annotations

import asyncio
import os
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from skyvern.forge.sdk.copilot.request_policy import (
    _classifier_fallback_policy,
    build_classifier_fallback_floor,
)
from skyvern.forge.sdk.copilot.runtime import (
    OriginRunRedactionRegistry,
    PreRunPageReference,
    RegisteredArtifactEntry,
    RegisteredArtifactEvidence,
)
from skyvern.forge.sdk.copilot.tools import completion as completion_module
from skyvern.forge.sdk.copilot.tools import run_execution as run_execution_module
from skyvern.forge.sdk.copilot.tools.credentials import (
    _extract_credential_ids_from_workflow_definition,
)
from skyvern.forge.sdk.schemas.credentials import CredentialVaultType, PasswordCredential
from skyvern.forge.sdk.workflow import runtime_secret_bridge
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext
from skyvern.forge.sdk.workflow.models.block import _register_code_block_secret
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
        browser_session_id=None,
        codeblock_redaction_parameters={},
        origin_run_redaction_registry=None,
        scouted_credential_field_inventory_by_credential_id={},
        last_workflow=SimpleNamespace(workflow_definition=SimpleNamespace(parameters=[])),
        last_run_blocks_workflow_run_id=None,
        dispatched_run_ids_this_turn=set(),
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


@pytest.mark.asyncio
async def test_track_a_excludes_nonsensitive_parameters_from_origin_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _producer_ctx()

    def serialize(values: dict[str, object]) -> dict[str, object]:
        return dict(values)

    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(AGENT_FUNCTION=SimpleNamespace(serialize_codeblock_parameters=serialize)),
    )

    registry = await run_execution_module._bind_origin_run_redaction_registry(
        ctx,
        workflow_run_id="wr_origin",
        parameter_values={"account": "private-value"},
        credential_ids=[],
        sensitive_parameter_keys=[],
    )

    assert registry == OriginRunRedactionRegistry(
        "wr_origin",
        {},
        contains_sensitive_values=False,
        contains_all_sensitive_values=True,
        artifact_parameters={"account": "private-value"},
    )
    assert ctx.origin_run_redaction_registry is registry
    assert ctx.codeblock_redaction_parameters == {}


@pytest.mark.asyncio
async def test_nonsensitive_origin_parameters_still_scrub_dispatched_terminal_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameter_value = "ordinary-run-parameter"
    html = f"<html><body><p>{parameter_value}</p><p>safe evidence</p></body></html>"
    artifacts = [_html_artifact("art_parameter", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_parameter": html.encode()})
    seen_parameters: list[object] = []

    def redact(value: object, parameters: object) -> object:
        seen_parameters.append(parameters)
        return value.replace(parameter_value, "[REDACTED]") if isinstance(value, str) else value

    run_execution_module.app.AGENT_FUNCTION = SimpleNamespace(redact_codeblock_parameter_values=redact)
    ctx = _producer_ctx()

    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_origin",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_origin",
            {},
            contains_sensitive_values=False,
            contains_all_sensitive_values=True,
            artifact_parameters={"account": parameter_value},
        ),
    )

    assert result is not None
    assert seen_parameters == [{"account": parameter_value}]
    assert parameter_value not in str(result)
    assert "safe evidence" in page_evidence_prose_text(result)


def test_origin_run_redaction_registry_defensively_copies_and_exposes_parameters_immutably() -> None:
    mutable = {"password": "origin-secret", "credential": {"fields": ["username", "password"]}}
    registry = OriginRunRedactionRegistry(
        "wr_origin", mutable, contains_sensitive_values=True, contains_all_sensitive_values=True
    )

    mutable["password"] = "replacement-secret"
    assert registry.parameters["password"] == "origin-secret"
    with pytest.raises(TypeError):
        registry.parameters["password"] = "replacement-secret"  # type: ignore[index]
    with pytest.raises(TypeError):
        registry.parameters["credential"]["fields"] = ()  # type: ignore[index]
    with pytest.raises(AttributeError):
        registry.parameters["credential"]["fields"].append("otp")  # type: ignore[union-attr]
    with pytest.raises(FrozenInstanceError):
        registry.parameters = {}  # type: ignore[misc]


@pytest.mark.asyncio
async def test_track_a_registry_refuses_credential_run_even_when_scout_values_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _producer_ctx()
    ctx.organization_id = "o_1"
    ctx.secret_scrub_values = ["scouted-value"]
    ctx.scouted_credential_field_inventory_by_credential_id = {"cred_origin": frozenset({"password"})}
    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(serialize_codeblock_parameters=dict),
            DATABASE=SimpleNamespace(credentials=SimpleNamespace(get_credential=AsyncMock(return_value=None))),
        ),
    )

    registry = await run_execution_module._bind_origin_run_redaction_registry(
        ctx,
        workflow_run_id="wr_origin",
        parameter_values={"login_credential": "cred_origin"},
        credential_ids=["cred_origin"],
        sensitive_parameter_keys=[],
    )

    assert registry.workflow_run_id == "wr_origin"
    assert registry.contains_all_sensitive_values is False
    assert "scouted-value" not in registry.parameters.values()
    artifacts = [_html_artifact("art_sensitive", ArtifactType.HTML_ACTION)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_sensitive": b"scouted-value"})
    monkeypatch.setattr(run_execution_module, "_workflow_requires_terminal_artifact_redaction", lambda _: True)

    evidence = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_origin",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=registry,
    )

    assert evidence is None
    assert retrieved_ids == []


@pytest.mark.asyncio
async def test_track_a_registry_admits_complete_run_bound_static_credential_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _producer_ctx()
    ctx.organization_id = "o_1"
    ctx.secret_scrub_values = []
    db_credential = SimpleNamespace(vault_type=None, totp_identifier=None)
    credential = PasswordCredential(username="private-user", password="private-pass")
    credential_item = SimpleNamespace(credential=credential)
    service = SimpleNamespace(get_credential_item=AsyncMock(return_value=credential_item))
    process = AsyncMock(return_value=credential_item)
    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                serialize_codeblock_parameters=lambda values: values,
                process_registered_credential_item=process,
            ),
            DATABASE=SimpleNamespace(credentials=SimpleNamespace(get_credential=AsyncMock(return_value=db_credential))),
            CREDENTIAL_VAULT_SERVICES={CredentialVaultType.BITWARDEN: service},
        ),
    )

    registry = await run_execution_module._bind_origin_run_redaction_registry(
        ctx,
        workflow_run_id="wr_origin",
        parameter_values={"login_credential": "cred_origin"},
        credential_ids=["cred_origin"],
        sensitive_parameter_keys=[],
    )

    assert registry.contains_all_sensitive_values is True
    assert "cred_origin" not in registry.parameters.values()
    assert "cred_origin" not in ctx.secret_scrub_values
    assert registry.parameters["copilot_run_credential_0"]["username"] == "private-user"
    assert registry.parameters["copilot_run_credential_0"]["password"] == "private-pass"
    assert "private-user" in ctx.secret_scrub_values
    assert "private-pass" in ctx.secret_scrub_values
    process.assert_awaited_once_with(
        workflow_run_id="wr_origin",
        db_credential=db_credential,
        credential_item=credential_item,
    )


@pytest.mark.asyncio
async def test_track_a_registry_keeps_static_credential_values_when_totp_is_dynamic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _producer_ctx()
    ctx.organization_id = "o_1"
    ctx.secret_scrub_values = []
    db_credential = SimpleNamespace(vault_type=None, totp_identifier="totp_ref")
    credential = PasswordCredential(username="private-user", password="private-pass", totp_identifier="totp_ref")
    credential_item = SimpleNamespace(credential=credential)
    service = SimpleNamespace(get_credential_item=AsyncMock(return_value=credential_item))
    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(
            AGENT_FUNCTION=SimpleNamespace(
                serialize_codeblock_parameters=lambda values: values,
                process_registered_credential_item=AsyncMock(return_value=credential_item),
            ),
            DATABASE=SimpleNamespace(credentials=SimpleNamespace(get_credential=AsyncMock(return_value=db_credential))),
            CREDENTIAL_VAULT_SERVICES={CredentialVaultType.BITWARDEN: service},
        ),
    )

    registry = await run_execution_module._bind_origin_run_redaction_registry(
        ctx,
        workflow_run_id="wr_origin",
        parameter_values={"login_credential": "cred_origin"},
        credential_ids=["cred_origin"],
        sensitive_parameter_keys=[],
    )

    assert registry.contains_all_sensitive_values is False
    assert registry.contains_all_static_sensitive_values is True
    assert registry.awaiting_runtime_secret_values is True
    assert registry.parameters["copilot_run_credential_0"]["username"] == "private-user"
    assert registry.parameters["copilot_run_credential_0"]["password"] == "private-pass"
    assert "totp_identifier" not in registry.parameters["copilot_run_credential_0"]
    assert "totp_ref" not in ctx.secret_scrub_values


@pytest.mark.asyncio
async def test_track_a_registry_imports_dispatched_runtime_otp_without_local_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_otp = "654321"
    ctx = _producer_ctx()
    ctx.organization_id = "o_1"
    ctx.secret_scrub_values = []
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_origin",
        {"credential": {"username": "private-user", "password": "private-pass"}},
        contains_sensitive_values=True,
        contains_all_sensitive_values=False,
        contains_all_static_sensitive_values=True,
        awaiting_runtime_secret_values=True,
        artifact_parameters={"account": "ordinary-run-parameter"},
    )
    consume = AsyncMock(return_value={runtime_otp})
    monkeypatch.setattr(run_execution_module, "consume_copilot_runtime_secret_values", consume)

    registry = await run_execution_module._complete_origin_run_redaction_registry_from_runtime(ctx, "wr_origin")

    assert registry is not None
    assert registry.contains_all_sensitive_values is True
    assert registry.awaiting_runtime_secret_values is False
    assert registry.parameters["copilot_run_runtime_secret_values"] == (runtime_otp,)
    assert registry.artifact_parameters["account"] == "ordinary-run-parameter"
    assert registry.artifact_parameters["copilot_run_runtime_secret_values"] == (runtime_otp,)
    assert runtime_otp in ctx.secret_scrub_values
    consume.assert_awaited_once_with(organization_id=ctx.organization_id, workflow_run_id="wr_origin")


@pytest.mark.asyncio
async def test_runtime_bridge_completion_excludes_totp_routing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, str] = {}

    class FakeLocalCache:
        is_shared = False

        def get_lock(self, *_args: object, **_kwargs: object) -> asyncio.Lock:
            return asyncio.Lock()

        async def set(self, key: str, value: str, ex: int) -> None:
            values[key] = value

        async def get(self, key: str) -> str | None:
            return values.get(key)

    monkeypatch.setattr(runtime_secret_bridge, "app", SimpleNamespace(CACHE=FakeLocalCache()))
    monkeypatch.setattr(
        run_execution_module,
        "consume_copilot_runtime_secret_values",
        runtime_secret_bridge.consume_copilot_runtime_secret_values,
    )
    runtime_otp = "654321"
    ctx = _producer_ctx()
    ctx.organization_id = "o_1"
    ctx.secret_scrub_values = ["private-user", "private-pass"]
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_origin",
        {"credential": {"username": "private-user", "password": "private-pass"}},
        contains_sensitive_values=True,
        contains_all_sensitive_values=False,
        contains_all_static_sensitive_values=True,
        awaiting_runtime_secret_values=True,
    )
    workflow_run_context = SimpleNamespace(
        secrets={
            "username": "private-user",
            "password": "private-pass",
            "totp_type": "authenticator",
            "totp_identifier": "totp",
            "runtime": runtime_otp,
        },
        runtime_otp_values={runtime_otp},
    )

    assert await runtime_secret_bridge.publish_copilot_runtime_secret_values(
        organization_id=ctx.organization_id,
        workflow_run_id="wr_origin",
        workflow_run_context=workflow_run_context,
    )
    registry = await run_execution_module._complete_origin_run_redaction_registry_from_runtime(ctx, "wr_origin")

    assert registry is not None
    assert registry.parameters["credential"] == {"username": "private-user", "password": "private-pass"}
    assert registry.parameters["copilot_run_runtime_secret_values"] == (runtime_otp,)
    assert {"private-user", "private-pass", runtime_otp}.issubset(ctx.secret_scrub_values)
    assert "totp" not in ctx.secret_scrub_values
    assert "authenticator" not in ctx.secret_scrub_values


@pytest.mark.asyncio
async def test_runtime_secret_bridge_round_trips_exact_values_through_local_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, str] = {}

    class FakeLocalCache:
        is_shared = False

        def get_lock(self, *_args: object, **_kwargs: object) -> asyncio.Lock:
            return asyncio.Lock()

        async def set(self, key: str, value: str, ex: int) -> None:
            values[key] = value

        async def get(self, key: str) -> str | None:
            return values.get(key)

    monkeypatch.setattr(runtime_secret_bridge, "app", SimpleNamespace(CACHE=FakeLocalCache()))
    workflow_run_context = SimpleNamespace(
        secrets={"password": "private-pass", "totp_identifier": "totp", "runtime": "654321"},
        runtime_otp_values={"654321"},
    )

    published = await runtime_secret_bridge.publish_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
        workflow_run_context=workflow_run_context,
    )
    consumed = await runtime_secret_bridge.consume_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
    )

    assert published is True
    assert consumed == {"654321"}


@pytest.mark.asyncio
async def test_runtime_secret_bridge_carries_a_code_block_minted_otp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-time code a code block mints or polls never passes through the OTP resolver, so the
    bridge only carries it if the code block registers it as a runtime OTP rather than a bare secret."""
    values: dict[str, str] = {}

    class FakeLocalCache:
        is_shared = False

        def get_lock(self, *_args: object, **_kwargs: object) -> asyncio.Lock:
            return asyncio.Lock()

        async def set(self, key: str, value: str, ex: int) -> None:
            values[key] = value

        async def get(self, key: str) -> str | None:
            return values.get(key)

    monkeypatch.setattr(runtime_secret_bridge, "app", SimpleNamespace(CACHE=FakeLocalCache()))
    workflow_run_context = WorkflowRunContext("title", "wid", "wpid", "wr_origin", None)
    workflow_run_context.secrets["totp_identifier"] = "totp"
    _register_code_block_secret(workflow_run_context, "654321")

    published = await runtime_secret_bridge.publish_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
        workflow_run_context=workflow_run_context,
    )
    consumed = await runtime_secret_bridge.consume_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
    )

    assert published is True
    assert consumed == {"654321"}
    assert "654321" in workflow_run_context.secrets.values()


@pytest.mark.asyncio
async def test_runtime_secret_bridge_encrypts_shared_cache_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    values: dict[str, str] = {}
    plaintext: str | None = None

    class FakeSharedCache:
        is_shared = True

        def get_lock(self, *_args: object, **_kwargs: object) -> asyncio.Lock:
            return asyncio.Lock()

        async def set(self, key: str, value: str, ex: int) -> None:
            values[key] = value

        async def get(self, key: str) -> str | None:
            return values.get(key)

    async def fake_encrypt(value: str, _method: object) -> str:
        nonlocal plaintext
        plaintext = value
        return "opaque-ciphertext"

    async def fake_decrypt(_value: str, _method: object) -> str:
        assert plaintext is not None
        return plaintext

    monkeypatch.setattr(runtime_secret_bridge, "app", SimpleNamespace(CACHE=FakeSharedCache()))
    monkeypatch.setattr(
        runtime_secret_bridge,
        "encryptor",
        SimpleNamespace(encrypt=fake_encrypt, decrypt=fake_decrypt),
    )
    workflow_run_context = SimpleNamespace(
        secrets={"password": "private-pass", "runtime": "654321"},
        runtime_otp_values={"654321"},
    )

    assert await runtime_secret_bridge.publish_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
        workflow_run_context=workflow_run_context,
    )
    assert all("private-pass" not in stored and "654321" not in stored for stored in values.values())
    assert await runtime_secret_bridge.consume_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
    ) == {"654321"}


@pytest.mark.asyncio
async def test_runtime_secret_bridge_allows_exactly_one_concurrent_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[str, str] = {}

    class FakeLocalCache:
        is_shared = False

        def get_lock(self, *_args: object, **_kwargs: object) -> asyncio.Lock:
            return asyncio.Lock()

        async def set(self, key: str, value: str, ex: int) -> None:
            values[key] = value

        async def get(self, key: str) -> str | None:
            return values.get(key)

    monkeypatch.setattr(runtime_secret_bridge, "app", SimpleNamespace(CACHE=FakeLocalCache()))
    workflow_run_context = SimpleNamespace(
        secrets={"runtime": "654321"},
        runtime_otp_values={"654321"},
    )
    assert await runtime_secret_bridge.publish_copilot_runtime_secret_values(
        organization_id="o_1",
        workflow_run_id="wr_origin",
        workflow_run_context=workflow_run_context,
    )

    consumed = await asyncio.gather(
        runtime_secret_bridge.consume_copilot_runtime_secret_values(organization_id="o_1", workflow_run_id="wr_origin"),
        runtime_secret_bridge.consume_copilot_runtime_secret_values(organization_id="o_1", workflow_run_id="wr_origin"),
    )

    assert consumed.count({"654321"}) == 1
    assert consumed.count(None) == 1


@pytest.mark.asyncio
async def test_origin_registry_recovers_when_a_later_terminal_read_finds_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _producer_ctx()
    ctx.organization_id = "o_1"
    ctx.workflow_permanent_id = "wp_origin"
    ctx.secret_scrub_values = []
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_origin",
        {"credential": {"password": "private-pass"}},
        contains_sensitive_values=True,
        contains_all_sensitive_values=False,
        contains_all_static_sensitive_values=True,
        awaiting_runtime_secret_values=True,
    )
    consume = AsyncMock(side_effect=[None, {"654321"}])
    monkeypatch.setattr(run_execution_module, "consume_copilot_runtime_secret_values", consume)

    run = SimpleNamespace(
        status="completed",
        workflow_permanent_id=ctx.workflow_permanent_id,
        workflow_id="wf_origin",
        failure_reason=None,
        browser_session_id=None,
    )
    workflow = SimpleNamespace(workflow_definition=SimpleNamespace(parameters=[]))
    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(
            DATABASE=SimpleNamespace(
                workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=run)),
                workflows=SimpleNamespace(get_workflow_for_workflow_run=AsyncMock(return_value=workflow)),
                observer=SimpleNamespace(get_workflow_run_blocks=AsyncMock(return_value=[])),
            ),
            AGENT_FUNCTION=SimpleNamespace(
                should_dispatch_copilot_block_run_to_worker=AsyncMock(return_value=False),
            ),
        ),
    )
    monkeypatch.setattr(run_execution_module, "_attach_action_traces", AsyncMock())
    monkeypatch.setattr(run_execution_module, "_attach_registered_output_parameter_values", AsyncMock())
    monkeypatch.setattr(run_execution_module, "_fetch_dispatched_terminal_page_evidence", AsyncMock(return_value=None))

    first = await run_execution_module._complete_origin_run_redaction_registry_from_runtime(ctx, "wr_origin")
    result = await run_execution_module._get_run_results(
        {"workflow_run_id": "wr_origin"},
        ctx,
        read_live_page=False,
    )
    second = ctx.origin_run_redaction_registry

    assert first is not None and first.contains_all_sensitive_values is False
    assert result["ok"] is True
    assert second is not None and second.contains_all_sensitive_values is True
    assert second.parameters["copilot_run_runtime_secret_values"] == ("654321",)
    assert consume.await_count == 2


@pytest.mark.asyncio
async def test_sensitive_terminal_artifact_rejected_when_noncredential_secret_value_is_unregistered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _producer_ctx()
    registry = await run_execution_module._bind_origin_run_redaction_registry(
        ctx,
        workflow_run_id="wr_origin",
        parameter_values={"ordinary": "visible"},
        credential_ids=[],
        sensitive_parameter_keys=["aws_secret"],
    )
    artifacts = [_html_artifact("art_secret", ArtifactType.HTML_ACTION)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_secret": b"unregistered-origin-secret"})
    monkeypatch.setattr(run_execution_module, "_workflow_requires_terminal_artifact_redaction", lambda _: True)

    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_origin",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=registry,
    )

    assert registry.contains_all_sensitive_values is False
    assert result is None
    assert retrieved_ids == []


@pytest.mark.asyncio
async def test_origin_registry_is_incomplete_when_serializer_omits_resolved_sensitive_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _producer_ctx()
    monkeypatch.setattr(
        run_execution_module,
        "app",
        SimpleNamespace(AGENT_FUNCTION=SimpleNamespace(serialize_codeblock_parameters=lambda _values: {})),
    )

    registry = await run_execution_module._bind_origin_run_redaction_registry(
        ctx,
        workflow_run_id="wr_origin",
        parameter_values={"aws_secret": "resolved-secret"},
        credential_ids=[],
        sensitive_parameter_keys=["aws_secret"],
    )

    assert registry.parameters == {}
    assert registry.contains_all_sensitive_values is False


def test_origin_registry_candidate_scan_includes_fallback_credentials() -> None:
    credential_ids = _extract_credential_ids_from_workflow_definition(
        {
            "parameters": [
                {
                    "parameter_type": "credential",
                    "credential_id": "cred_primary",
                    "credential_ids": ["cred_rotating"],
                    "fallback_credential_ids": ["cred_fallback"],
                }
            ],
            "blocks": [],
        }
    )

    assert credential_ids == ["cred_primary", "cred_rotating", "cred_fallback"]


def test_independent_post_run_snapshot_removes_singular_selector_recommendation() -> None:
    ctx = _producer_ctx()
    ctx.composition_page_evidence = {
        "observed_after_workflow_run": True,
        "workflow_run_id": "wr_current",
        "source_tool": "inspect_page_for_composition",
        "clickable_controls": [
            {
                "text": "Star",
                "selector": "button.auth-state",
                "selector_match_count": 1,
                "selector_candidates": [
                    {"selector": "button.auth-state", "source": "class", "match_count": 1},
                    {"selector": 'button[data-action="star"]', "source": "data_action", "match_count": 1},
                ],
            }
        ],
    }

    snapshot = _snapshot_from_ctx(ctx, "wr_current")
    control = snapshot.block_outputs["post_run_page_observation"]["clickable_controls"][0]

    assert "selector" not in control
    assert "selector_match_count" not in control
    assert control["selector_candidates"] == [
        {"selector": "button.auth-state", "source": "class"},
        {"selector": 'button[data-action="star"]', "source": "data_action"},
    ]


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
    ctx = _producer_ctx()
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_disp",
            ctx.codeblock_redaction_parameters,
            contains_sensitive_values=False,
            contains_all_sensitive_values=True,
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatched_fetch_skips_oversize_before_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    oversize = run_execution_module._MAX_REGISTERED_ARTIFACT_BYTES + 1
    artifacts = [_html_artifact("art_big", ArtifactType.HTML_ACTION, file_size=oversize)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_big": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx()
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_disp",
            ctx.codeblock_redaction_parameters,
            contains_sensitive_values=False,
            contains_all_sensitive_values=True,
        ),
    )
    assert result is None
    assert retrieved_ids == []


@pytest.mark.asyncio
async def test_dispatched_fetch_rejects_bundled_zip_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_zip", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_zip": b"PK\x03\x04 whole zip archive bytes"})
    ctx = _producer_ctx()
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_disp", ctx.codeblock_redaction_parameters, False, True
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatched_fetch_rejects_empty_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_empty", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_empty": b""})
    ctx = _producer_ctx()
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_disp", ctx.codeblock_redaction_parameters, False, True
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_dispatched_fetch_parses_terminal_html(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx()
    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_disp", ctx.codeblock_redaction_parameters, False, True
        ),
    )
    assert result is not None
    assert "WTR-1842-DEMO" in page_evidence_prose_text(result)


@pytest.mark.asyncio
async def test_dispatched_fetch_scrubs_persisted_terminal_html_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential-secret-from-persisted-html"
    html = f'<html><body><input value="{secret}"><p>{secret}</p></body></html>'
    artifacts = [_html_artifact("art_secret", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_secret": html.encode()})
    seen: list[object] = []

    def scrub(value: object, redaction_parameters: object) -> object:
        seen.append(value)
        assert redaction_parameters == {"password": secret}
        return value.replace(secret, "[REDACTED]") if isinstance(value, str) else value

    ctx = _producer_ctx()
    ctx.codeblock_redaction_parameters = {"password": secret}
    run_execution_module.app.AGENT_FUNCTION = SimpleNamespace(redact_codeblock_parameter_values=scrub)
    monkeypatch.setattr(run_execution_module, "_workflow_requires_terminal_artifact_redaction", lambda _: True)

    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_disp",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_disp",
            ctx.codeblock_redaction_parameters,
            contains_sensitive_values=True,
            contains_all_sensitive_values=True,
        ),
    )

    assert result is not None
    assert seen == [html]
    assert secret not in page_evidence_prose_text(result)
    assert secret not in str(result)


@pytest.mark.asyncio
async def test_dispatched_fetch_rejects_sensitive_artifact_with_foreign_run_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential-secret-from-persisted-html"
    artifacts = [_html_artifact("art_secret", ArtifactType.HTML_ACTION)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_secret": secret.encode()})
    ctx = _producer_ctx()
    ctx.last_workflow = SimpleNamespace(
        workflow_definition=SimpleNamespace(parameters=[SimpleNamespace(workflow_parameter_type="credential_id")])
    )
    monkeypatch.setattr(run_execution_module, "_workflow_requires_terminal_artifact_redaction", lambda _: True)

    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_origin",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry("wr_foreign", {"password": secret}, True, True),
    )

    assert result is None
    assert retrieved_ids == []


@pytest.mark.asyncio
async def test_dispatched_fetch_rejects_sensitive_artifact_without_registered_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = [_html_artifact("art_secret", ArtifactType.HTML_ACTION)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_secret": b"sensitive page"})
    ctx = _producer_ctx()
    monkeypatch.setattr(run_execution_module, "_workflow_requires_terminal_artifact_redaction", lambda _: True)

    result = await run_execution_module._fetch_dispatched_terminal_page_evidence(
        run_id="wr_origin",
        organization_id="o_1",
        current_url="",
        workflow=ctx.last_workflow,
        origin_redaction_registry=OriginRunRedactionRegistry(
            "wr_origin",
            {"login_credential": "cred_id_only"},
            contains_sensitive_values=True,
            contains_all_sensitive_values=False,
        ),
    )

    assert result is None
    assert retrieved_ids == []


@pytest.mark.asyncio
async def test_dispatched_capture_does_not_license_artifact_from_mutable_context_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "origin-secret"
    artifacts = [_html_artifact("art_secret", ArtifactType.HTML_ACTION)]
    retrieved_ids = _stub_app(monkeypatch, artifacts, {"art_secret": secret.encode()})
    ctx = _producer_ctx(pre_run_prose=None)
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        "wr_origin", {"password": secret}, contains_sensitive_values=True, contains_all_sensitive_values=True
    )
    monkeypatch.setattr(run_execution_module, "_workflow_requires_terminal_artifact_redaction", lambda _: True)

    async def no_session_evidence(*_: object, **__: object) -> tuple[None, None, None, None]:
        return None, None, None, None

    monkeypatch.setattr(run_execution_module, "_read_run_session_page_evidence", no_session_evidence)

    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx,
        workflow=ctx.last_workflow,
        run_id="wr_origin",
        run_session_id="pbs_origin",
        organization_id="o_1",
        current_url="",
        origin_redaction_registry=None,
    )

    assert retrieved_ids == []
    assert ctx.composition_page_evidence is None


@pytest.mark.asyncio
async def test_dispatched_producer_confirms_value_only_post_run(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
    )
    assert ctx.composition_page_evidence["observed_after_workflow_run"] is True
    assert ctx.composition_page_evidence["workflow_run_id"] == "wr_disp"
    assert ctx.pre_run_page_reference is not None
    assert ctx.pre_run_page_reference.workflow_run_id == "wr_disp"
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "evidence_confirms"
    assert verdict.evidence_source == "independent_page_evidence"


@pytest.mark.asyncio
async def test_dispatched_producer_prefers_worker_artifact_over_a_substituted_session_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CDP read that landed on a replacement session still satisfies the usable check, so without
    the session comparison it would be stamped, refused, and leave the run with no evidence."""
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx()

    async def fake_read(
        inner_ctx: object, *, run_session_id: str, current_url: str
    ) -> tuple[dict[str, object], str, None, None]:
        return {"observed_empty_page": True, "current_url": current_url}, "pbs_replacement", None, None

    monkeypatch.setattr(run_execution_module, "_read_run_session_page_evidence", fake_read)

    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
    )

    stored = ctx.composition_page_evidence
    assert stored["source_browser_session_id"] == "pbs_run_disp"
    assert stored["observed_after_workflow_run"] is True
    assert "WTR-1842-DEMO" in page_evidence_prose_text(stored)


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
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
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
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.evidence_source == "independent_page_evidence"


@pytest.mark.asyncio
async def test_dispatched_producer_negative_control_value_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_NO_VALUE.encode()})
    ctx = _producer_ctx()
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
    )
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.reason_code != "evidence_confirms"


@pytest.mark.asyncio
async def test_dispatched_producer_value_in_baseline_does_not_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = [_html_artifact("art_action", ArtifactType.HTML_ACTION)]
    _stub_app(monkeypatch, artifacts, {"art_action": _HTML_WITH_VALUE.encode()})
    ctx = _producer_ctx(pre_run_prose="Prior page already showed WTR-1842-DEMO earlier.")
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
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
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
    )
    assert ctx.pre_run_page_reference is None
    verdict = _grade(_requested_criterion("WTR-1842-DEMO"), _snapshot_from_ctx(ctx, "wr_disp"))
    assert verdict.reason_code != "evidence_confirms"


@pytest.mark.asyncio
async def test_dispatched_producer_abstains_without_terminal_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_app(monkeypatch, artifacts=[], retrieved={})
    ctx = _producer_ctx(pre_run_prose=None)
    await run_execution_module._capture_dispatched_terminal_page_evidence(
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
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
        ctx, run_id="wr_disp", run_session_id="pbs_run_disp", organization_id="o_1", current_url=""
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
    assert any(container.get("selector_candidates") for container in containers)


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


def _star_ctx() -> _GroundingCtx:
    # Authored output contract declares the extraction's output path, so the block label is accepted.
    ctx = _GroundingCtx()
    ctx.code_artifact_metadata = {
        "extract_star_count": {"claimed_outcomes": [{"goal_value_paths": ["output.star_count"]}]}
    }
    return ctx


def _value_present_criterion() -> object:
    return make_completion_criterion(
        "c_star",
        "the number of stars for https://example.com/example-org/example-repo is retrieved",
        output_path="output.star_count",
        expected_output_shape="value_present",
    )


# block_outputs shape is copied from a real live code-only run (wr_555210248334407824): the
# extraction block nests its value under an ``output`` key beside ``evidence_text``.
def _star_snapshot(value: object = 22600) -> RunEvidenceSnapshot:
    return RunEvidenceSnapshot(
        block_outputs={"extract_star_count": {"evidence_text": "22.6k stars", "output": {"star_count": value}}},
        block_output_sources={"extract_star_count": "runtime_output"},
    )


def test_value_present_requested_output_credited_by_presence() -> None:
    verdicts = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], _star_snapshot())
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict.state == "satisfied"
    assert verdict.reason_code == "requested_output_present"


def test_value_present_requested_output_reaches_full_satisfaction() -> None:
    verdict = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], _star_snapshot())[0]
    result = CompletionVerificationResult(status="evaluated", criterion_ids=["c_star"], verdicts=[verdict])
    assert result.is_fully_satisfied() is True


def test_value_present_requested_output_abstains_when_value_missing() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={"extract_star_count": {"evidence_text": "no count found", "output": {}}},
        block_output_sources={"extract_star_count": "runtime_output"},
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], snapshot)[0]
    assert verdict.state != "satisfied"


def test_typed_shape_without_expected_value_still_abstains() -> None:
    # A shape that is NOT value_present (e.g. numeric_identifier) must keep abstaining on a present
    # value with no exact expected_output_value to prove -- the fix is scoped to value_present only.
    criterion = make_completion_criterion(
        "c_star",
        "the star count is retrieved",
        output_path="output.star_count",
        expected_output_shape="numeric_identifier",
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [criterion], _star_snapshot())[0]
    assert verdict.state != "satisfied"


@pytest.mark.parametrize("sentinel", ["N/A", "unknown", "Not Found", "  none  ", "-", "TBD"])
def test_value_present_not_found_sentinel_abstains(sentinel: str) -> None:
    # A failed extraction that returns a not-found sentinel instead of an empty value must not
    # verify: presence of "N/A"/"unknown" abstains rather than crediting delivery.
    snapshot = RunEvidenceSnapshot(
        block_outputs={"extract_star_count": {"evidence_text": "no count", "output": {"star_count": sentinel}}},
        block_output_sources={"extract_star_count": "runtime_output"},
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], snapshot)[0]
    assert verdict.state != "satisfied"


def test_value_present_abstains_when_emitting_block_failed() -> None:
    # Positive success signal: a value emitted by a block that failed is not a verified delivery.
    snapshot = RunEvidenceSnapshot(
        block_outputs={"extract_star_count": {"evidence_text": "22.6k stars", "output": {"star_count": 22600}}},
        block_output_sources={"extract_star_count": "runtime_output"},
        failed_block_labels=["extract_star_count"],
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], snapshot)[0]
    assert verdict.state != "satisfied"


def test_value_present_abstains_on_structured_error_payload() -> None:
    snapshot = RunEvidenceSnapshot(
        block_outputs={"extract_star_count": {"output": {"star_count": {"error": "extraction failed"}}}},
        block_output_sources={"extract_star_count": "runtime_output"},
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], snapshot)[0]
    assert verdict.state != "satisfied"


def test_value_present_credit_uses_presence_grounding_mode() -> None:
    verdict = grade_requested_output_criteria(_star_ctx(), [_value_present_criterion()], _star_snapshot())[0]
    assert verdict.state == "satisfied"
    assert verdict.grounding_mode == "presence"


def test_value_present_requiring_independent_evidence_abstains_on_self_emitted() -> None:
    # A value_present criterion that requests independent evidence is not certified by a self-emitted
    # (runtime_output) block value; the independence bar is preserved.
    criterion = make_completion_criterion(
        "c_star",
        "the number of stars for https://example.com/example-org/example-repo is retrieved",
        output_path="output.star_count",
        expected_output_shape="value_present",
        requested_output_evidence_source="independent_run_evidence",
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [criterion], _star_snapshot())[0]
    assert verdict.state != "satisfied"


def test_value_present_abstains_when_registered_output_producer_block_failed() -> None:
    # The value resolves via the <label>_output registered-output key, but its producer block failed;
    # the failed-block guard resolves the bare producer label so this still abstains.
    ctx = _GroundingCtx()
    ctx.code_artifact_metadata = {
        "extract_star_count_output": {"claimed_outcomes": [{"goal_value_paths": ["output.star_count"]}]}
    }
    snapshot = RunEvidenceSnapshot(
        block_outputs={"extract_star_count_output": {"output": {"star_count": 22600}}},
        block_output_sources={"extract_star_count_output": "registered_output_parameter"},
        failed_block_labels=["extract_star_count"],
    )
    verdict = grade_requested_output_criteria(ctx, [_value_present_criterion()], snapshot)[0]
    assert verdict.state != "satisfied"


# Byte-exact rows from the SKY-13332 live witness (wr_557702915819208430) and the SKY-13200 custody
# dir: a dashboard renders "8.45K" while the block registers the integer 8450, and the same tile's
# "-8.0%" delta yields a bare 8 that must not pass as the count.
_DELTA_DIGIT_EVIDENCE = "Visitors\n-8.0%"
_TILE_EVIDENCE = "Visitors\n-15.0%\n8.45K\nvs. 9.99K prior"


def test_literal_containment_alone_would_miss_the_correct_extraction() -> None:
    # Pins why the numeric-equivalence half is load-bearing: the correct value is not a substring of
    # the tile that displays it, so a containment-only rule leaves a right answer unconfirmable.
    assert _boundary_delimited_present("8450", _TILE_EVIDENCE) is False


def _exact_value_criterion(value: str) -> object:
    return make_completion_criterion(
        "c_star",
        "the number of stars for https://example.com/example-org/example-repo is retrieved",
        output_path="output.star_count",
        expected_output_value=value,
    )


def test_registered_scalar_does_not_witness_itself() -> None:
    # The delta digit reached a verified terminal because the block's own registered output counted
    # as independent confirmation of that same value.
    snapshot = RunEvidenceSnapshot(
        block_outputs={
            "extract_star_count_output": {
                "evidence_text": _DELTA_DIGIT_EVIDENCE,
                "output": {"star_count": 8},
            }
        },
        block_output_sources={"extract_star_count_output": "registered_output_parameter"},
    )
    verdict = grade_requested_output_criteria(_star_ctx(), [_exact_value_criterion("8")], snapshot)[0]
    assert verdict.state != "satisfied"


def test_runtime_output_scalar_still_credits_normally() -> None:
    # The narrowing targets the self-witnessing source only; ordinary runtime_output grading is
    # untouched, so the reject does not swallow the legitimate path.
    verdict = grade_requested_output_criteria(_star_ctx(), [_exact_value_criterion("22600")], _star_snapshot())[0]
    assert verdict.state == "satisfied"
