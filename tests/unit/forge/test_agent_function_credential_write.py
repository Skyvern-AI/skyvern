import pytest

from skyvern.forge.agent_functions import AgentFunction


@pytest.mark.asyncio
async def test_default_organization_credential_secret_policy_is_a_noop() -> None:
    assert await AgentFunction().should_redact_organization_credential_secrets(None, "org_test") is False
