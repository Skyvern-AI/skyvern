import pytest
import yaml

from skyvern.forge.sdk.copilot.code_block_steps import (
    analyze_code_actions,
    apply_derived_code_block_steps,
    derive_code_block_steps,
    derive_code_block_steps_in_yaml,
    fill_code_block_prompts_in_yaml,
)
from skyvern.webeye.actions.action_types import ActionType


def test_analyze_maps_playwright_calls_to_action_types_with_line_ranges():
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    await page.wait_for_load_state('load')\n"
        "    await page.get_by_role('link', name='Login').click()\n"
        "    await page.get_by_label('Username').fill(str(username))\n"
        "    await page.get_by_label('Country').select_option('US')\n"
        "    await page.keyboard.press('Enter')\n"
    )
    spans = analyze_code_actions(code)
    assert [(s.action_type, s.line_start) for s in spans] == [
        ("goto_url", 2),
        ("click", 4),
        ("input_text", 5),
        ("select_option", 6),
        ("keypress", 7),
    ]


def test_analyze_maps_page_evaluate_and_other_recorder_calls_to_action_types():
    # The static editor preview must surface the same calls the runtime recorder
    # records (code_block_recorder._PAGE_ACTION_MAP / _LOCATOR_ACTION_MAP), so the
    # editor step count matches the timeline. page.evaluate was previously dropped.
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    await page.evaluate('() => document.title')\n"
        "    await page.get_by_role('link', name='Docs').hover()\n"
        "    await page.go_forward()\n"
    )
    spans = analyze_code_actions(code)
    assert [(s.action_type, s.line_start) for s in spans] == [
        ("goto_url", 2),
        ("execute_js", 3),
        ("hover", 4),
        ("go_forward", 5),
    ]


def test_derive_steps_surfaces_page_evaluate_with_a_label():
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    await page.evaluate('() => document.title')\n"
    )
    steps = derive_code_block_steps(code)
    # Step count must match the number of actions actually in the script (2, not 1).
    assert [s["action_type"] for s in steps] == ["goto_url", "execute_js"]
    assert steps[1]["description"]  # surfaced with a non-empty, human label, not dropped


def test_analyze_skips_noise_and_returns_empty_on_syntax_error():
    # wait_for_load_state is paired sync noise, never its own step.
    assert analyze_code_actions("async def run(page):\n    await page.wait_for_load_state('load')\n") == []
    assert analyze_code_actions("def broken(:\n") == []


def test_derive_steps_returns_dicts_with_templated_descriptions():
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    await page.get_by_role('button', name='Submit').click()\n"
        "    await page.get_by_label('Email').fill(str(email))\n"
    )
    steps = derive_code_block_steps(code)
    assert steps == [
        {"description": "Open https://example.com/", "action_type": "goto_url", "line_start": 2, "line_end": 2},
        {"description": 'Click "Submit"', "action_type": "click", "line_start": 3, "line_end": 3},
        {"description": 'Type into "Email"', "action_type": "input_text", "line_start": 4, "line_end": 4},
    ]


def test_derive_steps_empty_code_is_empty():
    assert derive_code_block_steps("") == []
    assert derive_code_block_steps("x = 1\n") == []


@pytest.mark.asyncio
async def test_apply_sets_steps_on_code_blocks_and_leaves_others_untouched():
    src = {
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "code",
                    "label": "block_1",
                    "code": "async def run(page):\n    await page.goto('https://x.com/')\n",
                },
                {"block_type": "task", "label": "t1", "url": "https://x.com"},
                {
                    "block_type": "for_loop",
                    "label": "loop",
                    "loop_blocks": [
                        {
                            "block_type": "code",
                            "label": "inner",
                            "code": "async def run(page):\n    await page.get_by_role('button', name='Go').click()\n",
                        },
                    ],
                },
            ]
        }
    }
    out = yaml.safe_load(await apply_derived_code_block_steps(yaml.safe_dump(src)))
    blocks = out["workflow_definition"]["blocks"]
    assert blocks[0]["steps"] == [
        {"description": "Open https://x.com/", "action_type": "goto_url", "line_start": 2, "line_end": 2}
    ]
    assert "steps" not in blocks[1]  # non-code block untouched
    assert blocks[2]["loop_blocks"][0]["steps"][0]["action_type"] == "click"  # nested code block annotated


@pytest.mark.asyncio
async def test_apply_is_noop_on_unparseable_yaml():
    assert await apply_derived_code_block_steps("::not yaml::") == "::not yaml::"


