import dataclasses
import inspect
import traceback

import pytest

from skyvern.forge.sdk import terminal_callback_binding as binding_module
from skyvern.forge.sdk.browser_action_policy import PolicyOutcome
from skyvern.forge.sdk.terminal_callback_binding import (
    UNMEDIATED_EPOCH,
    AuthorizedTransition,
    TerminalBindingError,
    TerminalBindingReason,
    TerminalCallbackBroker,
    TerminalTransitionKind,
    project_terminal_transition,
)
from skyvern.webeye.actions.action_types import ActionType

OWNER = "o_12887"
OTHER_OWNER = "o_other_12887"
RUN = "wr_12887"
OTHER_RUN = "wr_other_12887"
EPOCH = 7
CALLBACK_SECRET = "tok-must-not-leak-2f9c"
CALLBACK_URL = f"https://hooks.example.test/skyvern?token={CALLBACK_SECRET}"
# The untrusted output of a run. It must never be an input that grants authority or names a
# destination, so it never appears in any call below — the contract is that there is nowhere to put it.
RESULT_PAYLOAD = "extracted-result-that-must-not-create-authority"


def bound_broker(callback_url: str | None = CALLBACK_URL) -> TerminalCallbackBroker:
    broker = TerminalCallbackBroker()
    broker.bind_run(run_id=RUN, owner_id=OWNER, callback_url=callback_url)
    return broker


def authorize(
    broker: TerminalCallbackBroker,
    *,
    run_id: str = RUN,
    owner_id: str = OWNER,
    action_type: ActionType | str = ActionType.COMPLETE,
    action_epoch: int | None = EPOCH,
    current_epoch: int = EPOCH,
    policy_outcome: PolicyOutcome = PolicyOutcome.ALLOWED,
) -> AuthorizedTransition:
    return broker.authorize_terminal_transition(
        run_id=run_id,
        owner_id=owner_id,
        action_type=action_type,
        action_epoch=action_epoch,
        current_epoch=current_epoch,
        policy_outcome=policy_outcome,
    )


