from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, TypeAlias
from urllib.parse import urlparse

from skyvern.forge.sdk.schemas.task_v2 import TaskV2
from skyvern.forge.sdk.schemas.tasks import Task, TaskResponse
from skyvern.forge.sdk.services.request_principal import RequestPrincipal
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunResponseBase
from skyvern.schemas.runs import TaskRunResponse, WorkflowRunResponse
from skyvern.webeye.schemas import BrowserSessionResponse

if TYPE_CHECKING:
    from skyvern.forge.agent_functions import AgentFunction

BrowserSessionAddressResponse: TypeAlias = (
    BrowserSessionResponse
    | WorkflowRun
    | Task
    | TaskResponse
    | WorkflowRunResponseBase
    | TaskRunResponse
    | WorkflowRunResponse
    | TaskV2
)

_CDP_PATH_CREDENTIAL_MARKERS = ("apikey", "api-key", "api_key", "key", "token")
_CDP_DISCOVERY_SUFFIXES = ("/json/version", "/json/list", "/json")


async def redact_browser_session_addresses(
    responses: Sequence[BrowserSessionAddressResponse],
    organization_id: str,
    agent_function: AgentFunction,
    principal: RequestPrincipal | None,
    *,
    task_v2_browser_session_id: str | None = None,
) -> None:
    def router_address_session_ids(browser_address: str | None) -> set[str]:
        if not isinstance(browser_address, str) or not browser_address:
            return set()
        try:
            parsed = urlparse(browser_address)
        except ValueError:
            return set()
        if parsed.scheme not in {"ws", "wss", "http", "https"}:
            return set()
        path = parsed.path[:-1] if parsed.path.endswith("/") and parsed.path != "/" else parsed.path
        for suffix in _CDP_DISCOVERY_SUFFIXES:
            if path == suffix or path.endswith(suffix):
                path = path[: -len(suffix)] or "/"
                break
        segments = [segment for segment in path.split("/") if segment]
        if (
            len(segments) == 5
            and segments[0].startswith("pbs_")
            and segments[1]
            and segments[2:4] == ["devtools", "browser"]
        ):
            session_ids = {segments[0]}
            if segments[-1].startswith("pbs_"):
                session_ids.add(segments[-1])
            return session_ids
        credential_segment = next(
            (
                index + 1
                for index, segment in enumerate(segments[:-1])
                if segment.lower() in _CDP_PATH_CREDENTIAL_MARKERS
            ),
            None,
        )
        if not segments or credential_segment == len(segments) - 1:
            return set()
        session_id = segments[-1]
        return {session_id} if session_id.startswith("pbs_") else set()

    def response_browser_addresses(
        response: BrowserSessionAddressResponse,
    ) -> list[str | None]:
        if isinstance(response, TaskResponse):
            return [response.request.browser_address]
        if isinstance(response, (TaskRunResponse, WorkflowRunResponse)):
            return [response.run_request.browser_address if response.run_request else None]
        if isinstance(response, WorkflowRunResponseBase):
            return [response.browser_address, response.task_v2.browser_address if response.task_v2 else None]
        return [response.browser_address]

    def response_declared_session_ids(response: BrowserSessionAddressResponse) -> set[str]:
        if isinstance(response, TaskV2):
            return {task_v2_browser_session_id} if task_v2_browser_session_id else set()
        if isinstance(response, (TaskRunResponse, WorkflowRunResponse)):
            session_ids = {response.browser_session_id} if response.browser_session_id else set()
            if response.run_request and response.run_request.browser_session_id:
                session_ids.add(response.run_request.browser_session_id)
            return session_ids
        if isinstance(response, TaskResponse):
            return {response.browser_session_id} if response.browser_session_id else set()
        return {response.browser_session_id} if response.browser_session_id else set()

    def response_session_ids(
        response: BrowserSessionAddressResponse,
    ) -> set[str]:
        addresses = response_browser_addresses(response)
        if not any(isinstance(address, str) and address for address in addresses) and not (
            isinstance(response, BrowserSessionResponse) and response.vnc_streaming_supported
        ):
            return set()
        session_ids = response_declared_session_ids(response)
        for address in addresses:
            session_ids.update(router_address_session_ids(address))
        return session_ids

    def response_has_unresolved_address(response: BrowserSessionAddressResponse) -> bool:
        declared_session_ids = response_declared_session_ids(response)
        for address in response_browser_addresses(response):
            if not isinstance(address, str) or not address:
                continue
            session_ids = router_address_session_ids(address)
            if not session_ids or (declared_session_ids and not declared_session_ids.intersection(session_ids)):
                return True
        return False

    response_ids = [
        (response, response_session_ids(response), response_has_unresolved_address(response)) for response in responses
    ]
    session_ids = set().union(*(session_ids for _, session_ids, _ in response_ids))
    connectable_ids = (
        await agent_function.browser_session_ids_authorized_for_connect(principal, organization_id, session_ids)
        if session_ids
        else set()
    )
    unresolved_requires_redaction = (
        await agent_function.should_redact_unresolved_browser_session_address(principal, organization_id)
        if any(unresolved for _, _, unresolved in response_ids)
        else False
    )
    for response, session_ids, unresolved in response_ids:
        if (not session_ids or session_ids <= connectable_ids) and not (unresolved and unresolved_requires_redaction):
            continue
        if isinstance(response, (BrowserSessionResponse, WorkflowRun, Task, WorkflowRunResponseBase, TaskV2)):
            response.browser_address = None
            if isinstance(response, BrowserSessionResponse):
                response.vnc_streaming_supported = False
                response.browser_address_redacted = True
            elif isinstance(response, WorkflowRunResponseBase) and response.task_v2:
                response.task_v2.browser_address = None
        elif isinstance(response, TaskResponse):
            response.request.browser_address = None
        elif isinstance(response, (TaskRunResponse, WorkflowRunResponse)) and response.run_request:
            response.run_request.browser_address = None
