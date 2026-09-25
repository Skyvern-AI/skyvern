from datetime import datetime, timezone

import pytest
import yaml
from structlog.testing import capture_logs

from skyvern.forge.sdk.copilot.code_block_steps import fill_code_block_error_code_mappings_in_yaml
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.tools.run_execution import _workflow_from_prior_draft
from skyvern.forge.sdk.copilot.tools.workflow_update import strip_copilot_yaml_headers
from skyvern.forge.sdk.copilot.workflow_yaml import (
    strip_private_workflow_settings,
    workflow_to_copilot_yaml,
)
from skyvern.forge.sdk.routes.workflow_copilot import (
    _blockless_submission_fallback,
    _ensure_copilot_workflow_yaml,
    _prior_copilot_workflow_yaml,
)
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatRequest
from skyvern.forge.sdk.workflow.models.block import FileDownloadBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, WorkflowParameter, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition
from skyvern.schemas.proxy_location import GeoTarget
from skyvern.schemas.runs import ProxyLocationInput
from tests.copilot_policy_support import (
    SCREEN_INTERRUPTED_DRAFT_YAML,
    SCREEN_INTERRUPTED_LABELS,
    screen_interrupted_proposal,
)

_PROXY_CASES = [
    pytest.param(None, False, id="null"),
    pytest.param("RESIDENTIAL", False, id="enum"),
    pytest.param({"url": "http://synthetic@proxy.example.test:8080"}, True, id="url"),
    pytest.param(
        {"server": "proxy.example.test", "username": "synthetic-user", "password": "synthetic-password"},
        True,
        id="mapping-without-url",
    ),
    pytest.param({}, True, id="empty-mapping"),
    pytest.param({"country": "invalid", "city": "synthetic-city"}, True, id="invalid-geo"),
    pytest.param({"country": "US", "password": "synthetic-password"}, True, id="geo-with-credentials"),
    pytest.param({"country": "us", "subdivision": "US-CA"}, False, id="geo-mapping"),
    pytest.param({"country": "US"}, False, id="geo-country"),
    pytest.param({"country": "US", "city": None, "subdivision": None}, False, id="geo-null-fields"),
    pytest.param({"country": "US", "isISP": True}, False, id="geo-isp"),
    pytest.param({"country": "US", "isISP": False}, False, id="geo-not-isp"),
    pytest.param(
        {"country": "US", "subdivision": "SECRET", "city": "http://synthetic:secret@example.test", "isISP": True},
        False,
        id="granular-geo-isp",
    ),
    pytest.param({"country": "US", "isISP": "synthetic"}, True, id="geo-invalid-isp"),
    pytest.param({"country": "US", "isISP": True, "token": "synthetic-token"}, True, id="geo-isp-with-unknown-key"),
    pytest.param(GeoTarget(country="US", subdivision="CA", city="Los Angeles"), False, id="geo-model"),
]


def test_invalid_geo_proxy_is_private_and_logs_only_fields_and_error_types() -> None:
    data = {"proxy_location": {"country": "XX"}}

    with capture_logs() as logs:
        assert strip_private_workflow_settings(data)

    assert "proxy_location" not in data
    assert logs == [
        {
            "event": "Withholding invalid geo proxy mapping from Copilot",
            "field_names": ["country"],
            "error_types": ["value_error"],
            "log_level": "warning",
        }
    ]


def _output_parameter(now: datetime) -> OutputParameter:
    return OutputParameter(
        output_parameter_id="op_block_2",
        workflow_id="w_saved",
        key="block_2_output",
        created_at=now,
        modified_at=now,
    )


def _saved_workflow() -> Workflow:
    now = datetime.now(timezone.utc)
    invoice_date = WorkflowParameter(
        workflow_parameter_id="wp_invoice_date",
        workflow_id="w_saved",
        key="invoice_date",
        workflow_parameter_type=WorkflowParameterType.STRING,
        created_at=now,
        modified_at=now,
    )
    block_output = _output_parameter(now)
    block = FileDownloadBlock(
        label="block_2",
        output_parameter=block_output,
        navigation_goal="Download the invoice for {{ invoice_date }}",
        parameters=[invoice_date],
        error_code_mapping={
            "DATA_UNAVAILABLE": ("only if the account exists but the invoice for {{ invoice_date }} is missing"),
        },
    )

    return Workflow(
        workflow_id="w_saved",
        organization_id="o_test",
        title="Saved workflow",
        workflow_permanent_id="wpid_test",
        version=3,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(
            parameters=[invoice_date, block_output],
            blocks=[block],
        ),
        created_at=now,
        modified_at=now,
    )


