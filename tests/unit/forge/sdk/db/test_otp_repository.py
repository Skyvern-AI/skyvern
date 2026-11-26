import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from skyvern.forge import agent_functions
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import TOTPCodeModel
from skyvern.forge.sdk.db.repositories.otp import _strict_identifier
from skyvern.forge.sdk.schemas.totp_codes import OTPType, RawTOTPCode, TOTPCode
from skyvern.services import otp_service
from skyvern.services.otp_email import EmailOTPCandidate

ReaderName = Literal["get_otp_codes", "get_recent_otp_codes", "get_raw_otp_codes"]


async def _read_codes(
    agent_db: AgentDB,
    reader_name: ReaderName,
    totp_identifier: str,
) -> Sequence[TOTPCode | RawTOTPCode]:
    if reader_name == "get_otp_codes":
        return await agent_db.otp.get_otp_codes(
            organization_id="o_test",
            totp_identifier=totp_identifier,
        )
    if reader_name == "get_recent_otp_codes":
        return await agent_db.otp.get_recent_otp_codes(
            organization_id="o_test",
            totp_identifier=totp_identifier,
        )
    return await agent_db.otp.get_raw_otp_codes(
        organization_id="o_test",
        totp_identifier=totp_identifier,
    )


@pytest.mark.parametrize("reader_name", ["get_otp_codes", "get_recent_otp_codes", "get_raw_otp_codes"])
@pytest.mark.parametrize(
    ("stored_identifier", "lookup_identifier"),
    [
        pytest.param(
            "Tenant.User@Example.Test",
            "tenant.user@example.test",
            id="legacy-mixed-case-row",
        ),
        pytest.param(
            "tenant.user@example.test",
            "Tenant.User@Example.Test",
            id="tenant-cased-lookup",
        ),
    ],
)
@pytest.mark.asyncio
async def test_otp_repository_readers_match_email_identifiers_case_insensitively(
    agent_db: AgentDB,
    reader_name: ReaderName,
    stored_identifier: str,
    lookup_identifier: str,
) -> None:
    parse_status = "raw" if reader_name == "get_raw_otp_codes" else "parsed"
    async with agent_db.Session() as session:
        session.add(
            TOTPCodeModel(
                totp_code_id="otp_legacy_case",
                organization_id="o_test",
                totp_identifier=stored_identifier,
                content="123456",
                code=None if parse_status == "raw" else "123456",
                otp_type=None if parse_status == "raw" else "totp",
                parse_status=parse_status,
            )
        )
        await session.commit()

    results = await _read_codes(agent_db, reader_name, lookup_identifier)

    assert [result.totp_code_id for result in results] == ["otp_legacy_case"]


