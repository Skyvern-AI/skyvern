import pytest

from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.schemas.credentials import CredentialType, SecretCredential


@pytest.mark.asyncio
async def test_default_credential_write_validation_is_a_noop() -> None:
    result = await AgentFunction().validate_credential_write(
        organization_id="org_test",
        credential_type=CredentialType.SECRET,
        credential=SecretCredential(secret_value="opaque-value"),
    )

    assert result is None


@pytest.mark.asyncio
async def test_default_credential_write_lock_is_a_noop() -> None:
    async with AgentFunction().credential_write_lock(
        organization_id="org_test",
        credential_id="cred_test",
    ):
        pass
