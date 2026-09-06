import textwrap

import pytest
import yaml

from skyvern.forge.sdk.copilot.code_block_steps import (
    analyze_code_actions,
    apply_derived_code_block_steps,
    bind_referenced_parameters_in_yaml,
    derive_code_block_steps,
    derive_code_block_steps_in_yaml,
    fill_code_block_prompts_in_yaml,
)
from skyvern.forge.sdk.copilot.code_block_synthesis import synthesize_code_block
from skyvern.webeye.actions.action_types import ActionType

pytestmark = pytest.mark.usefixtures("no_saved_workflow")


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


def test_synthesized_and_derived_steps_share_exact_field_set():
    trajectory = [
        {
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.com/catalog",
            "typed_value": "widget",
            "role": "textbox",
            "accessible_name": "Search",
        },
        {
            "tool_name": "click",
            "selector": "#search-submit",
            "source_url": "https://example.com/catalog",
            "role": "button",
            "accessible_name": "Submit",
        },
    ]
    synthesized = synthesize_code_block(trajectory)
    assert synthesized is not None

    standalone_code = textwrap.dedent(synthesized.code)
    derived_steps = derive_code_block_steps(standalone_code)
    expected_fields = {"description", "action_type", "line_start", "line_end"}

    for producer_name, steps in (("synthesize", synthesized.steps), ("derive", derived_steps)):
        assert steps, f"{producer_name} must produce representative steps"
        for step in steps:
            assert set(step) == expected_fields, producer_name


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


def _workflow_with(code, parameter_keys=None, parameters=(("site_credentials", "credential_id"),)):
    block = {"block_type": "code", "label": "authenticate", "code": code}
    if parameter_keys is not None:
        block["parameter_keys"] = parameter_keys
    return yaml.safe_dump(
        {
            "workflow_definition": {
                "parameters": [{"key": key, "parameter_type": kind} for key, kind in parameters],
                "blocks": [block],
            }
        }
    )


def _bound_keys(src):
    return yaml.safe_load(bind_referenced_parameters_in_yaml(src))["workflow_definition"]["blocks"][0].get(
        "parameter_keys"
    )


def test_bind_adds_a_declared_parameter_the_code_names():
    # Recorded defect: the submission declares the parameter and the code uses it, but the
    # block omits parameter_keys, so the name is missing from runtime scope and the block
    # raises NameError after the login has already happened.
    src = _workflow_with("await page.get_by_label('Email').fill(site_credentials.username)\n")
    assert _bound_keys(src) == ["site_credentials"]


def test_bind_keeps_existing_keys_and_appends_only_what_is_missing():
    src = _workflow_with(
        "print(run_id, site_credentials.password)\n",
        parameter_keys=["run_id"],
        parameters=(("site_credentials", "credential_id"), ("run_id", "string")),
    )
    assert _bound_keys(src) == ["run_id", "site_credentials"]


def test_bind_ignores_names_the_workflow_never_declared():
    src = _workflow_with("print(undeclared_secret)\n")
    assert _bound_keys(src) is None


def test_bind_does_not_match_a_declared_key_inside_a_longer_name():
    src = _workflow_with("print(site_credentials.username)\n", parameters=(("credentials", "string"),))
    assert _bound_keys(src) is None


def test_bind_ignores_a_name_that_only_appears_in_a_docstring_or_string():
    # Binding puts the real value in the block's scope, so a textual mention must not be
    # enough: a docstring naming the key would hand a credential to code that never read it.
    src = _workflow_with('"""Reads site_credentials from the vault."""\nprint("site_credentials")\n')
    assert _bound_keys(src) is None


def test_bind_ignores_a_name_the_code_only_assigns():
    # A block defining its own name has not read the parameter; binding it there would widen
    # the real value's scope to code that never asked for it.
    src = _workflow_with('site_credentials = {"user": "local"}\nawait page.goto("https://x.test/")\n')
    assert _bound_keys(src) is None


def test_bind_skips_a_key_the_executor_reserves():
    # `password` resolves to a bound credential's secret in the executor namespace, so binding
    # a parameter under that name hands the block the credential instead of the parameter.
    src = _workflow_with("print(password)\n", parameters=(("password", "string"),))
    assert _bound_keys(src) is None


def test_bind_noop_on_unparseable_code():
    # Nothing is bound rather than everything: an unparseable block names no identifiers.
    src = _workflow_with("def broken(:\n")
    assert _bound_keys(src) is None


def test_bind_is_idempotent_so_callers_can_bind_before_the_seam():
    # Callers bind their own copy before conversion, or the converted workflow and the text the
    # user accepts disagree about a block's scope: the run gets the keys, the saved document
    # does not, and the block dies on the NameError binding exists to prevent.
    src = _workflow_with("await page.get_by_label('Email').fill(site_credentials.username)\n")
    once = bind_referenced_parameters_in_yaml(src)
    assert bind_referenced_parameters_in_yaml(once) == once
    assert yaml.safe_load(once)["workflow_definition"]["blocks"][0]["parameter_keys"] == ["site_credentials"]


