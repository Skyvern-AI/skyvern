"""Shared OTP group binding and scoped retry browser operations."""

import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Iterator, Literal, TypedDict

import structlog
from playwright.async_api import Locator, Page

from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import MultiFieldTotpAttempt
from skyvern.utils.contained_effects import contained_effect
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from skyvern.webeye.scraper.scraper import structural_identity
from skyvern.webeye.utils.document import get_main_document_loader_id
from skyvern.webeye.utils.dom import resolve_locator
from skyvern.webeye.utils.page import SkyvernFrame

LOG = structlog.get_logger()


class MultiFieldTotpLogContext(TypedDict):
    task_id: str | None
    workflow_run_id: str | None
    step_id: str | None


class MultiFieldTotpBindingFailure(StrEnum):
    TEARDOWN = "teardown"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class MultiFieldTotpGroup:
    box_element_ids: list[str]
    container: dict[str, Any]
    frame: str
    ancestor_chain: list[dict[str, Any]]


def _multi_field_totp_node_frame(scraped_page: ScrapedPage, node: dict[str, Any]) -> str:
    node_id = node.get("id")
    frame = node.get("frame") or (
        scraped_page.id_to_frame_dict.get(node_id, "main.frame") if isinstance(node_id, str) else "main.frame"
    )
    if not isinstance(frame, str):
        raise TypeError("Malformed element frame")
    return frame


def _multi_field_totp_frame_chain(
    scraped_page: ScrapedPage, path: list[dict[str, Any]], frame: str
) -> list[dict[str, Any]]:
    return [node for node in path if _multi_field_totp_node_frame(scraped_page, node) == frame]


def _multi_field_totp_input_eligible(attributes: dict[str, Any]) -> bool:
    input_type = attributes.get("type", "text")
    if not isinstance(input_type, str) or input_type.lower() not in {"text", "number", "tel", "password"}:
        return False
    maxlength = attributes.get("maxlength")
    if maxlength is not None and not isinstance(maxlength, (str, int)):
        raise ValueError("Malformed input length")
    maxlength_value = int(maxlength) if maxlength is not None and str(maxlength).strip() else None
    pattern = attributes.get("pattern", "")
    inputmode = attributes.get("inputmode", "")
    if not isinstance(pattern, str) or not isinstance(inputmode, str):
        raise ValueError("Malformed input constraint")
    has_digit_constraint = bool(
        inputmode.lower() in {"numeric", "decimal"}
        or "digit" in pattern.lower()
        or "\\d" in pattern
        or "[0-9]" in pattern
        or pattern.isdigit()
    )
    return maxlength_value == 1 or (maxlength_value is None and has_digit_constraint)


def multi_field_totp_candidate_input_count(scraped_page: ScrapedPage) -> int:
    eligible_ids: set[str] = set()
    for element in scraped_page.elements:
        if str(element.get("tagName", "")).lower() != "input" or not isinstance(element.get("attributes"), dict):
            continue
        try:
            if _multi_field_totp_input_eligible(element["attributes"]) and isinstance(element.get("id"), str):
                eligible_ids.add(element["id"])
        except (TypeError, ValueError):
            continue
    return len(eligible_ids)


def _multi_field_totp_box_groups(
    scraped_page: ScrapedPage | None, expected_digits: int | None
) -> list[MultiFieldTotpGroup] | None:
    """Return container-scoped groups, optionally restricted to a width, or None for malformed data."""
    if scraped_page is None or (
        expected_digits is not None
        and (not isinstance(expected_digits, int) or isinstance(expected_digits, bool) or expected_digits <= 0)
    ):
        return None

    try:
        eligible: dict[str, str] = {}
        ordered_ids: list[str] = []
        flat_ids: set[str] = set()
        for element in scraped_page.elements:
            element_id = element.get("id")
            if element_id is not None:
                if not isinstance(element_id, str) or not element_id or element_id in flat_ids:
                    return None
                flat_ids.add(element_id)
            tag_name = element.get("tagName")
            if not isinstance(tag_name, str):
                return None
            if tag_name.lower() != "input":
                continue
            attributes = element.get("attributes")
            frame = element.get("frame")
            if frame is None:
                frame = getattr(scraped_page, "id_to_frame_dict", {}).get(element_id, "main.frame")
            if not isinstance(element_id, str) or not element_id or not isinstance(attributes, dict):
                return None
            if not isinstance(frame, str) or not frame:
                return None

            if not _multi_field_totp_input_eligible(attributes):
                continue
            if element_id in eligible:
                LOG.warning(
                    "Duplicate eligible multi-field TOTP element id; refusing to infer a group",
                    element_id=element_id,
                )
                return None
            eligible[element_id] = frame
            ordered_ids.append(element_id)

        paths: dict[str, list[dict[str, Any]]] = {}
        tree_ids: set[str] = set()

        def visit(nodes: list[dict[str, Any]], ancestors: list[dict[str, Any]]) -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    raise TypeError("Malformed element tree node")
                node_id = node.get("id")
                if node_id is not None:
                    if not isinstance(node_id, str) or not node_id or node_id in tree_ids:
                        raise ValueError("Duplicate element tree id")
                    tree_ids.add(node_id)
                next_ancestors = [*ancestors, node]
                if isinstance(node_id, str) and node_id in eligible:
                    paths[node_id] = next_ancestors
                children = node.get("children", [])
                if not isinstance(children, list):
                    raise TypeError("Malformed element tree children")
                visit(children, next_ancestors)

        visit(scraped_page.element_tree, [])
        if set(paths) != set(eligible):
            return None

        if not eligible:
            return []

        form_groups: dict[tuple[str, int], tuple[dict[str, Any], list[str]]] = {}
        unscoped_by_frame: dict[str, list[str]] = {}
        for element_id in ordered_ids:
            frame = eligible[element_id]
            path = paths[element_id]
            forms = [
                node
                for node in path[:-1]
                if isinstance(node.get("tagName"), str)
                and node["tagName"].lower() == "form"
                and (
                    node.get("frame") or getattr(scraped_page, "id_to_frame_dict", {}).get(node.get("id"), "main.frame")
                )
                == frame
            ]
            if forms:
                form_groups.setdefault((frame, id(forms[-1])), (forms[-1], []))[1].append(element_id)
            else:
                unscoped_by_frame.setdefault(frame, []).append(element_id)

        def make_group(container: dict[str, Any], ids: list[str], frame: str) -> MultiFieldTotpGroup:
            path = paths[ids[0]]
            container_index = next(index for index, node in enumerate(path) if node is container)
            chain = _multi_field_totp_frame_chain(scraped_page, path[: container_index + 1], frame)
            if not chain or chain[-1] is not container:
                raise ValueError("Missing owning-container ancestry")
            return MultiFieldTotpGroup(ids, container, frame, chain)

        candidates = [
            make_group(container, ids, frame)
            for (frame, _), (container, ids) in form_groups.items()
            if (len(ids) == expected_digits if expected_digits is not None else len(ids) >= 2)
        ]
        no_form_candidates: list[tuple[dict[str, Any], list[str]]] = []
        for frame, ids in unscoped_by_frame.items():
            seen_containers: set[int] = set()
            for element_id in ids:
                for container in paths[element_id][:-1]:
                    container_identity = id(container)
                    if container_identity in seen_containers:
                        continue
                    seen_containers.add(container_identity)
                    container_tag = container.get("tagName")
                    container_frame = container.get("frame")
                    if container_frame is None:
                        container_frame = getattr(scraped_page, "id_to_frame_dict", {}).get(
                            container.get("id"), "main.frame"
                        )
                    if not isinstance(container_tag, str) or not isinstance(container_frame, str):
                        return None
                    if container_tag.lower() in {"#document", "body", "document", "html"}:
                        continue
                    if container_frame != frame:
                        continue
                    descendant_ids = [
                        candidate_id
                        for candidate_id in ids
                        if any(node is container for node in paths[candidate_id][:-1])
                    ]
                    if (
                        len(descendant_ids) == expected_digits
                        if expected_digits is not None
                        else len(descendant_ids) >= 2
                    ):
                        no_form_candidates.append((container, descendant_ids))

        def is_descendant_container(
            ancestor: dict[str, Any], descendant: dict[str, Any], path: list[dict[str, Any]]
        ) -> bool:
            ancestor_index = next((index for index, node in enumerate(path) if node is ancestor), None)
            descendant_index = next((index for index, node in enumerate(path) if node is descendant), None)
            return ancestor_index is not None and descendant_index is not None and descendant_index > ancestor_index

        lowest_no_form_candidates: list[tuple[dict[str, Any], list[str]]] = []
        for container, ids in no_form_candidates:
            has_lower_candidate = any(
                container is not other_container
                and len(ids) == len(other_ids)
                and set(ids) == set(other_ids)
                and is_descendant_container(container, other_container, paths[other_ids[0]][:-1])
                for other_container, other_ids in no_form_candidates
            )
            if not has_lower_candidate:
                lowest_no_form_candidates.append((container, ids))
        candidates.extend(make_group(container, ids, eligible[ids[0]]) for container, ids in lowest_no_form_candidates)
        return candidates
    except Exception:  # noqa: BLE001 - malformed page data must fail closed
        return None


