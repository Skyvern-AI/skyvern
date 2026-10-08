import math
import textwrap
import time
import timeit
from typing import Any

import pytest
import yaml

from skyvern.forge.sdk.copilot.code_block_steps import (
    analyze_code_actions,
    bind_referenced_parameters_in_yaml,
    carry_user_owned_goals_in_yaml,
    code_block_labels_awaiting_goal_rebuild,
    code_block_labels_with_user_owned_goal,
    code_typed_values,
    derive_code_block_steps,
    derive_code_block_steps_in_yaml,
    user_owned_goal_carry_disclosure,
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


def test_analyze_maps_the_bare_solve_captcha_builtin_to_a_step_with_its_line():
    code = (
        "async def run(page):\n"
        "    await page.goto('https://example.com/')\n"
        "    await page.get_by_role('button', name='Search').click()\n"
        "    await solve_captcha(page)\n"
        "    await page.get_by_role('button', name='Continue').click()\n"
    )
    spans = analyze_code_actions(code)
    assert [(s.action_type, s.line_start) for s in spans] == [
        ("goto_url", 2),
        ("click", 3),
        ("solve_captcha", 4),
        ("click", 5),
    ]
    solve = spans[2]
    assert solve.receiver == ""
    assert solve.first_arg is None
    assert solve.prompt is None


def test_analyze_ignores_a_bare_call_that_is_not_a_code_block_builtin():
    code = "async def run(page):\n    await fetch_rows(page)\n    await page.click('#next')\n"
    assert [(s.action_type, s.line_start) for s in analyze_code_actions(code)] == [("click", 3)]


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


def test_derived_steps_keep_a_recording_repair_step():
    trajectory = [
        {"tool_name": "select_option", "selector": "#country", "value": "", "source_url": "https://example.com/"},
        {"tool_name": "click", "selector": "#next"},
    ]
    synthesized = synthesize_code_block(trajectory, _allow_recording_fallbacks=True)
    assert synthesized is not None

    derived = derive_code_block_steps(textwrap.dedent(synthesized.code))

    assert [(step["description"], step["action_type"]) for step in derived][1] == (
        "Repair recorded select_option",
        "select_option",
    )
    assert derived[1]["line_start"] == synthesized.steps[1]["line_start"]


def test_derive_steps_empty_code_is_empty():
    assert derive_code_block_steps("") == []
    assert derive_code_block_steps("x = 1\n") == []


def test_derive_in_yaml_sets_steps_on_code_blocks_and_leaves_others_untouched():
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
    out = yaml.safe_load(derive_code_block_steps_in_yaml(yaml.safe_dump(src)))
    blocks = out["workflow_definition"]["blocks"]
    assert blocks[0]["steps"] == [
        {"description": "Open https://x.com/", "action_type": "goto_url", "line_start": 2, "line_end": 2}
    ]
    assert "steps" not in blocks[1]  # non-code block untouched
    assert blocks[2]["loop_blocks"][0]["steps"][0]["action_type"] == "click"  # nested code block annotated


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


def test_derive_in_yaml_rebuilds_stale_steps_from_code():
    stale = [{"description": "Open the homepage", "action_type": "click", "line_start": 4, "line_end": 9}]
    src = {
        "workflow_definition": {
            "blocks": [
                {
                    "block_type": "code",
                    "label": "block_1",
                    "code": "await page.goto('https://x.com/')\n",
                    "steps": stale,
                }
            ]
        }
    }
    out = yaml.safe_load(derive_code_block_steps_in_yaml(yaml.safe_dump(src)))
    assert out["workflow_definition"]["blocks"][0]["steps"] == [
        {"description": "Open https://x.com/", "action_type": "goto_url", "line_start": 1, "line_end": 1}
    ]


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


def test_goto_on_a_parameter_names_the_variable():
    code = "async def run(page, product_url):\n    await page.goto(product_url)\n"
    steps = derive_code_block_steps(code)
    assert steps[0]["action_type"] == "goto_url"
    assert steps[0]["description"] == "Open the product url"


@pytest.mark.parametrize(
    ("code", "description"),
    [
        ("await page.goto(url='https://example.com/a')\n", "Open https://example.com/a"),
        ("async def run(page, product_url):\n    await page.goto(url=product_url)\n", "Open the product url"),
        ("url = 'https://example.com/a'\nawait page.goto(url=url, wait_until='load')\n", "Open https://example.com/a"),
    ],
)
def test_goto_with_url_keyword_is_labeled_like_the_positional_form(code, description):
    assert derive_code_block_steps(code)[0]["description"] == description


def test_goto_on_a_variable_bound_once_to_an_address_shows_the_address():
    code = "async def run(page):\n    url = 'https://example.com/a'\n    await page.goto(url)\n"
    assert derive_code_block_steps(code)[0]["description"] == "Open https://example.com/a"


@pytest.mark.parametrize(
    "code",
    [
        (
            "async def run(page):\n    url = 'https://example.com/a'\n"
            "    url = 'https://example.com/b'\n    await page.goto(url)\n"
        ),
        "async def run(page, url='https://example.com/a'):\n    await page.goto(url)\n",
        "async def run(page):\n    url = 'https://example.com/a'\n    url += '/b'\n    await page.goto(url)\n",
        "async def run(page):\n    await page.goto(url)\n    url = 'https://example.com/a'\n",
        "async def run(page):\n    url = build('https://example.com/a')\n    await page.goto(url)\n",
        "async def run(page, flag):\n    if flag:\n        url = 'https://example.com/a'\n    await page.goto(url)\n",
        "def helper():\n    url = 'https://example.com/a'\n\nasync def run(page):\n    await page.goto(url)\n",
    ],
)
def test_goto_on_a_variable_without_a_single_fixed_address_names_the_variable(code):
    assert derive_code_block_steps(code)[0]["description"] == "Open the url"


def test_goto_address_shows_a_plain_http_address_unchanged():
    code = "url = 'https://example.com:8443/a/b'\nawait page.goto(url)\n"
    assert derive_code_block_steps(code)[0]["description"] == "Open https://example.com:8443/a/b"


@pytest.mark.parametrize(
    "address",
    [
        "https://user:pass@example.com/a",
        "admin:hunter2@example.com/login",
        "{{ start_url }}",
        "https://example.com/{{ path }}",
        "https://example.com/a?token=abc",
        "https://example.com/a#frag",
        "ftp://example.com/a",
        "https://example.com:99999/a",
    ],
)
def test_goto_address_that_is_not_a_plain_http_address_names_the_variable(address):
    code = f"url = {address!r}\nawait page.goto(url)\n"
    assert derive_code_block_steps(code)[0]["description"] == "Open the url"


def _derive_seconds(code: str) -> float:
    # Thread CPU time, so a busy neighbour thread cannot stall one run; timeit pauses the garbage collector.
    return timeit.timeit(lambda: derive_code_block_steps(code), number=1, timer=time.thread_time)


def _derive_growth(small: str, large: str) -> float:
    # A busy core still slows CPU time, so the runs alternate and the best of each is compared.
    small_best = large_best = math.inf
    for _ in range(5):
        small_best = min(small_best, _derive_seconds(small))
        large_best = min(large_best, _derive_seconds(large))
    return large_best / small_best


def test_many_gotos_on_variables_derive_in_linear_time():
    def gotos(count: int) -> str:
        return "".join(f"url_{i} = 'https://example.com/{i}'\nawait page.goto(url_{i})\n" for i in range(count))

    assert derive_code_block_steps(gotos(2000))[-1]["description"] == "Open https://example.com/1999"
    # Quadratic growth makes 8x the gotos take ~64x as long; linear stays near 8x.
    assert _derive_growth(gotos(250), gotos(2000)) < 24


def test_unclosed_template_openers_derive_in_linear_time():
    def commented(count: int) -> str:
        return "await page.goto('https://example.com/a')\n" + "x = 1  # {# note {% here\n" * count

    assert derive_code_block_steps(commented(2000))[0]["description"] == "Open https://example.com/a"
    # Quadratic growth makes 8x the lines take ~64x as long; linear stays near 8x.
    assert _derive_growth(commented(250), commented(2000)) < 24


def test_reads_chained_in_one_expression_derive_in_linear_time():
    def chained(count: int) -> str:
        return "total = " + " + ".join(f"await page.locator('#p{i}').inner_text()" for i in range(count)) + "\n"

    # Python 3.11's parser rejects much longer chains, which would return [] and pass vacuously.
    assert derive_code_block_steps(chained(2000))[0]["description"] == "Extract total"
    # Quadratic growth makes 8x the reads take ~64x as long; linear stays near 8x.
    assert _derive_growth(chained(250), chained(2000)) < 24


def test_code_too_deep_for_the_parser_derives_no_steps():
    assert derive_code_block_steps("x = " + "-" * 200000 + "1\n") == []


def test_goto_on_a_loop_variable_keeps_the_open_each_wording():
    code = (
        "async def run(page):\n    url = 'https://example.com/a'\n    for url in urls:\n        await page.goto(url)\n"
    )
    assert derive_code_block_steps(code)[0]["description"] == "Open each url"


def test_goto_on_an_expression_keeps_the_vague_wording():
    code = "async def run(page):\n    await page.goto(links[0])\n"
    assert derive_code_block_steps(code)[0]["description"] == "Open the linked page"


@pytest.mark.parametrize(
    "code, expected",
    [
        ("price = await page.locator('.p').inner_text()\n", "Extract price"),
        ("price = (await page.locator('.p').inner_text()).strip()\n", "Extract price"),
        ("status = (await page.locator('.s').text_content() or '').strip()\n", "Extract status"),
        ("first_link = (await page.locator('a').all_text_contents())[0]\n", "Extract first link"),
        ("price = await page.get_by_role('cell', name='Price').inner_text()\n", 'Extract price from "Price"'),
        ("await page.get_by_label('Price').inner_text()\n", 'Extract "Price"'),
        ("await page.get_by_label(text='Price').inner_text()\n", 'Extract "Price"'),
        ("total = await page.get_by_text(text='Total').inner_text()\n", 'Extract total from "Total"'),
        ("results.append(await page.locator('.row').inner_text())\n", "Extract results"),
        ("row = {'title': await page.locator('.t').inner_text()}\n", "Extract title"),
    ],
)
def test_read_is_named_from_where_the_code_stores_it_or_the_element_it_cites(code, expected):
    assert [s["description"] for s in derive_code_block_steps(code)] == [expected]


def test_template_control_blocks_keep_steps_and_their_line_numbers():
    code = "{% if enabled %}\nawait page.goto('https://example.com/a')\n{% endif %}\nprice = await page.locator('.p').inner_text()\n"
    assert [(s["description"], s["line_start"]) for s in derive_code_block_steps(code)] == [
        ("Open https://example.com/a", 2),
        ("Extract price", 4),
    ]


def test_template_comment_does_not_cost_the_block_its_steps():
    code = "{# optional note #}\nprice = await page.locator('.p').inner_text()\n"
    assert [(s["description"], s["line_start"]) for s in derive_code_block_steps(code)] == [("Extract price", 2)]


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ("{% if enabled %}await page.goto('https://example.com/a'){% endif %}\n", 1),
        ("{# note #}await page.goto('https://example.com/a')\n", 1),
        ("{% set x =\n  1 %}\nawait page.goto('https://example.com/a')\n", 3),
        ("{% if f %}value = 1{% else %}value = 2{% endif %}\nawait page.goto('https://example.com/a')\n", 2),
    ],
)
def test_python_sharing_a_line_with_a_template_tag_keeps_its_step(code, line):
    assert [(s["description"], s["line_start"]) for s in derive_code_block_steps(code)] == [
        ("Open https://example.com/a", line)
    ]


