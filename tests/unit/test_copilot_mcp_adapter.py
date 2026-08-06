import inspect

from skyvern.forge.sdk.copilot.mcp_adapter import _requested_output_path_choices


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "output_path": {"type": "string", "description": "The requested output this read fills."},
        },
        "required": ["expression"],
    }


class TestRequestedOutputPathChoices:
    def test_the_turn_s_requested_paths_become_the_choices(self) -> None:
        # Live shape (SKY-13226): a free-form path let the model name its own purpose, so the read
        # that saw the requested quantity was filed as exploration and never witnessed the path.
        schema = _requested_output_path_choices(_schema(), ["output.azure_error_count"])

        assert schema["properties"]["output_path"]["enum"] == ["output.azure_error_count"]
        assert "output.azure_error_count" in schema["properties"]["output_path"]["description"]

    def test_exploration_still_passes_by_omitting_the_path(self) -> None:
        schema = _requested_output_path_choices(_schema(), ["output.azure_error_count"])

        assert "output_path" not in schema["required"]

    def test_every_requested_path_stays_available_for_a_reread(self) -> None:
        schema = _requested_output_path_choices(_schema(), ["output.a", "output.b"])

        assert schema["properties"]["output_path"]["enum"] == ["output.a", "output.b"]

    def test_a_turn_owing_no_output_leaves_the_schema_alone(self) -> None:
        assert _requested_output_path_choices(_schema(), []) == _schema()


class TestClaimedOutputWithoutValue:
    def test_a_declared_read_that_gathered_candidates_is_told_what_to_do_next(self) -> None:
        # Live shape (SKY-13226): the read named the requested path and returned a filtered list of
        # every matching line on the page, so no single value was recorded. The bare fact was emitted
        # six times in one turn and went unacted on; the sibling signal carrying a hint was followed.
        from skyvern.forge.sdk.copilot.tools import mcp_hooks

        source = inspect.getsource(mcp_hooks)
        marker = source.index('data["claimed_output_without_a_single_value"]')
        following = source[marker : marker + 700]

        assert "claimed_output_read_hint" in following
        assert "output_path=" in following


def test_the_declared_path_says_the_expression_is_that_value() -> None:
    from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
    from skyvern.forge.sdk.copilot.tools.mcp_hooks import _build_skyvern_mcp_overlays

    overlays = _build_skyvern_mcp_overlays(BlockAuthoringPolicy.CODE_ONLY_BROWSER)
    description = overlays["evaluate"].copilot_params["output_path"]["description"]

    assert "evaluates to that one value" in description
    assert "exploration" in description
