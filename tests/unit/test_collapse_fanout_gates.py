import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.actions import handler

UMBRELLA_FLAG = "COLLAPSE_XP_ASSIGNMENT"


class CaptureLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.records.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.records.append(("warning", event, kwargs))


class FakeExperimentationProvider:
    def __init__(self, results: dict[str, bool], *, raises_for: set[str] | None = None) -> None:
        self.results = results
        self.raises_for = raises_for or set()
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []

    async def is_feature_enabled_cached(
        self,
        feature_name: str,
        distinct_id: str,
        properties: dict[str, str],
    ) -> bool:
        self.calls.append(("cached", feature_name, distinct_id, properties))
        if feature_name in self.raises_for:
            raise RuntimeError("feature flag unavailable")
        return self.results[feature_name]

    async def resolve_feature_enabled_unrecorded(
        self,
        feature_name: str,
        distinct_id: str,
        properties: dict[str, str],
    ) -> bool:
        self.calls.append(("unrecorded", feature_name, distinct_id, properties))
        await asyncio.sleep(0)
        if feature_name in self.raises_for:
            raise RuntimeError("feature flag unavailable")
        return self.results[feature_name]


def _task(*, organization_id: str | None = "o_123", workflow_run_id: str | None = "wr_456") -> SimpleNamespace:
    return SimpleNamespace(
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        task_id="tsk_789",
    )


def _set_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeExperimentationProvider) -> None:
    monkeypatch.setattr(handler.app, "EXPERIMENTATION_PROVIDER", provider)


def _umbrella_calls(provider: FakeExperimentationProvider) -> list[tuple[str, str, str, dict[str, str]]]:
    return [call for call in provider.calls if call[1] == UMBRELLA_FLAG]


@pytest.mark.asyncio
async def test_collapse_family_off_skips_umbrella(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider({handler.COLLAPSE_SELECT_FANOUT_FLAG: False})
    _set_provider(monkeypatch, provider)

    assert await handler._is_collapse_select_fanout_enabled(_task()) is False
    assert provider.calls == [("cached", handler.COLLAPSE_SELECT_FANOUT_FLAG, "o_123", {"organization_id": "o_123"})]
    assert _umbrella_calls(provider) == []


@pytest.mark.asyncio
async def test_collapse_family_and_umbrella_on_uses_workflow_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)

    assert await handler._is_collapse_select_fanout_enabled(_task()) is True
    assert _umbrella_calls(provider) == [("unrecorded", UMBRELLA_FLAG, "wr_456", {"organization_id": "o_123"})]


@pytest.mark.asyncio
async def test_collapse_umbrella_falls_back_to_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)

    assert await handler._is_collapse_custom_select_fanout_enabled(_task(workflow_run_id=None)) is True
    assert _umbrella_calls(provider) == [("unrecorded", UMBRELLA_FLAG, "tsk_789", {"organization_id": "o_123"})]


@pytest.mark.asyncio
async def test_collapse_umbrella_off_keeps_control_path(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG: True,
            UMBRELLA_FLAG: False,
        }
    )
    _set_provider(monkeypatch, provider)

    assert await handler._is_collapse_autocomplete_fanout_enabled(_task()) is False


@pytest.mark.asyncio
async def test_collapse_family_error_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        },
        raises_for={handler.COLLAPSE_SELECT_FANOUT_FLAG},
    )
    _set_provider(monkeypatch, provider)

    assert await handler._is_collapse_select_fanout_enabled(_task()) is False
    assert _umbrella_calls(provider) == []


@pytest.mark.asyncio
async def test_collapse_missing_organization_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider({})
    _set_provider(monkeypatch, provider)

    assert await handler._is_collapse_select_fanout_enabled(_task(organization_id=None)) is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_collapse_gates_share_one_umbrella_cohort(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            handler.COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG: True,
            handler.COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)
    task = _task()

    with skyvern_context.scoped(SkyvernContext()):
        assert await handler._is_collapse_select_fanout_enabled(task) is True
        assert await handler._is_collapse_custom_select_fanout_enabled(task) is True
        assert await handler._is_collapse_autocomplete_fanout_enabled(task) is True

    assert _umbrella_calls(provider) == [("unrecorded", UMBRELLA_FLAG, "wr_456", {"organization_id": "o_123"})]


