from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from skyvern.exceptions import IllegitComplete
from skyvern.forge.sdk.fail_fast import shadow
from skyvern.forge.sdk.fail_fast.shadow import (
    _act_fp,
    _build_fingerprint,
    _evaluate,
    _record_step,
    _StepFingerprint,
    _TaskLedger,
    _value_signature,
)
from skyvern.forge.sdk.models import Step, StepStatus
from skyvern.schemas.steps import AgentStepOutput
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action
from skyvern.webeye.actions.responses import ActionFailure


def _fp(
    *, state: str | None = "S", acts: tuple[str, ...] = (), plan: str | None = None, illegit: bool = False
) -> _StepFingerprint:
    return _StepFingerprint(
        step_order=0,
        retry_index=0,
        status="completed",
        state_fp=state,
        act_fps=acts,
        plan_fp=plan,
        illegit_complete=illegit,
    )


def _ledger(fingerprints: list[_StepFingerprint]) -> deque[_StepFingerprint]:
    return deque(fingerprints, maxlen=24)


def _make_step(*, output: AgentStepOutput | None = None, order: int = 0, is_speculative: bool = False) -> Step:
    now = datetime.now(timezone.utc)
    return Step(
        created_at=now,
        modified_at=now,
        task_id="tsk_test",
        step_id=f"step_{order}",
        status=StepStatus.completed,
        order=order,
        is_last=False,
        organization_id="o_test",
        output=output,
        is_speculative=is_speculative,
    )


@pytest.fixture(autouse=True)
def _clear_ledgers():
    shadow._LEDGERS.clear()
    yield
    shadow._LEDGERS.clear()


def test_no_progress_fires_at_threshold():
    fired = dict(_evaluate(_ledger([_fp(state="A"), _fp(state="A"), _fp(state="A")])))
    assert fired["no_progress"]["streak"] == 3


def test_no_progress_below_threshold_does_not_fire():
    assert "no_progress" not in dict(_evaluate(_ledger([_fp(state="A"), _fp(state="A")])))


def test_no_progress_resets_when_page_changes():
    # Trailing run is broken by the change, so it must not fire.
    assert "no_progress" not in dict(_evaluate(_ledger([_fp(state="A"), _fp(state="A"), _fp(state="B")])))


def test_no_progress_ignores_unknown_state():
    assert "no_progress" not in dict(_evaluate(_ledger([_fp(state=None), _fp(state=None), _fp(state=None)])))


def test_action_repetition_fires_on_repeated_element():
    same = "click:hash-1:"
    fired = dict(_evaluate(_ledger([_fp(state=str(i), acts=(same,)) for i in range(3)])))
    assert fired["action_repetition"]["repeats"] == 3


def test_illegit_streak_requires_same_state():
    same_state = [_fp(state="A", illegit=True) for _ in range(3)]
    assert "illegit_complete_streak" in dict(_evaluate(_ledger(same_state)))

    changing_state = [_fp(state="A", illegit=True), _fp(state="B", illegit=True), _fp(state="C", illegit=True)]
    assert "illegit_complete_streak" not in dict(_evaluate(_ledger(changing_state)))


def test_plan_stagnation_fires_on_repeated_plan():
    fired = dict(_evaluate(_ledger([_fp(state=str(i), plan="same-plan") for i in range(3)])))
    assert fired["plan_stagnation"]["streak"] == 3


def test_record_step_dedups_per_tripwire():
    ledger = _TaskLedger()
    assert _record_step(ledger, _fp(state="A")) == []
    assert _record_step(ledger, _fp(state="A")) == []
    third = _record_step(ledger, _fp(state="A"))
    assert any(tripwire == "no_progress" for tripwire, _ in third)
    fourth = _record_step(ledger, _fp(state="A"))
    assert all(tripwire != "no_progress" for tripwire, _ in fourth)


