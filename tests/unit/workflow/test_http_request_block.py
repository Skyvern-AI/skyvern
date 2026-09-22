import json
import socket
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urljoin

import aiohttp
import pytest
from aiohttp import web
from structlog.testing import capture_logs

from skyvern.forge.sdk.core import aiohttp_helper
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
    context.carried_block_labels = set()
    context.blocks_metadata = {}
    context.include_secrets_in_templates = False
    context.credential_totp_identifiers = {}
    context.workflow_title = "workflow"
    context.workflow_id = "workflow-1"
    context.workflow_permanent_id = "wpid-1"
    context.workflow_run_id = "wr-1"
    context.browser_session_id = None
    context.mask_secrets = False
    context.credential_tested_urls = {}
    return context


def _context_with_credential(tested_url: str | None) -> WorkflowRunContext:
    context = _make_context(
        secrets={"placeholder_AAAA_username": "agent@example.test", "placeholder_AAAA_password": "hunter2-secret"},
        values={
            "login_credentials": {
                "context": "These values are placeholders.",
                "username": "placeholder_AAAA_username",
                "password": "placeholder_AAAA_password",
            }
        },
    )
    if tested_url:
        context.credential_tested_urls = {"login_credentials": tested_url}
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
    async def test_execute_rejects_hostname_resolving_to_private_ip_before_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _make_context()
        block = _http_block(url="https://evil.example.test/api")
        db_mock = AsyncMock()
        client_session = MagicMock()

        def resolves_private(host: str, port: int | None, *args: object, **kwargs: object) -> list[object]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", port or 0))]

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr("skyvern.utils.url_validators.socket.getaddrinfo", resolves_private)
        monkeypatch.setattr("skyvern.forge.sdk.core.aiohttp_helper.aiohttp.ClientSession", client_session)
        monkeypatch.setattr(block_module.app, "DATABASE", db_mock)

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.status == BlockStatus.failed
        assert result.failure_reason is not None
        assert "blocked" in result.failure_reason
        client_session.assert_not_called()

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