@pytest.mark.parametrize(
    "code",
    [
        "if await page.locator('.price-box').inner_text():\n    pass\n",
        "async def run(page):\n    return await page.locator('.price-box').inner_text()\n",
        "price = parse(await page.locator('.price-box').inner_text())\n",
        "await page.locator('.price-box').inner_text()\n",
    ],
)
def test_read_without_a_name_in_the_code_keeps_the_vague_wording(code):
    assert [s["description"] for s in derive_code_block_steps(code)] == ["Extract information from the page"]


def test_fill_value_bound_to_a_literal_never_appears_in_the_label():
    code = "async def run(page):\n    v = 'secret'\n    await page.fill('#x', v)\n    await page.type('#y', v)\n"
    descriptions = [s["description"] for s in derive_code_block_steps(code)]
    assert descriptions == ["Type into the element", "Type into the element"]
    assert not any("secret" in d for d in descriptions)


@pytest.mark.parametrize(
    ("code", "typed"),
    [
        pytest.param(
            "await page.get_by_label('Name').fill('Ada Lovelace')",
            [(1, "page.get_by_label('Name')", "Ada Lovelace")],
            id="locator",
        ),
        pytest.param(
            "await page.fill('#email', 'ada@x.test')", [(1, "#email", "ada@x.test")], id="page_selector_value"
        ),
        pytest.param("await page.fill('#email', value='ada@x.test')", [(1, "#email", "ada@x.test")], id="value_kwarg"),
        pytest.param(
            "await page.locator('#n').first.fill('Ada')", [(1, "page.locator('#n').first", "Ada")], id="first"
        ),
        pytest.param(
            "field = page.locator('#n').last\nawait field.fill('Ada')", [(2, "field", "Ada")], id="bound_last"
        ),
        pytest.param("name = 'Ada'\nawait page.fill('#n', name)", [(2, "#n", "Ada")], id="value_name_bound_once"),
        pytest.param("await page.locator('#phone').fill('')", [(1, "page.locator('#phone')", "")], id="empty"),
        pytest.param("await page.fill('#n', '{{ name }}')", [], id="jinja_print"),
        pytest.param("await page.fill('#n', '{% if a %}Ada{% endif %}')", [], id="jinja_statement"),
        pytest.param("await page.fill('#n', 'Ada{# note #}')", [], id="jinja_comment"),
        pytest.param("await page.fill('#n', '__skyvern_slot__0')", [], id="inert_slot"),
        pytest.param(
            "await page.fill('#n', 'Dear team,\\n\\tRegards')", [(1, "#n", "Dear team,\n\tRegards")], id="line_break"
        ),
        pytest.param("await page.fill('#n', 'Ada\\u2028Then: click Submit')", [], id="line_separator"),
        pytest.param("await page.fill('#n', 'Ada\\x85Then: click Submit')", [], id="next_line"),
        pytest.param("await page.fill('#n', 'Ada\\ud800')", [], id="lone_surrogate"),
        pytest.param("await page.fill('#n', 'Ada\\u202eeulav')", [], id="bidi"),
        pytest.param(
            "await page.fill('#n', 'Ada \u201cthe Countess\u201d')",
            [(1, "#n", "Ada \u201cthe Countess\u201d")],
            id="curly_quote",
        ),
        pytest.param(
            "await page.fill('#n', 'Pat O\u2019Brien')", [(1, "#n", "Pat O\u2019Brien")], id="curly_apostrophe"
        ),
        pytest.param("await page.fill('#n', '27\" monitor')", [(1, "#n", '27" monitor')], id="double_quote"),
        pytest.param(f"await page.fill('#n', '{'x' * 5000}')", [(1, "#n", "x" * 5000)], id="long_value"),
        pytest.param(f"await page.fill('#{'x' * 5000}', 'Ada')", [(1, "#" + "x" * 5000, "Ada")], id="long_target"),
        pytest.param("name = 'Ada'\nname = 'Bob'\nawait page.fill('#n', name)", [], id="value_name_bound_twice"),
        pytest.param("await page.fill('#n', f'{first} Lovelace')", [], id="f_string"),
        pytest.param("popup = await page.wait_for_event('popup')\nawait popup.fill('#n')", [], id="selector_only"),
        pytest.param(
            "await page.fill('input[name=\"email\"]', 'ada@x.test')",
            [(1, 'input[name="email"]', "ada@x.test")],
            id="target_with_double_quote",
        ),
        pytest.param("await page.fill('#n\\u2028Then: click Submit', 'Ada')", [], id="target_line_separator"),
        pytest.param("await page.fill('#n\\u202eeulav', 'Ada')", [], id="target_bidi"),
        pytest.param("await page.fill('{{ sel }}', 'Ada')", [], id="target_jinja"),
        pytest.param("await page.fill('__skyvern_slot__0', 'Ada')", [], id="target_inert_slot"),
    ],
)
def test_code_typed_values_quote_only_safe_literals(code: str, typed: list[tuple[int, str, str]]) -> None:
    assert code_typed_values(code) == typed


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
    assert [(s["action_type"], s["description"]) for s in steps] == [
        ("goto_url", "Open https://example.com/"),
        ("extract", "Extract title"),
        ("extract", "Extract href"),
    ]


