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
    # None is used for old data so that we can differentiate between old and new data which only has boolean
    interacted_with_sibling: bool | None = None
    interacted_with_parent: bool | None = None

    def __str__(self) -> str:
        return (
            f"ActionResult(success={self.success}, exception_type={self.exception_type}, "
            f"exception_message={self.exception_message}), data={self.data}"
        )

    def __repr__(self) -> str:
        return self.__str__()


class ActionSuccess(ActionResult):
    def __init__(
        self,
        data: dict[str, Any] | list | str | None = None,
        javascript_triggered: bool = False,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
    ):
        super().__init__(
            success=True,
            data=data,
            javascript_triggered=javascript_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
        )


class ActionFailure(ActionResult):
    def __init__(
        self,
        exception: Exception,
        javascript_triggered: bool = False,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
    ):
        super().__init__(
            success=False,
            exception_type=type(exception).__name__,
            exception_message=remove_whitespace(str(exception)),
            javascript_triggered=javascript_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
        )


# TODO: action is aborted. but action chains need to be continued in forge/agent.agent_step
# so set success to True for right now.
class ActionAbort(ActionResult):
    def __init__(
        self,
        javascript_triggered: bool = False,
        interacted_with_sibling: bool = False,
        interacted_with_parent: bool = False,
    ):
        super().__init__(
            success=True,
            javascript_triggered=javascript_triggered,
            interacted_with_sibling=interacted_with_sibling,
            interacted_with_parent=interacted_with_parent,
        )
