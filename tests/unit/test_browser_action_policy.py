import ast
import dataclasses
from enum import StrEnum
from pathlib import Path

import pytest
from pydantic import ValidationError

from skyvern.config import Settings
from skyvern.forge.sdk.browser_action_policy import (
    REASON_PRECEDENCE,
    UNWIRED_AUTHORITY,
    ActionClass,
    ActionProjection,
    ActionTarget,
    AuthorityState,
    BrowserActionPolicy,
    BrowserActionRequest,
    BrowserOrigin,
    ElementDestination,
    ObservationVerdict,
    ObservedElement,
    PageObservation,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    ProtectedReference,
    ProtectedReferenceKind,
    ResolvedTarget,
    RuntimeOriginAuthority,
    TargetKind,
    canonicalize_origin,
    classify_action_type,
    coerce_verdict,
    decide_browser_action,
    declare_origin,
    declare_policy,
    hydrate_destination,
    with_resolved_target,
)
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import (
    Action,
    CheckboxAction,
    ClickAction,
    ClosePageAction,
    CompleteAction,
    DownloadFileAction,
    DragAction,
    ExecuteJsAction,
    ExtractAction,
    GoBackAction,
    GoForwardAction,
    GotoUrlAction,
    HoverAction,
    InputTextAction,
    KeypressAction,
    LeftMouseAction,
    MoveAction,
    NewTabAction,
    NullAction,
    PasteTextAction,
    ReloadPageAction,
    ScrollAction,
    SelectOption,
    SelectOptionAction,
    SolveCaptchaAction,
    SwitchTabAction,
    TerminateAction,
    UploadFileAction,
    VerificationCodeAction,
    WaitAction,
)

OWNER = "wr_12872"
HOME = "https://example.com/start"
HOME_ORIGIN = declare_origin(HOME)
POLICY = declare_policy(owner_id=OWNER, origin_urls=[HOME])
EPOCH = 7
# The runtime authority the existing gates are exercised against: established, and wide enough that
# it never narrows the ceiling. Tests about authority itself pass their own.
FULL_AUTHORITY = RuntimeOriginAuthority(state=AuthorityState.ESTABLISHED, origins=POLICY.allowed_origins)
# Scrape-time destination facts for the element the standard test click lands on: a same-origin
# anchor. Facts ATTACH a target but never establish completeness — a main-world-sourced fact must
# never be able to produce ALLOWED — so a hydrated mutating action still reads INCOMPLETE.
ANCHOR_HOME = ElementDestination(kind=TargetKind.ANCHOR, url=HOME)
OBSERVED_HASH = "eh-observed"

# Every concrete action model at its minimum valid construction, keyed by discriminator.
CONCRETE_ACTIONS: dict[ActionType, Action] = {
    ActionType.CLICK: ClickAction(element_id="1"),
    ActionType.INPUT_TEXT: InputTextAction(element_id="1", text="hello"),
    ActionType.PASTE_TEXT: PasteTextAction(element_id="1", text="a\tb"),
    ActionType.UPLOAD_FILE: UploadFileAction(element_id="1", file_url="https://example.com/f.pdf"),
    ActionType.DOWNLOAD_FILE: DownloadFileAction(file_name="f.pdf"),
    ActionType.SELECT_OPTION: SelectOptionAction(element_id="1", option=SelectOption(label="a")),
    ActionType.CHECKBOX: CheckboxAction(element_id="1", is_checked=True),
    ActionType.WAIT: WaitAction(),
    ActionType.HOVER: HoverAction(element_id="1"),
    ActionType.NULL_ACTION: NullAction(),
    ActionType.SOLVE_CAPTCHA: SolveCaptchaAction(),
    ActionType.TERMINATE: TerminateAction(),
    ActionType.COMPLETE: CompleteAction(),
    ActionType.RELOAD_PAGE: ReloadPageAction(),
    ActionType.CLOSE_PAGE: ClosePageAction(),
    ActionType.NEW_TAB: NewTabAction(url=HOME),
    ActionType.SWITCH_TAB: SwitchTabAction(tab_index=0),
    ActionType.EXTRACT: ExtractAction(),
    ActionType.VERIFICATION_CODE: VerificationCodeAction(verification_code="123456"),
    ActionType.GOTO_URL: GotoUrlAction(url=HOME),
    ActionType.GO_BACK: GoBackAction(),
    ActionType.GO_FORWARD: GoForwardAction(),
    ActionType.SCROLL: ScrollAction(),
    ActionType.KEYPRESS: KeypressAction(keys=["Enter"]),
    ActionType.MOVE: MoveAction(),
    ActionType.DRAG: DragAction(),
    ActionType.LEFT_MOUSE: LeftMouseAction(direction="down"),
    ActionType.EXECUTE_JS: ExecuteJsAction(js_code="1"),
}


def build_request(
    action: Action,
    *,
    policy: BrowserActionPolicy | None = POLICY,
    page_url: str = HOME,
    evidence_epoch: int | None = EPOCH,
    request_epoch: int = EPOCH,
    verdict: ObservationVerdict = ObservationVerdict.NO_MATCH,
    protected_references: tuple[ProtectedReference, ...] = (),
    evidence: PageObservation | None = None,
    omit_evidence: bool = False,
    authority: RuntimeOriginAuthority = FULL_AUTHORITY,
    destination: ElementDestination | None = None,
) -> BrowserActionRequest:
    if not omit_evidence and evidence is None:
        evidence = PageObservation(
            page_url=page_url,
            observation_epoch=EPOCH if evidence_epoch is None else evidence_epoch,
            verdict=verdict,
        )
    projection = project_for(action)
    if destination is not None:
        projection = hydrate_destination(
            projection,
            claimed_element_hash=None,
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=destination),
        )
    return BrowserActionRequest(
        policy=policy,
        projection=projection,
        authority=authority,
        evidence=None if omit_evidence else evidence,
        request_epoch=request_epoch,
        protected_references=protected_references,
    )


def project_for(action: Action) -> ActionProjection:
    from skyvern.forge.sdk.browser_action_policy import project_action

    return project_action(action)


def secret_ref(owner_id: str = OWNER) -> ProtectedReference:
    return ProtectedReference(
        kind=ProtectedReferenceKind.SECRET,
        reference_id="ref_1",
        owner_id=owner_id,
    )


def file_ref(owner_id: str = OWNER) -> ProtectedReference:
    return ProtectedReference(
        kind=ProtectedReferenceKind.FILE,
        reference_id="ref_2",
        owner_id=owner_id,
    )


