import ast
from collections import Counter
from pathlib import Path

# Lower bound: this broad Playwright/CDP-shaped surface is intentionally conservative, but Python's
# dynamic receivers mean no static method-name inventory can prove exhaustiveness. Runtime binding
# checks land with the sink integration; broadening this set may legitimately discover more sites.
_CANDIDATE_METHODS = frozenset(
    """accept add_cookies add_init_script bring_to_front check clear clear_cookies clear_permissions
    click close dblclick dispatch_event dismiss down drag_and_drop drag_to emulate_media evaluate
    expose_binding expose_function fill focus go_back go_forward goto grant_permissions hover insert_text
    move new_page press press_sequentially reload route scroll_into_view_if_needed select_text select_option
    send set_checked set_content set_extra_http_headers set_files set_geolocation set_input_files set_offline
    set_viewport_size tap type uncheck unroute up wheel""".split()
)

_DISCOVERED_BROWSER_API_CALLS = {
    "skyvern/forge/agent.py": Counter({"close": 1, "evaluate": 2, "new_page": 1}),
    "skyvern/forge/agent_functions.py": Counter({"close": 1, "scroll_into_view_if_needed": 1}),
    "skyvern/webeye/actions/handler.py": Counter(
        {
            "bring_to_front": 2,
            "check": 2,
            "click": 24,
            "clear": 4,
            "close": 5,
            "dblclick": 2,
            "evaluate": 15,
            "fill": 2,
            "focus": 3,
            "go_back": 1,
            "go_forward": 1,
            "goto": 2,
            "grant_permissions": 2,
            "hover": 1,
            "move": 1,
            "new_page": 2,
            "reload": 1,
            "scroll_into_view_if_needed": 3,
            "select_option": 6,
            "send": 3,
            "set_files": 2,
            "set_input_files": 1,
            "uncheck": 2,
            "wheel": 5,
        }
    ),
    "skyvern/webeye/actions/handler_utils.py": Counter(
        {"dispatch_event": 1, "down": 3, "evaluate": 1, "fill": 3, "press": 1, "up": 3}
    ),
    "skyvern/webeye/dialog_handler.py": Counter({"accept": 3, "dismiss": 1}),
    "skyvern/webeye/dom_inspection.py": Counter({"evaluate": 6}),
    "skyvern/webeye/utils/dom.py": Counter(
        {
            "check": 2,
            "click": 3,
            "dblclick": 1,
            "evaluate": 3,
            "fill": 1,
            "focus": 2,
            "goto": 2,
            "hover": 1,
            "press": 1,
            "scroll_into_view_if_needed": 2,
            "uncheck": 2,
        }
    ),
    "skyvern/forge/sdk/event/default.py": Counter(
        {"clear": 1, "click": 2, "move": 2, "scroll_into_view_if_needed": 1, "type": 3, "wheel": 1}
    ),
    "skyvern/forge/sdk/event/factory.py": Counter({"click": 1, "wheel": 1}),
    "skyvern/webeye/real_browser_state.py": Counter({"close": 5, "evaluate": 1, "goto": 1, "new_page": 3, "reload": 2}),
}

_EVALUATE_CALLERS = {
    # Read-only DOM fingerprint sample for the v3 settle-before-complete check, and the per-document
    # nonce the v3 loop reads to tell whether a failed batched call navigated the page.
    "skyvern/forge/agent.py": Counter({"_page_fingerprint": 1, "_page_probe": 1}),
    "skyvern/webeye/actions/handler_utils.py": Counter({"_uses_native_value_set_fill": 1}),
    "skyvern/webeye/actions/handler.py": Counter(
        {
            "_blob_iframe_src_titles": 1,
            "_collect_inline_iframe_src_candidates": 1,
            "_evaluate_element_scoped": 1,
            # grid row-selection snapshot read, post-click settle re-read, and cell hit-test (SKY-13695)
            "_read_grid_row_selection": 1,
            "_grid_row_reached_state": 1,
            "_drive_grid_row_selection": 1,
            # detached-clone constraint check inside _static_declared_constraint_evidence's nested _inner (SKY-13631)
            "_inner": 1,
            "_normal_select_readback_contradicts": 1,
            "_probe_tel_browser_validity": 1,
            "handle_click_action": 2,
            "handle_scroll_action": 4,
        }
    ),
    "skyvern/webeye/dom_inspection.py": Counter(
        {
            "read_current_url": 1,
            # locator-scoped live selected/checked read for the cached click guard (SKY-14051)
            "read_locator_selected_state": 1,
            # locator-scoped live isContentEditable read; routes blocks nested in an editing host to the atomic fill
            "read_locator_is_content_editable": 1,
            "read_locator_tag_name": 1,
            "read_resolved_anchor_href": 1,
            "read_whether_link_or_button": 1,
        }
    ),
    "skyvern/webeye/real_browser_state.py": Counter({"stop_page_loading": 1}),
    # read-only hit-test geometry check gating the custom-select intercept JS fallback
    "skyvern/webeye/utils/dom.py": Counter(
        {"apply_secret_visual_mask": 1, "blur": 1, "_pointer_interceptor_matches_label": 1}
    ),
}


