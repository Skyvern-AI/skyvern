from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from skyvern.cli.config_command import config_app
from skyvern.cli.mcp_tools.org import _UPDATE_FIELDS, skyvern_org_update
from skyvern.forge.sdk.schemas.organizations import OrganizationUpdate


class TestOrganizationUpdateSchema:
    def test_accepts_all_settable_fields(self) -> None:
        update = OrganizationUpdate(
            max_steps_per_run=25,
            max_retries_per_step=3,
            webhook_callback_url="https://example.com/hook",
            artifact_url_expiry_seconds=3600,
        )
        assert update.model_dump(exclude_unset=True) == {
            "max_steps_per_run": 25,
            "max_retries_per_step": 3,
            "webhook_callback_url": "https://example.com/hook",
            "artifact_url_expiry_seconds": 3600,
        }

    def test_partial_update_excludes_unset(self) -> None:
        update = OrganizationUpdate(max_steps_per_run=10)
        assert update.model_dump(exclude_unset=True) == {"max_steps_per_run": 10}

    def test_clear_artifact_flag_defaults_false(self) -> None:
        assert OrganizationUpdate().clear_artifact_url_expiry_seconds is False

    def test_rejects_non_int_max_steps(self) -> None:
        with pytest.raises(ValueError):
            OrganizationUpdate(max_steps_per_run="not a number")  # type: ignore[arg-type]

    def test_zero_max_retries_round_trips(self) -> None:
        # 0 means "disable retries" — see ForgeAgent.execute_step.
        update = OrganizationUpdate(max_retries_per_step=0)
        assert update.model_dump(exclude_unset=True) == {"max_retries_per_step": 0}

    def test_rejects_zero_max_steps_per_run(self) -> None:
        with pytest.raises(ValueError):
            OrganizationUpdate(max_steps_per_run=0)

    def test_rejects_negative_max_steps_per_run(self) -> None:
        with pytest.raises(ValueError):
            OrganizationUpdate(max_steps_per_run=-1)

    def test_rejects_negative_max_retries_per_step(self) -> None:
        with pytest.raises(ValueError):
            OrganizationUpdate(max_retries_per_step=-1)

    def test_empty_webhook_url_round_trips(self) -> None:
        # "" clears the webhook via the repository's ``is not None`` guard.
        update = OrganizationUpdate(webhook_callback_url="")
        assert update.model_dump(exclude_unset=True) == {"webhook_callback_url": ""}


class TestMcpUpdateFieldsDerivedFromSchema:
    def test_update_fields_match_schema(self) -> None:
        assert _UPDATE_FIELDS == frozenset(OrganizationUpdate.model_fields)


class TestMcpUpdateRejectsNoneValues:
    def test_explicit_none_rejected(self) -> None:
        result = asyncio.run(skyvern_org_update(updates={"max_steps_per_run": None}))
        assert not result["ok"]
        assert "None" in result["error"]["message"]


class TestConfigCli:
    def test_set_rejects_unknown_key(self) -> None:
        runner = CliRunner()
        result = runner.invoke(config_app, ["set", "totally_made_up_key", "5"])
        assert result.exit_code != 0
        assert "Unknown key" in result.output or "Unknown key" in (result.stderr or "")

    def test_get_rejects_unknown_key(self) -> None:
        runner = CliRunner()
        result = runner.invoke(config_app, ["get", "totally_made_up_key"])
        assert result.exit_code != 0

    def test_get_rejects_write_only_clear_flag(self) -> None:
        # clear_artifact_url_expiry_seconds is a verb — readable settings exclude it.
        runner = CliRunner()
        result = runner.invoke(config_app, ["get", "clear_artifact_url_expiry_seconds"])
        assert result.exit_code != 0

    def test_set_rejects_non_int_for_int_key(self) -> None:
        runner = CliRunner()
        result = runner.invoke(config_app, ["set", "max_steps_per_run", "twenty"])
        assert result.exit_code != 0

    def test_help_lists_subcommands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(config_app, ["--help"])
        assert result.exit_code == 0
        assert "show" in result.output
        assert "get" in result.output
        assert "set" in result.output
