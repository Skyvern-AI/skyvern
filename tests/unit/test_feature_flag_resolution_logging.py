import json
from typing import Any

import pytest

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.experimentation import providers as providers_module
from skyvern.forge.sdk.experimentation.providers import BaseExperimentationProvider


class CaptureLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def debug(self, event: str, **kwargs: Any) -> None:
        self.records.append(("debug", event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.records.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.records.append(("warning", event, kwargs))

    def exception(self, event: str, **kwargs: Any) -> None:
        self.records.append(("exception", event, kwargs))


class FakeExperimentationProvider(BaseExperimentationProvider):
    def __init__(
        self,
        *,
        enabled_results: list[bool] | None = None,
        value_results: list[str | None] | None = None,
        payload_results: list[Any] | None = None,
    ) -> None:
        super().__init__()
        self.enabled_results = list(enabled_results or [])
        self.value_results = list(value_results or [])
        self.payload_results = list(payload_results or [])
        self.enabled_calls: list[tuple[str, str, dict | None]] = []
        self.value_calls: list[tuple[str, str, dict | None]] = []
        self.payload_calls: list[tuple[str, str, dict | None]] = []

    async def _is_feature_enabled(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> bool:
        self.enabled_calls.append((feature_name, distinct_id, properties))
        resolved_value = self.enabled_results.pop(0)
        return resolved_value

    async def _get_value(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> str | None:
        self.value_calls.append((feature_name, distinct_id, properties))
        resolved_value = self.value_results.pop(0)
        return resolved_value

    async def _get_payload(self, feature_name: str, distinct_id: str, properties: dict | None = None) -> Any:
        self.payload_calls.append((feature_name, distinct_id, properties))
        resolved_value = self.payload_results.pop(0)
        return resolved_value


@pytest.fixture(autouse=True)
def reset_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


def _set_context() -> None:
    skyvern_context.set(
        SkyvernContext(
            request_id="req_123",
            organization_id="ctx_org",
            task_id="tsk_123",
            workflow_run_id="wr_123",
            workflow_permanent_id="wfp_123",
            browser_session_id="pbs_123",
        )
    )


def _set_logger(monkeypatch: pytest.MonkeyPatch, logger: CaptureLogger) -> None:
    monkeypatch.setattr(skyvern_context, "LOG", logger)
    monkeypatch.setattr(providers_module, "LOG", logger)


def test_record_workflow_feature_flags_uses_context_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(
        SkyvernContext(
            request_id="req_123",
            organization_id="org_123",
            workflow_run_id="wr_123",
            workflow_permanent_id="wfp_123",
            task_id="tsk_123",
            browser_session_id="pbs_123",
        )
    )

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="value",
        resolved_value="variant-a",
    )

    skyvern_context.reset()

    assert [event for _, event, _ in logger.records if event == "feature_flag_resolution"] == [
        "feature_flag_resolution"
    ]
    _, event, fields = logger.records[0]
    assert event == "feature_flag_resolution"
    assert fields["feature_name"] == "TEST_FLAG"
    assert fields["resolution_kind"] == "value"
    assert fields["resolved_value"] == "variant-a"
    assert fields["organization_id"] == "org_123"
    assert fields["workflow_run_id"] == "wr_123"
    assert fields["workflow_permanent_id"] == "wfp_123"
    assert fields["task_id"] == "tsk_123"
    assert fields["browser_session_id"] == "pbs_123"
    assert fields["request_id"] == "req_123"

    _, event, fields = logger.records[1]
    assert event == "workflow_feature_flags"
    assert fields["organization_id"] == "org_123"
    assert fields["workflow_run_id"] == "wr_123"
    assert fields["workflow_permanent_id"] == "wfp_123"
    assert fields["task_id"] == "tsk_123"
    assert fields["browser_session_id"] == "pbs_123"
    assert fields["request_id"] == "req_123"
    assert fields["feature_resolutions"] == {"TEST_FLAG": "variant-a"}


@pytest.mark.asyncio
async def test_cached_enabled_logging_is_normalized_and_property_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    _set_context()

    provider = FakeExperimentationProvider(enabled_results=[True, False])
    first_properties = {
        "task_url": "https://first.example.com/path?foo=bar",
        "organization_id": "org_123",
        "alpha": "1",
    }
    second_properties = {
        "task_url": "https://second.example.com/other",
        "organization_id": "org_123",
        "alpha": "1",
    }

    assert await provider.is_feature_enabled_cached("TEST_FLAG", "distinct_123", first_properties) is True
    assert await provider.is_feature_enabled_cached("TEST_FLAG", "distinct_123", second_properties) is False
    assert await provider.is_feature_enabled_cached("TEST_FLAG", "distinct_123", second_properties) is False

    assert len(provider.enabled_calls) == 2

    skyvern_context.reset()

    debug_records = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"]
    assert len(debug_records) == 3
    assert debug_records[0]["resolved_value"] is True
    assert debug_records[1]["resolved_value"] is False
    assert debug_records[2]["resolved_value"] is False
    assert debug_records[0]["organization_id"] == "ctx_org"
    assert debug_records[0]["workflow_run_id"] == "wr_123"
    assert debug_records[0]["workflow_permanent_id"] == "wfp_123"
    assert debug_records[0]["task_id"] == "tsk_123"
    assert debug_records[0]["browser_session_id"] == "pbs_123"

    _, event, fields = logger.records[-1]
    assert event == "workflow_feature_flags"
    assert fields["workflow_run_id"] == "wr_123"
    assert fields["feature_resolutions"] == {"TEST_FLAG": False}
    assert "task_v2_id" not in fields


@pytest.mark.asyncio
async def test_payload_logging_serializes_payload_values(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    _set_context()

    provider = FakeExperimentationProvider(
        payload_results=[
            {
                "cpu": "1",
                "memory": "4096",
                "mode": "burst",
                "nested": {"region": "us-east-1"},
                "items": [{"kind": "worker", "size": "large"}],
            }
        ]
    )

    payload = await provider.get_payload_cached(
        "WORKFLOW_COMPUTE_PROFILE",
        "distinct_123",
        {"organization_id": "org_456", "task_url": "https://secure.example.com/run/1"},
    )

    assert isinstance(payload, dict)

    skyvern_context.reset()

    debug_records = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"]
    assert len(debug_records) == 1
    assert debug_records[0]["feature_name"] == "WORKFLOW_COMPUTE_PROFILE"
    assert debug_records[0]["resolution_kind"] == "payload"
    assert debug_records[0]["organization_id"] == "ctx_org"

    _, event, fields = logger.records[-1]
    assert event == "workflow_feature_flags"
    serialized_payload = json.loads(fields["feature_resolutions"]["WORKFLOW_COMPUTE_PROFILE"])
    assert serialized_payload["cpu"] == "1"
    assert serialized_payload["memory"] == "4096"
    assert serialized_payload["mode"] == "burst"
    assert serialized_payload["nested"]["region"] == "us-east-1"
    assert serialized_payload["items"][0]["kind"] == "worker"


@pytest.mark.asyncio
async def test_direct_experimentation_methods_emit_feature_resolution_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    _set_context()

    provider = FakeExperimentationProvider(
        enabled_results=[True],
        value_results=["variant-a"],
        payload_results=[{"mode": "burst"}],
    )

    assert await provider.is_feature_enabled("DIRECT_ENABLED", "distinct_123", {"organization_id": "org_123"}) is True
    assert await provider.get_value("DIRECT_VALUE", "distinct_123", {"organization_id": "org_123"}) == "variant-a"
    assert await provider.get_payload("DIRECT_PAYLOAD", "distinct_123", {"organization_id": "org_123"}) == {
        "mode": "burst"
    }

    skyvern_context.reset()

    debug_records = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"]
    assert len(debug_records) == 3
    assert debug_records[0]["feature_name"] == "DIRECT_ENABLED"
    assert debug_records[0]["resolved_value"] is True
    assert debug_records[1]["feature_name"] == "DIRECT_VALUE"
    assert debug_records[1]["resolved_value"] == "variant-a"
    assert debug_records[2]["feature_name"] == "DIRECT_PAYLOAD"
    assert debug_records[2]["resolved_value"] == {"mode": "burst"}

    summary_fields = [fields for _, event, fields in logger.records if event == "workflow_feature_flags"][0]
    assert summary_fields["feature_resolutions"] == {
        "DIRECT_ENABLED": True,
        "DIRECT_PAYLOAD": json.dumps({"mode": "burst"}, sort_keys=True),
        "DIRECT_VALUE": "variant-a",
    }


@pytest.mark.asyncio
async def test_cache_hit_resolutions_only_appear_in_summary_per_workflow_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    provider = FakeExperimentationProvider(enabled_results=[False])
    properties = {"organization_id": "org_123"}

    skyvern_context.set(
        SkyvernContext(
            request_id="req_123",
            organization_id="org_123",
            task_id="tsk_123",
            workflow_run_id="wr_123",
            workflow_permanent_id="wfp_123",
        )
    )
    assert await provider.is_feature_enabled_cached("TEST_FLAG", "org_123", properties) is False
    assert await provider.is_feature_enabled_cached("TEST_FLAG", "org_123", properties) is False
    assert await provider.is_feature_enabled_cached("TEST_FLAG", "org_123", properties) is False

    debug_records = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"]
    assert len(debug_records) == 3
    assert all(record["resolved_value"] is False for record in debug_records)

    skyvern_context.reset()

    first_summary = [fields for _, event, fields in logger.records if event == "workflow_feature_flags"][0]
    assert first_summary["workflow_run_id"] == "wr_123"
    assert first_summary["feature_resolutions"] == {"TEST_FLAG": False}

    skyvern_context.set(
        SkyvernContext(
            request_id="req_456",
            organization_id="org_123",
            task_id="tsk_456",
            workflow_run_id="wr_456",
            workflow_permanent_id="wfp_123",
        )
    )
    assert await provider.is_feature_enabled_cached("TEST_FLAG", "org_123", properties) is False

    debug_records_after_second_run = [
        fields for _, event, fields in logger.records if event == "feature_flag_resolution"
    ]
    assert len(debug_records_after_second_run) == 4
    assert all(record["resolved_value"] is False for record in debug_records_after_second_run)

    skyvern_context.reset()

    summary_records = [fields for _, event, fields in logger.records if event == "workflow_feature_flags"]
    assert len(summary_records) == 2
    assert summary_records[1]["workflow_run_id"] == "wr_456"
    assert summary_records[1]["feature_resolutions"] == {"TEST_FLAG": False}


def test_workflow_feature_flags_reads_metadata_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(
        SkyvernContext(
            request_id="req_123",
            organization_id="org_123",
            workflow_run_id="wr_123",
            workflow_permanent_id="wfp_123",
            task_id="tsk_123",
        )
    )

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="value",
        resolved_value="variant-a",
    )

    skyvern_context.reset()

    debug_fields = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"][0]
    assert debug_fields["organization_id"] == "org_123"
    assert debug_fields["workflow_run_id"] == "wr_123"
    assert debug_fields["workflow_permanent_id"] == "wfp_123"
    assert debug_fields["task_id"] == "tsk_123"
    assert debug_fields["request_id"] == "req_123"

    summary_fields = [fields for _, event, fields in logger.records if event == "workflow_feature_flags"][0]
    assert summary_fields["organization_id"] == "org_123"
    assert summary_fields["workflow_run_id"] == "wr_123"
    assert summary_fields["workflow_permanent_id"] == "wfp_123"
    assert summary_fields["task_id"] == "tsk_123"
    assert summary_fields["request_id"] == "req_123"


def test_workflow_feature_flags_uses_last_effective_value_for_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(
        SkyvernContext(
            organization_id="org_123",
            workflow_run_id="wr_123",
            workflow_permanent_id="wfp_123",
        )
    )

    providers_module.record_feature_flag_resolution(
        feature_name="YESCAPTCHA_VERSION",
        resolution_kind="value",
        resolved_value=None,
    )
    providers_module.record_feature_flag_resolution(
        feature_name="YESCAPTCHA_VERSION",
        resolution_kind="value",
        resolved_value="1-3-0",
    )

    skyvern_context.reset()

    summary_fields = [fields for _, event, fields in logger.records if event == "workflow_feature_flags"][0]
    assert summary_fields["feature_resolutions"]["YESCAPTCHA_VERSION"] == "1-3-0"


@pytest.mark.asyncio
async def test_get_value_cached_caches_none_and_uses_sorted_properties_key() -> None:
    provider = FakeExperimentationProvider(value_results=[None, "variant-b"])
    properties_a = {"workflow_permanent_id": "wfp_123", "organization_id": "org_123"}
    properties_b = {"organization_id": "org_123", "workflow_permanent_id": "wfp_123"}
    properties_c = {"organization_id": "org_123", "workflow_permanent_id": "wfp_123", "task_url": "https://example.com"}

    assert await provider.get_value_cached("VALUE_FLAG", "distinct_123", properties_a) is None
    assert await provider.get_value_cached("VALUE_FLAG", "distinct_123", properties_b) is None
    assert await provider.get_value_cached("VALUE_FLAG", "distinct_123", properties_c) == "variant-b"

    assert provider.value_calls == [
        ("VALUE_FLAG", "distinct_123", properties_a),
        ("VALUE_FLAG", "distinct_123", properties_c),
    ]


@pytest.mark.asyncio
async def test_get_payload_cached_caches_none_payload_results() -> None:
    provider = FakeExperimentationProvider(payload_results=[None, {"mode": "burst"}])
    properties = {"organization_id": "org_123"}
    other_properties = {"organization_id": "org_123", "task_url": "https://example.com"}

    assert await provider.get_payload_cached("PAYLOAD_FLAG", "distinct_123", properties) is None
    assert await provider.get_payload_cached("PAYLOAD_FLAG", "distinct_123", properties) is None
    assert await provider.get_payload_cached("PAYLOAD_FLAG", "distinct_123", other_properties) == {"mode": "burst"}

    assert provider.payload_calls == [
        ("PAYLOAD_FLAG", "distinct_123", properties),
        ("PAYLOAD_FLAG", "distinct_123", other_properties),
    ]


@pytest.mark.asyncio
async def test_cached_helpers_use_independent_caches_per_resolution_kind() -> None:
    provider = FakeExperimentationProvider(
        enabled_results=[True],
        value_results=["variant-a"],
        payload_results=[{"mode": "burst"}],
    )
    properties = {"organization_id": "org_123"}

    assert await provider.is_feature_enabled_cached("SHARED_FLAG", "distinct_123", properties) is True
    assert await provider.is_feature_enabled_cached("SHARED_FLAG", "distinct_123", properties) is True
    assert await provider.get_value_cached("SHARED_FLAG", "distinct_123", properties) == "variant-a"
    assert await provider.get_value_cached("SHARED_FLAG", "distinct_123", properties) == "variant-a"
    assert await provider.get_payload_cached("SHARED_FLAG", "distinct_123", properties) == {"mode": "burst"}
    assert await provider.get_payload_cached("SHARED_FLAG", "distinct_123", properties) == {"mode": "burst"}

    assert provider.enabled_calls == [("SHARED_FLAG", "distinct_123", properties)]
    assert provider.value_calls == [("SHARED_FLAG", "distinct_123", properties)]
    assert provider.payload_calls == [("SHARED_FLAG", "distinct_123", properties)]


def test_request_only_context_does_not_emit_workflow_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(SkyvernContext(request_id="req_only", organization_id="org_123"))

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="enabled",
        resolved_value=False,
    )

    skyvern_context.reset()

    debug_fields = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"][0]
    assert debug_fields["request_id"] == "req_only"
    assert debug_fields["organization_id"] == "org_123"
    assert [event for _, event, _ in logger.records if event == "workflow_feature_flags"] == []


def test_task_only_context_emits_task_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(SkyvernContext(task_id="tsk_only", organization_id="org_123"))

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="enabled",
        resolved_value=False,
    )

    skyvern_context.reset()

    debug_fields = [fields for _, event, fields in logger.records if event == "feature_flag_resolution"][0]
    assert debug_fields["task_id"] == "tsk_only"
    assert debug_fields["organization_id"] == "org_123"

    assert [event for _, event, _ in logger.records if event == "workflow_feature_flags"] == []
    task_summaries = [fields for _, event, fields in logger.records if event == "task_feature_flags"]
    assert len(task_summaries) == 1
    summary = task_summaries[0]
    assert summary["task_id"] == "tsk_only"
    assert summary["organization_id"] == "org_123"
    assert summary["feature_resolutions"] == {"TEST_FLAG": False}
    assert "workflow_run_id" not in summary
    assert "workflow_permanent_id" not in summary


def test_task_v2_only_context_emits_task_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(SkyvernContext(task_v2_id="tv2_only", organization_id="org_123"))

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="value",
        resolved_value="variant-a",
    )

    skyvern_context.reset()

    task_summaries = [fields for _, event, fields in logger.records if event == "task_feature_flags"]
    assert len(task_summaries) == 1
    summary = task_summaries[0]
    assert summary["task_v2_id"] == "tv2_only"
    assert summary["feature_resolutions"] == {"TEST_FLAG": "variant-a"}
    assert "workflow_run_id" not in summary
    assert [event for _, event, _ in logger.records if event == "workflow_feature_flags"] == []


