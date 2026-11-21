from __future__ import annotations

from typing import Literal, TypedDict

from skyvern.forge.sdk.agents.skills import (
    SkillAlreadyLoaded,
    SkillError,
    SkillLoaded,
    SkillLoadResult,
    SkillOwner,
    SkillReadResult,
    SkillRefusal,
    SkillResource,
    SkillSession,
    is_refusal,
)

LOAD_SKILL_DESCRIPTION = (
    "Returns the named skill's instructions as `body`, its `version`, `owner`, and `files`, the paths of its other "
    "files. Loading the same version again in this request returns `status: already_loaded` without the body. "
    "Errors carry `data.reason`: `unknown_skill` with `names`, the organization and workflow skills, or `withheld` "
    "when the skill was made from run data the user's role cannot read."
)
READ_SKILL_RESOURCE_DESCRIPTION = (
    "Returns the `text` of one file of the named skill, by its `path` as listed in `files`. Files under `scripts/` "
    "are returned as text and never run. Errors carry `data.reason`: `unknown_skill` with `names`, the organization "
    "and workflow skills, `unknown_file` with the skill's `files`, or `withheld` when the skill was made from run "
    "data the user's role cannot read."
)


class SkillErrorData(TypedDict, total=False):
    reason: SkillError
    names: list[str]
    files: list[str]


class SkillToolError(TypedDict):
    ok: Literal[False]
    error: str
    data: SkillErrorData


class SkillToolSuccess(TypedDict):
    ok: Literal[True]
    data: SkillLoaded | SkillAlreadyLoaded | SkillResource


SkillToolResult = SkillToolSuccess | SkillToolError


def _tool_error(refusal: SkillRefusal) -> SkillToolError:
    data = SkillErrorData(reason=refusal["error"])
    if "names" in refusal:
        data["names"] = refusal["names"]
    if "files" in refusal:
        data["files"] = refusal["files"]
    return SkillToolError(ok=False, error=refusal["message"], data=data)


def _tool_result(result: SkillLoadResult | SkillReadResult, owner: SkillOwner | None) -> SkillToolResult:
    if is_refusal(result):
        return _tool_error(result)
    data = result.copy()
    data["owner"] = owner
    return SkillToolSuccess(ok=True, data=data)


def load_skill(session: SkillSession, name: str) -> SkillToolResult:
    return _tool_result(session.load(name), session.owner(name))


def read_skill_resource(session: SkillSession, name: str, path: str) -> SkillToolResult:
    return _tool_result(session.read_resource(name, path), session.owner(name))