def test_value_signature_never_leaks_raw_value():
    secret = "123-45-6789"
    action = Action(action_type=ActionType.INPUT_TEXT, element_id="e1", text=secret)
    signature = _value_signature(action)
    assert secret not in signature
    assert len(signature) == 12
    assert secret not in _act_fp(action)


def test_act_fp_is_stable():
    make = lambda: Action(action_type=ActionType.INPUT_TEXT, element_id="e1", text="value")  # noqa: E731
    assert _act_fp(make()) == _act_fp(make())


def test_build_fingerprint_detects_illegit_complete():
    complete = Action(action_type=ActionType.COMPLETE)
    rejected = ActionFailure(exception=IllegitComplete(data={"error": "user goal not achieved"}))
    step = _make_step(output=AgentStepOutput(actions_and_results=[(complete, [rejected])]))

    fingerprint = _build_fingerprint(step, None)

    assert fingerprint.illegit_complete is True
    assert fingerprint.state_fp is None  # no scraped page provided


def test_build_fingerprint_no_illegit_for_other_failures():
    complete = Action(action_type=ActionType.COMPLETE)
    other = ActionFailure(exception=ValueError("transient"))
    step = _make_step(output=AgentStepOutput(actions_and_results=[(complete, [other])]))
    assert _build_fingerprint(step, None).illegit_complete is False


@pytest.mark.asyncio
async def test_recorder_skips_when_disabled(monkeypatch):
    async def _disabled(_task, _org):
        return False

    monkeypatch.setattr(shadow, "_shadow_enabled", _disabled)
    events: list[tuple[str, dict]] = []
    logger = SimpleNamespace(info=lambda e, **k: events.append((e, k)), warning=lambda e, **k: events.append((e, k)))

    await shadow.record_fail_fast_shadow(
        task=SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1"),
        step=_make_step(),
        organization=SimpleNamespace(organization_id="o_1"),
        scraped_page=None,
        logger=logger,
    )
    assert events == []
    assert not shadow._LEDGERS


@pytest.mark.asyncio
async def test_recorder_skips_speculative_steps(monkeypatch):
    async def _enabled(_task, _org):
        return True

    monkeypatch.setattr(shadow, "_shadow_enabled", _enabled)
    events: list[tuple[str, dict]] = []
    logger = SimpleNamespace(info=lambda e, **k: events.append((e, k)), warning=lambda e, **k: events.append((e, k)))

    await shadow.record_fail_fast_shadow(
        task=SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1"),
        step=_make_step(is_speculative=True),
        organization=SimpleNamespace(organization_id="o_1"),
        scraped_page=None,
        logger=logger,
    )
    assert events == []
    assert not shadow._LEDGERS


@pytest.mark.asyncio
async def test_recorder_emits_once_when_enabled(monkeypatch):
    async def _enabled(_task, _org):
        return True

    monkeypatch.setattr(shadow, "_shadow_enabled", _enabled)
    events: list[tuple[str, dict]] = []
    logger = SimpleNamespace(info=lambda e, **k: events.append((e, k)), warning=lambda e, **k: events.append((e, k)))

    # A stable fake page yields a stable state_fp, so no_progress trips after K identical steps.
    page = SimpleNamespace(last_used_element_tree_html="<div>stable</div>", url="https://example.test/x")
    task = SimpleNamespace(task_id="tsk_enabled", workflow_run_id="wr_1")
    org = SimpleNamespace(organization_id="o_1")

    for order in range(4):
        await shadow.record_fail_fast_shadow(
            task=task, step=_make_step(order=order), organization=org, scraped_page=page, logger=logger
        )

    no_progress = [payload for event, payload in events if payload.get("tripwire_id") == "no_progress"]
    assert len(no_progress) == 1
    assert no_progress[0]["status"] == "would_fire"
    assert no_progress[0]["would_action"] == "terminate"
    assert no_progress[0]["organization_id"] == "o_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, False),
        (False, False),  # PostHog returns a bare bool for a disabled multivariate flag
        (True, True),
        ("on", True),
        ("1", True),
        ("true", True),
        ("ENABLED", True),
        ("2.5", True),
        ("0", False),
        ("off", False),
        ("", False),
        ("nonsense", False),
    ],
)
async def test_shadow_enabled_parses_flag_values(monkeypatch, raw, expected):
    class _Provider:
        async def get_value_cached(self, *args, **kwargs):
            return raw

    monkeypatch.setattr(shadow.app, "EXPERIMENTATION_PROVIDER", _Provider())
    enabled = await shadow._shadow_enabled(
        SimpleNamespace(task_id="t", workflow_run_id="wr"),
        SimpleNamespace(organization_id="o"),
    )
    assert enabled is expected


