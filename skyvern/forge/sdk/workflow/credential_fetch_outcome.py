import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, TypeAlias

import aiohttp
import structlog
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from fastapi import HTTPException
from google.api_core.exceptions import GoogleAPICallError, RetryError

from skyvern.exceptions import (
    BitwardenAccessDeniedError,
    CredentialItemNotFoundError,
    CredentialParameterNotFoundError,
    CredentialSourceNotConfiguredError,
    CredentialVaultNotConfiguredError,
    DisabledBlockExecutionError,
    HttpException,
    OnePasswordRateLimitError,
    OnePasswordServiceUnavailableError,
    OnePasswordSessionExpiredError,
    RuntimeSequentialCredentialUnsupported,
)
from skyvern.forge.sdk.schemas.credentials import CredentialVaultType
from skyvern.forge.sdk.services.credential.custom_credential_vault_service import CustomCredentialConfigurationError
from skyvern.forge.sdk.workflow.models.parameter import (
    Parameter,
    ParameterType,
    WorkflowParameter,
    WorkflowParameterType,
)

LOG = structlog.get_logger()

# One line per logical credential read on the Run path, logged by the layer that owns its last retry
# and fallback, so a read those recover logs "succeeded". Dashboards count by outcome: keep it closed.
RUN_CREDENTIAL_FETCH_FINISHED_MESSAGE = "Run credential fetch finished"
CredentialFetchOutcome = Literal["succeeded", "provider_error", "missing_binding", "customer_config", "unexpected"]
CredentialFetchProvider: TypeAlias = CredentialVaultType | Literal["onepassword", "unknown"]

_MISSING_BINDING_ERRORS: tuple[type[BaseException], ...] = (
    CredentialParameterNotFoundError,
    CredentialItemNotFoundError,
)
_CUSTOMER_CONFIG_ERRORS: tuple[type[BaseException], ...] = (
    CredentialSourceNotConfiguredError,
    CustomCredentialConfigurationError,
    BitwardenAccessDeniedError,
    OnePasswordSessionExpiredError,
    RuntimeSequentialCredentialUnsupported,
    DisabledBlockExecutionError,
)
_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
    ServiceRequestError,
    ServiceResponseError,
    RetryError,
    OnePasswordServiceUnavailableError,
    OnePasswordRateLimitError,
    CredentialVaultNotConfiguredError,
)
_PLATFORM_VAULT_BINDINGS = (ParameterType.CREDENTIAL, WorkflowParameterType.CREDENTIAL_ID)


@dataclass
class CredentialFetch:
    provider: CredentialFetchProvider
    parameter_type: ParameterType | WorkflowParameterType

    @property
    def customer_owned(self) -> bool:
        """Whether the vault and the identity reading it are the customer's, so a rejection is theirs to fix."""
        return self.provider == CredentialVaultType.CUSTOM or self.parameter_type not in _PLATFORM_VAULT_BINDINGS


def _status_outcome(status: int | None, *, customer_owned: bool) -> CredentialFetchOutcome:
    if status is None or status in (408, 429) or status >= 500:
        return "provider_error"
    if status == 404:
        return "missing_binding"
    if 400 <= status < 500:
        return "customer_config" if customer_owned else "provider_error"
    return "unexpected"


def _link_outcome(error: BaseException, *, customer_owned: bool) -> CredentialFetchOutcome | None:
    if isinstance(error, _MISSING_BINDING_ERRORS):
        return "missing_binding"
    if isinstance(error, _CUSTOMER_CONFIG_ERRORS):
        return "customer_config"
    if isinstance(error, _PROVIDER_ERRORS):
        return "provider_error"
    if isinstance(error, (HttpException, HTTPException)):
        return _status_outcome(error.status_code, customer_owned=customer_owned)
    if isinstance(error, aiohttp.ClientResponseError):
        return _status_outcome(error.status, customer_owned=customer_owned)
    if isinstance(error, ClientAuthenticationError):
        return _status_outcome(error.status_code or 401, customer_owned=customer_owned)
    if isinstance(error, HttpResponseError):
        return _status_outcome(error.status_code, customer_owned=customer_owned)
    if isinstance(error, GoogleAPICallError):
        return _status_outcome(error.code, customer_owned=customer_owned)
    return None


def classify_credential_fetch_failure(
    error: BaseException, *, customer_owned: bool
) -> tuple[CredentialFetchOutcome, str]:
    """The outcome and the exception class that decided it, walking the chain the traceback would print.

    Runs inside the caller's except block, so it must not raise.
    """
    link: BaseException | None = error
    seen: set[int] = set()
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        outcome = _link_outcome(link, customer_owned=customer_owned)
        if outcome is not None:
            return outcome, type(link).__name__
        if link.__cause__ is not None:
            link = link.__cause__
        elif link.__suppress_context__:
            link = None
        else:
            link = link.__context__
    return "unexpected", type(error).__name__


def _log_outcome(
    fetch: CredentialFetch,
    outcome: CredentialFetchOutcome,
    failure_type: str | None,
    started: float,
) -> None:
    log = LOG.info if outcome == "succeeded" else LOG.warning
    log(
        RUN_CREDENTIAL_FETCH_FINISHED_MESSAGE,
        provider=str(fetch.provider),
        parameter_type=str(fetch.parameter_type),
        outcome=outcome,
        failure_type=failure_type,
        duration_seconds=round(time.monotonic() - started, 3),
    )


@asynccontextmanager
async def record_credential_fetch(
    parameter: Parameter,
    provider: CredentialFetchProvider = "unknown",
) -> AsyncIterator[CredentialFetch]:
    """Log one outcome for the credential read in the block; a cancelled read logs nothing.

    Set ``provider`` on the yielded handle when it is only known partway through the read.
    """
    parameter_type = (
        parameter.workflow_parameter_type if isinstance(parameter, WorkflowParameter) else parameter.parameter_type
    )
    fetch = CredentialFetch(provider=provider, parameter_type=parameter_type)
    started = time.monotonic()
    try:
        yield fetch
    except Exception as error:
        outcome, failure_type = classify_credential_fetch_failure(error, customer_owned=fetch.customer_owned)
        _log_outcome(fetch, outcome, failure_type, started)
        raise
    _log_outcome(fetch, "succeeded", None, started)
