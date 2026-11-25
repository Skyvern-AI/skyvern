from datetime import UTC, datetime
from typing import Any

import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.routes import prompts
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.prompts import ImprovePromptRequest

FAKE_PROMPT = "Open the synthetic portal and collect fixture record 4242"
FAKE_CONTEXT = {"navigation_goal": "synthetic navigation goal 9876", "data_schema": "synthetic schema field"}


@pytest.mark.asyncio
async def test_improve_prompt_logs_lengths_not_prompt_or_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm_handler(**_kwargs: Any) -> dict[str, str]:
        return {"improved_prompt": "  improved synthetic prompt  "}

    monkeypatch.setattr(prompts.app, "LLM_API_HANDLER", fake_llm_handler)
    now = datetime.now(UTC)
    organization = Organization(organization_id="o_fake", organization_name="Fake Org", created_at=now, modified_at=now)

    with capture_logs() as logs:
        response = await prompts.improve_prompt(
            request=ImprovePromptRequest(prompt=FAKE_PROMPT, context=FAKE_CONTEXT),
            use_case="new_workflow",
            current_org=organization,
        )

    assert response.improved == "improved synthetic prompt"
    assert response.original == FAKE_PROMPT
    rendered = repr(logs)
    assert FAKE_PROMPT not in rendered
    for value in FAKE_CONTEXT.values():
        assert value not in rendered
    improving = next(entry for entry in logs if entry["event"] == "Improving prompt")
    assert improving["prompt_length"] == len(FAKE_PROMPT)
    assert improving["context_keys_count"] == len(FAKE_CONTEXT)
    improved = next(entry for entry in logs if entry["event"] == "Prompt improved")
    assert improved["prompt_length"] == len(FAKE_PROMPT)
