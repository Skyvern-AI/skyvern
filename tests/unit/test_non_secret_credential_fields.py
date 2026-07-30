from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.schemas.credentials import CreditCardBillingAddress, CreditCardCredential
from skyvern.forge.sdk.workflow import context_manager as context_manager_module
from skyvern.forge.sdk.workflow.context_manager import RANDOM_SECRET_ID_PREFIX, WorkflowRunContext

_CARD = CreditCardCredential(
    card_number="4111111111111111",
    card_cvv="587",
    card_exp_month="12",
    card_exp_year="2030",
    card_brand="visa",
    card_holder_name="Test Holder",
    billing_address=CreditCardBillingAddress(
        city="San Francisco",
        state_code="CA",
        country_code="US",
        postal_code="94105",
    ),
)


def _context() -> WorkflowRunContext:
    return WorkflowRunContext(
        workflow_title="t",
        workflow_id="w_test",
        workflow_permanent_id="wpid_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )


async def _register(monkeypatch: pytest.MonkeyPatch) -> tuple[WorkflowRunContext, dict[str, Any]]:
    context = _context()
    parameter = MagicMock(key="card")

    db_credential = MagicMock(vault_type=None, totp_identifier=None)
    vault = MagicMock()
    vault.get_credential_item = AsyncMock(return_value=MagicMock(credential=_CARD))

    app = MagicMock()
    app.DATABASE.credentials.get_credential = AsyncMock(return_value=db_credential)
    app.CREDENTIAL_VAULT_SERVICES.get.return_value = vault
    monkeypatch.setattr(context_manager_module, "app", app)

    await context._register_credential_parameter_value(
        credential_id="cred_test",
        parameter=parameter,
        organization=MagicMock(organization_id="o_test"),
    )
    return context, context.values["card"]


@pytest.mark.asyncio
async def test_low_entropy_fields_are_not_registered_as_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    context, values = await _register(monkeypatch)

    assert values["card_brand"] == "visa"
    assert "visa" not in context.secrets.values()
    # Billing fields stay masked: the safe credential API excludes them on purpose.
    assert str(values["billing_address_state_code"]).startswith(RANDOM_SECRET_ID_PREFIX)
    assert "CA" in context.secrets.values()


@pytest.mark.asyncio
async def test_card_number_and_cvv_are_still_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    context, values = await _register(monkeypatch)

    for field in ("card_number", "card_cvv"):
        assert str(values[field]).startswith(RANDOM_SECRET_ID_PREFIX)
    assert "4111111111111111" in context.secrets.values()
    assert "587" in context.secrets.values()


@pytest.mark.asyncio
async def test_a_registered_brand_no_longer_corrupts_unrelated_output(monkeypatch: pytest.MonkeyPatch) -> None:
    context, _ = await _register(monkeypatch)

    payload = {"note": "a travel visa is required", "brand": "visa"}

    assert context.mask_secrets_in_data(payload) == payload
