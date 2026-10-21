import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from skyvern.cli.mcp_tools import mcp as skyvern_mcp
from skyvern.forge import app
from skyvern.forge.sdk.copilot.agent import _make_agent_result, _reshow_unselected_google_setup
from skyvern.forge.sdk.copilot.browser_ablation import (
    REPAIR_PROBE_MODES,
    CopilotBrowserCodeMode,
    CopilotEvalMode,
    resolve_copilot_tool_surface,
    surface_runs_blocks,
)
from skyvern.forge.sdk.copilot.context import USER_FACING_REASON_PARAM, CopilotContext
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, _ground_user_provided_sites
from skyvern.forge.sdk.copilot.tools import (
    _LIST_INTEGRATIONS_RUN_GUIDANCE,
    _build_skyvern_mcp_overlays,
    copilot_native_tools,
    get_skyvern_mcp_alias_map,
    list_integrations_tool,
    offer_demonstration_tool,
)
from skyvern.forge.sdk.copilot.tools.integrations import (
    _list_integrations,
    _read_google_sheet,
    _request_google_connection,
    _serialize,
)
from skyvern.forge.sdk.copilot.turn_outcome import connected_account_choice_context
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ResponseKind, TurnOutcome
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.schemas.microsoft_oauth import MicrosoftOAuthCredentialBase
from tests.unit.copilot_test_helpers import make_copilot_ctx

ORGANIZATION_ID = "o_test_org"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

TOKEN_FIELD_NAMES = (
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "secret",
    "id_token",
    "encrypted_refresh_token",
)


def _google(**overrides: object) -> GoogleOAuthCredentialBase:
    defaults = {
        "id": "goac_1",
        "organization_id": ORGANIZATION_ID,
        "credential_name": "Sheets account",
        "state": "active",
        "scopes_requested": ["https://www.googleapis.com/auth/spreadsheets"],
        "scopes_granted": ["https://www.googleapis.com/auth/spreadsheets"],
        "created_at": datetime(2026, 6, 19),
        "modified_at": datetime(2026, 6, 19),
    }
    return GoogleOAuthCredentialBase(**{**defaults, **overrides})


def _microsoft(**overrides: object) -> MicrosoftOAuthCredentialBase:
    defaults = {
        "id": "msoac_1",
        "organization_id": ORGANIZATION_ID,
        "credential_name": "Outlook account",
        "state": "active",
        "scopes_requested": ["Mail.Send"],
        "scopes_granted": ["Mail.Send"],
        "created_at": datetime(2026, 6, 19),
        "modified_at": datetime(2026, 6, 19),
    }
    return MicrosoftOAuthCredentialBase(**{**defaults, **overrides})


@pytest.fixture
def patched_services(monkeypatch: pytest.MonkeyPatch):
    def _apply(google: list[GoogleOAuthCredentialBase], microsoft: list[MicrosoftOAuthCredentialBase]) -> None:
        async def fake_google(organization_id: str) -> list[GoogleOAuthCredentialBase]:
            assert organization_id == ORGANIZATION_ID
            return google

        async def fake_microsoft(organization_id: str) -> list[MicrosoftOAuthCredentialBase]:
            assert organization_id == ORGANIZATION_ID
            return microsoft

        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.integrations.google_oauth_service.get_visible_credentials_for_org",
            fake_google,
        )
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.integrations.microsoft_oauth_service.get_credentials_for_org",
            fake_microsoft,
        )

    return _apply


@pytest.mark.asyncio
async def test_lists_both_providers_with_the_fields_the_agent_needs(patched_services) -> None:
    patched_services([_google()], [_microsoft()])
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    google_entry, microsoft_entry = result["data"]["integrations"]
    assert google_entry == {
        "connection_id": "goac_1",
        "provider": "google",
        "name": "Sheets account",
        "state": "active",
        "scopes_granted": ["https://www.googleapis.com/auth/spreadsheets"],
    }
    assert microsoft_entry == {
        "connection_id": "msoac_1",
        "provider": "microsoft",
        "name": "Outlook account",
        "state": "active",
        "scopes_granted": ["Mail.Send"],
    }


@pytest.mark.asyncio
async def test_reports_no_integrations_without_erroring(patched_services) -> None:
    patched_services([], [])
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    assert result["ok"] is True
    assert result["data"] == {"integrations": [], "count": 0}