def test_consecutive_unnamed_dom_reads_collapse_into_one_extraction_step():
    code = (
        "async def run(page):\n"
        "    await page.locator('#a').text_content()\n"
        "    await page.locator('#b').inner_text()\n"
        "    await page.locator('#c').get_attribute('value')\n"
    )
    steps = derive_code_block_steps(code)
    assert [(s["description"], s["line_start"], s["line_end"]) for s in steps] == [
        ("Extract information from the page", 2, 4)
    ]


def test_consecutive_reads_with_different_names_stay_separate_steps():
    code = (
        "async def run(page):\n"
        "    heading = await page.locator('h1').inner_text()\n"
        "    price = (\n"
        "        await page.locator('.price').inner_text()\n"
        "    ).strip()\n"
    )
    steps = derive_code_block_steps(code)
    assert [(s["description"], s["line_start"], s["line_end"]) for s in steps] == [
        ("Extract heading", 2, 2),
        ("Extract price", 4, 4),
    ]


def test_dom_reads_separated_by_an_action_are_distinct_steps():
    code = (
        "async def run(page):\n"
        "    name = await page.locator('#name').text_content()\n"
        "    await page.get_by_role('button', name='Next').click()\n"
        "    price = await page.locator('#price').text_content()\n"
    )
    steps = derive_code_block_steps(code)
    assert [s["action_type"] for s in steps] == ["extract", "click", "extract"]


