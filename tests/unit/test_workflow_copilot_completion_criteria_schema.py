import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from skyvern.forge import app
from skyvern.forge.sdk.copilot.completion_criteria_store import StoredCriteriaSnapshot, criteria_from_json
from skyvern.forge.sdk.db.base_alchemy_db import BaseAlchemyDB
from skyvern.forge.sdk.db.models import WorkflowCopilotCompletionCriteriaSetModel
from skyvern.forge.sdk.db.repositories.workflow_parameters import WorkflowParametersRepository
from skyvern.forge.sdk.routes.workflow_copilot import _load_completion_criteria_snapshot
from skyvern.forge.sdk.schemas.workflow_copilot import (
    NonAdoptableCriteriaSet,
    WorkflowCopilotCompletionCriteriaSet,
)
from tests.unit.conftest import MockAsyncSessionCtx, make_mock_session

_V1_ROW_FIXTURE = Path(__file__).parent / "fixtures" / "workflow_copilot_completion_criteria_v1_row.json"
_V1_CRITERIA_PAYLOAD_MD5 = "8d79f38a1b6d136a0ecf7beaaa62f13f"


def _load_v1_row_data() -> dict[str, Any]:
    return json.loads(_V1_ROW_FIXTURE.read_text())


def _make_row(**overrides: Any) -> WorkflowCopilotCompletionCriteriaSetModel:
    data = _load_v1_row_data()
    data.update(overrides)
    return WorkflowCopilotCompletionCriteriaSetModel(**data)


async def _load_latest(
    row: WorkflowCopilotCompletionCriteriaSetModel,
) -> WorkflowCopilotCompletionCriteriaSet | NonAdoptableCriteriaSet | None:
    session = make_mock_session(row)
    repo = WorkflowParametersRepository(session_factory=lambda: MockAsyncSessionCtx(session))
    return await repo.get_latest_workflow_copilot_completion_criteria_set(
        organization_id=row.organization_id,
        workflow_copilot_chat_id=row.workflow_copilot_chat_id,
    )


def test_completion_criteria_set_keeps_list_storage_shape() -> None:
    now = datetime.now(UTC)
    criteria = [{"id": "c0", "outcome": "done", "pinability": "pinned"}]

    stored = WorkflowCopilotCompletionCriteriaSet.model_validate(
        {
            "completion_criteria_set_id": "wccs_1",
            "organization_id": "o_1",
            "workflow_copilot_chat_id": "wcc_1",
            "goal_epoch": 1,
            "status": "active",
            "criteria": criteria,
            "created_at": now,
            "modified_at": now,
        }
    )

    assert stored.criteria == criteria


def test_v1_fixture_criteria_payload_matches_custody_md5() -> None:
    data = _load_v1_row_data()
    payload_md5 = hashlib.md5(json.dumps(data["criteria"]).encode(), usedforsecurity=False).hexdigest()
    assert payload_md5 == _V1_CRITERIA_PAYLOAD_MD5


@pytest.mark.asyncio
async def test_loader_adopts_v1_envelope_row_preserving_recorded_criteria() -> None:
    data = _load_v1_row_data()
    loaded = await _load_latest(WorkflowCopilotCompletionCriteriaSetModel(**data))

    assert isinstance(loaded, WorkflowCopilotCompletionCriteriaSet)
    assert loaded.completion_criteria_set_id == data["completion_criteria_set_id"]
    assert loaded.goal_epoch == data["goal_epoch"]
    assert loaded.status == "active"
    assert loaded.criteria == data["criteria"]["criteria"]
    assert len(loaded.criteria) == 8
    assert [criterion["id"] for criterion in loaded.criteria] == [f"c{i}" for i in range(8)]
    assert loaded.source_turn_id == data["source_turn_id"]
    assert loaded.source_goal_text == data["source_goal_text"]
    assert loaded.source_goal_text is not None
    assert loaded.created_at is not None
    assert loaded.modified_at is not None


