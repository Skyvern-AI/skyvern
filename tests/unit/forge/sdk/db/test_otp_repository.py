from collections.abc import Sequence
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import agent_functions
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import TOTPCodeModel
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
async def test_promote_raw_otp_code_returns_promoted_row(agent_db: AgentDB) -> None:
    async with agent_db.Session() as session:
        session.add(
            TOTPCodeModel(
                totp_code_id="otp_promote",
                organization_id="o_test",
                totp_identifier="user@example.test",
                content="your code is 123456",
                code=None,
                otp_type=None,
                parse_status="raw",
            )
        )
        await session.commit()

    promoted = await agent_db.otp.promote_raw_otp_code(
        totp_code_id="otp_promote",
        organization_id="o_test",
        code="123456",
        otp_type=OTPType.TOTP,
    )

    assert promoted is not None
    assert promoted.totp_code_id == "otp_promote"
    assert promoted.code == "123456"