def assert_protected_data_absent_from_module_traceback(error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert CALLBACK_URL not in rendered
    assert CALLBACK_SECRET not in rendered
    traceback_node = error.__traceback__
    module_frames = 0
    while traceback_node is not None:
        if traceback_node.tb_frame.f_code.co_filename.endswith("/terminal_callback_binding.py"):
            module_frames += 1
            frame_locals = repr(traceback_node.tb_frame.f_locals)
            assert CALLBACK_URL not in frame_locals
            assert CALLBACK_SECRET not in frame_locals
        traceback_node = traceback_node.tb_next
    assert module_frames > 0


class _MutableAction:
    """Stand-in for an agent action whose ``action_type`` a handler rewrites in place."""

    def __init__(self, action_type: ActionType) -> None:
        self.action_type = action_type


# --- re-projection of the effective terminal action (AC2) -------------------------------------


def test_project_maps_the_two_terminal_actions() -> None:
    assert project_terminal_transition(ActionType.COMPLETE) is TerminalTransitionKind.COMPLETE
    assert project_terminal_transition(ActionType.TERMINATE) is TerminalTransitionKind.TERMINATE


def test_project_normalizes_string_action_types() -> None:
    assert project_terminal_transition("complete") is TerminalTransitionKind.COMPLETE
    assert project_terminal_transition("terminate") is TerminalTransitionKind.TERMINATE


@pytest.mark.parametrize(
    "action_type",
    [ActionType.CLICK, ActionType.INPUT_TEXT, ActionType.GOTO_URL, ActionType.EXTRACT, "extract", "", "not_a_type"],
)
def test_project_returns_none_for_non_terminal_actions(action_type: ActionType | str) -> None:
    assert project_terminal_transition(action_type) is None


@pytest.mark.parametrize("action_type", [None, 7, object()])
def test_project_fails_closed_on_unusable_action_types(action_type: object) -> None:
    assert project_terminal_transition(action_type) is None


def test_project_reflects_an_in_place_complete_to_terminate_conversion() -> None:
    action = _MutableAction(ActionType.COMPLETE)
    assert project_terminal_transition(action.action_type) is TerminalTransitionKind.COMPLETE
    action.action_type = ActionType.TERMINATE
    assert project_terminal_transition(action.action_type) is TerminalTransitionKind.TERMINATE


# --- the authorized transition gates both the terminal write and the callback (AC1) -----------


def test_authorization_binds_to_the_effective_action_and_run() -> None:
    broker = bound_broker()

    transition = authorize(broker, action_type=ActionType.COMPLETE)

    assert isinstance(transition, AuthorizedTransition)
    assert transition.effective is TerminalTransitionKind.COMPLETE
    assert transition.run_id == RUN
    assert transition.transition_id.startswith("txn_")


def test_a_terminate_authorization_carries_the_terminate_effect() -> None:
    broker = bound_broker()

    transition = authorize(broker, action_type=ActionType.TERMINATE)

    assert transition.effective is TerminalTransitionKind.TERMINATE


def test_authorized_transition_resolves_the_bound_callback() -> None:
    broker = bound_broker()

    transition = authorize(broker)

    assert broker.resolve_callback(transition, run_id=RUN) == CALLBACK_URL


def test_distinct_runs_are_authorized_independently() -> None:
    broker = TerminalCallbackBroker()
    broker.bind_run(run_id=RUN, owner_id=OWNER, callback_url=CALLBACK_URL)
    broker.bind_run(run_id=OTHER_RUN, owner_id=OWNER, callback_url=None)

    first = authorize(broker, run_id=RUN)
    second = authorize(broker, run_id=OTHER_RUN)

    assert first.transition_id != second.transition_id
    assert broker.resolve_callback(first, run_id=RUN) == CALLBACK_URL
    assert broker.resolve_callback(second, run_id=OTHER_RUN) is None


# --- a converted COMPLETE is authorized as the effective TERMINATE (AC2) -----------------------


def test_a_converted_action_is_authorized_as_the_effective_terminate() -> None:
    broker = bound_broker()
    action = _MutableAction(ActionType.COMPLETE)
    action.action_type = ActionType.TERMINATE

    transition = authorize(broker, action_type=action.action_type)

    assert transition.effective is TerminalTransitionKind.TERMINATE


# --- untrusted result payloads cannot create authority or name a destination (AC3, AC5) --------


def test_no_authorization_input_can_carry_an_untrusted_result_payload() -> None:
    parameters = set(inspect.signature(TerminalCallbackBroker.authorize_terminal_transition).parameters)

    for forbidden in ("result", "results", "payload", "extracted_information", "output", "data", "body"):
        assert forbidden not in parameters


def test_callback_resolution_exposes_no_destination_parameter() -> None:
    parameters = set(inspect.signature(TerminalCallbackBroker.resolve_callback).parameters)

    for forbidden in ("url", "target", "target_url", "destination", "callback_url", "webhook_url"):
        assert forbidden not in parameters


def test_only_run_binding_supplies_the_callback_destination() -> None:
    bind_parameters = set(inspect.signature(TerminalCallbackBroker.bind_run).parameters)

    assert "callback_url" in bind_parameters


# --- the callback destination is the run's original authenticated configuration (AC4) ----------


def test_callback_destination_is_exactly_the_bound_configuration() -> None:
    broker = bound_broker(callback_url=CALLBACK_URL)

    transition = authorize(broker)

    assert broker.resolve_callback(transition, run_id=RUN) == CALLBACK_URL


def test_a_run_without_a_callback_resolves_to_none_rather_than_erroring() -> None:
    broker = bound_broker(callback_url=None)

    transition = authorize(broker)

    assert broker.resolve_callback(transition, run_id=RUN) is None


# --- blocked / stale / replayed / wrong-run / unmediated produce neither write nor callback (AC6)


@pytest.mark.parametrize(
    "policy_outcome", [PolicyOutcome.DENIED, PolicyOutcome.UNSUPPORTED, PolicyOutcome.NOT_ENROLLED]
)
def test_a_blocked_action_is_not_authorized(policy_outcome: PolicyOutcome) -> None:
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker, policy_outcome=policy_outcome)

    assert caught.value.reason is TerminalBindingReason.BLOCKED_ACTION