def _chat_request(workflow_yaml: str) -> WorkflowCopilotChatRequest:
    return WorkflowCopilotChatRequest(
        workflow_permanent_id="wpid_test",
        workflow_id="w_client",
        message="why did block_2 not trigger DATA_UNAVAILABLE?",
        workflow_yaml=workflow_yaml,
    )


def test_workflow_to_copilot_yaml_keeps_saved_blocks_without_runtime_fields() -> None:
    persisted_yaml = workflow_to_copilot_yaml(_saved_workflow())
    parsed = yaml.safe_load(persisted_yaml)

    blocks = parsed["workflow_definition"]["blocks"]
    assert blocks[0]["label"] == "block_2"
    assert blocks[0]["block_type"] == "file_download"
    assert blocks[0]["error_code_mapping"]["DATA_UNAVAILABLE"].startswith("only if the account exists")
    assert blocks[0]["parameter_keys"] == ["invoice_date"]
    assert "output_parameter" not in blocks[0]
    assert "parameters" not in blocks[0]
    assert all(parameter["parameter_type"] != "output" for parameter in parsed["workflow_definition"]["parameters"])


def test_ensure_copilot_workflow_yaml_uses_persisted_workflow_when_request_has_no_blocks() -> None:
    chat_request = _chat_request(
        """
title: Stale client workflow
workflow_definition:
  parameters: []
  blocks: []
"""
    )

    _ensure_copilot_workflow_yaml(chat_request, _saved_workflow())

    parsed = yaml.safe_load(chat_request.workflow_yaml)
    assert parsed["workflow_definition"]["blocks"][0]["label"] == "block_2"
    assert parsed["workflow_definition"]["blocks"][0]["block_type"] == "file_download"


@pytest.mark.parametrize("document", ["workflow_definition: [", "value: !!int invalid"])
@pytest.mark.parametrize("has_saved_blocks", [False, True])
def test_ensure_copilot_workflow_yaml_withholds_unparsable_client_and_proposal(
    document: str, has_saved_blocks: bool
) -> None:
    document = "totp_identifier: synthetic-private\n" + document
    chat_request = _chat_request(document)
    workflow = _saved_workflow()
    if not has_saved_blocks:
        workflow.workflow_definition.blocks = []
    captured = _ensure_copilot_workflow_yaml(chat_request, workflow)
    assert captured == {}
    if has_saved_blocks:
        parsed = yaml.safe_load(chat_request.workflow_yaml)
        assert parsed["workflow_definition"]["blocks"][0]["label"] == "block_2"
    else:
        assert chat_request.workflow_yaml == ""
    assert (
        _blockless_submission_fallback(proposed_workflow={"_copilot_yaml": document}, submitted_workflow_yaml=document)
        is None
    )
    assert _prior_copilot_workflow_yaml(
        proposed_workflow={"_copilot_yaml": document}, persisted_workflow_yaml=chat_request.workflow_yaml
    ) == (chat_request.workflow_yaml or None)
    assert "synthetic-private" not in (chat_request.workflow_yaml or "")


def test_ensure_copilot_workflow_yaml_ignores_persisted_workflow_without_definition() -> None:
    workflow = _saved_workflow()
    workflow.workflow_definition = None
    chat_request = _chat_request(
        """
title: Stale client workflow
workflow_definition:
  parameters: []
  blocks: []
"""
    )

    _ensure_copilot_workflow_yaml(chat_request, workflow)

    parsed = yaml.safe_load(chat_request.workflow_yaml)
    assert parsed["workflow_definition"]["blocks"] == []


def test_ensure_copilot_workflow_yaml_preserves_client_workflow_when_it_has_blocks() -> None:
    client_yaml = """
title: Client workflow
workflow_definition:
  parameters: []
  blocks:
    - block_type: goto_url
      label: client_block
      url: https://example.com
"""
    chat_request = _chat_request(client_yaml)

    _ensure_copilot_workflow_yaml(chat_request, _saved_workflow())

    assert chat_request.workflow_yaml == client_yaml