def _multi_field_totp_box_group(scraped_page: ScrapedPage | None, expected_digits: int | None) -> list[str] | None:
    """Return the only frame/container-scoped OTP group, optionally restricted to a width."""
    groups = _multi_field_totp_box_groups(scraped_page, expected_digits)
    return groups[0].box_element_ids if groups is not None and len(groups) == 1 else None


def _multi_field_totp_structural_identity(element: dict) -> str:
    def without_values(node: dict) -> dict:
        copied = dict(node)
        if "attributes" in node:
            copied["attributes"] = {
                key: value
                for key, value in node["attributes"].items()
                if key not in {"value", SKYVERN_ID_ATTR, "aria-invalid", "aria-busy", "disabled"}
                and not key.startswith("data-skyvern-")
            }
            placeholder = copied["attributes"].get("placeholder")
            if isinstance(placeholder, str) and re.fullmatch(r"[0-9]+|\*+", placeholder):
                copied["attributes"]["placeholder"] = "<otp-digit>"
            # Playwright hides carets inline during screenshots; concurrent scrapes can capture that declaration.
            if isinstance(style := copied["attributes"].get("style"), str):
                style = re.sub(
                    r"(?:^|;)\s*caret-color\s*:\s*transparent\s*!important\s*(?=;|$)",
                    "",
                    style,
                    flags=re.IGNORECASE,
                ).strip(" ;\t\r\n")
                if style:
                    copied["attributes"]["style"] = style
                else:
                    copied["attributes"].pop("style", None)
        if "children" in node:
            copied["children"] = [without_values(child) for child in node["children"] if isinstance(child, dict)]
        return copied

    # Filling changes values and Skyvern attributes; remounts restore values but discard injected attributes.
    return structural_identity(without_values(element))


def _multi_field_totp_scope_identity(ancestor_chain: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        _multi_field_totp_structural_identity({"tagName": node["tagName"], "attributes": node.get("attributes", {})})
        for node in ancestor_chain
    )


def _multi_field_totp_container_identity(group: MultiFieldTotpGroup) -> tuple[str, ...]:
    return _multi_field_totp_scope_identity(group.ancestor_chain)


def multi_field_totp_group_identity(scraped_page: ScrapedPage, expected_digits: int) -> str | None:
    groups = _multi_field_totp_box_groups(scraped_page, expected_digits)
    if groups is None or len(groups) != 1:
        return None
    group = groups[0]
    elements = {element["id"]: element for element in scraped_page.elements if "id" in element}
    identity = (
        group.frame,
        _multi_field_totp_container_identity(group),
        tuple(_multi_field_totp_structural_identity(elements[element_id]) for element_id in group.box_element_ids),
    )
    return hashlib.sha256(repr(identity).encode()).hexdigest()


def _multi_field_totp_matching_scopes(
    scraped_page: ScrapedPage, identity: tuple[str, ...]
) -> list[tuple[dict[str, Any], str]]:
    matches: list[tuple[dict[str, Any], str]] = []

    def visit(nodes: list[dict[str, Any]], path: list[dict[str, Any]]) -> None:
        for node in nodes:
            current_path = [*path, node]
            frame = _multi_field_totp_node_frame(scraped_page, node)
            chain = _multi_field_totp_frame_chain(scraped_page, current_path, frame)
            if len(chain) == len(identity) and _multi_field_totp_scope_identity(chain) == identity:
                matches.append((node, frame))
            visit(node.get("children", []), current_path)

    visit(scraped_page.element_tree, [])
    return matches