def code_ref(owner_id: str = OWNER) -> ProtectedReference:
    return ProtectedReference(
        kind=ProtectedReferenceKind.VERIFICATION_CODE,
        reference_id="ref_3",
        owner_id=owner_id,
    )


def refs_for(action: Action) -> tuple[ProtectedReference, ...]:
    projection = project_for(action)
    supplied = {
        ProtectedReferenceKind.SECRET: secret_ref(),
        ProtectedReferenceKind.FILE: file_ref(),
        ProtectedReferenceKind.VERIFICATION_CODE: code_ref(),
    }
    return tuple(supplied[kind] for kind in sorted(projection.required_references))


class TestObservationVerdictContract:
    def test_member_values_are_pinned_literal_strings(self) -> None:
        # Pins THIS enum's wire values, which is the half of the contract this package controls.
        # It cannot observe cloud/prompt_security.DetectionOutcome — the OSS boundary forbids
        # importing it — so the two-sided cross-check belongs in tests/cloud/ once that lands.
        assert ObservationVerdict.NO_MATCH.value == "no_match"
        assert ObservationVerdict.SUSPICIOUS.value == "suspicious"
        assert ObservationVerdict.UNKNOWN.value == "unknown"
        assert [member.value for member in ObservationVerdict] == ["no_match", "suspicious", "unknown"]

    def test_accepts_a_foreign_str_enum_by_value(self) -> None:
        class ForeignDetectionOutcome(StrEnum):
            NO_MATCH = "no_match"
            SUSPICIOUS = "suspicious"
            UNKNOWN = "unknown"

        for foreign in ForeignDetectionOutcome:
            assert coerce_verdict(foreign) is ObservationVerdict(foreign.value)

    @pytest.mark.parametrize("value", [None, "", "clean", "NO_MATCH", 0, object(), ["no_match"]])
    def test_unrecognized_verdict_fails_closed_to_unknown(self, value: object) -> None:
        assert coerce_verdict(value) is ObservationVerdict.UNKNOWN


class TestOriginCanonicalization:
    @pytest.mark.parametrize(
        ("url", "canonical"),
        [
            ("https://example.com", "https://example.com"),
            ("https://example.com:443/path?q=1#f", "https://example.com"),
            ("http://example.com:80/", "http://example.com"),
            ("https://example.com:8443/", "https://example.com:8443"),
            ("HTTPS://ExAmPlE.CoM/", "https://example.com"),
            ("https://example.com./", "https://example.com"),
            ("https://[2001:0db8:0000:0000:0000:0000:0000:0001]/", "https://[2001:db8::1]"),
            ("https://192.168.1.1/", "https://192.168.1.1"),
            ("https://xn--bcher-kva.example/", "https://xn--bcher-kva.example"),
            ("ws://example.com/socket", "http://example.com"),
            ("wss://example.com/socket", "https://example.com"),
        ],
    )
    def test_canonicalizes_deterministically(self, url: str, canonical: str) -> None:
        origin = canonicalize_origin(url)
        assert origin is not None
        assert origin.canonical == canonical
        assert canonicalize_origin(url) == origin

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "   ",
            " https://example.com",
            "https://example.com ",
            "https://user:pw@example.com/",
            "https://user@example.com/",
            "https:\\\\example.com/",
            "https://exa\tmple.com/",
            "https://exa\nmple.com/",
            "https://example.com\x7f/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,x",
            "about:blank",
            "chrome://newtab/",
            "ftp://example.com/",
            "https:///path",
            "https://%2e%2e/",
            "not a url",
            123,
            # Ambiguous dotted-decimal forms: ip_address rejects leading zeros (CVE-2021-29921),
            # so silently IDNA-encoding them would mint an origin that never matches its own host.
            "https://192.168.001.001/",
            "https://0177.0.0.1/",
            "https://1/",
        ],
    )
    def test_runtime_extraction_fails_closed(self, url: object) -> None:
        assert canonicalize_origin(url) is None

    @pytest.mark.parametrize(
        "url",
        [
            # stdlib .encode("idna") is IDNA2003 + nameprep; browsers use UTS-46 nontransitional.
            # Nameprep folds ß to "ss" and ς to σ, so trusting it would authorize a host the browser
            # never visits. Operators declare the A-label instead, which is what the browser uses.
            "https://faß.de",
            "https://σος.example",
            "https://exa‌mple.com",
            "https://bücher.example",
            "https://example。com。",
            "https://example。com",
            # WHATWG parses a trailing numeric label as an IPv4 literal, so these reach a host that
            # ipaddress refuses to canonicalize for us.
            "https://0x7f000001",
            "https://example.0x1",
            "https://2130706433",
        ],
    )
    def test_browser_divergent_hosts_are_rejected(self, url: str) -> None:
        assert canonicalize_origin(url) is None

    def test_sharp_s_twin_cannot_borrow_an_allowlisted_origin(self) -> None:
        # A browser sends faß.de to xn--fa-hia.de, a separately registrable domain. GotoUrlAction.url
        # is model-proposed text, so this is attacker-reachable under indirect prompt injection.
        policy = declare_policy(owner_id=OWNER, origin_urls=["https://fass.de"])
        request = BrowserActionRequest(
            policy=policy,
            projection=project_for(GotoUrlAction(url="https://faß.de")),
            authority=RuntimeOriginAuthority(state=AuthorityState.ESTABLISHED, origins=policy.allowed_origins),
            request_epoch=EPOCH,
            evidence=PageObservation(
                page_url="https://fass.de", observation_epoch=EPOCH, verdict=ObservationVerdict.NO_MATCH
            ),
        )
        decision = decide_browser_action(request)
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_TARGET_ORIGIN in decision.reasons

    def test_equivalent_host_spellings_share_one_canonical_form(self) -> None:
        # A declared origin that canonicalizes to a host no page can ever present would be a policy
        # that silently matches nothing, so equivalent spellings must collapse to one value.
        for spelling in (
            "https://example.com",
            "https://example.com.",
            "https://EXAMPLE.COM.",
            "https://example.com..",
        ):
            assert canonicalize_origin(spelling) == HOME_ORIGIN, spelling
        # A confusable is a DIFFERENT host and must NOT collapse onto the declared one.
        assert canonicalize_origin("https://exаmple.com") is None

    def test_declaration_is_strict(self) -> None:
        assert declare_origin("https://example.com:443/x") == BrowserOrigin(
            scheme="https", host="example.com", port=443
        )
        with pytest.raises(ValueError):
            declare_origin("file:///etc/passwd")
        with pytest.raises(ValueError):
            declare_policy(owner_id=OWNER, origin_urls=["https://example.com", "javascript:alert(1)"])

    def test_declared_policy_is_immutable_and_hashable(self) -> None:
        policy = declare_policy(owner_id=OWNER, origin_urls=[HOME, HOME])
        assert policy.allowed_origins == frozenset({HOME_ORIGIN})
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.owner_id = "other"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            HOME_ORIGIN.host = "evil.example"  # type: ignore[misc]


