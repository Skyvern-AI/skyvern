from __future__ import annotations

import keyword
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlparse

import structlog

from skyvern.client.types.workflow_definition_yaml_blocks_item import (
    WorkflowDefinitionYamlBlocksItem_Action,
    WorkflowDefinitionYamlBlocksItem_Code,
    WorkflowDefinitionYamlBlocksItem_GotoUrl,
)
from skyvern.client.types.workflow_definition_yaml_parameters_item import WorkflowDefinitionYamlParametersItem_Workflow
from skyvern.forge.sdk.copilot.code_block_steps import derive_code_block_steps
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block, synthesize_goto_code_block
from skyvern.forge.sdk.workflow.exceptions import InsecureCodeDetected
from skyvern.forge.sdk.workflow.models.block import CodeBlock
from skyvern.services.browser_recording.v2.keyfold import Fact, fold
from skyvern.services.browser_recording.v2.session import RecordingSessionV2, StepV2

LOG = structlog.get_logger()

RenderMode = Literal["blocks", "code"]

_INTERACTIVE_KINDS = frozenset({"click", "type_text", "press_key"})
_LOCATOR_REQUIRED_KINDS = frozenset({"click", "type_text"})
# Below this the synthesizer's own wait_for_load_state after each click already covers the settle.
_WAIT_THRESHOLD_MS = 2000


@dataclass(slots=True)
class RenderResult:
    blocks: list[dict]
    parameters: list[dict]
    mode: RenderMode
    diagnostics: dict
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StepOverlay:
    kept_step_ids: frozenset[str]
    titles: dict[str, str]
    urls: dict[str, str]


def overlay_from_draft_steps(draft_steps: Sequence[Mapping[str, Any]] | None) -> StepOverlay | None:
    if draft_steps is None:
        return None

    kept: set[str] = set()
    titles: dict[str, str] = {}
    urls: dict[str, str] = {}
    for draft in draft_steps:
        step_id = str(draft.get("step_id") or "")
        if not step_id:
            continue
        kept.add(step_id)
        title = str(draft.get("title") or "").strip()
        if title:
            titles[step_id] = title
        url = str(draft.get("url") or "").strip()
        if url:
            urls[step_id] = url

    if draft_steps and not kept:
        # Drafts without step ids cannot be joined back to steps; keeping the raw
        # steps beats treating the whole recording as deleted.
        return None
    return StepOverlay(frozenset(kept), titles, urls)


def _overlaid_steps(session: RecordingSessionV2, overlay: StepOverlay | None) -> list[StepV2]:
    steps = session.steps
    if overlay is None:
        return steps

    kept: list[StepV2] = []
    for step in steps:
        if step.step_id not in overlay.kept_step_ids:
            continue
        title = overlay.titles.get(step.step_id)
        url = overlay.urls.get(step.step_id)
        if title or url:
            step = replace(step, title=title or step.title, url=url or step.url)
        kept.append(step)
    return kept


def _facts_by_step_id(session: RecordingSessionV2) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    for fact in fold(session.ledger.rows()):
        first_seq = fact.gesture_seqs[0] if fact.gesture_seqs else 0
        facts[f"{session.browser_session_id}:{first_seq}"] = fact
    return facts


def _slug(text: str) -> str:
    slug = re.sub(r"\W+", "_", text.strip().lower()).strip("_")[:60]
    if not slug or slug[0].isdigit():
        slug = f"field_{slug}".rstrip("_")
    if keyword.iskeyword(slug):
        slug = f"{slug}_field"
    return slug


def _host(url: str | None) -> str:
    return urlparse(url).netloc if url else ""


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _has_locator(fact: Fact | None) -> bool:
    return bool(fact and (fact.selector or (fact.role and fact.accessible_name)))


def _diagnostics(session: RecordingSessionV2, facts: Mapping[str, Fact], dropped: int, unlocatable: int) -> dict:
    return {
        "rows": len(session.ledger.rows()),
        "facts": len(facts),
        "dropped": dropped,
        "unlocatable": unlocatable,
    }


def _blank_default(parameter: Mapping[str, Any]) -> dict:
    return WorkflowDefinitionYamlParametersItem_Workflow(
        key=str(parameter["key"]),
        workflow_parameter_type="string",
        # Recorded values never persist as defaults: a secret typed into any field
        # must not land in a DB-stored, API-exposed default_value.
        default_value="",
        description="",
    ).dict()


def render_blocks(session: RecordingSessionV2, overlay: StepOverlay | None = None) -> RenderResult:
    facts = _facts_by_step_id(session)
    steps = _overlaid_steps(session, overlay)

    blocks: list[dict] = []
    parameters: list[dict] = []
    keys_by_identity: dict[tuple[str, str, str], str] = {}
    used_keys: set[str] = set()
    used_labels: set[str] = set()
    dropped = 0
    unlocatable = 0

    for step in steps:
        fact = facts.get(step.step_id)
        if step.kind == "goto_url":
            if not step.url:
                dropped += 1
                continue
            label = _unique(_slug(f"goto_{_host(step.url)}") or "goto_url", used_labels)
            blocks.append(WorkflowDefinitionYamlBlocksItem_GotoUrl(label=label, url=step.url).dict())
            continue

        if step.kind not in _INTERACTIVE_KINDS:
            dropped += 1
            continue

        if step.kind in _LOCATOR_REQUIRED_KINDS and not _has_locator(fact):
            unlocatable += 1

        parameter_key: str | None = None
        if step.kind == "type_text":
            identity = (
                (fact.selector or "") if fact else "",
                (fact.role or "") if fact else "",
                step.accessible_name or "",
            )
            parameter_key = keys_by_identity.get(identity)
            if parameter_key is None:
                base = step.accessible_name or (fact.tag if fact and fact.tag else "") or "field"
                parameter_key = _unique(_slug(base), used_keys)
                keys_by_identity[identity] = parameter_key
                parameters.append(_blank_default({"key": parameter_key}))

        navigation_goal = (
            f"{step.title} with the value of the {parameter_key} parameter" if parameter_key else step.title
        )
        blocks.append(
            WorkflowDefinitionYamlBlocksItem_Action(
                label=_unique(_slug(step.title) or "act", used_labels),
                title=step.title,
                navigation_goal=navigation_goal,
                error_code_mapping=None,
                # The editor's convertToNode reads block.parameters.map(p => p.key).
                parameters=[{"key": parameter_key}] if parameter_key else [],
                parameter_keys=[parameter_key] if parameter_key else [],
            ).dict()
        )

    return RenderResult(
        blocks=blocks,
        parameters=parameters,
        mode="blocks",
        diagnostics=_diagnostics(session, facts, dropped, unlocatable),
    )


