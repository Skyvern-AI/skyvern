"""Tests for page URL propagation in action summaries and reviewer templates."""

from skyvern.utils.css_selector import build_action_summary
from skyvern.webeye.actions.actions import Action, ActionType


def _make_action(page_url: str | None = None, **kwargs) -> Action:
    """Create a minimal Action with optional page_url in skyvern_element_data."""
    element_data = {"page_url": page_url} if page_url else {}
    defaults = {
        "action_type": ActionType.CLICK,
        "status": "completed",
        "intention": "Click button",
        "skyvern_element_data": element_data or None,
    }
    defaults.update(kwargs)
    return Action(**defaults)


def test_build_action_summary_includes_page_url():
    """page_url from skyvern_element_data is included in the summary."""
    action = _make_action(page_url="https://example.com/login")
    summary = build_action_summary(action)
    assert summary["page_url"] == "https://example.com/login"


def test_build_action_summary_strips_query_params():
    """Query params are stripped to avoid leaking OAuth tokens, emails, session IDs."""
    action = _make_action(page_url="https://sso.example.com/auth?code=abc123&state=xyz&email=user@corp.com")
    summary = build_action_summary(action)
    assert summary["page_url"] == "https://sso.example.com/auth"
    assert "abc123" not in summary["page_url"]
    assert "email" not in summary["page_url"]


def test_build_action_summary_page_url_none_when_missing():
    """page_url is None when skyvern_element_data has no page_url."""
    action = _make_action()
    summary = build_action_summary(action)
    assert summary["page_url"] is None


def test_build_action_summary_page_url_with_element_data():
    """page_url coexists with element attributes without interference."""
    action = Action(
        action_type=ActionType.INPUT_TEXT,
        status="completed",
        intention="Fill email",
        skyvern_element_data={
            "page_url": "https://sso.example.com/auth",
            "tagName": "input",
            "attributes": {"id": "email", "type": "text"},
        },
    )
    summary = build_action_summary(action)
    assert summary["page_url"] == "https://sso.example.com/auth"
    assert summary["element_tag"] == "input"
    assert summary["all_attributes"]["id"] == "email"


def test_build_action_summary_includes_selector_options():
    """selector_options shows ALL viable selectors ranked by stability."""
    action = Action(
        action_type=ActionType.INPUT_TEXT,
        status="completed",
        intention="Fill passcode",
        skyvern_element_data={
            "tagName": "input",
            "attributes": {"id": "input61", "name": "credentials.passcode", "type": "password"},
        },
    )
    summary = build_action_summary(action)
    assert summary["selector_options"] is not None
    selectors = [s for s, _ in summary["selector_options"]]
    assert 'input[name="credentials.passcode"]' in selectors
    assert "#input61" in selectors


def test_build_action_summary_selector_options_none_when_no_element():
    """selector_options is None when no element data."""
    action = _make_action()
    summary = build_action_summary(action)
    assert summary["selector_options"] is None


def test_template_url_rendering():
    """The reviewer template renders [url: ...] and [url changed: ...] correctly."""
    from skyvern.forge.prompts import prompt_engine

    episodes = [
        {
            "block_label": "login",
            "fallback_type": "full_block",
            "error_message": "Selector failed",
            "classify_result": None,
            "agent_actions": {
                "actions": [
                    {
                        "action_type": "click",
                        "intention": "Click Continue",
                        "status": "completed",
                        "page_url": "https://portal.example.com/login",
                    },
                    {
                        "action_type": "input_text",
                        "intention": "Fill password",
                        "status": "completed",
                        "page_url": "https://portal.example.com/login",
                    },
                    {
                        "action_type": "click",
                        "intention": "Click Submit",
                        "status": "completed",
                        "page_url": "https://sso.example.com/auth",
                    },
                    {
                        "action_type": "input_text",
                        "intention": "Fill SSO password",
                        "status": "completed",
                        "page_url": "https://sso.example.com/auth",
                    },
                ],
            },
            "page_url": "https://portal.example.com/login",
            "page_text_snapshot": "Login page",
        }
    ]

    prompt = prompt_engine.load_prompt(
        template="script-reviewer",
        navigation_goal="Log in to the portal",
        existing_code="async def login(page, context): pass",
        episodes=episodes,
        function_signature="async def login(page: SkyvernPage, context: RunContext)",
        stale_branches=[],
        parameter_keys=["username", "password"],
        historical_episodes=[],
        run_parameter_values={},
        user_instructions=None,
    )

    # First action should show [url: portal.example.com/login]
    assert "[url: https://portal.example.com/login]" in prompt
    # Third action should show page navigation to sso.example.com/auth
    assert "page navigated to https://sso.example.com/auth" in prompt
    # Second action (same URL as first) should NOT show url
    lines = prompt.split("\n")
    action_2_lines = [line for line in lines if "2. input_text:" in line]
    assert action_2_lines, "Action 2 should exist"
    assert "[url:" not in action_2_lines[0]
    assert "page navigated" not in action_2_lines[0]


class TestBuildActionSummariesWithTiming:
    """Tests for build_action_summaries_with_timing shared helper."""

    def _make_action_with_timestamp(self, ts, **kwargs):
        from skyvern.webeye.actions.actions import Action, ActionType

        defaults = {
            "action_type": ActionType.CLICK,
            "status": "completed",
            "intention": "Click button",
            "created_at": ts,
            "skyvern_element_data": None,
        }
        defaults.update(kwargs)
        return Action(**defaults)

    def test_computes_deltas_between_actions(self):
        from datetime import datetime, timezone

        from skyvern.utils.css_selector import build_action_summaries_with_timing

        t0 = datetime(2026, 4, 6, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 4, 6, 12, 0, 2, tzinfo=timezone.utc)  # +2s
        t2 = datetime(2026, 4, 6, 12, 0, 9, tzinfo=timezone.utc)  # +7s

        actions = [
            self._make_action_with_timestamp(t0),
            self._make_action_with_timestamp(t1),
            self._make_action_with_timestamp(t2),
        ]
        summaries = build_action_summaries_with_timing(actions)
        assert len(summaries) == 3
        assert "seconds_since_previous" not in summaries[0]  # first action, no previous
        assert summaries[1]["seconds_since_previous"] == 2.0
        assert summaries[2]["seconds_since_previous"] == 7.0

    def test_handles_none_timestamps(self):
        from skyvern.utils.css_selector import build_action_summaries_with_timing

        actions = [
            self._make_action_with_timestamp(None),
            self._make_action_with_timestamp(None),
        ]
        summaries = build_action_summaries_with_timing(actions)
        assert len(summaries) == 2
        assert "seconds_since_previous" not in summaries[0]
        assert "seconds_since_previous" not in summaries[1]

    def test_negative_delta_clamped_to_zero(self):
        from datetime import datetime, timezone

        from skyvern.utils.css_selector import build_action_summaries_with_timing

        t0 = datetime(2026, 4, 6, 12, 0, 10, tzinfo=timezone.utc)
        t1 = datetime(2026, 4, 6, 12, 0, 5, tzinfo=timezone.utc)  # earlier (clock skew)

        actions = [
            self._make_action_with_timestamp(t0),
            self._make_action_with_timestamp(t1),
        ]
        summaries = build_action_summaries_with_timing(actions)
        # Negative delta clamped to 0
        assert summaries[1]["seconds_since_previous"] == 0.0
