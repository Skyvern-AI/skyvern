from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

import scripts.backfill_encrypt_file_block_secrets as backfill
from scripts.backfill_encrypt_file_block_secrets import _non_deleted_workflows_query, encrypt_file_block_secrets
from skyvern.config import settings
from skyvern.forge.sdk.db.models import WorkflowModel
from skyvern.forge.sdk.workflow.secret_encryption import decrypt_secret_field_value, is_encrypted_secret


@pytest.fixture
def enabled_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENABLE_ENCRYPTION", True)
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SECRET_KEY", "unit-test-secret-key-please-0000")
    monkeypatch.setattr(settings, "ENCRYPTOR_AES_SALT", "unit-test-salt-000")


class _ScalarResult:
    def __init__(self, workflows: list[Any]) -> None:
        self.workflows = workflows

    def all(self) -> list[Any]:
        return self.workflows


class _FakeSession:
    def __init__(self, workflows: list[Any], events: list[str]) -> None:
        self.workflows = workflows
        self.events = events
        self.added: list[Any] = []
        self.commit = AsyncMock(side_effect=self._record_commit)
        self.rollback = AsyncMock()

    def _record_commit(self) -> None:
        self.events.append("commit")

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.events.append("exit")

    async def scalars(self, _query: Any) -> _ScalarResult:
        return _ScalarResult(self.workflows)

    def add(self, workflow: Any) -> None:
        self.added.append(workflow)


class _FakeSessionFactory:
    def __init__(self, batches: list[list[Any]], events: list[str]) -> None:
        self.sessions = [_FakeSession(batch, events) for batch in batches]
        self._remaining_sessions = iter(self.sessions)

    def __call__(self) -> _FakeSession:
        return next(self._remaining_sessions)


def _workflow(workflow_permanent_id: str, secret: str) -> SimpleNamespace:
    return SimpleNamespace(
        organization_id="o_test",
        workflow_permanent_id=workflow_permanent_id,
        workflow_definition={"blocks": [{"block_type": "file_download", "sftp_password": secret}]},
    )


def _install_fake_database(
    monkeypatch: pytest.MonkeyPatch,
    workflows: list[Any],
    delete_cached_scripts: AsyncMock,
    events: list[str],
) -> _FakeSessionFactory:
    session_factory = _FakeSessionFactory([workflows, []], events)
    database = SimpleNamespace(
        Session=session_factory,
        scripts=SimpleNamespace(delete_workflow_scripts_by_permanent_id=delete_cached_scripts),
    )
    monkeypatch.setattr(backfill.app, "DATABASE", database)
    monkeypatch.setattr(backfill, "flag_modified", lambda *_args: None)
    return session_factory


@pytest.mark.asyncio
async def test_encrypt_file_block_secrets_transforms_literals_and_is_idempotent(
    enabled_encryption: None,
) -> None:
    workflow_definition: dict[str, Any] = {
        "blocks": [
            {
                "block_type": "file_upload",
                "aws_secret_access_key": "upload-literal-secret",
                "sftp_password": "{{ sftp_password }}",
            },
            {
                "block_type": "for_loop",
                "loop_blocks": [
                    {
                        "block_type": "file_download",
                        "azure_storage_account_key": "nested-literal-secret",
                    }
                ],
            },
            {
                "block_type": "text_prompt",
                "sftp_password": "non-file-literal-secret",
            },
        ]
    }

    transformed, fields_encrypted = await encrypt_file_block_secrets(workflow_definition, "o_test")

    upload = transformed["blocks"][0]
    nested_download = transformed["blocks"][1]["loop_blocks"][0]
    non_file = transformed["blocks"][2]
    assert fields_encrypted == 2
    assert is_encrypted_secret(upload["aws_secret_access_key"])
    assert upload["sftp_password"] == "{{ sftp_password }}"
    assert is_encrypted_secret(nested_download["azure_storage_account_key"])
    assert (
        await decrypt_secret_field_value(
            upload["aws_secret_access_key"], organization_id="o_test", field_name="aws_secret_access_key"
        )
        == "upload-literal-secret"
    )
    assert (
        await decrypt_secret_field_value(
            nested_download["azure_storage_account_key"],
            organization_id="o_test",
            field_name="azure_storage_account_key",
        )
        == "nested-literal-secret"
    )
    assert non_file["sftp_password"] == "non-file-literal-secret"
    assert workflow_definition["blocks"][0]["aws_secret_access_key"] == "upload-literal-secret"
    assert workflow_definition["blocks"][1]["loop_blocks"][0]["azure_storage_account_key"] == ("nested-literal-secret")

    rerun, rerun_fields_encrypted = await encrypt_file_block_secrets(transformed, "o_test")

    assert rerun_fields_encrypted == 0
    assert rerun == transformed


def test_non_deleted_workflows_query_processes_all_versions_in_batches() -> None:
    query = _non_deleted_workflows_query(offset=200)
    compiled = str(query.compile(compile_kwargs={"literal_binds": True}))

    assert "workflows.deleted_at IS NULL" in compiled
    assert "max(" not in compiled.lower()
    assert "group by" not in compiled.lower()
    assert query._limit_clause is not None
    assert query._offset_clause is not None
    assert query._limit_clause.value == 100
    assert query._offset_clause.value == 200
    assert list(query._order_by_clauses) == [
        WorkflowModel.workflow_permanent_id,
        WorkflowModel.version,
        WorkflowModel.workflow_id,
    ]


@pytest.mark.asyncio
async def test_main_invalidates_cached_scripts_once_per_modified_wpid_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    enabled_encryption: None,
) -> None:
    events: list[str] = []

    async def record_delete(*, organization_id: str, workflow_permanent_id: str) -> int:
        events.append(f"delete:{workflow_permanent_id}")
        return 1

    delete_cached_scripts = AsyncMock(side_effect=record_delete)
    sessions = _install_fake_database(
        monkeypatch,
        [
            _workflow("wp_one", "first-secret"),
            _workflow("wp_one", "second-secret"),
            _workflow("wp_two", "third-secret"),
        ],
        delete_cached_scripts,
        events,
    )
    monkeypatch.setattr(backfill, "_parse_args", lambda: SimpleNamespace(dry_run=False))

    await backfill.main()

    assert delete_cached_scripts.await_args_list == [
        call(organization_id="o_test", workflow_permanent_id="wp_one"),
        call(organization_id="o_test", workflow_permanent_id="wp_two"),
    ]
    sessions.sessions[0].commit.assert_awaited_once_with()
    assert events == ["delete:wp_one", "delete:wp_two", "commit", "exit", "exit"]


@pytest.mark.asyncio
async def test_main_dry_run_does_not_invalidate_cached_scripts(
    monkeypatch: pytest.MonkeyPatch,
    enabled_encryption: None,
) -> None:
    events: list[str] = []
    delete_cached_scripts = AsyncMock()
    sessions = _install_fake_database(
        monkeypatch,
        [_workflow("wp_dry_run", "dry-run-secret")],
        delete_cached_scripts,
        events,
    )
    monkeypatch.setattr(backfill, "_parse_args", lambda: SimpleNamespace(dry_run=True))

    await backfill.main()

    delete_cached_scripts.assert_not_awaited()
    sessions.sessions[0].commit.assert_not_awaited()
