"""Round-trip tests for the update-path parameter reconcile helpers.

Exercises ``WorkflowsRepository.update_workflow_and_reconcile_definition_params``
against an in-memory SQLite DB so add / update / remove / type-change / revive
semantics plus the critical ID-preservation invariant are verified end to end.

The ID-preservation invariant is the load-bearing one: workflow_run_parameters
and workflow_run_output_parameters reference parameter IDs, so edits that
generate new YAML-derived IDs must be patched to the preserved DB ID before
the workflow_definition JSON is serialized.

The atomic combined method is the only public surface; the staticmethod helper
``_reconcile_definition_parameters_in_session`` is a package-internal
implementation detail and is not exercised directly here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from skyvern.forge.sdk.db.agent_db import AgentDB
from skyvern.forge.sdk.db.models import Base
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.workflow.models.block import CodeBlock, ForLoopBlock
from skyvern.forge.sdk.workflow.models.parameter import (
    AWSSecretParameter,
    OutputParameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import WorkflowDefinition

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def agent_db(db_engine: Any) -> AsyncGenerator[AgentDB]:
    yield AgentDB(database_string="sqlite+aiosqlite:///:memory:", debug_enabled=True, db_engine=db_engine)


@pytest_asyncio.fixture
async def seeded_workflow(agent_db: AgentDB) -> dict[str, str]:
    org = await agent_db.organizations.create_organization(
        organization_name="Test Org",
        domain="reconcile.test",
    )
    workflow = await agent_db.workflows.create_workflow(
        title="Test Workflow",
        workflow_definition={"parameters": [], "blocks": []},
        organization_id=org.organization_id,
    )
    return {"organization_id": org.organization_id, "workflow_id": workflow.workflow_id}


def _wp(
    key: str,
    ptype: WorkflowParameterType = WorkflowParameterType.STRING,
    workflow_id: str = "",
    default_value: Any = None,
    description: str | None = None,
    param_id: str | None = None,
) -> WorkflowParameter:
    now = datetime.now(timezone.utc)
    return WorkflowParameter(
        workflow_parameter_id=param_id or f"wp_{key}_{ptype.value}",
        workflow_parameter_type=ptype,
        key=key,
        description=description,
        workflow_id=workflow_id,
        default_value=default_value,
        created_at=now,
        modified_at=now,
    )


def _op(
    key: str,
    workflow_id: str = "",
    description: str | None = None,
    param_id: str | None = None,
) -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        output_parameter_id=param_id or f"op_{key}",
        key=key,
        description=description,
        workflow_id=workflow_id,
        created_at=now,
        modified_at=now,
    )


async def _reconcile(
    agent_db: AgentDB,
    ids: dict[str, str],
    params: list[Any],
) -> None:
    """Drive reconcile through the only public entry point — the atomic
    combined method — with an empty-blocks ``WorkflowDefinition``.
    """
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=ids["workflow_id"],
        organization_id=ids["organization_id"],
        workflow_definition=WorkflowDefinition(parameters=params, blocks=[]),
    )


# --------------------------------------------------------------------------- #
# Reconcile helper — round trip                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reconcile_adds_new_workflow_parameter(agent_db: AgentDB, seeded_workflow: dict[str, str]) -> None:
    workflow_id = seeded_workflow["workflow_id"]
    desired = [_wp("ticker", workflow_id=workflow_id, default_value="AAPL")]

    await _reconcile(agent_db, seeded_workflow, desired)

    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    assert len(persisted) == 1
    assert persisted[0].key == "ticker"
    assert persisted[0].default_value == "AAPL"


@pytest.mark.asyncio
async def test_reconcile_in_place_preserves_id_and_updates_fields(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    workflow_id = seeded_workflow["workflow_id"]
    first = _wp("ticker", workflow_id=workflow_id, default_value="AAPL", description="old")
    await _reconcile(agent_db, seeded_workflow, [first])
    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    original_id = persisted[0].workflow_parameter_id

    # Second pass with a DIFFERENT generated ID and updated fields.
    second = _wp(
        "ticker",
        workflow_id=workflow_id,
        default_value="MSFT",
        description="new",
        param_id="wp_freshly_generated_different",
    )
    await _reconcile(agent_db, seeded_workflow, [second])

    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    assert len(persisted) == 1
    # ID stays stable across edits so FKs keep resolving.
    assert persisted[0].workflow_parameter_id == original_id
    assert persisted[0].default_value == "MSFT"
    assert persisted[0].description == "new"
    # And the incoming object was patched so its ID equals the DB row's ID —
    # critical for the caller to serialize the JSON with the canonical ID.
    assert second.workflow_parameter_id == original_id


@pytest.mark.asyncio
async def test_reconcile_removes_absent_parameter(agent_db: AgentDB, seeded_workflow: dict[str, str]) -> None:
    workflow_id = seeded_workflow["workflow_id"]
    initial = [
        _wp("ticker", workflow_id=workflow_id),
        _wp("product_sku", workflow_id=workflow_id),
    ]
    await _reconcile(agent_db, seeded_workflow, initial)

    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    product_sku_id = next(p.workflow_parameter_id for p in persisted if p.key == "product_sku")

    trimmed = [_wp("ticker", workflow_id=workflow_id)]
    await _reconcile(agent_db, seeded_workflow, trimmed)

    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    assert [p.key for p in persisted] == ["ticker"]

    # Historical by-ID resolution still returns the soft-deleted row — required
    # for historical workflow-run lookups to keep resolving.
    historical = await agent_db.workflow_params.get_workflow_parameter(product_sku_id)
    assert historical is not None
    assert historical.key == "product_sku"


@pytest.mark.asyncio
async def test_reconcile_type_change_soft_deletes_old_inserts_new(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    workflow_id = seeded_workflow["workflow_id"]
    await _reconcile(
        agent_db,
        seeded_workflow,
        [_wp("cfg", WorkflowParameterType.STRING, workflow_id=workflow_id, default_value="abc")],
    )
    string_id = (await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id))[
        0
    ].workflow_parameter_id

    await _reconcile(
        agent_db,
        seeded_workflow,
        [_wp("cfg", WorkflowParameterType.JSON, workflow_id=workflow_id, default_value={"a": 1})],
    )

    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    assert len(persisted) == 1
    assert persisted[0].key == "cfg"
    assert persisted[0].workflow_parameter_type == WorkflowParameterType.JSON
    assert persisted[0].workflow_parameter_id != string_id

    # Old STRING row soft-deleted; still resolvable by ID for historical joins.
    historical_string = await agent_db.workflow_params.get_workflow_parameter(string_id)
    assert historical_string is not None
    assert historical_string.workflow_parameter_type == WorkflowParameterType.STRING


@pytest.mark.asyncio
async def test_reconcile_revives_soft_deleted_row_with_same_id(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    workflow_id = seeded_workflow["workflow_id"]
    await _reconcile(
        agent_db,
        seeded_workflow,
        [_wp("ticker", workflow_id=workflow_id)],
    )
    original_id = (await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id))[
        0
    ].workflow_parameter_id

    await _reconcile(agent_db, seeded_workflow, [])
    assert await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id) == []

    revived = _wp("ticker", workflow_id=workflow_id, param_id="wp_new_id")
    await _reconcile(agent_db, seeded_workflow, [revived])

    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    assert len(persisted) == 1
    assert persisted[0].workflow_parameter_id == original_id
    assert revived.workflow_parameter_id == original_id


@pytest.mark.asyncio
async def test_reconcile_output_parameter_add_update_remove(agent_db: AgentDB, seeded_workflow: dict[str, str]) -> None:
    workflow_id = seeded_workflow["workflow_id"]

    await _reconcile(
        agent_db,
        seeded_workflow,
        [
            _op("nav_output", workflow_id=workflow_id, description="old"),
            _op("extract_output", workflow_id=workflow_id),
        ],
    )
    persisted = await agent_db.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id)
    keys = {p.key: p.output_parameter_id for p in persisted}
    assert set(keys) == {"nav_output", "extract_output"}
    nav_id = keys["nav_output"]

    updated_nav = _op("nav_output", workflow_id=workflow_id, description="new", param_id="op_regenerated")
    await _reconcile(agent_db, seeded_workflow, [updated_nav])

    persisted = await agent_db.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id)
    assert len(persisted) == 1
    assert persisted[0].key == "nav_output"
    assert persisted[0].output_parameter_id == nav_id
    assert persisted[0].description == "new"
    # Incoming pydantic object patched to the preserved DB ID.
    assert updated_nav.output_parameter_id == nav_id


@pytest.mark.asyncio
async def test_get_workflow_parameters_excludes_soft_deleted(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    workflow_id = seeded_workflow["workflow_id"]
    await _reconcile(
        agent_db,
        seeded_workflow,
        [_wp("ticker", workflow_id=workflow_id), _wp("product_sku", workflow_id=workflow_id)],
    )
    await _reconcile(
        agent_db,
        seeded_workflow,
        [_wp("ticker", workflow_id=workflow_id)],
    )
    persisted = await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id)
    assert [p.key for p in persisted] == ["ticker"]


# --------------------------------------------------------------------------- #
# Atomic combined operation                                                   #
# --------------------------------------------------------------------------- #


def _make_definition(params: list[Any]) -> WorkflowDefinition:
    return WorkflowDefinition(parameters=params, blocks=[])


@pytest.mark.asyncio
async def test_atomic_op_aligns_json_ids_with_preserved_db_ids(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """Regression: the workflow_definition JSON column must carry the preserved
    DB ID, not a freshly generated one from the incoming YAML-derived
    WorkflowDefinition.

    Runtime reads output_parameter_id off the workflow JSON
    (see script_service._update_workflow_output_parameter) then resolves DB
    rows by that same ID
    (service.get_output_parameter_workflow_run_output_parameter_tuples). If
    these drift, historical result joins break.
    """
    workflow_id = seeded_workflow["workflow_id"]
    organization_id = seeded_workflow["organization_id"]

    first_def = _make_definition(
        [
            _wp("ticker", workflow_id=workflow_id, default_value="AAPL", param_id="wp_v1"),
            _op("nav_output", workflow_id=workflow_id, param_id="op_v1"),
        ]
    )
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=first_def,
    )
    persisted_wp = (await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id))[0]
    persisted_op = (await agent_db.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id))[0]
    db_wp_id = persisted_wp.workflow_parameter_id
    db_op_id = persisted_op.output_parameter_id

    # Second update: edit only the default_value. The incoming pydantic objects
    # have freshly-generated IDs (mimics the YAML converter path).
    second_def = _make_definition(
        [
            _wp("ticker", workflow_id=workflow_id, default_value="MSFT", param_id="wp_new_random"),
            _op("nav_output", workflow_id=workflow_id, description="updated", param_id="op_new_random"),
        ]
    )
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=second_def,
    )

    persisted_wp = (await agent_db.workflow_params.get_workflow_parameters(workflow_id=workflow_id))[0]
    persisted_op = (await agent_db.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id))[0]
    assert persisted_wp.workflow_parameter_id == db_wp_id
    assert persisted_op.output_parameter_id == db_op_id
    assert persisted_wp.default_value == "MSFT"
    assert persisted_op.description == "updated"

    # The workflow_definition JSON now carries the preserved IDs, not the
    # freshly generated ones.
    workflow_row = await agent_db.workflows.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
    assert workflow_row is not None
    json_params = workflow_row.workflow_definition.parameters
    json_wp = next(p for p in json_params if isinstance(p, WorkflowParameter))
    json_op = next(p for p in json_params if isinstance(p, OutputParameter))
    assert json_wp.workflow_parameter_id == db_wp_id
    assert json_op.output_parameter_id == db_op_id


# --------------------------------------------------------------------------- #
# Helper method + encoding smoke                                              #
# --------------------------------------------------------------------------- #


def test_reconcile_helper_is_package_internal_only() -> None:
    """The in-session helpers stay available for composition by the atomic
    method; the old public ``reconcile_workflow_definition_parameters``
    wrapper is intentionally absent because it committed parameter rows
    without also persisting the re-serialized workflow_definition JSON.
    """
    mock_session = MagicMock()
    repo = WorkflowParametersRepository(session_factory=mock_session, debug_enabled=False)
    assert hasattr(repo, "_reconcile_definition_parameters_in_session")
    assert not hasattr(repo, "reconcile_workflow_definition_parameters")


def test_workflows_repo_exposes_atomic_combined_op() -> None:
    mock_session = MagicMock()
    repo = WorkflowsRepository(session_factory=mock_session, debug_enabled=False)
    assert hasattr(repo, "update_workflow_and_reconcile_definition_params")
    assert hasattr(repo, "update_workflow")


def test_encode_default_value_string() -> None:
    param = _wp("ticker", WorkflowParameterType.STRING, default_value="AAPL")
    assert WorkflowParametersRepository._encode_workflow_parameter_default(param) == "AAPL"


def test_encode_default_value_json_is_serialized() -> None:
    payload = {"a": 1, "b": [2, 3]}
    param = _wp("cfg", WorkflowParameterType.JSON, default_value=payload)
    encoded = WorkflowParametersRepository._encode_workflow_parameter_default(param)
    assert encoded is not None
    assert json.loads(encoded) == payload


def test_encode_default_value_none_passes_through() -> None:
    param = _wp("ticker", WorkflowParameterType.STRING, default_value=None)
    assert WorkflowParametersRepository._encode_workflow_parameter_default(param) is None


# --------------------------------------------------------------------------- #
# Regression coverage for the invariants the atomic combined method depends   #
# on: block-level output-parameter alignment and credential-row existence.    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_block_output_parameter_ids_align_after_definition_roundtrip(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """block.output_parameter.output_parameter_id in the stored JSON must
    match the reconciled DB row even when the incoming WorkflowDefinition
    has been round-tripped through model_validate(model_dump(...)) and
    the top-level parameter is a different instance from the one embedded
    in the block.
    """
    workflow_id = seeded_workflow["workflow_id"]
    organization_id = seeded_workflow["organization_id"]

    # First pass: seed DB rows for the output parameter via the atomic method.
    first_op = _op("step_one_output", workflow_id=workflow_id, param_id="op_first")
    first_block = CodeBlock(
        label="step_one",
        output_parameter=first_op,
        code="pass",
    )
    first_def = WorkflowDefinition(parameters=[first_op], blocks=[first_block])
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=first_def,
    )
    db_op_id = (await agent_db.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id))[
        0
    ].output_parameter_id

    # Build a second WorkflowDefinition with a FRESHLY-GENERATED output
    # parameter ID for the same key, then round-trip through
    # model_validate(model_dump(...)) so the block's nested output_parameter
    # is a distinct instance from the top-level parameter list.
    second_op = _op("step_one_output", workflow_id=workflow_id, param_id="op_regenerated_distinct")
    second_block = CodeBlock(
        label="step_one",
        output_parameter=second_op,
        code="pass",
    )
    raw = WorkflowDefinition(parameters=[second_op], blocks=[second_block])
    second_def = WorkflowDefinition.model_validate(raw.model_dump(mode="json"))

    # Sanity: after the round-trip, the top-level and block-level output
    # parameter objects are no longer the same Python instance.
    assert second_def.parameters[0] is not second_def.blocks[0].output_parameter

    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=second_def,
    )

    workflow_row = await agent_db.workflows.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
    assert workflow_row is not None
    stored_top_level = workflow_row.workflow_definition.parameters[0]
    stored_block = workflow_row.workflow_definition.blocks[0]
    assert isinstance(stored_top_level, OutputParameter)
    # Both the top-level parameter and the block's nested output_parameter
    # now carry the preserved DB ID — no drift.
    assert stored_top_level.output_parameter_id == db_op_id
    assert stored_block.output_parameter.output_parameter_id == db_op_id


@pytest.mark.asyncio
async def test_block_output_parameter_aligns_through_nested_for_loop(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """The block walker must recurse into ForLoopBlock.loop_blocks so nested
    blocks' output_parameter IDs stay aligned with the reconciled top-level
    parameters list.
    """
    workflow_id = seeded_workflow["workflow_id"]
    organization_id = seeded_workflow["organization_id"]

    outer_op = _op("outer_output", workflow_id=workflow_id, param_id="op_outer")
    inner_op = _op("inner_output", workflow_id=workflow_id, param_id="op_inner")
    inner_block = CodeBlock(label="inner", output_parameter=inner_op, code="pass")
    loop_block = ForLoopBlock(label="outer", output_parameter=outer_op, loop_blocks=[inner_block])
    first_def = WorkflowDefinition(parameters=[outer_op, inner_op], blocks=[loop_block])
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=first_def,
    )
    by_key = {
        op.key: op.output_parameter_id
        for op in await agent_db.workflow_params.get_workflow_output_parameters(workflow_id=workflow_id)
    }
    db_outer_id = by_key["outer_output"]
    db_inner_id = by_key["inner_output"]

    # Second pass: re-apply with freshly generated IDs, round-tripped to
    # break object identity across the nested block too.
    outer_op2 = _op("outer_output", workflow_id=workflow_id, param_id="op_outer_new")
    inner_op2 = _op("inner_output", workflow_id=workflow_id, param_id="op_inner_new")
    inner_block2 = CodeBlock(label="inner", output_parameter=inner_op2, code="pass")
    loop_block2 = ForLoopBlock(label="outer", output_parameter=outer_op2, loop_blocks=[inner_block2])
    raw = WorkflowDefinition(parameters=[outer_op2, inner_op2], blocks=[loop_block2])
    second_def = WorkflowDefinition.model_validate(raw.model_dump(mode="json"))

    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=second_def,
    )

    workflow_row = await agent_db.workflows.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
    assert workflow_row is not None
    stored_outer = workflow_row.workflow_definition.blocks[0]
    stored_inner = stored_outer.loop_blocks[0]
    assert stored_outer.output_parameter.output_parameter_id == db_outer_id
    assert stored_inner.output_parameter.output_parameter_id == db_inner_id


@pytest.mark.asyncio
async def test_atomic_write_accepts_credential_parameter_without_existing_row(
    agent_db: AgentDB, seeded_workflow: dict[str, str]
) -> None:
    """Credential-subclass parameters are intentionally out of scope for
    reconcile: the copilot's core flow is ``list_credentials`` -> reference a
    ``credential_id`` in a new ``CredentialParameter`` in the YAML ->
    ``update_workflow``, and runtime resolves that ``credential_id`` off the
    pydantic instance loaded from the JSON column (see
    ``WorkflowService._resolve_login_block_browser_profile_id``).  The atomic
    write must therefore persist the JSON as-is even when the credential
    param has no corresponding row in the credential side table.  The side
    tables are populated by the YAML create path via
    ``save_workflow_definition_parameters`` and used for workflow
    search/metadata; in-place edits may leave them stale relative to the
    JSON, which is accepted by the architecture.
    """
    workflow_id = seeded_workflow["workflow_id"]
    organization_id = seeded_workflow["organization_id"]

    now = datetime.now(timezone.utc)
    new_credential = AWSSecretParameter(
        aws_secret_parameter_id="aws_from_copilot",
        parameter_type=ParameterType.AWS_SECRET,
        key="aws_creds",
        description="referenced by the copilot after list_credentials",
        workflow_id=workflow_id,
        aws_key="AWS_KEY",
        created_at=now,
        modified_at=now,
    )
    wp = _wp("ticker", workflow_id=workflow_id, default_value="AAPL")

    # No pre-save of the credential row — this simulates the copilot's
    # in-place update flow, which does not round-trip through
    # save_workflow_definition_parameters.
    await agent_db.workflows.update_workflow_and_reconcile_definition_params(
        workflow_id=workflow_id,
        organization_id=organization_id,
        workflow_definition=WorkflowDefinition(parameters=[wp, new_credential], blocks=[]),
    )

    workflow_row = await agent_db.workflows.get_workflow(workflow_id=workflow_id, organization_id=organization_id)
    assert workflow_row is not None
    stored_params_by_key = {p.key: p for p in workflow_row.workflow_definition.parameters}
    assert set(stored_params_by_key) == {"ticker", "aws_creds"}
    # The credential parameter is serialized into the JSON with its
    # caller-supplied ID; runtime will resolve off this JSON instance.
    aws_in_json = stored_params_by_key["aws_creds"]
    assert aws_in_json.aws_secret_parameter_id == "aws_from_copilot"  # type: ignore[union-attr]
    assert aws_in_json.key == "aws_creds"
