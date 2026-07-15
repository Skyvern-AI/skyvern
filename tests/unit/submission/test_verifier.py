from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest

from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.forge.sdk.submission.models import (
    BrowserPath,
    CandidateWindow,
    DownloadEvidence,
    NetworkEvidence,
    PageConfirmationEvidence,
    SubmissionSignal,
    TierAEvaluation,
    TierBEvaluation,
    UrlTransitionEvidence,
)
from skyvern.forge.sdk.submission.verifier import (
    CandidateEvaluation,
    build_candidate_windows,
    classify_browser_path,
    combine,
    detect_submit_candidates,
    evaluate_tier_a,
    evaluate_tier_b,
    find_latest_candidate_step_pair,
)
from skyvern.schemas.steps import AgentStepOutput, BrowserMetadata
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.responses import ActionResult

BASE_TIME = datetime(2026, 7, 9, 12, 0, 0)


def _step(step_id: str, order: int, *, url: str | None = None) -> Step:
    created_at = BASE_TIME + timedelta(seconds=order * 20)
    return Step(
        created_at=created_at,
        modified_at=created_at + timedelta(seconds=5),
        task_id="task_1",
        step_id=step_id,
        status=StepStatus.completed,
        output=AgentStepOutput(browser_metadata=BrowserMetadata(website_url=url)),
        order=order,
        is_last=False,
        organization_id="org_1",
    )


def _submit_click(*, step_id: str = "step_1", text: str = "Submit") -> ClickAction:
    return ClickAction(
        element_id="button_1",
        reasoning="submit",
        action_id="action_1",
        task_id="task_1",
        step_id=step_id,
        skyvern_element_data={"tagName": "button", "attributes": {}, "text": text},
    )


def _tier_a(
    *,
    evidence: list[NetworkEvidence] | None = None,
    har_present: bool = True,
    har_parsed: bool = True,
    har_entry_count: int = 1,
    ambiguous_entry_count: int = 0,
) -> TierAEvaluation:
    return TierAEvaluation(
        evidence=evidence or [],
        har_present=har_present,
        har_parsed=har_parsed,
        har_entry_count=har_entry_count,
        ambiguous_entry_count=ambiguous_entry_count,
    )


def _tier_b(
    evidence: list[PageConfirmationEvidence | UrlTransitionEvidence | DownloadEvidence] | None = None,
    *,
    page_confirmation_evaluated: bool = True,
) -> TierBEvaluation:
    return TierBEvaluation(
        evidence=evidence or [],
        page_confirmation_evaluated=page_confirmation_evaluated,
    )


def _network_evidence() -> NetworkEvidence:
    return NetworkEvidence(
        origin="https://example.com",
        method="POST",
        status=201,
        started_at=BASE_TIME,
        correlated_step_id="step_1",
    )


def _page_evidence() -> PageConfirmationEvidence:
    return PageConfirmationEvidence(
        phrase="confirmation number",
        value_sha256=hashlib.sha256(b"abc-123").hexdigest(),
        absent_pre_submit=True,
    )


def _har_for_candidate(*, method: str = "POST", status: int = 201) -> bytes:
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "startedDateTime": "2026-07-09T12:00:22Z",
                        "request": {"method": method, "url": "https://example.com/api/submit"},
                        "response": {"status": status, "content": {"mimeType": "application/json"}},
                        "_resourceType": "xhr",
                    }
                ]
            }
        }
    ).encode()


def _har_at(*offsets: int) -> bytes:
    return json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "startedDateTime": (BASE_TIME + timedelta(seconds=offset)).isoformat() + "Z",
                        "request": {"method": "POST", "url": "https://example.com/api/submit"},
                        "response": {"status": 201, "content": {"mimeType": "application/json"}},
                        "_resourceType": "xhr",
                    }
                    for offset in offsets
                ]
            }
        }
    ).encode()


def _window(step_id: str, started_at: int, ended_at: int) -> CandidateWindow:
    return CandidateWindow(
        step_id=step_id,
        started_at=BASE_TIME + timedelta(seconds=started_at),
        ended_at=BASE_TIME + timedelta(seconds=ended_at),
    )