def _goal_yaml(
    *,
    prompt: str,
    code: str,
    user_owned_goal: bool | None = None,
    goal_needs_regeneration: bool | None = None,
    code_edited_by_hand: bool | None = None,
    label: str = "login",
) -> str:
    block: dict[str, str | bool] = {"block_type": "code", "label": label, "prompt": prompt, "code": code}
    if user_owned_goal is not None:
        block["user_owned_goal"] = user_owned_goal
    if goal_needs_regeneration is not None:
        block["goal_needs_regeneration"] = goal_needs_regeneration
    if code_edited_by_hand is not None:
        block["code_edited_by_hand"] = code_edited_by_hand
    return yaml.safe_dump({"workflow_definition": {"blocks": [block]}}, sort_keys=False)


def _goal_block(workflow_yaml: str) -> dict[str, Any]:
    return yaml.safe_load(workflow_yaml)["workflow_definition"]["blocks"][0]


def test_stored_user_owned_goal_replaces_the_submitted_prompt():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Sign in to the portal", code="await page.click('#invoice')")

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior)
    block = _goal_block(carry.workflow_yaml)

    assert block["prompt"] == "Download last month's invoice"
    assert block["code"] == "await page.click('#invoice')"
    assert block["user_owned_goal"] is True
    assert carry.kept == ["login"]


