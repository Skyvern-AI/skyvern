from skyvern.core.script_generations.transform_workflow_run import _process_action_for_block
from skyvern.webeye.actions.actions import TASK_V3_ACTION_DESCRIPTION_PREFIX, ClickAction


def test_a_v3_rows_display_label_never_reaches_code_generation() -> None:
    # A v3 row's intention is a timeline label, and code generation reads intention as the cached
    # script's AI prompt and as a field-naming hint. A pre-v3 row's intention is a real prompt.
    v3_row = ClickAction(
        element_id="a", description=f"{TASK_V3_ACTION_DESCRIPTION_PREFIX}click #a", intention="Clicked a button"
    )
    pre_v3_row = ClickAction(element_id="b", description="Click sign in", intention="Click the sign in button")

    assert _process_action_for_block(v3_row, {})["intention"] is None
    assert _process_action_for_block(pre_v3_row, {})["intention"] == "Click the sign in button"