def _combine(
    *,
    tier_a: TierAEvaluation | None = None,
    tier_b: TierBEvaluation | None = None,
    submit_intent_detected: bool = True,
    browser_path: BrowserPath = BrowserPath.SKYVERN_CREATED,
    cua_run: bool = False,
    coordinate_click: bool = False,
):
    tier_a = tier_a or _tier_a()
    return combine(
        tier_a=tier_a,
        candidate_evaluations=[
            CandidateEvaluation(
                step_id="step_1",
                tier_a=tier_a.evidence,
                tier_b=tier_b or _tier_b(),
                is_latest=True,
            )
        ],
        detected_candidate_step_ids=["step_1"],
        submit_intent_detected=submit_intent_detected,
        browser_path=browser_path,
        cua_run=cua_run,
        coordinate_click=coordinate_click,
    )


def test_classify_browser_path_uses_first_matching_signal() -> None:
    assert (
        classify_browser_path(
            browser_session_id="session_1",
            task_browser_session_id=None,
            remote_browser_session_id="remote_1",
            task_browser_address="ws://example.test",
            needs_cdp_frame_publisher=True,
            browser_type="chromium-headless",
        )
        == BrowserPath.SESSION_ATTACHED
    )
    assert (
        classify_browser_path(
            browser_session_id=None,
            task_browser_session_id=None,
            remote_browser_session_id="remote_1",
            task_browser_address="ws://example.test",
            needs_cdp_frame_publisher=True,
            browser_type="chromium-headless",
        )
        == BrowserPath.VENDOR_REUSED
    )
    assert (
        classify_browser_path(
            browser_session_id=None,
            task_browser_session_id=None,
            remote_browser_session_id=None,
            task_browser_address=None,
            needs_cdp_frame_publisher=True,
            browser_type="chromium-headless",
        )
        == BrowserPath.CDP_CONNECT
    )
    assert (
        classify_browser_path(
            browser_session_id=None,
            task_browser_session_id=None,
            remote_browser_session_id=None,
            task_browser_address=None,
            needs_cdp_frame_publisher=False,
            browser_type="chromium-headful",
        )
        == BrowserPath.SKYVERN_CREATED
    )
    assert (
        classify_browser_path(
            browser_session_id=None,
            task_browser_session_id=None,
            remote_browser_session_id=None,
            task_browser_address=None,
            needs_cdp_frame_publisher=False,
            browser_type="other",
        )
        == BrowserPath.UNKNOWN
    )


def test_detect_submit_candidates_and_coordinate_click_override() -> None:
    typed_submit = ClickAction(
        element_id="button_1",
        reasoning="submit",
        step_id="step_1",
        skyvern_element_data={"tagName": "input", "attributes": {"type": "submit"}, "text": "Continue"},
    )
    verb_submit = _submit_click(text="Apply now")
    false_boundary = _submit_click(text="Resubmit")
    split_phrase = ClickAction(
        element_id="button_2",
        reasoning="continue",
        step_id="step_1",
        skyvern_element_data={
            "tagName": "button",
            "attributes": {"aria-label": "order"},
            "text": "Place",
        },
    )
    coordinate_click = ClickAction(
        element_id="button_3",
        reasoning="coordinate",
        step_id="step_2",
        x=10,
        skyvern_element_data={"tagName": "button", "attributes": {"type": "submit"}, "text": "Submit"},
    )

    detected = detect_submit_candidates([typed_submit, verb_submit, false_boundary, split_phrase, coordinate_click])

    assert [candidate.step_id for candidate in detected.candidates] == ["step_1", "step_1"]
    assert detected.submit_intent_detected is True
    assert detected.coordinate_click is True
    assert "Apply now" not in detected.model_dump_json()


