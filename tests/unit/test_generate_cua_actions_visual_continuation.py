"""Regression coverage for the OpenAI CUA continuation when the prior response has no computer_call.

When the previous CUA response contains no `computer_call`, Skyvern answers the model's
question via `cua-answer-question` and continues the conversation. The current-frame invariant
is that this no-call continuation carries the current browser screenshot alongside the textual
answer, matching the `computer_call_output` branch, which already forwards the current frame. This
keeps both continuation paths at parity so the model always reasons over the latest screenshot.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.models import StepStatus
from tests.unit.helpers import make_organization, make_step, make_task

SCREENSHOT_BYTES = b"\x89PNG\r\n\x1a\nfake-screenshot-bytes"
HELPER_ANSWER = "I'll scroll down to see the rest of the page."


def _fake_usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )


def _flatten_input_message(create_input: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return (texts, image_urls) contained in a Responses-API user message input."""
    texts: list[str] = []
    image_urls: list[str] = []
    for item in create_input:
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if part.get("type") in ("input_text", "text") and part.get("text"):
                    texts.append(part["text"])
                elif part.get("type") == "input_image" and part.get("image_url"):
                    image_urls.append(part["image_url"])
    return texts, image_urls


async def _invoke_no_call_continuation(
    monkeypatch: pytest.MonkeyPatch, helper_answer: str | None = HELPER_ANSWER
) -> AsyncMock:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="cua-step", status=StepStatus.created, order=0, output=None)

    previous_response = SimpleNamespace(id="resp_prev", output=[])
    scraped_page = SimpleNamespace(screenshots=[SCREENSHOT_BYTES])

    create_mock = AsyncMock(return_value=SimpleNamespace(id="resp_new", output=[], usage=_fake_usage()))
    monkeypatch.setattr(
        agent_module.app,
        "OPENAI_CLIENT",
        SimpleNamespace(responses=SimpleNamespace(create=create_mock)),
        raising=False,
    )
    monkeypatch.setattr(
        agent_module.app,
        "DATABASE",
        SimpleNamespace(tasks=SimpleNamespace(update_step=AsyncMock())),
        raising=False,
    )

    monkeypatch.setattr(agent_module, "load_prompt_with_elements", MagicMock(return_value="prompt"))
    monkeypatch.setattr(
        agent_module.skyvern_context,
        "ensure_context",
        lambda: SimpleNamespace(totp_codes={}),
    )
    monkeypatch.setattr(
        agent_module,
        "get_org_aware_primary_llm_api_handler",
        lambda default=None: AsyncMock(return_value={"answer": helper_answer}),
    )
    monkeypatch.setattr(agent_module, "parse_cua_actions", AsyncMock(return_value=[]))

    await agent._generate_cua_actions(
        task=task, step=step, scraped_page=scraped_page, previous_response=previous_response
    )
    return create_mock


@pytest.mark.asyncio
async def test_no_computer_call_continuation_includes_answer_and_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_mock = await _invoke_no_call_continuation(monkeypatch)

    create_mock.assert_awaited_once()
    create_input = create_mock.await_args.kwargs["input"]
    texts, image_urls = _flatten_input_message(create_input)

    assert any(HELPER_ANSWER in text for text in texts)

    expected_data_url = f"data:image/png;base64,{base64.b64encode(SCREENSHOT_BYTES).decode('utf-8')}"
    assert expected_data_url in image_urls


@pytest.mark.asyncio
async def test_truthy_answer_appends_execute_directive(monkeypatch: pytest.MonkeyPatch) -> None:
    create_mock = await _invoke_no_call_continuation(monkeypatch)

    create_input = create_mock.await_args.kwargs["input"]
    texts, _ = _flatten_input_message(create_input)

    combined = next(text for text in texts if HELPER_ANSWER in text)
    assert agent_module.CUA_EXECUTE_ACTIONS_DIRECTIVE in combined


@pytest.mark.asyncio
async def test_no_answer_default_question_omits_directive(monkeypatch: pytest.MonkeyPatch) -> None:
    create_mock = await _invoke_no_call_continuation(monkeypatch, helper_answer="")

    create_input = create_mock.await_args.kwargs["input"]
    texts, _ = _flatten_input_message(create_input)

    assert any("I don't know." in text for text in texts)
    assert all(agent_module.CUA_EXECUTE_ACTIONS_DIRECTIVE not in text for text in texts)


@pytest.mark.asyncio
async def test_computer_call_output_continuation_shape_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = ForgeAgent()
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(now, organization)
    step = make_step(now, task, step_id="cua-step", status=StepStatus.created, order=0, output=None)

    computer_call = SimpleNamespace(
        type="computer_call",
        call_id="call_123",
        action=SimpleNamespace(type="scroll"),
        pending_safety_checks=[],
    )
    previous_response = SimpleNamespace(id="resp_prev", output=[computer_call])
    scraped_page = SimpleNamespace(screenshots=[SCREENSHOT_BYTES])

    create_mock = AsyncMock(return_value=SimpleNamespace(id="resp_new", output=[], usage=_fake_usage()))
    monkeypatch.setattr(
        agent_module.app,
        "OPENAI_CLIENT",
        SimpleNamespace(responses=SimpleNamespace(create=create_mock)),
        raising=False,
    )
    monkeypatch.setattr(
        agent_module.app,
        "DATABASE",
        SimpleNamespace(tasks=SimpleNamespace(update_step=AsyncMock())),
        raising=False,
    )
    monkeypatch.setattr(agent_module, "parse_cua_actions", AsyncMock(return_value=[]))

    await agent._generate_cua_actions(
        task=task, step=step, scraped_page=scraped_page, previous_response=previous_response
    )

    create_input = create_mock.await_args.kwargs["input"]
    expected_data_url = f"data:image/png;base64,{base64.b64encode(SCREENSHOT_BYTES).decode('utf-8')}"
    assert create_input == [
        {
            "call_id": "call_123",
            "type": "computer_call_output",
            "output": {"type": "input_image", "image_url": expected_data_url},
        }
    ]