@pytest.mark.parametrize("reader_name", ["get_otp_codes", "get_recent_otp_codes", "get_raw_otp_codes"])
@pytest.mark.asyncio
async def test_email_poller_persisted_identifier_is_reachable_by_all_readers(
    agent_db: AgentDB,
    reader_name: ReaderName,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_status = "raw" if reader_name == "get_raw_otp_codes" else "parsed"

    async def create_otp_code(
        organization_id: str,
        totp_identifier: str,
        content: str,
        code: str,
        otp_type: OTPType,
        **_kwargs: object,
    ) -> None:
        async with agent_db.Session() as session:
            session.add(
                TOTPCodeModel(
                    totp_code_id="otp_poller_identifier",
                    organization_id=organization_id,
                    totp_identifier=totp_identifier,
                    content=content,
                    code=None if parse_status == "raw" else code,
                    otp_type=None if parse_status == "raw" else otp_type,
                    parse_status=parse_status,
                )
            )
            await session.commit()

    source = SimpleNamespace(
        name="probe",
        list_credential_ids=AsyncMock(return_value=["credential"]),
        search_recent_otp_messages=AsyncMock(
            return_value=[EmailOTPCandidate(message_id="message", content="Your code is 123456")]
        ),
    )
    monkeypatch.setattr(agent_functions, "build_email_otp_sources", lambda _agent: [source])
    monkeypatch.setattr(otp_service, "parse_otp_login", AsyncMock(return_value=otp_service.OTPValue(value="123456")))
    monkeypatch.setattr(
        agent_functions.app,
        "DATABASE",
        SimpleNamespace(otp=SimpleNamespace(create_otp_code=create_otp_code)),
    )

    result = await AgentFunction().get_otp_value_from_email(
        organization_id="o_test",
        totp_identifier=" Tenant.User@Example.Test ",
    )

    assert result == otp_service.OTPValue(value="123456")
    results = await _read_codes(agent_db, reader_name, "Tenant.User@Example.Test")
    assert [stored.totp_code_id for stored in results] == ["otp_poller_identifier"]
    assert [stored.totp_identifier for stored in results] == ["tenant.user@example.test"]


@pytest.mark.parametrize("reader_name", ["get_otp_codes", "get_recent_otp_codes", "get_raw_otp_codes"])
@pytest.mark.asyncio
async def test_otp_repository_readers_match_non_email_identifiers_exactly(
    agent_db: AgentDB,
    reader_name: ReaderName,
) -> None:
    parse_status = "raw" if reader_name == "get_raw_otp_codes" else "parsed"
    async with agent_db.Session() as session:
        session.add_all(
            [
                TOTPCodeModel(
                    totp_code_id="otp_username_upper",
                    organization_id="o_test",
                    totp_identifier="UserA",
                    content="123456",
                    code=None if parse_status == "raw" else "123456",
                    otp_type=None if parse_status == "raw" else "totp",
                    parse_status=parse_status,
                ),
                TOTPCodeModel(
                    totp_code_id="otp_username_lower",
                    organization_id="o_test",
                    totp_identifier="usera",
                    content="654321",
                    code=None if parse_status == "raw" else "654321",
                    otp_type=None if parse_status == "raw" else "totp",
                    parse_status=parse_status,
                ),
            ]
        )
        await session.commit()

    upper_results = await _read_codes(agent_db, reader_name, "UserA")
    lower_results = await _read_codes(agent_db, reader_name, "usera")

    assert [result.totp_code_id for result in upper_results] == ["otp_username_upper"]
    assert [result.totp_code_id for result in lower_results] == ["otp_username_lower"]


@pytest.mark.asyncio
async def test_promote_raw_otp_code_and_strict_claims_are_atomic(
    org_scoped_db: tuple[AgentDB, Any, str],
) -> None:
    agent_db, _, organization_id = org_scoped_db
    async with agent_db.Session() as session:
        session.add_all(
            [
                TOTPCodeModel(
                    totp_code_id="otp_promote",
                    organization_id=organization_id,
                    totp_identifier="user@example.test",
                    content="your code is 123456",
                    code=None,
                    otp_type=None,
                    parse_status="raw",
                ),
                TOTPCodeModel(
                    totp_code_id="otp_strict_promote",
                    organization_id=organization_id,
                    totp_identifier=_strict_identifier("strict@example.test", "otpr_raw"),
                    content="your strict code is 654321",
                    code=None,
                    otp_type=None,
                    parse_status="strict_raw",
                    otp_request_id="otpr_raw",
                ),
            ]
        )
        await session.commit()

    promoted = await agent_db.otp.promote_raw_otp_code(
        totp_code_id="otp_promote",
        organization_id=organization_id,
        code="123456",
        otp_type=OTPType.TOTP,
    )

    assert promoted is not None
    assert promoted.totp_code_id == "otp_promote"
    assert promoted.code == "123456"
    strict_promoted = await agent_db.otp.promote_raw_otp_code(
        totp_code_id="otp_strict_promote",
        organization_id=organization_id,
        code="654321",
        otp_type=OTPType.TOTP,
    )
    assert strict_promoted is not None
    async with agent_db.Session() as session:
        assert (
            await session.scalar(select(TOTPCodeModel.parse_status).where(TOTPCodeModel.totp_code_id == "otp_promote"))
            == "parsed"
        )
        assert (
            await session.scalar(
                select(TOTPCodeModel.parse_status).where(TOTPCodeModel.totp_code_id == "otp_strict_promote")
            )
            == "strict_parsed"
        )

    task_a = await agent_db.tasks.create_task(
        url="https://example.test/a",
        title="Strict owner A",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization_id,
        otp_request_id="otpr_a",
    )
    task_b = await agent_db.tasks.create_task(
        url="https://example.test/b",
        title="Strict owner B",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization_id,
        otp_request_id="otpr_b",
    )
    task_resend = await agent_db.tasks.create_task(
        url="https://example.test/resend",
        title="Strict resend owner",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization_id,
        otp_request_id="otpr_resend",
    )
    root_workflow = await agent_db.workflows.create_workflow(
        title="Strict root workflow",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=organization_id,
        workflow_permanent_id="wpid_strict_root",
    )
    child_workflow = await agent_db.workflows.create_workflow(
        title="Strict child workflow",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=organization_id,
        workflow_permanent_id="wpid_strict_child",
    )
    root_workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id=root_workflow.workflow_permanent_id,
        workflow_id=root_workflow.workflow_id,
        organization_id=organization_id,
        otp_request_id="otpr_workflow_chain",
    )
    child_workflow_run = await agent_db.workflow_runs.create_workflow_run(
        workflow_permanent_id=child_workflow.workflow_permanent_id,
        workflow_id=child_workflow.workflow_id,
        organization_id=organization_id,
        parent_workflow_run_id=root_workflow_run.workflow_run_id,
    )
    resend_at = datetime.now(UTC).replace(tzinfo=None)
    async with agent_db.Session() as session:
        session.add_all(
            [
                TOTPCodeModel(
                    totp_code_id="otp_marked_a",
                    organization_id=organization_id,
                    totp_identifier=_strict_identifier("shared@example.test", "otpr_a"),
                    content="111111",
                    code="111111",
                    otp_type="totp",
                    parse_status="strict_parsed",
                    otp_request_id="otpr_a",
                    task_id=task_a.task_id,
                ),
                TOTPCodeModel(
                    totp_code_id="otp_marked_b",
                    organization_id=organization_id,
                    totp_identifier=_strict_identifier("shared@example.test", "otpr_b"),
                    content="222222",
                    code="222222",
                    otp_type="totp",
                    parse_status="strict_parsed",
                    otp_request_id="otpr_b",
                    task_id=task_b.task_id,
                ),
                TOTPCodeModel(
                    totp_code_id="otp_legacy",
                    organization_id=organization_id,
                    totp_identifier="shared@example.test",
                    content="333333",
                    code="333333",
                    otp_type="totp",
                    parse_status="parsed",
                ),
                TOTPCodeModel(
                    totp_code_id="otp_resend_old",
                    organization_id=organization_id,
                    totp_identifier=_strict_identifier("resend@example.test", "otpr_resend"),
                    content="555111",
                    code="555111",
                    otp_type="totp",
                    parse_status="strict_parsed",
                    otp_request_id="otpr_resend",
                    task_id=task_resend.task_id,
                    created_at=resend_at - timedelta(minutes=1),
                ),
                TOTPCodeModel(
                    totp_code_id="otp_resend_new",
                    organization_id=organization_id,
                    totp_identifier=_strict_identifier("resend@example.test", "otpr_resend"),
                    content="555222",
                    code="555222",
                    otp_type="totp",
                    parse_status="strict_parsed",
                    otp_request_id="otpr_resend",
                    task_id=task_resend.task_id,
                    created_at=resend_at,
                ),
                TOTPCodeModel(
                    totp_code_id="otp_workflow_root_delivery",
                    organization_id=organization_id,
                    totp_identifier=_strict_identifier("workflow@example.test", "otpr_workflow_chain"),
                    content="666777",
                    code="666777",
                    otp_type="totp",
                    parse_status="strict_parsed",
                    otp_request_id="otpr_workflow_chain",
                    workflow_id=root_workflow.workflow_id,
                    workflow_run_id=root_workflow_run.workflow_run_id,
                ),
            ]
        )
        await session.commit()

    legacy = await agent_db.otp.get_otp_codes(
        organization_id=organization_id,
        totp_identifier="shared@example.test",
    )
    unfiltered_legacy = await agent_db.otp.get_recent_otp_codes(organization_id=organization_id)
    child_claim = await agent_db.otp.claim_otp_code(
        organization_id=organization_id,
        totp_identifier="workflow@example.test",
        otp_request_id="otpr_workflow_chain",
        workflow_id=child_workflow.workflow_id,
        workflow_run_id=child_workflow_run.workflow_run_id,
    )
    wrong_owner = await agent_db.otp.claim_otp_code(
        organization_id=organization_id,
        totp_identifier="shared@example.test",
        otp_request_id="otpr_a",
        task_id=task_b.task_id,
    )
    wrong_org = await agent_db.otp.claim_otp_code(
        organization_id="o_other",
        totp_identifier="shared@example.test",
        otp_request_id="otpr_a",
        task_id=task_a.task_id,
    )
    winners = await asyncio.gather(
        *(
            agent_db.otp.claim_otp_code(
                organization_id=organization_id,
                totp_identifier="shared@example.test",
                otp_request_id="otpr_a",
                task_id=task_a.task_id,
            )
            for _ in range(2)
        )
    )

    assert [row.totp_code_id for row in legacy] == ["otp_legacy"]
    assert all(row.otp_request_id is None for row in unfiltered_legacy)
    assert child_claim is not None
    assert child_claim.totp_code_id == "otp_workflow_root_delivery"
    assert child_claim.claimed_by_workflow_run_id == child_workflow_run.workflow_run_id
    assert wrong_owner is None
    assert wrong_org is None
    assert [row.totp_code_id for row in winners if row is not None] == ["otp_marked_a"]
    async with agent_db.Session() as session:
        base_reader_rows = (
            await session.scalars(
                select(TOTPCodeModel).where(
                    TOTPCodeModel.organization_id == organization_id,
                    func.lower(TOTPCodeModel.totp_identifier) == "shared@example.test",
                    TOTPCodeModel.parse_status == "parsed",
                )
            )
        ).all()
    assert [row.totp_code_id for row in base_reader_rows] == ["otp_legacy"]
    assert await agent_db.otp.reject_claimed_otp_code(
        totp_code_id="otp_marked_a",
        organization_id=organization_id,
        task_id=task_a.task_id,
        workflow_run_id=None,
    )
    assert not await agent_db.otp.reject_claimed_otp_code(
        totp_code_id="otp_marked_a",
        organization_id=organization_id,
        task_id=task_a.task_id,
        workflow_run_id=None,
    )
    assert (
        await agent_db.otp.claim_otp_code(
            organization_id=organization_id,
            totp_identifier="shared@example.test",
            otp_request_id="otpr_a",
            task_id=task_a.task_id,
        )
        is None
    )
    first_delivery = await agent_db.otp.create_otp_code_if_new(
        organization_id=organization_id,
        totp_identifier="shared@example.test",
        content="444444",
        code="444444",
        otp_type=OTPType.TOTP,
        task_id=task_b.task_id,
        otp_request_id="otpr_b",
    )
    duplicate_delivery = await agent_db.otp.create_otp_code_if_new(
        organization_id=organization_id,
        totp_identifier="shared@example.test",
        content="444444",
        code="444444",
        otp_type=OTPType.TOTP,
        task_id=task_b.task_id,
        otp_request_id="otpr_b",
    )
    assert first_delivery is not None
    assert first_delivery.totp_identifier == "shared@example.test"
    assert duplicate_delivery is None

    literal_codes = [
        r"\xGG",
        r"\x4142",
        "AB",
        "424242",
        r"https://example.test/verify\path?token=\xGG",
    ]
    literal_deliveries: list[TOTPCode] = []
    for code in literal_codes:
        created = await agent_db.otp.create_otp_code_if_new(
            organization_id=organization_id,
            totp_identifier="literal@example.test",
            content=code,
            code=code,
            otp_type=OTPType.TOTP,
            task_id=task_b.task_id,
            otp_request_id="otpr_b",
        )
        duplicate = await agent_db.otp.create_otp_code_if_new(
            organization_id=organization_id,
            totp_identifier="literal@example.test",
            content=code,
            code=code,
            otp_type=OTPType.TOTP,
            task_id=task_b.task_id,
            otp_request_id="otpr_b",
        )
        assert created is not None
        assert duplicate is None
        literal_deliveries.append(created)
    assert [delivery.code for delivery in literal_deliveries] == literal_codes
    assert await agent_db.otp.create_otp_code_if_new(
        organization_id=organization_id,
        totp_identifier="literal@example.test",
        content=r"\xGG",
        code=r"\xGG",
        otp_type=OTPType.TOTP,
        task_id=task_a.task_id,
        otp_request_id="otpr_a",
    )
    assert await agent_db.otp.create_otp_code_if_new(
        organization_id=organization_id,
        totp_identifier="other-literal@example.test",
        content=r"\xGG",
        code=r"\xGG",
        otp_type=OTPType.TOTP,
        task_id=task_b.task_id,
        otp_request_id="otpr_b",
    )
    async with agent_db.Session() as session:
        assert (
            await session.scalar(
                select(TOTPCodeModel.parse_status).where(
                    TOTPCodeModel.totp_code_id == first_delivery.totp_code_id,
                )
            )
            == "strict_parsed"
        )
        assert (
            await session.scalar(
                select(TOTPCodeModel.totp_code_id).where(
                    TOTPCodeModel.organization_id == organization_id,
                    func.lower(TOTPCodeModel.totp_identifier) == "shared@example.test",
                    TOTPCodeModel.code == "444444",
                )
            )
            is None
        )
    resend = await agent_db.otp.claim_otp_code(
        organization_id=organization_id,
        totp_identifier="resend@example.test",
        otp_request_id="otpr_resend",
        task_id=task_resend.task_id,
    )
    assert resend is not None
    assert resend.totp_code_id == "otp_resend_new"

    formatted_phone = await agent_db.otp.create_otp_code_if_new(
        organization_id=organization_id,
        totp_identifier="+1 (415) 555-1234",
        content="777777",
        code="777777",
        otp_type=OTPType.TOTP,
        task_id=task_b.task_id,
        otp_request_id="otpr_b",
    )
    equivalent_phone = await agent_db.otp.create_otp_code_if_new(
        organization_id=organization_id,
        totp_identifier="14155551234",
        content="777777",
        code="777777",
        otp_type=OTPType.TOTP,
        task_id=task_b.task_id,
        otp_request_id="otpr_b",
    )
    assert formatted_phone is not None
    assert equivalent_phone is None

    webhook_task = await agent_db.tasks.create_task(
        url="https://example.test/webhook",
        title="Strict webhook owner",
        navigation_goal=None,
        data_extraction_goal=None,
        navigation_payload=None,
        organization_id=organization_id,
        otp_request_id="otpr_webhook",
    )
    webhook_winners = await asyncio.gather(
        *(
            agent_db.otp.claim_strict_webhook_otp_code(
                organization_id=organization_id,
                otp_request_id="otpr_webhook",
                content="888111",
                code="888111",
                otp_type=OTPType.TOTP,
                task_id=webhook_task.task_id,
                workflow_run_id=None,
            )
            for _ in range(2)
        )
    )
    assert webhook_winners.count(True) == 1
    assert webhook_winners.count(False) == 1
    for code in literal_codes:
        assert await agent_db.otp.claim_strict_webhook_otp_code(
            organization_id=organization_id,
            otp_request_id="otpr_webhook",
            content=code,
            code=code,
            otp_type=OTPType.TOTP,
            task_id=webhook_task.task_id,
            workflow_run_id=None,
        )
        assert not await agent_db.otp.claim_strict_webhook_otp_code(
            organization_id=organization_id,
            otp_request_id="otpr_webhook",
            content=code,
            code=code,
            otp_type=OTPType.TOTP,
            task_id=webhook_task.task_id,
            workflow_run_id=None,
        )
    assert not await agent_db.otp.claim_strict_webhook_otp_code(
        organization_id="o_other",
        otp_request_id="otpr_webhook",
        content="888222",
        code="888222",
        otp_type=OTPType.TOTP,
        task_id=webhook_task.task_id,
        workflow_run_id=None,
    )
    assert not await agent_db.otp.claim_strict_webhook_otp_code(
        organization_id=organization_id,
        otp_request_id="otpr_webhook",
        content="888222",
        code="888222",
        otp_type=OTPType.TOTP,
        task_id=task_a.task_id,
        workflow_run_id=None,
    )
    assert await agent_db.otp.claim_strict_webhook_otp_code(
        organization_id=organization_id,
        otp_request_id="otpr_webhook",
        content="888222",
        code="888222",
        otp_type=OTPType.TOTP,
        task_id=webhook_task.task_id,
        workflow_run_id=None,
    )

    async with agent_db.Session() as session:
        webhook_rows = (
            await session.scalars(
                select(TOTPCodeModel).where(
                    TOTPCodeModel.organization_id == organization_id,
                    TOTPCodeModel.otp_request_id == "otpr_webhook",
                )
            )
        ).all()
    assert {row.code for row in webhook_rows} == {"888111", "888222", *literal_codes}
    assert all(row.totp_identifier is None for row in webhook_rows)
    assert all(row.claimed_by_task_id == webhook_task.task_id for row in webhook_rows)
    assert all(row.claimed_at is not None for row in webhook_rows)