_CDP_SENDS = {
    "skyvern/webeye/actions/handler.py": Counter(
        {
            ("_write_clipboard_text_in_isolated_world", "Page.createIsolatedWorld"): 1,
            ("_write_clipboard_text_in_isolated_world", "Page.getFrameTree"): 1,
            ("_write_clipboard_text_in_isolated_world", "Runtime.callFunctionOn"): 1,
        }
    )
}

_NON_BROWSER_CANDIDATES = Counter(
    {
        ("_drain_and_move_staged_xhr", "move", "shutil"): 1,
        ("_on_response_event", "clear", "self._drained"): 1,
        ("disable", "clear", "self._child_pages_with_bootstrap_allowance"): 1,
        ("disable", "clear", "self._extra_pages"): 1,
        ("disable", "clear", "self._in_flight_requests"): 1,
    }
)


def _owned_source_paths() -> tuple[str, ...]:
    paths = {
        Path("skyvern/forge/agent.py"),
        Path("skyvern/forge/agent_functions.py"),
        Path("skyvern/webeye/dialog_handler.py"),
        Path("skyvern/webeye/dom_inspection.py"),
        Path("skyvern/webeye/real_browser_state.py"),
        Path("skyvern/webeye/utils/dom.py"),
        *Path("skyvern/webeye/actions").glob("*.py"),
        *Path("skyvern/forge/sdk/event").glob("*.py"),
    }
    return tuple(sorted(path.as_posix() for path in paths))


def _candidate_methods(path: str, methods: frozenset[str]) -> Counter[str]:
    tree = ast.parse(Path(path).read_text())
    return Counter(
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in methods
    )


def _candidate_signatures(path: str, methods: frozenset[str]) -> Counter[tuple[str, str, str]]:
    tree = ast.parse(Path(path).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    signatures: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in methods):
            continue
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef)):
                signatures.append((current.name, node.func.attr, ast.unparse(node.func.value)))
                break
    return Counter(signatures)


def _callers_for_method(path: str, method: str) -> Counter[str]:
    tree = ast.parse(Path(path).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    callers: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method):
            continue
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef)):
                callers.append(current.name)
                break
    return Counter(callers)


def _cdp_send_callers(path: str) -> Counter[tuple[str, str]]:
    tree = ast.parse(Path(path).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    callers: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef)):
                callers.append((current.name, node.args[0].value))
                break
    return Counter(callers)


def test_discovered_browser_api_lower_bound_is_stable() -> None:
    observed = {
        path: methods for path in _owned_source_paths() if (methods := _candidate_methods(path, _CANDIDATE_METHODS))
    }

    assert observed == _DISCOVERED_BROWSER_API_CALLS
    assert sum(sum(methods.values()) for methods in observed.values()) == 164
    handler_candidates = _candidate_signatures("skyvern/webeye/actions/handler.py", _CANDIDATE_METHODS)
    classified_non_browser = Counter(
        {signature: count for signature, count in handler_candidates.items() if signature in _NON_BROWSER_CANDIDATES}
    )
    assert classified_non_browser == _NON_BROWSER_CANDIDATES
    assert sum(_NON_BROWSER_CANDIDATES.values()) == 5
    assert sum(sum(methods.values()) for methods in observed.values()) - sum(_NON_BROWSER_CANDIDATES.values()) == 159


def test_every_raw_evaluate_call_is_classified() -> None:
    observed = {path: callers for path in _owned_source_paths() if (callers := _callers_for_method(path, "evaluate"))}

    assert observed == _EVALUATE_CALLERS
    assert sum(sum(callers.values()) for callers in observed.values()) == 28


def test_every_cdp_dispatch_is_classified_by_exact_command() -> None:
    observed = {path: callers for path in _owned_source_paths() if (callers := _cdp_send_callers(path))}

    assert observed == _CDP_SENDS