def _multi_field_totp_scope_has_input(
    scraped_page: ScrapedPage, container: dict[str, Any], frame: str, excluded_ids: list[str]
) -> bool:
    if _multi_field_totp_node_frame(scraped_page, container) != frame:
        return False
    # Occupancy is independent of maxlength and group size; a wider replacement field is still a live input.
    if container["tagName"].lower() == "input" and container.get("id") not in excluded_ids:
        input_type = container.get("attributes", {}).get("type", "text")
        if not isinstance(input_type, str):
            raise TypeError("Malformed input type")
        if input_type.lower() in {"text", "number", "tel", "password"}:
            return True
    return any(
        _multi_field_totp_scope_has_input(scraped_page, child, frame, excluded_ids)
        for child in container.get("children", [])
    )


async def _multi_field_totp_frame_gone(page: Page, frame_id: str) -> bool | None:
    """Confirm removal only after every live child frame can be identified."""
    try:
        frames = page.frames
        if not isinstance(frames, (list, tuple)):
            return None
        unknown_frame = False
        for frame in frames:
            if frame is page.main_frame or frame.is_detached():
                continue
            try:
                frame_element = await frame.frame_element()
                # The iframe's ElementHandle is owned by the frame that resolved it -- its parent, or the
                # main frame when an orphan attach briefly leaves `parent_frame` None -- so read the id
                # there through the common evaluate abstraction, in the handle's own context.
                # `get_attribute` would instead block for the full 30s action timeout re-resolving
                # `:scope` once the parent document navigated.
                current_id = await SkyvernFrame.evaluate(
                    frame=frame.parent_frame or page.main_frame,
                    expression=f"(element) => element.getAttribute({json.dumps(SKYVERN_ID_ATTR)})",
                    arg=frame_element,
                )
            except Exception:
                unknown_frame = True
                continue
            if current_id == frame_id:
                return False
            if not current_id:
                unknown_frame = True
        return None if unknown_frame else True
    except Exception:
        return None


async def _document_continuity(scraped_page: ScrapedPage, page: Page) -> bool | None:
    """Return whether the live page still has the batch's original document.

    ``None`` is deliberately indeterminate: destroyed execution contexts and failed probes must
    fall through to the legacy dispatch path rather than being treated as continuity.
    """
    stored_loader_id = getattr(scraped_page, "_document_loader_id", None)
    if stored_loader_id is None:
        return None
    current_loader_id = await get_main_document_loader_id(page)
    return current_loader_id == stored_loader_id if current_loader_id is not None else None


