import argparse
import asyncio
import copy
from typing import Any

import structlog
from sqlalchemy import Select, select
from sqlalchemy.orm.attributes import flag_modified

from skyvern.forge import app
from skyvern.forge.forge_app_initializer import start_forge_app
from skyvern.forge.sdk.db.models import WorkflowModel
from skyvern.forge.sdk.workflow.secret_encryption import (
    SENSITIVE_DESTINATION_FIELDS,
    encrypt_secret_field_value,
    encryption_available,
)

LOG = structlog.get_logger()
BATCH_SIZE = 100
FILE_BLOCK_TYPES = frozenset({"file_upload", "file_download"})
LOOP_BLOCK_TYPES = frozenset({"for_loop", "while_loop"})


async def encrypt_file_block_secrets(
    workflow_definition: dict[str, Any], organization_id: str | None
) -> tuple[dict[str, Any], int]:
    transformed = copy.deepcopy(workflow_definition)

    async def encrypt_blocks(blocks: Any) -> int:
        if not isinstance(blocks, list):
            return 0

        fields_encrypted = 0
        for block in blocks:
            if not isinstance(block, dict):
                continue

            block_type = block.get("block_type")
            if block_type in FILE_BLOCK_TYPES:
                for field_name in SENSITIVE_DESTINATION_FIELDS:
                    value = block.get(field_name)
                    if not isinstance(value, str):
                        continue

                    encrypted_value = await encrypt_secret_field_value(
                        value, organization_id=organization_id, field_name=field_name
                    )
                    if encrypted_value != value:
                        block[field_name] = encrypted_value
                        fields_encrypted += 1
            elif block_type in LOOP_BLOCK_TYPES:
                fields_encrypted += await encrypt_blocks(block.get("loop_blocks"))

        return fields_encrypted

    fields_encrypted = await encrypt_blocks(transformed.get("blocks"))
    return transformed, fields_encrypted


def _contains_file_block(blocks: Any) -> bool:
    if not isinstance(blocks, list):
        return False

    for block in blocks:
        if not isinstance(block, dict):
            continue

        block_type = block.get("block_type")
        if block_type in FILE_BLOCK_TYPES:
            return True
        if block_type in LOOP_BLOCK_TYPES and _contains_file_block(block.get("loop_blocks")):
            return True

    return False


def _non_deleted_workflows_query(offset: int) -> Select[tuple[WorkflowModel]]:
    return (
        select(WorkflowModel)
        .where(WorkflowModel.deleted_at.is_(None))
        .order_by(WorkflowModel.workflow_permanent_id, WorkflowModel.version, WorkflowModel.workflow_id)
        .limit(BATCH_SIZE)
        .offset(offset)
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Encrypt literal secrets in stored workflow file blocks.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", dest="dry_run", help="Report changes without writing.")
    mode.add_argument("--commit", action="store_false", dest="dry_run", help="Write encrypted values to the database.")
    parser.set_defaults(dry_run=True)
    return parser.parse_args()


def _require_encryption() -> None:
    if not encryption_available():
        raise SystemExit(
            "File block secret backfill requires authenticated encryption. Set ENABLE_ENCRYPTION=true and configure "
            "ENCRYPTOR_AES_SECRET_KEY (and optionally ENCRYPTOR_AES_SALT)."
        )


async def main() -> None:
    args = _parse_args()
    _require_encryption()

    workflows_scanned = 0
    workflows_changed = 0
    fields_encrypted = 0
    scripts_invalidated = 0
    invalidated: set[tuple[str, str]] = set()
    offset = 0

    LOG.info("Starting file block secret backfill", dry_run=args.dry_run, batch_size=BATCH_SIZE)
    while True:
        batch_invalidations: list[tuple[str, str]] = []
        batch_keys: set[tuple[str, str]] = set()
        async with app.DATABASE.Session() as session:
            workflows = (await session.scalars(_non_deleted_workflows_query(offset))).all()
            if not workflows:
                break

            workflows_scanned += len(workflows)
            for workflow in workflows:
                definition = workflow.workflow_definition
                if not isinstance(definition, dict) or not _contains_file_block(definition.get("blocks")):
                    continue

                transformed, encrypted_count = await encrypt_file_block_secrets(definition, workflow.organization_id)
                if encrypted_count == 0:
                    continue

                workflows_changed += 1
                fields_encrypted += encrypted_count
                if not args.dry_run:
                    workflow.workflow_definition = transformed
                    flag_modified(workflow, "workflow_definition")
                    session.add(workflow)
                    key = (workflow.organization_id, workflow.workflow_permanent_id)
                    if key not in invalidated and key not in batch_keys:
                        batch_invalidations.append(key)
                        batch_keys.add(key)

            if not args.dry_run:
                try:
                    # Invalidate cached scripts BEFORE committing the encrypted definitions: if a
                    # delete fails, the batch aborts and a rerun retries both. Committing first
                    # would let a failed invalidation leave stale plaintext that a rerun (seeing an
                    # already-encrypted definition) would never retry.
                    for organization_id, workflow_permanent_id in batch_invalidations:
                        if (organization_id, workflow_permanent_id) in invalidated:
                            continue
                        scripts_invalidated += await app.DATABASE.scripts.delete_workflow_scripts_by_permanent_id(
                            organization_id=organization_id,
                            workflow_permanent_id=workflow_permanent_id,
                        )
                        invalidated.add((organization_id, workflow_permanent_id))
                    await session.commit()
                except Exception:
                    await session.rollback()
                    LOG.error("Failed to commit file block secret backfill batch", batch_offset=offset)
                    raise RuntimeError("Failed to commit file block secret backfill batch") from None

            offset += len(workflows)

    LOG.info(
        "File block secret backfill complete",
        dry_run=args.dry_run,
        workflows_scanned=workflows_scanned,
        workflows_changed=workflows_changed,
        fields_encrypted=fields_encrypted,
        scripts_invalidated=scripts_invalidated,
    )


if __name__ == "__main__":
    start_forge_app()
    asyncio.run(main())
