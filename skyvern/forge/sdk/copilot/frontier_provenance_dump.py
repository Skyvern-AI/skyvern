"""Serializable capture of the frontier planner, edit invalidation, and the terminal-ready latch.

Enabled by ``COPILOT_DUMP_FRONTIER_PROVENANCE=<directory>``; see
``dev_scripts/replay_frontier_provenance.py`` for the replayer that consumes these packets.

Local development only. Packets carry unredacted workflow definitions and extracted block
outputs, so setting this anywhere real writes customer data to disk.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from skyvern.forge.sdk.copilot.runtime import AgentContext

LOG = structlog.get_logger()


def frontier_dump_root() -> Path | None:
    root = os.getenv("COPILOT_DUMP_FRONTIER_PROVENANCE")
    return Path(root).expanduser() if root else None


def trust_snapshot(ctx: AgentContext) -> dict[str, Any]:
    # Callers build this outside write_packet's guard, so it swallows its own failures: an
    # observability emit must never replace the result the planner decided.
    try:
        return _trust_snapshot(ctx)
    except Exception:
        LOG.debug("Frontier provenance trust snapshot failed", exc_info=True)
        return {"trust_snapshot_failed": True}


def _trust_snapshot(ctx: AgentContext) -> dict[str, Any]:
    evidence = ctx.workflow_verification_evidence
    return {
        "verified_prefix_labels": list(ctx.verified_prefix_labels or []),
        "composition_verified_labels": list(ctx.composition_verified_labels or []),
        "verified_block_outputs": _json_safe(ctx.verified_block_outputs),
        "verified_prefix_block_end_urls": dict(ctx.verified_prefix_block_end_urls or {}),
        "verified_prefix_block_end_session_id": ctx.verified_prefix_block_end_session_id,
        "verified_prefix_terminal_label": ctx.verified_prefix_terminal_label,
        "frontier_resume_session_id": ctx.frontier_resume_session_id,
        "frontier_start_provenance": ctx.frontier_start_provenance,
        "last_full_workflow_test_ok": ctx.last_full_workflow_test_ok,
        "last_requested_block_labels": list(ctx.last_requested_block_labels or []),
        "last_executed_block_labels": list(ctx.last_executed_block_labels or []),
        "last_frontier_start_label": ctx.last_frontier_start_label,
        "turn_origin": str(ctx.turn_origin),
        "block_verified": list(evidence.block_verified or []),
        "full_workflow_verified": evidence.full_workflow_verified,
    }


def definition_payload(definition: object | None) -> dict[str, Any] | None:
    if not isinstance(definition, BaseModel):
        return None
    try:
        return definition.model_dump(mode="json")
    except Exception:
        LOG.debug("Frontier provenance definition dump failed", exc_info=True)
        return {"definition_payload_failed": True}


def write_packet(kind: str, payload: dict[str, Any]) -> None:
    root = frontier_dump_root()
    if root is None:
        return
    try:
        directory = root / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps({"kind": kind, **payload}, indent=2, default=str))
    except Exception:
        LOG.debug("Frontier provenance dump failed", kind=kind, exc_info=True)


def _json_safe(value: dict[str, Any] | None) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(value or {}, default=str))
    except Exception:
        return {}
