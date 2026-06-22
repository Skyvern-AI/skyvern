from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from skyvern.forge.sdk.copilot.composition_evidence import has_bounded_page_schema
from skyvern.forge.sdk.copilot.runtime import AgentContext

from ._shared import (
    _DISCOVERY_ANTI_BOT_PATTERNS,
    _append_flow_evidence,
    _composition_evidence_page_url,
    _fallback_page_info,
    _same_page_ignoring_fragment,
    _workflow_verification_evidence,
)
from .scouting import _consume_pending_browser_interaction_observation, _mark_post_run_page_observed

_EVALUATE_EVIDENCE_CONFIDENCE_WITH_SCHEMA = 0.75
_EVALUATE_EVIDENCE_CONFIDENCE_ANTIBOT_ONLY = 0.4


async def _resolve_url_title(raw: dict[str, Any], ctx: AgentContext) -> tuple[str, str]:
    """Extract URL and title from raw MCP result, falling back to live page info."""
    browser_ctx = raw.get("browser_context", {})
    url = browser_ctx.get("url", "")
    title = browser_ctx.get("title", "")
    if not url:
        url, fallback_title = await _fallback_page_info(ctx)
        if fallback_title:
            title = fallback_title
    return url, title


def _bounded_observation_text(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _nested_observation_text(value: object, *, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(value, str):
        return _bounded_observation_text(value, 500)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in list(value.items())[:40]:
            parts.append(_bounded_observation_text(key, 120))
            parts.append(_nested_observation_text(item, depth=depth + 1))
        return _bounded_observation_text(" ".join(part for part in parts if part), 2500)
    if isinstance(value, list):
        return _bounded_observation_text(
            " ".join(part for item in value[:40] if (part := _nested_observation_text(item, depth=depth + 1))),
            2500,
        )
    return ""


@dataclass(frozen=True)
class _ObservedFieldEvidence:
    name: str
    id: str
    label: str
    type: str
    placeholder: str
    required: bool
    selector: str

    @classmethod
    def from_raw(cls, raw_field: dict[str, Any]) -> _ObservedFieldEvidence:
        label = raw_field.get("label")
        if not label and isinstance(raw_field.get("labels"), list):
            label = " ".join(str(item) for item in raw_field["labels"][:2])
        return cls(
            name=_bounded_observation_text(raw_field.get("name"), 120),
            id=_bounded_observation_text(raw_field.get("id"), 120),
            label=_bounded_observation_text(label, 240),
            type=_bounded_observation_text(raw_field.get("type"), 40),
            placeholder=_bounded_observation_text(raw_field.get("placeholder"), 240),
            required=bool(raw_field.get("required")),
            selector=_bounded_observation_text(raw_field.get("selector"), 160),
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "placeholder": self.placeholder,
            "required": self.required,
            "selector": self.selector,
        }


def _normalize_observed_fields(raw_fields: object) -> list[dict[str, Any]]:
    if not isinstance(raw_fields, list):
        return []
    fields: list[dict[str, Any]] = []
    for raw_field in raw_fields[:20]:
        if not isinstance(raw_field, dict):
            continue
        fields.append(_ObservedFieldEvidence.from_raw(raw_field).as_evidence())
    return fields


def _normalize_observed_forms(observed_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_forms = observed_data.get("forms")
    forms: list[dict[str, Any]] = []
    if isinstance(raw_forms, list):
        for raw_form in raw_forms[:5]:
            if not isinstance(raw_form, dict):
                continue
            fields = _normalize_observed_fields(raw_form.get("fields") or raw_form.get("inputs"))
            submit_controls = _normalize_observed_fields(
                raw_form.get("submit_controls") or raw_form.get("buttons") or raw_form.get("submits")
            )
            forms.append(
                {
                    "id": _bounded_observation_text(raw_form.get("id"), 120),
                    "name": _bounded_observation_text(raw_form.get("name"), 120),
                    "action": _bounded_observation_text(raw_form.get("action"), 240),
                    "method": _bounded_observation_text(raw_form.get("method"), 20),
                    "fields": fields,
                    "submit_controls": submit_controls[:10],
                }
            )
    if forms:
        return forms
    fields = _normalize_observed_fields(observed_data.get("inputs") or observed_data.get("fields"))
    if not fields:
        return []
    return [{"id": "", "name": "", "action": "", "method": "", "fields": fields, "submit_controls": []}]


def _evaluate_observed_payload(observed_data: object) -> dict[str, Any]:
    if not isinstance(observed_data, dict):
        return {}
    raw_result = observed_data.get("result")
    if isinstance(raw_result, dict):
        payload: dict[str, Any] = dict(raw_result)
    elif isinstance(raw_result, list):
        payload = {"rows": raw_result}
    else:
        payload = {}
    for key, value in observed_data.items():
        if key == "result":
            continue
        if key not in payload or payload.get(key) in (None, "", [], {}):
            payload[key] = value
    return payload or observed_data


def _observed_row_text(row: object) -> str:
    if isinstance(row, str):
        return _bounded_observation_text(row, 500)
    if not isinstance(row, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "label", "name", "title"):
        value = row.get(key)
        if isinstance(value, str):
            parts.append(value)
    cells = row.get("cells")
    if isinstance(cells, list):
        for cell in cells[:12]:
            if isinstance(cell, dict):
                for key in ("text", "value"):
                    value = cell.get(key)
                    if isinstance(value, str):
                        parts.append(value)
            elif isinstance(cell, str):
                parts.append(cell)
    return _bounded_observation_text(" ".join(parts), 500)


def _normalize_observed_result_containers(observed_data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = observed_data.get("result_containers") or observed_data.get("tables")
    containers: list[dict[str, Any]] = []
    if isinstance(raw_results, list):
        for item in raw_results[:8]:
            if isinstance(item, dict):
                containers.append(
                    {
                        "tag": _bounded_observation_text(item.get("tag") or item.get("type"), 40),
                        "id": _bounded_observation_text(item.get("id"), 120),
                        "selector": _bounded_observation_text(item.get("selector"), 160),
                    }
                )
    if containers:
        return containers
    raw_rows = observed_data.get("rows")
    if isinstance(raw_rows, list) and raw_rows:
        sample_rows = [text for row in raw_rows[:5] if (text := _observed_row_text(row))]
        return [
            {
                "tag": "table",
                "id": _bounded_observation_text(observed_data.get("id"), 120),
                "selector": _bounded_observation_text(observed_data.get("selector"), 160),
                "row_count": len(raw_rows),
                "sample_rows": sample_rows,
            }
        ]
    table = observed_data.get("table")
    if table:
        return [{"tag": "table", "id": "", "selector": ""}]
    return []


def _normalize_evaluate_challenge_state(
    observed_data: dict[str, Any],
    anti_bot_indicators: list[str],
) -> dict[str, Any]:
    detected = bool(anti_bot_indicators)
    gated_controls = _disabled_submit_controls(observed_data)
    for key, value in observed_data.items():
        normalized_key = str(key).strip().lower().replace("_", " ").replace("-", " ")
        if "disabled" not in normalized_key or value is not True:
            continue
        if any(token in normalized_key for token in ("submit", "search", "button", "btn")):
            gated_controls.append(
                {
                    "text": _bounded_observation_text(str(key), 120),
                    "id": "",
                    "name": _bounded_observation_text(str(key), 120),
                    "selector": "",
                    "disabled": True,
                }
            )
    indicator_text = " ".join(anti_bot_indicators).lower()
    if "access denied" in indicator_text:
        kind = "access_denied"
    elif "turnstile" in indicator_text or "captcha" in indicator_text or "are you a robot" in indicator_text:
        kind = "captcha"
    elif detected:
        kind = "human_verification"
    else:
        kind = "none"
    gates_submit_controls = bool(detected and gated_controls)
    return {
        "detected": detected,
        "kind": kind,
        "source": "mcp_evaluate" if detected else "",
        "indicators": anti_bot_indicators[:8],
        "requires_human_verification": gates_submit_controls,
        "visual_location": "",
        "gates_submit_controls": gates_submit_controls,
        "gated_submit_controls": gated_controls[:5] if detected else [],
    }


def _disabled_submit_controls(observed_data: object) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []

    def visit(value: object, depth: int = 0) -> None:
        if depth > 4 or len(controls) >= 5:
            return
        if isinstance(value, list):
            for item in value[:40]:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return

        disabled = value.get("disabled") is True or value.get("ariaDisabled") is True
        text_parts: list[str] = []
        for key in (
            "text",
            "label",
            "name",
            "id",
            "selector",
            "value",
            "aria_label",
            "aria-label",
            "title",
            "type",
            "role",
        ):
            item = value.get(key)
            if isinstance(item, str):
                text_parts.append(item)
        label = _bounded_observation_text(" ".join(text_parts), 160)
        normalized = label.lower().replace("_", " ").replace("-", " ")
        if disabled and any(token in normalized for token in ("submit", "search", "button", "btn")):
            controls.append(
                {
                    "text": label,
                    "id": _bounded_observation_text(value.get("id"), 120),
                    "name": _bounded_observation_text(value.get("name"), 120),
                    "selector": _bounded_observation_text(value.get("selector"), 160),
                    "disabled": True,
                }
            )

        for item in value.values():
            visit(item, depth + 1)

    visit(observed_data)
    return controls[:5]


def _evaluate_anti_bot_indicators(observed_data: dict[str, Any], text: str) -> list[str]:
    key_text = " ".join(str(key) for key in observed_data.keys()).lower()
    nested_text = _nested_observation_text(observed_data).lower()
    combined = f"{text} {key_text} {nested_text}"[:5000]
    return [pattern for pattern in _DISCOVERY_ANTI_BOT_PATTERNS if pattern in combined]


def _normalize_evaluate_page_schema(observed_data: object) -> dict[str, Any]:
    observed_data = _evaluate_observed_payload(observed_data)
    if not observed_data:
        return {}
    text_parts = [
        observed_data.get("body"),
        observed_data.get("bodyText"),
        observed_data.get("text"),
        observed_data.get("html"),
    ]
    text = " ".join(part for part in text_parts if isinstance(part, str)).lower()[:4096]
    anti_bot = _evaluate_anti_bot_indicators(observed_data, text)
    forms = _normalize_observed_forms(observed_data)
    result_containers = _normalize_observed_result_containers(observed_data)
    if not forms and not result_containers and not anti_bot:
        return {}
    return {
        "forms": forms,
        "result_containers": result_containers,
        "anti_bot_indicators": anti_bot,
        "challenge_state": _normalize_evaluate_challenge_state(observed_data, anti_bot),
        "evidence_sources": ["mcp_evaluate"],
        "evidence_confidence": (
            _EVALUATE_EVIDENCE_CONFIDENCE_WITH_SCHEMA
            if forms or result_containers
            else _EVALUATE_EVIDENCE_CONFIDENCE_ANTIBOT_ONLY
        ),
    }


def _record_composition_page_observation(
    ctx: AgentContext,
    *,
    source_tool: str,
    url: str,
    title: str = "",
    observed_data: object | None = None,
    append_to_flow: bool = False,
    reached_via: str = "current_page",
) -> int | None:
    if not url:
        return None
    _mark_post_run_page_observed(ctx, source_tool=source_tool, url=url)
    if title:
        _workflow_verification_evidence(ctx).page_title = title[:160]
    existing = ctx.composition_page_evidence

    evidence: dict[str, Any] = {
        "inspected_url": url,
        "current_url": url,
        "page_title": title[:240],
        "forms": [],
        "navigation_targets": [],
        "result_containers": [],
        "anti_bot_indicators": [],
        "evidence_confidence": 0.0,
        "source_tool": source_tool,
        "observed_after_workflow_run": False,
    }
    if isinstance(observed_data, dict):
        observed_title = observed_data.get("title")
        if isinstance(observed_title, str) and observed_title and not evidence["page_title"]:
            evidence["page_title"] = observed_title[:240]
    if source_tool == "get_browser_screenshot":
        evidence.update(
            {
                "evidence_sources": ["screenshot"],
                "screenshot_used": True,
            }
        )
    elif source_tool == "evaluate":
        evidence.update(_normalize_evaluate_page_schema(observed_data))
    run_id = ctx.last_run_blocks_workflow_run_id
    if isinstance(run_id, str) and run_id:
        evidence["workflow_run_id"] = run_id
        evidence["observed_after_workflow_run"] = True

    observation_step: int | None = None
    if append_to_flow and has_bounded_page_schema(evidence):
        actual_reached_via = reached_via
        if reached_via == "auto":
            if isinstance(run_id, str) and run_id:
                actual_reached_via = "post_run"
            elif _consume_pending_browser_interaction_observation(ctx, current_url=url, evidence=evidence):
                actual_reached_via = "interaction"
            else:
                actual_reached_via = "current_page"
        observation_step = _append_flow_evidence(ctx, evidence, reached_via=actual_reached_via)

    if not _should_keep_existing_composition_page_evidence(existing, evidence):
        ctx.composition_page_evidence = evidence
    return observation_step


def _should_keep_existing_composition_page_evidence(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> bool:
    if not isinstance(existing, dict) or existing.get("source_tool") != "inspect_page_for_composition":
        return False
    if incoming.get("source_tool") != "evaluate":
        return True
    if not has_bounded_page_schema(incoming):
        return True
    if not has_bounded_page_schema(existing):
        return False
    existing_url = _composition_evidence_page_url(existing)
    incoming_url = _composition_evidence_page_url(incoming)
    if existing_url and incoming_url and not _same_page_ignoring_fragment(existing_url, incoming_url):
        return False
    return True