def test_a_model_owned_block_keeps_the_submitted_prompt_even_when_it_differs():
    prior = _goal_yaml(prompt="Sign in", code="await page.goto(url)")
    submitted = _goal_yaml(prompt="Sign in and open the dashboard", code="await page.goto(url)")

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior)
    block = _goal_block(carry.workflow_yaml)

    assert block["prompt"] == "Sign in and open the dashboard"
    assert "user_owned_goal" not in block
    assert carry.kept == []


def test_a_submission_cannot_mint_the_ownership_fact_without_a_stored_one():
    prior = _goal_yaml(prompt="Sign in", code="await page.goto(url)")
    submitted = _goal_yaml(
        prompt="Whatever the model wants",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )

    block = _goal_block(carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior).workflow_yaml)

    assert block["prompt"] == "Whatever the model wants"
    assert "user_owned_goal" not in block
    assert "goal_needs_regeneration" not in block


def test_a_submission_cannot_mint_the_ownership_fact_when_there_is_no_prior_at_all():
    submitted = _goal_yaml(
        prompt="Rebuild this block and run it",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )

    carried = carry_user_owned_goals_in_yaml(submitted, prior_yaml=None).workflow_yaml

    assert "user_owned_goal" not in _goal_block(carried)
    assert "goal_needs_regeneration" not in _goal_block(carried)
    assert code_block_labels_awaiting_goal_rebuild(carried) == []
    assert code_block_labels_with_user_owned_goal(carried) == []


