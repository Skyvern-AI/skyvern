"""Tests for ScriptReviewer classify-branch consolidation pass.

The consolidation pass enforces Rule 6b deterministically: when the LLM
emits multiple ``page.classify()`` branches with byte-identical action
bodies, drop the duplicates and keep the branch with the more informative
``text_patterns``.

Reproduces the SKY-9439 shape: a classify call with 4 of 12 branches
carrying ``"N/A"`` text_patterns and resolving to identical actions as
another branch — the reviewer was net-adding instead of replacing/merging.
"""

import textwrap

from skyvern.services.script_reviewer import ScriptReviewer


class TestConsolidateClassifyDuplicates:
    """Direct tests for ``_consolidate_classify_duplicates``."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_no_classify_no_change(self) -> None:
        """Code without page.classify() must pass through untouched."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                await page.complete()
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert new_code == code
        assert dropped == []

    def test_distinct_branches_unchanged(self) -> None:
        """Two branches with distinct actions must not be merged."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "Apply Now", "b": "Submit Application"},
                )
                if state == "a":
                    await page.click(selector='button:has-text("Apply")', ai='fallback', prompt='apply')
                elif state == "b":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Handle the form")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == []
        assert new_code == code

    def test_na_pattern_duplicate_merged(self) -> None:
        """Branch with N/A text_patterns + duplicate body should merge into the keyed branch."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"bills": "bills page", "view_bills_fallback": "fallback variant"},
                    text_patterns={"bills": "Billing & Payments", "view_bills_fallback": "N/A"},
                )
                if state == "bills":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                elif state == "view_bills_fallback":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                else:
                    await page.element_fallback(navigation_goal="Navigate to bills")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == ["view_bills_fallback"]
        # Dropped key removed from options, text_patterns, and the if-chain.
        assert "view_bills_fallback" not in new_code
        # Kept branch's identity is intact.
        assert '"bills"' in new_code
        # Fallback else: branch is preserved.
        assert "element_fallback" in new_code
        # Only one keyed arm remains; resulting code is still parseable.
        compile(new_code, "<test>", "exec")

    def test_five_arm_classify_with_four_na_duplicates_collapsed(self) -> None:
        """SKY-9439 reproducer: 5-arm classify, 4 N/A duplicates → keep the keyed branch only."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={
                        "bills_billing": "billing area",
                        "bills_billing_link": "alt billing 1",
                        "bills_billing_only": "alt billing 2",
                        "view_bills_fallback": "alt billing 3",
                        "view_bills_billing": "alt billing 4",
                    },
                    text_patterns={
                        "bills_billing": "Billing & Payments",
                        "bills_billing_link": "N/A",
                        "bills_billing_only": "N/A",
                        "view_bills_fallback": "N/A",
                        "view_bills_billing": "N/A",
                    },
                )
                if state == "bills_billing":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                elif state == "bills_billing_link":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                elif state == "bills_billing_only":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                elif state == "view_bills_fallback":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                elif state == "view_bills_billing":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                else:
                    await page.element_fallback(navigation_goal="Navigate to bills")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # All four N/A duplicates dropped; the keyed branch retains.
        assert set(dropped) == {
            "bills_billing_link",
            "bills_billing_only",
            "view_bills_fallback",
            "view_bills_billing",
        }
        for k in dropped:
            assert k not in new_code
        assert '"bills_billing"' in new_code
        # Resulting code still parses and the else: fallback is intact.
        compile(new_code, "<test>", "exec")
        assert "element_fallback" in new_code

    def test_byte_identical_with_identical_meaningful_text_patterns_collapsed(self) -> None:
        """Rule 6b's distinctness condition: identical patterns + duplicate actions = redundant.

        Both branches have the **same** non-empty text_pattern AND the same
        body. At runtime the second branch can never match a page that the
        first didn't already match — it's truly redundant, not a Rule 6b
        exception. Must be collapsed to prevent unbounded growth.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "Apply Now", "b": "Apply Now"},
                )
                if state == "a":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "b":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit form")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Same pattern + same body → truly redundant; drop the second.
        assert dropped == ["b"]
        assert 'state == "b"' not in new_code
        compile(new_code, "<test>", "exec")

    def test_byte_identical_with_identical_meaningful_url_patterns_collapsed(self) -> None:
        """Identical URL regex + identical body → redundant. Drop one."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "N/A", "b": "N/A"},
                    url_patterns={"a": "example\\\\.com/foo", "b": "example\\\\.com/foo"},
                )
                if state == "a":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "b":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == ["b"]
        compile(new_code, "<test>", "exec")

    def test_byte_identical_with_distinct_meaningful_patterns_preserved(self) -> None:
        """Rule 6b: distinct text_patterns with duplicate actions are explicitly allowed.

        Both branches carry meaningful, **different** text patterns.
        Collapsing them would drop a deterministic Tier-1 match surface for
        the second pattern. Per script-reviewer.j2:11 ("unless it has
        distinct text_patterns that improve page detection"), both branches
        must be preserved.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "Page A", "b": "Page B"},
                )
                if state == "a":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "b":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit form")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Both have signal → preserved; nothing dropped.
        assert dropped == []
        assert new_code == code

    def test_three_way_dedup_one_keyed_two_na_all_identical(self) -> None:
        """Three identical bodies (1 keyed + 2 N/A) → keep the keyed one, drop two."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"keyed": "real variant", "na1": "alt 1", "na2": "alt 2"},
                    text_patterns={"keyed": "Submit Application", "na1": "N/A", "na2": ""},
                )
                if state == "keyed":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "na1":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "na2":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert set(dropped) == {"na1", "na2"}
        assert '"keyed"' in new_code
        compile(new_code, "<test>", "exec")

    def test_drops_head_arm_promotes_next(self) -> None:
        """If the head ``if`` arm is dropped, the next ``elif`` becomes the new head."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"bad": "duplicate variant", "keep": "real variant"},
                    text_patterns={"bad": "N/A", "keep": "Real Page"},
                )
                if state == "bad":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                elif state == "keep":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                else:
                    await page.element_fallback(navigation_goal="X")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == ["bad"]
        assert 'state == "keep"' in new_code
        # The else fallback survives.
        assert "element_fallback" in new_code
        compile(new_code, "<test>", "exec")

    def test_two_arm_dedup_collapses_to_single_keyed_arm(self) -> None:
        """A 2-arm classify with both bodies identical and both N/A → keep first arm.

        This is the boundary case: the keeper selection always preserves at
        least one arm per signature group, so the post-consolidation chain
        retains exactly one keyed arm + the else fallback.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "N/A", "b": "N/A"},
                )
                if state == "a":
                    await page.click(selector='button', ai='fallback', prompt='click')
                elif state == "b":
                    await page.click(selector='button', ai='fallback', prompt='click')
                else:
                    await page.element_fallback(navigation_goal="X")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == ["b"]
        assert 'state == "a"' in new_code
        compile(new_code, "<test>", "exec")

    def test_distinct_signal_kinds_both_preserved(self) -> None:
        """Branches with distinct kinds of meaningful signal (URL vs text) are both kept.

        Per Rule 6b, distinct deterministic match surfaces with duplicate
        actions are explicitly allowed: each contributes a separate runtime
        path to the same handler. Collapsing to one would drop the other's
        deterministic match (e.g., a page that matches the URL regex but
        whose text doesn't include "Submit" would lose its Tier-0 hit).
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"text_only": "alt 1", "url_branch": "alt 2"},
                    text_patterns={"text_only": "Submit", "url_branch": "N/A"},
                    url_patterns={"text_only": "", "url_branch": "example\\\\.com/foo"},
                )
                if state == "text_only":
                    await page.click(selector='button:has-text("Go")', ai='fallback', prompt='go')
                elif state == "url_branch":
                    await page.click(selector='button:has-text("Go")', ai='fallback', prompt='go')
                else:
                    await page.element_fallback(navigation_goal="Go")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Both branches carry signal of different kinds → both preserved.
        assert dropped == []
        assert '"url_branch"' in new_code
        assert '"text_only"' in new_code
        assert "example" in new_code

    def test_invalid_url_regex_treated_as_no_url_signal(self) -> None:
        """A syntactically-non-empty but invalid-regex URL pattern provides no runtime signal.

        Runtime classify wraps ``re.search`` in ``try/except re.error`` and
        silently skips invalid patterns (real_skyvern_page_ai.py:598-610).
        Scoring that as URL signal would let consolidation prefer a branch
        whose URL regex never matches at runtime, regressing routing for the
        same page state.

        Here ``"(((unbalanced"`` is unparseable — the branch with text-only
        signal must win.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"bad_url": "alt 1", "text_only": "alt 2"},
                    text_patterns={"bad_url": "N/A", "text_only": "Submit Application"},
                    url_patterns={"bad_url": "(((unbalanced", "text_only": ""},
                )
                if state == "bad_url":
                    await page.click(selector='button:has-text("Go")', ai='fallback', prompt='go')
                elif state == "text_only":
                    await page.click(selector='button:has-text("Go")', ai='fallback', prompt='go')
                else:
                    await page.element_fallback(navigation_goal="Go")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # bad_url's regex is invalid → no URL signal. text_only has text signal.
        # text_only wins. (Without the regex check, bad_url would have falsely
        # outranked text_only because URL > text in the tier cascade.)
        assert dropped == ["bad_url"]
        assert '"text_only"' in new_code
        compile(new_code, "<test>", "exec")

    def test_branches_with_overlapping_signals_both_preserved(self) -> None:
        """Both branches carry meaningful signal (one text-only, one text+URL): both preserved.

        Even though the second branch's signal is "stronger" (URL plus text),
        dropping the first removes the deterministic Tier-1 match for pages
        whose URL doesn't match the regex but whose text does include
        "Apply". Rule 6b's allowance for distinct meaningful patterns
        applies regardless of relative signal strength.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"text_only": "alt 1", "both": "alt 2"},
                    text_patterns={"text_only": "Apply", "both": "Submit"},
                    url_patterns={"text_only": "", "both": "example\\\\.com/apply"},
                )
                if state == "text_only":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                elif state == "both":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                else:
                    await page.element_fallback(navigation_goal="X")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == []
        assert '"text_only"' in new_code
        assert '"both"' in new_code

    def test_unparseable_input_is_passthrough(self) -> None:
        """Malformed code passes through unchanged (defense-in-depth)."""
        bad = "async def block_fn(page, context\n    await page.classify("
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(bad)
        assert new_code == bad
        assert dropped == []

    def test_no_if_chain_after_classify_skipped(self) -> None:
        """If classify isn't followed by an if-chain on its var, skip consolidation."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "x", "b": "y"},
                    text_patterns={"a": "X", "b": "Y"},
                )
                await page.complete()
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert new_code == code
        assert dropped == []

    def test_non_literal_options_skipped(self) -> None:
        """If ``options=`` is a name reference (not a literal Dict), skip consolidation.

        Without this guard, the if-arms would be elided but the dropped keys
        would remain valid classify outputs — a behavioral regression: at
        runtime, ``state == "<dropped>"`` would never match and the dropped
        key's actions would be lost (execution would fall through to the
        ``else:`` fallback).
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                opts = {"bills": "billing area", "view_bills_fallback": "alt"}
                tps = {"bills": "Billing", "view_bills_fallback": "N/A"}
                state = await page.classify(options=opts, text_patterns=tps)
                if state == "bills":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                elif state == "view_bills_fallback":
                    await page.click(selector='a:has-text("View Bills")', ai='fallback', prompt='view bills')
                else:
                    await page.element_fallback(navigation_goal="Navigate to bills")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Non-literal options ⇒ no consolidation. Both arms preserved.
        assert dropped == []
        assert new_code == code
        assert "view_bills_fallback" in new_code

    def test_list_valued_text_patterns_meaningful_branch_kept(self) -> None:
        """``text_patterns: dict[str, str | list[str]]`` — list values carry a real signal.

        When one branch has list-valued patterns (with at least one non-empty
        element) and a duplicate-action sibling has ``"N/A"``, the list-valued
        branch must be kept, regardless of source order.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"weak": "duplicate variant", "strong": "real variant"},
                    text_patterns={"weak": "N/A", "strong": ["Apply Now", "Submit Application"]},
                )
                if state == "weak":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "strong":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # The list-valued (meaningful) branch is kept; the N/A is dropped even
        # though it appears first in source.
        assert dropped == ["weak"]
        assert '"strong"' in new_code
        assert "weak" not in new_code or 'state == "weak"' not in new_code
        compile(new_code, "<test>", "exec")

    def test_list_with_mixed_real_and_na_treated_as_no_signal(self) -> None:
        """List with one N-A element fails runtime ``all()`` matching → no signal.

        Runtime classify uses ``all(p in extracted_text for p in patterns)``
        (real_skyvern_page_ai.py:623-624). A mixed list (one real token + one
        N-A placeholder) cannot match the page text. Treating it as
        meaningful would let consolidation prefer it over a sibling with a
        clean string signal — biasing toward branches that are actually
        weaker at runtime.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"mixed": "duplicate variant", "clean": "real variant"},
                    text_patterns={"mixed": ["Apply Now", "N/A"], "clean": "Submit Application"},
                )
                if state == "mixed":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "clean":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # "mixed" has no signal (one element is N/A → list fails runtime all-match).
        # "clean" has a meaningful single string. "clean" wins.
        assert dropped == ["mixed"]
        assert '"clean"' in new_code
        compile(new_code, "<test>", "exec")

    def test_list_valued_all_empty_strings_treated_as_no_signal(self) -> None:
        """A list of empty/N-A strings carries no signal — falls back to source order."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"first": "x", "second": "y"},
                    text_patterns={"first": ["", "N/A"], "second": []},
                )
                if state == "first":
                    await page.click(selector='button', ai='fallback', prompt='click')
                elif state == "second":
                    await page.click(selector='button', ai='fallback', prompt='click')
                else:
                    await page.element_fallback(navigation_goal="x")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Neither branch has signal → keep first, drop second by source order.
        assert dropped == ["second"]
        compile(new_code, "<test>", "exec")

    def test_non_literal_text_patterns_still_consolidates(self) -> None:
        """Non-literal text_patterns is OK as long as ``options=`` is a literal Dict.

        ``text_patterns`` non-literal just means we cannot read pattern values
        for the keep-vs-drop preference — we fall back to source order. The
        rewrite is still semantically safe because keys are still removable
        from ``options`` and the if-chain.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                tps = {"a": "Apply", "b": "Submit"}
                state = await page.classify(options={"a": "alpha", "b": "beta"}, text_patterns=tps)
                if state == "a":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "b":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Bodies match; keep first by source order (no preference data available).
        assert dropped == ["b"]
        # Verify "b" is removed from options dict and the if-chain. Use the
        # specific runtime-relevant tokens — the bare letter "b" appears too
        # often in unrelated places (Python keywords, strings) to be a clean
        # negative assertion.
        assert '"b": "beta"' not in new_code
        assert 'state == "b"' not in new_code
        # Surviving branch is intact.
        assert '"a": "alpha"' in new_code
        assert 'state == "a"' in new_code
        compile(new_code, "<test>", "exec")

    def test_consolidator_skips_when_var_rebound_before_if_chain(self) -> None:
        """If ``var`` is rebound to a non-classify value between classify and the if-chain,
        skip consolidation (CORR-8). Otherwise we'd elide arms from an if-chain that
        isn't actually keyed on classify outputs.

        Concrete pattern: ``state = await page.classify(...)`` then
        ``state = result.get('status')`` then ``if state == "approved"`` —
        the if-arms test the rebound value, not classify's output. Their
        keys ("approved", "denied") are not in classify's options ({"a", "b"}),
        so the subset-of-options safety gate correctly skips this classify.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "Apply", "b": "Submit"},
                )
                state = "approved"
                if state == "approved":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                elif state == "denied":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                else:
                    await page.element_fallback(navigation_goal="X")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Arm keys ("approved", "denied") not in classify options {"a", "b"} → skip.
        # Both arms must be preserved even though their bodies are identical.
        assert dropped == []
        assert 'state == "approved"' in new_code
        assert 'state == "denied"' in new_code

    def test_consolidator_skips_when_var_rebound_to_overlapping_key(self) -> None:
        """Subset-of-options is not enough — rebinding can preserve key overlap.

        Concrete CORR-8 sharper-variant: classify with options ``{"a", "b"}``
        and the rebound value happens to also be ``"a"`` or ``"b"``. Subset
        gate alone would pass. Dataflow rebinding gate must catch this and
        skip consolidation.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "Apply", "b": "Submit"},
                )
                state = "a"
                if state == "a":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                elif state == "b":
                    await page.click(selector='button:has-text("X")', ai='fallback', prompt='x')
                else:
                    await page.element_fallback(navigation_goal="X")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Rebinding detected → skip. Both arms preserved despite identical bodies.
        assert dropped == []
        assert 'state == "a"' in new_code
        assert 'state == "b"' in new_code

    def test_consolidator_skips_when_body_references_classify_var(self) -> None:
        """If an arm body references the classify variable, refuse to consolidate (CORR-10).

        Two arms can have textually-identical bodies but different runtime
        effects when the body's behavior depends on the matched variable's
        value (logging, forwarding it onward, etc.). The example here is
        contrived but real: if a future block emits ``await page.click(prompt=f"go {state}")``
        in each arm, the *string* is identical but the runtime click-prompt
        differs based on which arm matched.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": "Apply", "b": "Submit"},
                )
                if state == "a":
                    await page.click(prompt=f"go to {state} branch", ai='fallback')
                elif state == "b":
                    await page.click(prompt=f"go to {state} branch", ai='fallback')
                else:
                    await page.element_fallback(navigation_goal="X")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Body references ``state`` → consolidation skipped. Both arms preserved.
        assert dropped == []
        assert 'state == "a"' in new_code
        assert 'state == "b"' in new_code

    def test_consolidator_does_not_collapse_branches_with_opaque_patterns(self) -> None:
        """Non-literal pattern values must NOT bucket together as identical (CORR-9).

        If both branches use name refs (``text_patterns={"a": tp_a, "b": tp_b}``),
        we cannot read the values to verify distinctness. We must NOT collapse
        them in Stage 1. Stage 2's all-no-signal path collapses them by source
        order, but only because they're truly indistinguishable to us — not
        because we proved them identical.

        Here we use distinct meaningful text strings for each branch but wrap
        them in name references the consolidator can't read. Old code would
        bucket both under ``(None, None)`` and drop the second; new code keeps
        them via opaque sentinels — they end up no-signal in Stage 2 which
        collapses to first. Asserting the no-signal Stage 2 outcome.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                tp_a = "Apply Now"
                tp_b = "Submit Application"
                state = await page.classify(
                    options={"a": "variant a", "b": "variant b"},
                    text_patterns={"a": tp_a, "b": tp_b},
                )
                if state == "a":
                    await page.click(selector='button:has-text("Go")', ai='fallback', prompt='go')
                elif state == "b":
                    await page.click(selector='button:has-text("Go")', ai='fallback', prompt='go')
                else:
                    await page.element_fallback(navigation_goal="Go")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        # Both opaque → Stage 2 all-no-signal path drops second by source order.
        # Crucially: it's NOT Stage 1 dropping them because they "looked
        # identical" — they're now in distinct opaque buckets.
        assert dropped == ["b"]
        compile(new_code, "<test>", "exec")

    def test_consolidation_with_intervening_statement_between_classify_and_if(self) -> None:
        """LLM may emit a temp variable between classify and the if-chain.

        ``_validate_classify_handling`` allows up to 8 such intervening
        statements; the consolidator must accept the same shape or it
        leaves duplicates uncollapsed for code that passed validation
        (COMP-7).
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"keyed": "real variant", "duplicate": "alt variant"},
                    text_patterns={"keyed": "Submit Application", "duplicate": "N/A"},
                )
                page_url = page.url
                if state == "keyed":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                elif state == "duplicate":
                    await page.click(selector='button:has-text("Submit")', ai='fallback', prompt='submit')
                else:
                    await page.element_fallback(navigation_goal="Submit")
            """
        )
        new_code, dropped = self.reviewer._consolidate_classify_duplicates(code)
        assert dropped == ["duplicate"]
        assert '"keyed"' in new_code
        assert "page_url = page.url" in new_code  # intervening line preserved
        assert 'state == "duplicate"' not in new_code
        compile(new_code, "<test>", "exec")


