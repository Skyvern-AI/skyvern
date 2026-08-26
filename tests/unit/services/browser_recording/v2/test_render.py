import json
from collections.abc import Iterator

import pytest

from skyvern.services.browser_recording.v2.ledger import Effect, Gesture, GestureKind, get_ledger, stop_ledger
from skyvern.services.browser_recording.v2.render import render_blocks, render_code
from skyvern.services.browser_recording.v2.session import (
    RecordingSessionV2,
    discard_session_v2,
    start_session_v2,
)

PBS_ID = "pbs_render_v2"
URL = "https://example.test/form"
EMAIL_VALUE = "alpha"
PASSWORD_VALUE = "omega"


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    discard_session_v2(PBS_ID)
    stop_ledger(PBS_ID)
    yield
    discard_session_v2(PBS_ID)
    stop_ledger(PBS_ID)


def _session() -> RecordingSessionV2:
    return start_session_v2(
        browser_session_id=PBS_ID,
        organization_id="org_123",
        workflow_permanent_id="wpid_123",
        on_update=lambda _update: None,
    )


class _Script:
    def __init__(self, session: RecordingSessionV2) -> None:
        ledger = get_ledger(PBS_ID)
        assert ledger is not None
        self.ledger = ledger
        self.clock = 0.0

    def _tick(self) -> float:
        self.clock += 1.0
        return self.clock

    def gesture(self, kind: GestureKind, **kwargs: object) -> None:
        self.ledger.append(Gesture(seq=0, t_received=self._tick(), kind=kind, page_key="page-1", url=URL, **kwargs))

    def navigate(self, url: str = URL, *, is_main_frame: bool = True) -> None:
        self.ledger.append_effect(
            Effect(
                seq=0,
                t_received=self._tick(),
                kind="navigation",
                page_key="page-1",
                url=url,
                is_main_frame=is_main_frame,
            )
        )

    def click(self, **target: object) -> None:
        self.gesture("mouse_pressed", x=10, y=20, button="left", click_count=1, **target)
        self.gesture("mouse_released", x=10, y=20, button="left", click_count=1)

    def type_text(self, text: str) -> None:
        for character in text:
            self.gesture("key", key=character, text=character, key_event_type="keyDown")


def _scripted_session() -> RecordingSessionV2:
    session = _session()
    script = _Script(session)
    script.navigate()
    script.click(selector="#search", role="button", accessible_name="Search", tag="button")
    script.click(selector="#email", role="textbox", accessible_name="Email", tag="input", input_type="email")
    script.type_text(EMAIL_VALUE)
    script.click(
        selector="#password",
        role="textbox",
        accessible_name="Password",
        tag="input",
        input_type="password",
        is_secret=True,
    )
    script.type_text(PASSWORD_VALUE)
    script.gesture("key", key="Enter", key_event_type="keyDown")
    session.interpret()
    return session


@pytest.mark.asyncio
async def test_render_blocks_emits_tier1_blocks_and_blank_parameters() -> None:
    result = render_blocks(_scripted_session())

    assert [block["block_type"] for block in result.blocks] == [
        "goto_url",
        "action",
        "action",
        "action",
        "action",
        "action",
        "action",
    ]
    assert [block["title"] for block in result.blocks if block["block_type"] == "action"] == [
        "Click Search",
        "Click Email",
        "Type into Email",
        "Click Password",
        "Type into Password",
        "Press Enter",
    ]
    assert [parameter["key"] for parameter in result.parameters] == ["email", "password"]
    assert all(parameter["default_value"] == "" for parameter in result.parameters)

    typed_blocks = [block for block in result.blocks if block.get("parameter_keys")]
    assert [block["parameter_keys"] for block in typed_blocks] == [["email"], ["password"]]

    rendered = json.dumps({"blocks": result.blocks, "parameters": result.parameters})
    assert "typed_length" not in rendered
    assert EMAIL_VALUE not in rendered
    assert PASSWORD_VALUE not in rendered
    assert result.diagnostics["dropped"] == 0
    assert result.diagnostics["unlocatable"] == 0


@pytest.mark.asyncio
async def test_render_code_emits_located_code_without_typed_values() -> None:
    result = render_code(_scripted_session())

    assert result is not None
    assert result.mode == "code"
    code = "\n".join(str(block["code"]) for block in result.blocks)
    assert "#search" in code
    assert "#email" in code
    assert "#password" in code

    rendered = json.dumps({"blocks": result.blocks, "parameters": result.parameters})
    assert EMAIL_VALUE not in rendered
    assert PASSWORD_VALUE not in rendered
    assert "typed_length" not in rendered
    assert all(parameter["default_value"] == "" for parameter in result.parameters)
    assert result.parameters


@pytest.mark.asyncio
async def test_render_code_falls_back_when_a_click_has_no_locator() -> None:
    session = _session()
    script = _Script(session)
    script.navigate()
    script.click(selector="#search", role="button", accessible_name="Search")
    script.click()
    session.interpret()

    assert render_code(session) is None
    assert render_blocks(session).diagnostics["unlocatable"] == 1


@pytest.mark.asyncio
async def test_a_sub_frame_navigation_is_not_a_step_in_either_renderer() -> None:
    """Only main-frame navigations are user intent; an ad iframe must not split the script."""
    session = _session()
    script = _Script(session)
    script.navigate()
    script.click(selector="#search", role="button", accessible_name="Search")
    script.navigate("https://ads.example.test/frame", is_main_frame=False)
    script.click(selector="#next", role="button", accessible_name="Next")
    session.interpret()

    blocks = render_blocks(session)
    assert [block["block_type"] for block in blocks.blocks] == ["goto_url", "action", "action"]

    code = render_code(session)
    assert code is not None
    assert len(code.blocks) == 1


@pytest.mark.asyncio
async def test_consecutive_navigations_render_the_same_way_in_both_renderers() -> None:
    session = _session()
    script = _Script(session)
    script.navigate("https://example.test/one")
    script.navigate("https://example.test/two")
    script.click(selector="#search", role="button", accessible_name="Search")
    session.interpret()

    blocks = render_blocks(session)
    assert [block["url"] for block in blocks.blocks if block["block_type"] == "goto_url"] == [
        "https://example.test/one",
        "https://example.test/two",
    ]

    code = render_code(session)
    assert code is not None
    rendered = "\n".join(str(block["code"]) for block in code.blocks)
    assert rendered.count("https://example.test/one") == 1
    assert rendered.count("https://example.test/two") == 1


@pytest.mark.asyncio
async def test_closed_shadow_row_renders_the_host_selector() -> None:
    session = _session()
    script = _Script(session)
    script.navigate()
    script.click(selector="#host", role="button", accessible_name="Submit", shadow_path=["#inner-button"])
    session.interpret()

    result = render_code(session)

    assert result is not None
    code = "\n".join(str(block["code"]) for block in result.blocks)
    assert "#host" in code
    assert "#inner-button" not in code
    assert any("closed shadow root" in note for note in result.notes)