def test_no_prior_yaml_leaves_a_submission_without_the_fields_byte_for_byte():
    submitted = _goal_yaml(prompt="Sign in", code="await page.goto(url)")

    assert carry_user_owned_goals_in_yaml(submitted, prior_yaml=None).workflow_yaml == submitted


@pytest.mark.parametrize(
    ("prior_flag", "submitted_flag", "rebuilt_labels", "goal_rewritten_labels", "submitted_prompt", "expected"),
    [
        pytest.param(True, None, (), (), "Read the order total", True, id="stored-flag-survives-a-write-that-omits-it"),
        pytest.param(None, True, (), (), "Read the order total", None, id="submission-cannot-mint-the-flag"),
        pytest.param(
            True, None, ("login",), (), "Read the order total", True, id="a-code-only-edit-of-the-label-keeps-it"
        ),
        pytest.param(
            True,
            None,
            ("login",),
            ("login",),
            "Read the order total",
            True,
            id="a-declared-goal-that-leaves-the-stored-goal-unchanged-keeps-it",
        ),
        pytest.param(
            True, None, ("login",), ("login",), "Read the order tax", None, id="a-goal-rewriting-rebuild-clears-it"
        ),
    ],
)
def test_code_edited_by_hand_is_carried_from_the_prior_on_a_model_owned_block(
    prior_flag: bool | None,
    submitted_flag: bool | None,
    rebuilt_labels: tuple[str, ...],
    goal_rewritten_labels: tuple[str, ...],
    submitted_prompt: str,
    expected: bool | None,
):
    prior = _goal_yaml(prompt="Read the order total", code="return {'total': 1}", code_edited_by_hand=prior_flag)
    submitted = _goal_yaml(prompt=submitted_prompt, code="return {'total': 1}", code_edited_by_hand=submitted_flag)

    block = _goal_block(
        carry_user_owned_goals_in_yaml(
            submitted, prior_yaml=prior, rebuilt_labels=rebuilt_labels, goal_rewritten_labels=goal_rewritten_labels
        ).workflow_yaml
    )

    assert block.get("code_edited_by_hand") is expected


@pytest.mark.parametrize(
    ("goal_needs_regeneration", "expected"),
    [
        pytest.param(False, True, id="owned-goal-discards-the-declared-goal-so-the-flag-stays"),
        pytest.param(True, None, id="rebuilding-a-pending-owned-goal-clears-it"),
    ],
)
def test_a_goal_rewriting_copilot_write_on_a_hand_edited_owned_block(
    goal_needs_regeneration: bool, expected: bool | None
):
    prior = _goal_yaml(
        prompt="Read the order total",
        code="return {'total': 1, 'currency': 'USD'}",
        user_owned_goal=True,
        goal_needs_regeneration=goal_needs_regeneration,
        code_edited_by_hand=True,
    )
    submitted = _goal_yaml(prompt="Read the order total and its tax", code="return {'total': 1, 'tax': 0}")

    block = _goal_block(
        carry_user_owned_goals_in_yaml(
            submitted, prior_yaml=prior, rebuilt_labels=("login",), goal_rewritten_labels=("login",)
        ).workflow_yaml
    )

    assert block["prompt"] == "Read the order total"
    assert block.get("code_edited_by_hand") is expected