@pytest.mark.asyncio
async def test_loader_adopts_current_bare_list_row_unchanged() -> None:
    criteria = [{"id": "c0", "outcome": "done", "pinability": "pinned"}]
    loaded = await _load_latest(_make_row(criteria=criteria))

    assert isinstance(loaded, WorkflowCopilotCompletionCriteriaSet)
    assert loaded.criteria == criteria


@pytest.mark.asyncio
async def test_loader_adopts_current_bare_list_with_known_antecedent_family() -> None:
    criteria = [{"id": "c0", "outcome": "done", "antecedent_family": "blocker"}]

    loaded = await _load_latest(_make_row(criteria=criteria))

    assert isinstance(loaded, WorkflowCopilotCompletionCriteriaSet)
    assert loaded.criteria == criteria


@pytest.mark.asyncio
@pytest.mark.parametrize("enveloped", [False, True], ids=["bare_list", "v1_envelope"])
async def test_loader_adopts_coherent_floor_rekeyed_association(enveloped: bool) -> None:
    criteria = [
        {
            "id": "c0",
            "outcome": "done",
            "requested_output_floor_rekeyed": True,
            "floor_rekeyed_from_path": "output.blocker",
        }
    ]

    stored: object = {"contract_version": 1, "criteria": criteria} if enveloped else criteria
    loaded = await _load_latest(_make_row(criteria=stored))

    assert isinstance(loaded, WorkflowCopilotCompletionCriteriaSet)
    assert loaded.criteria == criteria


@pytest.mark.asyncio
@pytest.mark.parametrize("enveloped", [False, True], ids=["bare_list", "v1_envelope"])
@pytest.mark.parametrize(
    "stored_pair",
    [
        {"requested_output_floor_rekeyed": False},
        {"requested_output_floor_rekeyed": None},
        {"requested_output_floor_rekeyed": True},
        {"floor_rekeyed_from_path": None},
        {"floor_rekeyed_from_path": "output.blocker"},
        {"requested_output_floor_rekeyed": False, "floor_rekeyed_from_path": None},
        {"requested_output_floor_rekeyed": False, "floor_rekeyed_from_path": "output.blocker"},
        {"requested_output_floor_rekeyed": True, "floor_rekeyed_from_path": None},
        {"requested_output_floor_rekeyed": True, "floor_rekeyed_from_path": "blocker"},
    ],
)
async def test_loader_rejects_malformed_floor_rekeyed_association(
    enveloped: bool,
    stored_pair: dict[str, object],
) -> None:
    criteria = [{"id": "c0", "outcome": "done", **stored_pair}]

    stored: object = {"contract_version": 1, "criteria": criteria} if enveloped else criteria
    loaded = await _load_latest(_make_row(criteria=stored))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "undecodable_v1_criteria"


@pytest.mark.asyncio
async def test_loader_rejects_current_bare_list_with_unknown_antecedent_family() -> None:
    criteria = [{"id": "c0", "outcome": "done", "antecedent_family": "speculative_future_family"}]

    loaded = await _load_latest(_make_row(criteria=criteria))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "undecodable_v1_criteria"


@pytest.mark.asyncio
@pytest.mark.parametrize("enveloped", [False, True], ids=["bare_list", "v1_envelope"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("antecedent_family", None),
        ("requested_output_evidence_source", "future_evidence_source"),
        ("mint_disposition", "future_mint_disposition"),
        ("terminal_action_verification_mode", "future_verification_mode"),
    ],
)
async def test_loader_rejects_present_authority_field_that_decode_would_normalize(
    enveloped: bool,
    field: str,
    value: object,
) -> None:
    criterion: dict[str, object] = {
        "id": "c0",
        "outcome": "done",
        "kind": "terminal_action",
        field: value,
    }
    criteria: object = {"contract_version": 1, "criteria": [criterion]} if enveloped else [criterion]

    loaded = await _load_latest(_make_row(criteria=criteria))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "undecodable_v1_criteria"


