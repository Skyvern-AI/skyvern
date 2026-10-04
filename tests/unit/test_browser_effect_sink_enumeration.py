import ast
import re
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
    "skyvern/forge/agent.py": Counter({"close": 1, "evaluate": 4, "new_page": 1}),
    "skyvern/forge/agent_functions.py": Counter({"close": 1, "scroll_into_view_if_needed": 1}),
    "skyvern/webeye/actions/handler.py": Counter(
        {
            "bring_to_front": 2,
            "check": 2,
            "click": 25,
            "clear": 6,
            # 6 = 5 prior + the bounded v4 FileDownloadBlock synchronous popup close (SKY-15371).
            "close": 6,
            "dblclick": 2,
            "evaluate": 15,
            "fill": 2,
            "focus": 4,
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
            "type": 1,
            "uncheck": 2,
            "wheel": 5,
        }
    ),
    "skyvern/webeye/actions/multi_field_totp.py": Counter({"fill": 1, "click": 2, "press": 1, "evaluate": 5}),
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
            "evaluate": 5,
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
    "skyvern/forge/taskv3/input_dispatch.py": Counter(
        {
            "click": 4,
            "evaluate": 1,
            "fill": 3,
            "focus": 2,
            "hover": 1,
            "press": 3,
            "press_sequentially": 1,
            "select_option": 2,
            "set_files": 1,
            "set_input_files": 1,
            "type": 2,
            "wheel": 1,
        }
    ),
    "skyvern/webeye/real_browser_state.py": Counter({"close": 6, "evaluate": 1, "goto": 1, "new_page": 3, "reload": 2}),
}

_EVALUATE_CALLERS = {
    # Read-only DOM fingerprint sample for the v3 settle-before-complete check, and the per-document
    # nonce the v3 loop reads to tell whether a failed batched call navigated the page.
    # _page_fingerprint samples TWICE: the page's own document, and each readable child frame. Both
    # are the same read-only probe; the second exists because the settle check is a live gate and a
    # main-frame-only sample reads a page whose child frame is still rendering as settled (SKY-14657).
    # _document_identity reads the same nonce in the main document and each acted-in child frame, via
    # the shared _realm_part helper (one evaluate call site, invoked once per realm) that also reads
    # each realm's browser-owned _realm_document_id (SKY-17372).
    "skyvern/forge/agent.py": Counter({"_page_fingerprint": 2, "_page_probe": 1, "_realm_part": 1}),
    "skyvern/webeye/actions/multi_field_totp.py": Counter(
        {
            "_multi_field_totp_frame_gone": 1,
            "_multi_field_totp_widget_snapshot": 1,
            "bind": 1,
            "refresh": 1,
            "find_multi_field_totp_submit_controls": 1,
        }
    ),
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
    # The hidden-native-control click V3 fires in-page.
    "skyvern/forge/taskv3/input_dispatch.py": Counter({"js_click": 1}),
    # OTP box/scope/document marker writes, visual masks and read-only hit-test geometry.
    "skyvern/webeye/utils/dom.py": Counter(
        {"apply_secret_visual_mask": 1, "mark_totp_box": 2, "blur": 1, "_pointer_interceptor_matches_label": 1}
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
        ("disable", "clear", "self._status_observation_requests"): 1,
        ("disable", "clear", "self._status_observation_child_pages"): 1,
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
        Path("skyvern/forge/taskv3/input_dispatch.py"),
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
    assert sum(sum(methods.values()) for methods in observed.values()) == 206
    handler_candidates = _candidate_signatures("skyvern/webeye/actions/handler.py", _CANDIDATE_METHODS)
    classified_non_browser = Counter(
        {signature: count for signature, count in handler_candidates.items() if signature in _NON_BROWSER_CANDIDATES}
    )
    assert classified_non_browser == _NON_BROWSER_CANDIDATES
    assert sum(_NON_BROWSER_CANDIDATES.values()) == 7
    assert sum(sum(methods.values()) for methods in observed.values()) - sum(_NON_BROWSER_CANDIDATES.values()) == 199


def test_every_raw_evaluate_call_is_classified() -> None:
    observed = {path: callers for path in _owned_source_paths() if (callers := _callers_for_method(path, "evaluate"))}

    assert observed == _EVALUATE_CALLERS
    assert sum(sum(callers.values()) for callers in observed.values()) == 38


def test_every_cdp_dispatch_is_classified_by_exact_command() -> None:
    observed = {path: callers for path in _owned_source_paths() if (callers := _cdp_send_callers(path))}

    assert observed == _CDP_SENDS


# Task V3 sends input only through input_dispatch.
_INPUT_DISPATCH = Path("skyvern/forge/taskv3/input_dispatch.py")
_TASKV3_BANNED_INPUT_METHODS = frozenset(
    """check click dblclick dispatch_event drag_and_drop drag_to fill focus hover insert_text press press_sequentially select_option
    select_text set_checked set_files set_input_files tap type uncheck""".split()
)
_TASKV3_BANNED_DEVICES = frozenset({"mouse", "keyboard"})
_IN_PAGE_INPUT_JS = re.compile(r"\.click\(\)|dispatchEvent\(|new (Mouse|Keyboard|Pointer)Event|Input\.dispatch")
# `clear` is also a list and dict method, so it is banned only on a receiver shaped like an element.
_ELEMENT_SOURCES = frozenset({"locator", "query_selector", "element_handle", "nth", "first", "last"})


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef)):
            return current.name
    return "<module>"


def _is_element(node: ast.expr, element_names: set[str]) -> bool:
    if isinstance(node, ast.Await):
        node = node.value
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id in element_names
    return isinstance(node, ast.Attribute) and (node.attr in _ELEMENT_SOURCES or node.attr.startswith("get_by_"))


def _direct_input_sites(path: Path, only_function: str | None = None) -> list[str]:
    tree = ast.parse(path.read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    element_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_element(node.value, set())
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    sites: list[str] = []
    for node in ast.walk(tree):
        found: str | None = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            dispatched = isinstance(receiver, ast.Name) and receiver.id == "input_dispatch"
            if node.func.attr in _TASKV3_BANNED_INPUT_METHODS and not dispatched:
                found = f"{node.func.attr} on {ast.unparse(node.func.value)}"
            elif node.func.attr == "clear" and _is_element(receiver, element_names):
                found = f"clear on {ast.unparse(receiver)}"
        elif isinstance(node, ast.Attribute) and node.attr in _TASKV3_BANNED_DEVICES:
            found = f".{node.attr} on {ast.unparse(node.value)}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _TASKV3_BANNED_INPUT_METHODS | _TASKV3_BANNED_DEVICES
        ):
            found = f"getattr {node.args[1].value!r}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _IN_PAGE_INPUT_JS.search(node.value):
            found = "in-page input script"
        if found is None:
            continue
        function = _enclosing_function(parents, node)
        if only_function is not None and function != only_function:
            continue
        sites.append(f"{path}:{getattr(node, 'lineno', '?')} {function} {found}")
    return sites


def test_taskv3_input_flows_only_through_input_dispatch() -> None:
    sites = [
        site
        for path in sorted(Path("skyvern/forge/taskv3").rglob("*.py"))
        if path != _INPUT_DISPATCH
        for site in _direct_input_sites(path)
    ]
    # The captcha ladder lives outside taskv3 and V3 hands it the click to use.
    sites += _direct_input_sites(Path("skyvern/webeye/utils/captcha_solver.py"), "_solve_challenge_ladder_impl")

    assert sites == []