def test_an_unaccepted_copilot_goal_on_a_hand_edited_owned_block_keeps_the_stored_goal_and_flag():
    prior = _goal_yaml(
        prompt="Read the order total",
        code="return {'total': 1, 'currency': 'USD'}",
        user_owned_goal=True,
        code_edited_by_hand=True,
    )
    submitted = _goal_yaml(prompt="Read the order total and its currency", code="return {'total': 1}")

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior)
    block = _goal_block(carry.workflow_yaml)

    assert block["prompt"] == "Read the order total"
    assert block["user_owned_goal"] is True
    assert block["code_edited_by_hand"] is True
    assert carry.kept == ["login"]


def test_an_accepted_goal_update_survives_a_later_copilot_write():
    accepted = _goal_yaml(
        prompt="Read the order total and its currency",
        code="return {'total': 1, 'currency': 'USD'}",
        user_owned_goal=True,
        goal_needs_regeneration=False,
        code_edited_by_hand=False,
    )
    submitted = _goal_yaml(prompt="Read the order total", code="return {'total': 1, 'currency': 'USD'}")

    block = _goal_block(carry_user_owned_goals_in_yaml(submitted, prior_yaml=accepted).workflow_yaml)

    assert block["prompt"] == "Read the order total and its currency"
    assert block["user_owned_goal"] is True
    assert "code_edited_by_hand" not in block


def test_resubmitting_identical_code_leaves_the_block_awaiting_a_rebuild():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Sign in", code="await page.goto(url)")

    carried = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior).workflow_yaml

    assert _goal_block(carried)["goal_needs_regeneration"] is True
    assert code_block_labels_awaiting_goal_rebuild(carried) == ["login"]


def test_an_unattributed_code_edit_does_not_cancel_the_pending_rebuild():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Sign in", code="await page.goto(url)  # tidied")

    carried = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior).workflow_yaml

    assert _goal_block(carried)["goal_needs_regeneration"] is True
    assert code_block_labels_awaiting_goal_rebuild(carried) == ["login"]


def test_a_rebuild_of_the_block_clears_the_pending_rebuild_and_keeps_ownership():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Sign in", code="await page.get_by_role('link', name='Invoice').click()")

    carried = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior, rebuilt_labels={"login"}).workflow_yaml
    block = _goal_block(carried)

    assert block["goal_needs_regeneration"] is False
    assert block["user_owned_goal"] is True
    assert block["prompt"] == "Download last month's invoice"
    assert code_block_labels_awaiting_goal_rebuild(carried) == []


def test_a_rebuild_that_reproduces_the_same_code_still_clears_the_pending_rebuild():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Sign in", code="await page.goto(url)")

    carried = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior, rebuilt_labels={"login"}).workflow_yaml

    assert _goal_block(carried)["goal_needs_regeneration"] is False
    assert code_block_labels_awaiting_goal_rebuild(carried) == []


def test_a_differently_cased_block_type_cannot_dodge_the_carry():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(
        prompt="Whatever the model wants",
        code="await page.goto(url)",
        user_owned_goal=True,
    ).replace("block_type: code", "block_type: CODE ")

    block = _goal_block(carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior).workflow_yaml)

    assert block["prompt"] == "Download last month's invoice"
    assert block["goal_needs_regeneration"] is True