def test_serializer_is_an_allowlist_so_a_new_token_field_cannot_leak() -> None:
    leaky_google = SimpleNamespace(
        id="goac_1",
        credential_name="Sheets account",
        state="active",
        email_address="mailbox@example.com",
        scopes_granted=["https://www.googleapis.com/auth/spreadsheets"],
        **{name: f"SECRET_{name}" for name in TOKEN_FIELD_NAMES},
    )

    entry = _serialize(leaky_google, "google")

    assert set(entry) == {"connection_id", "provider", "name", "state", "scopes_granted", "email_address"}
    for name in TOKEN_FIELD_NAMES:
        assert name not in entry
    assert not any("SECRET_" in str(value) for value in entry.values())


@pytest.mark.asyncio
async def test_surfaces_the_mailbox_address_so_the_agent_can_bind_an_identifier(patched_services) -> None:
    patched_services(
        [_google(email_address="inbox@example.com")],
        [_microsoft(email_address="outlook@example.com")],
    )
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    google_entry, microsoft_entry = result["data"]["integrations"]
    assert google_entry["email_address"] == "inbox@example.com"
    assert microsoft_entry["email_address"] == "outlook@example.com"


@pytest.mark.asyncio
async def test_lists_a_google_connection_whose_grant_expired(patched_services) -> None:
    patched_services([_google(state="error")], [])
    ctx = SimpleNamespace(organization_id=ORGANIZATION_ID)

    result = await _list_integrations({}, ctx)

    assert result["data"]["count"] == 1
    assert result["data"]["integrations"][0]["state"] == "error"


def test_tool_description_states_facts_without_prescribing_dialogue() -> None:
    description = list_integrations_tool.description

    assert "state` is `active`" in description
    assert "state` is `error`" in description
    assert "ask the user" not in description.lower()
    assert "reconnect" not in description.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, *REPAIR_PROBE_MODES, CopilotEvalMode.BROWSER_ABLATION])
@pytest.mark.parametrize("browser_tools_available", [True, False])
@pytest.mark.parametrize("browser_code_mode", [CopilotBrowserCodeMode.ADD, CopilotBrowserCodeMode.REPLACE])
async def test_run_guidance_joins_the_description_only_on_a_surface_that_runs_blocks(
    mode: CopilotEvalMode | None, browser_tools_available: bool, browser_code_mode: CopilotBrowserCodeMode
) -> None:
    surface = resolve_copilot_tool_surface(
        mode=mode,
        native_tools=copilot_native_tools(
            supports_question_tool=True,
            browser_code_available=browser_code_mode == CopilotBrowserCodeMode.REPLACE,
            run_tools_available=surface_runs_blocks(mode=mode, browser_tools_available=browser_tools_available),
        ),
        alias_map=get_skyvern_mcp_alias_map(),
        overlays=_build_skyvern_mcp_overlays(),
        registered_mcp_tools=(
            await skyvern_mcp.list_tools(run_middleware=False) if mode == CopilotEvalMode.BROWSER_ABLATION else None
        ),
        browser_tools_available=browser_tools_available,
        browser_code_mode=browser_code_mode,
    )
    [tool] = [tool for tool in surface.native_tools if tool.name == "list_integrations"]

    runs_blocks = "update_and_run_blocks" in surface.ordered_native_names
    assert tool.description == (
        f"{list_integrations_tool.description}\n\n{_LIST_INTEGRATIONS_RUN_GUIDANCE}"
        if runs_blocks
        else list_integrations_tool.description
    )


SPREADSHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
OTHER_SPREADSHEET_ID = "1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210"
BUDGET_GID = 1226505961
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit?gid={BUDGET_GID}#gid={BUDGET_GID}"
OPENING_TOKEN = "ya29.a0AfB_byC-opening-account-access-token"
DENIED_TOKEN = "ya29.a0AfB_byC-denied-account-access-token"
SHEET_METADATA = {
    "properties": {"title": "Fleet metrics"},
    "sheets": [
        {"properties": {"sheetId": 0, "title": "Summary", "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
        {
            "properties": {
                "sheetId": BUDGET_GID,
                "title": "Budget",
                "gridProperties": {"rowCount": 12, "columnCount": 3},
            }
        },
    ],
}

SheetRows = list[list[str]]
TransportInstaller = Callable[[Callable[[httpx.Request], httpx.Response]], None]
SheetsApiInstaller = Callable[..., list[httpx.Request]]


def _grid_payload(rows: SheetRows) -> dict[str, object]:
    row_data = [{"values": [{"formattedValue": cell} for cell in row]} for row in rows]
    return {"sheets": [{"data": [{"rowData": row_data}]}]}


def _sheet_ctx(user_message: str = f"read Sessions Started and update {SHEET_URL}") -> CopilotContext:
    policy = RequestPolicy()
    _ground_user_provided_sites(policy, user_message, [])
    return make_copilot_ctx(organization_id=ORGANIZATION_ID, request_policy=policy)


@pytest.fixture
def sheets_api(monkeypatch: pytest.MonkeyPatch, mock_sheets_transport: TransportInstaller) -> SheetsApiInstaller:
    def _apply(
        connections: list[GoogleOAuthCredentialBase],
        tokens: dict[str, str | None],
        rows: SheetRows | None = None,
        values_response: httpx.Response | None = None,
    ) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.headers["Authorization"] != f"Bearer {OPENING_TOKEN}":
                denied = {"error": {"code": 403, "message": "The caller does not have permission"}}
                return httpx.Response(403, json=denied)
            if "ranges" in request.url.params:
                if values_response is not None:
                    return values_response
                return httpx.Response(200, json=_grid_payload(rows or [["Metric", "Value"], ["Sessions", "72.51k"]]))
            return httpx.Response(200, json=SHEET_METADATA)

        async def mint(organization_id: str, connection_id: str) -> str | None:
            assert organization_id == ORGANIZATION_ID
            return tokens[connection_id]

        async def visible(organization_id: str) -> list[GoogleOAuthCredentialBase]:
            return connections

        mock_sheets_transport(handler)
        monkeypatch.setattr(app.AGENT_FUNCTION, "get_google_sheets_credentials", mint)
        monkeypatch.setattr(
            "skyvern.forge.sdk.copilot.tools.integrations.google_oauth_service.get_visible_credentials_for_org",
            visible,
        )
        return requests

    return _apply


@pytest.mark.asyncio
async def test_read_google_sheet_reports_each_connection_and_reads_the_gid_tab_without_token_material(
    sheets_api: SheetsApiInstaller,
) -> None:
    requests = sheets_api(
        [
            _google(id="goac_denied", credential_name="Personal"),
            _google(id="goac_opens", credential_name="Ops", email_address="ops@example.com"),
            _google(id="goac_expired_grant", credential_name="Old"),
            _google(id="goac_gmail_only", scopes_granted=["https://www.googleapis.com/auth/gmail.readonly"]),
            _google(id="goac_needs_reconnect", state="error"),
        ],
        {"goac_denied": DENIED_TOKEN, "goac_opens": OPENING_TOKEN, "goac_expired_grant": None},
    )

    result = await _read_google_sheet({"spreadsheet_url": SHEET_URL}, _sheet_ctx())

    assert result["ok"] is True
    data = result["data"]
    assert data["connections"] == [
        {
            "connection_id": "goac_denied",
            "name": "Personal",
            "status": "no_access",
            "reason": "403: The caller does not have permission",
        },
        {"connection_id": "goac_opens", "name": "Ops", "status": "opened", "email_address": "ops@example.com"},
        {"connection_id": "goac_expired_grant", "name": "Old", "status": "token_unavailable"},
    ]
    assert data["title"] == "Fleet metrics"
    assert data["tabs"] == [
        {"title": "Summary", "gid": 0, "row_count": 1000, "column_count": 26},
        {"title": "Budget", "gid": BUDGET_GID, "row_count": 12, "column_count": 3},
    ]
    assert data["read_through"] == "goac_opens"
    assert data["range"] == "'Budget'!A1:C12"
    assert data["values"] == [["Metric", "Value"], ["Sessions", "72.51k"]]
    assert data["truncated"] is False
    assert requests[-1].url.params["ranges"] == "'Budget'!A1:C12"
    serialized = json.dumps(result)
    assert "ya29." not in serialized
    assert not any(name in serialized for name in ("access_token", "refresh_token", "client_secret", "id_token"))


@pytest.mark.asyncio
async def test_read_google_sheet_reads_a_requested_range_on_the_gid_tab(sheets_api: SheetsApiInstaller) -> None:
    requests = sheets_api([_google(id="goac_opens")], {"goac_opens": OPENING_TOKEN}, rows=[["72.51k"]])

    result = await _read_google_sheet(
        {"spreadsheet_url": SHEET_URL, "connection_id": "goac_opens", "range": "B2"}, _sheet_ctx()
    )

    assert result["data"]["range"] == "'Budget'!B2"
    assert result["data"]["values"] == [["72.51k"]]
    assert requests[-1].url.params["ranges"] == "'Budget'!B2"


@pytest.mark.asyncio
async def test_read_google_sheet_refuses_a_spreadsheet_the_user_did_not_give_before_any_call(
    sheets_api: SheetsApiInstaller,
) -> None:
    requests = sheets_api([_google(id="goac_opens")], {})
    ctx = _sheet_ctx()

    result = await _read_google_sheet(
        {"spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{OTHER_SPREADSHEET_ID}/edit"}, ctx
    )

    assert result["ok"] is False
    assert "data" not in result
    assert requests == []


def test_every_spreadsheet_url_the_user_wrote_is_readable_not_one_per_origin() -> None:
    policy = RequestPolicy()
    second_url = f"https://docs.google.com/spreadsheets/d/{OTHER_SPREADSHEET_ID}/edit"

    _ground_user_provided_sites(policy, f"move the totals from {SHEET_URL} to {second_url}.", [])

    assert policy.user_provided_spreadsheet_ids == [SPREADSHEET_ID, OTHER_SPREADSHEET_ID]


@pytest.mark.asyncio
async def test_read_google_sheet_reports_a_connection_id_outside_the_eligible_set_without_trying_it(
    sheets_api: SheetsApiInstaller,
) -> None:
    requests = sheets_api([_google(id="goac_opens"), _google(id="goac_needs_reconnect", state="error")], {})

    result = await _read_google_sheet(
        {"spreadsheet_url": SHEET_URL, "connection_id": "goac_needs_reconnect"}, _sheet_ctx()
    )

    assert result["ok"] is True
    assert result["data"]["connections"] == [{"connection_id": "goac_needs_reconnect", "status": "not_eligible"}]
    assert requests == []


@pytest.mark.asyncio
async def test_read_google_sheet_reports_a_hung_connection_as_a_timeout_row(
    monkeypatch: pytest.MonkeyPatch, sheets_api: SheetsApiInstaller
) -> None:
    sheets_api([_google(id="goac_hung"), _google(id="goac_opens")], {"goac_opens": OPENING_TOKEN})
    mint = app.AGENT_FUNCTION.get_google_sheets_credentials

    async def hung_mint(organization_id: str, connection_id: str) -> str | None:
        if connection_id == "goac_hung":
            await asyncio.Event().wait()
        return await mint(organization_id, connection_id)

    monkeypatch.setattr(app.AGENT_FUNCTION, "get_google_sheets_credentials", hung_mint)
    monkeypatch.setattr("skyvern.forge.sdk.copilot.tools.integrations._SHEET_READ_TIMEOUT_SECONDS", 0.05)

    result = await asyncio.wait_for(_read_google_sheet({"spreadsheet_url": SHEET_URL}, _sheet_ctx()), timeout=2)

    assert result["ok"] is True
    assert [(row["connection_id"], row["status"]) for row in result["data"]["connections"]] == [
        ("goac_hung", "error"),
        ("goac_opens", "opened"),
    ]
    assert result["data"]["connections"][0]["reason"] == "timeout"
    assert result["data"]["read_through"] == "goac_opens"


@pytest.mark.asyncio
async def test_read_google_sheet_reads_a_named_tab_as_given_and_reports_a_gid_no_tab_has(
    sheets_api: SheetsApiInstaller,
) -> None:
    requests = sheets_api([_google(id="goac_opens")], {"goac_opens": OPENING_TOKEN})
    stale_gid_url = SHEET_URL.replace(str(BUDGET_GID), "424242")

    named_tab = await _read_google_sheet({"spreadsheet_url": SHEET_URL, "range": "Summary"}, _sheet_ctx())
    stale_gid = await _read_google_sheet({"spreadsheet_url": stale_gid_url}, _sheet_ctx())

    assert named_tab["data"]["range"] == "Summary"
    assert "424242" in stale_gid["data"]["values_error"]
    assert "values" not in stale_gid["data"]
    assert [request.url.params.get("ranges") for request in requests] == [None, "Summary", None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values_response", "values_error"),
    [
        (httpx.Response(200, text="<html>not json</html>"), "JSONDecodeError"),
        (
            httpx.Response(400, json={"error": {"message": f"Unable to parse range for {OPENING_TOKEN}"}}),
            "400: Unable to parse range for [token]",
        ),
    ],
)
async def test_read_google_sheet_keeps_the_opened_connection_and_no_token_when_the_values_read_fails(
    sheets_api: SheetsApiInstaller, values_response: httpx.Response, values_error: str
) -> None:
    sheets_api([_google(id="goac_opens")], {"goac_opens": OPENING_TOKEN}, values_response=values_response)

    result = await _read_google_sheet({"spreadsheet_url": SHEET_URL}, _sheet_ctx())

    assert result["ok"] is True
    assert result["data"]["connections"][0]["status"] == "opened"
    assert result["data"]["values_error"] == values_error
    assert "ya29." not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("cell_character", ["x", "表", "😀"])
async def test_read_google_sheet_bounds_an_oversized_sheet_and_says_so(
    sheets_api: SheetsApiInstaller, cell_character: str
) -> None:
    sheets_api([_google(id="goac_opens")], {"goac_opens": OPENING_TOKEN}, rows=[[cell_character * 5_000] * 60] * 400)

    result = await _read_google_sheet({"spreadsheet_url": SHEET_URL, "range": "A1:ZZ400"}, _sheet_ctx())

    values = result["data"]["values"]
    assert 0 < len(values) <= 50
    assert all(len(row) <= 26 and all(len(cell) <= 200 for cell in row) for row in values)
    assert result["data"]["truncated"] is True
    assert len(json.dumps(result)) < 20_000


def test_read_google_sheet_description_states_facts_without_steering() -> None:
    registered = {
        tool.name: tool
        for tool in copilot_native_tools(
            supports_question_tool=True, browser_code_available=False, run_tools_available=True
        )
    }
    tool = registered["read_google_sheet"]
    description = tool.description.lower()

    assert set(tool.params_json_schema["properties"]) == {
        "spreadsheet_url",
        "connection_id",
        "range",
        USER_FACING_REASON_PARAM,
    }
    for status in ("opened", "no_access", "token_unavailable", "not_eligible"):
        assert status in description
    for steering in ("ask the user", "reconnect", "instead of", "prefer", "do not", "must call", "browser ui"):
        assert steering not in description


def _setup_rows() -> list[GoogleOAuthCredentialBase]:
    return [
        _google(id="goac_sheets_a", credential_name="Sheets A"),
        _google(id="goac_sheets_b", credential_name="Sheets B"),
        _google(id="goac_sheets_expired", credential_name="Sheets expired", state="error"),
        _google(
            id="goac_mail_expired",
            credential_name="Mail expired",
            state="error",
            scopes_requested=[GMAIL_READ_SCOPE],
            scopes_granted=[GMAIL_READ_SCOPE],
        ),
        _google(
            id="goac_send_only",
            credential_name="Send only",
            scopes_requested=[GMAIL_SEND_SCOPE],
            scopes_granted=[GMAIL_SEND_SCOPE],
        ),
        _google(
            id="goac_mail_sheets_reauth_cancelled",
            credential_name="Mail, Sheets reconnect cancelled",
            scopes_requested=[SHEETS_SCOPE],
            scopes_granted=[GMAIL_READ_SCOPE],
        ),
        _google(
            id="goac_drive_sheets_reauth_cancelled",
            credential_name="Drive, Sheets reconnect cancelled",
            scopes_requested=[SHEETS_SCOPE],
            scopes_granted=["https://www.googleapis.com/auth/drive"],
        ),
    ]


@pytest.mark.asyncio
async def test_request_google_connection_offers_one_unbound_card_per_profile_without_touching_the_workflow(
    patched_services,
) -> None:
    patched_services([], [])
    ctx = make_copilot_ctx(organization_id=ORGANIZATION_ID, workflow_yaml="title: untouched\n")

    gmail = await _request_google_connection("gmail", None, ctx)
    sheets = await _request_google_connection("google_sheets", None, ctx)
    await _request_google_connection("gmail", None, ctx)

    assert gmail == {
        "ok": True,
        "data": {"status": "offered", "scope_profile": "gmail", "card": "connect", "compatible_connections": []},
    }
    assert sheets["data"]["scope_profile"] == "google_sheets"
    assert [(notice.scopeProfile, notice.origin, notice.condition) for notice in ctx.google_setup_notices] == [
        ("google_sheets", "setup", "unbound"),
        ("gmail", "setup", "unbound"),
    ]
    assert ctx.google_connection_notices == []
    assert ctx.workflow_yaml == "title: untouched\n"
    assert not ctx.update_workflow_called
    payloads = ctx.google_connection_notice_payloads()
    assert {(payload["scopeProfile"], payload["connectionId"]) for payload in payloads} == {
        ("gmail", None),
        ("google_sheets", None),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_profile", "connection_id", "card", "reconnect_id", "ignored_id", "choice_ids"),
    [
        ("google_sheets", None, "connect", None, None, ["goac_sheets_a", "goac_sheets_b"]),
        ("google_sheets", "goac_sheets_expired", "reconnect", "goac_sheets_expired", None, None),
        ("gmail", "goac_mail_expired", "reconnect", "goac_mail_expired", None, None),
        ("gmail", "goac_sheets_expired", "connect", None, "goac_sheets_expired", ["goac_mail_sheets_reauth_cancelled"]),
        ("google_sheets", "goac_mail_expired", "connect", None, "goac_mail_expired", None),
        ("gmail", "goac_send_only", "connect", None, "goac_send_only", ["goac_mail_sheets_reauth_cancelled"]),
        (
            "google_sheets",
            "goac_mail_sheets_reauth_cancelled",
            "connect",
            None,
            "goac_mail_sheets_reauth_cancelled",
            ["goac_sheets_a", "goac_sheets_b"],
        ),
        (
            "google_sheets",
            "goac_drive_sheets_reauth_cancelled",
            "connect",
            None,
            "goac_drive_sheets_reauth_cancelled",
            ["goac_sheets_a", "goac_sheets_b"],
        ),
        (
            "gmail",
            "goac_other_org_or_deleted",
            "connect",
            None,
            "goac_other_org_or_deleted",
            ["goac_mail_sheets_reauth_cancelled"],
        ),
        ("google_sheets", "goac_sheets_a", "connect", None, "goac_sheets_a", None),
    ],
)
async def test_request_google_connection_reconnects_only_a_same_profile_row(
    patched_services,
    scope_profile: str,
    connection_id: str | None,
    card: str,
    reconnect_id: str | None,
    ignored_id: str | None,
    choice_ids: list[str] | None,
) -> None:
    patched_services(_setup_rows(), [])
    ctx = make_copilot_ctx(organization_id=ORGANIZATION_ID)

    result = await _request_google_connection(scope_profile, connection_id, ctx)

    data = result["data"]
    (notice,) = ctx.google_setup_notices
    assert data["card"] == card
    assert data.get("reconnect_connection_id") == reconnect_id
    assert data.get("ignored_connection_id") == ignored_id
    assert notice.connectionId == reconnect_id
    assert notice.scopeProfile == scope_profile
    expected_ready = {
        "gmail": ["goac_mail_sheets_reauth_cancelled"],
        "google_sheets": ["goac_sheets_a", "goac_sheets_b"],
    }[scope_profile]
    assert [row["connection_id"] for row in data["compatible_connections"]] == expected_ready
    if choice_ids is not None:
        assert [choice.connection_id for choice in notice.choices] == choice_ids
    serialized = json.dumps(result)
    for name in TOKEN_FIELD_NAMES:
        assert f'"{name}"' not in serialized


@pytest.mark.parametrize("supports_google_setup_card", [False, True])
def test_request_google_connection_is_offered_only_to_a_client_that_renders_the_card(
    supports_google_setup_card: bool,
) -> None:
    names = {
        tool.name
        for tool in copilot_native_tools(
            supports_question_tool=True,
            browser_code_available=False,
            run_tools_available=True,
            supports_google_setup_card=supports_google_setup_card,
        )
    }

    assert ("request_google_connection" in names) is supports_google_setup_card


@pytest.mark.parametrize("supports_demonstration_offer", [False, True])
def test_offer_demonstration_is_offered_only_to_a_client_that_renders_the_card(
    supports_demonstration_offer: bool,
) -> None:
    names = {
        tool.name
        for tool in copilot_native_tools(
            supports_question_tool=True,
            browser_code_available=False,
            run_tools_available=True,
            supports_demonstration_offer=supports_demonstration_offer,
        )
    }

    assert ("offer_demonstration" in names) is supports_demonstration_offer


@pytest.mark.asyncio
async def test_offer_demonstration_sets_the_turn_flag_and_narrative_payload() -> None:
    ctx = make_copilot_ctx()

    await offer_demonstration_tool.on_invoke_tool(
        SimpleNamespace(context=ctx, tool_name="offer_demonstration"),
        "{}",
    )
    result = _make_agent_result(
        ctx,
        user_response="Show me.",
        updated_workflow=None,
        global_llm_context=None,
        narrative_payload={"turnId": "turn-1", "terminal": "response"},
        turn_outcome=TurnOutcome(response_kind=ResponseKind.ANSWER),
    )

    assert ctx.demonstration_offered is True
    assert result.narrative_payload is not None
    assert result.narrative_payload["demonstrationOffer"] is True


def _persisted_offer_turn(ctx: CopilotContext) -> TurnOutcome:
    result = _make_agent_result(
        ctx,
        user_response="ok",
        updated_workflow=None,
        global_llm_context=None,
        narrative_payload={"turnId": "turn-1", "terminal": "response"},
        turn_outcome=TurnOutcome(response_kind=ResponseKind.ANSWER),
    )
    assert result.turn_outcome is not None
    return TurnOutcome.model_validate_json(result.turn_outcome.model_dump_json())


@pytest.mark.asyncio
async def test_picking_one_setup_card_offers_its_unpicked_sibling_again_on_the_selection_turn(
    patched_services,
) -> None:
    gmail_row = _google(
        id="goac_mail", credential_name="Mail", scopes_requested=[GMAIL_READ_SCOPE], scopes_granted=[GMAIL_READ_SCOPE]
    )
    patched_services([gmail_row, _google(id="goac_sheets")], [])
    offer_ctx = make_copilot_ctx(organization_id=ORGANIZATION_ID)
    await _request_google_connection("gmail", None, offer_ctx)
    await _request_google_connection("google_sheets", None, offer_ctx)
    offer_turn = _persisted_offer_turn(offer_ctx)

    selection_ctx = make_copilot_ctx(organization_id=ORGANIZATION_ID)
    gmail_pick = RequestPolicy(
        picked_connected_account_id="goac_mail", picked_connected_account_scopes=[GMAIL_READ_SCOPE]
    )
    reshown = await _reshow_unselected_google_setup(selection_ctx, offer_turn, gmail_pick)
    facts = json.loads(
        connected_account_choice_context(
            offer_turn,
            explicit_selected_connection_id="goac_mail",
            explicit_selected_connection_scopes=[GMAIL_READ_SCOPE],
            reshown_setup_profiles=reshown,
        )
    )

    assert [
        (notice.scopeProfile, notice.condition, [choice.connection_id for choice in notice.choices])
        for notice in selection_ctx.google_setup_notices
    ] == [("google_sheets", "unbound", ["goac_sheets"])]
    assert facts["unselected_setup_cards_shown_again"] == ["google_sheets"]

    free_text_ctx = make_copilot_ctx(organization_id=ORGANIZATION_ID)
    assert await _reshow_unselected_google_setup(free_text_ctx, offer_turn, RequestPolicy()) == []
    assert free_text_ctx.google_setup_notices == []

    sheets_pick = RequestPolicy(
        picked_connected_account_id="goac_sheets", picked_connected_account_scopes=[SHEETS_SCOPE]
    )
    done_ctx = make_copilot_ctx(organization_id=ORGANIZATION_ID)
    assert await _reshow_unselected_google_setup(done_ctx, _persisted_offer_turn(selection_ctx), sheets_pick) == []
    assert done_ctx.google_setup_notices == []