def _interaction(step: StepV2, fact: Fact) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if fact.selector:
        base["selector"] = fact.selector
    if fact.role:
        base["role"] = fact.role
    if fact.accessible_name:
        base["accessible_name"] = fact.accessible_name

    if step.kind == "click":
        return {"tool_name": "click", **base}
    if step.kind == "press_key":
        return {"tool_name": "press_key", "key": fact.key}
    return {"tool_name": "type_text", **base, "typed_value": fact.typed_value, "typed_length": fact.typed_length}


def render_code(session: RecordingSessionV2, overlay: StepOverlay | None = None) -> RenderResult | None:
    facts = _facts_by_step_id(session)
    steps = _overlaid_steps(session, overlay)

    segments: list[tuple[str | None, list[dict[str, Any]]]] = [(None, [])]
    notes: list[str] = []
    dropped = 0
    shadow_hosts = 0

    for step in steps:
        if step.kind == "goto_url":
            if not step.url:
                dropped += 1
                continue
            # Every navigation gets its own segment so code renders one page.goto per
            # navigation, exactly as render_blocks emits one goto_url block per navigation.
            if segments[-1] == (None, []):
                segments[-1] = (step.url, [])
            else:
                segments.append((step.url, []))
            continue

        if step.kind not in _INTERACTIVE_KINDS:
            dropped += 1
            continue

        fact = facts.get(step.step_id)
        if fact is None or (step.kind in _LOCATOR_REQUIRED_KINDS and not _has_locator(fact)):
            LOG.info(
                "Record Browser v2 code render found an unlocatable interaction",
                browser_session_id=session.browser_session_id,
                step_kind=step.kind,
            )
            return None

        if fact.shadow_path:
            shadow_hosts += 1

        segments[-1][1].append(_interaction(step, fact))
        if step.settle_ms is not None and step.settle_ms > _WAIT_THRESHOLD_MS:
            segments[-1][1].append({"tool_name": "wait", "duration_ms": step.settle_ms})

    if shadow_hosts:
        notes.append(f"{shadow_hosts} interaction(s) inside a closed shadow root use their host element's selector")

    if segments[0][0] is None and segments[0][1]:
        first_url = next((step.url for step in steps if step.url), None)
        segments[0] = (first_url, segments[0][1])

    blocks: list[dict] = []
    parameter_keys_seen: set[str] = set()
    parameters: list[dict] = []
    used_labels: set[str] = set()

    for source_url, trajectory in segments:
        if trajectory and source_url:
            trajectory[0] = {**trajectory[0], "source_url": source_url}
        if trajectory:
            synthesized = synthesize_code_block(trajectory, strict_selectors=False)
        elif source_url:
            synthesized = synthesize_goto_code_block(source_url)
        else:
            continue
        if synthesized is None:
            continue

        code = textwrap.dedent(synthesized.code)
        try:
            CodeBlock.is_safe_code(code)
        # ast.parse raises ValueError on e.g. null bytes; any failure inside the safety
        # gate must fall back to blocks, never surface as an error to the operator.
        except (SyntaxError, ValueError, InsecureCodeDetected):
            LOG.warning(
                "Record Browser v2 code render rejected by the code safety gate",
                browser_session_id=session.browser_session_id,
                exc_info=True,
            )
            return None

        notes.extend(synthesized.notes)
        block_parameter_keys: list[str] = []
        for parameter in synthesized.parameters:
            key = str(parameter.get("key") or "").strip()
            if not key or parameter.get("credential_id"):
                continue
            block_parameter_keys.append(key)
            if key not in parameter_keys_seen:
                parameter_keys_seen.add(key)
                parameters.append(_blank_default({"key": key}))

        blocks.append(
            WorkflowDefinitionYamlBlocksItem_Code(
                label=_unique(_slug(f"recorded_{_host(source_url)}") or "recorded_steps", used_labels),
                code=code,
                parameter_keys=block_parameter_keys or None,
                parameters=[{"key": key} for key in block_parameter_keys],
                # A non-null prompt is what makes the editor render the code-first node; ""
                # leaves the Goal for the user, because a fabricated one would arm self-heal.
                prompt="",
                steps=derive_code_block_steps(code) or None,
            ).dict()
        )

    if not blocks:
        return None

    return RenderResult(
        blocks=blocks,
        parameters=parameters,
        mode="code",
        diagnostics=_diagnostics(session, facts, dropped, 0),
        notes=notes,
    )
