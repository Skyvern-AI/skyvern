from typing import Any

from pydantic import BaseModel

from skyvern.webeye.string_util import remove_whitespace


class ActionResult(BaseModel):
    success: bool
    stop_execution_on_failure: bool = True
    exception_type: str | None = None
    exception_message: str | None = None
    data: dict[str, Any] | list | str | None = None
    step_retry_number: int | None = None
    step_order: int | None = None
    download_triggered: bool | None = None
    upload_file_triggered: bool | None = None
    needs_followup: bool | None = None
    followup_message: str | None = None
    downloaded_files: list[str] | None = None  # Actual file names that were downloaded
    # None is used for old data so that we can differentiate between old and new data which only has boolean
    interacted_with_sibling: bool | None = None
    interacted_with_parent: bool | None = None
    skip_remaining_actions: bool | None = None
    tool_call_result: dict[str, Any] | None = None
    # Set by an action setup that physically performed the interaction, as opposed to one that
    # suppressed it because the control already held the desired state. Both shapes stop the raw
    # click, so callers mirroring the setup contract have no other way to tell them apart.
    setup_performed: bool = False
    # Observational commit evidence for an autocomplete selection: the clicked option's label and
    # the control value observed immediately afterward. Set together only when both are
    # boundary-delimited fragments of the option label (a selection-specific transition); both stay
    # None on a no-op, unrelated transform, ambiguous, failed, or secret selection.
    committed_option: str | None = None
    committed_value: str | None = None

    def __str__(self) -> str:
        results = [f"ActionResult(success={self.success}"]
        if self.exception_type or self.exception_message:
            results.append(f"exception_type={self.exception_type}")
            results.append(f"exception_message={self.exception_message}")
        if self.data:
            results.append(f"data={self.data}")
        if self.step_order:
            results.append(f"step_order={self.step_order}")
        if self.step_retry_number:
            results.append(f"step_retry_number={self.step_retry_number}")
        if self.download_triggered is not None:
            results.append(f"download_triggered={self.download_triggered}")
        if self.upload_file_triggered is not None:
            results.append(f"upload_file_triggered={self.upload_file_triggered}")
        if self.needs_followup is not None:
            results.append(f"needs_followup={self.needs_followup}")
        if self.followup_message is not None:
            results.append(f"followup_message={self.followup_message}")
        if self.downloaded_files is not None:
            results.append(f"downloaded_files={self.downloaded_files}")
        if self.interacted_with_sibling is not None:
            results.append(f"interacted_with_sibling={self.interacted_with_sibling}")
        if self.interacted_with_parent is not None:
            results.append(f"interacted_with_parent={self.interacted_with_parent}")
        if self.skip_remaining_actions is not None:
            results.append(f"skip_remaining_actions={self.skip_remaining_actions}")
        if self.committed_option is not None:
            results.append(f"committed_option={self.committed_option}")
        if self.committed_value is not None:
            results.append(f"committed_value={self.committed_value}")

        return ", ".join(results) + ")"

    def __repr__(self) -> str:
        return self.__str__()


class ActionSuccess(ActionResult):
    def __init__(
        self,
        data: dict[str, Any] | list | str | None = None,
        download_triggered: bool | None = None,
        downloaded_files: list[str] | None = None,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
        setup_performed: bool = False,
        committed_option: str | None = None,
        committed_value: str | None = None,
    ):
        super().__init__(
            success=True,
            data=data,
            download_triggered=download_triggered,
            downloaded_files=downloaded_files,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
            setup_performed=setup_performed,
            committed_option=committed_option,
            committed_value=committed_value,
        )


class ActionFailure(ActionResult):
    def __init__(
        self,
        exception: Exception,
        stop_execution_on_failure: bool = True,
        download_triggered: bool | None = None,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
        setup_performed: bool = False,
    ):
        super().__init__(
            success=False,
            exception_type=type(exception).__name__,
            stop_execution_on_failure=stop_execution_on_failure,
            exception_message=remove_whitespace(str(exception)),
            download_triggered=download_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
            setup_performed=setup_performed,
        )


# TODO: action is aborted. but action chains need to be continued in forge/agent.agent_step
# so set success to True for right now.
class ActionAbort(ActionResult):
    def __init__(
        self,
        download_triggered: bool | None = None,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
        setup_performed: bool = False,
    ):
        super().__init__(
            success=True,
            download_triggered=download_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
            setup_performed=setup_performed,
        )


# The tool-result text a tool caller (e.g. Yutori Navigator) must see when an action was skipped
# because its target went stale: the action did NOT run, so the model must re-observe and re-plan
# rather than assume it executed. Generic, carries no customer or DOM text.
STALE_TARGET_TOOL_RESULT = (
    "The target element became stale before this action ran, so it was NOT executed. "
    "Re-observe the current page and re-plan from what is visible now; do not assume this action ran."
)


class StaleActionAbort(ActionAbort):
    """A batched action that was NOT executed because its target went stale -- remounted by a preceding
    action in the same batch -- and could not be safely remapped. It is an ActionAbort so the batch
    still stops and the action persists as ``skipped``, but tool callers must be told the action did not
    execute and the page must be re-observed, never that it succeeded (see STALE_TARGET_TOOL_RESULT)."""
