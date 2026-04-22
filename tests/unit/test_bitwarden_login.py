import pytest

from skyvern.forge.sdk.services.bitwarden import BitwardenService, RunCommandResult


@pytest.mark.asyncio
async def test_login_ignores_data_file_creation_notice_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_command(*args, **kwargs) -> RunCommandResult:
        return RunCommandResult(
            stdout="You are logged in!\n\nTo unlock your vault, use the `unlock` command.",
            stderr='Could not find data file, "/tmp/bitwarden/data.json"; creating it instead.\n',
            returncode=0,
        )

    monkeypatch.setattr(BitwardenService, "run_command", fake_run_command)

    await BitwardenService.login("client-id", "client-secret", master_password="master-password")