class TestClassificationCoverage:
    def test_every_action_type_is_classified(self) -> None:
        unclassified = [member for member in ActionType if classify_action_type(member) is None]
        assert unclassified == []

    def test_every_action_type_has_a_concrete_model_under_test(self) -> None:
        assert set(CONCRETE_ACTIONS) == set(ActionType)

    def test_every_concrete_model_projects_without_defects(self) -> None:
        for action_type, action in CONCRETE_ACTIONS.items():
            projection = project_for(action)
            assert projection.defects == (), f"{action_type} projected with defects {projection.defects}"
            assert projection.action_type is action_type
            assert projection.action_class is classify_action_type(action_type)

    def test_execute_js_is_unsupported(self) -> None:
        assert classify_action_type(ActionType.EXECUTE_JS) is ActionClass.UNSUPPORTED

    def test_coordinate_only_actions_are_unresolvable(self) -> None:
        for action_type in (ActionType.DRAG, ActionType.LEFT_MOUSE):
            assert classify_action_type(action_type) is ActionClass.UNRESOLVABLE
            assert project_for(CONCRETE_ACTIONS[action_type]).target.resolvable is False

    def test_unknown_action_type_fails_closed(self) -> None:
        assert classify_action_type("teleport") is None
        rogue = NullAction()
        rogue.action_type = "teleport"  # type: ignore[assignment]
        projection = project_for(rogue)
        assert PolicyReason.UNKNOWN_ACTION in projection.defects
        assert projection.action_class is None

    def test_model_discriminator_mismatch_fails_closed(self) -> None:
        # A0 finding F1: action_type is a defaulted field, not a Literal discriminator, so this
        # mismatched object constructs cleanly.
        mismatched = ClickAction(element_id="1", action_type=ActionType.GOTO_URL)
        assert mismatched.action_type is ActionType.GOTO_URL
        projection = project_for(mismatched)
        assert PolicyReason.ACTION_MODEL_MISMATCH in projection.defects

        decision = decide_browser_action(build_request(mismatched))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.ACTION_MODEL_MISMATCH in decision.reasons

    def test_complete_to_terminate_rewrite_is_re_projected_not_memoized(self) -> None:
        # A0 finding F4: handler.py rewrites action.action_type = TERMINATE on a CompleteAction.
        action = CompleteAction()
        assert project_for(action).defects == ()
        action.action_type = ActionType.TERMINATE
        assert PolicyReason.ACTION_MODEL_MISMATCH in project_for(action).defects
        assert decide_browser_action(build_request(action)).outcome is PolicyOutcome.DENIED

        replacement = TerminateAction()
        assert project_for(replacement).defects == ()
        assert decide_browser_action(build_request(replacement)).outcome is PolicyOutcome.ALLOWED

    def test_element_identity_is_not_a_policy_fact(self) -> None:
        # `_retarget_disabled_element_for_click` swaps the element a click lands on without touching
        # `action.element_id`, and SKY-12874 deliberately does not re-project there because the
        # projection cannot change. That reasoning holds only while element identity stays out of
        # the projection. The day it goes in, this fails and names the site that needs a call.
        from skyvern.forge.sdk.browser_action_policy import project_action

        assert project_action(ClickAction(element_id="1")) == project_action(ClickAction(element_id="2"))

    def test_navigation_targets_are_typed_page_targets(self) -> None:
        page_home = ResolvedTarget(kind=TargetKind.PAGE, url=HOME)
        assert project_for(GotoUrlAction(url=HOME)).target.resolved == (page_home,)
        assert project_for(NewTabAction(url=HOME)).target.resolved == (page_home,)
        assert project_for(DownloadFileAction(file_name="f", download_url=HOME)).target.resolved == (page_home,)
        assert project_for(ClickAction(element_id="1")).target.resolved == ()
        assert project_for(UploadFileAction(element_id="1", file_url=HOME)).target.resolved == (page_home,)
        assert project_for(ClickAction(element_id="1", file_url=HOME)).target.resolved == (page_home,)

    def test_fetchable_file_urls_are_origin_checked(self) -> None:
        decision = decide_browser_action(
            build_request(
                UploadFileAction(element_id="1", file_url="https://evil.example/x.pdf"),
                protected_references=(file_ref(),),
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED in decision.reasons

    def test_protected_reference_requirements_are_declared(self) -> None:
        assert project_for(
            InputTextAction(element_id="1", text="x", totp_code_required=True)
        ).required_references == frozenset({ProtectedReferenceKind.SECRET})
        assert project_for(VerificationCodeAction(verification_code="1")).required_references == frozenset(
            {ProtectedReferenceKind.VERIFICATION_CODE}
        )
        assert project_for(
            UploadFileAction(element_id="1", file_url="https://example.com/f.pdf")
        ).required_references == frozenset({ProtectedReferenceKind.FILE})
        assert project_for(InputTextAction(element_id="1", text="x")).required_references == frozenset()


class TestUnclassifiableProjections:
    def test_a_hand_built_unclassified_projection_is_denied(self) -> None:
        # ActionProjection is public API and `defects` defaults to (), so downstream tickets can
        # construct one directly. Unclassifiable must deny, not fall through to the recovery set.
        projection = ActionProjection(
            action_type=ActionType.GOTO_URL,
            action_class=None,
            target=ActionTarget(resolved=(ResolvedTarget(kind=TargetKind.PAGE, url="https://totally-evil.example/"),)),
        )
        decision = decide_browser_action(
            BrowserActionRequest(
                policy=POLICY,
                projection=projection,
                authority=FULL_AUTHORITY,
                request_epoch=EPOCH,
                evidence=PageObservation(
                    page_url="https://also-evil.example/",
                    observation_epoch=EPOCH,
                    verdict=ObservationVerdict.SUSPICIOUS,
                ),
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.UNKNOWN_ACTION in decision.reasons
        assert PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED in decision.reasons
        assert PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED in decision.reasons
        assert PolicyReason.UNTRUSTED_OBSERVATION in decision.reasons

    def test_an_action_type_missing_from_the_tables_fails_closed_without_crashing(self) -> None:
        rogue = NullAction()
        rogue.action_type = "teleport"  # type: ignore[assignment]
        assert project_for(rogue).defects == (PolicyReason.UNKNOWN_ACTION,)

    def test_an_unmapped_action_type_degrades_instead_of_crashing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulates a future ActionType member that nobody added to the tables. A crash inside the
        # policy core would make observe mode behavioural, which is the one thing it must never be.
        import skyvern.forge.sdk.browser_action_policy as module

        monkeypatch.delitem(module._ACTION_MODELS, ActionType.CLICK)
        monkeypatch.delitem(module._ACTION_CLASSES, ActionType.CLICK)
        projection = project_for(ClickAction(element_id="1"))
        assert projection.defects == (PolicyReason.UNKNOWN_ACTION,)
        assert decide_browser_action(build_request(ClickAction(element_id="1"))).outcome is PolicyOutcome.DENIED

    def test_protected_reference_kind_is_type_checked(self) -> None:
        # `kind in ProtectedReferenceKind` raises TypeError for a plain string on 3.11.
        assert ProtectedReference(kind="secret", reference_id="r", owner_id=OWNER).complete is False  # type: ignore[arg-type]
        assert ProtectedReference(kind=ProtectedReferenceKind.SECRET, reference_id="r", owner_id=OWNER).complete


class TestEnrollmentBoundary:
    def test_policy_none_is_the_exclusive_not_enrolled_result(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), policy=None))
        assert decision.outcome is PolicyOutcome.NOT_ENROLLED
        assert decision.reasons == ()

    def test_not_enrolled_survives_every_other_defect(self) -> None:
        decision = decide_browser_action(
            build_request(
                ExecuteJsAction(js_code="1"),
                policy=None,
                page_url="javascript:alert(1)",
                evidence_epoch=EPOCH - 3,
                verdict=ObservationVerdict.SUSPICIOUS,
            )
        )
        assert decision.outcome is PolicyOutcome.NOT_ENROLLED
        assert decision.reasons == ()

    def test_no_enrolled_request_can_yield_not_enrolled(self) -> None:
        outcomes = set()
        for action in CONCRETE_ACTIONS.values():
            for verdict in ObservationVerdict:
                for page_url in (HOME, "https://evil.example/"):
                    for epoch in (EPOCH, EPOCH - 1):
                        decision = decide_browser_action(
                            build_request(
                                action,
                                page_url=page_url,
                                evidence_epoch=epoch,
                                verdict=verdict,
                                protected_references=refs_for(action),
                            )
                        )
                        outcomes.add(decision.outcome)
        assert PolicyOutcome.NOT_ENROLLED not in outcomes


class TestEvidenceRequirements:
    def test_missing_evidence_denies(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), omit_evidence=True))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons

    def test_stale_epoch_denies(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), evidence_epoch=EPOCH - 1))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.STALE_PAGE_EVIDENCE in decision.reasons

    def test_future_epoch_denies(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), evidence_epoch=EPOCH + 1))
        assert PolicyReason.STALE_PAGE_EVIDENCE in decision.reasons

    def test_unparseable_page_url_denies(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), page_url="about:blank"))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_PAGE_ORIGIN in decision.reasons

    def test_benign_actions_still_require_fresh_evidence(self) -> None:
        decision = decide_browser_action(build_request(WaitAction(), evidence_epoch=EPOCH - 1))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.STALE_PAGE_EVIDENCE in decision.reasons