@pytest.mark.asyncio
async def test_shadow_enabled_survives_provider_error(monkeypatch):
    class _Provider:
        async def get_value_cached(self, *args, **kwargs):
            raise RuntimeError("posthog unavailable")

    monkeypatch.setattr(shadow.app, "EXPERIMENTATION_PROVIDER", _Provider())
    enabled = await shadow._shadow_enabled(
        SimpleNamespace(task_id="t", workflow_run_id="wr"),
        SimpleNamespace(organization_id="o"),
    )
    assert enabled is False


def test_ledger_eviction_bounds_memory():
    for i in range(shadow._MAX_TASKS + 25):
        shadow._get_ledger(f"task_{i}")
    assert len(shadow._LEDGERS) == shadow._MAX_TASKS
    assert "task_0" not in shadow._LEDGERS  # oldest evicted
    assert f"task_{shadow._MAX_TASKS + 24}" in shadow._LEDGERS  # newest retained


def test_dedup_is_best_effort_after_eviction():
    # In-app dedup lives in the resident ledger. A fresh ledger (what eviction + re-creation
    # yields) re-emits the same tripwire. This is the accepted contract — the offline metric
    # dedups by (task_id, tripwire_id).
    resident = _TaskLedger()
    for _ in range(3):
        _record_step(resident, _fp(state="A"))
    assert "no_progress" in resident.fired

    refreshed = _TaskLedger()
    re_emitted: set[str] = set()
    for _ in range(3):
        re_emitted.update(tripwire for tripwire, _ in _record_step(refreshed, _fp(state="A")))
    assert "no_progress" in re_emitted


def test_recorder_is_wired_into_execute_step():
    # Guards against the seam call being silently removed by a future refactor.
    import inspect

    from skyvern.forge.agent import ForgeAgent

    assert "record_fail_fast_shadow" in inspect.getsource(ForgeAgent.execute_step)


def test_act_fps_exclude_non_web_actions_with_hallucinated_hash():
    # WAIT/COMPLETE/TERMINATE can carry a hallucinated skyvern_element_hash; they must not
    # count as element interactions for action_repetition.
    waits = [Action(action_type=ActionType.WAIT, skyvern_element_hash="hallucinated") for _ in range(3)]
    step = _make_step(output=AgentStepOutput(actions_and_results=[(a, []) for a in waits]))
    assert _build_fingerprint(step, None).act_fps == ()


def test_act_fps_include_web_actions():
    clicks = [Action(action_type=ActionType.CLICK, skyvern_element_hash="h1") for _ in range(2)]
    step = _make_step(output=AgentStepOutput(actions_and_results=[(a, []) for a in clicks]))
    assert len(_build_fingerprint(step, None).act_fps) == 2


def test_illegit_streak_breaks_on_unknown_latest_state():
    # Three illegit-completes in state A, but the most-recent step has unknown (None) state:
    # we can't confirm it's the same stuck state, so the streak must NOT fire.
    seq = [
        _fp(state="A", illegit=True),
        _fp(state="A", illegit=True),
        _fp(state="A", illegit=True),
        _fp(state=None, illegit=True),
    ]
    assert "illegit_complete_streak" not in dict(_evaluate(_ledger(seq)))
