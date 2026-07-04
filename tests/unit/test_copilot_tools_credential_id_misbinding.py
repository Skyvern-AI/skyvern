from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import (
    _credential_id_misbinding_error_message,
    _credential_id_misbinding_findings,
    _list_credentials,
    _update_workflow,
)


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def test_credential_id_in_navigation_goal_is_flagged() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://authenticationtest.com/loginUserAndPassword/
            navigation_goal: Sign in with credential cred_527971855302737592 by entering its username and password.
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "login_to_portal",
            "field": "navigation_goal",
            "credential_id": "cred_527971855302737592",
        }
    ]


@pytest.mark.parametrize(
    "yaml",
    [
        pytest.param(
            _yaml(
                """
                title: Sign in
                workflow_definition:
                  parameters:
                  - key: login_credentials
                    parameter_type: workflow
                    workflow_parameter_type: credential_id
                    default_value: cred_527971855302737592
                  blocks:
                  - block_type: login
                    label: login_to_portal
                    url: https://authenticationtest.com/loginUserAndPassword/
                    parameter_keys: [login_credentials]
                """
            ),
            id="credential_id_bound_through_workflow_parameter_is_allowed",
        ),
        pytest.param(
            _yaml(
                """
                title: Sign in
                workflow_definition:
                  parameters: []
                  blocks:
                  - block_type: login
                    label: login_to_portal
                    url: https://authenticationtest.com/loginUserAndPassword/
                    parameters:
                    - parameter_type: credential
                      key: login_credentials
                      credential_id: cred_527971855302737592
                """
            ),
            id="credential_id_bound_through_block_credential_parameter_is_allowed",
        ),
        pytest.param(
            _yaml(
                """
                title: Sign in
                workflow_definition:
                  parameters:
                  - key: login_credentials
                    parameter_type: credential
                    credential_id: cred_527971855302737592
                    credential_ids:
                    - cred_527971855302737592
                    - cred_827971855302737593
                  blocks:
                  - block_type: login
                    label: login_to_portal
                    url: https://authenticationtest.com/loginUserAndPassword/
                    parameter_keys: [login_credentials]
                """
            ),
            id="credential_ids_bound_through_credential_parameter_are_allowed",
        ),
        pytest.param(
            _yaml(
                """
                title: Sign in
                workflow_definition:
                  parameters:
                  - key: login_credentials
                    parameter_type: workflow
                    workflow_parameter_type: credential_id
                    default_value: cred_527971855302737592
                  blocks:
                  - block_type: login
                    label: login_to_portal
                    navigation_goal: Sign in using {{ login_credentials }}.
                    parameter_keys: [login_credentials]
                """
            ),
            id="jinja_reference_to_credential_parameter_is_not_flagged",
        ),
        pytest.param(
            _yaml(
                """
                title: Visit
                workflow_definition:
                  parameters: []
                  blocks:
                  - block_type: navigation
                    label: visit
                    url: https://example.com/
                    navigation_goal: Open the homepage.
                """
            ),
            id="workflow_without_credential_ids_is_inert",
        ),
    ],
)
def test_legal_credential_id_binding_shapes_are_allowed(yaml: str) -> None:
    assert _credential_id_misbinding_findings(yaml) == []


def test_credential_parameter_unrelated_field_with_credential_id_is_still_flagged() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters:
          - key: login_credentials
            parameter_type: credential
            credential_id: cred_527971855302737592
            credential_ids:
            - cred_527971855302737592
            - cred_827971855302737593
            description: Use cred_927971855302737594 for this login.
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://authenticationtest.com/loginUserAndPassword/
            parameter_keys: [login_credentials]
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "workflow",
            "field": "description",
            "credential_id": "cred_927971855302737594",
        }
    ]


def test_credential_id_in_parameter_keys_list_is_flagged() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://authenticationtest.com/loginUserAndPassword/
            parameter_keys: [cred_527971855302737592]
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "login_to_portal",
            "field": "parameter_keys",
            "credential_id": "cred_527971855302737592",
        }
    ]


