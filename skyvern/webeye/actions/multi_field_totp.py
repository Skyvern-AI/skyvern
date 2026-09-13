"""Shared OTP group discovery and continuity checks; no browser writes."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from playwright.async_api import Page

from skyvern.constants import SKYVERN_ID_ATTR
from skyvern.forge.sdk.core.skyvern_context import MultiFieldTotpAttempt
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from skyvern.webeye.scraper.scraper import structural_identity
from skyvern.webeye.utils.document import get_main_document_loader_id
from skyvern.webeye.utils.dom import resolve_locator

LOG = structlog.get_logger()


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


def _multi_field_totp_box_groups(
    scraped_page: ScrapedPage | None, expected_digits: int
) -> list[MultiFieldTotpGroup] | None:
    """Return exact-size groups with their owning containers, or None for malformed page data."""
    if (
        scraped_page is None
        or not isinstance(expected_digits, int)
        or isinstance(expected_digits, bool)
        or expected_digits <= 0
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

            input_type = attributes.get("type", "text")
            if not isinstance(input_type, str) or input_type.lower() not in {"text", "number", "tel", "password"}:
                continue
            maxlength = attributes.get("maxlength")
            if maxlength is not None and not isinstance(maxlength, (str, int)):
                return None
            try:
                maxlength_value = int(maxlength) if maxlength is not None and str(maxlength).strip() else None
            except (TypeError, ValueError):
                return None
            pattern = attributes.get("pattern", "")
            inputmode = attributes.get("inputmode", "")
            if not isinstance(pattern, str) or not isinstance(inputmode, str):
                return None
            has_digit_constraint = bool(
                inputmode.lower() in {"numeric", "decimal"}
                or "digit" in pattern.lower()
                or "\\d" in pattern
                or "[0-9]" in pattern
                or pattern.isdigit()
            )
            if maxlength_value is not None and maxlength_value < 1:
                continue
            if maxlength_value != 1 and (
                maxlength_value is not None and maxlength_value > 1 or not has_digit_constraint
            ):
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
            if len(ids) == expected_digits
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
                    if len(descendant_ids) == expected_digits:
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


def _multi_field_totp_box_group(scraped_page: ScrapedPage | None, expected_digits: int) -> list[str] | None:
    """Return the only exact-size, frame/container-scoped group of OTP inputs on a page."""
    groups = _multi_field_totp_box_groups(scraped_page, expected_digits)
    return groups[0].box_element_ids if groups is not None and len(groups) == 1 else None


def _multi_field_totp_structural_identity(element: dict) -> str:
    def without_values(node: dict) -> dict:
        copied = dict(node)
        if "attributes" in node:
            copied["attributes"] = {
                key: value
                for key, value in node["attributes"].items()
                if key not in {"value", SKYVERN_ID_ATTR} and not key.startswith("data-skyvern-")
            }
            placeholder = copied["attributes"].get("placeholder")
            if isinstance(placeholder, str) and re.fullmatch(r"[0-9]+|\*+", placeholder):
                copied["attributes"]["placeholder"] = "<otp-digit>"
            # Screenshot caret cleanup can leave an empty style attribute that a remount drops.
            if copied["attributes"].get("style") == "":
                del copied["attributes"]["style"]
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
                current_id = await frame_element.get_attribute(SKYVERN_ID_ATTR)
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
    scraped_page: ScrapedPage, page: Page, attempt: MultiFieldTotpAttempt
) -> tuple[ScrapedPage, list[str]] | MultiFieldTotpBindingFailure:
    """Rebind only to the original widget; distinguish its removal from an unreadable observation."""
    old_ids = attempt.box_element_ids
    live_box_count: int | None = None
    candidate_group_count: int | None = None
    original_frames: list[str] = []
    fresh_frames: list[str] = []
    loader_id_known = bool(getattr(scraped_page, "_document_loader_id", None))

    def classified(reason_code: str, failure: MultiFieldTotpBindingFailure) -> MultiFieldTotpBindingFailure:
        LOG.info(
            "Multi-field OTP binding rejected",
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

        fresh = await scraped_page.generate_scraped_page_without_screenshots()
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