async def _refresh_multi_field_totp_group_binding(
    scraped_page: ScrapedPage,
    page: Page,
    attempt: MultiFieldTotpAttempt,
    *,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    step_id: str | None = None,
    fresh_snapshot: ScrapedPage | None = None,
) -> tuple[ScrapedPage, list[str]] | MultiFieldTotpBindingFailure:
    """Rebind only to the original widget; distinguish its removal from an unreadable observation."""
    old_ids = attempt.box_element_ids
    live_box_count: int | None = None
    candidate_group_count: int | None = None
    original_frames: list[str] = []
    fresh_frames: list[str] = []
    loader_id_known = bool(getattr(scraped_page, "_document_loader_id", None))

    def classified(reason_code: str, failure: MultiFieldTotpBindingFailure) -> MultiFieldTotpBindingFailure:
        context = skyvern_context.current()
        LOG.info(
            "Multi-field OTP binding rejected",
            task_id=task_id or (context.task_id if context else None),
            workflow_run_id=workflow_run_id or (context.workflow_run_id if context else None),
            step_id=step_id or (context.step_id if context else None),
            reason=reason_code,
            reason_code=reason_code,
            classification=failure.value,
            live_boxes=live_box_count,
            candidate_groups=candidate_group_count,
            original_frames=original_frames,
            fresh_frames=fresh_frames,
            loader_id_known=loader_id_known,
        )
        return failure

    try:
        original_frames = sorted({scraped_page.id_to_frame_dict[element_id] for element_id in old_ids})
        continuity = await _document_continuity(scraped_page, page)
        loader_id_known = loader_id_known and continuity is not None
        if continuity is False:
            return classified("document_changed_before_snapshot", MultiFieldTotpBindingFailure.TEARDOWN)
        if page.url != scraped_page.url:
            return classified("url_changed_before_snapshot", MultiFieldTotpBindingFailure.TEARDOWN)
        original_groups = _multi_field_totp_box_groups(scraped_page, attempt.expected_digits)
        if original_groups is None or len(original_groups) != 1 or original_groups[0].box_element_ids != old_ids:
            return classified("original_group_invalid", MultiFieldTotpBindingFailure.UNCONFIRMED)
        original_group = original_groups[0]
        container_identity = _multi_field_totp_container_identity(original_group)

        fresh = (
            fresh_snapshot
            if fresh_snapshot is not None
            else await scraped_page.generate_scraped_page_without_screenshots()
        )
        fresh_frames = sorted(set(fresh.id_to_frame_dict.values()))
        continuity = await _document_continuity(scraped_page, page)
        loader_id_known = loader_id_known and continuity is not None
        fresh_loader_id = getattr(fresh, "_document_loader_id", None)
        loader_id_known = loader_id_known and bool(fresh_loader_id)
        if continuity is False or (
            fresh_loader_id
            and getattr(scraped_page, "_document_loader_id", None)
            and fresh_loader_id != scraped_page._document_loader_id
        ):
            return classified("document_changed_after_snapshot", MultiFieldTotpBindingFailure.TEARDOWN)
        if fresh.url != scraped_page.url or page.url != scraped_page.url:
            return classified("url_changed_after_snapshot", MultiFieldTotpBindingFailure.TEARDOWN)
        groups = _multi_field_totp_box_groups(fresh, attempt.expected_digits)
        candidate_group_count = len(groups) if groups is not None else None
        if groups is None:
            return classified("malformed_fresh_groups", MultiFieldTotpBindingFailure.UNCONFIRMED)
        if len(groups) > 1:
            return classified("ambiguous_fresh_groups", MultiFieldTotpBindingFailure.UNCONFIRMED)

        # A returned snapshot observes the main frame; child frames can be omitted by the scraper.
        frame_observed = original_group.frame == "main.frame" or original_group.frame in fresh_frames
        frame_gone: bool | None = False
        if not frame_observed:
            frame_gone = await _multi_field_totp_frame_gone(page, original_group.frame)
            if frame_gone is not True:
                return classified("original_frame_unobserved", MultiFieldTotpBindingFailure.UNCONFIRMED)

        live_boxes = []
        if frame_gone:
            live_boxes = [False] * len(old_ids)
        else:
            for element_id in old_ids:
                css = scraped_page.id_to_css_dict.get(element_id)
                frame = scraped_page.id_to_frame_dict.get(element_id)
                if not css or not frame:
                    return classified("box_locator_metadata_missing", MultiFieldTotpBindingFailure.UNCONFIRMED)
                locator, _ = await resolve_locator(scraped_page, page, frame, css)
                count = await locator.count()
                if count not in (0, 1):
                    return classified("original_box_count_ambiguous", MultiFieldTotpBindingFailure.UNCONFIRMED)
                live_boxes.append(count == 1)
                live_box_count = sum(live_boxes)
        live_box_count = sum(live_boxes)
        scopes = _multi_field_totp_matching_scopes(fresh, container_identity)
        if len(scopes) > 1:
            return classified("ambiguous_original_scope", MultiFieldTotpBindingFailure.UNCONFIRMED)
        matching_groups = [
            group for group in groups if _multi_field_totp_container_identity(group) == container_identity
        ]
        container_live = False
        live_container_scope: tuple[dict[str, Any], str] | None = None
        if not matching_groups and not frame_gone:
            # Mutable ancestor attributes cannot establish removal of a still-live owning container.
            container_id = original_group.container.get("id")
            if not isinstance(container_id, str):
                return classified("container_locator_metadata_missing", MultiFieldTotpBindingFailure.UNCONFIRMED)
            container_css = scraped_page.id_to_css_dict.get(container_id)
            container_frame = scraped_page.id_to_frame_dict.get(container_id)
            if not container_css or not container_frame or container_frame != original_group.frame:
                return classified("container_locator_metadata_missing", MultiFieldTotpBindingFailure.UNCONFIRMED)
            locator, _ = await resolve_locator(scraped_page, page, container_frame, container_css)
            container_count = await locator.count()
            if container_count not in (0, 1):
                return classified("original_container_count_ambiguous", MultiFieldTotpBindingFailure.UNCONFIRMED)
            container_live = container_count == 1
            if container_live:
                fresh_container = fresh.id_to_element_dict.get(container_id)
                if fresh_container is not None and fresh.id_to_frame_dict.get(container_id) == container_frame:
                    live_container_scope = fresh_container, container_frame
        continuity = await _document_continuity(scraped_page, page)
        loader_id_known = loader_id_known and continuity is not None
        if continuity is False:
            return classified("document_changed_after_probe", MultiFieldTotpBindingFailure.TEARDOWN)
        if page.url != scraped_page.url:
            return classified("url_changed_after_probe", MultiFieldTotpBindingFailure.TEARDOWN)

        if not matching_groups:
            if container_live:
                if live_container_scope is None:
                    return classified("live_container_unobserved", MultiFieldTotpBindingFailure.UNCONFIRMED)
                if _multi_field_totp_scope_has_input(fresh, *live_container_scope, old_ids):
                    return classified("replacement_inputs_in_original_scope", MultiFieldTotpBindingFailure.UNCONFIRMED)
                return classified("original_container_still_live", MultiFieldTotpBindingFailure.UNCONFIRMED)
            if scopes:
                return classified("original_scope_still_present", MultiFieldTotpBindingFailure.UNCONFIRMED)
            if groups:
                return classified("ancestor_identity_mismatch", MultiFieldTotpBindingFailure.UNCONFIRMED)
            if not any(live_boxes) and (frame_observed or frame_gone):
                return classified("original_widget_scope_gone", MultiFieldTotpBindingFailure.TEARDOWN)
            return classified("original_container_not_confirmed", MultiFieldTotpBindingFailure.UNCONFIRMED)
        if len(scopes) != 1:
            return classified("original_scope_not_confirmed", MultiFieldTotpBindingFailure.UNCONFIRMED)
        group = matching_groups[0]
        if all(live_boxes):
            if (
                group.box_element_ids != old_ids
                or group.frame != original_group.frame
                or any(
                    fresh.id_to_frame_dict.get(element_id) != scraped_page.id_to_frame_dict.get(element_id)
                    for element_id in old_ids
                )
            ):
                return classified("live_id_or_frame_changed", MultiFieldTotpBindingFailure.UNCONFIRMED)
        elif (
            original_group.frame != "main.frame"
            or group.frame != "main.frame"
            or any(scraped_page.id_to_frame_dict.get(element_id) != "main.frame" for element_id in old_ids)
            or any(fresh.id_to_frame_dict.get(element_id) != "main.frame" for element_id in group.box_element_ids)
        ):
            return classified("remount_frame_not_main", MultiFieldTotpBindingFailure.UNCONFIRMED)

        old_identity = [
            _multi_field_totp_structural_identity(scraped_page.id_to_element_dict[element_id]) for element_id in old_ids
        ]
        new_identity = [
            _multi_field_totp_structural_identity(fresh.id_to_element_dict[element_id])
            for element_id in group.box_element_ids
        ]
        if old_identity != new_identity:
            return classified("box_identity_mismatch", MultiFieldTotpBindingFailure.UNCONFIRMED)
        if not loader_id_known:
            return classified("loader_id_unknown", MultiFieldTotpBindingFailure.UNCONFIRMED)
        return fresh, group.box_element_ids
    except Exception:
        return classified("observation_failed", MultiFieldTotpBindingFailure.UNCONFIRMED)


class RetryOutcome(StrEnum):
    RETRIED = "retried"
    NO_RETRY = "no_retry"
    SECOND_REJECTION = "second_rejection"


SECOND_REJECTION_REASON = "The one-time code was rejected twice."


class MultiFieldTotpSubmitControlNotActionable(Exception):
    pass


