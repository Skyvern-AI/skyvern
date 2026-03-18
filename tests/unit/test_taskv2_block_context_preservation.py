from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext


def test_loop_internal_state_dropped_by_naive_context_replacement():
    """Demonstrates the bug: creating a new context without carrying loop_internal_state drops it."""
    original_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_1",
        run_id="wr_1",
        loop_internal_state={"downloaded_file_signatures_before_iteration": [("a.pdf", "abc", "https://files/a.pdf")]},
    )
    skyvern_context.set(original_context)

    # Simulate TaskV2Block.finally creating a new context WITHOUT preserving loop state
    context = skyvern_context.current()
    new_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_1",
        run_id=context.run_id,
    )
    skyvern_context.set(new_context)

    result_context = skyvern_context.current()
    # BUG: loop_internal_state is None
    assert result_context.loop_internal_state is None

    skyvern_context.reset()


def test_loop_internal_state_preserved_after_fix():
    """After fix: new context preserves loop_internal_state from old context."""
    original_state = {"downloaded_file_signatures_before_iteration": [("a.pdf", "abc", "https://files/a.pdf")]}
    original_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_1",
        run_id="wr_1",
        loop_internal_state=original_state,
    )
    skyvern_context.set(original_context)

    # Simulate the FIXED TaskV2Block.finally
    context = skyvern_context.current()
    loop_state = context.loop_internal_state if context else None
    new_context = SkyvernContext(
        organization_id="org_1",
        workflow_run_id="wr_1",
        run_id=context.run_id,
        loop_internal_state=loop_state,
    )
    skyvern_context.set(new_context)

    result_context = skyvern_context.current()
    assert result_context.loop_internal_state is not None
    assert result_context.loop_internal_state == original_state

    skyvern_context.reset()
