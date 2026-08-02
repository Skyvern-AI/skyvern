"""Consumer-bound authorization for the two terminal browser-firewall sinks (SKY-12887).

Completion/termination persistence and the outbound webhook callback are SENSITIVE SINKS, not
bookkeeping. A terminal status write and a callback delivery must each bind to the exact task/run
transition that authorizes them, computed immediately before the effective write or delivery — never
carried from an earlier, now-stale decision.

Two properties this module exists to guarantee:

  * The terminal write binds to the EFFECTIVE action as it stands right now. A COMPLETE the agent
    converted to a TERMINATE in place (``handle_complete_action`` rewrites ``action.action_type``)
    is re-projected here and authorized as a TERMINATE — the authorization is derived from the live
    action type, so a converted action cannot ride in on a stale COMPLETE authorization.
  * The callback destination binds INDEPENDENTLY to the run's original authenticated configuration,
    captured once at run creation. There is nowhere in this API to hand in a destination, so an
    untrusted result payload can neither supply, rewrite, nor redirect where a webhook is sent.

Everything here is fail-closed. The untrusted output of a run never enters this module, so it cannot
create authority. A blocked, stale, replayed, wrong-run, or unmediated action yields no
``AuthorizedTransition``; without one, neither the terminal write nor the callback can proceed. The
callback destination and every protected value stay out of errors, reprs and tracebacks — the reason
codes are the only surface a caller may log, and they carry no protected data BY CONSTRUCTION.

Like :mod:`skyvern.forge.sdk.protected_reference`, this is the contract the enforcing sinks consume;
wiring the concrete task, workflow-run and block writes to it is a separate integration step.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from skyvern.forge.sdk.browser_action_policy import PolicyOutcome
from skyvern.webeye.actions.action_types import ActionType

#: An action that never carried an observation epoch was never mediated by the firewall's action
#: path. Mirrors ``browser_action_preflight.UNSTAMPED_EPOCH``: no real epoch is negative, so a
#: sentinel can never be mistaken for a mediated one.
UNMEDIATED_EPOCH = -1


class TerminalTransitionKind(StrEnum):
    COMPLETE = "complete"
    TERMINATE = "terminate"


class TerminalBindingReason(StrEnum):
    INCOMPLETE_BINDING = "incomplete_binding"
    RUN_ALREADY_BOUND = "run_already_bound"
    UNBOUND_RUN = "unbound_run"
    WRONG_RUN = "wrong_run"
    NOT_A_TERMINAL_ACTION = "not_a_terminal_action"
    BLOCKED_ACTION = "blocked_action"
    UNMEDIATED_TRANSITION = "unmediated_transition"
    STALE_TRANSITION = "stale_transition"
    REPLAYED_TRANSITION = "replayed_transition"
    FORGED_AUTHORIZATION = "forged_authorization"


class TerminalBindingError(RuntimeError):
    """A safe failure containing no callback destination and no protected value."""

    def __init__(self, reason: TerminalBindingReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def _raise_safe_error(reason: TerminalBindingReason) -> NoReturn:
    raise TerminalBindingError(reason)


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mediated(epoch: object) -> bool:
    return isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0


_TERMINAL_KINDS: dict[ActionType, TerminalTransitionKind] = {
    ActionType.COMPLETE: TerminalTransitionKind.COMPLETE,
    ActionType.TERMINATE: TerminalTransitionKind.TERMINATE,
}


def project_terminal_transition(action_type: ActionType | str) -> TerminalTransitionKind | None:
    """Re-project the effective terminal transition from an action type AS IT STANDS NOW.

    Never memoize per action id: a handler can rewrite ``action_type`` in place (COMPLETE becomes
    TERMINATE), so this must be read from the live value at the authorization site. Anything that is
    not one of the two terminal actions — including an unknown or unusable value — projects to
    ``None`` and is not a terminal transition.
    """
    try:
        normalized = ActionType(action_type)
    except (ValueError, TypeError):
        return None
    return _TERMINAL_KINDS.get(normalized)


@dataclass(frozen=True, slots=True)
class AuthorizedTransition:
    """Opaque proof that an exact task/run terminal transition was authorized.

    Gates both effective sinks: the terminal status write and, later, the outbound callback. It
    never carries the callback destination — resolving one requires handing this proof back to the
    broker that issued it, which alone holds the run's bound configuration.
    """

    transition_id: str
    run_id: str
    effective: TerminalTransitionKind


@dataclass(frozen=True, slots=True, repr=False)
class _RunBinding:
    owner_id: str
    callback_url: str | None

    def __repr__(self) -> str:
        # The default dataclass repr would render callback_url, which can embed credentials. Any
        # frame holding a binding (in locals, or a persisted run artifact) must not leak it.
        return f"_RunBinding(owner_id={self.owner_id!r}, callback={'set' if self.callback_url else 'none'})"


class TerminalCallbackBroker:
    """Binds terminal writes and webhook callbacks to authenticated run transitions.

    A run is bound ONCE, at authenticated creation, with its owner and its original callback
    destination. That binding is the single source of truth for the destination and cannot be
    replaced mid-run — a second bind is refused so a control-plane change cannot rewrite a live
    run's authority. Authorizing a terminal transition is single-use per run: a run transitions to
    terminal exactly once, and the resulting proof delivers its callback exactly once.
    """

    def __init__(self) -> None:
        self._runs: dict[str, _RunBinding] = {}
        self._terminal_runs: set[str] = set()
        self._authorized: dict[str, str] = {}
        self._delivered_callbacks: set[str] = set()
        self._issued_transition_ids: set[str] = set()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(runs={len(self._runs)}, terminal={len(self._terminal_runs)})"

    def bind_run(self, *, run_id: str, owner_id: str, callback_url: str | None = None) -> None:
        """Record the run's authenticated identity and its original callback destination.

        ``callback_url`` is the ONLY entry point for a webhook destination and comes from the
        authenticated run configuration, not from any run output. ``None`` means the run has no
        callback; a present-but-empty value is a misconfiguration and fails closed.
        """
        if not _present(run_id) or not _present(owner_id) or (callback_url is not None and not _present(callback_url)):
            del self, run_id, owner_id, callback_url
            _raise_safe_error(TerminalBindingReason.INCOMPLETE_BINDING)
        if run_id in self._runs:
            del self, run_id, owner_id, callback_url
            _raise_safe_error(TerminalBindingReason.RUN_ALREADY_BOUND)
        self._runs[run_id] = _RunBinding(owner_id=owner_id, callback_url=callback_url)

    def authorize_terminal_transition(
        self,
        *,
        run_id: str,
        owner_id: str,
        action_type: ActionType | str,
        action_epoch: int | None,
        current_epoch: int,
        policy_outcome: PolicyOutcome,
    ) -> AuthorizedTransition:
        """Authorize the effective terminal transition, immediately before persistence.

        The effective action is re-projected from ``action_type`` here, so a converted COMPLETE is
        authorized as the TERMINATE it now is. No result payload is an input, so untrusted output
        cannot create authority. Reasons are checked most-structural first: incomplete facts, an
        unbound or wrong-owner run, a non-terminal or policy-blocked action, an unmediated or stale
        transition, then a replayed one. Any failure raises and yields no proof — so a blocked,
        stale, replayed, wrong-run or unmediated action produces neither a terminal write nor a
        callback.
        """
        if (
            not _present(run_id)
            or not _present(owner_id)
            or not isinstance(current_epoch, int)
            or isinstance(current_epoch, bool)
        ):
            _raise_safe_error(TerminalBindingReason.INCOMPLETE_BINDING)
        binding = self._runs.get(run_id)
        if binding is None:
            _raise_safe_error(TerminalBindingReason.UNBOUND_RUN)
        if binding.owner_id != owner_id:
            _raise_safe_error(TerminalBindingReason.WRONG_RUN)
        effective = project_terminal_transition(action_type)
        if effective is None:
            _raise_safe_error(TerminalBindingReason.NOT_A_TERMINAL_ACTION)
        if policy_outcome is not PolicyOutcome.ALLOWED:
            _raise_safe_error(TerminalBindingReason.BLOCKED_ACTION)
        if not _mediated(action_epoch):
            _raise_safe_error(TerminalBindingReason.UNMEDIATED_TRANSITION)
        if action_epoch != current_epoch:
            _raise_safe_error(TerminalBindingReason.STALE_TRANSITION)
        if run_id in self._terminal_runs:
            _raise_safe_error(TerminalBindingReason.REPLAYED_TRANSITION)
        self._terminal_runs.add(run_id)
        transition_id = self._new_transition_id()
        self._authorized[transition_id] = run_id
        return AuthorizedTransition(transition_id=transition_id, run_id=run_id, effective=effective)

    def resolve_callback(self, transition: AuthorizedTransition, *, run_id: str) -> str | None:
        """Resolve the run's bound callback destination for an authorized terminal transition.

        Takes no destination: the URL is exactly the one bound at run creation, so untrusted output
        cannot supply, rewrite or redirect it. The proof must be one this broker issued for this
        exact run, unused. Returns ``None`` when the run configured no callback — nothing to deliver,
        not an error. Delivers at most once per transition.
        """
        if (
            not isinstance(transition, AuthorizedTransition)
            or not _present(transition.transition_id)
            or not _present(run_id)
        ):
            _raise_safe_error(TerminalBindingReason.INCOMPLETE_BINDING)
        authorized_run = self._authorized.get(transition.transition_id)
        if authorized_run is None:
            _raise_safe_error(TerminalBindingReason.FORGED_AUTHORIZATION)
        if authorized_run != run_id:
            _raise_safe_error(TerminalBindingReason.WRONG_RUN)
        if transition.transition_id in self._delivered_callbacks:
            _raise_safe_error(TerminalBindingReason.REPLAYED_TRANSITION)
        binding = self._runs.get(run_id)
        if binding is None:
            _raise_safe_error(TerminalBindingReason.UNBOUND_RUN)
        self._delivered_callbacks.add(transition.transition_id)
        return binding.callback_url

    def _new_transition_id(self) -> str:
        while True:
            transition_id = f"txn_{secrets.token_urlsafe(24)}"
            if transition_id not in self._issued_transition_ids:
                self._issued_transition_ids.add(transition_id)
                return transition_id
