from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from skyvern.forge.sdk.copilot.heal_content_sanitization import (
    build_heal_episode_detail,
    mask_heal_steps,
    mask_heal_text,
    sanitize_heal_content,
)
from skyvern.schemas.self_heal import HealEpisode


def test_sanitize_heal_content_redacts_email_password_and_colon_secret() -> None:
    text = "Use qa.user@example.test:FakePass123! to sign in.\napi_key: MySecretValue987"

    sanitized = sanitize_heal_content(text)

    assert sanitized is not None
    assert "FakePass123!" not in sanitized
    assert "MySecretValue987" not in sanitized
    assert sanitized.count("[REDACTED_SECRET]") >= 2


def test_sanitize_heal_content_redacts_placeholder_tokens() -> None:
    sanitized = sanitize_heal_content("username=placeholder_ABCD password=placeholder_a1B2c3")

    assert sanitized is not None
    assert "placeholder_ABCD" not in sanitized
    assert "placeholder_a1B2c3" not in sanitized
    assert sanitized.count("[REDACTED_SECRET]") == 2


def test_mask_heal_text_scrubs_registered_novel_value() -> None:
    ctx = SimpleNamespace(secret_scrub_values=["Zq9xNovelSecretNoKnownShape"], browser_session_id=None)

    sanitized = mask_heal_text(ctx, 'x = "Zq9xNovelSecretNoKnownShape"')

    assert sanitized is not None
    assert "Zq9xNovelSecretNoKnownShape" not in sanitized
    assert "[REDACTED_SECRET]" in sanitized


def test_sanitize_heal_content_does_not_corrupt_non_placeholder_identifiers() -> None:
    sanitized = sanitize_heal_content("block_placeholder_2 = 1")

    assert sanitized is not None
    assert "block_placeholder_2" in sanitized


def test_sanitize_heal_content_redacts_full_placeholder_token_with_suffix() -> None:
    sanitized = sanitize_heal_content('fill("#pw", "placeholder_ABCD_password")')

    assert sanitized is not None
    assert "placeholder_ABCD_password" not in sanitized
    assert "_password" not in sanitized


def test_mask_heal_steps_scrubs_registered_novel_value() -> None:
    ctx = SimpleNamespace(secret_scrub_values=["Zq9xNovelSecretNoKnownShape"], browser_session_id=None)

    sanitized = mask_heal_steps(ctx, [{"selector": "#pw", "value": "Zq9xNovelSecretNoKnownShape"}])

    assert isinstance(sanitized, list)
    assert sanitized[0]["value"] == "[REDACTED_SECRET]"


def test_sanitize_heal_content_truncates_and_appends_marker() -> None:
    sanitized = sanitize_heal_content("x" * 30, max_length=10)
    assert sanitized == ("x" * 10) + "\n…[truncated]"


def test_sanitize_heal_content_none_passthrough() -> None:
    assert sanitize_heal_content(None) is None


def test_sanitize_heal_content_empty_string_passthrough() -> None:
    assert sanitize_heal_content("") == ""


def test_sanitize_heal_content_is_idempotent() -> None:
    single = sanitize_heal_content("A" * 25, max_length=8)
    double = sanitize_heal_content(single, max_length=8)
    assert double == single


def test_build_heal_episode_detail_sanitizes_and_drops_raw_fields() -> None:
    now = datetime.now(timezone.utc)
    episode = HealEpisode(
        heal_episode_id="he_123",
        organization_id="o_123",
        workflow_permanent_id="wpid_123",
        workflow_id="w_123",
        workflow_run_id="wr_123",
        workflow_run_block_id="wrb_123",
        block_label="login_block",
        engine="code",
        status="fired_failed",
        block_prompt="Use qa.user@example.test:FakePass123! to sign in",
        block_code="api_key: MyRawApiSecret\nx = placeholder_ABCD",
        failure_message="Password: AnotherRawSecret",
        created_at=now,
        modified_at=now,
    )

    detail = build_heal_episode_detail(episode)

    assert "FakePass123!" not in (detail.sanitized_block_prompt or "")
    assert "MyRawApiSecret" not in (detail.sanitized_block_code or "")
    assert "placeholder_ABCD" not in (detail.sanitized_block_code or "")
    assert "AnotherRawSecret" not in (detail.sanitized_failure_message or "")
    assert not hasattr(detail, "block_code")


def test_build_heal_episode_detail_sanitizes_block_steps_string_leaves() -> None:
    now = datetime.now(timezone.utc)
    episode = HealEpisode(
        heal_episode_id="he_123",
        organization_id="o_123",
        workflow_permanent_id="wpid_123",
        workflow_id="w_123",
        workflow_run_id="wr_123",
        workflow_run_block_id="wrb_123",
        block_label="login_block",
        engine="code",
        status="fired_failed",
        block_steps=[{"selector": "#pw", "value": "sk-live-secretsecretsecret"}],
        created_at=now,
        modified_at=now,
    )

    detail = build_heal_episode_detail(episode)

    assert isinstance(detail.block_steps, list)
    assert detail.block_steps[0]["value"] == "[REDACTED_SECRET]"
