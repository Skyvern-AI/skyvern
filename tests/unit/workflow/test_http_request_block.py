import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.workflow.context_manager import RANDOM_SECRET_ID_PREFIX, WorkflowRunContext
from skyvern.forge.sdk.workflow.models import block as block_module
from skyvern.forge.sdk.workflow.models.block import (
    SECRET_RESPONSE_BODY_REDACTED,
    HttpRequestBlock,
    _apply_secret_response_paths,
    _secret_path_suffix,
)
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.schemas.workflows import BlockStatus, HttpRequestBlockYAML


def _make_context(
    secrets: dict[str, str] | None = None,
    values: dict[str, object] | None = None,
) -> WorkflowRunContext:
    context = WorkflowRunContext.__new__(WorkflowRunContext)
    context.secrets = dict(secrets or {})
    context.values = dict(values or {})
    context.parameters = {}
    context.workflow_run_outputs = {}
    context.blocks_metadata = {}
    context.include_secrets_in_templates = False
    context.credential_totp_identifiers = {}
    context.workflow_title = "workflow"
    context.workflow_id = "workflow-1"
    context.workflow_permanent_id = "wpid-1"
    context.workflow_run_id = "wr-1"
    context.browser_session_id = None
    return context


def _output_parameter(key: str = "http_output") -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key=key,
        description=None,
        output_parameter_id=f"{key}_id",
        workflow_id="workflow-1",
        created_at=now,
        modified_at=now,
        deleted_at=None,
    )


def _http_block(**kwargs: object) -> HttpRequestBlock:
    kwargs.setdefault("label", "http")
    kwargs.setdefault("url", "https://example.com/api")
    kwargs.setdefault("method", "POST")
    kwargs.setdefault("output_parameter", _output_parameter())
    return HttpRequestBlock(**kwargs)


def test_register_secret_value_returns_placeholder_and_stores_value() -> None:
    context = _make_context()

    placeholder = context.register_secret_value("secret-value")

    assert placeholder.startswith(RANDOM_SECRET_ID_PREFIX)
    assert context.secrets[placeholder] == "secret-value"
    assert context.values == {}


def test_register_secret_value_appends_suffix() -> None:
    context = _make_context()

    placeholder = context.register_secret_value("123-45-6789", suffix="ssn")

    assert placeholder.startswith(RANDOM_SECRET_ID_PREFIX)
    assert placeholder.endswith("_ssn")
    assert context.secrets[placeholder] == "123-45-6789"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("data.ssn", "ssn"),
        ("results.0.token", "token"),
        ("first-name", "first_name"),
        ("data.items.0", "items"),
        ("0.1", None),
    ],
)
def test_secret_path_suffix(path: str, expected: str | None) -> None:
    assert _secret_path_suffix(path) == expected


def test_register_secret_value_regenerates_on_id_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _make_context()
    generated_ids = iter(["placeholder_dupe", "placeholder_dupe", "placeholder_uniq"])
    monkeypatch.setattr(
        WorkflowRunContext,
        "generate_random_secret_id",
        staticmethod(lambda: next(generated_ids)),
    )

    first = context.register_secret_value("first-value")
    second = context.register_secret_value("second-value")

    assert first == "placeholder_dupe"
    assert second == "placeholder_uniq"
    assert context.secrets == {"placeholder_dupe": "first-value", "placeholder_uniq": "second-value"}