@pytest.mark.asyncio
async def test_loader_rejects_malformed_neutral_boolean_authority_projection() -> None:
    request_slot_id = "a" * 64
    criteria = [
        {
            "id": request_slot_id,
            "outcome": "The run reports whether public form exists.",
            "antecedent_family": "unconditional",
            "expected_output_shape": "goal_judgment_boolean",
            "requested_output_evidence_source": "independent_run_evidence",
            "kind": "outcome",
            "classification_output_key": "public_form_exists",
            "request_slot_id": request_slot_id,
            "pinability": "shapeless_valid",
            "mint_disposition": "decidable",
            "requested_output_floor_rekeyed": True,
            "floor_rekeyed_from_path": "output.login_only",
        }
    ]

    loaded = await _load_latest(_make_row(criteria=criteria))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "undecodable_v1_criteria"


@pytest.mark.asyncio
async def test_loader_admits_exact_legacy_floor_marked_neutral_boolean_tuple() -> None:
    request_slot_id = "a" * 64
    criteria = [
        {
            "id": request_slot_id,
            "outcome": "The run reports whether public form exists.",
            "antecedent_family": "unconditional",
            "expected_output_shape": "goal_judgment_boolean",
            "requested_output_evidence_source": "independent_run_evidence",
            "kind": "outcome",
            "classification_output_key": "public_form_exists",
            "request_slot_id": request_slot_id,
            "pinability": "shapeless_valid",
            "mint_disposition": "decidable",
            "requested_output_floor_rekeyed": True,
            "floor_rekeyed_from_path": "output.public_form_exists",
        }
    ]

    loaded = await _load_latest(_make_row(criteria=criteria))

    assert not isinstance(loaded, NonAdoptableCriteriaSet)
    (criterion,) = criteria_from_json(loaded.criteria)
    assert criterion.requested_output_floor_rekeyed is False
    assert criterion.floor_rekeyed_from_path is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criteria_value",
    [
        {"contract_version": 2, "criteria": [{"id": "c0", "outcome": "done"}]},
        {"criteria": [{"id": "c0", "outcome": "done"}]},
        {"contract_version": 1, "criteria": "not-a-list"},
        {"contract_version": 1, "criteria": [{"id": "c1", "outcome": "done", "pinability": []}]},
        "not-json-criteria",
        7,
        [1, 2],
    ],
)
async def test_loader_returns_unknown_shape_disposition_without_raising(criteria_value: Any) -> None:
    loaded = await _load_latest(_make_row(criteria=criteria_value))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "unknown_shape"
    assert loaded.completion_criteria_set_id == "wccs_100000000000000001"
    assert loaded.goal_epoch == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inner",
    [
        [],
        [{}],
        [{"id": "c0", "outcome": "done"}, {"bogus": True}],
    ],
)
async def test_loader_rejects_v1_envelope_whose_criteria_do_not_fully_decode(inner: list[Any]) -> None:
    loaded = await _load_latest(_make_row(criteria={"contract_version": 1, "criteria": inner}))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "undecodable_v1_criteria"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "criterion",
    [
        {"id": "c0", "outcome": "done", "kind": "speculative_future_kind"},
        {"id": "c0", "outcome": "done", "level": "epic"},
        {"id": "c0", "outcome": "done", "antecedent_family": "speculative_future_family"},
    ],
)
async def test_loader_rejects_v1_envelope_whose_enum_value_was_coerced(criterion: dict[str, Any]) -> None:
    loaded = await _load_latest(_make_row(criteria={"contract_version": 1, "criteria": [criterion]}))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "undecodable_v1_criteria"


@pytest.mark.asyncio
async def test_loader_adopts_v1_envelope_with_known_antecedent_family() -> None:
    criterion = {"id": "c0", "outcome": "done", "antecedent_family": "blocker"}

    loaded = await _load_latest(_make_row(criteria={"contract_version": 1, "criteria": [criterion]}))

    assert isinstance(loaded, WorkflowCopilotCompletionCriteriaSet)
    assert loaded.criteria == [criterion]