class TestProtectedReferences:
    def test_missing_required_reference_denies(self) -> None:
        action = VerificationCodeAction(verification_code="123456")
        decision = decide_browser_action(build_request(action, protected_references=()))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_PROTECTED_REFERENCE in decision.reasons

    @pytest.mark.parametrize("blank_field", ["reference_id", "owner_id"])
    def test_incomplete_reference_facts_deny(self, blank_field: str) -> None:
        fields = {"kind": ProtectedReferenceKind.VERIFICATION_CODE, "reference_id": "ref", "owner_id": OWNER}
        fields[blank_field] = ""
        reference = ProtectedReference(**fields)  # type: ignore[arg-type]
        assert reference.complete is False
        decision = decide_browser_action(
            build_request(VerificationCodeAction(verification_code="1"), protected_references=(reference,))
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.INCOMPLETE_PROTECTED_REFERENCE in decision.reasons

    def test_reference_owned_by_another_run_denies(self) -> None:
        decision = decide_browser_action(
            build_request(
                VerificationCodeAction(verification_code="1"),
                protected_references=(code_ref(owner_id="wr_other"),),
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.UNOWNED_PROTECTED_REFERENCE in decision.reasons

    def test_complete_owned_reference_opens_the_reference_gate(self) -> None:
        # No mutating element action can reach ALLOWED any more — main-world facts never establish
        # completeness — so the twin is restated as exact reasons: with the reference satisfied,
        # the ONLY remaining denial is the incomplete destination. Reference reasons absent proves
        # the gate opened.
        form_home = ElementDestination(kind=TargetKind.FORM, url=HOME, method="post")
        decision = decide_browser_action(
            build_request(
                InputTextAction(element_id="1", text="x", totp_code_required=True),
                protected_references=(secret_ref(),),
                destination=form_home,
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.INCOMPLETE_DESTINATION,)

    def test_verification_code_entry_is_destination_opaque(self) -> None:
        # Element-less mutating actions name no control, so no destination fact can ever complete
        # them. The reference being satisfied does not make the destination known.
        decision = decide_browser_action(
            build_request(VerificationCodeAction(verification_code="1"), protected_references=(code_ref(),))
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.INCOMPLETE_DESTINATION,)


class TestOriginAuthorization:
    def test_unauthorized_page_origin_denies(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), page_url="https://evil.example/"))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED in decision.reasons

    def test_unauthorized_target_origin_denies(self) -> None:
        decision = decide_browser_action(build_request(GotoUrlAction(url="https://evil.example/")))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED in decision.reasons

    def test_unparseable_target_denies(self) -> None:
        decision = decide_browser_action(build_request(GotoUrlAction(url="javascript:alert(1)")))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_TARGET_ORIGIN in decision.reasons

    def test_authorized_navigation_allows(self) -> None:
        assert decide_browser_action(build_request(GotoUrlAction(url=HOME))).outcome is PolicyOutcome.ALLOWED

    def test_benign_action_survives_an_unauthorized_origin(self) -> None:
        # The recovery set must let the agent get back to authorized ground.
        for action in (WaitAction(), ScrollAction(), GoBackAction(), TerminateAction()):
            decision = decide_browser_action(build_request(action, page_url="https://evil.example/"))
            assert decision.outcome is PolicyOutcome.ALLOWED, action.action_type

    def test_mutating_action_is_gated_on_an_unauthorized_origin(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), page_url="https://evil.example/"))
        assert decision.outcome is PolicyOutcome.DENIED