def test_combine_uses_the_strongest_coherent_candidate() -> None:
    older_network = _network_evidence()
    latest_page = _page_evidence()
    latest_download = DownloadEvidence(file_count=1)
    tier_a = _tier_a(evidence=[older_network])

    verdict = combine(
        tier_a=tier_a,
        candidate_evaluations=[
            CandidateEvaluation(
                step_id="step_1",
                tier_a=[older_network],
                tier_b=_tier_b(),
            ),
            CandidateEvaluation(
                step_id="step_3",
                tier_a=[],
                tier_b=_tier_b([latest_page]),
                is_latest=True,
            ),
        ],
        detected_candidate_step_ids=["step_1", "step_3"],
        submit_intent_detected=True,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    assert verdict.signal == SubmissionSignal.SUBMITTED_LIKELY
    assert verdict.tier_a == []
    assert verdict.tier_b == [latest_page]
    assert verdict.winning_step_id == "step_3"
    assert not any(note.startswith("winning_step_id:") for note in verdict.notes)

    verdict = combine(
        tier_a=tier_a,
        candidate_evaluations=[
            CandidateEvaluation(
                step_id="step_1",
                tier_a=[older_network],
                tier_b=_tier_b([latest_page]),
            ),
            CandidateEvaluation(
                step_id="step_3",
                tier_a=[],
                tier_b=_tier_b([latest_download]),
                is_latest=True,
            ),
        ],
        detected_candidate_step_ids=["step_1", "step_3"],
        submit_intent_detected=True,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    assert verdict.signal == SubmissionSignal.SUBMITTED_VERIFIED
    assert verdict.tier_a == [older_network]
    assert verdict.tier_b == [latest_page]
    assert verdict.winning_step_id == "step_1"
    assert not any(note.startswith("winning_step_id:") for note in verdict.notes)


def test_tier_a_discards_an_entry_matching_multiple_candidate_windows() -> None:
    windows = [_window("step_1", 10, 25), _window("step_3", 20, 35)]
    tier_a = evaluate_tier_a(_har_at(22), windows)

    verdict = combine(
        tier_a=tier_a,
        candidate_evaluations=[
            CandidateEvaluation(
                step_id="step_1",
                tier_a=[item for item in tier_a.evidence if item.correlated_step_id == "step_1"],
                tier_b=_tier_b([_page_evidence()]),
            ),
            CandidateEvaluation(
                step_id="step_3",
                tier_a=[item for item in tier_a.evidence if item.correlated_step_id == "step_3"],
                tier_b=_tier_b(),
                is_latest=True,
            ),
        ],
        detected_candidate_step_ids=["step_1", "step_3"],
        submit_intent_detected=True,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    assert verdict.signal == SubmissionSignal.SUBMITTED_LIKELY
    assert tier_a.evidence == []
    assert verdict.tier_a == []
    assert verdict.winning_step_id == "step_1"
    assert "ambiguous_entries:1" in verdict.notes


def test_tier_a_attributes_an_entry_matching_exactly_one_of_overlapping_windows() -> None:
    windows = [_window("step_1", 10, 25), _window("step_3", 20, 35)]

    evaluation = evaluate_tier_a(_har_at(12), windows)

    assert [item.correlated_step_id for item in evaluation.evidence] == ["step_1"]
    assert evaluation.ambiguous_entry_count == 0


def test_tier_a_attributes_two_unambiguous_entries_to_different_candidates() -> None:
    windows = [_window("step_1", 10, 25), _window("step_3", 20, 35)]

    evaluation = evaluate_tier_a(_har_at(12, 32), windows)

    assert [item.correlated_step_id for item in evaluation.evidence] == ["step_1", "step_3"]
    assert evaluation.ambiguous_entry_count == 0


def test_candidate_pair_uses_next_chronological_step_as_post_action() -> None:
    steps = [_step("step_3", 3), _step("step_1", 1), _step("step_2", 2)]
    detected = detect_submit_candidates([_submit_click(step_id="step_2")])

    windows = build_candidate_windows(detected, steps)
    pre_step, post_step = find_latest_candidate_step_pair(detected, steps)

    assert len(windows) == 1
    assert windows[0].step_id == "step_2"
    assert windows[0].started_at < steps[2].created_at
    assert pre_step is not None and pre_step.step_id == "step_2"
    assert post_step is not None and post_step.step_id == "step_3"


def test_latest_candidate_without_a_post_step_does_not_reuse_an_older_pair() -> None:
    steps = [_step("step_1", 1), _step("step_2", 2), _step("step_3", 3)]
    detected = detect_submit_candidates([_submit_click(step_id="step_1"), _submit_click(step_id="step_3")])

    pre_step, post_step = find_latest_candidate_step_pair(detected, steps)

    assert pre_step is not None and pre_step.step_id == "step_3"
    assert post_step is None


def test_tier_a_filters_non_submission_entries_and_normalizes_timestamps() -> None:
    step = _step("step_1", 1)
    windows = build_candidate_windows(detect_submit_candidates([_submit_click()]), [step])

    def entry(
        url: str,
        *,
        method: str = "POST",
        status: int = 200,
        resource_type: str | None = "xhr",
        started_at: str = "2026-07-09T12:00:22Z",
        mime_type: str = "application/json",
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "startedDateTime": started_at,
            "request": {"method": method, "url": url},
            "response": {"status": status, "content": {"mimeType": mime_type}},
        }
        if resource_type is not None:
            value["_resourceType"] = resource_type
        return value

    har_bytes = json.dumps(
        {
            "log": {
                "entries": [
                    entry("https://example.com/api/submit?token=secret", status=201),
                    entry("https://example.com/api/read", method="GET"),
                    entry("https://example.com/api/rejected", status=404),
                    entry("https://example.com/api/fail", status=500),
                    entry("https://example.com/image.png", resource_type="image", mime_type="image/png"),
                    entry("https://example.com/styles.css", resource_type=None, mime_type="text/css"),
                    entry(
                        "https://example.com/scripts/app.js",
                        resource_type=None,
                        mime_type="application/javascript",
                    ),
                    entry("https://example.com/api/late", started_at="2026-07-09T12:05:00+00:00"),
                ]
            }
        }
    ).encode()

    evaluation = evaluate_tier_a(har_bytes, windows)

    assert evaluation.har_present is True
    assert evaluation.har_parsed is True
    assert evaluation.har_entry_count == 8
    assert len(evaluation.evidence) == 1
    assert evaluation.evidence[0].origin == "https://example.com"
    assert evaluation.evidence[0].correlated_step_id == "step_1"


@pytest.mark.parametrize(("method", "status"), [("POST", 201), ("PUT", 204), ("PATCH", 302)])
def test_tier_a_accepts_each_submission_method_and_success_status(method: str, status: int) -> None:
    step = _step("step_1", 1)
    windows = build_candidate_windows(detect_submit_candidates([_submit_click()]), [step])
    har_bytes = json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "startedDateTime": "2026-07-09T12:00:22Z",
                        "request": {"method": method, "url": "https://example.com/api/submit"},
                        "response": {"status": status, "content": {"mimeType": "application/json"}},
                        "_resourceType": "fetch",
                    }
                ]
            }
        }
    ).encode()

    evaluation = evaluate_tier_a(har_bytes, windows)

    assert [item.method for item in evaluation.evidence] == [method]