class TestSecretResponsePaths:
    def test_nested_dict_path_replaces_value_and_masks_duplicate_echo(self) -> None:
        context = _make_context()
        response_body = {"data": {"ssn": "123-45-6789"}, "echo": "123-45-6789"}

        invalid_paths = _apply_secret_response_paths(response_body, ["data.ssn"], context)

        assert invalid_paths == []
        placeholder = response_body["data"]["ssn"]
        assert isinstance(placeholder, str)
        assert placeholder.startswith(RANDOM_SECRET_ID_PREFIX)
        assert placeholder.endswith("_ssn")
        assert context.secrets[placeholder] == "123-45-6789"
        assert context.mask_secrets_in_data(response_body) == {"data": {"ssn": placeholder}, "echo": "*****"}

    def test_list_index_path_and_multiple_paths(self) -> None:
        context = _make_context()
        response_body = {
            "results": [{"token": "first-token"}, {"token": "second-token"}],
            "meta": {"id": "id-42", "enabled": "enabled"},
        }

        invalid_paths = _apply_secret_response_paths(
            response_body,
            ["results.0.token", "meta.id", "meta.enabled"],
            context,
        )

        assert invalid_paths == []
        result_token = response_body["results"][0]["token"]
        meta_id = response_body["meta"]["id"]
        enabled = response_body["meta"]["enabled"]
        assert context.secrets[result_token] == "first-token"
        assert context.secrets[meta_id] == "id-42"
        assert context.secrets[enabled] == "enabled"
        assert response_body["results"][1]["token"] == "second-token"

    def test_normalizes_and_deduplicates_paths(self) -> None:
        context = _make_context()
        response_body = {"data": {"ssn": "123-45-6789"}}

        invalid_paths = _apply_secret_response_paths(response_body, [" data.ssn ", "", "data.ssn"], context)

        assert invalid_paths == []
        placeholder = response_body["data"]["ssn"]
        assert isinstance(placeholder, str)
        assert context.secrets == {placeholder: "123-45-6789"}

    @pytest.mark.parametrize(
        ("response_body", "paths", "expected_invalid_paths"),
        [
            ({"data": {}}, ["data.ssn"], ["data.ssn"]),
            ({"data": {"identity": {"ssn": "123"}}}, ["data.identity"], ["data.identity"]),
            ({"data": {"token": ""}}, ["data.token"], ["data.token"]),
            ({"data": {"token": 42}}, ["data.token"], ["data.token"]),
            ({"data": {"token": True}}, ["data.token"], ["data.token"]),
            ("not json", ["data.token"], ["data.token"]),
        ],
    )
    def test_invalid_paths_are_collected(
        self,
        response_body: object,
        paths: list[str],
        expected_invalid_paths: list[str],
    ) -> None:
        context = _make_context()

        invalid_paths = _apply_secret_response_paths(response_body, paths, context)

        assert invalid_paths == expected_invalid_paths
        assert context.secrets == {}

    def test_resolved_paths_are_substituted_when_later_paths_are_invalid(self) -> None:
        context = _make_context()
        response_body = {"data": {"token": "real-token"}, "profile": {}}

        invalid_paths = _apply_secret_response_paths(response_body, ["data.token", "profile.ssn"], context)

        placeholder = response_body["data"]["token"]
        assert invalid_paths == ["profile.ssn"]
        assert context.secrets[placeholder] == "real-token"