def test_credential_id_in_complete_and_terminate_criterion_is_flagged() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: login
            label: login_to_portal
            navigation_goal: Sign in to the portal.
            complete_criterion: Logged in as cred_527971855302737592.
            terminate_criterion: Abort if cred_527971855302737592 cannot be used.
        """
    )

    findings = _credential_id_misbinding_findings(yaml)
    fields = {(f["location"], f["field"]) for f in findings}

    assert fields == {
        ("login_to_portal", "complete_criterion"),
        ("login_to_portal", "terminate_criterion"),
    }


def test_credential_id_inside_loop_block_prose_is_flagged() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: for_loop
            label: outer
            loop_blocks:
            - block_type: login
              label: inner_login
              navigation_goal: Log in with cred_527971855302737592.
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "inner_login",
            "field": "navigation_goal",
            "credential_id": "cred_527971855302737592",
        }
    ]


def test_credential_id_in_block_url_prose_is_flagged() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://authenticationtest.com/loginUserAndPassword/?cred_527971855302737592
            navigation_goal: Sign in.
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "login_to_portal",
            "field": "url",
            "credential_id": "cred_527971855302737592",
        }
    ]


def test_credential_id_used_both_legally_and_in_prose_reports_only_the_misbinding() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters:
          - key: login_credentials
            parameter_type: workflow
            workflow_parameter_type: credential_id
            default_value: cred_527971855302737592
          blocks:
          - block_type: login
            label: login_to_portal
            navigation_goal: Sign in with cred_527971855302737592.
            parameter_keys: [login_credentials]
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "login_to_portal",
            "field": "navigation_goal",
            "credential_id": "cred_527971855302737592",
        }
    ]


def test_parameter_without_parameter_type_field_is_scanned() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters:
          - key: opaque
            description: stashed cred_527971855302737592 in a description
          blocks: []
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "workflow",
            "field": "description",
            "credential_id": "cred_527971855302737592",
        }
    ]


def test_multiple_distinct_credential_ids_in_same_field_are_all_reported() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: login
            label: login_to_portal
            navigation_goal: Try cred_111 then fall back to cred_222.
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert {f["credential_id"] for f in findings} == {"cred_111", "cred_222"}
    assert all(f["field"] == "navigation_goal" for f in findings)


def test_malformed_or_empty_yaml_is_inert() -> None:
    assert _credential_id_misbinding_findings("") == []
    assert _credential_id_misbinding_findings("- not a workflow yaml\n") == []
    assert _credential_id_misbinding_findings(":: broken yaml ::") == []


def test_error_message_names_block_label_field_and_credential_id() -> None:
    findings = [
        {
            "location": "login_to_portal",
            "field": "navigation_goal",
            "credential_id": "cred_527971855302737592",
        }
    ]

    message = _credential_id_misbinding_error_message(findings)

    assert "cred_527971855302737592" in message
    assert "navigation_goal" in message
    assert "login_to_portal" in message
    assert "credential parameter" in message.lower()


def test_error_message_steers_delete_not_relocate() -> None:
    """The message must tell the agent to delete the ID from prose fields, not
    relocate it — a relocate-only reading drives multi-iteration reject loops
    when the agent keeps moving the ID between criterion/goal fields."""
    findings = [
        {"location": "login_to_portal", "field": "complete_criterion", "credential_id": "cred_111"},
        {"location": "login_to_portal", "field": "terminate_criterion", "credential_id": "cred_111"},
    ]

    message = _credential_id_misbinding_error_message(findings)
    lowered = message.lower()

    assert "delete" in lowered
    assert "complete_criterion" in message
    assert "terminate_criterion" in message
    assert "parameter_keys" in message


def _ctx() -> MagicMock:
    ctx = MagicMock(spec=AgentContext)
    ctx.workflow_yaml = ""
    ctx.last_workflow_yaml = None
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    ctx.allow_untested_workflow_draft = False
    ctx.request_policy = None
    return ctx


@pytest.mark.asyncio
async def test_update_workflow_rejects_credential_id_in_navigation_goal() -> None:
    submitted = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters: []
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://authenticationtest.com/loginUserAndPassword/
            navigation_goal: Sign in with credential cred_527971855302737592 by entering its username and password.
        """
    )

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update._credential_reference_validation_error",
            new=AsyncMock(return_value=None),
        ),
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        result = await _update_workflow({"workflow_yaml": submitted}, _ctx())

    assert result["ok"] is False
    assert "cred_527971855302737592" in result["error"]
    assert "navigation_goal" in result["error"]
    assert "login_to_portal" in result["error"]
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_called()