class TestHttpRequestBlockCredentialSiteConfinement:
    @pytest.mark.asyncio
    async def test_execute_refuses_credential_sent_to_another_site(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://collector.example.net/ingest",
            body={"password": "{{ login_credentials.password }}"},
        )
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.status == BlockStatus.failed
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        assert "hunter2-secret" not in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_refuses_credential_in_a_header_to_another_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://collector.example.net/ingest",
            headers={"X-Token": "{{ login_credentials.password }}"},
        )
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_refuses_credential_rendered_into_a_header_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://collector.example.net/ingest",
            headers={"{{ login_credentials.password }}": "x"},
        )
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_sends_a_value_that_merely_contains_a_short_credential_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _make_context(
            secrets={"placeholder_AAAA_card_exp_year": "25"},
            values={
                "saved_card": {
                    "context": "These values are placeholders.",
                    "card_exp_year": "placeholder_AAAA_card_exp_year",
                }
            },
        )
        context.credential_tested_urls = {"saved_card": "https://login.example.com/login"}
        block = _http_block(url="https://api.example.net/v1/orders", body={"order": "sku-2590"})
        sent: dict[str, object] = {}

        async def fake_aiohttp_request(**kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            sent.update(kwargs)
            return 200, {"Content-Type": "application/json"}, {"ok": True}

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is True
        assert sent["data"] == {"order": "sku-2590"}

    @pytest.mark.asyncio
    async def test_execute_sends_a_request_that_merely_contains_the_username_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A login credential is in the run but the block never references it; its username
        text appearing in the URL path is a coincidence, not a release."""
        context = _make_context(
            secrets={"placeholder_AAAA_username": "admin", "placeholder_AAAA_password": "hunter2-secret"},
            values={
                "login_credentials": {
                    "context": "These values are placeholders.",
                    "username": "placeholder_AAAA_username",
                    "password": "placeholder_AAAA_password",
                }
            },
        )
        context.credential_tested_urls = {"login_credentials": "https://login.example.com/login"}
        block = _http_block(url="https://api.example.net/admin/report", method="GET")

        async def fake_aiohttp_request(**_kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            return 200, {"Content-Type": "application/json"}, {"ok": True}

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_refuses_a_short_credential_field_that_is_referenced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _make_context(
            secrets={"placeholder_AAAA_card_cvv": "123"},
            values={
                "saved_card": {"context": "These values are placeholders.", "card_cvv": "placeholder_AAAA_card_cvv"}
            },
        )
        context.credential_tested_urls = {"saved_card": "https://checkout.example.com/pay"}
        block = _http_block(url="https://collector.example.net/ingest", body={"cvv": "{{ saved_card.card_cvv }}"})
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "saved_card" in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_sends_an_unconfined_credential_that_shares_a_value_with_a_confined_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _make_context(
            secrets={"placeholder_AAAA_password": "same-secret", "placeholder_BBBB_password": "same-secret"},
            values={
                "cred_a": {"context": "These values are placeholders.", "password": "placeholder_AAAA_password"},
                "cred_b": {"context": "These values are placeholders.", "password": "placeholder_BBBB_password"},
            },
        )
        context.credential_tested_urls = {"cred_a": "https://login.example.com/login"}
        block = _http_block(url="https://api.example.net/token", body={"password": "{{ cred_b.password }}"})
        sent: dict[str, object] = {}

        async def fake_aiohttp_request(**kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            sent.update(kwargs)
            return 200, {"Content-Type": "application/json"}, {"ok": True}

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is True
        assert sent["data"] == {"password": "same-secret"}

    @pytest.mark.asyncio
    async def test_execute_refuses_a_same_site_file_url_whose_name_would_travel_off_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://collector.example.net/ingest",
            files={"doc": "https://login.example.com/export?download={{ login_credentials.password }}"},
        )
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_refuses_a_file_fetched_off_site_with_the_credential_in_its_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://api.example.com/upload",
            files={"doc": "https://collector.example.net/export?k={{ login_credentials.password }}"},
        )
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_refuses_a_credential_laundered_through_a_loop_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A for-loop over the credential parameter publishes its placeholder dict as current_item, so
        the template never names the credential; the surviving placeholder token still does."""
        context = _context_with_credential("https://login.example.com/login")
        context.values["current_item"] = dict(context.values["login_credentials"])
        block = _http_block(url="https://collector.example.net/ingest", body={"p": "{{ current_item.password }}"})
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        requested.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_holds_a_same_site_file_fetch_to_the_credential_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://api.example.com/upload",
            files={"doc": "https://login.example.com/export?k={{ login_credentials.password }}"},
        )
        fetched: dict[str, object] = {}

        async def fake_download_file(url: str, **kwargs: object) -> str:
            fetched["url"] = url
            fetched.update(kwargs)
            return "/nonexistent/doc.pdf"

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "download_file", fake_download_file)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        authorize = fetched["authorize_redirect"]
        assert callable(authorize)
        assert authorize("https://cdn.example.com/doc.pdf") is True
        assert authorize("https://collector.example.net/collect") is False

    @pytest.mark.asyncio
    async def test_execute_confines_only_the_file_entry_that_references_the_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://api.example.com/upload",
            files={
                "export": "https://login.example.com/export?k={{ login_credentials.password }}",
                "logo": "https://cdn.public.net/logo.png",
            },
        )
        fetched: dict[str, object] = {}

        async def fake_download_file(url: str, **kwargs: object) -> str:
            fetched[url] = kwargs.get("authorize_redirect")
            return "/nonexistent/file"

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "download_file", fake_download_file)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert "Refused" not in (result.failure_reason or "")
        assert fetched["https://cdn.public.net/logo.png"] is None
        authorize = fetched["https://login.example.com/export?k=hunter2-secret"]
        assert callable(authorize)
        assert authorize("https://collector.example.net/x") is False

    @pytest.mark.asyncio
    async def test_execute_renders_a_templated_file_field_name_and_keeps_its_entry_confined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = _context_with_credential("https://login.example.com/login")
        context.values["field_name"] = "export"
        block = _http_block(
            url="https://api.example.com/upload",
            files={"{{ field_name }}": "https://login.example.com/export?k={{ login_credentials.password }}"},
        )
        fetched: dict[str, object] = {}

        async def fake_download_file(url: str, **kwargs: object) -> str:
            fetched[url] = kwargs.get("authorize_redirect")
            return "/nonexistent/file"

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "download_file", fake_download_file)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert block.files is not None
        assert list(block.files) == ["export"]
        authorize = fetched["https://login.example.com/export?k=hunter2-secret"]
        assert callable(authorize)
        assert authorize("https://collector.example.net/x") is False

    @pytest.mark.asyncio
    async def test_execute_refuses_a_scheme_less_file_url_off_site(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://api.example.com/upload",
            files={"doc": "www.collector.example.net/export?k={{ login_credentials.password }}"},
        )
        requested = MagicMock()
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", requested)
        monkeypatch.setattr(block_module, "download_file", AsyncMock(return_value="/nonexistent/file"))
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is False
        assert result.failure_reason is not None
        assert "login_credentials" in result.failure_reason
        requested.assert_not_called()

    def test_unarmed_log_names_only_credentials_the_block_references(self) -> None:
        context = _make_context(
            secrets={"placeholder_AAAA_password": "a", "placeholder_BBBB_password": "b"},
            values={
                "referenced": {"context": "These values are placeholders.", "password": "placeholder_AAAA_password"},
                "elsewhere": {"context": "These values are placeholders.", "password": "placeholder_BBBB_password"},
            },
        )
        block = _http_block(body={"p": "{{ referenced.password }}"})

        with capture_logs() as logs:
            references = block._confined_credential_references(context)

        assert references == []
        unarmed = [log for log in logs if log["event"] == "http_request_credential_release_unarmed"]
        assert [log["parameter_key"] for log in unarmed] == ["referenced"]

    @pytest.mark.asyncio
    async def test_execute_refuses_a_redirect_off_the_credential_site(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real 307 from the credential's own origin to another one, through the real request loop;
        only the SSRF address pinning is bypassed so a loopback server can stand in for both."""
        received: dict[str, object] = {}

        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPTemporaryRedirect(location=f"http://localhost:{request.url.port}/collect")

        async def collect(request: web.Request) -> web.Response:
            received["headers"] = dict(request.headers)
            received["body"] = await request.text()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/redirect", redirect)
        app.router.add_post("/collect", collect)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        async def unpinned_fetch(url: str, *_args: object, **_kwargs: object) -> str:
            return url

        async def unpinned_redirect(url: str, location: str, *_args: object, **_kwargs: object) -> str:
            return urljoin(url, location)

        monkeypatch.setattr(aiohttp_helper, "validate_and_pin_fetch_url", unpinned_fetch)
        monkeypatch.setattr(aiohttp_helper, "validate_and_pin_redirect_url", unpinned_redirect)
        monkeypatch.setattr(aiohttp_helper, "ssrf_guarded_tcp_connector", lambda *_a, **_k: aiohttp.TCPConnector())
        context = _context_with_credential(f"http://127.0.0.1:{port}/login")
        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())
        block = _http_block(
            url=f"http://127.0.0.1:{port}/redirect",
            headers={"X-Api-Key": "{{ login_credentials.password }}", "Content-Type": "application/json"},
            body={"password": "{{ login_credentials.password }}"},
        )

        try:
            result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")
        finally:
            await runner.cleanup()

        assert result.success is False
        assert result.failure_reason is not None
        assert "Redirect blocked" in result.failure_reason
        assert "hunter2-secret" not in result.failure_reason
        assert received == {}

    @pytest.mark.asyncio
    async def test_execute_sends_credential_to_its_own_site(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(
            url="https://api.example.com/oauth/token",
            body={"password": "{{ login_credentials.password }}"},
        )
        sent: dict[str, object] = {}

        async def fake_aiohttp_request(**kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            sent.update(kwargs)
            return 200, {"Content-Type": "application/json"}, {"ok": True}

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is True
        assert sent["data"] == {"password": "hunter2-secret"}

    @pytest.mark.asyncio
    async def test_execute_allows_a_request_carrying_no_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = _context_with_credential("https://login.example.com/login")
        block = _http_block(url="https://collector.example.net/ingest", body={"note": "nothing secret here"})

        async def fake_aiohttp_request(**_kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            return 200, {"Content-Type": "application/json"}, {"ok": True}

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_sends_a_credential_without_a_tested_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        context = _context_with_credential(None)
        block = _http_block(
            url="https://api.example.com/oauth/token",
            body={"password": "{{ login_credentials.password }}"},
        )
        sent: dict[str, object] = {}

        async def fake_aiohttp_request(**kwargs: object) -> tuple[int, dict[str, str], dict[str, object]]:
            sent.update(kwargs)
            return 200, {"Content-Type": "application/json"}, {"ok": True}

        monkeypatch.setattr(HttpRequestBlock, "get_workflow_run_context", lambda _self, _workflow_run_id: context)
        monkeypatch.setattr(block_module, "aiohttp_request", fake_aiohttp_request)
        monkeypatch.setattr(block_module.app, "DATABASE", AsyncMock())

        result = await block.execute(workflow_run_id="wr-1", workflow_run_block_id="wrb-1")

        assert result.success is True
        assert sent["data"] == {"password": "hunter2-secret"}