def _code_block_yaml(*, manifest: object = ...) -> str:
    block = {"block_type": "code", "label": "regenerated", "code": "return {'ok': True}"}
    if manifest is not ...:
        block["error_code_mapping"] = manifest
    return yaml.safe_dump({"workflow_definition": {"parameters": [], "blocks": [block]}}, sort_keys=False)


def test_code_block_regeneration_preserves_omitted_manifest_by_label() -> None:
    prior = _code_block_yaml(manifest={"ACCOUNT_LOCKED": "Account is locked"})

    result = fill_code_block_error_code_mappings_in_yaml(_code_block_yaml(), prior_yaml=prior)

    block = yaml.safe_load(result)["workflow_definition"]["blocks"][0]
    assert block["error_code_mapping"] == {"ACCOUNT_LOCKED": "Account is locked"}


@pytest.mark.parametrize("explicit_removal", [None, {}])
def test_code_block_regeneration_honors_explicit_manifest_removal(explicit_removal: object) -> None:
    prior = _code_block_yaml(manifest={"ACCOUNT_LOCKED": "Account is locked"})

    result = fill_code_block_error_code_mappings_in_yaml(_code_block_yaml(manifest=explicit_removal), prior_yaml=prior)

    block = yaml.safe_load(result)["workflow_definition"]["blocks"][0]
    assert "error_code_mapping" in block
    assert block["error_code_mapping"] == explicit_removal


@pytest.mark.asyncio
async def test_screen_interrupted_candidate_prior_draft_resolves_every_requested_label(
    no_saved_workflow: None,
) -> None:
    retained_yaml = _prior_copilot_workflow_yaml(
        proposed_workflow=screen_interrupted_proposal(),
        persisted_workflow_yaml=None,
    )
    ctx = CopilotContext(
        organization_id="o_test",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_yaml="",
        browser_session_id=None,
        stream=None,
        prior_copilot_workflow_yaml=retained_yaml or "",
    )

    resolved = await _workflow_from_prior_draft(ctx, SCREEN_INTERRUPTED_LABELS)

    assert retained_yaml == SCREEN_INTERRUPTED_DRAFT_YAML
    assert resolved is not None
    assert [block.label for block in resolved.workflow_definition.blocks] == SCREEN_INTERRUPTED_LABELS


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("proxy_location,private", _PROXY_CASES)
def test_backend_copilot_yaml_masks_header_keys_and_omits_other_private_fields(
    fallback: bool, proxy_location: ProxyLocationInput, private: bool
) -> None:
    workflow = _saved_workflow()
    workflow.extra_http_headers = {"X-Test": "stored-value"}
    workflow.cdp_connect_headers = {"X-CDP": "stored-cdp-value"}
    workflow.proxy_location = proxy_location
    workflow.totp_identifier = "saved-identifier"
    workflow.totp_verification_url = "https://example.test/totp"
    workflow.webhook_callback_url = "https://synthetic:credential@example.test/private-hook?signature=synthetic"
    if fallback:
        request = _chat_request("")
        _ensure_copilot_workflow_yaml(request, workflow)
        document = request.workflow_yaml
    else:
        document = workflow_to_copilot_yaml(workflow)
    parsed = yaml.safe_load(document)
    assert parsed["extra_http_headers"] == {"X-Test": "***"}
    assert parsed["cdp_connect_headers"] == {"X-CDP": "***"}
    assert not {"totp_identifier", "totp_verification_url"} & parsed.keys()
    assert "webhook_callback_url" not in parsed
    assert "private-hook" not in document
    if private:
        assert "proxy_location" not in parsed
        assert "synthetic" not in document
    elif proxy_location is None or isinstance(proxy_location, str):
        assert parsed.get("proxy_location") == proxy_location
    else:
        expected = GeoTarget.model_validate(proxy_location)
        assert GeoTarget.model_validate(parsed["proxy_location"]) == expected