class TestRuntimeAuthority:
    """The enrolled policy is a ceiling, not the complete authority (ADR-0011).

    Every test here pins verdict=NO_MATCH and puts the page origin *inside* POLICY.allowed_origins,
    so the only thing that can deny is the authority gate. Without those two pins a request denies
    anyway on UNVERIFIED_OBSERVATION and the assertions would hold against any implementation.
    """

    # GOTO_URL is the axis-isolation vehicle here: its target is model-declared (genuinely known,
    # so it completes) — a hydrated click can no longer reach ALLOWED at all, which would make
    # every deny below prove nothing about the authority gate.

    def test_unwired_authority_denies_an_action_inside_the_ceiling(self) -> None:
        decision = decide_browser_action(build_request(GotoUrlAction(url=HOME), authority=UNWIRED_AUTHORITY))
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.UNWIRED_RUNTIME_AUTHORITY,)

    def test_established_authority_over_the_same_origin_allows(self) -> None:
        # The twin of the test above. If this one fails, the deny above proves nothing: it would be
        # denying for some reason other than the authority gate.
        decision = decide_browser_action(build_request(GotoUrlAction(url=HOME), authority=FULL_AUTHORITY))
        assert decision.outcome is PolicyOutcome.ALLOWED
        assert decision.reasons == ()

    def test_missing_authority_denies(self) -> None:
        authority = RuntimeOriginAuthority(state=AuthorityState.MISSING, origins=POLICY.allowed_origins)
        decision = decide_browser_action(build_request(GotoUrlAction(url=HOME), authority=authority))
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.MISSING_RUNTIME_AUTHORITY,)

    def test_invalidated_authority_denies_even_carrying_the_right_origins(self) -> None:
        # ADR-0011: rotation or conflict after a browser context is bound is permanent, and the
        # origins it used to carry do not buy it back.
        authority = RuntimeOriginAuthority(state=AuthorityState.INVALIDATED, origins=POLICY.allowed_origins)
        decision = decide_browser_action(build_request(GotoUrlAction(url=HOME), authority=authority))
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.INVALIDATED_RUNTIME_AUTHORITY,)

    def test_established_authority_narrower_than_the_ceiling_denies_outside_itself(self) -> None:
        other = "https://other.example/"
        wide = declare_policy(owner_id=OWNER, origin_urls=[HOME, other])
        narrow = RuntimeOriginAuthority(state=AuthorityState.ESTABLISHED, origins=frozenset({declare_origin(other)}))
        decision = decide_browser_action(
            # The target points inside the granted set, so the page origin is the only reason.
            build_request(GotoUrlAction(url=other), policy=wide, page_url=HOME, authority=narrow)
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED,)

    def test_authority_cannot_widen_the_ceiling(self) -> None:
        outside = "https://outside.example/"
        authority = RuntimeOriginAuthority(
            state=AuthorityState.ESTABLISHED,
            origins=frozenset({HOME_ORIGIN, declare_origin(outside)}),
        )
        decision = decide_browser_action(build_request(GotoUrlAction(url=HOME), page_url=outside, authority=authority))
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED,)

    def test_navigation_target_is_checked_against_the_intersection(self) -> None:
        wide = declare_policy(owner_id=OWNER, origin_urls=[HOME, "https://other.example/"])
        narrow = RuntimeOriginAuthority(state=AuthorityState.ESTABLISHED, origins=frozenset({HOME_ORIGIN}))
        decision = decide_browser_action(
            build_request(GotoUrlAction(url="https://other.example/next"), policy=wide, authority=narrow)
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED,)

    def test_recovery_actions_survive_an_unwired_authority(self) -> None:
        # ADR-0011 keeps read-only and recovery actions available so a run can stop safely. If the
        # authority gate ever swallows these, a protected run has no way out.
        for action in (WaitAction(), ScrollAction(), GoBackAction(), ExtractAction(), TerminateAction()):
            decision = decide_browser_action(build_request(action, authority=UNWIRED_AUTHORITY))
            assert decision.outcome is PolicyOutcome.ALLOWED, action.action_type

    def test_an_unenrolled_run_is_still_not_enrolled(self) -> None:
        decision = decide_browser_action(
            build_request(ClickAction(element_id="1"), policy=None, authority=UNWIRED_AUTHORITY)
        )
        assert decision.outcome is PolicyOutcome.NOT_ENROLLED

    def test_authority_is_required_with_no_default(self) -> None:
        # A default would let a caller reach the absent state by omission. It has to be named.
        with pytest.raises(TypeError):
            BrowserActionRequest(  # type: ignore[call-arg]
                policy=POLICY,
                projection=project_for(ClickAction(element_id="1")),
                request_epoch=EPOCH,
            )

    def test_unwired_is_a_state_of_its_own(self) -> None:
        # Collapsing unwired into missing is what would hide the empty seam, in source and in logs.
        assert UNWIRED_AUTHORITY.state is AuthorityState.UNWIRED
        assert UNWIRED_AUTHORITY.state is not AuthorityState.MISSING
        assert UNWIRED_AUTHORITY.origins == frozenset()


class TestObservationGating:
    def test_suspicious_observation_denies_a_mutating_action(self) -> None:
        decision = decide_browser_action(
            build_request(ClickAction(element_id="1"), verdict=ObservationVerdict.SUSPICIOUS)
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.UNTRUSTED_OBSERVATION in decision.reasons

    def test_unknown_observation_denies_a_mutating_action(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), verdict=ObservationVerdict.UNKNOWN))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.UNVERIFIED_OBSERVATION in decision.reasons

    def test_extract_respects_a_suspicious_verdict(self) -> None:
        # EXTRACT is the channel that pulls untrusted page content into the agent context, which is
        # the very attack SKY-12526 is about. Recovery does not mean reading a hostile page.
        assert classify_action_type(ActionType.EXTRACT) is ActionClass.READ
        decision = decide_browser_action(build_request(ExtractAction(), verdict=ObservationVerdict.SUSPICIOUS))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.UNTRUSTED_OBSERVATION in decision.reasons

    def test_extract_still_works_on_an_unauthorized_origin(self) -> None:
        # Origin and verdict are separate axes: EXTRACT stays available for recovery.
        decision = decide_browser_action(build_request(ExtractAction(), page_url="https://evil.example/"))
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_suspicious_observation_does_not_block_recovery(self) -> None:
        decision = decide_browser_action(build_request(GoBackAction(), verdict=ObservationVerdict.SUSPICIOUS))
        assert decision.outcome is PolicyOutcome.ALLOWED

    def test_raw_string_verdict_is_coerced_fail_closed(self) -> None:
        evidence = PageObservation(page_url=HOME, observation_epoch=EPOCH, verdict=ObservationVerdict.NO_MATCH)
        object.__setattr__(evidence, "verdict", "totally-clean")
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), evidence=evidence))
        assert PolicyReason.UNVERIFIED_OBSERVATION in decision.reasons