@pytest.mark.asyncio
async def test_update_workflow_allows_credential_id_in_credential_parameter_slot() -> None:
    submitted = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters:
          - key: login_credentials
            parameter_type: workflow
            workflow_parameter_type: credential_id
            default_value: cred_527971855302737592
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://authenticationtest.com/loginUserAndPassword/
            parameter_keys: [login_credentials]
            navigation_goal: Sign in to the portal.
        """
    )

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update._credential_reference_validation_error",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update._process_workflow_yaml",
            new=AsyncMock(
                return_value=MagicMock(
                    title="Sign in",
                    description=None,
                    workflow_definition=MagicMock(),
                    proxy_location=None,
                    webhook_callback_url=None,
                    persist_browser_session=False,
                    browser_profile_id=None,
                    model=None,
                    max_screenshot_scrolls=None,
                    extra_http_headers=None,
                    run_with=None,
                    ai_fallback=None,
                    cache_key=None,
                    run_sequentially=None,
                    sequential_key=None,
                ),
            ),
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.workflow_update.resolve_copilot_created_by_stamp",
            new=AsyncMock(return_value="copilot"),
        ),
        patch("skyvern.forge.sdk.copilot.tools.workflow_update._record_workflow_proxy_location_span"),
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        mock_app.DATABASE = MagicMock()
        result = await _update_workflow({"workflow_yaml": submitted}, _ctx())

    error_text = str(result.get("error", ""))
    assert "credential ID appeared" not in error_text, error_text


def test_error_message_groups_multiple_findings() -> None:
    findings = [
        {
            "location": "login_to_portal",
            "field": "navigation_goal",
            "credential_id": "cred_111",
        },
        {
            "location": "login_to_portal",
            "field": "complete_criterion",
            "credential_id": "cred_111",
        },
    ]

    message = _credential_id_misbinding_error_message(findings)

    assert "navigation_goal" in message
    assert "complete_criterion" in message
    assert message.count("cred_111") >= 1


class _ListedCredential:
    def __init__(self, credential_id: str, name: str, tested_url: str | None = None) -> None:
        self.credential_id = credential_id
        self.name = name
        self.tested_url = tested_url
        self.credential_type = "password"
        self.username = None
        self.totp_type = None
        self.card_last4 = None
        self.card_brand = None
        self.secret_label = None


def _ctx_with_policy() -> MagicMock:
    ctx = _ctx()
    ctx.request_policy = RequestPolicy(credential_input_kind="none")
    return ctx


@pytest.mark.asyncio
async def test_list_credentials_records_discovered_credentials_on_policy() -> None:
    ctx = _ctx_with_policy()
    listed = [_ListedCredential("cred_site_a", "Site A"), _ListedCredential("cred_site_b", "Site B")]
    with patch(
        "skyvern.forge.sdk.copilot.tools.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=listed)
    ):
        await _list_credentials({}, ctx)

    assert [c.credential_id for c in ctx.request_policy.discovered_credentials] == ["cred_site_a", "cred_site_b"]


@pytest.mark.asyncio
async def test_list_credentials_unions_across_pages_without_duplicates() -> None:
    ctx = _ctx_with_policy()
    ctx.request_policy.discovered_credentials = [_ListedCredential("cred_site_a", "Site A")]
    listed = [_ListedCredential("cred_site_a", "Site A"), _ListedCredential("cred_site_b", "Site B")]
    with patch(
        "skyvern.forge.sdk.copilot.tools.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=listed)
    ):
        await _list_credentials({"page": 2}, ctx)

    assert [c.credential_id for c in ctx.request_policy.discovered_credentials] == ["cred_site_a", "cred_site_b"]


@pytest.mark.asyncio
async def test_list_credentials_without_request_policy_is_inert() -> None:
    ctx = _ctx()
    ctx.request_policy = None
    listed = [_ListedCredential("cred_site_a", "Site A")]
    with patch(
        "skyvern.forge.sdk.copilot.tools.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=listed)
    ):
        result = await _list_credentials({}, ctx)

    assert result["ok"] is True