def test_derive_in_yaml_fills_steps_when_absent():
    src = (
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: block_2\n"
        "    code: |\n"
        "      await page.goto('https://x.com/')\n"
        "      await page.get_by_role('link', name='login').click()\n"
    )
    out = yaml.safe_load(derive_code_block_steps_in_yaml(src))
    steps = out["workflow_definition"]["blocks"][0]["steps"]
    assert [s["action_type"] for s in steps] == ["goto_url", "click"]


def test_derive_in_yaml_preserves_existing_steps():
    # An LLM-refined steps list must survive untouched; deterministic derivation
    # is a fallback only when steps are absent.
    refined = [{"description": "Open the homepage", "action_type": "goto_url", "line_start": 1, "line_end": 1}]
    src = {
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "code",
                    "label": "block_1",
                    "code": "await page.goto('https://x.com/')\n",
                    "steps": refined,
                }
            ]
        }
    }
    out = yaml.safe_load(derive_code_block_steps_in_yaml(yaml.safe_dump(src)))
    assert out["workflow_definition"]["blocks"][0]["steps"] == refined


def test_derive_in_yaml_noop_on_unparseable():
    assert derive_code_block_steps_in_yaml("::not yaml::") == "::not yaml::"


def test_fill_prompts_preserves_prior_block_prompt_across_regen():
    # Regenerating a code block replaces the whole block YAML, dropping the goal.
    # Without the prompt the editor renders the legacy code-only layout, so the
    # block's prior prompt must be carried forward (exact user text).
    prior = (
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: block_1\n"
        "    prompt: Build an agent to find the top post on the site\n"
        "    code: 'x = 1'\n"
    )
    regenerated = (
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: block_1\n"
        "    code: |\n"
        "      await page.goto('https://example.com/')\n"
    )
    out = yaml.safe_load(fill_code_block_prompts_in_yaml(regenerated, prior_yaml=prior))
    assert out["workflow_definition"]["blocks"][0]["prompt"] == "Build an agent to find the top post on the site"


def test_fill_prompts_falls_back_to_declared_goal_for_new_block():
    new = "workflow_definition:\n  blocks:\n  - block_type: code\n    label: block_2\n    code: 'x = 1'\n"
    out = yaml.safe_load(
        fill_code_block_prompts_in_yaml(new, prior_yaml=None, fallback_goals={"block_2": "Search the catalog"})
    )
    assert out["workflow_definition"]["blocks"][0]["prompt"] == "Search the catalog"


def test_fill_prompts_prefers_prior_over_fallback_and_preserves_existing():
    prior = "workflow_definition:\n  blocks:\n  - block_type: code\n    label: b\n    prompt: Exact user text\n    code: 'x=1'\n"
    new = (
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: b\n"
        "    code: 'y=2'\n"
        "  - block_type: code\n"
        "    label: c\n"
        "    prompt: Already set\n"
        "    code: 'z=3'\n"
    )
    out = yaml.safe_load(
        fill_code_block_prompts_in_yaml(new, prior_yaml=prior, fallback_goals={"b": "model goal", "c": "ignored"})
    )
    blocks = out["workflow_definition"]["blocks"]
    assert blocks[0]["prompt"] == "Exact user text"  # prior beats fallback
    assert blocks[1]["prompt"] == "Already set"  # existing prompt untouched


def test_fill_prompts_noop_without_sources():
    new = "workflow_definition:\n  blocks:\n  - block_type: code\n    label: b\n    code: 'x=1'\n"
    out = yaml.safe_load(fill_code_block_prompts_in_yaml(new))
    assert "prompt" not in out["workflow_definition"]["blocks"][0]


def test_process_workflow_yaml_derives_code_block_steps_for_replace_path():
    # Regression: the inline REPLACE_WORKFLOW path (v1 and v2) builds the
    # frontend-facing workflow via _process_workflow_yaml without first deriving
    # steps, so a generated code block surfaced as "No steps yet" in the plain
    # editor view while the update_workflow tool path showed them.
    from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml

    yaml_str = (
        "title: HN Login\n"
        "workflow_definition:\n"
        "  parameters: []\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: block_2\n"
        "    prompt: Go to the site and log in\n"
        "    code: |\n"
        "      await page.goto('https://example.com/')\n"
        "      await page.get_by_role('link', name='login').click()\n"
    )
    wf = _process_workflow_yaml(
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        workflow_yaml=yaml_str,
    )
    block = wf.workflow_definition.blocks[0]
    assert block.steps is not None
    assert [s.action_type for s in block.steps] == [ActionType.GOTO_URL, ActionType.CLICK]