@pytest.mark.asyncio
async def test_collapse_umbrella_assignment_stays_sticky_for_run(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            handler.COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)
    task = _task()

    with skyvern_context.scoped(SkyvernContext()):
        assert await handler._is_collapse_select_fanout_enabled(task) is True
        provider.results[UMBRELLA_FLAG] = False
        assert await handler._is_collapse_select_fanout_enabled(task) is True
        assert await handler._is_collapse_custom_select_fanout_enabled(task) is True

    assert _umbrella_calls(provider) == [("unrecorded", UMBRELLA_FLAG, "wr_456", {"organization_id": "o_123"})]


@pytest.mark.asyncio
async def test_collapse_umbrella_assignment_survives_context_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            handler.COLLAPSE_CUSTOM_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)
    task = _task()

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=task.workflow_run_id)):
        assert await handler._is_collapse_select_fanout_enabled(task) is True
        skyvern_context.replace(SkyvernContext(workflow_run_id=task.workflow_run_id))
        provider.results[UMBRELLA_FLAG] = False
        assert await handler._is_collapse_select_fanout_enabled(task) is True
        assert await handler._is_collapse_custom_select_fanout_enabled(task) is True

    assert _umbrella_calls(provider) == [("unrecorded", UMBRELLA_FLAG, "wr_456", {"organization_id": "o_123"})]


@pytest.mark.asyncio
async def test_collapse_umbrella_never_records_against_ambient_child_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)
    logger = CaptureLogger()
    monkeypatch.setattr(handler, "LOG", logger)
    task = _task(workflow_run_id="wr_parent")

    with skyvern_context.scoped(SkyvernContext(workflow_run_id="wr_child")) as context:
        assert await handler._is_collapse_select_fanout_enabled(task) is True
        assert handler._COLLAPSE_XP_ASSIGNMENT_MEMO["wr_parent"] is True
        assert UMBRELLA_FLAG not in context.feature_flag_entries

    assert [record for record in logger.records if record[1] == "collapse_xp_assignment"] == [
        (
            "info",
            "collapse_xp_assignment",
            {
                "workflow_run_id": "wr_parent",
                "task_id": "tsk_789",
                "organization_id": "o_123",
                "assigned": True,
                "pinned_on_error": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_collapse_umbrella_first_writer_logs_once_without_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        }
    )
    _set_provider(monkeypatch, provider)
    logger = CaptureLogger()
    monkeypatch.setattr(handler, "LOG", logger)
    task = _task()

    assert await asyncio.gather(
        handler._is_collapse_select_fanout_enabled(task),
        handler._is_collapse_select_fanout_enabled(task),
    ) == [True, True]
    assert len(_umbrella_calls(provider)) == 2
    assert len([record for record in logger.records if record[1] == "collapse_xp_assignment"]) == 1


@pytest.mark.asyncio
async def test_collapse_umbrella_error_pins_run_to_control(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeExperimentationProvider(
        {
            handler.COLLAPSE_SELECT_FANOUT_FLAG: True,
            handler.COLLAPSE_AUTOCOMPLETE_FANOUT_FLAG: True,
            UMBRELLA_FLAG: True,
        },
        raises_for={UMBRELLA_FLAG},
    )
    _set_provider(monkeypatch, provider)
    logger = CaptureLogger()
    monkeypatch.setattr(handler, "LOG", logger)
    task = _task()

    with skyvern_context.scoped(SkyvernContext(workflow_run_id=task.workflow_run_id)) as context:
        assert await handler._is_collapse_select_fanout_enabled(task) is False
        assert UMBRELLA_FLAG not in context.feature_flag_entries
        provider.raises_for.remove(UMBRELLA_FLAG)
        assert await handler._is_collapse_select_fanout_enabled(task) is False
        assert await handler._is_collapse_autocomplete_fanout_enabled(task) is False

    assert _umbrella_calls(provider) == [("unrecorded", UMBRELLA_FLAG, "wr_456", {"organization_id": "o_123"})]
    assert [record for record in logger.records if record[1] == "collapse_xp_assignment"] == [
        (
            "info",
            "collapse_xp_assignment",
            {
                "workflow_run_id": "wr_456",
                "task_id": "tsk_789",
                "organization_id": "o_123",
                "assigned": False,
                "pinned_on_error": True,
            },
        )
    ]