_MULTI_FIELD_TOTP_OBSERVER_JS = r"""(box, {prefix, reset, replaceBoxes, expected}) => {
    const doc = box.ownerDocument;
    let state = doc.__skyvernOtpFillObserver;
    const signature = node => {
        const parts = ['type', 'id', 'name', 'autocomplete'].map(key => node.getAttribute(key) || '');
        return parts.slice(1).some(Boolean) ? JSON.stringify(parts) : null;
    };
    if (reset || !state || state.prefix !== prefix) {
        if (state) {
            doc.removeEventListener('input', state.listener, true);
            state.observer?.disconnect();
        }
        state = {prefix, scope: box.parentElement || box.getRootNode(), max: 0, inputCount: 0, signatures: new Set()};
        state.liveInputs = scope => Array.from(scope.querySelectorAll('input')).filter(node => {
            const type = (node.getAttribute('type') || 'text').toLowerCase();
            if (!['text', 'number', 'tel', 'password'].includes(type)) return false;
            for (let ancestor = node; ancestor && ancestor !== doc; ancestor = ancestor.parentElement) {
                if (ancestor.getAttribute?.('aria-hidden') === 'true') return false;
                const style = getComputedStyle(ancestor);
                if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
            }
            const max = node.getAttribute('maxlength');
            if (max !== null && max.trim() !== '') return Number(max) === 1;
            const mode = (node.getAttribute('inputmode') || '').toLowerCase();
            const pattern = node.getAttribute('pattern') || '';
            return ['numeric', 'decimal'].includes(mode) || /digit|\\d|\[0-9\]/i.test(pattern) || /^\d+$/.test(pattern) ||
                state.signatures.has(signature(node));
        });
        state.observe = () => {
            state.observer.disconnect();
            state.observer.observe(state.scope, {childList: true, subtree: true, attributes: true, attributeFilter: ['value']});
            state.observer.observe(doc, {childList: true, subtree: true});
        };
        state.resolveScope = target => {
            if (state.scope.isConnected) return true;
            let anchor = target?.isConnected ? target : doc.activeElement;
            while (anchor?.shadowRoot?.activeElement) anchor = anchor.shadowRoot.activeElement;
            while (anchor) {
                const eligibleCount = anchor.querySelectorAll ? state.liveInputs(anchor).length : 0;
                if (eligibleCount >= expected && eligibleCount <= state.inputCount) {
                    state.scope = anchor;
                    state.observe();
                    return true;
                }
                const root = anchor.getRootNode?.();
                anchor = anchor.parentElement || anchor.host || (root !== anchor ? root : null);
            }
            return false;
        };
        state.measure = target => {
            if (!state.resolveScope(target)) return;
            const live = state.liveInputs(state.scope);
            if (live.length !== expected) return;
            const count = Math.min(expected, live.filter(node => node.value.length >= 1).length);
            if (count > state.max) {
                state.max = count;
                doc.documentElement.setAttribute('data-skyvern-otp-max-filled', String(count));
                console.debug(prefix + count);
            }
        };
        state.listener = event => state.measure(event.composedPath?.()[0] || event.target);
        state.observer = new MutationObserver(() => state.measure(doc.activeElement));
        doc.__skyvernOtpFillObserver = state;
        doc.documentElement.setAttribute('data-skyvern-otp-max-filled', '0');
        doc.addEventListener('input', state.listener, true);
    }
    if (replaceBoxes) {
        state.scope = box.parentElement || box.getRootNode();
        state.signatures.clear();
    }
    const boxSignature = signature(box);
    if (boxSignature) state.signatures.add(boxSignature);
    while (!state.scope.contains(box)) {
        const parent = state.scope.parentElement || state.scope.getRootNode();
        if (parent === state.scope) throw new Error('OTP observer scope unavailable');
        state.scope = parent;
    }
    state.inputCount = state.liveInputs(state.scope).length;
    state.observe();
}"""


@dataclass
class MultiFieldTotpFillObserver:
    page: Page
    attempt: MultiFieldTotpAttempt
    prefix: str

    def on_console(self, message: Any) -> None:
        text = message.text
        if isinstance(text, str) and text.startswith(self.prefix):
            count = text[len(self.prefix) :]
            if count.isascii() and count.isdigit() and 0 <= int(count) <= self.attempt.expected_digits:
                self.attempt.observed_max_filled = max(self.attempt.observed_max_filled, int(count))

    async def bind(self, boxes: list[Locator], *, reset: bool = False) -> None:
        for index, box in enumerate(boxes):
            await box.evaluate(
                _MULTI_FIELD_TOTP_OBSERVER_JS,
                {
                    "prefix": self.prefix,
                    "reset": reset and index == 0,
                    "replaceBoxes": index == 0,
                    "expected": self.attempt.expected_digits,
                },
                timeout=1000,
            )

    async def refresh(self, boxes: list[Locator]) -> None:
        try:
            if not boxes or await boxes[0].count() != 1:
                return
            count = await boxes[0].evaluate(
                "(box, prefix) => box.ownerDocument.__skyvernOtpFillObserver?.prefix === prefix ? Number(box.ownerDocument.documentElement.getAttribute('data-skyvern-otp-max-filled')) : 0",
                self.prefix,
                timeout=1000,
            )
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= self.attempt.expected_digits:
                self.attempt.observed_max_filled = max(self.attempt.observed_max_filled, count)
        except Exception:
            # The console count survives destruction of the document that held the attribute.
            pass

    def stop(self) -> None:
        self.page.remove_listener("console", self.on_console)


async def install_multi_field_totp_fill_observer(
    page: Page, boxes: list[Locator], attempt: MultiFieldTotpAttempt
) -> MultiFieldTotpFillObserver | None:
    attempt.observed_max_filled = 0
    observer = MultiFieldTotpFillObserver(page, attempt, f"__skyvern_otp_filled_{secrets.token_hex(8)}:")
    try:
        page.on("console", observer.on_console)
        await observer.bind(boxes, reset=True)
    except BaseException as exc:
        try:
            observer.stop()
        except Exception:
            pass
        if not isinstance(exc, Exception):
            raise
        LOG.info("Multi-field OTP fill observer unavailable", error_type=type(exc).__name__)
        return None
    return observer


def parse_multi_field_totp_rejection(response: Any) -> bool:
    return (
        isinstance(response, dict)
        and response.get("code_rejected") is True
        and isinstance(response.get("reason"), str)
        and bool(response["reason"].strip())
    )


