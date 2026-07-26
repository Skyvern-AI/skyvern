"""Scout CAPTCHA solving (SKY-12967): on settled evidence the scout tries the shared
solve routes, records the encounter, and continues whether or not it passed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.copilot.challenge_evidence import (
    ChallengeEvidenceSource,
    composition_challenge_carrier,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tools.scouting import (
    _challenge_identity,
    solve_challenge_when_evidence_settles,
)
from skyvern.forge.sdk.workflow.models.block import CodeBlockCaptchaError


def _mock_page_evidence_with_challenge() -> dict[str, Any]:
    """Page evidence with a detected challenge carrier."""
    return {
        "current_url": "https://example.com/login",
        "challenge_controls": [
            {
                "tag": "div",
                "text": "Please verify you're human",
                "id": "captcha-container",
            }
        ],
        "challenge_state": {
            "detected": True,
            "evidence_source": "challenge_state",
        },
    }


def _mock_page_evidence_no_challenge() -> dict[str, Any]:
    """Page evidence with no challenge."""
    return {
        "current_url": "https://example.com/results",
        "forms": [
            {
                "fields": [
                    {"name": "q", "type": "text", "selector": "#search"},
                ],
            }
        ],
    }


def _mock_context() -> AgentContext:
    """Minimal AgentContext stand-in, restricted to attributes the real class declares.

    A SimpleNamespace accepts any attribute, so an implementation reading a field
    AgentContext does not have still passes here unless the names are checked.
    """
    attrs = {
        "organization_id": "org_123",
        "workflow_id": "w_456",
        "workflow_permanent_id": "wpid_456",
        "scouted_interactions": [],
        "scout_trajectory": [],
        "challenge_solve_attempts": {},
        "never_captured_obligation": None,
        "last_evaluate_actionable_signature": None,
        "last_scout_observation_trajectory_index": None,
    }
    declared = set(AgentContext.__annotations__)
    undeclared = sorted(set(attrs) - declared)
    assert not undeclared, f"not AgentContext fields: {undeclared}"
    return SimpleNamespace(**attrs)


@pytest.mark.asyncio
async def test_attempt_scout_captcha_solve_no_challenge() -> None:
    """When no challenge is detected, returns False without attempting solve."""
    ctx = _mock_context()
    page_evidence = _mock_page_evidence_no_challenge()

    result = await solve_challenge_when_evidence_settles(
        ctx, page_evidence, url="https://example.com/results", observed_after_interaction=True
    )

    assert result is False
    assert len(ctx.scouted_interactions) == 0


@pytest.mark.asyncio
async def test_attempt_scout_captcha_solve_no_page_evidence() -> None:
    """When page_evidence is None, returns False."""
    ctx = _mock_context()

    result = await solve_challenge_when_evidence_settles(
        ctx, None, url="https://example.com/login", observed_after_interaction=True
    )

    assert result is False
    assert len(ctx.scouted_interactions) == 0


@pytest.mark.asyncio
async def test_attempt_scout_captcha_solve_challenge_detected_and_solved() -> None:
    """When a challenge is detected and successfully solved, calls solve and records."""
    ctx = _mock_context()
    page_evidence = _mock_page_evidence_with_challenge()

    mock_page = AsyncMock()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = mock_page

    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "button.btn--login", "source_url": "https://example.com/login"}
    ]

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
        ) as mock_solve,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx, page_evidence, url="https://example.com/login", observed_after_interaction=True
        )

    assert result is True
    mock_solve.assert_called_once()
    boundary = ctx.scout_trajectory[-1]
    assert boundary["tool_name"] == "click"
    assert composition_challenge_carrier(boundary) is ChallengeEvidenceSource.CHALLENGE_STATE


@pytest.mark.asyncio
async def test_attempt_scout_captcha_solve_stamps_the_boundary_even_when_the_solve_fails() -> None:
    """A failed solve still marks the encounter, so synthesis emits at that boundary."""
    ctx = _mock_context()
    page_evidence = _mock_page_evidence_with_challenge()
    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "button.btn--login", "source_url": "https://example.com/login"}
    ]

    mock_page = AsyncMock()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = mock_page

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
            side_effect=CodeBlockCaptchaError("CAPTCHA could not be solved"),
        ),
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx, page_evidence, url="https://example.com/login", observed_after_interaction=True
        )

    assert result is False
    assert composition_challenge_carrier(ctx.scout_trajectory[-1]) is ChallengeEvidenceSource.CHALLENGE_STATE


@pytest.mark.asyncio
async def test_attempt_scout_captcha_solve_no_browser_context() -> None:
    """When browser_state is None, returns False."""
    ctx = _mock_context()
    page_evidence = _mock_page_evidence_with_challenge()

    with patch(
        "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
        return_value=None,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx, page_evidence, url="https://example.com/login", observed_after_interaction=True
        )

    assert result is False
    assert len(ctx.scouted_interactions) == 0


@pytest.mark.asyncio
async def test_attempt_scout_captcha_solve_no_page() -> None:
    """When get_working_page returns None, returns False."""
    ctx = _mock_context()
    page_evidence = _mock_page_evidence_with_challenge()

    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = None

    with patch(
        "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
        return_value=mock_browser_state,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx, page_evidence, url="https://example.com/login", observed_after_interaction=True
        )

    assert result is False
    assert len(ctx.scouted_interactions) == 0


@pytest.mark.asyncio
async def test_inspection_capture_solves_without_attributing_the_carrier() -> None:
    """A capture with no interaction behind it still solves, but stamps nothing.

    Attribution is a property of the seam: the act-observe capture follows a recorded
    interaction, an inspection capture does not, so only the former can name a boundary.
    """
    ctx = _mock_context()
    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "button.btn--login", "source_url": "https://example.com/login"}
    ]
    mock_page = AsyncMock()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = mock_page

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
        ) as mock_solve,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx,
            _mock_page_evidence_with_challenge(),
            url="https://example.com/login",
            observed_after_interaction=False,
        )

    assert result is True
    mock_solve.assert_called_once()
    assert composition_challenge_carrier(ctx.scout_trajectory[-1]) is None


@pytest.mark.asyncio
async def test_unsolvable_challenge_is_recorded_and_exploration_continues() -> None:
    """Every route failing records the encounter as unsolved and returns; it never terminates."""
    ctx = _mock_context()
    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "button.btn--login", "source_url": "https://example.com/login"}
    ]
    mock_page = AsyncMock()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = mock_page

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
            side_effect=CodeBlockCaptchaError("CAPTCHA could not be solved"),
        ),
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx,
            _mock_page_evidence_with_challenge(),
            url="https://example.com/login",
            observed_after_interaction=True,
        )

    assert result is False
    boundary = ctx.scout_trajectory[-1]
    assert composition_challenge_carrier(boundary) is ChallengeEvidenceSource.CHALLENGE_STATE
    assert boundary["challenge_state"]["outcome"] == "unsolved"


@pytest.mark.asyncio
async def test_encounter_is_recorded_even_when_no_page_is_available_to_solve_on() -> None:
    """Recording happens on detection, so an unreachable page cannot lose the fact."""
    ctx = _mock_context()
    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "button.btn--login", "source_url": "https://example.com/login"}
    ]
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = None

    with patch(
        "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
        return_value=mock_browser_state,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx,
            _mock_page_evidence_with_challenge(),
            url="https://example.com/login",
            observed_after_interaction=True,
        )

    assert result is False
    assert composition_challenge_carrier(ctx.scout_trajectory[-1]) is ChallengeEvidenceSource.CHALLENGE_STATE


@pytest.mark.asyncio
async def test_a_raising_browser_lookup_cannot_end_exploration() -> None:
    """Resolving the browser can raise; that must not propagate out and stop the turn."""
    ctx = _mock_context()
    ctx.scout_trajectory = [
        {"tool_name": "click", "selector": "button.btn--login", "source_url": "https://example.com/login"}
    ]

    with patch(
        "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
        side_effect=RuntimeError("browser session gone"),
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx,
            _mock_page_evidence_with_challenge(),
            url="https://example.com/login",
            observed_after_interaction=True,
        )

    assert result is False
    assert composition_challenge_carrier(ctx.scout_trajectory[-1]) is ChallengeEvidenceSource.CHALLENGE_STATE


@pytest.mark.asyncio
async def test_a_raising_page_lookup_cannot_end_exploration() -> None:
    """Same for the working-page lookup, which is a separate await that can raise."""
    ctx = _mock_context()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.side_effect = RuntimeError("target closed")

    with patch(
        "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
        return_value=mock_browser_state,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx,
            _mock_page_evidence_with_challenge(),
            url="https://example.com/login",
            observed_after_interaction=False,
        )

    assert result is False


@pytest.mark.asyncio
async def test_a_stuck_challenge_stops_being_paid_for_but_keeps_being_recorded() -> None:
    """The cap bounds paid attempts; the boundary record is never gated by it."""
    ctx = _mock_context()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
            side_effect=CodeBlockCaptchaError("CAPTCHA could not be solved"),
        ) as mock_solve,
    ):
        for _ in range(6):
            ctx.scout_trajectory = [{"tool_name": "click", "selector": "#s", "source_url": "u"}]
            await solve_challenge_when_evidence_settles(
                ctx,
                _mock_page_evidence_with_challenge(),
                url="https://example.com/login",
                observed_after_interaction=True,
            )
            # Recorded on every pass, including the ones the cap refused to pay for.
            assert composition_challenge_carrier(ctx.scout_trajectory[-1]) is ChallengeEvidenceSource.CHALLENGE_STATE

    assert mock_solve.await_count == 3


@pytest.mark.asyncio
async def test_a_distinct_challenge_gets_its_own_attempts() -> None:
    """Keying on the challenge, not the page, keeps a later distinct one from inheriting the cap."""
    ctx = _mock_context()
    # Key off what production actually computes; a hand-picked key can never collide and
    # would make this pass whether or not per-challenge keying works.
    spent = dict(_mock_page_evidence_with_challenge())
    spent["challenge_controls"] = [{"tag": "div", "id": "c", "data_sitekey": "sitekey-already-spent"}]
    ctx.challenge_solve_attempts = {_challenge_identity(spent): 99}
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
        ) as mock_solve,
    ):
        result = await solve_challenge_when_evidence_settles(
            ctx,
            _mock_page_evidence_with_challenge(),
            url="https://example.com/login",
            observed_after_interaction=False,
        )

    assert result is True
    mock_solve.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_rotating_widget_src_cannot_slip_the_attempt_cap() -> None:
    """Common widgets re-render with a fresh cache-buster in src; identity must not follow it."""
    ctx = _mock_context()
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = AsyncMock()

    def _evidence(nonce: str) -> dict[str, Any]:
        packet = dict(_mock_page_evidence_with_challenge())
        packet["challenge_controls"] = [
            {
                "tag": "iframe",
                "selector": "#captcha-frame",
                "src": f"https://example.com/recaptcha/api2/anchor?k=sitekey&cb={nonce}",
            }
        ]
        return packet

    assert _challenge_identity(_evidence("aaa")) == _challenge_identity(_evidence("zzz"))

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
            side_effect=CodeBlockCaptchaError("CAPTCHA could not be solved"),
        ) as mock_solve,
    ):
        for nonce in ("a", "b", "c", "d", "e", "f"):
            await solve_challenge_when_evidence_settles(
                ctx, _evidence(nonce), url="https://example.com/login", observed_after_interaction=False
            )

    assert mock_solve.await_count == 3


@pytest.mark.asyncio
async def test_a_state_only_carrier_is_not_reported_as_solved() -> None:
    """The routes return normally when their probes match nothing, so completing is not solving."""
    ctx = _mock_context()
    ctx.scout_trajectory = [{"tool_name": "click", "selector": "#s", "source_url": "u"}]
    evidence = {
        "current_url": "https://example.com/login",
        # No challenge_controls: the carrier is asserted by state alone, which the DOM probes
        # inside the solve routes cannot match.
        "challenge_state": {"evidence_source": "challenge_state", "requires_human_verification": True},
    }
    mock_browser_state = AsyncMock()
    mock_browser_state.get_working_page.return_value = AsyncMock()

    with (
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting.resolve_browser_state_for_context",
            return_value=mock_browser_state,
        ),
        patch(
            "skyvern.forge.sdk.copilot.tools.scouting._code_block_solve_captcha_builtin",
            new_callable=AsyncMock,
        ),
    ):
        await solve_challenge_when_evidence_settles(
            ctx, evidence, url="https://example.com/login", observed_after_interaction=True
        )

    assert ctx.scout_trajectory[-1]["challenge_state"]["outcome"] == "attempted"