@pytest.mark.asyncio
async def test_apply_derived_steps_on_copilot_yaml_shape():
    # Mirrors the _copilot_yaml payload that apply-proposed-workflow reads from
    # the stashed proposal. Steps must be populated so manual-accept persists them.
    copilot_yaml = (
        "title: Search\n"
        "workflow_definition:\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: do_search\n"
        "    code: |\n"
        "      await page.goto('https://example.com/')\n"
        "      await page.get_by_label('Query').fill(str(query))\n"
    )
    enriched = yaml.safe_load(await apply_derived_code_block_steps(copilot_yaml))
    steps = enriched["workflow_definition"]["blocks"][0]["steps"]
    assert len(steps) == 2
    assert steps[0]["action_type"] == "goto_url"
    assert steps[1]["action_type"] == "input_text"


def test_multiline_call_span_covers_all_lines():
    code = "async def run(page):\n    await page.get_by_label('Email').fill(\n        str(email)\n    )\n"
    spans = analyze_code_actions(code)
    assert spans[0].action_type == "input_text"
    assert spans[0].line_start == 2 and spans[0].line_end == 4


def test_get_by_role_without_name_falls_back_to_the_element():
    code = "async def run(page):\n    await page.get_by_role('button').click()\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["description"] == "Click the element"


def test_extract_call_surfaces_as_a_distinct_step_with_prompt_copy():
    # page.extract() is the code-first extraction surface and must surface as its own step.
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    data = await page.extract(prompt='Extract the URLs of the top 20 posts', schema={'type': 'object'})\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["goto_url", "extract"]
    assert steps[1]["description"] == "Extract the URLs of the top 20 posts"


def test_multiline_prompt_literal_renders_without_backslash_escapes():
    # A YAML block-scalar / multiline prompt must collapse to readable copy, not leak
    # the source-level "\n" escape that ast.unparse would re-introduce.
    code = "async def run(page):\n    await page.extract(prompt='Extract the URLs\\nof the top 20 posts')\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["action_type"] == "extract"
    assert steps[0]["description"] == "Extract the URLs of the top 20 posts"


def test_extract_without_prompt_literal_falls_back_to_a_generic_extract_label():
    code = "async def run(page):\n    data = await page.extract(prompt=goal)\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["action_type"] == "extract"
    assert steps[0]["description"] == "Extract information from the page"


def test_whitespace_only_prompt_falls_back_instead_of_rendering_blank_copy():
    code = "async def run(page):\n    data = await page.extract(prompt='   \\n  ')\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["action_type"] == "extract"
    assert steps[0]["description"] == "Extract information from the page"


def test_check_and_uncheck_map_to_checkbox_matching_the_recorder():
    code = (
        "async def run(page):\n"
        "    await page.get_by_label('Remember me').check()\n"
        "    await page.get_by_label('Subscribe').uncheck()\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["checkbox", "checkbox"]
    assert steps[0]["description"] == 'Toggle "Remember me"'


def test_extraction_then_looped_navigation_are_distinguishable():
    # The canonical failing case: open a site, extract a list of links, then for
    # each link open it and extract its contents. Every step must read as the
    # action it performs, and the two navigations must not be the same generic copy.
    code = (
        "async def run(page, limit):\n"
        "    await page.goto('https://example.com/')\n"
        "    posts = await page.extract(prompt='Extract the URLs of the top posts')\n"
        "    for post in posts['posts'][:limit]:\n"
        "        await page.goto(post['url'])\n"
        "        await page.extract(prompt='Extract the top comment on the post')\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["goto_url", "extract", "goto_url", "extract"]
    descriptions = [s["description"] for s in steps]
    assert descriptions == [
        "Open https://example.com/",
        "Extract the URLs of the top posts",
        "Open each post",
        "Extract the top comment on the post",
    ]
    # The follow-up navigation must be distinguishable from the first, not a repeated label.
    assert descriptions[0] != descriptions[2]


def test_goto_with_non_literal_url_outside_a_loop_describes_a_linked_page():
    code = "async def run(page, target_url):\n    await page.goto(target_url)\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["action_type"] == "goto_url"
    assert steps[0]["description"] == "Open the linked page"


def test_prompt_kwarg_is_preferred_as_step_copy_for_interactions():
    code = "async def run(page):\n    await page.click('#login', prompt='Click the login button')\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["action_type"] == "click"
    assert steps[0]["description"] == "Click the login button"


def test_skyvern_page_high_level_actions_surface_as_steps():
    # The durable copilot surface is the @action_wrap-decorated SkyvernPage API,
    # not only raw Playwright calls; these must each render as their own step.
    code = (
        "async def run(page, doc):\n"
        "    await page.select_option('#country', prompt='Choose the country')\n"
        "    await page.upload_file('#file', files=str(doc))\n"
        "    await page.complete(prompt='Confirm the form was submitted')\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["select_option", "upload_file", "complete"]
    assert steps[0]["description"] == "Choose the country"
    assert steps[2]["description"] == "Confirm the form was submitted"