def test_workflow_with_task_emits_only_workflow_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(
        SkyvernContext(
            organization_id="org_123",
            workflow_run_id="wr_123",
            workflow_permanent_id="wfp_123",
            task_id="tsk_123",
        )
    )

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="enabled",
        resolved_value=True,
    )

    skyvern_context.reset()

    workflow_summaries = [fields for _, event, fields in logger.records if event == "workflow_feature_flags"]
    task_summaries = [fields for _, event, fields in logger.records if event == "task_feature_flags"]
    assert len(workflow_summaries) == 1
    assert len(task_summaries) == 0
    assert workflow_summaries[0]["workflow_run_id"] == "wr_123"
    assert workflow_summaries[0]["task_id"] == "tsk_123"
    assert workflow_summaries[0]["feature_resolutions"] == {"TEST_FLAG": True}


def test_run_id_only_context_emits_task_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = CaptureLogger()
    _set_logger(monkeypatch, logger)
    skyvern_context.set(SkyvernContext(run_id="run_only", organization_id="org_123"))

    providers_module.record_feature_flag_resolution(
        feature_name="TEST_FLAG",
        resolution_kind="enabled",
        resolved_value=True,
    )

    skyvern_context.reset()

    task_summaries = [fields for _, event, fields in logger.records if event == "task_feature_flags"]
    assert len(task_summaries) == 1
    assert task_summaries[0]["run_id"] == "run_only"
    assert task_summaries[0]["feature_resolutions"] == {"TEST_FLAG": True}