async def _retry_box_locators(page: Page, scraped_page: ScrapedPage, element_ids: list[str]) -> list[Locator]:
    boxes = []
    for element_id in element_ids:
        locator, _ = await resolve_locator(
            scraped_page,
            page,
            scraped_page.id_to_frame_dict[element_id],
            scraped_page.id_to_css_dict[element_id],
        )
        boxes.append(locator)
    return boxes


async def clear_multi_field_totp_boxes(page: Page, scraped_page: ScrapedPage, element_ids: list[str]) -> bool:
    boxes = await _retry_box_locators(page, scraped_page, element_ids)
    for box in boxes:
        await box.fill("", timeout=2000)
    return bool(boxes) and all([await box.input_value(timeout=2000) == "" for box in boxes])


async def _multi_field_totp_widget_scopes(boxes: list[Locator], *, include_ancestors: bool = False) -> list[Locator]:
    if not boxes:
        return []
    form = boxes[0].locator("xpath=ancestor::form[1]")
    if await form.count() == 1 and all([await form.filter(has=box).count() == 1 for box in boxes]):
        return [form]
    scopes = []
    ancestors = boxes[0].locator("xpath=ancestor::*[not(self::body or self::html)]")
    for index in reversed(range(await ancestors.count())):
        ancestor = ancestors.nth(index)
        if all([await ancestor.filter(has=box).count() == 1 for box in boxes]):
            scopes.append(ancestor)
            if not include_ancestors or len(scopes) == 3:
                break
    return scopes


_MULTI_FIELD_TOTP_SUBMIT_WORDS = ("verify", "submit", "continue", "confirm", "next", "sign in", "log in", "done")
_MULTI_FIELD_TOTP_SUBMIT_SELECTOR = 'button, input[type="submit"], [role="button"]'
_MULTI_FIELD_TOTP_SUBMIT_METADATA_JS = """(scope, selector) => {
    const owner = scope.tagName === 'FORM' ? scope : null;
    return Array.from(scope.querySelectorAll(selector)).map(control => {
        const form = control.closest('form');
        const formAttribute = control.getAttribute('form');
        const style = getComputedStyle(control);
        return {
            tag: control.localName,
            type: control.getAttribute('type'),
            has_form: owner !== null,
            form_matches: owner !== null
                ? form === owner && (formAttribute === null || formAttribute === owner.getAttribute('id'))
                : form === null && formAttribute === null,
            disabled: control.hasAttribute('disabled') || control.matches(':disabled'),
            aria_disabled: control.closest('[aria-disabled="true" i]') !== null,
            visible: control.isConnected && control.getClientRects().length > 0 &&
                !['hidden', 'collapse'].includes(style.visibility) && style.display !== 'none',
            label: control.getAttribute('aria-label') || control.innerText || control.getAttribute('value') || '',
        };
    });
}"""


@dataclass
class MultiFieldTotpSubmitControl:
    locator: Locator
    log_fields: dict[str, Any]


def _multi_field_totp_submit_control_log_fields(
    metadata: dict[str, Any], *, candidate_index: int | None = None
) -> dict[str, Any]:
    tag, input_type = metadata.get("tag"), metadata.get("type")
    label = metadata.get("label") or ""
    return {
        "control_tag": tag if tag in {"button", "input", "a", "div", "span"} else "other",
        "control_type": input_type
        if input_type in {"submit", "button", "reset", "text", "tel", "number", "password"}
        else None,
        "rank_word": next(
            (word for word in _MULTI_FIELD_TOTP_SUBMIT_WORDS if re.search(r"\b" + word + r"\b", label, re.I)), None
        ),
        "submit_type": input_type == "submit",
        "candidate_index": candidate_index,
    }


async def find_multi_field_totp_submit_controls(boxes: list[Locator]) -> list[MultiFieldTotpSubmitControl]:
    scopes = await _multi_field_totp_widget_scopes(boxes, include_ancestors=True)
    if not scopes:
        return []
    # The outermost permitted scope contains every candidate without duplicating nested scopes.
    scope = scopes[-1]
    controls = scope.locator(_MULTI_FIELD_TOTP_SUBMIT_SELECTOR)
    metadata = await scope.evaluate(
        _MULTI_FIELD_TOTP_SUBMIT_METADATA_JS, _MULTI_FIELD_TOTP_SUBMIT_SELECTOR, timeout=1000
    )
    candidates: list[tuple[int, MultiFieldTotpSubmitControl]] = []
    for index, item in enumerate(metadata):
        if not item["form_matches"] or item["disabled"] or item["aria_disabled"] or not item["visible"]:
            continue
        fields = _multi_field_totp_submit_control_log_fields(item, candidate_index=index)
        word = fields["rank_word"]
        rank = _MULTI_FIELD_TOTP_SUBMIT_WORDS.index(word) if word else len(_MULTI_FIELD_TOTP_SUBMIT_WORDS)
        form_submit = fields["submit_type"] and item["has_form"]
        if word or form_submit:
            candidates.append(
                (rank - 9 if form_submit else rank, MultiFieldTotpSubmitControl(controls.nth(index), fields))
            )
    return [candidate for _, candidate in sorted(candidates, key=lambda candidate: candidate[0])]


_MULTI_FIELD_TOTP_WIDGET_FINGERPRINT_JS = """(element) => {
    if (!element.isConnected) return null;
    const result = {};
    const hash = (content) => {
        let value = 2166136261;
        for (let i = 0; i < content.length; i++) value = Math.imul(value ^ content.charCodeAt(i), 16777619);
        return (value >>> 0).toString(16);
    };
    const withoutDigits = value => value.normalize('NFKC').replace(/[0-9]+(?::[0-9]+)*(?:s\\b)?/g, '<number>');
    const visit = (node, path, inheritedFeedback = false) => {
        if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(node.tagName)) return;
        if (node !== element && node.tagName === 'FORM') return;
        const text = ['INPUT', 'TEXTAREA'].includes(node.tagName) ? '' :
            Array.from(node.childNodes).filter(child => child.nodeType === 3)
                .map(child => child.textContent).join('').trim();
        const attributes = Array.from(node.attributes)
            .filter(attr => !['value', 'unique_id'].includes(attr.name) && !attr.name.startsWith('data-skyvern-'))
            .map(attr => [attr.name, attr.value]).sort((a, b) => a[0].localeCompare(b[0]));
        const style = getComputedStyle(node);
        const visible = node.getClientRects().length > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        const content = JSON.stringify([node.tagName, text, attributes, visible]);
        const nonNumeric = JSON.stringify([node.tagName, withoutDigits(text), attributes.map(([key, value]) => [key, withoutDigits(value)]), visible]);
        const feedback = inheritedFeedback || attributes.some(([key, value]) =>
            (key === 'role' && value.split(/\\s+/).includes('alert')) || key === 'aria-live' || key === 'aria-invalid');
        const counter = /\\b(?:invalid|incorrect|wrong|expired)\\b/i.test(text) &&
            text.normalize('NFKC').match(/\\b(\\d+)\\s+(?:attempts?|tries)\\s+(?:left|remaining)\\b/i);
        const signal = feedback ? '1' : counter ? `counter-${hash(counter[1])}` : '0';
        result[path] = [hash(content), hash(nonNumeric), signal].join(':');
        Array.from(node.children).forEach((child, index) => visit(child, `${path}/${index}`, feedback));
    };
    visit(element, 'root');
    return result;
}"""


