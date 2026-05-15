import pytest

from skyvern.core.script_generations.script_block_extractor import (
    RunSignatureValidationError,
    ScriptBlockExtractionError,
    extract_script_blocks,
)


def test_extracts_try_else_block() -> None:
    source = """
import skyvern

@skyvern.workflow(title="t")
async def run(parameters):
    page, context = await skyvern.setup(parameters, dict)
    try:
        await page.wait_for_load_state()
    except TimeoutError:
        pass
    else:
        await skyvern.run_task(prompt="...", label="step_a", cache_key="step_a")
"""
    workflow_definition = {"blocks": [{"label": "step_a", "block_type": "navigation"}]}

    result = extract_script_blocks(source, workflow_definition)

    assert [block.label for block in result.blocks] == ["step_a"]
    assert result.blocks[0].is_cacheable is True
    assert result.blocks[0].run_signature.startswith("await skyvern.run_task")


def test_extracts_nested_loop_child_block_type() -> None:
    source = """
import skyvern

async def run(parameters):
    page, context = await skyvern.setup(parameters, dict)
    async for current_value in skyvern.loop(label="outer", cache_key="outer", loop_over="items"):
        await skyvern.run_task(prompt="...", label="inner_a", cache_key="inner_a")
"""
    workflow_definition = {
        "blocks": [
            {
                "label": "outer",
                "block_type": "for_loop",
                "loop_blocks": [{"label": "inner_a", "block_type": "navigation"}],
            }
        ]
    }

    result = extract_script_blocks(source, workflow_definition)

    assert [block.label for block in result.blocks] == ["outer", "inner_a"]
    outer = result.blocks[0]
    inner = result.blocks[1]
    assert outer.is_compound is True
    assert outer.is_cacheable is True
    assert outer.run_signature.startswith("async for ")
    assert inner.block_type == "navigation"
    assert inner.is_cacheable is True


def test_missing_global_is_reported_for_signature() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(url=LOGIN_URL, prompt="...", label="login", cache_key="login")
"""
    workflow_definition = {"blocks": [{"label": "login", "block_type": "login"}]}

    result = extract_script_blocks(source, workflow_definition)

    assert result.blocks[0].missing_globals == ("LOGIN_URL",)


def test_defined_global_is_allowed_for_signature() -> None:
    source = """
import skyvern

LOGIN_URL = "https://example.com"

async def run(parameters):
    await skyvern.run_task(url=LOGIN_URL, prompt="...", label="login", cache_key="login")
"""
    workflow_definition = {"blocks": [{"label": "login", "block_type": "login"}]}

    result = extract_script_blocks(source, workflow_definition)

    assert result.blocks[0].missing_globals == ()


def test_entry_function_parameters_and_locals_are_allowed_for_signature() -> None:
    source = """
import skyvern

async def run(parameters):
    page, context = await skyvern.setup(parameters, dict)
    await skyvern.run_task(
        url=parameters["url"],
        prompt=context.prompt,
        label="login",
        cache_key="login",
    )
"""
    workflow_definition = {"blocks": [{"label": "login", "block_type": "login"}]}

    result = extract_script_blocks(source, workflow_definition)

    assert result.blocks[0].missing_globals == ()


def test_wildcard_imports_are_rejected() -> None:
    source = """
from constants import *
import skyvern

async def run(parameters):
    await skyvern.run_task(url=LOGIN_URL, prompt="...", label="login", cache_key="login")
"""
    workflow_definition = {"blocks": [{"label": "login", "block_type": "login"}]}

    with pytest.raises(RunSignatureValidationError, match="Wildcard imports"):
        extract_script_blocks(source, workflow_definition)


def test_requires_entry_function() -> None:
    with pytest.raises(ScriptBlockExtractionError, match="Could not find"):
        extract_script_blocks("import skyvern\n", {"blocks": []})


def test_known_non_cacheable_block_is_extracted_but_not_cacheable() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.validate(complete_criterion="done", terminate_criterion="stop", label="check")
"""
    workflow_definition = {"blocks": [{"label": "check", "block_type": "validation"}]}

    result = extract_script_blocks(source, workflow_definition)

    assert result.blocks[0].label == "check"
    assert result.blocks[0].is_cacheable is False


def test_unknown_block_type_warns_and_is_not_cacheable() -> None:
    source = """
import skyvern

async def run(parameters):
    await skyvern.run_task(prompt="...", label="future", cache_key="future")
"""
    workflow_definition = {"blocks": [{"label": "future", "block_type": "future_block"}]}

    result = extract_script_blocks(source, workflow_definition)

    assert result.blocks[0].is_cacheable is False
    assert result.warnings == (
        "Unknown workflow block type 'future_block' for label 'future'; treating as non-cacheable",
    )