def test_tier_a_malformed_har_is_unavailable_not_an_exception() -> None:
    evaluation = evaluate_tier_a(b"not-json", [])

    assert evaluation.har_present is True
    assert evaluation.har_parsed is False
    assert evaluation.har_entry_count == 0
    assert evaluation.evidence == []


def test_tier_a_excessively_nested_har_is_unavailable_not_an_exception() -> None:
    deeply_nested_json = b'{"x":' * 10_000 + b"0" + b"}" * 10_000

    evaluation = evaluate_tier_a(deeply_nested_json, [])

    assert evaluation.har_present is True
    assert evaluation.har_parsed is False
    assert evaluation.har_entry_count == 0


def test_tier_a_empty_har_is_missing() -> None:
    evaluation = evaluate_tier_a(b"", [])

    assert evaluation.har_present is False
    assert evaluation.har_parsed is False
    assert evaluation.har_entry_count == 0


def test_tier_b_detects_independent_evidence_without_retaining_raw_values() -> None:
    fake_value = "abc-123"
    evaluation = evaluate_tier_b(
        pre_url="https://example.test/apply/user@example.test/token-before?draft=private#form",
        post_url="https://example.test/complete/user@example.test/token-after?token=private#receipt",
        action_results=[ActionResult(success=True, download_triggered=True, downloaded_files=["private-receipt.pdf"])],
        pre_page_text="Application ready",
        post_page_text=f"Thank you. Confirmation number: {fake_value}",
    )

    assert evaluation.page_confirmation_evaluated is True
    assert len(evaluation.evidence) == 3
    url_evidence = next(evidence for evidence in evaluation.evidence if isinstance(evidence, UrlTransitionEvidence))
    confirmation = next(evidence for evidence in evaluation.evidence if isinstance(evidence, PageConfirmationEvidence))
    download = next(evidence for evidence in evaluation.evidence if isinstance(evidence, DownloadEvidence))
    assert url_evidence.from_origin == "https://example.test"
    assert url_evidence.to_origin == "https://example.test"
    assert url_evidence.path_changed is True
    assert confirmation.value_sha256 == hashlib.sha256(fake_value.encode()).hexdigest()
    assert download.file_count == 1
    assert fake_value not in evaluation.model_dump_json()
    assert "private-receipt.pdf" not in evaluation.model_dump_json()
    assert "token=" not in evaluation.model_dump_json()
    assert "user@example.test" not in evaluation.model_dump_json()
    assert "token-before" not in evaluation.model_dump_json()
    assert "token-after" not in evaluation.model_dump_json()