@dataclass
class MultiFieldTotpSubmissionBaseline:
    url: str
    loader_id: str | None
    nodes: dict[str, str]
    volatile_paths: frozenset[str]


async def _multi_field_totp_widget_snapshot(
    boxes: list[Locator], *, scope: Locator | None = None
) -> dict[str, str] | None:
    if scope is None:
        scopes = await _multi_field_totp_widget_scopes(boxes, include_ancestors=True)
        if not scopes:
            return None
        scope = scopes[-1]
    snapshot = await scope.evaluate(_MULTI_FIELD_TOTP_WIDGET_FINGERPRINT_JS, timeout=1000)
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in snapshot.items()
    ):
        raise TypeError("Invalid OTP widget snapshot")
    return snapshot


def _multi_field_totp_widget_changed(baseline: MultiFieldTotpSubmissionBaseline, current: dict[str, str]) -> bool:
    for path in baseline.nodes.keys() | current.keys():
        before, after = baseline.nodes.get(path), current.get(path)
        if path in baseline.volatile_paths or before == after:
            continue
        if before is not None and after is not None:
            old_parts, new_parts = before.split(":"), after.split(":")
            if len(old_parts) == len(new_parts) == 3 and old_parts[1:] == new_parts[1:] and new_parts[2] != "1":
                continue
        return True
    return False


async def capture_multi_field_totp_submission_baseline(
    page: Page, boxes: list[Locator], *, sample_count: Literal[1, 3] = 3
) -> MultiFieldTotpSubmissionBaseline | None:
    try:
        url = page.url
        scopes = await _multi_field_totp_widget_scopes(boxes, include_ancestors=True)
        if not scopes:
            return None
        samples = []
        for sample_index in range(sample_count):
            if sample_index:
                await asyncio.sleep(0.6)
            snapshot = await _multi_field_totp_widget_snapshot(boxes, scope=scopes[-1])
            if snapshot is None or page.url != url:
                return None
            samples.append(snapshot)
        return MultiFieldTotpSubmissionBaseline(
            url=url,
            loader_id=await get_main_document_loader_id(page),
            nodes=samples[-1],
            volatile_paths=frozenset(
                path
                for path in set().union(*(sample.keys() for sample in samples))
                if any(sample.get(path) != samples[0].get(path) for sample in samples[1:])
            ),
        )
    except Exception:
        return None


async def multi_field_totp_submission_evidence(
    page: Page,
    scraped_page: ScrapedPage,
    attempt: MultiFieldTotpAttempt,
    *,
    baseline: MultiFieldTotpSubmissionBaseline | None = None,
    post_dispatch: bool = False,
) -> bool:
    were_full = attempt.observed_max_filled == attempt.expected_digits
    if not post_dispatch and not were_full:
        return False
    original_url = baseline.url if baseline is not None else scraped_page.url
    old_loader = baseline.loader_id if baseline is not None else getattr(scraped_page, "_document_loader_id", None)

    if await _multi_field_totp_document_changed(page, original_url, old_loader):
        return True
    boxes: list[Locator] = []
    try:
        boxes = await _retry_box_locators(page, scraped_page, attempt.box_element_ids)
        counts = [await box.count() for box in boxes]
        if not all(count == 1 for count in counts):
            binding = await _refresh_multi_field_totp_group_binding(scraped_page, page, attempt)
            if isinstance(binding, MultiFieldTotpBindingFailure):
                return (
                    binding == MultiFieldTotpBindingFailure.TEARDOWN
                    or (bool(counts) and not any(counts))
                    or await _multi_field_totp_document_changed(page, original_url, old_loader)
                )
            fresh, ids = binding
            boxes = await _retry_box_locators(page, fresh, ids)
        cleared = bool(boxes) and all([await box.input_value(timeout=1000) == "" for box in boxes])
        if cleared and were_full:
            return True
        if await _multi_field_totp_document_changed(page, original_url, old_loader):
            return True
        if post_dispatch and baseline is not None:
            current = await _multi_field_totp_widget_snapshot(boxes)
            if current is None:
                return await _multi_field_totp_boxes_disappeared(boxes) or await _multi_field_totp_document_changed(
                    page, original_url, old_loader
                )
            return page.url != original_url or _multi_field_totp_widget_changed(baseline, current)
        return False
    except Exception:
        if post_dispatch and (
            await _multi_field_totp_document_changed(page, original_url, old_loader)
            or await _multi_field_totp_boxes_disappeared(boxes)
        ):
            return True
        raise


async def _multi_field_totp_document_changed(page: Page, original_url: str, old_loader: str | None) -> bool:
    if page.url != original_url:
        return True
    loader = await get_main_document_loader_id(page)
    return page.url != original_url or (loader is not None and old_loader is not None and loader != old_loader)


async def _multi_field_totp_boxes_disappeared(boxes: list[Locator]) -> bool:
    try:
        return bool(boxes) and all([await box.count() == 0 for box in boxes])
    except Exception:
        return False