def test_empty_document_proposal_fallback_omits_private_fields() -> None:
    document = yaml.safe_dump(
        {
            "title": "Header test",
            "totp_identifier": "synthetic-identifier",
            "totp_verification_url": "https://example.test/totp",
            "webhook_callback_url": "https://example.test/private-hook?signature=synthetic",
            "extra_http_headers": {"X-Test": "stored-value"},
            "cdp_connect_headers": {"X-CDP": "stored-cdp-value"},
            "workflow_definition": {
                "parameters": [],
                "blocks": [{"label": "read", "block_type": "code", "code": "return 1"}],
            },
        }
    )
    fallback = _blockless_submission_fallback(proposed_workflow={"_copilot_yaml": document}, submitted_workflow_yaml="")
    assert fallback is not None
    assert "webhook_callback_url" not in fallback
    assert "private-hook" not in fallback
    parsed = yaml.safe_load(fallback)
    assert not {"totp_identifier", "totp_verification_url"} & parsed.keys()
    assert parsed["extra_http_headers"] == {"X-Test": "***"}
    assert parsed["cdp_connect_headers"] == {"X-CDP": "***"}


@pytest.mark.parametrize("has_blocks", [False, True])
def test_client_private_fields_are_stripped_before_early_return(has_blocks: bool) -> None:
    workflow = _saved_workflow()
    workflow.workflow_definition = None
    document = {
        "title": "Client draft",
        "extra_http_headers": {"X-Test": "synthetic-http"},
        "cdp_connect_headers": {"X-Test": "synthetic-cdp"},
        "totp_identifier": "synthetic-identifier",
        "totp_verification_url": "https://example.test/totp",
        "webhook_callback_url": "https://example.test/private-hook?signature=synthetic",
        "workflow_definition": {
            "blocks": [{"label": "read", "block_type": "code", "code": "return 1"}] if has_blocks else []
        },
    }
    request = _chat_request(yaml.safe_dump(document))
    captured = _ensure_copilot_workflow_yaml(request, workflow)
    expected_settings = {key: value for key, value in document.items() if key not in {"workflow_definition", "title"}}
    assert captured == expected_settings
    forwarded = yaml.safe_load(request.workflow_yaml)
    assert "webhook_callback_url" not in forwarded
    assert "private-hook" not in request.workflow_yaml
    assert not {"totp_identifier", "totp_verification_url"} & forwarded.keys()
    assert forwarded["extra_http_headers"] == {"X-Test": "***"}
    assert forwarded["cdp_connect_headers"] == {"X-Test": "***"}
    assert forwarded["workflow_definition"] == document["workflow_definition"]


def test_strip_private_fields_preserves_unicode_and_literal_code_scalar() -> None:
    document = """title: Café
extra_http_headers: {X-Test: synthetic}
totp_identifier: synthetic-identifier
workflow_definition:
  blocks:
  - block_type: code
    label: read
    code: |
      message = "café"
      return message
"""
    stripped = strip_copilot_yaml_headers(document)
    assert "Café" in stripped and 'message = "café"' in stripped
    code_node = yaml.compose(stripped).value[-1][1].value[0][1].value[0].value[-1][1]
    assert code_node.style == "|"
    assert yaml.safe_load(stripped)["workflow_definition"] == yaml.safe_load(document)["workflow_definition"]
    assert strip_copilot_yaml_headers(stripped) == stripped


@pytest.mark.parametrize("proxy_location,private", _PROXY_CASES)
def test_reused_and_client_yaml_strip_custom_proxy_location(proxy_location: ProxyLocationInput, private: bool) -> None:
    proxy_value = proxy_location.model_dump(mode="json") if isinstance(proxy_location, GeoTarget) else proxy_location
    document = yaml.safe_dump(
        {
            "title": "Proxy fixture",
            "proxy_location": proxy_value,
            "workflow_definition": {"blocks": [{"block_type": "code", "label": "read", "code": "return 1"}]},
        }
    )
    request = _chat_request(document)
    captured = _ensure_copilot_workflow_yaml(request, _saved_workflow())
    expected_settings = {"proxy_location": proxy_value}
    assert captured == expected_settings
    reused = _prior_copilot_workflow_yaml(
        proposed_workflow={"_copilot_yaml": document},
        persisted_workflow_yaml=None,
    )
    for result in (strip_copilot_yaml_headers(document), request.workflow_yaml, reused):
        parsed = yaml.safe_load(result)
        if private:
            assert "proxy_location" not in parsed
            assert "synthetic" not in result
        else:
            assert "proxy_location" in parsed
            assert parsed["proxy_location"] == proxy_value