def test_tier_b_ignores_query_fragment_only_url_changes_and_existing_confirmation() -> None:
    evaluation = evaluate_tier_b(
        pre_url="https://example.test/form?draft=one#start",
        post_url="https://example.test/form?draft=two#end",
        action_results=[],
        pre_page_text="Thank you for reviewing your request.",
        post_page_text="Thank you for reviewing your request.",
    )

    assert evaluation.page_confirmation_evaluated is True
    assert evaluation.evidence == []


@pytest.mark.parametrize(
    "result",
    [
        ActionResult(success=True, download_triggered=True),
        ActionResult(success=True, downloaded_files=["receipt.pdf"]),
    ],
)
def test_tier_b_accepts_each_download_signal(result: ActionResult) -> None:
    evaluation = evaluate_tier_b(
        pre_url=None,
        post_url=None,
        action_results=[result],
        pre_page_text="Ready",
        post_page_text="Still ready",
    )

    assert evaluation.evidence == [DownloadEvidence(file_count=1)]


def test_all_submission_signal_values_are_reachable() -> None:
    assert (
        _combine(tier_a=_tier_a(evidence=[_network_evidence()]), tier_b=_tier_b([_page_evidence()])).signal
        == SubmissionSignal.SUBMITTED_VERIFIED
    )
    assert _combine(tier_a=_tier_a(evidence=[_network_evidence()])).signal == SubmissionSignal.SUBMITTED_LIKELY
    assert _combine(tier_b=_tier_b([_page_evidence()])).signal == SubmissionSignal.SUBMITTED_LIKELY
    assert _combine().signal == SubmissionSignal.NOT_SUBMITTED
    assert _combine(tier_a=_tier_a(har_present=False, har_parsed=False, har_entry_count=0)).signal == (
        SubmissionSignal.NOT_EVALUATED
    )


