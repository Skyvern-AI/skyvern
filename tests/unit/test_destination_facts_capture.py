"""Scrape-time destination-fact capture for the browser action firewall (SKY-12875).

The facts are born in domUtils.buildElementObject and MUST be stripped out of the element dicts at
the JS->Python boundary, before any downstream consumer sees them: element hashes (cached-action
matching), skyvern_element_data (DB rows and the public SDK Action type), the element-tree
artifact, prompt building, and incremental dropdown dedup all depend on the dicts being
byte-identical to a build that never captured facts. The strip is unconditional in both policy
modes, so observe and disabled cannot diverge on this path by construction.
"""

from __future__ import annotations

import ast
import copy
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.scraper.scraper import build_element_dict, hash_element
from skyvern.webeye.utils.page import SkyvernFrame, pop_destination_facts

_REPO_ROOT = Path(__file__).parent.parent.parent
_PAGE_MODULE = _REPO_ROOT / "skyvern" / "webeye" / "utils" / "page.py"
_NODE = shutil.which("node")

ANCHOR_FACTS = {"kind": "anchor", "url": "https://example.com/next"}
FORM_FACTS = {"kind": "form", "url": "https://example.com/submit", "method": "post"}


def element(element_id: str, *, destination: dict | None = None, children: list | None = None) -> dict:
    node: dict = {
        "id": element_id,
        "frame": "main.frame",
        "tagName": "a",
        "attributes": {"href": "/next"},
        "children": children or [],
    }
    if destination is not None:
        node["destination"] = destination
    return node


class TestPopDestinationFacts:
    def test_popping_leaves_the_dict_byte_identical_to_a_factless_build(self) -> None:
        with_facts = element("e1", destination=ANCHOR_FACTS)
        never_had_facts = element("e1")
        facts = pop_destination_facts([with_facts])
        assert with_facts == never_had_facts
        assert hash_element(with_facts) == hash_element(never_had_facts)
        assert facts == {"e1": ANCHOR_FACTS}

    def test_nested_children_are_stripped_and_collected(self) -> None:
        child = element("e2", destination=FORM_FACTS)
        parent = element("e1", destination=ANCHOR_FACTS, children=[child])
        facts = pop_destination_facts([parent])
        assert facts == {"e1": ANCHOR_FACTS, "e2": FORM_FACTS}
        assert "destination" not in parent
        assert "destination" not in child

    def test_a_page_authored_destination_attribute_survives(self) -> None:
        # Gate finding M1: <div destination="shipping"> is ordinary markup, every attribute is
        # collected verbatim, and a key-name-only strip DELETED it — changing hashes, cached
        # matching, prompts and persisted rows on pages with no facts involved at all. DOM
        # attribute values are always strings, so the mapping shape is what identifies ours.
        node = element("e1")
        node["attributes"]["destination"] = "shipping"
        node["attributes"]["data-destination"] = "warehouse"
        untouched = copy.deepcopy(node)

        facts = pop_destination_facts([node])

        assert facts == {}
        assert node == untouched

    def test_a_string_destination_never_seeds_the_sidecar(self) -> None:
        # The same overmatch let a page seed the sidecar with an id of its choosing while capture
        # was DISABLED, just by authoring the attribute on an element carrying an id.
        node = element("e1")
        node["attributes"]["destination"] = "https://attacker.example/x"
        assert pop_destination_facts([node]) == {}

    def test_wrapper_relocated_keys_are_stripped_at_any_depth(self) -> None:
        # Gate finding F4: a hostile wrapper around the writable page-global builder can copy a
        # fact into a NESTED position — an attribute value, a grandchild inside a non-children
        # container — where a children-only walk would miss it and it would flow into hashes,
        # prompts and persisted skyvern_element_data.
        node = element("e1")
        node["attributes"]["nested"] = {"destination": ANCHOR_FACTS}
        node["options"] = [{"optionIndex": 0, "destination": FORM_FACTS}]
        grandchild = {"tagName": "i", "destination": ANCHOR_FACTS}
        node["children"].append({"tagName": "span", "wrapped": [grandchild]})
        pop_destination_facts([node])

        def leaked(payload: object) -> bool:
            stack = [payload]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    if "destination" in current:
                        return True
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)
            return False

        assert not leaked(node)

    def test_a_factless_element_contributes_nothing(self) -> None:
        node = element("e1")
        assert pop_destination_facts([node]) == {}

    def test_a_cyclic_payload_terminates(self) -> None:
        # Playwright's value protocol reconstructs shared refs, so a tampered page can hand back a
        # cyclic tree. The strip must terminate on it rather than hang the worker.
        node = element("e1", destination=ANCHOR_FACTS)
        node["children"].append(node)
        facts = pop_destination_facts([node])
        assert facts == {"e1": ANCHOR_FACTS}

    def test_hostile_shapes_do_not_raise(self) -> None:
        # The payload is page-controlled through the JS it runs; the strip must survive anything.
        nodes = [
            {"destination": ANCHOR_FACTS},  # no id at all
            {"id": 7, "destination": ANCHOR_FACTS},  # non-string id
            {"id": "", "destination": ANCHOR_FACTS},  # empty id
            {"id": "ok", "destination": ANCHOR_FACTS, "children": "not-a-list"},
            "not-a-dict",
            None,
        ]
        facts = pop_destination_facts(nodes)  # type: ignore[arg-type]
        assert facts == {"ok": ANCHOR_FACTS}
        assert all("destination" not in node for node in nodes if isinstance(node, dict))

    def test_build_element_dict_receives_clean_dicts_only(self) -> None:
        # The maps that feed skyvern_element_data and cached-action hashes are built downstream of
        # the strip; this pins that a stripped element hashes identically wherever it lands next.
        stripped = element("e1", destination=ANCHOR_FACTS)
        pop_destination_facts([stripped])
        _, id_to_element, _, id_to_hash, _ = build_element_dict([stripped])
        assert "destination" not in id_to_element["e1"]
        assert id_to_hash["e1"] == hash_element(element("e1"))