class TestUnsupportedActions:
    def test_execute_js_is_unsupported_not_merely_denied(self) -> None:
        decision = decide_browser_action(build_request(ExecuteJsAction(js_code="1")))
        assert decision.outcome is PolicyOutcome.UNSUPPORTED
        assert PolicyReason.UNSUPPORTED_ACTION in decision.reasons

    def test_execute_js_stays_unsupported_on_an_authorized_clean_page(self) -> None:
        decision = decide_browser_action(
            build_request(ExecuteJsAction(js_code="1"), verdict=ObservationVerdict.NO_MATCH)
        )
        assert decision.outcome is PolicyOutcome.UNSUPPORTED

    def test_unresolvable_actions_deny(self) -> None:
        for action in (DragAction(), LeftMouseAction(direction="down")):
            decision = decide_browser_action(build_request(action))
            assert decision.outcome is PolicyOutcome.DENIED, action.action_type
            assert PolicyReason.UNRESOLVABLE_TARGET in decision.reasons


class TestReasonPrecedence:
    def test_precedence_covers_every_reason_exactly_once(self) -> None:
        assert list(REASON_PRECEDENCE) == sorted(REASON_PRECEDENCE, key=REASON_PRECEDENCE.index)
        assert set(REASON_PRECEDENCE) == set(PolicyReason)
        assert len(REASON_PRECEDENCE) == len(set(REASON_PRECEDENCE)) == len(PolicyReason)

    def test_all_applicable_reasons_are_returned_in_precedence_order(self) -> None:
        decision = decide_browser_action(
            build_request(
                GotoUrlAction(url="https://evil.example/"),
                page_url="https://also-evil.example/",
                evidence_epoch=EPOCH - 2,
                verdict=ObservationVerdict.SUSPICIOUS,
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        expected = {
            PolicyReason.STALE_PAGE_EVIDENCE,
            PolicyReason.UNTRUSTED_OBSERVATION,
            PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED,
            PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED,
        }
        assert set(decision.reasons) == expected
        assert list(decision.reasons) == sorted(decision.reasons, key=REASON_PRECEDENCE.index)

    def test_repeated_reasons_collapse_to_one(self) -> None:
        # Two unauthorized targets raise the same reason twice; the decision must report it once.
        projection = ActionProjection(
            action_type=ActionType.GOTO_URL,
            action_class=ActionClass.NAVIGATION,
            target=ActionTarget(
                resolved=(
                    ResolvedTarget(kind=TargetKind.PAGE, url="https://evil.example/a"),
                    ResolvedTarget(kind=TargetKind.PAGE, url="https://evil.example/b"),
                )
            ),
        )
        decision = decide_browser_action(
            BrowserActionRequest(
                policy=POLICY,
                projection=projection,
                authority=FULL_AUTHORITY,
                request_epoch=EPOCH,
                evidence=PageObservation(page_url=HOME, observation_epoch=EPOCH, verdict=ObservationVerdict.NO_MATCH),
            )
        )
        assert decision.reasons == (PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED,)

    def test_reason_codes_are_stable_strings(self) -> None:
        assert {member.value for member in PolicyReason} == {
            "unsupported_action",
            "unknown_action",
            "action_model_mismatch",
            "unresolvable_target",
            "incomplete_destination",
            "element_hash_mismatch",
            "missing_page_evidence",
            "stale_page_evidence",
            "missing_protected_reference",
            "incomplete_protected_reference",
            "unowned_protected_reference",
            "untrusted_observation",
            "unverified_observation",
            "unwired_runtime_authority",
            "missing_runtime_authority",
            "invalidated_runtime_authority",
            "missing_page_origin",
            "page_origin_not_authorized",
            "missing_target_origin",
            "target_origin_not_authorized",
        }


class TestDestinationCompleteness:
    """SKY-12875 AC6: a destination-opaque control is INCOMPLETE, never implicitly safe. The
    completeness default at projection time is therefore False for every mutating action — an
    unhydrated projection must read as unknown-destination, not as no-destination-to-check."""

    def test_element_targeted_mutating_actions_start_incomplete(self) -> None:
        for action in (
            ClickAction(element_id="1"),
            InputTextAction(element_id="1", text="x"),
            SelectOptionAction(element_id="1", option=SelectOption(label="a")),
            CheckboxAction(element_id="1", is_checked=True),
            UploadFileAction(element_id="1", file_url=HOME),
        ):
            assert project_for(action).target.complete is False, action.action_type

    def test_element_less_mutating_actions_are_permanently_incomplete(self) -> None:
        # No control named means no destination fact can ever complete them: KEYPRESS can submit a
        # form, SOLVE_CAPTCHA and VERIFICATION_CODE type into elements the projection cannot see.
        for action in (
            KeypressAction(keys=["Enter"]),
            SolveCaptchaAction(),
            VerificationCodeAction(verification_code="1"),
        ):
            assert project_for(action).target.complete is False, action.action_type

    def test_navigation_with_an_explicit_url_is_complete(self) -> None:
        assert project_for(GotoUrlAction(url=HOME)).target.complete is True
        assert project_for(NewTabAction(url=HOME)).target.complete is True

    def test_reload_targets_the_current_page_and_is_complete(self) -> None:
        # The destination is exactly the page origin, which the page-origin gate already checks.
        assert project_for(ReloadPageAction()).target.complete is True

    def test_go_forward_is_incomplete(self) -> None:
        # The history destination is unknowable before execution.
        assert project_for(GoForwardAction()).target.complete is False

    def test_byte_carrying_download_is_incomplete(self) -> None:
        # SKY-12874 called confining a byte download the sink's job; under the opaque-never-safe
        # rule the honest preflight classification is incomplete. With a URL it is a PAGE target.
        assert project_for(DownloadFileAction(file_name="f")).target.complete is False
        assert project_for(DownloadFileAction(file_name="f", download_url=HOME)).target.complete is True

    def test_recovery_classes_are_not_destination_gated(self) -> None:
        for action in (WaitAction(), ScrollAction(), GoBackAction(), ExtractAction(), TerminateAction()):
            assert project_for(action).target.complete is True, action.action_type

    def test_incomplete_destination_denies_gated_classes_only(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1")))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.INCOMPLETE_DESTINATION in decision.reasons

        benign = ActionProjection(
            action_type=ActionType.WAIT,
            action_class=ActionClass.BENIGN,
            target=ActionTarget(complete=False),
        )
        decision = decide_browser_action(
            BrowserActionRequest(
                policy=POLICY,
                projection=benign,
                authority=FULL_AUTHORITY,
                request_epoch=EPOCH,
                evidence=PageObservation(page_url=HOME, observation_epoch=EPOCH, verdict=ObservationVerdict.NO_MATCH),
            )
        )
        assert PolicyReason.INCOMPLETE_DESTINATION not in decision.reasons

    def test_go_forward_reports_incomplete_destination(self) -> None:
        decision = decide_browser_action(build_request(GoForwardAction()))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.INCOMPLETE_DESTINATION in decision.reasons


class TestDestinationHydration:
    """SKY-12875: scrape-time DOM facts become typed resolved targets. The facts are stale,
    untrusted preflight input — they can complete a destination or flag an identity mismatch, and
    they can never substitute for evidence, verdict or authority."""

    def test_anchor_facts_resolve_a_typed_anchor_target_without_completing(self) -> None:
        projection = hydrate_destination(
            project_for(ClickAction(element_id="1")),
            claimed_element_hash=None,
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=ANCHOR_HOME),
        )
        assert projection.target.resolved == (ResolvedTarget(kind=TargetKind.ANCHOR, url=HOME),)
        # A main-world-sourced fact never establishes completeness: it names where the page CLAIMS
        # the click goes, not where native activation goes.
        assert projection.target.complete is False
        assert projection.defects == ()

    def test_form_facts_resolve_a_typed_form_target_without_completing(self) -> None:
        form = ElementDestination(kind=TargetKind.FORM, url="https://example.com/submit", method="post")
        projection = hydrate_destination(
            project_for(InputTextAction(element_id="1", text="x")),
            claimed_element_hash=None,
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=form),
        )
        assert projection.target.resolved == (
            ResolvedTarget(kind=TargetKind.FORM, url="https://example.com/submit", method="post"),
        )
        assert projection.target.complete is False

    def test_hydration_appends_to_model_derived_targets(self) -> None:
        # A click that fetches a file AND sits on an anchor carries both destinations.
        projection = hydrate_destination(
            project_for(ClickAction(element_id="1", file_url=HOME)),
            claimed_element_hash=None,
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=ANCHOR_HOME),
        )
        assert ResolvedTarget(kind=TargetKind.PAGE, url=HOME) in projection.target.resolved
        assert ResolvedTarget(kind=TargetKind.ANCHOR, url=HOME) in projection.target.resolved

    def test_an_unobserved_element_stays_incomplete(self) -> None:
        projection = hydrate_destination(
            project_for(ClickAction(element_id="1")),
            claimed_element_hash=None,
            observed=None,
        )
        assert projection.target.complete is False
        assert projection.target.resolved == ()

    def test_an_opaque_destination_stays_incomplete(self) -> None:
        # The element was observed and bears no destination-carrying structure: a plain button, a
        # div with a JS handler. Observed does not mean safe.
        for destination in (None, ElementDestination(kind=TargetKind.ANCHOR, url=None)):
            projection = hydrate_destination(
                project_for(ClickAction(element_id="1")),
                claimed_element_hash=None,
                observed=ObservedElement(element_hash=OBSERVED_HASH, destination=destination),
            )
            assert projection.target.complete is False
            assert projection.target.resolved == ()

    def test_a_hash_mismatch_is_a_defect_and_discards_the_facts(self) -> None:
        # The action claims one element identity, the observation recorded another. The facts keyed
        # by that id describe an element the action was not planned against.
        projection = hydrate_destination(
            project_for(ClickAction(element_id="1")),
            claimed_element_hash="stale-hash-from-a-previous-run",
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=ANCHOR_HOME),
        )
        assert PolicyReason.ELEMENT_HASH_MISMATCH in projection.defects
        assert projection.target.complete is False
        assert projection.target.resolved == ()

        decision = decide_browser_action(
            BrowserActionRequest(
                policy=POLICY,
                projection=projection,
                authority=FULL_AUTHORITY,
                request_epoch=EPOCH,
                evidence=PageObservation(page_url=HOME, observation_epoch=EPOCH, verdict=ObservationVerdict.NO_MATCH),
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.ELEMENT_HASH_MISMATCH in decision.reasons

    def test_a_matching_claimed_hash_hydrates(self) -> None:
        projection = hydrate_destination(
            project_for(ClickAction(element_id="1")),
            claimed_element_hash=OBSERVED_HASH,
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=ANCHOR_HOME),
        )
        assert projection.defects == ()
        assert projection.target.resolved != ()

    def test_a_defective_projection_is_never_hydrated(self) -> None:
        # The element_id that keyed the lookup came from a model whose field layout cannot be
        # trusted, so the facts it found must not be attached.
        mismatched = ClickAction(element_id="1", action_type=ActionType.GOTO_URL)
        projection = hydrate_destination(
            project_for(mismatched),
            claimed_element_hash=None,
            observed=ObservedElement(element_hash=OBSERVED_HASH, destination=ANCHOR_HOME),
        )
        assert projection.target.resolved == ()
        assert PolicyReason.ACTION_MODEL_MISMATCH in projection.defects

    def test_a_cross_origin_form_is_denied_on_its_target_origin(self) -> None:
        exfil = ElementDestination(kind=TargetKind.FORM, url="https://collector.example/steal", method="post")
        decision = decide_browser_action(build_request(InputTextAction(element_id="1", text="x"), destination=exfil))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED in decision.reasons

    def test_a_same_origin_form_passes_the_target_gate_but_never_allows(self) -> None:
        form = ElementDestination(kind=TargetKind.FORM, url=HOME, method="post")
        decision = decide_browser_action(build_request(InputTextAction(element_id="1", text="x"), destination=form))
        assert PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED not in decision.reasons
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.INCOMPLETE_DESTINATION in decision.reasons

    def test_a_forged_safe_fact_can_never_produce_allowed(self) -> None:
        # The gate falsification, kept as a test: a page that hides ping= from a patched main-world
        # getAttribute yields a perfectly safe-looking fact. With established authority, a clean
        # verdict and fresh evidence, the decision must still be DENIED — a main-world-sourced
        # fact must never be what establishes completeness.
        decision = decide_browser_action(
            build_request(
                ClickAction(element_id="1"),
                destination=ANCHOR_HOME,
                authority=FULL_AUTHORITY,
                verdict=ObservationVerdict.NO_MATCH,
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert decision.reasons == (PolicyReason.INCOMPLETE_DESTINATION,)

    def test_a_malformed_resolved_url_denies_as_missing_target_origin(self) -> None:
        # javascript: resolves to a syntactically valid URL with no usable origin.
        weird = ElementDestination(kind=TargetKind.ANCHOR, url="javascript:void(0)")
        decision = decide_browser_action(build_request(ClickAction(element_id="1"), destination=weird))
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_TARGET_ORIGIN in decision.reasons

    def test_facts_are_never_authorization(self) -> None:
        # The AC stated as an executable sentence: perfect destination facts change nothing about
        # evidence, verdict or authority. Metadata is preflight input, not final authorization.
        decision = decide_browser_action(
            build_request(
                ClickAction(element_id="1"),
                destination=ANCHOR_HOME,
                authority=UNWIRED_AUTHORITY,
                verdict=ObservationVerdict.UNKNOWN,
                omit_evidence=True,
            )
        )
        assert decision.outcome is PolicyOutcome.DENIED
        assert PolicyReason.MISSING_PAGE_EVIDENCE in decision.reasons
        assert PolicyReason.UNVERIFIED_OBSERVATION in decision.reasons
        assert PolicyReason.UNWIRED_RUNTIME_AUTHORITY in decision.reasons

    def test_tab_resolution_attaches_a_typed_tab_target(self) -> None:
        projection = with_resolved_target(
            project_for(SwitchTabAction(tab_index=1)),
            ResolvedTarget(kind=TargetKind.TAB, url="https://example.com/other-tab"),
        )
        assert projection.target.resolved == (ResolvedTarget(kind=TargetKind.TAB, url="https://example.com/other-tab"),)
        assert projection.target.complete is True

    def test_an_unresolvable_tab_stays_incomplete(self) -> None:
        projection = with_resolved_target(project_for(SwitchTabAction(tab_index=9)), None)
        assert projection.target.resolved == ()
        assert projection.target.complete is False

    def test_a_defective_projection_never_gains_a_runtime_target(self) -> None:
        # The twin of hydrate_destination's defect guard: a model whose field layout the core just
        # refused to trust must not be handed a target either, let alone marked complete.
        mismatched = SwitchTabAction(tab_index=1, action_type=ActionType.CLICK)
        projection = with_resolved_target(
            project_for(mismatched),
            ResolvedTarget(kind=TargetKind.TAB, url="https://evil.example/x"),
        )
        assert projection.target.resolved == ()
        assert projection.target.complete is False
        assert PolicyReason.ACTION_MODEL_MISMATCH in projection.defects

    def test_a_defective_projection_reads_incomplete_not_complete(self) -> None:
        # An unknowable destination must not read "complete" just because the defect reasons
        # already deny; honesty of the record outlives today's reason set.
        rogue = NullAction()
        rogue.action_type = "teleport"  # type: ignore[assignment]
        assert project_for(rogue).target.complete is False
        assert project_for(ClickAction(element_id="1", action_type=ActionType.GOTO_URL)).target.complete is False


class TestHappyPath:
    def test_clean_authorized_navigation_allows(self) -> None:
        # The only ALLOWED path for a destination-bearing action is a model-declared URL; a
        # hydrated mutating action can never reach ALLOWED (see the forged-safe-fact test).
        decision = decide_browser_action(build_request(GotoUrlAction(url=HOME)))
        assert decision == PolicyDecision(outcome=PolicyOutcome.ALLOWED, reasons=())

    def test_decisions_are_immutable(self) -> None:
        decision = decide_browser_action(build_request(ClickAction(element_id="1")))
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.outcome = PolicyOutcome.DENIED  # type: ignore[misc]

    def test_deciding_twice_yields_an_equal_decision(self) -> None:
        request = build_request(ClickAction(element_id="1"), destination=ANCHOR_HOME)
        assert decide_browser_action(request) == decide_browser_action(request)


class TestPurity:
    MODULE = Path(__file__).resolve().parents[2] / "skyvern" / "forge" / "sdk" / "browser_action_policy.py"

    def parsed(self) -> ast.Module:
        return ast.parse(self.MODULE.read_text())

    def test_module_declares_no_async_work(self) -> None:
        offenders = [node.name for node in ast.walk(self.parsed()) if isinstance(node, ast.AsyncFunctionDef)]
        assert offenders == []
        assert not [node for node in ast.walk(self.parsed()) if isinstance(node, (ast.Await, ast.AsyncWith))]

    def test_module_imports_nothing_impure(self) -> None:
        forbidden = {
            "asyncio",
            "cloud",
            "httpx",
            "requests",
            "aiohttp",
            "socket",
            "subprocess",
            "os",
            "pathlib",
            "skyvern.config",
            "skyvern.forge.sdk.core.skyvern_context",
        }
        imported: set[str] = set()
        for node in ast.walk(self.parsed()):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert imported & forbidden == set()
        assert not any(name.split(".")[0] == "cloud" for name in imported)

    def test_module_does_no_global_context_lookup(self) -> None:
        source = self.MODULE.read_text()
        for needle in ("skyvern_context", "contextvars", "ContextVar", "app.AGENT_FUNCTION", "open("):
            assert needle not in source, needle

    def test_every_public_dataclass_is_frozen(self) -> None:
        for obj in (
            BrowserOrigin,
            ProtectedReference,
            ActionTarget,
            ActionProjection,
            PageObservation,
            BrowserActionPolicy,
            BrowserActionRequest,
            PolicyDecision,
            ResolvedTarget,
            ElementDestination,
            ObservedElement,
        ):
            assert dataclasses.is_dataclass(obj)
            assert obj.__dataclass_params__.frozen is True, obj.__name__


class TestConfiguration:
    def test_mode_defaults_to_disabled(self) -> None:
        assert Settings().BROWSER_ACTION_POLICY_MODE == "disabled"

    def test_observe_is_accepted(self) -> None:
        assert Settings(BROWSER_ACTION_POLICY_MODE="observe").BROWSER_ACTION_POLICY_MODE == "observe"

    @pytest.mark.parametrize("mode", ["enforce", "block", "ENFORCE", "on", ""])
    def test_enforce_is_rejected_by_configuration(self, mode: str) -> None:
        with pytest.raises(ValidationError):
            Settings(BROWSER_ACTION_POLICY_MODE=mode)
