from __future__ import annotations

from typing import Any

from ._common import ErrorCode, make_error, make_result


def validate_folder_id(folder_id: str, action: str) -> dict[str, Any] | None:
    if "/" in folder_id or "\\" in folder_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "folder_id must not contain path separators",
                "Provide a valid folder ID (starts with fld_)",
            ),
        )
    if not folder_id.startswith("fld_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid folder_id format: {folder_id!r}",
                "Folder IDs start with fld_. Use skyvern_folder_list to find valid IDs.",
            ),
        )
    return None


def validate_workflow_id(workflow_id: str, action: str) -> dict[str, Any] | None:
    if "/" in workflow_id or "\\" in workflow_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "workflow_id must not contain path separators",
                "Provide a valid workflow permanent ID (starts with wpid_)",
            ),
        )
    if not workflow_id.startswith("wpid_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid workflow_id format: {workflow_id!r}",
                "Workflow IDs start with wpid_. Use skyvern_workflow_list to find valid IDs.",
            ),
        )
    return None


def validate_run_id(run_id: str, action: str) -> dict[str, Any] | None:
    if "/" in run_id or "\\" in run_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "run_id must not contain path separators",
                "Provide a valid run ID (starts with wr_ or tsk_v2_)",
            ),
        )
    if not run_id.startswith("wr_") and not run_id.startswith("tsk_v2_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid run_id format: {run_id!r}",
                "Run IDs start with wr_ (workflow runs) or tsk_v2_ (task runs). Check skyvern_workflow_run output.",
            ),
        )
    return None


def validate_workflow_run_id(workflow_run_id: str, action: str) -> dict[str, Any] | None:
    """Accept only wr_-prefixed IDs. The browser-profile-create source
    lookup is not compatible with tsk_v2_ IDs, so a tsk_v2_ ID must not
    slip through client-side validation."""
    if "/" in workflow_run_id or "\\" in workflow_run_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "workflow_run_id must not contain path separators",
                "Provide a valid workflow run ID (starts with wr_)",
            ),
        )
    if not workflow_run_id.startswith("wr_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid workflow_run_id format: {workflow_run_id!r}",
                "Workflow run IDs start with wr_. Task-v2 IDs (tsk_v2_) are not accepted here. Use skyvern_workflow_status or skyvern_workflow_run output to find the wr_ ID.",
            ),
        )
    return None


def validate_browser_profile_id(browser_profile_id: str, action: str) -> dict[str, Any] | None:
    if "/" in browser_profile_id or "\\" in browser_profile_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "browser_profile_id must not contain path separators",
                "Provide a valid browser profile ID (starts with bp_)",
            ),
        )
    if not browser_profile_id.startswith("bp_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid browser_profile_id format: {browser_profile_id!r}",
                "Browser profile IDs start with bp_. Use skyvern_browser_profile_list to find valid IDs.",
            ),
        )
    return None


def validate_script_id(script_id: str, action: str) -> dict[str, Any] | None:
    if "/" in script_id or "\\" in script_id:
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                "script_id must not contain path separators",
                "Provide a valid script ID (starts with s_)",
            ),
        )
    if not script_id.startswith("s_"):
        return make_result(
            action,
            ok=False,
            error=make_error(
                ErrorCode.INVALID_INPUT,
                f"Invalid script_id format: {script_id!r}",
                "Script IDs start with s_. Use skyvern_script_list_for_workflow to find script IDs.",
            ),
        )
    return None