def test_an_impossible_date_scalar_in_the_workflow_does_not_break_the_carry_or_the_readers():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Sign in", code="await page.goto(url)") + "parameters:\n- default_value: 2025-02-30\n"

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior + "parameters:\n- default_value: 2025-02-30\n")

    assert _goal_block(carry.workflow_yaml)["prompt"] == "Download last month's invoice"
    assert code_block_labels_awaiting_goal_rebuild(carry.workflow_yaml) == ["login"]
    assert code_block_labels_with_user_owned_goal(carry.workflow_yaml) == ["login"]


def test_a_stored_goal_that_is_not_text_never_overwrites_the_submitted_prompt():
    prior = yaml.safe_dump(
        {
            "workflow_definition": {
                "blocks": [
                    {
                        "block_type": "code",
                        "label": "login",
                        "prompt": None,
                        "code": "await page.goto(url)",
                        "user_owned_goal": True,
                    }
                ]
            }
        },
        sort_keys=False,
    )
    submitted = _goal_yaml(prompt="Sign in to the portal", code="await page.goto(url)")

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior)

    assert _goal_block(carry.workflow_yaml)["prompt"] == "Sign in to the portal"
    assert carry.kept == []


def test_a_dropped_user_owned_block_is_disclosed_by_label_without_its_goal_text():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
    )
    submitted = _goal_yaml(prompt="Export the report", code="await page.goto(url)", label="export")

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior)

    assert carry.dropped == ["login"]
    disclosure = user_owned_goal_carry_disclosure(carry)
    assert disclosure["stored_goal_dropped"] == ["login"]
    assert "Download last month's invoice" not in str(disclosure)


def test_a_new_label_the_stored_workflow_does_not_have_is_model_owned():
    prior = _goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )
    submitted = _goal_yaml(prompt="Export the report", code="await page.goto(url)", label="export")

    block = _goal_block(carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior).workflow_yaml)

    assert block["prompt"] == "Export the report"
    assert "user_owned_goal" not in block


def test_an_unparseable_prior_yaml_leaves_the_submission_and_its_fields_untouched():
    submitted = _goal_yaml(
        prompt="Sign in",
        code="await page.goto(url)",
        user_owned_goal=True,
        goal_needs_regeneration=True,
    )

    assert carry_user_owned_goals_in_yaml(submitted, prior_yaml="title: [unclosed").workflow_yaml == submitted


def test_a_block_marked_stale_without_ownership_is_not_awaiting_a_rebuild():
    orphan_flag = _goal_yaml(prompt="Sign in", code="await page.goto(url)", goal_needs_regeneration=True)

    assert code_block_labels_awaiting_goal_rebuild(orphan_flag) == []
    assert code_block_labels_with_user_owned_goal(orphan_flag) == []


def _nested_goal_yaml(*, prompt: str, code: str, user_owned_goal: bool | None = None) -> str:
    inner: dict[str, str | bool] = {"block_type": "code", "label": "inner_login", "prompt": prompt, "code": code}
    if user_owned_goal is not None:
        inner["user_owned_goal"] = user_owned_goal
        inner["goal_needs_regeneration"] = user_owned_goal
    loop = {"block_type": "for_loop", "label": "each_row", "loop_blocks": [inner]}
    return yaml.safe_dump({"workflow_definition": {"blocks": [loop]}}, sort_keys=False)


def test_a_user_owned_goal_inside_a_loop_block_is_carried_named_and_disclosed():
    prior = _nested_goal_yaml(
        prompt="Download last month's invoice",
        code="await page.goto(url)",
        user_owned_goal=True,
    )
    submitted = _nested_goal_yaml(prompt="Sign in to the portal", code="await page.goto(url)")

    carry = carry_user_owned_goals_in_yaml(submitted, prior_yaml=prior)
    inner = yaml.safe_load(carry.workflow_yaml)["workflow_definition"]["blocks"][0]["loop_blocks"][0]

    assert inner["prompt"] == "Download last month's invoice"
    assert inner["user_owned_goal"] is True
    assert code_block_labels_awaiting_goal_rebuild(carry.workflow_yaml) == ["inner_login"]
    assert code_block_labels_with_user_owned_goal(carry.workflow_yaml) == ["inner_login"]
    assert carry.kept == ["inner_login"]


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
