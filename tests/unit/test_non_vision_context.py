from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.core.skyvern_context import LLMVisionMode, SkyvernContext
from skyvern.forge.sdk.prompting import NON_VISION_CONTEXT_HEADER, _with_non_vision_context
from skyvern.webeye.scraper import non_vision_context


def test_accessibility_context_script_keeps_backslash_regex_escaped() -> None:
    assert '/["\\\\]/g' in non_vision_context._ACCESSIBILITY_CONTEXT_SCRIPT
    assert '/["\\]/g' not in non_vision_context._ACCESSIBILITY_CONTEXT_SCRIPT


class FakePage:
    async def evaluate(self, _script: str, args: dict) -> dict:
        return {
            "title": "Example",
            "url": "https://example.test",
            "visible_text": "Save changes",
            "accessibility_tree": [
                {
                    "skyvern_id": "abc",
                    "tag": "button",
                    "role": "button",
                    "name": "Save",
                }
            ],
            "max_nodes": args["maxNodes"],
        }


class SensitiveFakePage:
    async def evaluate(self, _script: str, _args: dict) -> dict:
        return {
            "visible_text": "Password hunter2 One-time code 123456 Email user@example.test",
            "accessibility_tree": [
                {
                    "tag": "input",
                    "role": "textbox",
                    "name": "Password",
                    "type": "password",
                    "value": "hunter2",
                },
                {
                    "tag": "input",
                    "role": "textbox",
                    "name": "One-time code",
                    "type": "text",
                    "autocomplete": "one-time-code",
                    "value": "123456",
                },
                {
                    "tag": "input",
                    "role": "textbox",
                    "name": "Email",
                    "type": "email",
                    "value": "user@example.test",
                },
            ],
        }


class LargeFakePage:
    async def evaluate(self, _script: str, _args: dict) -> dict:
        return {
            "title": "Large page",
            "url": "https://example.test/large",
            "visible_text": "Visible text " * 100,
            "accessibility_tree": [
                {
                    "tag": "button",
                    "role": "button",
                    "name": f"Button {index}",
                    "text": "Button text " * 20,
                }
                for index in range(20)
            ],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        LLMVisionMode.CONTROL,
        LLMVisionMode.FALLBACK_WITHOUT_A11Y,
    ],
)
async def test_build_non_vision_context_if_needed_omits_accessibility_for_modes_without_a11y(
    monkeypatch: pytest.MonkeyPatch,
    mode: LLMVisionMode,
) -> None:
    monkeypatch.setattr(
        non_vision_context.skyvern_context,
        "current",
        lambda: SkyvernContext(llm_vision_mode=mode),
    )

    rendered = await non_vision_context.build_non_vision_page_context_if_needed(page=FakePage())

    assert rendered is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        LLMVisionMode.NO_IMAGES_WITH_A11Y,
        LLMVisionMode.FALLBACK_WITH_A11Y,
    ],
)
async def test_build_non_vision_context_includes_accessibility_tree_for_a11y_modes(
    monkeypatch: pytest.MonkeyPatch,
    mode: LLMVisionMode,
) -> None:
    monkeypatch.setattr(
        non_vision_context.skyvern_context,
        "current",
        lambda: SkyvernContext(llm_vision_mode=mode),
    )
    scraped_page = SimpleNamespace(url="https://fallback.test", extracted_text="Fallback text")

    rendered = await non_vision_context.build_non_vision_page_context_if_needed(
        scraped_page=scraped_page,
        page=FakePage(),
    )

    assert rendered is not None
    assert '"accessibility_tree"' in rendered
    assert '"skyvern_id":"abc"' in rendered
    assert '"visible_text":"Save changes"' in rendered


@pytest.mark.asyncio
async def test_build_non_vision_context_redacts_sensitive_input_values() -> None:
    scraped_page = SimpleNamespace(
        url="https://example.test/login",
        extracted_text="Fallback text with password hunter2 and one-time code 123456",
    )

    rendered = await non_vision_context.build_non_vision_page_context(
        page=SensitiveFakePage(), scraped_page=scraped_page
    )

    assert rendered is not None
    assert "hunter2" not in rendered
    assert "123456" not in rendered
    assert rendered.count('"value_redacted":true') == 2
    assert "user@example.test" in rendered
    assert "visible_text" not in json.loads(rendered)


@pytest.mark.asyncio
async def test_build_non_vision_context_truncates_as_valid_json() -> None:
    rendered = await non_vision_context.build_non_vision_page_context(page=LargeFakePage(), max_chars=250)

    assert rendered is not None
    assert len(rendered) <= 250
    parsed = json.loads(rendered)
    assert parsed["truncated"] is True


def test_prompt_engine_appends_non_vision_context_once() -> None:
    rendered = _with_non_vision_context(
        "Base prompt",
        "skyvern/example",
        {"non_vision_page_context": '{"accessibility_tree":[]}'},
    )

    assert rendered.count(NON_VISION_CONTEXT_HEADER) == 1
    assert '{"accessibility_tree":[]}' in rendered
    assert _with_non_vision_context(rendered, "skyvern/example", {"non_vision_page_context": "{}"}) == rendered


def test_prompt_engine_does_not_append_non_vision_context_to_static_cache_template() -> None:
    rendered = _with_non_vision_context(
        "Static prompt",
        "skyvern/extract-action-static",
        {"non_vision_page_context": '{"accessibility_tree":[]}'},
    )

    assert rendered == "Static prompt"
