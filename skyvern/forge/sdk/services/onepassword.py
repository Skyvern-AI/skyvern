import asyncio
import json
import os
from enum import StrEnum
from typing import Dict, Optional

import structlog
from pydantic import BaseModel

LOG = structlog.get_logger()


class OnePasswordConstants(StrEnum):
    TOKEN = "OP_TOKEN"
    VAULT_ID = "OP_VAULT_ID"
    ITEM_ID = "OP_ITEM_ID"
    USERNAME = "OP_USERNAME"
    PASSWORD = "OP_PASSWORD"
    TOTP = "OP_TOTP"


class RunCommandResult(BaseModel):
    stdout: str
    stderr: str
    returncode: int


class OnePasswordService:
    @staticmethod
    async def run_command(
        command: list[str], additional_env: Optional[Dict[str, str]] = None, timeout: int = 60
    ) -> RunCommandResult:
        env = os.environ.copy()
        if additional_env:
            env.update(additional_env)
        try:
            async with asyncio.timeout(timeout):
                shell_subprocess = await asyncio.create_subprocess_shell(
                    " ".join(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await shell_subprocess.communicate()
                return RunCommandResult(
                    stdout=stdout.decode(),
                    stderr=stderr.decode(),
                    returncode=shell_subprocess.returncode,
                )
        except asyncio.TimeoutError as e:
            LOG.error("1Password command timed out", exc_info=True)
            raise e

    @staticmethod
    async def get_login_item(token: str, vault_id: str, item_id: str) -> Dict[str, str]:
        env = {"OP_SESSION": token}
        command = ["op", "item", "get", item_id, "--vault", vault_id, "--format", "json"]
        result = await OnePasswordService.run_command(command, env)
        if result.returncode != 0:
            raise RuntimeError(f"op item get failed: {result.stderr}")
        try:
            data = json.loads(result.stdout)
        except Exception as e:
            raise RuntimeError(f"Failed to parse op item output: {e}")

        username = ""
        password = ""
        totp = ""
        for field in data.get("fields", []):
            label = (field.get("label") or "").lower()
            value = field.get("value", "")
            if label == "username" and not username:
                username = value
            elif label == "password" and not password:
                password = value
            elif label in {"one-time password", "otp", "totp"}:
                totp = value
        return {
            OnePasswordConstants.USERNAME: username,
            OnePasswordConstants.PASSWORD: password,
            OnePasswordConstants.TOTP: totp,
        }
