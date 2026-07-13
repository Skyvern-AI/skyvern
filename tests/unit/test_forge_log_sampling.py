import pytest
import structlog

import skyvern.forge.sdk.forge_log as forge_log
from skyvern.config import settings
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import add_kv_pairs_to_msg, sample_logs_processor

SAMPLED_ORG = "o_sampled"


@pytest.fixture
def sampled_org(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(skyvern_context, "current", lambda: SkyvernContext(organization_id=SAMPLED_ORG))
    monkeypatch.setattr(settings, "LOG_SAMPLING_ORG_IDS", [SAMPLED_ORG])
    return SAMPLED_ORG


def _freeze_random(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    monkeypatch.setattr(forge_log.random, "random", lambda: value)


def test_drops_marked_info_for_sampled_org_above_rate(monkeypatch: pytest.MonkeyPatch, sampled_org: str) -> None:
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.1)
    _freeze_random(monkeypatch, 0.5)
    with pytest.raises(structlog.DropEvent):
        sample_logs_processor(None, "info", {"event": "noisy", "sampling": True})  # type: ignore[arg-type]


def test_keeps_marked_info_for_sampled_org_below_rate(monkeypatch: pytest.MonkeyPatch, sampled_org: str) -> None:
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.1)
    _freeze_random(monkeypatch, 0.05)
    result = sample_logs_processor(None, "info", {"event": "noisy", "sampling": True})  # type: ignore[arg-type]
    assert result["event"] == "noisy"
    assert "sampling" not in result


def test_keeps_marked_info_for_unsampled_org(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skyvern_context, "current", lambda: SkyvernContext(organization_id="o_other"))
    monkeypatch.setattr(settings, "LOG_SAMPLING_ORG_IDS", [SAMPLED_ORG])
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.0)
    _freeze_random(monkeypatch, 0.99)
    result = sample_logs_processor(None, "info", {"event": "noisy", "sampling": True})  # type: ignore[arg-type]
    assert result["event"] == "noisy"
    assert "sampling" not in result


def test_never_drops_warning_even_when_marked(monkeypatch: pytest.MonkeyPatch, sampled_org: str) -> None:
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.0)
    _freeze_random(monkeypatch, 0.99)
    result = sample_logs_processor(None, "warning", {"event": "important", "sampling": True})  # type: ignore[arg-type]
    assert result["event"] == "important"
    assert "sampling" not in result


def test_unmarked_info_passes_through_untouched(monkeypatch: pytest.MonkeyPatch, sampled_org: str) -> None:
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.0)
    _freeze_random(monkeypatch, 0.99)
    event = {"event": "keep me", "step_id": "stp_1"}
    result = sample_logs_processor(None, "info", event)  # type: ignore[arg-type]
    assert result == event


def test_empty_org_list_disables_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skyvern_context, "current", lambda: SkyvernContext(organization_id=SAMPLED_ORG))
    monkeypatch.setattr(settings, "LOG_SAMPLING_ORG_IDS", [])
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.0)
    _freeze_random(monkeypatch, 0.99)
    result = sample_logs_processor(None, "info", {"event": "noisy", "sampling": True})  # type: ignore[arg-type]
    assert result["event"] == "noisy"
    assert "sampling" not in result


def test_sampling_marker_excluded_from_rendered_message() -> None:
    event = {"msg": "Handling action", "sampling": True, "action_type": "click"}
    result = add_kv_pairs_to_msg(None, "info", event)  # type: ignore[arg-type]
    assert "sampling" not in result["msg"]
    assert "action_type=click" in result["msg"]


def test_no_context_keeps_marked_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skyvern_context, "current", lambda: None)
    monkeypatch.setattr(settings, "LOG_SAMPLING_ORG_IDS", [SAMPLED_ORG])
    monkeypatch.setattr(settings, "LOG_SAMPLING_RATE", 0.0)
    _freeze_random(monkeypatch, 0.99)
    result = sample_logs_processor(None, "info", {"event": "noisy", "sampling": True})  # type: ignore[arg-type]
    assert result["event"] == "noisy"
    assert "sampling" not in result
