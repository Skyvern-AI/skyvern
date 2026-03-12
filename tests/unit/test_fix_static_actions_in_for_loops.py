"""Tests for _fix_static_actions_in_for_loops and _patch_static_clicks_in_block."""

import textwrap

from skyvern.services.workflow_script_service import (
    _fix_static_actions_in_for_loops,
    _patch_static_clicks_in_block,
)

# ---------------------------------------------------------------------------
# _patch_static_clicks_in_block (low-level helper)
# ---------------------------------------------------------------------------


class TestPatchStaticClicksInBlock:
    """Unit tests for the inner helper that patches individual click calls."""

    def test_static_click_with_fallback_is_upgraded(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
                prompt='Click the download link',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        assert "ai='fallback'" not in result

    def test_static_click_prompt_gets_current_value(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
                prompt='Click the download link',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "current_value" in result
        assert "prompt=f'" in result or 'prompt=f"' in result

    def test_click_referencing_current_value_is_untouched(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector=f'a:has-text("{current_value["title"]}")',
                ai='fallback',
                prompt='Click the item',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert result == body

    def test_already_proactive_click_is_untouched(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='proactive',
                prompt='Click the download link',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert result == body

    def test_double_quoted_fallback_is_upgraded(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector="a.download-link",
                ai="fallback",
                prompt="Click the download link",
            )""")
        result = _patch_static_clicks_in_block(body)
        assert 'ai="proactive"' in result
        assert 'ai="fallback"' not in result
        assert "current_value" in result

    def test_double_quoted_proactive_is_untouched(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector="a.download-link",
                ai="proactive",
                prompt="Click the download link",
            )""")
        result = _patch_static_clicks_in_block(body)
        assert result == body

    def test_click_without_prompt_gets_prompt_added(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        assert "prompt=f'" in result
        assert "current_value" in result

    def test_multiple_clicks_only_static_ones_patched(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='button.accept',
                ai='fallback',
                prompt='Accept terms',
            )
            await page.click(
                selector=f'a:has-text("{current_value["name"]}")',
                ai='fallback',
                prompt='Click the item',
            )""")
        result = _patch_static_clicks_in_block(body)
        # First click (static) should be patched
        assert "ai='proactive'" in result
        # Second click (has current_value) should keep fallback
        assert "ai='fallback'" in result

    def test_no_click_calls_returns_unchanged(self) -> None:
        body = textwrap.dedent("""\
            await page.goto(url='https://example.com')
            data = await page.extract(prompt='Get items')""")
        result = _patch_static_clicks_in_block(body)
        assert result == body

    def test_original_prompt_text_is_preserved(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
                prompt='Download the quarterly report',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "Download the quarterly report" in result
        assert "Target: {current_value}" in result

    def test_single_quoted_prompt_with_escaped_apostrophe_is_preserved(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
                prompt='Open the applicant\\'s file',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        assert "applicant\\'s file" in result
        assert "Target: {current_value}" in result

    def test_double_quoted_prompt_with_escaped_quotes_is_preserved(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector="a.download-link",
                ai="fallback",
                prompt="Click the \\\"download\\\" link",
            )""")
        result = _patch_static_clicks_in_block(body)
        assert 'ai="proactive"' in result
        assert '\\"download\\"' in result
        assert "Target: {current_value}" in result

    def test_multiline_prompt_is_preserved(self) -> None:
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
                prompt='Click the first matching row
and then open the details panel',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        assert "Click the first matching row" in result
        assert "and then open the details panel" in result
        assert "Target: {current_value}" in result

    def test_unterminated_prompt_with_many_backslashes_is_left_unchanged(self) -> None:
        repeated_backslashes = "\\\\a" * 4000
        body = (
            "await page.click(\n"
            "    selector='a.download-link',\n"
            "    ai='fallback',\n"
            f'    prompt="{repeated_backslashes}\n'
            ")"
        )
        result = _patch_static_clicks_in_block(body)
        assert result == body


# ---------------------------------------------------------------------------
# _fix_static_actions_in_for_loops (top-level function)
# ---------------------------------------------------------------------------


class TestFixStaticActionsInForLoops:
    """Integration tests for the full for-loop detection and patching."""

    def test_static_click_inside_for_loop_is_patched(self) -> None:
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                await page.click(
                    selector='a.download-link',
                    ai='fallback',
                    prompt='Click the download link',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='proactive'" in result
        assert "current_value" in result.split("prompt=")[1]

    def test_click_outside_for_loop_is_untouched(self) -> None:
        code = textwrap.dedent("""\
            await page.click(
                selector='button.submit',
                ai='fallback',
                prompt='Click submit',
            )""")
        result = _fix_static_actions_in_for_loops(code)
        assert result == code

    def test_click_after_for_loop_is_untouched(self) -> None:
        """Code that follows the for-loop (at same or lesser indent) should not be patched."""
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                data = await page.extract(prompt='Get data')

            await page.click(
                selector='button.submit',
                ai='fallback',
                prompt='Click submit',
            )""")
        result = _fix_static_actions_in_for_loops(code)
        # The click after the loop should still be fallback
        lines_after_loop = result.split("await page.click(")[1]
        assert "ai='fallback'" in lines_after_loop

    def test_nested_indentation_preserved(self) -> None:
        code = textwrap.dedent("""\
            async def block_fn(page, context):
                items = await page.extract(prompt='Get items')
                async for current_value in skyvern.loop(items):
                    await page.click(
                        selector='a.download-link',
                        ai='fallback',
                        prompt='Click link',
                    )""")
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='proactive'" in result
        # Verify the function def and extract are still there
        assert "async def block_fn" in result
        assert "await page.extract" in result

    def test_multiple_for_loops_both_patched(self) -> None:
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items_a):
                await page.click(
                    selector='a.link-a',
                    ai='fallback',
                    prompt='Click A',
                )

            async for current_value in skyvern.loop(items_b):
                await page.click(
                    selector='a.link-b',
                    ai='fallback',
                    prompt='Click B',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        assert result.count("ai='proactive'") == 2
        assert result.count("ai='fallback'") == 0

    def test_for_loop_with_current_value_click_is_untouched(self) -> None:
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                await page.click(
                    selector=f'a:has-text("{current_value["title"]}")',
                    ai='fallback',
                    prompt='Click the item',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='fallback'" in result
        assert "ai='proactive'" not in result

    def test_for_loop_with_proactive_click_is_untouched(self) -> None:
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                await page.click(
                    selector='a.download-link',
                    ai='proactive',
                    prompt='Click the download link',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        assert result == code

    def test_code_with_no_for_loops_is_untouched(self) -> None:
        code = textwrap.dedent("""\
            async def block_fn(page, context):
                await page.goto(url='https://example.com')
                data = await page.extract(prompt='Get items')
                return data""")
        result = _fix_static_actions_in_for_loops(code)
        assert result == code

    def test_empty_string(self) -> None:
        assert _fix_static_actions_in_for_loops("") == ""

    def test_realistic_download_script(self) -> None:
        """Simulate the real-world bug: a download script with static click inside for-loop."""
        code = textwrap.dedent("""\
            async def download_block(page, context):
                items = await page.extract(
                    data_extraction_goal='Extract all downloadable file names and links',
                    output_type='list',
                )
                async for current_value in skyvern.loop(items):
                    await page.click(
                        selector='a.file-download',
                        ai='fallback',
                        prompt='Click the download button for the file',
                    )
                    await page.wait_for_download()

                return {"downloaded": len(items)}""")
        result = _fix_static_actions_in_for_loops(code)
        # The static click should be patched
        assert "ai='proactive'" in result
        assert "current_value" in result.split("prompt=f'")[1].split("'")[0]
        # The wait_for_download and return should still be there
        assert "await page.wait_for_download()" in result
        assert 'return {"downloaded": len(items)}' in result

    def test_for_loop_body_with_blank_lines(self) -> None:
        """Blank lines inside the for-loop body should not break body detection."""
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                data = await page.extract(prompt='Get info')

                await page.click(
                    selector='a.download',
                    ai='fallback',
                    prompt='Download file',
                )

            result = "done"
            """)
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='proactive'" in result
        # Code after the loop should be untouched
        assert 'result = "done"' in result

    def test_complex_loop_expression(self) -> None:
        """Loop with complex expression in skyvern.loop() should still match."""
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(context.parameters["items"], max_iterations=10):
                await page.click(
                    selector='button.process',
                    ai='fallback',
                    prompt='Process the item',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='proactive'" in result

    def test_mixed_static_and_dynamic_clicks_in_loop(self) -> None:
        """Only the static click should be patched; the dynamic one should be left alone."""
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                await page.click(
                    selector='button.expand',
                    ai='fallback',
                    prompt='Expand the row',
                )
                await page.click(
                    selector=f'a:has-text("{current_value["name"]}")',
                    ai='fallback',
                    prompt='Click the specific item',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        # Static click patched
        assert "ai='proactive'" in result
        # Dynamic click preserved
        assert "ai='fallback'" in result

    def test_selector_with_nested_parens_is_matched(self) -> None:
        """CSS pseudo-selectors with parens (e.g. :has-text()) must not truncate the regex match."""
        code = textwrap.dedent("""\
            async for current_value in skyvern.loop(items):
                await page.click(
                    selector='a.file:has-text("Download")',
                    ai='fallback',
                    prompt='Click the link',
                )""")
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='proactive'" in result
        assert "ai='fallback'" not in result
        # The output must be syntactically coherent (no dangling args)
        assert result.count("await page.click(") == 1

    def test_selector_with_nth_child_parens(self) -> None:
        """Selectors like :nth-child(2) must not break the regex."""
        body = textwrap.dedent("""\
            await page.click(
                selector='tr:nth-child(2)',
                ai='fallback',
                prompt='Click row',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        assert "ai='fallback'" not in result
        assert "current_value" in result

    def test_click_without_ai_kwarg_is_untouched(self) -> None:
        """Clicks with no ai= kwarg should not be modified."""
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                prompt='Click the download link',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert result == body

    def test_deeply_nested_click_indent_is_correct(self) -> None:
        """When a click is inside a function + for-loop, injected prompt must align correctly."""
        code = textwrap.dedent("""\
            async def block_fn(page, context):
                items = await page.extract(prompt='Get items')
                async for current_value in skyvern.loop(items):
                    await page.click(
                        selector='a.download-link',
                        ai='fallback',
                    )""")
        result = _fix_static_actions_in_for_loops(code)
        assert "ai='proactive'" in result
        # Verify the injected prompt line is indented deeper than the click call
        for line in result.split("\n"):
            if "prompt=f'" in line and "current_value" in line:
                # The click `await` is at 8-space indent; kwarg should be at 12
                leading = len(line) - len(line.lstrip())
                assert leading == 12, f"Expected 12-space indent, got {leading}"
                break
        else:
            raise AssertionError("Expected a prompt=f line with current_value")

    def test_prompt_with_curly_braces_are_escaped(self) -> None:
        """Existing braces in prompt text must be escaped to avoid f-string evaluation."""
        body = textwrap.dedent("""\
            await page.click(
                selector='a.download-link',
                ai='fallback',
                prompt='Extract items matching {pattern}',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        # Original braces must be doubled so they're literal in the f-string
        assert "{{pattern}}" in result
        # The injected Target should still reference current_value
        assert "{current_value}" in result

    def test_two_level_nested_selector_is_matched(self) -> None:
        """Selectors like tr:has(td:has-text("Report")) must not break the regex."""
        body = textwrap.dedent("""\
            await page.click(
                selector='tr:has(td:has-text("Report"))',
                ai='fallback',
                prompt='Click row',
            )""")
        result = _patch_static_clicks_in_block(body)
        assert "ai='proactive'" in result
        assert "ai='fallback'" not in result
        assert "current_value" in result
