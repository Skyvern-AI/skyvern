"""Skyvern MCP agent skill tools: list, read and delete an organization's stored skills (Skyvern Cloud only)."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field

from skyvern.client.errors import NotFoundError

from ._common import ErrorCode, Timer, make_error, make_result, raw_http_delete, raw_http_get

_NOT_AVAILABLE = "agent skills are not available on this Skyvern server"
_WORKFLOW_ID = Annotated[
    str | None,
    Field(description="Workflow permanent ID (wpid_...) of a workflow skill. Omit for an org-wide skill."),
]


def _skill_path(name: str) -> str:
    return f"api/v1/agent_skills/{quote(name, safe='')}"


def _scope(workflow_permanent_id: str | None) -> dict[str, str]:
    return {"workflow_permanent_id": workflow_permanent_id} if workflow_permanent_id is not None else {}


def _skill_not_found(name: str, workflow_permanent_id: str | None, version: int | None = None) -> str:
    skill = f"Skill {name!r}" + (f" version {version}" if version is not None else "")
    where = (
        f" on workflow {workflow_permanent_id!r}, the workflow does not exist"
        if workflow_permanent_id is not None
        else ""
    )
    return f"{skill} not found{where}, or {_NOT_AVAILABLE}."


def _failure(action: str, timer: Timer, code: str, message: str, hint: str) -> dict[str, Any]:
    return make_result(action, ok=False, timing_ms=timer.timing_ms, error=make_error(code, message, hint))


async def skyvern_agent_skill_list(
    workflow_permanent_id: Annotated[
        str | None, Field(description="Workflow permanent ID (wpid_...) to list only that workflow's skills.")
    ] = None,
) -> dict[str, Any]:
    """List the organization's stored agent skills (name, description, version, author), without file text.
    Without workflow_permanent_id it returns org-wide skills plus the skills of every workflow you can see; with it,
    that workflow's skills only. Covers stored skills only. Needs Skyvern Cloud.
    """
    action = "skyvern_agent_skill_list"
    with Timer() as timer:
        try:
            skills = await raw_http_get("api/v1/agent_skills", params=_scope(workflow_permanent_id))
            timer.mark("http")
        except NotFoundError:
            if workflow_permanent_id is not None:
                message = f"Workflow {workflow_permanent_id!r} not found, or {_NOT_AVAILABLE}."
                return _failure(
                    action, timer, ErrorCode.API_ERROR, message, "Use skyvern_workflow_list to find workflow IDs."
                )
            message = "Agent skills are not available on this Skyvern server."
            return _failure(action, timer, ErrorCode.API_ERROR, message, "Agent skills need Skyvern Cloud.")
        except Exception as e:
            return _failure(action, timer, ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection.")
    return make_result(action, data={"skills": skills, "count": len(skills)}, timing_ms=timer.timing_ms)


async def skyvern_agent_skill_get(
    name: Annotated[str, Field(description="Skill name, as returned by skyvern_agent_skill_list", min_length=1)],
    workflow_permanent_id: _WORKFLOW_ID = None,
    version: Annotated[int | None, Field(description="Version to read. Omit for the current version.", ge=1)] = None,
    path: Annotated[str, Field(description="File in the skill to return", min_length=1)] = "SKILL.md",
) -> dict[str, Any]:
    """Read one file of a stored agent skill, plus its file list, author and version history. Covers stored skills
    only. Text of a curator-written version is withheld unless your role may read workflow runs. Needs Skyvern Cloud.
    """
    action = "skyvern_agent_skill_get"
    params: dict[str, str | int] = _scope(workflow_permanent_id) | ({"version": version} if version is not None else {})
    with Timer() as timer:
        try:
            skill = await raw_http_get(_skill_path(name), params=params)
            timer.mark("http")
        except NotFoundError:
            return _failure(
                action,
                timer,
                ErrorCode.INVALID_INPUT,
                _skill_not_found(name, workflow_permanent_id, version),
                "Use skyvern_agent_skill_list to find skill names.",
            )
        except Exception as e:
            return _failure(action, timer, ErrorCode.API_ERROR, str(e), "Check your API key and Skyvern connection.")

    files: dict[str, str] | None = skill.pop("files")
    data = {**skill, "file_paths": sorted(files) if files is not None else None, "file": None}
    if files is not None:
        if path not in files:
            return _failure(
                action,
                timer,
                ErrorCode.INVALID_INPUT,
                f"File {path!r} is not in skill {name!r}. Files: {', '.join(sorted(files))}",
                "Pass one of the listed file paths as path.",
            )
        data["file"] = {"path": path, "text": files[path]}
    return make_result(action, data=data, timing_ms=timer.timing_ms)


async def skyvern_agent_skill_delete(
    name: Annotated[str, Field(description="Skill name to delete", min_length=1)],
    workflow_permanent_id: _WORKFLOW_ID = None,
) -> dict[str, Any]:
    """Delete a stored agent skill and every one of its versions. Needs Skyvern Cloud."""
    action = "skyvern_agent_skill_delete"
    with Timer() as timer:
        try:
            await raw_http_delete(_skill_path(name), params=_scope(workflow_permanent_id))
            timer.mark("http")
        except NotFoundError:
            return _failure(
                action,
                timer,
                ErrorCode.INVALID_INPUT,
                _skill_not_found(name, workflow_permanent_id),
                "Use skyvern_agent_skill_list to find skill names.",
            )
        except Exception as e:
            return _failure(action, timer, ErrorCode.API_ERROR, str(e), "Check your API key and your role.")
    return make_result(
        action,
        data={"deleted": name, "workflow_permanent_id": workflow_permanent_id},
        timing_ms=timer.timing_ms,
    )
