from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

import httpx
from google.oauth2.credentials import Credentials

from skyvern.forge.sdk.services import google_gmail_service, google_oauth_service, microsoft_oauth_service
from skyvern.services.email import outlook

if TYPE_CHECKING:
    from skyvern.forge.agent_functions import AgentFunction

MAX_SEEN_EMAIL_MESSAGE_IDS = 500
MAX_HYDRATION_FAILURE_WITHHOLDS = 2
_HYDRATION_FAILURE_COUNTS_STATE_KEY = "gmail_hydration_failure_counts"


@dataclass(frozen=True)
class EmailOTPCandidate:
    message_id: str
    content: str


class EmailOTPSearchError(Exception):
    def __init__(
        self,
        message: str,
        *,
        source: str,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.status = status
        self.code = code


@dataclass
class EmailOTPSourceContext:
    """Per-source polling cache passed through the AgentFunction hook."""

    credential_ids: list[str] | None = None
    credential_ids_loaded_at: datetime | None = None
    last_searched_at_by_credential: dict[str, datetime] = field(default_factory=dict)
    seen_message_keys: set[tuple[str, str]] = field(default_factory=set)
    seen_message_key_order: deque[tuple[str, str]] = field(default_factory=deque)
    provider_state: dict[str, dict] = field(default_factory=dict)

    def has_seen_message(self, credential_id: str, message_id: str) -> bool:
        return (credential_id, message_id) in self.seen_message_keys

    def seen_message_ids_for_credential(self, credential_id: str) -> set[str]:
        return {
            message_id
            for candidate_credential_id, message_id in self.seen_message_keys
            if candidate_credential_id == credential_id
        }

    def remember_message(self, credential_id: str, message_id: str) -> None:
        key = (credential_id, message_id)
        if key in self.seen_message_keys:
            return
        self.seen_message_keys.add(key)
        self.seen_message_key_order.append(key)
        while len(self.seen_message_key_order) > MAX_SEEN_EMAIL_MESSAGE_IDS:
            self.seen_message_keys.discard(self.seen_message_key_order.popleft())


@dataclass
class EmailOTPVerificationContext:
    """Whole-poll cache: one EmailOTPSourceContext per source name."""

    per_source: dict[str, EmailOTPSourceContext] = field(default_factory=dict)

    def for_source(self, source_name: str) -> EmailOTPSourceContext:
        return self.per_source.setdefault(source_name, EmailOTPSourceContext())


class EmailOTPSource(Protocol):
    name: str

    async def list_credential_ids(self, organization_id: str) -> list[str]: ...

    async def search_recent_otp_messages(
        self,
        *,
        organization_id: str,
        credential_id: str,
        totp_identifier: str,
        created_after: datetime | None,
        max_results: int,
        context: EmailOTPSourceContext,
        client: httpx.AsyncClient,
    ) -> list[EmailOTPCandidate]: ...


class _GoogleCredentialGetter(Protocol):
    async def __call__(
        self,
        organization_id: str,
        credential_id: str,
        required_scopes: list[str] | None = None,
    ) -> Credentials | None: ...


class _MicrosoftCredentialGetter(Protocol):
    async def __call__(
        self,
        organization_id: str,
        credential_id: str,
        required_scopes: list[str] | None = None,
    ) -> str | None: ...


class GmailOTPSource:
    name = "gmail"

    def __init__(self, get_credentials: _GoogleCredentialGetter) -> None:
        self._get_credentials = get_credentials

    async def list_credential_ids(self, organization_id: str) -> list[str]:
        return [
            credential.id
            for credential in await google_oauth_service.get_credentials_for_org(organization_id)
            if google_oauth_service.has_required_scopes(
                credential.scopes_granted,
                google_oauth_service.GOOGLE_GMAIL_SCOPES,
            )
        ]

    async def search_recent_otp_messages(
        self,
        *,
        organization_id: str,
        credential_id: str,
        totp_identifier: str,
        created_after: datetime | None,
        max_results: int,
        context: EmailOTPSourceContext,
        client: httpx.AsyncClient,
    ) -> list[EmailOTPCandidate]:
        excluded_message_ids = context.seen_message_ids_for_credential(credential_id)
        credentials = await self._get_credentials(
            organization_id=organization_id,
            credential_id=credential_id,
            required_scopes=list(google_oauth_service.GOOGLE_GMAIL_SCOPES),
        )
        if not credentials or not credentials.token:
            return []
        try:
            candidates = await google_gmail_service.search_recent_otp_messages(
                access_token=credentials.token,
                totp_identifier=totp_identifier,
                created_after=created_after,
                max_results=max_results,
                client=client,
                excluded_message_ids=excluded_message_ids,
            )
        except google_gmail_service.GmailAPIError as exc:
            raise EmailOTPSearchError(
                str(exc),
                source=self.name,
                status=exc.status,
                code=exc.code,
            ) from exc
        withhold_counts: dict[str, int] = context.provider_state.setdefault(credential_id, {}).setdefault(
            _HYDRATION_FAILURE_COUNTS_STATE_KEY, {}
        )
        results: list[EmailOTPCandidate] = []
        for candidate in candidates:
            if candidate.message_id in excluded_message_ids:
                continue
            if candidate.hydration_failed:
                withholds = withhold_counts.get(candidate.message_id, 0)
                if withholds < MAX_HYDRATION_FAILURE_WITHHOLDS:
                    # Withhold while hydration can still recover, then degrade to parsing
                    # the assembled content so a permanent failure cannot stall the poll.
                    withhold_counts[candidate.message_id] = withholds + 1
                    continue
            results.append(
                EmailOTPCandidate(
                    message_id=candidate.message_id,
                    content=candidate.content,
                )
            )
        return results


class OutlookOTPSource:
    name = "outlook"

    def __init__(self, get_credentials: _MicrosoftCredentialGetter) -> None:
        self._get_credentials = get_credentials

    async def list_credential_ids(self, organization_id: str) -> list[str]:
        return [
            credential.id
            for credential in await microsoft_oauth_service.get_credentials_for_org(organization_id)
            if microsoft_oauth_service.has_required_scopes(credential.scopes_granted, ["Mail.Read"])
        ]

    async def search_recent_otp_messages(
        self,
        *,
        organization_id: str,
        credential_id: str,
        totp_identifier: str,
        created_after: datetime | None,
        max_results: int,
        context: EmailOTPSourceContext,
        client: httpx.AsyncClient,
    ) -> list[EmailOTPCandidate]:
        access_token = await self._get_credentials(
            organization_id=organization_id,
            credential_id=credential_id,
            required_scopes=["Mail.Read"],
        )
        if not access_token:
            return []
        excluded_message_ids = context.seen_message_ids_for_credential(credential_id)
        try:
            candidates = await outlook.search_recent_otp_messages(
                access_token=access_token,
                totp_identifier=totp_identifier,
                created_after=created_after,
                max_results=max_results,
                client=client,
                state=context.provider_state.setdefault(credential_id, {}),
                excluded_message_ids=excluded_message_ids,
            )
        except outlook.OutlookAPIError as exc:
            raise EmailOTPSearchError(
                str(exc),
                source=self.name,
                status=exc.status,
                code=exc.code,
            ) from exc
        return [
            EmailOTPCandidate(
                message_id=candidate.message_id,
                content=candidate.content,
            )
            for candidate in candidates
        ]


def build_email_otp_sources(agent_function: AgentFunction) -> list[EmailOTPSource]:
    return [
        GmailOTPSource(agent_function.get_google_workspace_credentials),
        OutlookOTPSource(agent_function.get_microsoft_credentials),
    ]