async def submit_multi_field_totp_retry(
    page: Page,
    scraped_page: ScrapedPage,
    attempt: MultiFieldTotpAttempt,
    *,
    binding_confirmed: bool = False,
    task_id: str | None = None,
    workflow_run_id: str | None = None,
    step_id: str | None = None,
) -> bool:
    context = skyvern_context.current()
    log_context = dict(
        task_id=task_id or (context.task_id if context else None),
        workflow_run_id=workflow_run_id or (context.workflow_run_id if context else None),
        step_id=step_id or (context.step_id if context else None),
    )

    started = time.monotonic()
    durations = dict.fromkeys(
        (
            "pre_evidence_ms",
            "rebind_ms",
            "locators_ms",
            "discovery_ms",
            "baseline_ms",
            "dispatch_ms",
            "post_evidence_ms",
        ),
        0.0,
    )
    candidates_considered = 0

    @contextmanager
    def timed(phase: str) -> Iterator[None]:
        phase_started = time.monotonic()
        try:
            yield
        finally:
            durations[phase] += (time.monotonic() - phase_started) * 1000

    def record(
        phase: str,
        status: str,
        fields: dict[str, Any],
        *,
        error: BaseException | None = None,
        reason: str | None = None,
    ) -> None:
        LOG.info(
            "Multi-field TOTP submit dispatch",
            phase=phase,
            status=status,
            **fields,
            **log_context,
            error_type=type(error).__name__ if error is not None else None,
            reason=reason,
        )

    async def dispatch(control: Locator, phase: str, fields: dict[str, Any]) -> None:
        record(phase, "started", fields)
        try:
            with timed("dispatch_ms"):
                if phase == "trial":
                    await control.click(trial=True, timeout=2000)
                elif phase == "click":
                    await control.click(timeout=2000, no_wait_after=True)
                else:
                    await control.press("Enter", timeout=2000)
        except (Exception, asyncio.CancelledError) as exc:
            record(phase, "failed", fields, error=exc)
            raise
        record(phase, "succeeded", fields)

    try:
        if skyvern_context.multi_field_totp_retry_budget_exhausted(
            log_context["task_id"],
            log_refusal=True,
            workflow_run_id=log_context["workflow_run_id"],
            step_id=log_context["step_id"],
        ):
            return False
        if attempt.fill_verified:
            with timed("pre_evidence_ms"):
                if await _multi_field_totp_document_changed(
                    page, scraped_page.url, getattr(scraped_page, "_document_loader_id", None)
                ):
                    return True
        else:
            with timed("pre_evidence_ms"):
                for _ in range(3):
                    if await multi_field_totp_submission_evidence(page, scraped_page, attempt):
                        return True
                    await asyncio.sleep(0.2)
        element_ids = attempt.box_element_ids
        if not binding_confirmed:
            with timed("rebind_ms"):
                binding = await _refresh_multi_field_totp_group_binding(scraped_page, page, attempt)
            if isinstance(binding, MultiFieldTotpBindingFailure):
                return (
                    binding == MultiFieldTotpBindingFailure.TEARDOWN
                    and attempt.observed_max_filled == attempt.expected_digits
                )
            scraped_page, element_ids = binding
            attempt = replace(attempt, box_element_ids=element_ids)
        with timed("locators_ms"):
            boxes = await _retry_box_locators(page, scraped_page, element_ids)
        with timed("discovery_ms"):
            controls = await find_multi_field_totp_submit_controls(boxes)
        last_trial_error: Exception | None = None
        chosen: Locator | None = None
        fields: dict[str, Any] = {}
        baseline = None
        for candidate in controls:
            candidates_considered += 1
            control, fields = candidate.locator, candidate.log_fields
            try:
                if await control.count() != 1:
                    record("trial", "failed", fields, reason="submit_control_not_actionable")
                    continue
                await dispatch(control, "trial", fields)
                with timed("baseline_ms"):
                    baseline = await capture_multi_field_totp_submission_baseline(page, boxes)
                with timed("pre_evidence_ms"):
                    if await multi_field_totp_submission_evidence(page, scraped_page, attempt):
                        return True
                if await control.count() != 1:
                    record("click", "failed", fields, reason="submit_control_not_actionable")
                    continue
            except Exception as exc:
                last_trial_error = exc
                continue
            chosen = control
            break
        phase = "click" if chosen is not None else "enter"
        if chosen is None:
            if not boxes or await boxes[-1].count() != 1:
                record("enter", "failed", {}, reason="submit_control_not_actionable")
                raise MultiFieldTotpSubmitControlNotActionable() from last_trial_error
            chosen = boxes[-1]
            fields = _multi_field_totp_submit_control_log_fields({"tag": "input"})
            with timed("baseline_ms"):
                baseline = await capture_multi_field_totp_submission_baseline(page, boxes)
            with timed("pre_evidence_ms"):
                if await multi_field_totp_submission_evidence(page, scraped_page, attempt):
                    return True
            if await chosen.count() != 1:
                record("enter", "failed", fields, reason="submit_control_not_actionable")
                raise MultiFieldTotpSubmitControlNotActionable() from last_trial_error
        try:
            await dispatch(chosen, phase, fields)
        except Exception as exc:
            with timed("post_evidence_ms"):
                if await _multi_field_totp_document_changed(
                    page, scraped_page.url, getattr(scraped_page, "_document_loader_id", None)
                ) or await _multi_field_totp_boxes_disappeared(boxes):
                    return True
            if phase == "enter":
                raise MultiFieldTotpSubmitControlNotActionable() from exc
            raise
        try:
            with timed("post_evidence_ms"):
                for _ in range(10):
                    if await multi_field_totp_submission_evidence(
                        page,
                        scraped_page,
                        attempt,
                        baseline=baseline,
                        post_dispatch=True,
                    ):
                        return True
                    await asyncio.sleep(0.2)
        except (Exception, asyncio.CancelledError) as exc:
            record(phase, "failed", fields, error=exc, reason="submission_observation_failed")
            if phase == "enter" and isinstance(exc, Exception):
                raise MultiFieldTotpSubmitControlNotActionable() from exc
            raise
        record(phase, "failed", fields, reason="no_submission_evidence")
        if phase == "enter":
            raise MultiFieldTotpSubmitControlNotActionable() from last_trial_error
        return False
    finally:
        with contained_effect("multi_field_totp_retry_submit_timing", **log_context):
            LOG.info(
                "Multi-field TOTP retry submit timing",
                **{phase: round(duration, 1) for phase, duration in durations.items()},
                candidates_considered=candidates_considered,
                total_ms=round((time.monotonic() - started) * 1000, 1),
                **log_context,
            )