@pytest.mark.parametrize("action_epoch", [None, UNMEDIATED_EPOCH, -5])
def test_an_unmediated_action_is_not_authorized(action_epoch: int | None) -> None:
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker, action_epoch=action_epoch, current_epoch=action_epoch if isinstance(action_epoch, int) else 0)

    assert caught.value.reason is TerminalBindingReason.UNMEDIATED_TRANSITION


def test_a_stale_action_is_not_authorized() -> None:
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker, action_epoch=EPOCH, current_epoch=EPOCH + 1)

    assert caught.value.reason is TerminalBindingReason.STALE_TRANSITION


def test_a_wrong_owner_is_not_authorized() -> None:
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker, owner_id=OTHER_OWNER)

    assert caught.value.reason is TerminalBindingReason.WRONG_RUN


def test_an_unbound_run_is_not_authorized() -> None:
    broker = TerminalCallbackBroker()

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker, run_id=OTHER_RUN)

    assert caught.value.reason is TerminalBindingReason.UNBOUND_RUN


def test_a_non_terminal_action_is_not_authorized() -> None:
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker, action_type=ActionType.CLICK)

    assert caught.value.reason is TerminalBindingReason.NOT_A_TERMINAL_ACTION


def test_a_replayed_transition_is_not_authorized() -> None:
    broker = bound_broker()
    authorize(broker)

    with pytest.raises(TerminalBindingError) as caught:
        authorize(broker)

    assert caught.value.reason is TerminalBindingReason.REPLAYED_TRANSITION


def test_a_denied_action_leaves_nothing_a_callback_could_bind_to() -> None:
    broker = bound_broker()
    try:
        authorize(broker, policy_outcome=PolicyOutcome.DENIED)
    except TerminalBindingError:
        pass
    forged = AuthorizedTransition(transition_id="txn_forged", run_id=RUN, effective=TerminalTransitionKind.COMPLETE)

    with pytest.raises(TerminalBindingError) as caught:
        broker.resolve_callback(forged, run_id=RUN)

    assert caught.value.reason is TerminalBindingReason.FORGED_AUTHORIZATION


# --- callbacks cannot be forged, redirected to another run, or replayed ------------------------


def test_a_forged_authorization_cannot_resolve_a_callback() -> None:
    broker = bound_broker()
    authorize(broker)
    forged = AuthorizedTransition(transition_id="txn_forged", run_id=RUN, effective=TerminalTransitionKind.COMPLETE)

    with pytest.raises(TerminalBindingError) as caught:
        broker.resolve_callback(forged, run_id=RUN)

    assert caught.value.reason is TerminalBindingReason.FORGED_AUTHORIZATION


def test_a_tampered_run_on_a_real_authorization_cannot_resolve_a_callback() -> None:
    broker = TerminalCallbackBroker()
    broker.bind_run(run_id=RUN, owner_id=OWNER, callback_url=CALLBACK_URL)
    broker.bind_run(run_id=OTHER_RUN, owner_id=OWNER, callback_url=None)
    transition = authorize(broker, run_id=RUN)
    tampered = dataclasses.replace(transition, run_id=OTHER_RUN)

    with pytest.raises(TerminalBindingError) as caught:
        broker.resolve_callback(tampered, run_id=OTHER_RUN)

    assert caught.value.reason is TerminalBindingReason.WRONG_RUN


def test_a_callback_is_deliverable_only_once() -> None:
    broker = bound_broker()
    transition = authorize(broker)
    assert broker.resolve_callback(transition, run_id=RUN) == CALLBACK_URL

    with pytest.raises(TerminalBindingError) as caught:
        broker.resolve_callback(transition, run_id=RUN)

    assert caught.value.reason is TerminalBindingReason.REPLAYED_TRANSITION