@pytest.mark.asyncio
async def test_loader_returns_highest_epoch_row_over_lower_epoch_non_adoptable(sqlite_engine: AsyncEngine) -> None:
    db = BaseAlchemyDB(sqlite_engine)
    repo = WorkflowParametersRepository(db.Session)
    data = _load_v1_row_data()
    now = datetime.now(UTC)
    malformed = WorkflowCopilotCompletionCriteriaSetModel(
        completion_criteria_set_id="wccs_lower_epoch",
        organization_id=data["organization_id"],
        workflow_copilot_chat_id=data["workflow_copilot_chat_id"],
        goal_epoch=1,
        status="active",
        criteria={"contract_version": 99, "criteria": [{"id": "c0", "outcome": "done"}]},
        created_at=now,
        modified_at=now,
    )
    adopted = WorkflowCopilotCompletionCriteriaSetModel(
        completion_criteria_set_id="wccs_higher_epoch",
        organization_id=data["organization_id"],
        workflow_copilot_chat_id=data["workflow_copilot_chat_id"],
        goal_epoch=2,
        status="active",
        criteria=data["criteria"],
        source_turn_id=data["source_turn_id"],
        source_goal_text=data["source_goal_text"],
        created_at=now,
        modified_at=now,
    )
    async with db.Session() as session:
        session.add_all([malformed, adopted])
        await session.commit()

    loaded = await repo.get_latest_workflow_copilot_completion_criteria_set(
        organization_id=data["organization_id"],
        workflow_copilot_chat_id=data["workflow_copilot_chat_id"],
    )

    assert isinstance(loaded, WorkflowCopilotCompletionCriteriaSet)
    assert loaded.completion_criteria_set_id == "wccs_higher_epoch"
    assert loaded.goal_epoch == 2
    assert len(loaded.criteria) == 8


@pytest.mark.asyncio
async def test_loader_absorbs_v1_scaffold_validation_failure_as_non_adoptable() -> None:
    loaded = await _load_latest(_make_row(created_at=None))

    assert isinstance(loaded, NonAdoptableCriteriaSet)
    assert loaded.reason == "unknown_shape"


@pytest.mark.asyncio
async def test_snapshot_maps_non_adoptable_to_no_active_set_and_next_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposition = NonAdoptableCriteriaSet(
        reason="unknown_shape",
        completion_criteria_set_id="wccs_x",
        goal_epoch=3,
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(get_latest_workflow_copilot_completion_criteria_set=AsyncMock(return_value=disposition)),
    )
    chat = SimpleNamespace(organization_id="o_1", workflow_copilot_chat_id="wcc_1")

    snapshot = await _load_completion_criteria_snapshot(chat)

    assert snapshot == StoredCriteriaSnapshot(active=None, next_epoch=4)


@pytest.mark.asyncio
async def test_snapshot_absorbs_adopted_row_criteria_decode_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    adopted = WorkflowCopilotCompletionCriteriaSet.model_validate(
        {
            "completion_criteria_set_id": "wccs_2",
            "organization_id": "o_1",
            "workflow_copilot_chat_id": "wcc_1",
            "goal_epoch": 2,
            "status": "active",
            "criteria": [{"id": "c0", "outcome": "done", "pinability": []}],
            "created_at": now,
            "modified_at": now,
        }
    )
    monkeypatch.setattr(
        app.DATABASE,
        "workflow_params",
        SimpleNamespace(get_latest_workflow_copilot_completion_criteria_set=AsyncMock(return_value=adopted)),
    )
    chat = SimpleNamespace(organization_id="o_1", workflow_copilot_chat_id="wcc_1")

    snapshot = await _load_completion_criteria_snapshot(chat)

    assert snapshot == StoredCriteriaSnapshot(active=None, next_epoch=3)