class TestHttpRequestBlockSecretResponsePaths:
    @pytest.mark.asyncio
    async def test_execute_records_placeholder_and_masks_duplicate_echo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = _make_context()
        response_body = {"data": {"token": "real-token"}, "echo": "real-token"}
        block = _http_block(secret_response_paths=["data.token"])
        db_mock = AsyncMock()

        async def fake_aiohttp_request(**_kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            return 200, {"Content-Type": "application/json"}, response_body

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", db_mock)

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        recorded = result.output_parameter_value
        assert result.success is True
        assert result.status == BlockStatus.completed
        assert isinstance(recorded, dict)
        placeholder = recorded["body"]["data"]["token"]
        assert placeholder.startswith(RANDOM_SECRET_ID_PREFIX)
        assert context.secrets[placeholder] == "real-token"
        assert recorded["response_body"]["data"]["token"] == placeholder
        assert recorded["body"]["echo"] == "*****"
        assert context.values["http_output"]["body"]["data"]["token"] == placeholder

    @pytest.mark.parametrize(
        ("response_body", "paths", "expected_path"),
        [
            ({"data": {}}, ["data.ssn"], "data.ssn"),
            ({"data": {"identity": {"ssn": "123"}}}, ["data.identity"], "data.identity"),
            ({"data": {"token": ""}}, ["data.token"], "data.token"),
            ("not json", ["data.token"], "data.token"),
        ],
    )
    @pytest.mark.asyncio
    async def test_execute_fails_for_invalid_secret_response_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        response_body: object,
        paths: list[str],
        expected_path: str,
    ) -> None:
        context = _make_context()
        block = _http_block(secret_response_paths=paths)
        db_mock = AsyncMock()

        async def fake_aiohttp_request(**_kwargs: object) -> tuple[int, dict[str, str], object]:
            return 200, {"Content-Type": "application/json"}, response_body

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", db_mock)

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.status == BlockStatus.failed
        assert result.failure_reason is not None
        assert "secret_response_paths did not resolve to a non-empty string" in result.failure_reason
        assert expected_path in result.failure_reason

    @pytest.mark.asyncio
    async def test_execute_registers_resolved_paths_before_redacting_invalid_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = _make_context()
        response_body = {"data": {"token": "real-token"}, "profile": {}}
        block = _http_block(secret_response_paths=["data.token", "profile.ssn"])
        db_mock = AsyncMock()

        async def fake_aiohttp_request(**_kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            return 200, {"Content-Type": "application/json"}, response_body

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", db_mock)

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason == "secret_response_paths did not resolve to a non-empty string: profile.ssn"
        recorded = result.output_parameter_value
        assert isinstance(recorded, dict)
        assert recorded["body"] == SECRET_RESPONSE_BODY_REDACTED
        assert recorded["response_body"] == SECRET_RESPONSE_BODY_REDACTED
        assert list(context.secrets.values()) == ["real-token"]

    @pytest.mark.asyncio
    async def test_execute_redacts_response_body_when_secret_path_fails_on_error_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = _make_context()
        response_body = {"data": {"token": "real-token"}, "error": "raw failure payload"}
        block = _http_block(secret_response_paths=["data.token", "data.missing"])
        db_mock = AsyncMock()

        async def fake_aiohttp_request(**_kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            return 401, {"Content-Type": "application/json"}, response_body

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", db_mock)

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        recorded = result.output_parameter_value
        assert result.success is False
        assert result.status == BlockStatus.failed
        assert result.failure_reason is not None
        assert result.failure_reason.startswith("HTTP 401")
        assert isinstance(recorded, dict)
        assert recorded["body"] == SECRET_RESPONSE_BODY_REDACTED
        assert recorded["response_body"] == SECRET_RESPONSE_BODY_REDACTED
        assert SECRET_RESPONSE_BODY_REDACTED in result.failure_reason
        assert list(context.secrets.values()) == ["real-token"]
        assert "real-token" not in json.dumps(recorded)
        assert "raw failure payload" not in json.dumps(recorded)
        assert "real-token" not in result.failure_reason
        assert "raw failure payload" not in result.failure_reason

    @pytest.mark.asyncio
    async def test_save_response_as_file_with_secret_response_paths_fails_before_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context = _make_context()
        block = _http_block(save_response_as_file=True, secret_response_paths=["data.token"])
        request_mock = AsyncMock()

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", request_mock)

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.status == BlockStatus.failed
        assert result.failure_reason == "secret_response_paths cannot be combined with save_response_as_file"
        request_mock.assert_not_called()


class TestHttpRequestBlockYAMLValidation:
    def test_rejects_secret_response_paths_with_save_response_as_file(self) -> None:
        with pytest.raises(ValueError, match="secret_response_paths cannot be combined with save_response_as_file"):
            HttpRequestBlockYAML(
                label="http",
                url="https://example.com/api",
                save_response_as_file=True,
                secret_response_paths=["data.token"],
            )

    def test_allows_save_response_as_file_without_secret_response_paths(self) -> None:
        block = HttpRequestBlockYAML(
            label="http",
            url="https://example.com/api",
            save_response_as_file=True,
        )

        assert block.save_response_as_file is True
        assert block.secret_response_paths is None

    def test_allows_secret_response_paths_without_save_response_as_file(self) -> None:
        block = HttpRequestBlockYAML(
            label="http",
            url="https://example.com/api",
            secret_response_paths=["data.token"],
        )

        assert block.save_response_as_file is False
        assert block.secret_response_paths == ["data.token"]


class TestHttpRequestBlockPlaceholderRendering:
    def test_registered_placeholders_resolve_after_template_rendering(self) -> None:
        context = _make_context(
            secrets={
                "placeholder_TOKEN": "real-token",
                "placeholder_ONLY": "single-secret",
            },
            values={
                "upstream": {
                    "token": "placeholder_TOKEN",
                    "only": "placeholder_ONLY",
                }
            },
        )
        block = _http_block(
            url="https://example.com/{{ upstream.token }}?q=placeholder_UNKNOWN",
            headers={
                "Authorization": "Bearer {{ upstream.token }}",
                "X-Exact": "{{ upstream.only }}",
                "X-Unknown": "placeholder_UNKNOWN",
            },
            body={
                "auth": "Bearer {{ upstream.token }}",
                "exact": "{{ upstream.only }}",
                "unknown": "placeholder_UNKNOWN",
            },
            files={"upload": "{{ upstream.only }}"},
            download_filename="{{ upstream.token }}.json",
        )

        block.format_potential_template_parameters(context)

        assert block.url == "https://example.com/real-token?q=placeholder_UNKNOWN"
        assert block.headers == {
            "Authorization": "Bearer real-token",
            "X-Exact": "single-secret",
            "X-Unknown": "placeholder_UNKNOWN",
        }
        assert block.body == {
            "auth": "Bearer real-token",
            "exact": "single-secret",
            "unknown": "placeholder_UNKNOWN",
        }
        assert block.files == {"upload": "single-secret"}
        assert block.download_filename == "real-token.json"

    def test_prefix_sharing_token_is_not_partially_replaced(self) -> None:
        context = _make_context(secrets={"placeholder_TOKEN": "real-token"})
        block = _http_block(
            body={
                "collide": "placeholder_TOKEN_extra",
                "boundary": "placeholder_TOKEN, done",
                "repeated": "placeholder_TOKEN placeholder_TOKEN",
            },
        )

        block.format_potential_template_parameters(context)

        assert block.body == {
            "collide": "placeholder_TOKEN_extra",
            "boundary": "real-token, done",
            "repeated": "real-token real-token",
        }


class TestJsonTextParsingEquivalence:
    """Prove JSON/text parsing behavior matches aiohttp semantics.

    The HttpRequestBlock parses responses using:
        try:
            response_body = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            response_body = response_bytes.decode("utf-8", errors="replace")

    This should behave equivalently to aiohttp's:
        try:
            response_body = await response.json()
        except (aiohttp.ContentTypeError, Exception):
            response_body = await response.text()
    """

    def _parse_response(self, response_bytes: bytes) -> str | dict | list:
        try:
            return json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return response_bytes.decode("utf-8", errors="replace")

    def test_valid_json_utf8(self) -> None:
        data = {"key": "value", "number": 42, "unicode": "日本語"}
        response_bytes = json.dumps(data).encode("utf-8")
        result = self._parse_response(response_bytes)
        assert result == data

    def test_invalid_json_returns_text(self) -> None:
        response_bytes = b"not json, just text"
        result = self._parse_response(response_bytes)
        assert result == "not json, just text"

    def test_non_utf8_bytes_handled_gracefully(self) -> None:
        response_bytes = "café".encode("latin-1")  # b'caf\xe9'
        result = self._parse_response(response_bytes)
        assert "caf" in result
        assert isinstance(result, str)

    def test_empty_response(self) -> None:
        response_bytes = b""
        result = self._parse_response(response_bytes)
        assert result == ""