def test_bind_noop_on_unparseable():
    assert bind_referenced_parameters_in_yaml("::not yaml::") == "::not yaml::"


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


@pytest.mark.asyncio
async def test_process_workflow_yaml_derives_code_block_steps_for_replace_path():
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
    wf = await _process_workflow_yaml(
        workflow_id="w_1",
        settings_fallback_yaml="enable_self_healing: false",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        workflow_yaml=yaml_str,
    )
    block = wf.workflow_definition.blocks[0]
    assert block.steps is not None
    assert [s.action_type for s in block.steps] == [ActionType.GOTO_URL, ActionType.CLICK]


@pytest.mark.asyncio
async def test_process_workflow_yaml_binds_a_parameter_the_code_names():
    from skyvern.forge.sdk.routes.workflow_copilot import _process_workflow_yaml

    yaml_str = (
        "title: Login\n"
        "workflow_definition:\n"
        "  parameters:\n"
        "  - key: site_login\n"
        "    parameter_type: workflow\n"
        "    workflow_parameter_type: string\n"
        "  blocks:\n"
        "  - block_type: code\n"
        "    label: authenticate\n"
        "    code: |\n"
        "      await page.get_by_label('Email').fill(site_login)\n"
    )
    wf = await _process_workflow_yaml(
        workflow_id="w_1",
        settings_fallback_yaml="enable_self_healing: false",
        workflow_permanent_id="wpid_1",
        organization_id="o_1",
        workflow_yaml=yaml_str,
    )
    block = wf.workflow_definition.blocks[0]
    assert [parameter.key for parameter in block.parameters] == ["site_login"]


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
    # The canonical failing case: open a site, read a list of links, then for each
    # link open it and read its contents. The two navigations must not share copy.
    code = (
        "async def run(page, limit):\n"
        "    await page.goto('https://example.com/')\n"
        "    posts = await page.locator('.post a').all_text_contents()\n"
        "    for post in posts[:limit]:\n"
        "        await page.goto(post)\n"
        "        await page.locator('.comment').inner_text()\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["goto_url", "extract", "goto_url", "extract"]
    descriptions = [s["description"] for s in steps]
    assert descriptions[0] == "Open https://example.com/"
    assert descriptions[2] == "Open each post"
    # The follow-up navigation must be distinguishable from the first, not a repeated label.
    assert descriptions[0] != descriptions[2]


def test_page_extract_is_not_a_code_block_step():
    # Code blocks run raw Playwright; page.extract is not part of the surface and
    # must not render a step in the editor preview.
    code = "async def run(page):\n    data = await page.extract(prompt='Extract the product names')\n"
    assert derive_code_block_steps(code) == []


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


def test_raw_dom_reads_surface_as_an_extraction_step_not_just_navigation():
    # The reported case: a block navigates then scrapes the DOM with raw Playwright
    # reads (no page.extract()). It must read as "navigate, then extract", not as a
    # lone "Goto URL" that hides everything the code does.
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    rows = page.locator('tr.item')\n"
        "    count = await rows.count()\n"
        "    results = []\n"
        "    for i in range(count):\n"
        "        row = rows.nth(i)\n"
        "        title = await row.locator('.title').text_content()\n"
        "        href = await row.locator('a').get_attribute('href')\n"
        "        results.append({'title': title, 'href': href})\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["goto_url", "extract"]
    assert steps[1]["description"] == "Extract information from the page"


def test_consecutive_dom_reads_collapse_into_one_extraction_step():
    # A scrape reads many fields; surfacing one step per read is noise. Collapse a
    # run of adjacent reads into a single step spanning their combined line range.
    code = (
        "async def run(page):\n"
        "    a = await page.locator('#a').text_content()\n"
        "    b = await page.locator('#b').inner_text()\n"
        "    c = await page.locator('#c').get_attribute('value')\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["extract"]
    assert steps[0]["line_start"] == 2
    assert steps[0]["line_end"] == 4


def test_dom_reads_separated_by_an_action_are_distinct_steps():
    code = (
        "async def run(page):\n"
        "    name = await page.locator('#name').text_content()\n"
        "    await page.get_by_role('button', name='Next').click()\n"
        "    price = await page.locator('#price').text_content()\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["extract", "click", "extract"]


def test_control_flow_reads_do_not_fabricate_an_extraction_step():
    # Visibility checks and counts gate control flow; they are not data extraction
    # and must not invent an extract step.
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    if await page.locator('#banner').is_visible():\n"
        "        await page.get_by_role('button', name='Close').click()\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["goto_url", "click"]
