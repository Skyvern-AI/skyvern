"""Skill protocol + registry for the v3 agentic reviewer.

A :class:`Skill` wraps one callable behind a tool-use schema. The agent loop
sees a list of skill schemas (Anthropic-style ``tools=[...]``), the LLM picks
one, the loop dispatches via the registry, the result feeds back as a
tool_result message.

The registry exists so:
- both mid-run and post-run agents can share the same skill implementations,
- terminal skills can be distinguished structurally (``is_terminal``),
- per-agent skill filters are easy to enforce (mid-run sees Interact skills;
  post-run sees Investigate-Artifacts skills).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Awaitable, Callable, Iterable

import structlog

LOG = structlog.get_logger()


# Per-skill execution timeout. Defends against runaway DB queries, slow
# Playwright evals, or hung HTTP calls. Looser than per-LLM-call timeout
# because some skills (live_get_dom on large pages, get_screenshots) are
# legitimately slow.
DEFAULT_SKILL_TIMEOUT_SECONDS = 15.0


class SkillError(Exception):
    """Raised by a skill handler when the operation fails recoverably.

    The agent loop catches this, formats the message as a tool_result with
    ``status=error``, and continues. Distinguishes intentional failures from
    bugs (which raise other exception types and propagate).
    """


@dataclasses.dataclass(frozen=True)
class SkillResult:
    """Structured response from a skill handler.

    ``status`` is one of ``ok``, ``error``, ``not_available``. The latter is
    used for cloud-only skills running in OSS / local-dev where the backing
    service is absent — agent prompt steers around it.
    """

    status: str
    data: Any | None = None
    error_message: str | None = None

    @classmethod
    def ok(cls, data: Any = None) -> SkillResult:
        return cls(status="ok", data=data)

    @classmethod
    def error(cls, message: str) -> SkillResult:
        return cls(status="error", error_message=message)

    @classmethod
    def not_available(cls, message: str) -> SkillResult:
        return cls(status="not_available", error_message=message)

    def to_tool_content(self) -> str:
        """Serialize for the tool_result content field.

        Returns a compact JSON-ish string. The agent prompt instructs the LLM
        that ``status`` is the primary signal — ``ok`` means the data is
        usable, ``error`` / ``not_available`` means it isn't.
        """
        import json

        payload: dict[str, Any] = {"status": self.status}
        if self.data is not None:
            payload["data"] = self.data
        if self.error_message is not None:
            payload["error"] = self.error_message
        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:
            return f"<<non-serializable skill result: status={self.status}>>"


@dataclasses.dataclass
class Skill:
    """Single tool-use skill.

    ``schema`` is a tool descriptor in Anthropic format (``name``,
    ``description``, ``input_schema``). The runtime emits this directly into
    the LLM call's ``tools=[...]`` parameter.

    ``handler`` receives a ``dict`` of LLM-supplied args plus a ``context``
    object that the registry passes through unchanged. The context shape
    differs by agent: mid-run passes :class:`FailureContext`, post-run passes
    :class:`PostRunContext`.

    ``is_terminal`` flags skills whose successful execution ends the agent
    loop. The agent loop respects this in two ways:
    - it doesn't add the terminal tool's result back to message history,
    - it produces a :class:`Decision` from the handler's return value.

    ``available_to`` is a coarse filter — ``{"midrun"}``, ``{"postrun"}``, or
    both. Enforced by the registry's ``for_agent_kind`` method.
    """

    name: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any], Any], Awaitable[SkillResult]]
    is_terminal: bool = False
    available_to: frozenset[str] = dataclasses.field(default_factory=lambda: frozenset({"midrun", "postrun"}))
    timeout_seconds: float = DEFAULT_SKILL_TIMEOUT_SECONDS

    async def execute(self, args: dict[str, Any], context: Any) -> SkillResult:
        try:
            return await asyncio.wait_for(self.handler(args, context), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            LOG.warning(
                "v3 skill timed out",
                skill_name=self.name,
                timeout_seconds=self.timeout_seconds,
            )
            return SkillResult.error(f"skill_timeout: {self.name} exceeded {self.timeout_seconds:.0f}s")
        except SkillError as exc:
            return SkillResult.error(str(exc))
        except Exception as exc:  # pragma: no cover — defensive, never crash the agent loop
            LOG.warning(
                "v3 skill raised unexpected exception",
                skill_name=self.name,
                exc_info=True,
            )
            return SkillResult.error(f"skill_exception: {type(exc).__name__}: {exc}")


class SkillRegistry:
    """Registry of skills available to one or both agents.

    Construct one registry, ``register()`` skills into it, then call
    :meth:`for_agent_kind` to get a filtered view appropriate for the agent.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"v3 skill {skill.name!r} already registered")
        self._skills[skill.name] = skill

    def register_many(self, skills: Iterable[Skill]) -> None:
        for skill in skills:
            self.register(skill)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def for_agent_kind(self, agent_kind: str) -> list[Skill]:
        """Return skills available to ``agent_kind`` (``midrun`` | ``postrun``)."""
        return [s for s in self._skills.values() if agent_kind in s.available_to]

    def tool_schemas(self, agent_kind: str) -> list[dict[str, Any]]:
        """Return the ``tools=`` descriptor list filtered by agent kind."""
        return [s.schema for s in self.for_agent_kind(agent_kind)]

    def terminal_names(self, agent_kind: str) -> frozenset[str]:
        return frozenset(s.name for s in self.for_agent_kind(agent_kind) if s.is_terminal)