# --- run binding is authenticated once and cannot be replaced mid-run -------------------------


@pytest.mark.parametrize("fields", [{"run_id": ""}, {"run_id": "   "}, {"owner_id": ""}, {"callback_url": "   "}])
def test_binding_fails_closed_on_incomplete_facts(fields: dict[str, str]) -> None:
    arguments = {"run_id": RUN, "owner_id": OWNER, "callback_url": CALLBACK_URL, **fields}

    with pytest.raises(TerminalBindingError) as caught:
        TerminalCallbackBroker().bind_run(**arguments)

    assert caught.value.reason is TerminalBindingReason.INCOMPLETE_BINDING
    assert_protected_data_absent_from_module_traceback(caught.value)


def test_rebinding_a_run_is_refused() -> None:
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        broker.bind_run(run_id=RUN, owner_id=OWNER, callback_url="https://hooks.example.test/other")

    assert caught.value.reason is TerminalBindingReason.RUN_ALREADY_BOUND


# --- opaque, unique capabilities --------------------------------------------------------------


def test_transition_ids_are_opaque_and_unique() -> None:
    broker = TerminalCallbackBroker()
    broker.bind_run(run_id=RUN, owner_id=OWNER, callback_url=None)
    broker.bind_run(run_id=OTHER_RUN, owner_id=OWNER, callback_url=None)

    first = authorize(broker, run_id=RUN).transition_id
    second = authorize(broker, run_id=OTHER_RUN).transition_id

    assert first.startswith("txn_")
    assert second.startswith("txn_")
    assert first != second


def test_transition_id_collision_retries_without_reusing_an_id(monkeypatch: pytest.MonkeyPatch) -> None:
    generated = iter(["collision", "collision", "unique"])
    monkeypatch.setattr(binding_module.secrets, "token_urlsafe", lambda _: next(generated))
    broker = TerminalCallbackBroker()
    broker.bind_run(run_id=RUN, owner_id=OWNER, callback_url=None)
    broker.bind_run(run_id=OTHER_RUN, owner_id=OWNER, callback_url=None)

    first = authorize(broker, run_id=RUN).transition_id
    second = authorize(broker, run_id=OTHER_RUN).transition_id

    assert first == "txn_collision"
    assert second == "txn_unique"


# --- decisions, capabilities and errors carry no protected data (AC7) --------------------------


def test_the_broker_repr_hides_the_callback_destination() -> None:
    broker = bound_broker()

    assert CALLBACK_URL not in repr(broker)
    assert CALLBACK_SECRET not in repr(broker)


def test_an_authorized_transition_never_reveals_the_callback_destination() -> None:
    broker = bound_broker()

    transition = authorize(broker)

    assert CALLBACK_URL not in repr(transition)
    assert CALLBACK_SECRET not in repr(transition)


def test_binding_errors_scrub_the_callback_destination_from_their_traceback() -> None:
    with pytest.raises(TerminalBindingError) as caught:
        TerminalCallbackBroker().bind_run(run_id=RUN, owner_id="", callback_url=CALLBACK_URL)

    assert CALLBACK_URL not in str(caught.value)
    assert CALLBACK_SECRET not in str(caught.value)
    assert_protected_data_absent_from_module_traceback(caught.value)


@pytest.mark.parametrize(
    "provoke",
    [
        lambda broker: broker.resolve_callback(
            AuthorizedTransition("txn_forged", RUN, TerminalTransitionKind.COMPLETE), run_id=RUN
        ),
        lambda broker: authorize(broker, policy_outcome=PolicyOutcome.DENIED),
        lambda broker: authorize(broker, action_epoch=None),
    ],
)
def test_authorization_and_resolution_errors_scrub_their_traceback(provoke) -> None:  # type: ignore[no-untyped-def]
    broker = bound_broker()

    with pytest.raises(TerminalBindingError) as caught:
        provoke(broker)

    assert_protected_data_absent_from_module_traceback(caught.value)
