"""Which browser tool surface the v3 loop offers, and what each state advertises.

The surface is resolved once per run and applied at the single point where the tool list is
assembled, so bare tasks and workflow blocks cannot diverge.
"""

from __future__ import annotations

from enum import StrEnum

import structlog

from skyvern.config import settings
from skyvern.forge.taskv3.loop import CODE_TOOL_NAME, ToolSpec

# What `replace` keeps. Perception and waiting survive because code that cannot see the page or wait
# for it is not a smaller surface, it is a blind one; the tools that ACT are the ones the code tool
# subsumes. Authentication and finish are assembled elsewhere and are out of this filter's reach.
LOG = structlog.get_logger()

RETAINED_TOOL_NAMES = frozenset({"observe", "get_html", "look", "wait"})


class CodeToolSurface(StrEnum):
    OFF = "off"
    ADD = "add"
    REPLACE = "replace"

    @property
    def offers_code_tool(self) -> bool:
        return self is not CodeToolSurface.OFF


def configured_surface() -> CodeToolSurface:
    """The configured surface, or OFF for any value this build does not recognise.

    Pydantic rejects an unknown env value at startup, so this guard is for a programmatic override
    reaching a build whose enum is older than the value it was given. OFF is the state that changes
    nothing, which is the right answer to a state this build cannot honour.
    """
    try:
        return CodeToolSurface(settings.TASK_V3_CODE_TOOL_SURFACE)
    except ValueError:
        return CodeToolSurface.OFF


def apply_surface(tools: list[ToolSpec], surface: CodeToolSurface, code_tool: ToolSpec | None) -> list[ToolSpec]:
    """The tool list a surface advertises, given the code tool the deployment could build.

    `code_tool` is None whenever the sandboxed runner is unavailable. A withheld code tool does NOT
    degrade `replace` into a surface with no way to act: the action tools stay, because a run that
    can perceive and wait but never act is a guaranteed failure produced by the withholding itself.
    """
    if surface is CodeToolSurface.OFF or code_tool is None:
        return list(tools)
    if code_tool.name != CODE_TOOL_NAME:
        # The submit guard decides "this call may have submitted a form" by matching the tool's NAME,
        # and it lives in a different module from whoever builds the tool. A differently-named tool
        # is one that guard cannot see, so it is refused rather than advertised -- the agreement
        # between the two is enforced here instead of left as a convention.
        LOG.error("taskv3 code tool refused", reason="unexpected_name", name=code_tool.name)
        return list(tools)
    kept = (
        [tool for tool in tools if tool.name in RETAINED_TOOL_NAMES]
        if surface is CodeToolSurface.REPLACE
        else list(tools)
    )
    # A page-mutating tool the loop cannot see as billable is invisible to the stall detector, the
    # no-progress streak and the settle probe, so this is asserted here rather than trusted from the
    # deployment that built it -- the same place the native action tools get the flag.
    #
    # One flag is the coarse answer, not the finished one: a single call performing many brokered
    # page operations still counts as one action, and a call that only computes counts as one too.
    # The accounting unit for a tool whose body the harness cannot see is the brokered operation,
    # not the call, and that belongs with the executor that introduces those operations.
    code_tool.billable = True
    return kept + [code_tool]