class TestCountClassifyBranches:
    """Tests for ``_count_classify_branches``."""

    def setup_method(self) -> None:
        self.reviewer = ScriptReviewer()

    def test_no_classify_returns_zero(self) -> None:
        assert self.reviewer._count_classify_branches("async def f(page, context):\n    pass\n") == 0

    def test_single_classify_three_options(self) -> None:
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "x", "b": "y", "c": "z"},
                    text_patterns={"a": "A", "b": "B", "c": "C"},
                )
                await page.complete()
            """
        )
        assert self.reviewer._count_classify_branches(code) == 3

    def test_two_classify_calls_summed(self) -> None:
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(options={"a": "x", "b": "y"}, text_patterns={"a": "A", "b": "B"})
                if state == "a":
                    other = await page.classify(options={"x": "p", "y": "q", "z": "r"}, text_patterns={"x": "X", "y": "Y", "z": "Z"})
                else:
                    await page.complete()
            """
        )
        assert self.reviewer._count_classify_branches(code) == 5

    def test_unparseable_returns_zero(self) -> None:
        assert self.reviewer._count_classify_branches("def broken(") == 0

    def test_non_literal_options_falls_back_to_if_arm_count(self) -> None:
        """When ``options=`` is a name reference, count via the if/elif arm shape."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                opts = {"a": "x", "b": "y", "c": "z"}
                state = await page.classify(options=opts, text_patterns={"a": "A", "b": "B", "c": "C"})
                if state == "a":
                    await page.complete()
                elif state == "b":
                    await page.complete()
                elif state == "c":
                    await page.complete()
                else:
                    await page.element_fallback(navigation_goal="x")
            """
        )
        # options non-literal → 0 from options. if-arm count = 3. max = 3.
        assert self.reviewer._count_classify_branches(code) == 3

    def test_extract_driven_if_arms_not_counted(self) -> None:
        """``status == 'approved'`` arms from ``page.extract()`` must not inflate the count.

        CORR-6: the reviewer prompt explicitly teaches non-classify branching
        on extract results (``status == 'approved'`` / ``status == 'denied'``).
        Counting those as classify arms would poison Rule-4 telemetry. The
        if-arm filter restricts to names known to be bound from
        ``page.classify(...)``.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                result = await page.extract(prompt='get status', schema={'type': 'object'})
                status = result.get('status')
                if status == "approved":
                    await page.complete()
                elif status == "denied":
                    await page.terminate(errors=["denied"])
                else:
                    await page.element_fallback(navigation_goal="x")
            """
        )
        # No classify call ⇒ options_count = 0. if_arm_count must also be 0
        # because ``status`` was not bound from classify.
        assert self.reviewer._count_classify_branches(code) == 0

    def test_variable_rebinding_clears_classify_status(self) -> None:
        """When a name previously bound to classify is reassigned to a non-classify value,
        subsequent if-arms testing that name MUST NOT be counted as classify branches.

        CORR-6 re-raise: without removing the name from ``_classify_vars`` on
        rebinding, a later ``if state == "approved"`` chain (driven by an
        extract result that happened to reuse the name) would inflate the
        count.
        """
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(options={"a": "x", "b": "y"}, text_patterns={"a": "A", "b": "B"})
                if state == "a":
                    await page.complete()
                elif state == "b":
                    await page.complete()
                else:
                    result = await page.extract(prompt='get status', schema={'type': 'object'})
                    state = result.get('status')
                    if state == "approved":
                        await page.complete()
                    elif state == "denied":
                        await page.terminate(errors=["denied"])
                    elif state == "pending":
                        await page.element_fallback(navigation_goal="x")
            """
        )
        # 2 classify arms (a, b). The 3 extract-driven arms (approved/denied/pending)
        # MUST NOT count even though they test the same name ``state``, because
        # ``state`` was rebound to a non-classify value.
        assert self.reviewer._count_classify_branches(code) == 2

    def test_extract_arms_alongside_classify_arms_only_classify_counted(self) -> None:
        """Mixed file: classify arms count, extract arms do not."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(options={"a": "x", "b": "y"}, text_patterns={"a": "A", "b": "B"})
                if state == "a":
                    await page.complete()
                elif state == "b":
                    await page.complete()
                else:
                    result = await page.extract(prompt='x', schema={'type': 'object'})
                    status = result.get('status')
                    if status == "approved":
                        await page.complete()
                    elif status == "denied":
                        await page.terminate(errors=["denied"])
            """
        )
        # 2 classify arms (a, b). The 2 extract-driven arms (approved, denied)
        # MUST NOT be counted. options_count = 2, if_arm_count = 2, max = 2.
        assert self.reviewer._count_classify_branches(code) == 2

    def test_options_and_if_arms_take_max(self) -> None:
        """When both signals are present, the max wins (they should agree in normal code)."""
        code = textwrap.dedent(
            """
            async def block_fn(page, context):
                state = await page.classify(
                    options={"a": "x", "b": "y"},
                    text_patterns={"a": "A", "b": "B"},
                )
                if state == "a":
                    await page.complete()
                elif state == "b":
                    await page.complete()
                else:
                    await page.element_fallback(navigation_goal="x")
            """
        )
        assert self.reviewer._count_classify_branches(code) == 2
