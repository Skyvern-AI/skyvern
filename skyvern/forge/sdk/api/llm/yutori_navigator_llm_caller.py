"""Yutori Navigator LLM Caller — extends Skyvern's LLMCaller for conversation management.

Manages multi-turn conversation history for the Yutori Navigator computer-use model.
Uses the Yutori SDK for screenshot encoding, key mapping, and coordinate conversion.
Routes API calls through the base class's _dispatch_llm_call() for artifact persistence,
cost tracking, and error handling.

Conversation format per https://docs.yutori.com/reference/navigator:
  user (task + screenshot) -> assistant (tool_calls) -> tool (result + url + screenshot) -> ...
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, TypedDict

import structlog
from yutori.navigator import format_stop_and_summarize, screenshot_to_data_url

from skyvern.forge.sdk.api.llm.api_handler_factory import LLMCaller
from skyvern.forge.sdk.models import Step
from skyvern.forge.sdk.schemas.tasks import Task

LOG = structlog.get_logger()


@dataclass
class NavigatorResponse:
    """Normalized response from Yutori Navigator for use in the agent loop."""

    content: str = ""
    finish_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """Each tool_call is {"id": ..., "function": {"name": ..., "arguments": ...}}"""


class PendingToolCall(TypedDict):
    id: str
    name: str
    arguments: str
    result: str | None


def _action_result_description(name: str, arguments_json: str) -> str:
    """Derive an action result description from the tool call, matching the SDK example.

    Used as a fallback when Skyvern's ActionResult has no data (e.g. browser actions).
    """
    try:
        args = json.loads(arguments_json)
    except (json.JSONDecodeError, TypeError):
        args = {}

    descs = {
        "left_click": "Clicked 1x with left",
        "double_click": "Clicked 2x with left",
        "triple_click": "Clicked 3x with left",
        "middle_click": "Clicked 1x with middle",
        "right_click": "Clicked 1x with right",
        "mouse_move": "Mouse moved and hovering",
        "mouse_down": "Mouse button pressed",
        "mouse_up": "Mouse button released",
        "drag": "Dragged successfully",
        "go_back": "Navigated back",
        "go_forward": "Navigated forward",
        "refresh": "Refreshed the page",
    }
    if name in descs:
        return descs[name]
    if name == "scroll":
        return f"Scrolled {args.get('direction', 'down')}"
    if name == "type":
        return f"Typed {len(args.get('text', ''))} characters"
    if name == "key_press":
        return f"Pressed key: {args.get('key', '')}"
    if name == "hold_key":
        return f"Held key: {args.get('key', '')}"
    if name == "goto_url":
        return f"Navigated to {args.get('url', '')}"
    if name == "wait":
        return f"Waited {args.get('duration', 5)}s"
    return f"Executed {name}"


class YutoriNavigatorLLMCaller(LLMCaller):
    """Yutori Navigator LLM caller extending Skyvern's LLMCaller base class.

    Manages multi-turn conversation via self.message_history (inherited).
    API calls route through the base class -> _dispatch_llm_call() -> _call_yutori_navigator()
    in api_handler_factory.py, giving us artifact persistence, cost tracking, and
    error handling for free.

    Pending tool calls are flushed into the message history at the start of each
    step via flush_pending_tool_results(), which appends descriptive results and
    the current screenshot.
    """

    def __init__(self, llm_key: str, screenshot_scaling_enabled: bool = False):
        super().__init__(llm_key, screenshot_scaling_enabled)
        self._conversation_initialized = False
        self._pending_tool_calls: list[PendingToolCall] = []
        self._task: Task | None = None

    def initialize_conversation(self, task: Task) -> None:
        """Initialize (or re-initialize) conversation. Resets history so retries start fresh."""
        self._task = task
        self.message_history = []
        self._pending_tool_calls = []
        self._conversation_initialized = True
        LOG.debug("Initialized Yutori Navigator conversation", task_id=task.task_id)

    def update_pending_result(self, tool_call_id: str, result: str | None) -> None:
        """Set the actual execution result for a pending tool call by tool_call_id."""
        for tool_call in self._pending_tool_calls:
            if tool_call["id"] == tool_call_id:
                tool_call["result"] = result
                return

    def flush_pending_tool_results(self, screenshot_bytes: bytes, current_url: str) -> None:
        """Flush all pending tool call results into the message history.

        Uses actual execution results when available (e.g. JS output from
        ExecuteJsAction), falls back to a descriptive string for actions
        without explicit results. The last tool call includes the screenshot.
        """
        if not self._pending_tool_calls:
            return

        data_url = screenshot_to_data_url(screenshot_bytes)
        image_content = {"type": "image_url", "image_url": {"url": data_url}}

        for i, tc in enumerate(self._pending_tool_calls):
            result_text = tc.get("result") or _action_result_description(tc["name"], tc["arguments"])
            result_text += f"\nCurrent URL: {current_url}"

            if i < len(self._pending_tool_calls) - 1:
                self.message_history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text,
                    }
                )
            else:
                self.message_history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": [
                            {"type": "text", "text": result_text},
                            image_content,
                        ],
                    }
                )

        self._pending_tool_calls = []

    def add_initial_message(self, screenshot_bytes: bytes) -> None:
        """Add the first user message with task description and screenshot."""
        if self.message_history:
            return  # Already initialized

        data_url = screenshot_to_data_url(screenshot_bytes)
        user_content: list[dict[str, Any]] = []
        if self._task:
            user_content.append({"type": "text", "text": f"Task: {self._task.navigation_goal or ''}"})
        user_content.append({"type": "image_url", "image_url": {"url": data_url}})
        self.message_history.append({"role": "user", "content": user_content})

    def add_stop_and_summarize(self, screenshot_bytes: bytes, current_url: str) -> None:
        """Append a stop-and-summarize user message for the last step.

        Flushes any pending tool call results first so the conversation stays valid,
        then appends a user message asking the model to summarize progress.
        """
        if not self.message_history:
            self.add_initial_message(screenshot_bytes)
        if self._pending_tool_calls:
            self.flush_pending_tool_results(screenshot_bytes, current_url)

        task_goal = self._task.navigation_goal if self._task else "the given task"
        data_url = screenshot_to_data_url(screenshot_bytes)
        stop_message = format_stop_and_summarize(task_goal)

        self.message_history.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": stop_message},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        )

    async def call(self, **kwargs: Any) -> Any:
        """Override to route through base class with message history."""
        return await super().call(
            use_message_history=True,
            raw_response=True,
            **kwargs,
        )

    async def generate_response(self, step: Step) -> NavigatorResponse:
        """Generate Navigator response and update conversation history.

        Returns a NavigatorResponse with normalized fields so callers don't need
        to handle dict vs object differences from the base class.
        """
        response = await self.call(step=step)

        # raw_response=True returns response.model_dump(exclude_none=True) — a dict
        if isinstance(response, dict):
            choice = response.get("choices", [{}])[0]
            message_data = choice.get("message", {})
            tool_calls_data = message_data.get("tool_calls") or []
            content = message_data.get("content") or ""
            finish_reason = choice.get("finish_reason")
            request_id = response.get("request_id")
        else:
            msg = response.choices[0].message
            content = msg.content or ""
            finish_reason = response.choices[0].finish_reason
            tool_calls_data = []
            if msg.tool_calls:
                tool_calls_data = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
            request_id = getattr(response, "request_id", None) or (
                response.model_extra.get("request_id") if getattr(response, "model_extra", None) else None
            )

        tool_names = [tc["function"]["name"] for tc in tool_calls_data]
        LOG.info(
            "Yutori Navigator response received",
            task_id=self._task.task_id if self._task else None,
            step_order=step.order,
            request_id=request_id,
            finish_reason=finish_reason,
            tool_calls=tool_names,
        )

        # Append assistant message to history
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls_data:
            assistant_msg["tool_calls"] = tool_calls_data
            self._pending_tool_calls = [
                {
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                    "result": None,
                }
                for tc in tool_calls_data
            ]
        self.message_history.append(assistant_msg)

        return NavigatorResponse(
            content=content,
            finish_reason=finish_reason,
            tool_calls=tool_calls_data,
        )
