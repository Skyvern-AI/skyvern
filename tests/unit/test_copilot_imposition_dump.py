from __future__ import annotations

import json
from types import SimpleNamespace

from dev_scripts.replay_imposition_decision import metadata_capture_is_structured
from skyvern.forge.sdk.copilot.tools import workflow_update
from skyvern.forge.sdk.copilot.tools.workflow_update import CodeArtifactMetadata


def test_imposition_dump_preserves_structured_code_artifact_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("COPILOT_DUMP_IMPOSITION_INPUTS", str(tmp_path))
    monkeypatch.setattr(workflow_update, "_prior_yaml_source", lambda _ctx: ("none", ""))
    monkeypatch.setattr(workflow_update, "_copilot_block_authoring_policy", lambda _ctx: "code_only_browser")
    monkeypatch.setattr(workflow_update, "synthesized_trajectory_reaches_goal", lambda _ctx: True)
    ctx = SimpleNamespace(
        scout_trajectory=[],
        flow_evidence=[],
        impose_synthesized_code_block=True,
        update_workflow_called=False,
        turn_origin="interactive",
        raw_code_artifact_metadata=[CodeArtifactMetadata(block_label="extract_count", declared_goal="Return count")],
        last_bound_requested_output_extraction_plan=None,
    )

    workflow_update._dump_imposition_decision(
        ctx,
        workflow_yaml="title: test\n",
        result=workflow_update._SynthesizedCodeImpositionResult(workflow_yaml="title: test\n"),
    )

    [dump_path] = tmp_path.glob("imposition-kept-*.json")
    payload = json.loads(dump_path.read_text())
    assert payload["schema_version"] == 3
    assert payload["raw_code_artifact_metadata"][0]["block_label"] == "extract_count"
    assert payload["raw_code_artifact_metadata"][0]["declared_goal"] == "Return count"
    assert metadata_capture_is_structured(payload["raw_code_artifact_metadata"])


def test_stringified_legacy_imposition_metadata_is_capture_limited() -> None:
    assert not metadata_capture_is_structured(["block_label='extract_count' declared_goal='Return count'"])
