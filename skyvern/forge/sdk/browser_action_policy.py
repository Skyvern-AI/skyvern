"""Pure decision core for the browser action firewall (SKY-12872).

Deterministic, side-effect free policy evaluation: callers hand in already-extracted facts and get
back an immutable decision. The module performs no I/O, no async work, no ambient-state lookup and
no classification of its own — a probabilistic verdict is evidence, never authority.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

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
    ReloadPageAction,
    ScrollAction,
    SelectOptionAction,
    SolveCaptchaAction,
    SwitchTabAction,
    TerminateAction,
    UploadFileAction,
    VerificationCodeAction,
    WaitAction,
)


class ObservationVerdict(StrEnum):
    """Detector evidence about the observed page.

    Member values are pinned to the cloud detector's DetectionOutcome, which the OSS boundary
    forbids importing here. Cloud passes its member straight in: StrEnum compares equal to its
    value, so the two vocabularies stay interchangeable without a dependency.
    """

    NO_MATCH = "no_match"
    SUSPICIOUS = "suspicious"
    UNKNOWN = "unknown"


class AuthorityState(StrEnum):
    """Availability of the run's ADR-0011 task-URL-derived origin authority.

    Separate from enrollment on purpose. Enrollment answers "is this run protected, and what did an
    operator authorize at most"; this answers "what may this run reach right now". Only ESTABLISHED
    grants anything, and the origins it grants are still intersected with the enrolled ceiling.
    """

    ESTABLISHED = "established"
    #: Never established, or lost before a browser context was bound. ADR-0011 blocks until it is.
    MISSING = "missing"
    #: Rotated or conflicted after a browser context was bound. ADR-0011 makes this permanent,
    #: because existing connections cannot be proven closed.
    INVALIDATED = "invalidated"
    #: No runtime authority source is wired into this build at all — see UNWIRED_AUTHORITY.
    UNWIRED = "unwired"


class ActionClass(StrEnum):
    BENIGN = "benign"
    READ = "read"
    TERMINAL = "terminal"
    MUTATING = "mutating"
    NAVIGATION = "navigation"
    EGRESS = "egress"
    UNRESOLVABLE = "unresolvable"
    UNSUPPORTED = "unsupported"


class ProtectedReferenceKind(StrEnum):
    SECRET = "secret"
    VERIFICATION_CODE = "verification_code"
    FILE = "file"


class PolicyOutcome(StrEnum):
    NOT_ENROLLED = "not_enrolled"
    ALLOWED = "allowed"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"


class PolicyReason(StrEnum):
    UNSUPPORTED_ACTION = "unsupported_action"
    UNKNOWN_ACTION = "unknown_action"
    ACTION_MODEL_MISMATCH = "action_model_mismatch"
    UNRESOLVABLE_TARGET = "unresolvable_target"
    MISSING_PAGE_EVIDENCE = "missing_page_evidence"
    STALE_PAGE_EVIDENCE = "stale_page_evidence"
    MISSING_PROTECTED_REFERENCE = "missing_protected_reference"
    INCOMPLETE_PROTECTED_REFERENCE = "incomplete_protected_reference"
    UNOWNED_PROTECTED_REFERENCE = "unowned_protected_reference"
    UNTRUSTED_OBSERVATION = "untrusted_observation"
    UNVERIFIED_OBSERVATION = "unverified_observation"
    UNWIRED_RUNTIME_AUTHORITY = "unwired_runtime_authority"
    MISSING_RUNTIME_AUTHORITY = "missing_runtime_authority"
    INVALIDATED_RUNTIME_AUTHORITY = "invalidated_runtime_authority"
    MISSING_PAGE_ORIGIN = "missing_page_origin"
    PAGE_ORIGIN_NOT_AUTHORIZED = "page_origin_not_authorized"
    MISSING_TARGET_ORIGIN = "missing_target_origin"
    TARGET_ORIGIN_NOT_AUTHORIZED = "target_origin_not_authorized"


# Documented precedence, most structural first: what the action *is*, then whether the evidence
# supporting it is usable, then whether the origins it touches were granted. Decisions report every
# applicable reason in this order so a caller can log the full picture and act on reasons[0].
REASON_PRECEDENCE: tuple[PolicyReason, ...] = (
    PolicyReason.UNSUPPORTED_ACTION,
    PolicyReason.UNKNOWN_ACTION,
    PolicyReason.ACTION_MODEL_MISMATCH,
    PolicyReason.UNRESOLVABLE_TARGET,
    PolicyReason.MISSING_PAGE_EVIDENCE,
    PolicyReason.STALE_PAGE_EVIDENCE,
    PolicyReason.MISSING_PROTECTED_REFERENCE,
    PolicyReason.INCOMPLETE_PROTECTED_REFERENCE,
    PolicyReason.UNOWNED_PROTECTED_REFERENCE,
    PolicyReason.UNTRUSTED_OBSERVATION,
    PolicyReason.UNVERIFIED_OBSERVATION,
    PolicyReason.UNWIRED_RUNTIME_AUTHORITY,
    PolicyReason.MISSING_RUNTIME_AUTHORITY,
    PolicyReason.INVALIDATED_RUNTIME_AUTHORITY,
    PolicyReason.MISSING_PAGE_ORIGIN,
    PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED,
    PolicyReason.MISSING_TARGET_ORIGIN,
    PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED,
)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_IPV4_LAST_LABEL = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}

# Origin and verdict are independent axes and must not share one exemption set.
# Recovery means letting a stranded agent navigate back from an unauthorized origin, so the recovery
# set is exempt from the origin check. It does not mean letting it READ a hostile page: EXTRACT
# pulls untrusted content into the agent's context, which is the exposure this control exists for,
# so it stays subject to the detector verdict.
_ORIGIN_UNGATED_CLASSES = frozenset({ActionClass.BENIGN, ActionClass.READ, ActionClass.TERMINAL})
_VERDICT_UNGATED_CLASSES = frozenset({ActionClass.BENIGN, ActionClass.TERMINAL})

_ACTION_CLASSES: dict[ActionType, ActionClass] = {
    ActionType.NULL_ACTION: ActionClass.BENIGN,
    ActionType.WAIT: ActionClass.BENIGN,
    ActionType.HOVER: ActionClass.BENIGN,
    ActionType.EXTRACT: ActionClass.READ,
    ActionType.SCROLL: ActionClass.BENIGN,
    ActionType.MOVE: ActionClass.BENIGN,
    ActionType.GO_BACK: ActionClass.BENIGN,
    ActionType.CLOSE_PAGE: ActionClass.BENIGN,
    ActionType.SWITCH_TAB: ActionClass.BENIGN,
    ActionType.TERMINATE: ActionClass.TERMINAL,
    ActionType.COMPLETE: ActionClass.TERMINAL,
    ActionType.CLICK: ActionClass.MUTATING,
    ActionType.INPUT_TEXT: ActionClass.MUTATING,
    ActionType.UPLOAD_FILE: ActionClass.MUTATING,
    ActionType.SELECT_OPTION: ActionClass.MUTATING,
    ActionType.CHECKBOX: ActionClass.MUTATING,
    ActionType.KEYPRESS: ActionClass.MUTATING,
    ActionType.SOLVE_CAPTCHA: ActionClass.MUTATING,
    ActionType.VERIFICATION_CODE: ActionClass.MUTATING,
    # GO_FORWARD is gated while GO_BACK is not: forward can advance to a page the agent never
    # authorized in this epoch, whereas back only revisits history it already passed policy on.
    ActionType.GOTO_URL: ActionClass.NAVIGATION,
    ActionType.NEW_TAB: ActionClass.NAVIGATION,
    ActionType.RELOAD_PAGE: ActionClass.NAVIGATION,
    ActionType.GO_FORWARD: ActionClass.NAVIGATION,
    ActionType.DOWNLOAD_FILE: ActionClass.EGRESS,
    ActionType.DRAG: ActionClass.UNRESOLVABLE,
    ActionType.LEFT_MOUSE: ActionClass.UNRESOLVABLE,
    ActionType.EXECUTE_JS: ActionClass.UNSUPPORTED,
}

# Exact model expected for each discriminator. `action_type` is a defaulted field rather than a
# Literal discriminator, so a mismatched pair such as ClickAction(action_type=GOTO_URL) constructs
# cleanly; this table is what makes that detectable.
_ACTION_MODELS: dict[ActionType, type[Action]] = {
    ActionType.NULL_ACTION: NullAction,
    ActionType.WAIT: WaitAction,
    ActionType.HOVER: HoverAction,
    ActionType.EXTRACT: ExtractAction,
    ActionType.SCROLL: ScrollAction,
    ActionType.MOVE: MoveAction,
    ActionType.GO_BACK: GoBackAction,
    ActionType.CLOSE_PAGE: ClosePageAction,
    ActionType.SWITCH_TAB: SwitchTabAction,
    ActionType.TERMINATE: TerminateAction,
    ActionType.COMPLETE: CompleteAction,
    ActionType.CLICK: ClickAction,
    ActionType.INPUT_TEXT: InputTextAction,
    ActionType.UPLOAD_FILE: UploadFileAction,
    ActionType.SELECT_OPTION: SelectOptionAction,
    ActionType.CHECKBOX: CheckboxAction,
    ActionType.KEYPRESS: KeypressAction,
    ActionType.SOLVE_CAPTCHA: SolveCaptchaAction,
    ActionType.VERIFICATION_CODE: VerificationCodeAction,
    ActionType.GOTO_URL: GotoUrlAction,
    ActionType.NEW_TAB: NewTabAction,
    ActionType.RELOAD_PAGE: ReloadPageAction,
    ActionType.GO_FORWARD: GoForwardAction,
    ActionType.DOWNLOAD_FILE: DownloadFileAction,
    ActionType.DRAG: DragAction,
    ActionType.LEFT_MOUSE: LeftMouseAction,
    ActionType.EXECUTE_JS: ExecuteJsAction,
}


@dataclass(frozen=True, slots=True)
class BrowserOrigin:
    scheme: str
    host: str
    port: int

    @property
    def canonical(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.port == _DEFAULT_PORTS[self.scheme] else f":{self.port}"
        return f"{self.scheme}://{host}{port}"


@dataclass(frozen=True, slots=True)
class ProtectedReference:
    """Opaque handle to a secret, code or file. The protected value never enters this module."""

    kind: ProtectedReferenceKind
    reference_id: str
    owner_id: str

    @property
    def complete(self) -> bool:
        return isinstance(self.kind, ProtectedReferenceKind) and bool(self.reference_id) and bool(self.owner_id)


@dataclass(frozen=True, slots=True)
class ActionTarget:
    urls: tuple[str, ...] = ()
    resolvable: bool = True


@dataclass(frozen=True, slots=True)
class ActionProjection:
    action_type: ActionType | None
    action_class: ActionClass | None
    target: ActionTarget
    required_references: frozenset[ProtectedReferenceKind] = frozenset()
    defects: tuple[PolicyReason, ...] = ()


@dataclass(frozen=True, slots=True)
class PageObservation:
    page_url: str
    observation_epoch: int
    verdict: ObservationVerdict


@dataclass(frozen=True, slots=True)
class BrowserActionPolicy:
    owner_id: str
    allowed_origins: frozenset[BrowserOrigin]
    version: int = 1


@dataclass(frozen=True, slots=True)
class RuntimeOriginAuthority:
    """What the run may reach right now, per ADR-0011's task-URL-derived exact-origin authority.

    `origins` is only consulted when `state` is ESTABLISHED, and even then it is intersected with the
    enrolled ceiling: an authority naming an origin the operator never enrolled grants nothing.
    """

    state: AuthorityState
    origins: frozenset[BrowserOrigin] = frozenset()


#: The authority every caller passes today, because nothing derives one yet.
#:
#: SKY-12883, SKY-12884 and SKY-12886 are the tickets that fill this seam. Until they land, two
#: ADR-0011 behaviours have no implementation anywhere in this repository:
#:   * blocking until authority is established, and unblocking once it is;
#:   * permanently invalidating a task or workflow context when authority is lost or rotated after a
#:     browser context has been bound.
#: A static enrolled origin set never goes missing and never rotates, so neither behaviour can be
#: inferred from enrollment — do not read "the run is enrolled" as "the run has authority".
#: This state is deliberately distinct from MISSING so that source, decisions and observe-mode logs
#: all distinguish "the authority source says no" from "there is no authority source".
UNWIRED_AUTHORITY = RuntimeOriginAuthority(state=AuthorityState.UNWIRED)

_AUTHORITY_REASONS: dict[AuthorityState, PolicyReason] = {
    AuthorityState.UNWIRED: PolicyReason.UNWIRED_RUNTIME_AUTHORITY,
    AuthorityState.MISSING: PolicyReason.MISSING_RUNTIME_AUTHORITY,
    AuthorityState.INVALIDATED: PolicyReason.INVALIDATED_RUNTIME_AUTHORITY,
}


@dataclass(frozen=True, slots=True)
class BrowserActionRequest:
    policy: BrowserActionPolicy | None
    projection: ActionProjection
    # No default: the absent-authority states must be named at every construction site rather than
    # reachable by leaving an argument off.
    authority: RuntimeOriginAuthority
    request_epoch: int
    evidence: PageObservation | None = None
    protected_references: tuple[ProtectedReference, ...] = ()

    def __post_init__(self) -> None:
        # Omitting the field already raises. This rejects the other way in — passing None
        # explicitly — so absence is unreachable by assignment as well, and a gated decision can
        # never reach an authority it cannot read.
        if not isinstance(self.authority, RuntimeOriginAuthority):
            raise TypeError("A browser action request requires a runtime origin authority")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reasons: tuple[PolicyReason, ...] = ()


def coerce_verdict(value: object) -> ObservationVerdict:
    """Normalize a detector verdict by value, failing closed to UNKNOWN."""
    if not isinstance(value, str):
        return ObservationVerdict.UNKNOWN
    try:
        return ObservationVerdict(value)
    except ValueError:
        return ObservationVerdict.UNKNOWN


def canonicalize_origin(url: object) -> BrowserOrigin | None:
    """Extract a canonical origin from an untrusted runtime URL, or None if it is not usable."""
    if not isinstance(url, str) or not url or url != url.strip() or "\\" in url:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        return None
    try:
        parsed = urlsplit(url)
        raw_scheme = parsed.scheme.lower()
        scheme = _WEBSOCKET_SCHEMES.get(raw_scheme, raw_scheme)
        if scheme not in _DEFAULT_PORTS or parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname
        parsed_port = parsed.port
        port = parsed_port if parsed_port is not None else _DEFAULT_PORTS[scheme]
    except (UnicodeError, ValueError):
        return None
    if host is None or not parsed.netloc or "%" in host:
        return None
    host = host.rstrip(".").lower()
    if not host:
        return None
    if not host.isascii():
        # str.encode("idna") is IDNA2003 + nameprep, which folds ß to "ss" and ς to σ; browsers use
        # UTS-46 nontransitional and do not. Trusting it would authorize a host the browser never
        # visits, so a non-ASCII host is refused and operators declare the A-label the browser uses.
        return None
    try:
        host = ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        # WHATWG parses a host whose last label is numeric as an IPv4 literal, in decimal, octal or
        # hex. Anything ip_address refuses here is a malformed or legacy address literal (leading
        # zeros per CVE-2021-29921, bare integers, 0x7f000001). Passing it through as a domain name
        # would mint an origin that never matches the host a resolver actually reaches.
        if _IPV4_LAST_LABEL.fullmatch(host.rpartition(".")[2]):
            return None
        try:
            # IDNA rewrites non-ASCII label separators (U+3002 and friends) as ASCII dots, so the
            # root label can only be stripped once encoding has settled the separators.
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return None
        if not host:
            return None
    return BrowserOrigin(scheme=scheme, host=host, port=port)


def declare_origin(url: str) -> BrowserOrigin:
    """Validate an operator-declared origin, raising rather than silently narrowing it."""
    origin = canonicalize_origin(url)
    if origin is None:
        raise ValueError(f"Not a declarable browser origin: {url!r}")
    return origin


def declare_policy(*, owner_id: str, origin_urls: Iterable[str], version: int = 1) -> BrowserActionPolicy:
    if not owner_id:
        raise ValueError("A browser action policy requires an owner")
    origins = frozenset(declare_origin(url) for url in origin_urls)
    if not origins:
        raise ValueError("A browser action policy requires at least one declared origin")
    return BrowserActionPolicy(owner_id=owner_id, allowed_origins=origins, version=version)


def classify_action_type(action_type: ActionType | str) -> ActionClass | None:
    try:
        normalized = ActionType(action_type)
    except ValueError:
        return None
    return _ACTION_CLASSES.get(normalized)


def _required_references(action: Action, action_type: ActionType) -> frozenset[ProtectedReferenceKind]:
    if action_type is ActionType.VERIFICATION_CODE:
        return frozenset({ProtectedReferenceKind.VERIFICATION_CODE})
    if action_type is ActionType.UPLOAD_FILE:
        return frozenset({ProtectedReferenceKind.FILE})
    if isinstance(action, InputTextAction) and (action.totp_code_required or action.totp_identifier or action.totp_url):
        return frozenset({ProtectedReferenceKind.SECRET})
    if isinstance(action, ClickAction) and action.file_url:
        return frozenset({ProtectedReferenceKind.FILE})
    return frozenset()


def _target(action: Action, action_type: ActionType) -> ActionTarget:
    if _ACTION_CLASSES.get(action_type) is ActionClass.UNRESOLVABLE:
        return ActionTarget(resolvable=False)
    if isinstance(action, (GotoUrlAction, NewTabAction)):
        return ActionTarget(urls=(action.url,))
    if isinstance(action, DownloadFileAction) and action.download_url:
        return ActionTarget(urls=(action.download_url,))
    # A download carrying bytes rather than a URL has no destination to check here; confining where
    # it lands is the sink's job, so an EGRESS action is not always target-checked.
    if isinstance(action, (ClickAction, UploadFileAction)) and action.file_url:
        return ActionTarget(urls=(action.file_url,))
    return ActionTarget()


def project_action(action: Action) -> ActionProjection:
    """Project a concrete action model into policy facts, failing closed on any mismatch.

    Never memoize this per action id: handlers rewrite `action_type` in place (COMPLETE becomes
    TERMINATE), so a projection is only valid for the object as it stands right now.
    """
    try:
        action_type = ActionType(action.action_type)
    except ValueError:
        return ActionProjection(
            action_type=None,
            action_class=None,
            target=ActionTarget(resolvable=False),
            defects=(PolicyReason.UNKNOWN_ACTION,),
        )

    expected_model = _ACTION_MODELS.get(action_type)
    action_class = _ACTION_CLASSES.get(action_type)
    if expected_model is None or action_class is None:
        # A newly added ActionType that nobody mapped. Crashing inside a security control would make
        # observe mode behavioural, so it degrades to an unknown action instead.
        return ActionProjection(
            action_type=action_type,
            action_class=None,
            target=ActionTarget(resolvable=False),
            defects=(PolicyReason.UNKNOWN_ACTION,),
        )

    if type(action) is not expected_model:
        # The field layout cannot be trusted once the model and its discriminator disagree, so no
        # target or reference facts are extracted from it.
        return ActionProjection(
            action_type=action_type,
            action_class=None,
            target=ActionTarget(resolvable=False),
            defects=(PolicyReason.ACTION_MODEL_MISMATCH,),
        )

    return ActionProjection(
        action_type=action_type,
        action_class=action_class,
        target=_target(action, action_type),
        required_references=_required_references(action, action_type),
    )


def _reference_reasons(
    required: frozenset[ProtectedReferenceKind],
    supplied: tuple[ProtectedReference, ...],
    owner_id: str,
) -> set[PolicyReason]:
    reasons: set[PolicyReason] = set()
    usable: set[ProtectedReferenceKind] = set()
    for reference in supplied:
        if not reference.complete:
            reasons.add(PolicyReason.INCOMPLETE_PROTECTED_REFERENCE)
        elif reference.owner_id != owner_id:
            reasons.add(PolicyReason.UNOWNED_PROTECTED_REFERENCE)
        else:
            usable.add(reference.kind)
    if required - usable:
        reasons.add(PolicyReason.MISSING_PROTECTED_REFERENCE)
    return reasons


def decide_browser_action(request: BrowserActionRequest) -> PolicyDecision:
    """Evaluate one proposed browser action. `policy=None` is the only path to `not_enrolled`.

    The enrolled policy is a ceiling, not the complete authority: an origin-gated action is allowed
    only when a live `RuntimeOriginAuthority` grants it *and* the ceiling admits it.
    """
    policy = request.policy
    if policy is None:
        return PolicyDecision(outcome=PolicyOutcome.NOT_ENROLLED)

    projection = request.projection
    action_class = projection.action_class
    reasons: set[PolicyReason] = set(projection.defects)

    if action_class is None and not reasons & {PolicyReason.UNKNOWN_ACTION, PolicyReason.ACTION_MODEL_MISMATCH}:
        # ActionProjection is public API with `defects` defaulting to (), so a caller can hand us an
        # unclassified projection. Unclassifiable means deny, never fall through to the exempt set.
        reasons.add(PolicyReason.UNKNOWN_ACTION)
    if action_class is ActionClass.UNSUPPORTED:
        reasons.add(PolicyReason.UNSUPPORTED_ACTION)
    if action_class is ActionClass.UNRESOLVABLE or not projection.target.resolvable:
        reasons.add(PolicyReason.UNRESOLVABLE_TARGET)

    evidence = request.evidence
    if evidence is None:
        reasons.add(PolicyReason.MISSING_PAGE_EVIDENCE)
        page_origin = None
        verdict = ObservationVerdict.UNKNOWN
    else:
        if evidence.observation_epoch != request.request_epoch:
            reasons.add(PolicyReason.STALE_PAGE_EVIDENCE)
        page_origin = canonicalize_origin(evidence.page_url)
        if page_origin is None:
            reasons.add(PolicyReason.MISSING_PAGE_ORIGIN)
        verdict = coerce_verdict(evidence.verdict)

    reasons |= _reference_reasons(projection.required_references, request.protected_references, policy.owner_id)

    if action_class not in _VERDICT_UNGATED_CLASSES:
        if verdict is ObservationVerdict.SUSPICIOUS:
            reasons.add(PolicyReason.UNTRUSTED_OBSERVATION)
        elif verdict is ObservationVerdict.UNKNOWN:
            reasons.add(PolicyReason.UNVERIFIED_OBSERVATION)
    if action_class not in _ORIGIN_UNGATED_CLASSES:
        authority = request.authority
        authority_reason = _AUTHORITY_REASONS.get(authority.state)
        if authority.state is not AuthorityState.ESTABLISHED:
            # Being inside the enrolled ceiling is never sufficient to allow. Without a live
            # authority the answer to "what may this action reach" is unknown, and unknown denies.
            reasons.add(authority_reason if authority_reason is not None else PolicyReason.MISSING_RUNTIME_AUTHORITY)
        else:
            reachable = policy.allowed_origins & authority.origins
            if page_origin is not None and page_origin not in reachable:
                reasons.add(PolicyReason.PAGE_ORIGIN_NOT_AUTHORIZED)
            for url in projection.target.urls:
                target_origin = canonicalize_origin(url)
                if target_origin is None:
                    reasons.add(PolicyReason.MISSING_TARGET_ORIGIN)
                elif target_origin not in reachable:
                    reasons.add(PolicyReason.TARGET_ORIGIN_NOT_AUTHORIZED)

    if PolicyReason.UNSUPPORTED_ACTION in reasons:
        outcome = PolicyOutcome.UNSUPPORTED
    elif reasons:
        outcome = PolicyOutcome.DENIED
    else:
        outcome = PolicyOutcome.ALLOWED
    return PolicyDecision(outcome=outcome, reasons=tuple(sorted(reasons, key=REASON_PRECEDENCE.index)))
