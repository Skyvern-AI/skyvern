from typing import Any

from pydantic import BaseModel

from skyvern.webeye.string_util import remove_whitespace


class ActionResult(BaseModel):
    success: bool
    exception_type: str | None = None
    exception_message: str | None = None
    data: dict[str, Any] | list | str | None = None
    step_retry_number: int | None = None
    step_order: int | None = None
    javascript_triggered: bool = False
    download_triggered: bool | None = None
    # None is used for old data so that we can differentiate between old and new data which only has boolean
    interacted_with_sibling: bool | None = None
    interacted_with_parent: bool | None = None

    def __str__(self) -> str:
        results = ["ActionResult(success={self.success}"]
        if self.exception_type or self.exception_message:
            results.append(f"exception_type={self.exception_type}")
            results.append(f"exception_message={self.exception_message}")
        if self.data:
            results.append(f"data={self.data}")
        if self.step_order:
            results.append(f"step_order={self.step_order}")
        if self.step_retry_number:
            results.append(f"step_retry_number={self.step_retry_number}")
        if self.javascript_triggered:
            results.append(f"javascript_triggered={self.javascript_triggered}")
        if self.download_triggered is not None:
            results.append(f"download_triggered={self.download_triggered}")
        if self.interacted_with_sibling is not None:
            results.append(f"interacted_with_sibling={self.interacted_with_sibling}")
        if self.interacted_with_parent is not None:
            results.append(f"interacted_with_parent={self.interacted_with_parent}")

        return ", ".join(results) + ")"

    def __repr__(self) -> str:
        return self.__str__()


class ActionSuccess(ActionResult):
    def __init__(
        self,
        data: dict[str, Any] | list | str | None = None,
        javascript_triggered: bool = False,
        download_triggered: bool | None = None,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
    ):
        super().__init__(
            success=True,
            data=data,
            javascript_triggered=javascript_triggered,
            download_triggered=download_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
        )


class ActionFailure(ActionResult):
    def __init__(
        self,
        exception: Exception,
        javascript_triggered: bool = False,
        download_triggered: bool | None = None,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
    ):
        super().__init__(
            success=False,
            exception_type=type(exception).__name__,
            exception_message=remove_whitespace(str(exception)),
            javascript_triggered=javascript_triggered,
            download_triggered=download_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
        )


# TODO: action is aborted. but action chains need to be continued in forge/agent.agent_step
# so set success to True for right now.
class ActionAbort(ActionResult):
    def __init__(
        self,
        javascript_triggered: bool = False,
        download_triggered: bool | None = None,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
    ):
        super().__init__(
            success=True,
            javascript_triggered=javascript_triggered,
            download_triggered=download_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
        )