def _frame_with_fake_evaluate(payload: object) -> SkyvernFrame:
    frame = SkyvernFrame(MagicMock())
    frame._set_enriched_element_tree_flag = AsyncMock()  # type: ignore[method-assign]

    async def fake_evaluate(**_kwargs: object) -> object:
        return copy.deepcopy(payload)

    frame.evaluate = fake_evaluate  # type: ignore[method-assign]
    return frame


class TestEntryPointsStrip:
    """Every SkyvernFrame method that returns domUtils element objects strips the facts. The
    accepted-scrape entry point returns them as a sidecar; the rest discard them."""

    @pytest.mark.asyncio
    async def test_build_tree_from_body_strips_and_returns_the_sidecar(self) -> None:
        payload = [[element("e1", destination=ANCHOR_FACTS)], [element("e1", destination=ANCHOR_FACTS)]]
        frame = _frame_with_fake_evaluate(payload)
        elements, element_tree, destinations = await frame.build_tree_from_body(frame_name="main.frame", frame_index=0)
        assert destinations == {"e1": ANCHOR_FACTS}
        assert all("destination" not in node for node in elements)
        assert all("destination" not in node for node in element_tree)

    @pytest.mark.asyncio
    async def test_incremental_tree_is_stripped(self) -> None:
        payload = [[element("e1", destination=FORM_FACTS)], [element("e1", destination=FORM_FACTS)]]
        frame = _frame_with_fake_evaluate(payload)
        elements, element_tree = await frame.get_incremental_element_tree()
        assert all("destination" not in node for node in elements)
        assert all("destination" not in node for node in element_tree)

    @pytest.mark.asyncio
    async def test_tree_from_element_is_stripped(self) -> None:
        payload = [[element("e1", destination=ANCHOR_FACTS)], [element("e1", destination=ANCHOR_FACTS)]]
        frame = _frame_with_fake_evaluate(payload)
        elements, element_tree = await frame.build_tree_from_element(starter=MagicMock(), frame="main.frame")
        assert all("destination" not in node for node in elements)
        assert all("destination" not in node for node in element_tree)

    @pytest.mark.asyncio
    async def test_parse_element_from_html_is_stripped(self) -> None:
        frame = _frame_with_fake_evaluate(element("e1", destination=ANCHOR_FACTS))
        parsed = await frame.parse_element_from_html("main.frame", MagicMock(), True)
        assert "destination" not in parsed

    def test_the_element_producing_surface_is_exactly_the_four_stripped_methods(self) -> None:
        """The complete JS->Python surface for element objects, pinned BY QUALIFIED IDENTITY: the
        methods on the SkyvernFrame class, not any def sharing a name elsewhere. A new entry point
        fails here until its author strips the facts at the boundary and adds it to this set.

        Known non-producer with a colliding name: IncrementalScrapePage.get_incremental_element_tree
        (skyvern/webeye/scraper/scraper.py) is a WRAPPER over the SkyvernFrame method of the same
        name — it consumes the already-stripped result and evaluates no domUtils builder itself, so
        it is correctly not a boundary and correctly not in this set.
        """
        tree = ast.parse(_PAGE_MODULE.read_text())
        skyvern_frame = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SkyvernFrame"
        )
        producers: dict[str, str] = {}
        for node in skyvern_frame.body:
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                rendered = ast.unparse(node)
                if any(
                    f"{producer}(" in rendered
                    for producer in (
                        "buildTreeFromBody",
                        "getIncrementElements",
                        "buildElementTree",
                        "buildElementObject",
                    )
                ):
                    producers[node.name] = rendered
        assert set(producers) == {
            "build_tree_from_body",
            "get_incremental_element_tree",
            "build_tree_from_element",
            "parse_element_from_html",
        }, "the element-producing JS surface changed — strip the new entry point and update this set"
        for name, rendered in producers.items():
            assert "pop_destination_facts" in rendered, f"{name} returns element dicts without stripping facts"
        # The scan itself must keep matching: an evaluate refactor that hides the JS names from this
        # test would silently retire the pin rather than fail it.
        assert len(producers) == 4