@pytest.mark.parametrize(
    ("har_bytes", "post_page_text", "expected_signal"),
    [
        (_har_for_candidate(), "Thank you. Your request was received.", SubmissionSignal.SUBMITTED_VERIFIED),
        (_har_for_candidate(), "The form remains ready.", SubmissionSignal.SUBMITTED_LIKELY),
        (_har_for_candidate(method="GET"), "The form remains ready.", SubmissionSignal.NOT_SUBMITTED),
        (b"", "The form remains ready.", SubmissionSignal.NOT_EVALUATED),
    ],
)
def test_every_signal_is_reachable_from_raw_inputs(
    har_bytes: bytes,
    post_page_text: str,
    expected_signal: SubmissionSignal,
) -> None:
    step = _step("step_1", 1)
    detection = detect_submit_candidates([_submit_click()])
    tier_a = evaluate_tier_a(har_bytes, build_candidate_windows(detection, [step]))
    tier_b = evaluate_tier_b(
        pre_url="https://example.com/form",
        post_url="https://example.com/form",
        action_results=[],
        pre_page_text="Review your request.",
        post_page_text=post_page_text,
    )

    verdict = combine(
        tier_a=tier_a,
        candidate_evaluations=[
            CandidateEvaluation(
                step_id="step_1",
                tier_a=tier_a.evidence,
                tier_b=tier_b,
                is_latest=True,
            )
        ],
        detected_candidate_step_ids=["step_1"],
        submit_intent_detected=detection.submit_intent_detected,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    assert verdict.signal == expected_signal


def test_not_submitted_unreachable_with_ambiguous_entries() -> None:
    verdict = _combine(tier_a=_tier_a(ambiguous_entry_count=1))

    assert verdict.signal == SubmissionSignal.NOT_EVALUATED
    assert "ambiguous_entries:1" in verdict.notes


def test_not_submitted_unreachable_when_any_candidate_page_confirmation_unknown() -> None:
    older = CandidateEvaluation(step_id="step_1", tier_a=[], tier_b=_tier_b(page_confirmation_evaluated=False))
    latest = CandidateEvaluation(step_id="step_2", tier_a=[], tier_b=_tier_b(), is_latest=True)

    verdict = combine(
        tier_a=_tier_a(),
        candidate_evaluations=[older, latest],
        detected_candidate_step_ids=["step_1", "step_2"],
        submit_intent_detected=True,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    assert verdict.signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_when_a_detected_candidate_step_is_unmapped() -> None:
    mapped = CandidateEvaluation(step_id="step_valid", tier_a=[], tier_b=_tier_b(), is_latest=True)

    verdict = combine(
        tier_a=_tier_a(),
        candidate_evaluations=[mapped],
        detected_candidate_step_ids=["step_valid", "step_missing"],
        submit_intent_detected=True,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    assert verdict.signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_when_har_is_missing() -> None:
    assert _combine(tier_a=_tier_a(har_present=False)).signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_when_har_is_unparseable() -> None:
    assert _combine(tier_a=_tier_a(har_parsed=False)).signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_when_har_has_zero_entries() -> None:
    assert _combine(tier_a=_tier_a(har_entry_count=0)).signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_without_submit_intent() -> None:
    assert _combine(submit_intent_detected=False).signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_outside_skyvern_created_path() -> None:
    assert _combine(browser_path=BrowserPath.CDP_CONNECT).signal == SubmissionSignal.NOT_EVALUATED


def test_not_submitted_unreachable_when_page_confirmation_is_unavailable() -> None:
    assert _combine(tier_b=_tier_b(page_confirmation_evaluated=False)).signal == SubmissionSignal.NOT_EVALUATED


@pytest.mark.parametrize("override", [{"cua_run": True}, {"coordinate_click": True}])
def test_cua_and_coordinate_clicks_are_not_evaluated(override: dict[str, bool]) -> None:
    verdict = _combine(
        tier_a=_tier_a(evidence=[_network_evidence()]),
        tier_b=_tier_b([_page_evidence()]),
        **override,
    )

    assert verdict.signal == SubmissionSignal.NOT_EVALUATED


@pytest.mark.parametrize(
    "browser_path",
    [BrowserPath.CDP_CONNECT, BrowserPath.VENDOR_REUSED, BrowserPath.SESSION_ATTACHED, BrowserPath.UNKNOWN],
)
def test_non_skyvern_paths_are_tier_b_only_and_capped(browser_path: BrowserPath) -> None:
    verdict = _combine(
        tier_a=_tier_a(evidence=[_network_evidence()]),
        tier_b=_tier_b([_page_evidence(), DownloadEvidence(file_count=1)]),
        browser_path=browser_path,
    )

    assert verdict.signal == SubmissionSignal.SUBMITTED_LIKELY
    assert verdict.tier_a == []
    assert verdict.capped is True

    without_tier_b = _combine(
        tier_a=_tier_a(evidence=[_network_evidence()]),
        browser_path=browser_path,
    )
    assert without_tier_b.signal == SubmissionSignal.NOT_EVALUATED
    assert without_tier_b.tier_a == []
    assert without_tier_b.capped is True


def test_evidence_models_only_serialize_derived_facts() -> None:
    fake_value = "fake-confirmation-value"
    fake_filename = "private-receipt.pdf"
    har_bytes = json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "startedDateTime": "2026-07-09T12:00:22Z",
                        "request": {
                            "method": "POST",
                            "url": "https://example.com/api/submit?token=private",
                            "postData": {"text": fake_value},
                        },
                        "response": {
                            "status": 201,
                            "content": {"mimeType": "application/json", "text": fake_value},
                        },
                        "_resourceType": "fetch",
                    }
                ]
            }
        }
    ).encode()
    step = _step("step_1", 1)
    detection = detect_submit_candidates([_submit_click()])
    tier_a = evaluate_tier_a(har_bytes, build_candidate_windows(detection, [step]))
    tier_b = evaluate_tier_b(
        pre_url="https://example.com/form/user@example.com/private-token?draft=private#start",
        post_url="https://example.com/complete/user@example.com/private-token?token=private#receipt",
        action_results=[ActionResult(success=True, downloaded_files=[fake_filename])],
        pre_page_text="Review your request.",
        post_page_text=f"Confirmation number: {fake_value}",
    )
    verdict = combine(
        tier_a=tier_a,
        candidate_evaluations=[
            CandidateEvaluation(
                step_id="step_1",
                tier_a=tier_a.evidence,
                tier_b=tier_b,
                is_latest=True,
            )
        ],
        detected_candidate_step_ids=["step_1"],
        submit_intent_detected=detection.submit_intent_detected,
        browser_path=BrowserPath.SKYVERN_CREATED,
    )

    serialized = verdict.model_dump_json()
    assert verdict.signal == SubmissionSignal.SUBMITTED_VERIFIED
    assert fake_value not in serialized
    assert fake_filename not in serialized
    assert "?" not in serialized
    assert "body" not in serialized
    assert "postData" not in serialized
    assert "user@example.com" not in serialized
    assert "private-token" not in serialized
    assert hashlib.sha256(fake_value.encode()).hexdigest() in serialized
