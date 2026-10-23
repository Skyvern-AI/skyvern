from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.forge.sdk.copilot.credential_resolution import connected_vault_credential
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools import (
    _credential_id_misbinding_findings,
    _credential_reference_validation_error,
    _list_credentials,
    _update_workflow,
)
from skyvern.forge.sdk.copilot.tools.account_groups import AccountGroupRefused, _check_common_inputs
from skyvern.forge.sdk.copilot.tools.credentials import (
    _credential_run_approval_error,
    _google_connection_reference_ids,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import bitwarden_secret_bindings
from tests.unit.conftest import ORG_VAULT_ITEM_ID, ORG_VAULT_LOGIN_ID, OrgVaultFake


def _yaml(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def test_credential_id_in_navigation_goal_is_flagged() -> None:
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
            engine: skyvern-3.0
            url: https://authenticationtest.com/loginUserAndPassword/
            parameter_keys: [login_credentials]
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
            engine: skyvern-3.0
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
            engine: skyvern-3.0
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


def test_declared_credential_named_key_is_not_flagged_but_misbound_id_is() -> None:
    yaml = _yaml(
        """
        title: Sign in
        workflow_definition:
          parameters:
          - key: cred_x
            parameter_type: workflow
            workflow_parameter_type: credential_id
            default_value: cred_527971855302737592
          blocks:
          - block_type: http_request
            label: sign_in
            method: POST
            url: https://api.example.test/session
            body:
              username: "{{cred_x.username}}"
            parameter_keys: [cred_x]
          - block_type: code
            label: report_done
            code: print("signed in as cred_927971855302737594")
        """
    )

    findings = _credential_id_misbinding_findings(yaml)

    assert findings == [
        {
            "location": "report_done",
            "field": "code",
            "credential_id": "cred_927971855302737594",
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
            engine: skyvern-3.0
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


def _ctx() -> MagicMock:
    ctx = MagicMock(spec=AgentContext)
    ctx.workflow_yaml = ""
    ctx.scout_trajectory = []
    ctx.last_workflow_yaml = None
    ctx.workflow_id = "w_test"
    ctx.workflow_permanent_id = "wpid_test"
    ctx.organization_id = "o_test"
    ctx.allow_untested_workflow_draft = False
    ctx.request_policy = RequestPolicy(allow_update_workflow=True, allow_run_blocks=True)
    ctx.code_artifact_metadata = {}
    ctx.code_authoring_guardrail_reject_count = 0
    ctx.blocker_signal = None
    ctx.turn_halt = None
    ctx.build_test_outcomes = []
    ctx.latest_recorded_build_test_outcome = None
    ctx.credential_origin_recovery = None
    return ctx


@pytest.mark.asyncio
async def test_update_workflow_stages_credential_id_misbinding_without_authoring_a_finding() -> None:
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
            engine: skyvern-3.0
            url: https://authenticationtest.com/loginUserAndPassword/
            parameter_keys: [login_credentials]
            navigation_goal: Sign in with credential cred_527971855302737592 by entering its username and password.
        """
    )

    ctx = _ctx()
    ctx.request_policy.discovered_credentials = [
        _ListedCredential(
            "cred_527971855302737592",
            "Authentication Test",
            "https://authenticationtest.com/loginUserAndPassword/",
        )
    ]

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
        patch("skyvern.forge.sdk.copilot.tools.workflow_update._record_workflow_proxy_location_span"),
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        mock_app.DATABASE = MagicMock()
        result = await _update_workflow({"workflow_yaml": submitted}, ctx)

    assert result["ok"] is True, result
    assert "findings" not in result["data"]
    mock_app.WORKFLOW_SERVICE.update_workflow_definition.assert_not_awaited()
    assert ctx.staged_workflow is result["_workflow"]


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
            engine: skyvern-3.0
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
        patch("skyvern.forge.sdk.copilot.tools.workflow_update._record_workflow_proxy_location_span"),
        patch("skyvern.forge.sdk.copilot.tools.workflow_update.app") as mock_app,
    ):
        mock_app.WORKFLOW_SERVICE.update_workflow_definition = AsyncMock()
        mock_app.DATABASE = MagicMock()
        result = await _update_workflow({"workflow_yaml": submitted}, _ctx())

    error_text = str(result.get("error", ""))
    assert "credential ID appeared" not in error_text, error_text


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


_BITWARDEN_SECRET_KEYS = """
            bitwarden_client_id_aws_secret_key: SKYVERN_BITWARDEN_CLIENT_ID
            bitwarden_client_secret_aws_secret_key: SKYVERN_BITWARDEN_CLIENT_SECRET
            bitwarden_master_password_aws_secret_key: SKYVERN_BITWARDEN_MASTER_PASSWORD"""


def _bitwarden_workflow(
    parameter_fields: str, extra_parameters: str = "", secret_keys: str = _BITWARDEN_SECRET_KEYS
) -> str:
    return _yaml(
        f"""
        title: Sign in
        workflow_definition:
          parameters:
          - key: portal_login
            parameter_type: bitwarden_login_credential{secret_keys}{parameter_fields}{extra_parameters}
          blocks:
          - block_type: login
            label: login_to_portal
            url: https://portal.example.com/login
            parameter_keys: [portal_login]
        """
    )


_LITERAL_BINDING = _bitwarden_workflow(
    f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}\n            bitwarden_collection_id: col_1"
)


def _vault_ctx(*, persisted_ids: list[str] | None = None) -> AgentContext:
    ctx = _ctx_with_policy()
    ctx.request_policy.persisted_workflow_credential_ids = persisted_ids or []
    return ctx


@pytest.mark.asyncio
async def test_a_bitwarden_login_parameter_naming_a_listed_vault_login_is_accepted(org_vault: OrgVaultFake) -> None:
    assert await _credential_reference_validation_error(_LITERAL_BINDING, _vault_ctx()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "yaml",
    [
        pytest.param(_bitwarden_workflow('\n            bitwarden_item_id: "{{ item_id }}"'), id="templated"),
        pytest.param(_bitwarden_workflow("\n            bitwarden_item_id: portal_item"), id="key-reference"),
        pytest.param(_bitwarden_workflow("\n            url_parameter_key: portal_url"), id="url-only"),
        pytest.param(
            _bitwarden_workflow(
                f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}",
                f"""
          - key: "{ORG_VAULT_ITEM_ID}"
            parameter_type: workflow
            workflow_parameter_type: string
            default_value: shadow""",
            ),
            id="key-shadowed",
        ),
        pytest.param(
            _bitwarden_workflow(
                f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}",
                secret_keys=_BITWARDEN_SECRET_KEYS.replace("SKYVERN_BITWARDEN_MASTER_PASSWORD", "OTHER_SECRET"),
            ),
            id="unpinned-secret-key",
        ),
        pytest.param(
            _bitwarden_workflow("\n            bitwarden_item_id: 5c1d2e3f-0000-4000-8000-000000000009"),
            id="not-listed",
        ),
        pytest.param(
            _bitwarden_workflow(
                f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}\n            bitwarden_collection_id: col_9"
            ),
            id="other-collection",
        ),
        pytest.param(_bitwarden_workflow(f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}"), id="no-collection"),
        pytest.param(
            _bitwarden_workflow(
                f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}\n            bitwarden_collection_id: col_1",
                f"""
          - key: card
            parameter_type: bitwarden_credit_card_data{_BITWARDEN_SECRET_KEYS}
            bitwarden_collection_id: col_1
            bitwarden_item_id: {ORG_VAULT_ITEM_ID}""",
            ),
            id="new-credit-card-binding",
        ),
        pytest.param(
            _yaml(
                f"""
                title: Sign in
                workflow_definition:
                  parameters:
                  - key: portal_login
                    parameter_type: workflow
                    workflow_parameter_type: credential_id
                    default_value: {ORG_VAULT_LOGIN_ID}
                  blocks:
                  - block_type: login
                    label: login_to_portal
                    url: https://portal.example.com/login
                    parameter_keys: [portal_login]
                """
            ),
            id="credential-id-slot",
        ),
    ],
)
async def test_a_vault_login_binding_that_is_not_a_listed_literal_with_pinned_keys_is_refused(
    org_vault: OrgVaultFake, yaml: str
) -> None:
    assert await _credential_reference_validation_error(yaml, _vault_ctx()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", ["not_configured", "unreadable"])
async def test_a_literal_vault_binding_persists_while_the_vault_cannot_be_listed_but_a_template_does_not(
    org_vault: OrgVaultFake, connection: str
) -> None:
    if connection == "not_configured":
        org_vault.org_token.return_value = None
    else:
        org_vault.listing.side_effect = TimeoutError()
    templated = _bitwarden_workflow('\n            bitwarden_item_id: "{{ item_id }}"')

    assert await _credential_reference_validation_error(_LITERAL_BINDING, _vault_ctx()) is None
    assert await _credential_reference_validation_error(templated, _vault_ctx()) is not None


@pytest.mark.asyncio
async def test_a_credit_card_binding_the_workflow_already_had_is_kept(org_vault: OrgVaultFake) -> None:
    yaml = _bitwarden_workflow(
        f"\n            bitwarden_item_id: {ORG_VAULT_ITEM_ID}\n            bitwarden_collection_id: col_1",
        f"""
          - key: card
            parameter_type: bitwarden_credit_card_data{_BITWARDEN_SECRET_KEYS}
            bitwarden_collection_id: col_1
            bitwarden_item_id: {ORG_VAULT_ITEM_ID}""",
    )
    ctx = _vault_ctx()
    ctx.request_policy.existing_bitwarden_secret_bindings = bitwarden_secret_bindings(yaml)

    assert await _credential_reference_validation_error(yaml, ctx) is None


@pytest.mark.asyncio
async def test_a_url_only_bitwarden_lookup_the_saved_workflow_already_had_still_runs(org_vault: OrgVaultFake) -> None:
    yaml = _bitwarden_workflow("\n            url_parameter_key: portal_url")

    assert await _credential_reference_validation_error(yaml, _vault_ctx(persisted_ids=["bwitem_"])) is None


def test_a_vault_login_id_outside_a_bitwarden_parameter_is_refused_at_every_run_approval() -> None:
    policy = RequestPolicy(resolved_credentials=[connected_vault_credential("o_test", ORG_VAULT_ITEM_ID, "Portal")])
    slot_definition = {
        "parameters": [
            {
                "key": "portal_login",
                "parameter_type": "workflow",
                "workflow_parameter_type": "credential_id",
                "default_value": ORG_VAULT_LOGIN_ID,
            }
        ]
    }

    # Run inputs, account-group inputs and seed owners carry no definition; a schedule or run passes its own.
    assert _credential_run_approval_error([ORG_VAULT_LOGIN_ID], policy) is not None
    assert _credential_run_approval_error([ORG_VAULT_LOGIN_ID], policy, workflow_definition=slot_definition) is not None
    with pytest.raises(AccountGroupRefused):
        _check_common_inputs(MagicMock(request_policy=policy), {"portal_login": ORG_VAULT_LOGIN_ID})


def test_a_bitwarden_login_parameter_does_not_back_a_google_sheets_connection_slot() -> None:
    definition = {
        "parameters": [
            {
                "key": "portal_login",
                "parameter_type": "bitwarden_login_credential",
                "bitwarden_item_id": ORG_VAULT_ITEM_ID,
            }
        ],
        "blocks": [{"block_type": "google_sheets_read", "label": "read_sheet", "credential_id": "{{ portal_login }}"}],
    }

    assert _google_connection_reference_ids(definition, None) == ["{{ portal_login }}"]