class TestCaptureIsFlagGated:
    """The disabled-mode no-op invariant (rework of gate finding F1): with the policy disabled,
    the build asks JS for ZERO destination-fact work — no attribute reads, no URL resolution, no
    allocation. Capture cost is mode-gated while the strip stays unconditional (protection against
    wrapper injection, not capture cost): two different questions, two different mechanisms."""

    @staticmethod
    async def _flag_passed_to_js(mode: str, monkeypatch: pytest.MonkeyPatch) -> object:
        from skyvern.config import settings

        monkeypatch.setattr(settings, "BROWSER_ACTION_POLICY_MODE", mode)
        captured: dict = {}
        frame = SkyvernFrame(MagicMock())
        frame._set_enriched_element_tree_flag = AsyncMock()  # type: ignore[method-assign]

        async def fake_evaluate(**kwargs: object) -> object:
            captured.update(kwargs)
            return [[], []]

        frame.evaluate = fake_evaluate  # type: ignore[method-assign]
        await frame.build_tree_from_body(frame_name="main.frame", frame_index=0)
        arg = captured["arg"]
        assert isinstance(arg, list) and len(arg) == 4, "the capture flag no longer reaches JS"
        return arg[3]

    @pytest.mark.asyncio
    async def test_disabled_mode_asks_js_for_no_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._flag_passed_to_js("disabled", monkeypatch) is False

    @pytest.mark.asyncio
    async def test_observe_mode_asks_js_to_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The twin: without it, a pass-through hardcoded to False satisfies the test above while
        # silently disabling the feature.
        assert await self._flag_passed_to_js("observe", monkeypatch) is True


class TestScrapeAssemblyCarriesTheSidecar:
    @pytest.mark.asyncio
    async def test_frame_facts_are_merged_across_frames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.forge.sdk.core import skyvern_context
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
        from skyvern.webeye.scraper import scraper as scraper_module

        main_frame = _frame_with_fake_evaluate(
            [[element("e1", destination=ANCHOR_FACTS)], [element("e1", destination=ANCHOR_FACTS)]]
        )

        async def fake_create_instance(_frame: object, engine_selection: object = None) -> SkyvernFrame:
            return main_frame

        monkeypatch.setattr(SkyvernFrame, "create_instance", fake_create_instance)
        monkeypatch.setattr(scraper_module, "get_all_children_frames", AsyncMock(return_value=[]))
        skyvern_context.set(SkyvernContext(request_id="req_12875"))
        try:
            elements, _tree, destinations = await scraper_module.get_interactable_element_tree(MagicMock())
        finally:
            skyvern_context.reset()
        assert destinations == {"e1": ANCHOR_FACTS}
        assert all("destination" not in node for node in elements)


@pytest.mark.skipif(_NODE is None, reason="node not on PATH")
class TestDestinationFactsJs:
    def test_behavioral(self) -> None:
        script = Path(__file__).parent / "test_domutils_destination_facts.js"
        assert script.exists(), f"Missing {script}"
        result = subprocess.run(
            [_NODE, str(script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"Failed:\n{result.stdout}\n{result.stderr}"
