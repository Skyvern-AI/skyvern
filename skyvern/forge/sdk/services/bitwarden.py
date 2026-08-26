import asyncio
import atexit
import contextlib
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Tuple, TypeVar

import structlog
import tldextract
from pydantic import BaseModel

from skyvern.config import settings
from skyvern.exceptions import (
    BitwardenAccessDeniedError,
    BitwardenCreateCollectionError,
    BitwardenCreateCreditCardItemError,
    BitwardenCreateLoginItemError,
    BitwardenGetItemError,
    BitwardenListItemsError,
    BitwardenLoginError,
    BitwardenLogoutError,
    BitwardenSecretError,
    BitwardenSyncError,
    BitwardenUnlockError,
)
from skyvern.forge.sdk.api.aws import get_aws_client
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_delete, aiohttp_get_json, aiohttp_post
from skyvern.forge.sdk.schemas.credentials import (
    BitwardenItemOverview,
    CredentialItem,
    CredentialType,
    CreditCardBillingAddress,
    CreditCardCredential,
    PasswordCredential,
    SecretCredential,
)
from skyvern.utils.strings import is_uuid

LOG = structlog.get_logger()
BITWARDEN_SERVER_BASE_URL = f"{settings.BITWARDEN_SERVER}:{settings.BITWARDEN_SERVER_PORT or 8002}"
_RETRY_BACKOFF_BASE_SECONDS = 5.0
_RETRY_BACKOFF_CAP_SECONDS = 60.0


def _retry_backoff_seconds(attempt: int) -> float:
    # Full jitter: a batch of runs that failed together retries spread out instead of re-aligned.
    return random.uniform(0, min(_RETRY_BACKOFF_CAP_SECONDS, _RETRY_BACKOFF_BASE_SECONDS * (2**attempt)))


# The CLI keeps its login state, tokens and encrypted vault copy in one data directory. Pointing each
# vault identity at its own directory lets sessions for different organizations coexist, so a run
# never has to log another organization out to make room for its own login.
_BITWARDEN_APPDATA_ENV_VAR = "BITWARDENCLI_APPDATA_DIR"

# Each `bw` invocation is a Node process. Bound how many run at once so a burst of runs cannot starve
# the browsers sharing the pod, and so one slow command no longer blocks every other run's fetch.
_cli_command_semaphore = asyncio.Semaphore(max(1, settings.BITWARDEN_MAX_CONCURRENT_CLI_COMMANDS))

_EXPIRED_SESSION_MARKERS = (
    "you are not logged in",
    "vault is locked",
    "session key is invalid",
    "invalid session",
    "mac failed",
)
# A vault copy that predates the item looks exactly like an item that does not exist, so both are
# worth one forced sync before believing the miss.
_STALE_VAULT_MARKERS = ("not found", "no items found", "no item found")

# The fingerprint is only ever compared against others made in this process, so salt it with a random
# per-process key. An unsalted digest would be a fast, unkeyed function of the master password, which
# would turn any log line carrying it into an offline oracle for guessing that password.
_IDENTITY_FINGERPRINT_SALT = secrets.token_bytes(32)


class _SessionRepair(StrEnum):
    """What to do to a cached session that just failed a read."""

    NONE = "none"
    RESYNC = "resync"
    RELOGIN = "relogin"


def _repair_for_failure(message: str) -> _SessionRepair:
    lowered = message.lower()
    if any(marker in lowered for marker in _EXPIRED_SESSION_MARKERS):
        return _SessionRepair.RELOGIN
    if any(marker in lowered for marker in _STALE_VAULT_MARKERS):
        return _SessionRepair.RESYNC
    return _SessionRepair.NONE


@dataclass(frozen=True, repr=False)
class _VaultIdentity:
    """The credentials one cached CLI session is logged in as."""

    client_id: str | None
    client_secret: str | None
    email: str | None
    master_password: str

    @staticmethod
    def resolve(
        client_id: str | None,
        client_secret: str | None,
        email: str | None,
        master_password: str | None,
    ) -> "_VaultIdentity":
        # Mirror the fallbacks `login` applies, so the fingerprint names the login that will happen.
        return _VaultIdentity(
            client_id=client_id,
            client_secret=client_secret,
            email=email or settings.BITWARDEN_EMAIL,
            master_password=master_password or settings.BITWARDEN_MASTER_PASSWORD or "",
        )

    @property
    def uses_email_auth(self) -> bool:
        return bool(self.email and self.master_password)

    @property
    def fingerprint(self) -> str:
        """A stable, non-reversible name for this identity, safe to log and to use as a directory."""
        material = (
            ["email", self.email or "", self.master_password]
            if self.uses_email_auth
            else ["apikey", self.client_id or "", self.client_secret or "", self.master_password]
        )
        digest = hashlib.sha256(_IDENTITY_FINGERPRINT_SALT)
        digest.update("\x00".join(material).encode())
        return digest.hexdigest()

    def __repr__(self) -> str:
        return f"_VaultIdentity(fingerprint={self.fingerprint[:12]})"


class _CliSession:
    """A logged-in, unlocked CLI session and the data directory holding its copy of the vault."""

    def __init__(self, identity: _VaultIdentity, appdata_dir: str) -> None:
        self.identity = identity
        self.appdata_dir = appdata_dir
        # Serializes the steps that write to the data directory (login, unlock, sync). Reads against
        # an established session run concurrently.
        self.lock = asyncio.Lock()
        self.session_key: str | None = None
        self.synced_at: float = 0.0
        self.in_flight = 0
        self.retired = False
        self.last_used_at = time.monotonic()

    @property
    def env(self) -> dict[str, str]:
        return {_BITWARDEN_APPDATA_ENV_VAR: self.appdata_dir}

    @contextlib.asynccontextmanager
    async def in_use(self) -> AsyncIterator["_CliSession"]:
        """Keep this session's data directory alive while a command is running against it."""
        self.in_flight += 1
        self.last_used_at = time.monotonic()
        try:
            yield self
        finally:
            self.in_flight -= 1
            self.last_used_at = time.monotonic()
            if self.retired and self.in_flight == 0:
                self._remove_data_dir()

    def retire(self) -> None:
        """Log this identity out for good.

        Removing the directory is what logs it out: the directory holds the tokens and the vault
        copy. A retired session is never handed out again, so it is safe to wait for the readers
        still inside `in_use` rather than deleting the directory out from under them.
        """
        self.retired = True
        self.session_key = None
        if self.in_flight == 0:
            self._remove_data_dir()

    def _remove_data_dir(self) -> None:
        shutil.rmtree(self.appdata_dir, ignore_errors=True)


class _CliSessionCache:
    """Per-process store of live CLI sessions, keyed by vault identity and bounded by an LRU."""

    def __init__(self) -> None:
        self._sessions: OrderedDict[str, _CliSession] = OrderedDict()
        self._lock = asyncio.Lock()
        self._root: str | None = None
        self._generation = 0

    def _root_dir(self) -> str:
        if self._root is None:
            self._root = tempfile.mkdtemp(prefix="skyvern-bitwarden-")
            atexit.register(shutil.rmtree, self._root, True)
        return self._root

    async def checkout(self, identity: _VaultIdentity) -> _CliSession:
        async with self._lock:
            session = self._sessions.get(identity.fingerprint)
            if session is None:
                # A directory per session object, not per identity: a session retired while another
                # request still held it must never be able to delete its own replacement's data.
                self._generation += 1
                appdata_dir = os.path.join(self._root_dir(), f"{identity.fingerprint[:16]}-{self._generation}")
                session = _CliSession(identity, appdata_dir)
                self._sessions[identity.fingerprint] = session
            os.makedirs(session.appdata_dir, mode=0o700, exist_ok=True)
            self._sessions.move_to_end(identity.fingerprint)
            reclaimed = self._reclaim(keep=identity.fingerprint)

        await self._retire_all(reclaimed)
        return session

    async def release(self) -> None:
        """Sweep once a request is done with its session.

        Checkout alone is not enough: during a burst across more vaults than the cache holds, every
        entry is either in flight or the one being handed out, so nothing is reclaimable at that
        moment. Sweeping again as requests finish is what brings the cache back to capacity, and
        what lets an idle session expire while the pod is still doing other Bitwarden work.
        """
        async with self._lock:
            reclaimed = self._reclaim(keep=None)
        await self._retire_all(reclaimed)

    async def _retire_all(self, reclaimed: list[tuple[_CliSession, str]]) -> None:
        for stale, reason in reclaimed:
            LOG.info(
                "Retiring a cached Bitwarden CLI session",
                reason=reason,
                fingerprint=stale.identity.fingerprint[:12],
            )
            async with stale.lock:
                stale.retire()

    def _reclaim(self, keep: str | None) -> list[tuple[_CliSession, str]]:
        """Retire sessions that have gone idle or pushed the cache past capacity.

        The caller holds the cache lock. `keep` names the session this checkout is about to hand
        out: it has no reader yet, so it looks idle, and retiring it would send the caller to a
        directory that no longer exists and make every request for that vault log in again.
        """
        reclaimed: list[tuple[_CliSession, str]] = []
        capacity = max(1, settings.BITWARDEN_SESSION_CACHE_SIZE)
        max_idle = settings.BITWARDEN_SESSION_MAX_IDLE_SECONDS
        now = time.monotonic()
        for fingerprint, session in list(self._sessions.items()):
            # A session with a command in flight still owns its data directory; leave it alone and
            # let a later checkout reclaim it.
            if fingerprint == keep or session.in_flight:
                continue
            if max_idle > 0 and now - session.last_used_at >= max_idle:
                reason = "idle"
            elif len(self._sessions) > capacity:
                reason = "capacity"
            else:
                continue
            del self._sessions[fingerprint]
            reclaimed.append((session, reason))
        return reclaimed

    async def discard(self, session: _CliSession) -> None:
        async with self._lock:
            if self._sessions.get(session.identity.fingerprint) is session:
                del self._sessions[session.identity.fingerprint]
        async with session.lock:
            session.retire()

    async def clear(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            async with session.lock:
                session.retire()


_cli_sessions = _CliSessionCache()

T = TypeVar("T")


class BitwardenItemType(IntEnum):
    LOGIN = 1
    SECURE_NOTE = 2
    CREDIT_CARD = 3
    IDENTITY = 4


BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN = 1
CREDIT_CARD_BILLING_ADDRESS_FIELDS = (
    "line1",
    "line2",
    "city",
    "state",
    "state_code",
    "postal_code",
    "country",
    "country_code",
)


def _credit_card_extra_custom_field_values(credential: CreditCardCredential) -> dict[str, str]:
    values: dict[str, str] = {}
    if credential.billing_address is not None:
        for key, value in credential.billing_address.model_dump(exclude_none=True).items():
            if value:
                values[f"billing_address_{key}"] = value
    if credential.billing_email:
        values["billing_email"] = credential.billing_email
    if credential.billing_phone:
        values["billing_phone"] = credential.billing_phone
    if credential.metadata:
        for key, value in credential.metadata.items():
            if key and value:
                values[f"metadata_{key}"] = value
    return values


def _build_bitwarden_custom_fields(credential: CreditCardCredential) -> list[dict[str, str | int | None]]:
    return [
        {
            "name": name,
            "value": value,
            "type": BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
            "linkedId": None,
        }
        for name, value in _credit_card_extra_custom_field_values(credential).items()
    ]


def _build_bitwarden_login_custom_fields(credential: PasswordCredential) -> list[dict[str, str | int | None]]:
    return [
        {
            "name": f"metadata_{key}",
            "value": value,
            "type": BITWARDEN_CUSTOM_FIELD_TYPE_HIDDEN,
            "linkedId": None,
        }
        for key, value in (credential.metadata or {}).items()
        if key and value
    ]


def _password_metadata_from_bitwarden_item(item: dict) -> dict[str, str] | None:
    metadata = {
        name.removeprefix("metadata_"): value
        for field in item.get("fields") or []
        if isinstance((name := field.get("name")), str)
        and name.startswith("metadata_")
        and name != "metadata_"
        and isinstance((value := field.get("value")), str)
        and value
    }
    return metadata or None


def _extract_credit_card_extra_custom_field_values(item: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in item.get("fields") or []:
        name = field.get("name")
        value = field.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not value:
            continue
        if name == "billing_email" or name == "billing_phone" or name.startswith("billing_address_"):
            values[name] = value
        elif name.startswith("metadata_"):
            values[name] = value
    return values


def _credit_card_credential_from_bitwarden_item(item: dict) -> CreditCardCredential:
    card = item["card"]
    extra_values = _extract_credit_card_extra_custom_field_values(item)
    address_values = {
        key: extra_values[f"billing_address_{key}"]
        for key in CREDIT_CARD_BILLING_ADDRESS_FIELDS
        if f"billing_address_{key}" in extra_values
    }
    metadata = {
        key.removeprefix("metadata_"): value for key, value in extra_values.items() if key.startswith("metadata_")
    }
    return CreditCardCredential(
        card_holder_name=card["cardholderName"],
        card_number=card["number"],
        card_exp_month=card["expMonth"],
        card_exp_year=card["expYear"],
        card_cvv=card["code"],
        card_brand=card["brand"],
        billing_address=CreditCardBillingAddress(**address_values) if address_values else None,
        billing_email=extra_values.get("billing_email"),
        billing_phone=extra_values.get("billing_phone"),
        metadata=metadata or None,
    )


def get_bitwarden_item_type_code(item_type: BitwardenItemType) -> int:
    if item_type == BitwardenItemType.LOGIN:
        return 1
    elif item_type == BitwardenItemType.SECURE_NOTE:
        return 2
    elif item_type == BitwardenItemType.CREDIT_CARD:
        return 3
    elif item_type == BitwardenItemType.IDENTITY:
        return 4


def get_list_response_item_from_bitwarden_item(item: dict) -> CredentialItem:
    if item["type"] == BitwardenItemType.LOGIN:
        login = item["login"]
        totp = BitwardenService.normalize_totp_config(login.get("totp") or "")
        return CredentialItem(
            item_id=item["id"],
            credential=PasswordCredential(
                username=login["username"] or "",
                password=login["password"] or "",
                totp=totp,
                metadata=_password_metadata_from_bitwarden_item(item),
            ),
            name=item["name"],
            credential_type=CredentialType.PASSWORD,
        )
    elif item["type"] == BitwardenItemType.CREDIT_CARD:
        return CredentialItem(
            item_id=item["id"],
            credential=_credit_card_credential_from_bitwarden_item(item),
            name=item["name"],
            credential_type=CredentialType.CREDIT_CARD,
        )
    elif item["type"] == BitwardenItemType.SECURE_NOTE:
        notes = item.get("notes") or ""
        secret_value = ""
        secret_label = None
        try:
            parsed_notes = json.loads(notes)
            if isinstance(parsed_notes, dict):
                secret_value = parsed_notes.get("secret_value", "") or ""
                secret_label = parsed_notes.get("secret_label")
            else:
                secret_value = notes
        except Exception:
            secret_value = notes

        return CredentialItem(
            item_id=item["id"],
            credential=SecretCredential(secret_value=secret_value, secret_label=secret_label),
            name=item["name"],
            credential_type=CredentialType.SECRET,
        )
    else:
        raise BitwardenGetItemError(f"Unsupported item type: {item['type']}")


def get_bitwarden_item_overview_from_bitwarden_item(
    item: dict,
    allowed_collection_ids: list[str] | None = None,
    preferred_collection_id: str | None = None,
) -> BitwardenItemOverview | None:
    item_id, title, item_type = item.get("id"), item.get("name"), item.get("type")
    if not isinstance(item_type, int):
        return None
    try:
        credential_type = {
            BitwardenItemType.LOGIN: CredentialType.PASSWORD,
            BitwardenItemType.CREDIT_CARD: CredentialType.CREDIT_CARD,
            BitwardenItemType.SECURE_NOTE: CredentialType.SECRET,
            BitwardenItemType.IDENTITY: CredentialType.SECRET,
        }[BitwardenItemType(item_type)]
    except (TypeError, ValueError):
        return None
    if not isinstance(item_id, str) or not isinstance(title, str):
        return None

    collection_ids = [str(collection_id) for collection_id in item.get("collectionIds") or [] if collection_id]
    allowed_collection_ids = allowed_collection_ids or []
    # Collection-scoped CLI queries can omit collectionIds, so keep the queried collection as a fallback.
    collection_id = (
        preferred_collection_id
        if preferred_collection_id and preferred_collection_id in collection_ids
        else next((cid for cid in collection_ids if cid in allowed_collection_ids), None)
        or (collection_ids[0] if collection_ids else preferred_collection_id)
    )
    login = item.get("login")
    uris = login.get("uris") if isinstance(login, dict) else []
    url = next(
        (
            uri.get("uri")
            for uri in uris or []
            if isinstance(uri, dict) and isinstance(uri.get("uri"), str) and uri.get("uri")
        ),
        None,
    )

    return BitwardenItemOverview(
        item_id=item_id,
        title=title,
        collection_id=collection_id,
        credential_type=credential_type,
        url=url,
    )


# Bitwarden's per-URI match mode, as the vault stores it. A user who narrowed a URI below its default
# is saying the credential does not belong to the whole site, and `never` says not to offer it at all.
_BITWARDEN_URI_MATCH_DOMAIN = 0


def _whole_site_login_uris(login_item: dict[str, Any]) -> list[str]:
    """URIs whose match mode lets the credential stand for its whole site."""
    return [
        uri["uri"]
        for uri in login_item.get("uris") or []
        if isinstance(uri, dict)
        and uri.get("uri")
        and (uri.get("match") is None or uri.get("match") == _BITWARDEN_URI_MATCH_DOMAIN)
    ]


def is_valid_email(email: str | None) -> bool:
    if not email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None


class BitwardenConstants(StrEnum):
    BW_ORGANIZATION_ID = "BW_ORGANIZATION_ID"
    BW_COLLECTION_IDS = "BW_COLLECTION_IDS"

    CLIENT_ID = "BW_CLIENT_ID"
    CLIENT_SECRET = "BW_CLIENT_SECRET"
    MASTER_PASSWORD = "BW_MASTER_PASSWORD"
    URL = "BW_URL"
    BW_COLLECTION_ID = "BW_COLLECTION_ID"
    IDENTITY_KEY = "BW_IDENTITY_KEY"
    BW_ITEM_ID = "BW_ITEM_ID"

    USERNAME = "BW_USERNAME"
    PASSWORD = "BW_PASSWORD"
    TOTP = "BW_TOTP"

    CREDIT_CARD_HOLDER_NAME = "BW_CREDIT_CARD_HOLDER_NAME"
    CREDIT_CARD_NUMBER = "BW_CREDIT_CARD_NUMBER"
    CREDIT_CARD_EXPIRATION_MONTH = "BW_CREDIT_CARD_EXPIRATION_MONTH"
    CREDIT_CARD_EXPIRATION_YEAR = "BW_CREDIT_CARD_EXPIRATION_YEAR"
    CREDIT_CARD_CVV = "BW_CREDIT_CARD_CVV"
    CREDIT_CARD_BRAND = "BW_CREDIT_CARD_BRAND"

    SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID = "SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID"
    SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD = "SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD"
    SKYVERN_AUTH_BITWARDEN_CLIENT_ID = "SKYVERN_AUTH_BITWARDEN_CLIENT_ID"
    SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET = "SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET"


class BitwardenQueryResult(BaseModel):
    credential: dict[str, str]
    uris: list[str]


class RunCommandResult(BaseModel):
    stdout: str
    stderr: str
    returncode: int


class BitwardenService:
    @staticmethod
    def _is_ignorable_login_stderr(stderr: str) -> bool:
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        if not lines:
            return True

        ignorable_substrings = [
            "You are already logged in as",
        ]
        ignorable_regexes = [
            re.compile(r'^Could not find data file, ".+?/data\.json"; creating it instead\.$'),
        ]
        for line in lines:
            if any(s in line for s in ignorable_substrings):
                continue
            if any(r.match(line) for r in ignorable_regexes):
                continue
            return False
        return True

    @staticmethod
    async def _apply_jitter() -> None:
        """Apply random jitter delay to spread out concurrent Bitwarden CLI requests."""
        max_jitter = settings.BITWARDEN_MAX_JITTER_SECONDS
        if max_jitter > 0:
            jitter = random.uniform(0, max_jitter)
            LOG.debug("Applying Bitwarden jitter delay", jitter_seconds=round(jitter, 2))
            await asyncio.sleep(jitter)

    @staticmethod
    async def run_command(
        command: list[str], additional_env: dict[str, str] | None = None, timeout: int = 60
    ) -> RunCommandResult:
        """
        Run a CLI command with the specified additional environment variables and return the result.
        """
        env = os.environ.copy()  # Copy the current environment
        # Make sure node isn't returning warnings. Warnings are sent through stderr and we raise exceptions on stderr.
        env["NODE_NO_WARNINGS"] = "1"
        if additional_env:
            env.update(additional_env)  # Update with any additional environment variables

        shell_subprocess = None
        queued_at = time.monotonic()
        started = queued_at
        try:
            # Wait for a slot outside the command's own budget: `timeout` measures the command, and
            # the caller's attempt-level deadline is what bounds the queueing.
            async with _cli_command_semaphore:
                started = time.monotonic()
                async with asyncio.timeout(timeout):
                    shell_subprocess = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    stdout, stderr = await shell_subprocess.communicate()
                    return RunCommandResult(
                        stdout=stdout.decode(),
                        stderr=stderr.decode(),
                        returncode=shell_subprocess.returncode,
                    )
        except asyncio.TimeoutError:
            LOG.error(
                "Bitwarden command timed out",
                timeout_seconds=timeout,
                elapsed_seconds=round(time.monotonic() - started, 2),
                queued_seconds=round(started - queued_at, 2),
                command=command[0:2],
                exc_info=True,
            )
            raise
        except asyncio.CancelledError:
            # An enclosing deadline fired before this command's own budget; name the step so the
            # attempt-level timeout is attributable to it.
            LOG.warning(
                "Bitwarden command cancelled by an enclosing deadline",
                elapsed_seconds=round(time.monotonic() - started, 2),
                queued_seconds=round(started - queued_at, 2),
                command=command[0:2],
            )
            raise
        finally:
            if shell_subprocess and shell_subprocess.returncode is None:
                LOG.info("Killing orphaned Bitwarden subprocess", pid=shell_subprocess.pid)
                try:
                    shell_subprocess.kill()
                    await shell_subprocess.wait()
                except (ProcessLookupError, asyncio.CancelledError):
                    pass

    @staticmethod
    def _extract_session_key(unlock_cmd_output: str) -> str | None:
        # Split the text by lines
        lines = unlock_cmd_output.split("\n")

        # Look for the line containing the BW_SESSION
        for line in lines:
            if 'BW_SESSION="' in line:
                # Find the start and end positions of the session key
                start = line.find('BW_SESSION="') + len('BW_SESSION="')
                end = line.rfind('"', start)
                return line[start:end]

        return None

    @staticmethod
    async def _establish_cli_session(session: _CliSession, timeout: int) -> None:
        """Log this identity in and unlock it. The caller holds `session.lock`."""
        identity = session.identity
        await BitwardenService.login(
            identity.client_id,
            identity.client_secret,
            email=identity.email,
            master_password=identity.master_password,
            timeout=timeout,
            appdata_dir=session.appdata_dir,
        )
        session.session_key = await BitwardenService.unlock(
            identity.master_password, timeout=timeout, appdata_dir=session.appdata_dir
        )
        # Sync after unlocking rather than before, so the one call works for both auth modes: an
        # API-key login leaves the vault locked, and `bw sync` needs it open.
        await BitwardenService.sync(timeout=timeout, session_key=session.session_key, appdata_dir=session.appdata_dir)
        session.synced_at = time.monotonic()

    @staticmethod
    async def _prepare_cli_session(
        session: _CliSession,
        timeout: int,
        force_sync: bool = False,
    ) -> None:
        """Make `session` usable, logging in only if this process does not already hold a session."""
        async with session.lock:
            if session.session_key is None:
                await BitwardenService._establish_cli_session(session, timeout=timeout)
                return

            if (
                not force_sync
                and time.monotonic() - session.synced_at < settings.BITWARDEN_SESSION_SYNC_INTERVAL_SECONDS
            ):
                return
            try:
                await BitwardenService.sync(
                    timeout=timeout, session_key=session.session_key, appdata_dir=session.appdata_dir
                )
                session.synced_at = time.monotonic()
            except Exception:
                if force_sync:
                    # A caller that asked for a guaranteed-fresh vault has no way to tell a stale
                    # answer from a correct one — a listing that is missing an item someone just
                    # added looks exactly like a listing that is right. Say the refresh failed.
                    raise
                # An opportunistic refresh is different: the read that follows can still detect a
                # miss and force another sync, so a failure here is not worth failing the run over.
                LOG.warning("Could not refresh the cached Bitwarden vault; reading the cached copy", exc_info=True)

    @staticmethod
    async def _with_cli_session(
        identity: _VaultIdentity,
        timeout: int,
        run: Callable[[_CliSession], Awaitable[T]],
        force_sync: bool = False,
    ) -> T:
        """Run `run` against a live CLI session, repairing a stale session once before giving up."""
        for attempt in range(2):
            session = await _cli_sessions.checkout(identity)
            try:
                # Claim the session before the first await after checkout, so eviction cannot pull
                # the data directory out from under a command that is about to run against it.
                async with session.in_use():
                    await BitwardenService._prepare_cli_session(session, timeout=timeout, force_sync=force_sync)
                    return await run(session)
            except BitwardenAccessDeniedError:
                raise
            except BaseException as error:
                repair = _repair_for_failure(str(error)) if attempt == 0 else _SessionRepair.NONE
                # A session that never finished logging in is not a session; drop it rather than
                # hand the next run a half-built data directory.
                if session.session_key is None or repair is _SessionRepair.RELOGIN:
                    await _cli_sessions.discard(session)
                if repair is _SessionRepair.NONE or not isinstance(error, Exception):
                    raise
                LOG.info(
                    "Refreshing a cached Bitwarden CLI session before one retry",
                    repair=str(repair),
                    fingerprint=identity.fingerprint[:12],
                    error=str(error),
                )
                force_sync = True
            finally:
                # This request is done holding its session, which may be what makes another one
                # reclaimable. Runs on the way out of a return, a raise and a retry alike.
                await _cli_sessions.release()
        raise BitwardenListItemsError("Bitwarden CLI session could not be refreshed")

    @staticmethod
    async def _list_items_using_cli(
        session_key: str,
        bw_organization_id: str | None = None,
        collection_id: str | None = None,
        timeout: int = 60,
        appdata_dir: str | None = None,
    ) -> list[dict]:
        list_command = ["bw", "list", "items", "--session", session_key]
        if bw_organization_id:
            list_command.extend(["--organizationid", bw_organization_id])
        if collection_id:
            list_command.extend(["--collectionid", collection_id])

        env = {_BITWARDEN_APPDATA_ENV_VAR: appdata_dir} if appdata_dir else None
        items_result = await BitwardenService.run_command(list_command, additional_env=env, timeout=timeout)
        return BitwardenService._parse_listed_items(items_result)

    @staticmethod
    def _parse_listed_items(items_result: RunCommandResult) -> list[dict]:
        """Read `bw list items` output, naming the CLI's own complaint before blaming the JSON.

        A CLI whose session has expired exits non-zero with an empty stdout, so parsing first would
        report a JSON error and swallow the one detail — the stderr — that says to log in again.
        """
        if items_result.returncode != 0:
            raise BitwardenListItemsError(f"Failed to list Bitwarden items. Error: {items_result.stderr}")

        try:
            items = json.loads(items_result.stdout)
        except json.JSONDecodeError:
            raise BitwardenListItemsError(
                f"Failed to parse items JSON. Error: {items_result.stderr} Output: {items_result.stdout}"
            )
        if not isinstance(items, list):
            raise BitwardenListItemsError(f"Failed to parse items JSON. Output: {items_result.stdout}")
        return items

    @staticmethod
    def _parse_fetched_item(item_result: RunCommandResult, item_id: str) -> dict:
        """Read `bw get item` output, naming the CLI's own complaint before blaming the JSON.

        Same reasoning as `_parse_listed_items`: a dead session and an item the cached vault has
        never seen both report themselves on stderr, and both are recoverable — but only if the
        message survives to the caller.
        """
        if item_result.returncode != 0 or item_result.stderr:
            raise BitwardenGetItemError(f"Failed to get the bitwarden item {item_id}. Error: {item_result.stderr}")

        try:
            item = json.loads(item_result.stdout)
        except json.JSONDecodeError:
            raise BitwardenGetItemError(f"Failed to parse item JSON for item ID: {item_id}")
        if not item:
            raise BitwardenGetItemError(f"No item found in Bitwarden for item ID: {item_id}")
        return item

    @staticmethod
    async def list_item_overviews(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        email: str,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
    ) -> list[BitwardenItemOverview]:
        if not email or not master_password:
            raise BitwardenLoginError("Bitwarden item listing requires org-scoped email and master password")

        async def list_overviews(session: _CliSession) -> list[BitwardenItemOverview]:
            session_key = session.session_key or ""
            raw_items_with_preferred_collection: list[tuple[dict, str | None]] = []

            if bw_organization_id:
                raw_items = await BitwardenService._list_items_using_cli(
                    session_key=session_key,
                    bw_organization_id=bw_organization_id,
                    timeout=timeout,
                    appdata_dir=session.appdata_dir,
                )
                allowed_collection_ids = set(bw_collection_ids or [])
                if allowed_collection_ids:
                    raw_items = [
                        item
                        for item in raw_items
                        if any(cid in allowed_collection_ids for cid in item.get("collectionIds") or [])
                    ]
                raw_items_with_preferred_collection = [(item, None) for item in raw_items]
            elif bw_collection_ids:
                for collection_id in bw_collection_ids:
                    raw_items = await BitwardenService._list_items_using_cli(
                        session_key=session_key,
                        collection_id=collection_id,
                        timeout=timeout,
                        appdata_dir=session.appdata_dir,
                    )
                    raw_items_with_preferred_collection.extend((item, collection_id) for item in raw_items)
            else:
                raw_items = await BitwardenService._list_items_using_cli(
                    session_key=session_key, timeout=timeout, appdata_dir=session.appdata_dir
                )
                raw_items_with_preferred_collection = [(item, None) for item in raw_items]

            overviews: list[BitwardenItemOverview] = []
            seen_item_ids: set[str] = set()
            for item, preferred_collection_id in raw_items_with_preferred_collection:
                overview = get_bitwarden_item_overview_from_bitwarden_item(
                    item,
                    allowed_collection_ids=bw_collection_ids,
                    preferred_collection_id=preferred_collection_id,
                )
                if overview is not None and overview.item_id not in seen_item_ids:
                    seen_item_ids.add(overview.item_id)
                    overviews.append(overview)
            return overviews

        await BitwardenService._apply_jitter()
        identity = _VaultIdentity.resolve(client_id, client_secret, email, master_password)
        async with asyncio.timeout(timeout):
            # Someone browsing their own vault expects to see an item they just added, so this path
            # always syncs first. It still reuses the login, which is what a run's fetch pays for.
            return await BitwardenService._with_cli_session(
                identity,
                timeout=timeout,
                run=list_overviews,
                force_sync=True,
            )

    @staticmethod
    async def get_secret_value_from_url(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str | None = None,
        collection_id: str | None = None,
        item_id: str | None = None,
        max_retries: int = settings.BITWARDEN_MAX_RETRIES,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        email: str | None = None,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        fail_reasons: list[str] = []
        if not bw_organization_id and bw_collection_ids and collection_id not in bw_collection_ids:
            raise BitwardenAccessDeniedError()

        if item_id and not is_uuid(item_id):
            raise BitwardenGetItemError(f"Invalid item ID: {item_id}. Check if the item ID is correct")

        await BitwardenService._apply_jitter()
        for attempt in range(max_retries):
            try:
                # Every CLI command gets `timeout`; the attempt as a whole gets twice that, so one slow step
                # cannot consume the others' budget and a dead vault still fails in bounded time.
                async with asyncio.timeout(2 * timeout):
                    return await BitwardenService._get_secret_value_from_url(
                        client_id=client_id,
                        client_secret=client_secret,
                        master_password=master_password,
                        bw_organization_id=bw_organization_id,
                        bw_collection_ids=bw_collection_ids,
                        url=url,
                        collection_id=collection_id,
                        item_id=item_id,
                        timeout=timeout,
                        email=email,
                    )
            except BitwardenAccessDeniedError as e:
                raise e
            except Exception as e:
                LOG.info("Failed to get secret value from Bitwarden", tried_times=attempt + 1, exc_info=True)
                fail_reasons.append(f"{type(e).__name__}: {str(e)}")
                if attempt + 1 < max_retries:
                    delay = _retry_backoff_seconds(attempt)
                    LOG.info(
                        "Backing off before retrying Bitwarden", delay_seconds=round(delay, 2), attempt=attempt + 1
                    )
                    await asyncio.sleep(delay)
        raise BitwardenListItemsError(f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}")

    @staticmethod
    def normalize_totp_config(totp_value: str) -> str:
        """
        Preserve the raw TOTP value from Bitwarden.

        Args:
            totp_value: Raw TOTP secret, URI, or provider-specific payload.

        Returns:
            The raw TOTP value

        Example:
            >>> BitwardenService.normalize_totp_config("AAAAAABBBBBBB")
            "AAAAAABBBBBBB"
            >>> BitwardenService.normalize_totp_config("otpauth://totp/user@domain.com?secret=AAAAAABBBBBBB")
            "otpauth://totp/user@domain.com?secret=AAAAAABBBBBBB"
        """
        return totp_value.strip()

    @staticmethod
    def extract_totp_secret(totp_value: str) -> str:
        """Compatibility shim for callers using the old method name."""
        return BitwardenService.normalize_totp_config(totp_value)

    @staticmethod
    async def _get_secret_value_from_url(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        url: str | None = None,
        collection_id: str | None = None,
        item_id: str | None = None,
        timeout: int = 60,
        email: str | None = None,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """

        async def fetch(session: _CliSession) -> dict[str, str]:
            session_key = session.session_key or ""

            if item_id:  # if item_id provided, get single item by item id
                # One item decrypted, rather than the whole collection: this is the cheap path, and
                # the only one that stays cheap as a collection grows.
                command = ["bw", "get", "item", item_id, "--session", session_key]
                item_result = await BitwardenService.run_command(command, additional_env=session.env, timeout=timeout)
                item = BitwardenService._parse_fetched_item(item_result, item_id)

                login = item["login"]
                totp = BitwardenService.normalize_totp_config(login.get("totp") or "")

                return {
                    BitwardenConstants.USERNAME: login.get("username") or "",
                    BitwardenConstants.PASSWORD: login.get("password") or "",
                    BitwardenConstants.TOTP: totp,
                }
            elif not url:
                # if item_id is not provided, we need a url to search for items
                raise BitwardenGetItemError("No url or item ID provided")

            # Extract the domain from the URL and search for items in Bitwarden with that domain
            extract_url = tldextract.extract(url)
            domain = extract_url.domain
            list_command = [
                "bw",
                "list",
                "items",
                "--search",
                domain,
                "--session",
                session_key,
            ]
            if bw_organization_id:
                LOG.info(
                    "Organization ID is provided, filtering items by organization ID",
                    bw_organization_id=bw_organization_id,
                )
                list_command.extend(["--organizationid", bw_organization_id])
            elif collection_id:
                LOG.info("Collection ID is provided, filtering items by collection ID", collection_id=collection_id)
                list_command.extend(["--collectionid", collection_id])
            else:
                LOG.error("No collection ID or organization ID provided -- this is required")
                raise BitwardenListItemsError("No collection ID or organization ID provided -- this is required")
            items_result = await BitwardenService.run_command(list_command, additional_env=session.env, timeout=timeout)

            # Parse the items and extract credentials
            items = BitwardenService._parse_listed_items(items_result)

            # Since Bitwarden can't AND multiple filters, we only use organization id in the list command
            # but we still need to filter the items by collection id here
            if bw_organization_id and collection_id:
                filtered_items = []
                for item in items:
                    if "collectionIds" in item and collection_id in item["collectionIds"]:
                        filtered_items.append(item)
                items = filtered_items

            if not items:
                collection_id_str = f" in collection with ID: {collection_id}" if collection_id else ""
                raise BitwardenListItemsError(f"No items found in Bitwarden for URL: {url}{collection_id_str}")

            bitwarden_result: list[BitwardenQueryResult] = []
            for item in items:
                if "login" not in item:
                    continue

                login = item["login"]
                totp = BitwardenService.normalize_totp_config(login.get("totp") or "")

                bitwarden_result.append(
                    BitwardenQueryResult(
                        credential={
                            BitwardenConstants.USERNAME: login.get("username") or "",
                            BitwardenConstants.PASSWORD: login.get("password") or "",
                            BitwardenConstants.TOTP: totp,
                        },
                        uris=[uri.get("uri") for uri in login.get("uris") or [] if uri.get("uri")],
                    )
                )

            if len(bitwarden_result) == 0:
                return {}

            if len(bitwarden_result) == 1:
                return bitwarden_result[0].credential

            # Choose multiple credentials according to the defined rule,
            # if no cred matches the rule, return the first one.
            # TODO: For now hard code to choose the first matched result
            for single_result in bitwarden_result:
                # check the username is a valid email
                if is_valid_email(single_result.credential.get(BitwardenConstants.USERNAME)):
                    for uri in single_result.uris:
                        # check if the register_domain is the same
                        if extract_url.registered_domain == tldextract.extract(uri).registered_domain:
                            return single_result.credential
            LOG.warning("No credential in Bitwarden matches the rule, returning the first match")
            return bitwarden_result[0].credential

        identity = _VaultIdentity.resolve(client_id, client_secret, email, master_password)
        return await BitwardenService._with_cli_session(identity, timeout=timeout, run=fetch)

    @staticmethod
    async def get_sensitive_information_from_identity(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        identity_key: str,
        identity_fields: list[str],
        remaining_retries: int = settings.BITWARDEN_MAX_RETRIES,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        fail_reasons: list[str] = [],
        email: str | None = None,
    ) -> dict[str, str]:
        """
        Get the secret value from the Bitwarden CLI.
        """
        if not bw_organization_id and bw_collection_ids and collection_id not in bw_collection_ids:
            raise BitwardenAccessDeniedError()
        if not fail_reasons:
            await BitwardenService._apply_jitter()
        try:
            async with asyncio.timeout(timeout):
                return await BitwardenService._get_sensitive_information_from_identity(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    bw_organization_id=bw_organization_id,
                    bw_collection_ids=bw_collection_ids,
                    collection_id=collection_id,
                    identity_key=identity_key,
                    identity_fields=identity_fields,
                    email=email,
                    timeout=timeout,
                )
        except BitwardenAccessDeniedError as e:
            raise e
        except Exception as e:
            if remaining_retries <= 0:
                raise BitwardenListItemsError(
                    f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}"
                )

            remaining_retries -= 1
            LOG.info("Retrying to get sensitive information from Bitwarden", remaining_retries=remaining_retries)
            await asyncio.sleep(_retry_backoff_seconds(len(fail_reasons)))
            return await BitwardenService.get_sensitive_information_from_identity(
                client_id=client_id,
                client_secret=client_secret,
                master_password=master_password,
                bw_organization_id=bw_organization_id,
                bw_collection_ids=bw_collection_ids,
                collection_id=collection_id,
                identity_key=identity_key,
                identity_fields=identity_fields,
                remaining_retries=remaining_retries,
                # Double the timeout for the next retry
                timeout=timeout * 2,
                fail_reasons=fail_reasons + [f"{type(e).__name__}: {str(e)}"],
                email=email,
            )

    @staticmethod
    async def _get_sensitive_information_from_identity(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        collection_id: str,
        identity_key: str,
        identity_fields: list[str],
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        email: str | None = None,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
    ) -> dict[str, str]:
        """
        Get the sensitive information from the Bitwarden CLI.
        """
        if not bw_organization_id and not collection_id:
            raise BitwardenAccessDeniedError()

        async def fetch(session: _CliSession) -> dict[str, str]:
            # Step 3: Retrieve the items
            list_command = [
                "bw",
                "list",
                "items",
                "--search",
                identity_key,
                "--session",
                session.session_key or "",
                "--collectionid",
                collection_id,
            ]
            if bw_organization_id:
                list_command.extend(["--organizationid", bw_organization_id])
            items_result = await BitwardenService.run_command(list_command, additional_env=session.env, timeout=timeout)

            # Parse the items and extract sensitive information
            items = BitwardenService._parse_listed_items(items_result)
            if not items:
                raise BitwardenListItemsError(
                    f"No items found in Bitwarden for identity key: {identity_key} in collection with ID: {collection_id}"
                )

            # Filter the identity items
            # https://bitwarden.com/help/cli/#create lists the type of the identity items as 4

            # We may want to filter it by type in the future, but for now we just take the first item and check its identity fields
            # identity_items = [item for item in items if item["type"] == 4]

            identity_item = items[0]

            sensitive_information: dict[str, str] = {}
            for field in identity_fields:
                # The identity item may store sensitive information in custom fields or default fields
                # Custom fields are prioritized over default fields
                # TODO (kerem): Make this case insensitive?
                for item in identity_item["fields"]:
                    if item["name"] == field:
                        sensitive_information[field] = item["value"]
                        break

                if (
                    "identity" in identity_item
                    and field in identity_item["identity"]
                    and field not in sensitive_information
                ):
                    sensitive_information[field] = identity_item["identity"][field]

            return sensitive_information

        identity = _VaultIdentity.resolve(client_id, client_secret, email, master_password)
        return await BitwardenService._with_cli_session(identity, timeout=timeout, run=fetch)

    @staticmethod
    async def login(
        client_id: str | None,
        client_secret: str | None,
        email: str | None = None,
        master_password: str | None = None,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        appdata_dir: str | None = None,
    ) -> None:
        """
        Log in to the Bitwarden CLI.

        Supports two auth modes:
        1. Email + master_password (preferred when available)
        2. API key (client_id + client_secret) via --apikey flag
        """
        bw_email = email or settings.BITWARDEN_EMAIL
        bw_master_password = master_password or settings.BITWARDEN_MASTER_PASSWORD
        env = {
            "BW_CLIENTID": client_id or "",
            "BW_CLIENTSECRET": client_secret or "",
            "BW_PASSWORD": bw_master_password or "",
        }
        if appdata_dir:
            env[_BITWARDEN_APPDATA_ENV_VAR] = appdata_dir
        if bw_email and bw_master_password:
            login_command = ["bw", "login", bw_email, "--passwordenv", "BW_PASSWORD"]
        else:
            login_command = ["bw", "login", "--apikey"]
        login_result = await BitwardenService.run_command(login_command, env, timeout=timeout)

        # Validate the login result
        if login_result.stdout and "You are logged in!" not in login_result.stdout:
            raise BitwardenLoginError(f"Failed to log in. stdout: {login_result.stdout} stderr: {login_result.stderr}")

        if login_result.stderr and not BitwardenService._is_ignorable_login_stderr(login_result.stderr):
            raise BitwardenLoginError(f"Failed to log in. stdout: {login_result.stdout} stderr: {login_result.stderr}")

        LOG.info("Bitwarden login successful")

    @staticmethod
    async def unlock(
        master_password: str,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        appdata_dir: str | None = None,
    ) -> str:
        """
        Unlock the Bitwarden CLI.
        """
        env = {
            "BW_PASSWORD": master_password,
        }
        if appdata_dir:
            env[_BITWARDEN_APPDATA_ENV_VAR] = appdata_dir
        unlock_command = ["bw", "unlock", "--passwordenv", "BW_PASSWORD"]
        unlock_result = await BitwardenService.run_command(unlock_command, env, timeout=timeout)

        # Validate the unlock result
        if unlock_result.stdout and "Your vault is now unlocked!" not in unlock_result.stdout:
            raise BitwardenUnlockError(
                f"Failed to unlock vault. stdout: {unlock_result.stdout} stderr: {unlock_result.stderr}"
            )

        # Extract session key
        try:
            session_key = BitwardenService._extract_session_key(unlock_result.stdout)
        except Exception as e:
            raise BitwardenUnlockError(f"Unable to extract session key: {str(e)}. stderr: {unlock_result.stderr}")

        if not session_key:
            raise BitwardenUnlockError(f"Session key is empty. stderr: {unlock_result.stderr}")

        return session_key

    @staticmethod
    async def sync(
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
        session_key: str | None = None,
        appdata_dir: str | None = None,
    ) -> None:
        """
        Sync the Bitwarden CLI.
        """
        sync_command = ["bw", "sync"]
        if session_key:
            sync_command.extend(["--session", session_key])
        env = {_BITWARDEN_APPDATA_ENV_VAR: appdata_dir} if appdata_dir else None
        LOG.info("Bitwarden CLI sync started")
        sync_result = await BitwardenService.run_command(sync_command, additional_env=env, timeout=timeout)
        LOG.info("Bitwarden CLI sync completed")
        if sync_result.stderr:
            raise BitwardenSyncError(sync_result.stderr)

    @staticmethod
    async def logout(timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS, appdata_dir: str | None = None) -> None:
        """
        Log out of the Bitwarden CLI.
        """
        logout_command = ["bw", "logout"]
        env = {_BITWARDEN_APPDATA_ENV_VAR: appdata_dir} if appdata_dir else None
        logout_result = await BitwardenService.run_command(logout_command, additional_env=env, timeout=timeout)
        if logout_result.stderr and "You are not logged in." not in logout_result.stderr:
            raise BitwardenLogoutError(logout_result.stderr)

    @staticmethod
    async def _get_credit_card_data(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        item_id: str,
        email: str | None = None,
        timeout: int = settings.BITWARDEN_TIMEOUT_SECONDS,
    ) -> dict[str, str]:
        """
        Get the credit card data from the Bitwarden CLI.
        """
        # Bitwarden CLI doesn't support filtering by organization ID or collection ID for credit card data so we just raise an error if no collection ID or organization ID is provided
        if not bw_organization_id and not collection_id:
            LOG.error("No collection ID or organization ID provided -- this is required")
            raise BitwardenAccessDeniedError()

        async def fetch(session: _CliSession) -> dict[str, str]:
            # Step 3: Get the item
            get_command = [
                "bw",
                "get",
                "item",
                item_id,
                "--session",
                session.session_key or "",
            ]

            item_result = await BitwardenService.run_command(get_command, additional_env=session.env, timeout=timeout)

            # Parse the item and extract credit card data
            item = BitwardenService._parse_fetched_item(item_result, item_id)

            # Check if the bw_organization_id matches
            if bw_organization_id:
                item_organization_id = item.get("organizationId")
                if item_organization_id != bw_organization_id:
                    raise BitwardenAccessDeniedError()

            if bw_collection_ids:
                item_collection_ids = item.get("collectionIds")
                if item_collection_ids and collection_id not in bw_collection_ids:
                    raise BitwardenAccessDeniedError()

            # Check if the item is a credit card
            # https://bitwarden.com/help/cli/#create lists the type of the credit card items as 3
            if item["type"] != 3:
                raise BitwardenListItemsError(f"Item with ID: {item_id} is not a credit card type")

            credit_card_data = item["card"]

            mapped_credit_card_data: dict[str, str] = {
                BitwardenConstants.CREDIT_CARD_HOLDER_NAME: credit_card_data["cardholderName"],
                BitwardenConstants.CREDIT_CARD_NUMBER: credit_card_data["number"],
                BitwardenConstants.CREDIT_CARD_EXPIRATION_MONTH: credit_card_data["expMonth"],
                BitwardenConstants.CREDIT_CARD_EXPIRATION_YEAR: credit_card_data["expYear"],
                BitwardenConstants.CREDIT_CARD_CVV: credit_card_data["code"],
                BitwardenConstants.CREDIT_CARD_BRAND: credit_card_data["brand"],
            }
            mapped_credit_card_data.update(_extract_credit_card_extra_custom_field_values(item))

            return mapped_credit_card_data

        identity = _VaultIdentity.resolve(client_id, client_secret, email, master_password)
        return await BitwardenService._with_cli_session(identity, timeout=timeout, run=fetch)

    @staticmethod
    async def get_credit_card_data(
        client_id: str | None,
        client_secret: str | None,
        master_password: str,
        bw_organization_id: str | None,
        bw_collection_ids: list[str] | None,
        collection_id: str,
        item_id: str,
        remaining_retries: int = settings.BITWARDEN_MAX_RETRIES,
        fail_reasons: list[str] = [],
        email: str | None = None,
    ) -> dict[str, str]:
        """
        Get the credit card data from the Bitwarden CLI.
        """
        if not is_uuid(item_id):
            raise BitwardenGetItemError(f"Invalid item ID: {item_id}. Check if the item ID is correct")

        if not fail_reasons:
            await BitwardenService._apply_jitter()
        try:
            async with asyncio.timeout(settings.BITWARDEN_TIMEOUT_SECONDS):
                return await BitwardenService._get_credit_card_data(
                    client_id=client_id,
                    client_secret=client_secret,
                    master_password=master_password,
                    bw_organization_id=bw_organization_id,
                    bw_collection_ids=bw_collection_ids,
                    collection_id=collection_id,
                    item_id=item_id,
                    email=email,
                )
        except BitwardenAccessDeniedError as e:
            raise e
        except Exception as e:
            if remaining_retries <= 0:
                raise BitwardenListItemsError(
                    f"Bitwarden CLI failed after all retry attempts. Fail reasons: {fail_reasons}"
                )

            remaining_retries -= 1
            LOG.info("Retrying to get credit card data from Bitwarden", remaining_retries=remaining_retries)
            await asyncio.sleep(_retry_backoff_seconds(len(fail_reasons)))
            return await BitwardenService.get_credit_card_data(
                client_id=client_id,
                client_secret=client_secret,
                master_password=master_password,
                bw_organization_id=bw_organization_id,
                bw_collection_ids=bw_collection_ids,
                collection_id=collection_id,
                item_id=item_id,
                remaining_retries=remaining_retries,
                fail_reasons=fail_reasons + [f"{type(e).__name__}: {str(e)}"],
                email=email,
            )

    @staticmethod
    async def _unlock_using_server(master_password: str) -> None:
        status_response = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/status", retry=3, retry_timeout=30, timeout=120
        )
        status = status_response["data"]["template"]["status"]
        if status != "unlocked":
            await aiohttp_post(
                f"{BITWARDEN_SERVER_BASE_URL}/unlock", data={"password": master_password}, retry_timeout=30, timeout=120
            )

    @staticmethod
    async def _get_login_item_by_id_using_server(item_id: str) -> PasswordCredential:
        response = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}", retry=3, timeout=120, retry_timeout=30
        )
        if not response or response.get("success") is False:
            raise BitwardenGetItemError(f"Failed to get login item by ID: {item_id}")

        login = response["data"]["login"]
        totp = BitwardenService.normalize_totp_config(login.get("totp") or "")
        if not login:
            raise BitwardenGetItemError(f"Item with ID: {item_id} is not a login item")

        return PasswordCredential(
            username=login["username"] or "",
            password=login["password"] or "",
            totp=totp,
        )

    @staticmethod
    async def _create_login_item_using_server(
        bw_organization_id: str,
        collection_id: str,
        name: str,
        credential: PasswordCredential,
    ) -> str:
        item_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item", timeout=120)
        login_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item.login", timeout=120)

        item_template = item_template["data"]["template"]
        login_template = login_template["data"]["template"]

        login_template["username"] = credential.username
        login_template["password"] = credential.password
        login_template["totp"] = credential.totp

        item_template["type"] = get_bitwarden_item_type_code(BitwardenItemType.LOGIN)
        item_template["name"] = name
        item_template["login"] = login_template
        item_template["collectionIds"] = [collection_id]
        item_template["organizationId"] = bw_organization_id
        item_template["fields"] = _build_bitwarden_login_custom_fields(credential)

        response = await aiohttp_post(f"{BITWARDEN_SERVER_BASE_URL}/object/item", data=item_template, timeout=120)
        if not response or response.get("success") is False:
            raise BitwardenCreateLoginItemError("Failed to create login item")

        return response["data"]["id"]

    @staticmethod
    async def _create_credit_card_item_using_server(
        bw_organization_id: str,
        collection_id: str,
        name: str,
        credential: CreditCardCredential,
    ) -> str:
        item_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item", timeout=120)
        credit_card_template = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/object/template/item.card", timeout=120
        )

        item_template = item_template["data"]["template"]
        credit_card_template = credit_card_template["data"]["template"]

        credit_card_template["cardholderName"] = credential.card_holder_name
        credit_card_template["number"] = credential.card_number
        credit_card_template["expMonth"] = credential.card_exp_month
        credit_card_template["expYear"] = credential.card_exp_year
        credit_card_template["code"] = credential.card_cvv
        credit_card_template["brand"] = credential.card_brand

        item_template["type"] = get_bitwarden_item_type_code(BitwardenItemType.CREDIT_CARD)
        item_template["name"] = name
        item_template["card"] = credit_card_template
        item_template["fields"] = _build_bitwarden_custom_fields(credential)
        item_template["collectionIds"] = [collection_id]
        item_template["organizationId"] = bw_organization_id

        response = await aiohttp_post(f"{BITWARDEN_SERVER_BASE_URL}/object/item", data=item_template)
        if not response or response.get("success") is False:
            raise BitwardenCreateCreditCardItemError("Failed to create credit card item")

        return response["data"]["id"]

    @staticmethod
    async def create_credential_item(
        collection_id: str,
        name: str,
        credential: PasswordCredential | CreditCardCredential | SecretCredential,
    ) -> str:
        try:
            master_password, bw_organization_id, _, _ = await BitwardenService._get_skyvern_auth_secrets()

            await BitwardenService._unlock_using_server(master_password)
            if isinstance(credential, PasswordCredential):
                return await BitwardenService._create_login_item_using_server(
                    bw_organization_id=bw_organization_id,
                    collection_id=collection_id,
                    name=name,
                    credential=credential,
                )
            elif isinstance(credential, CreditCardCredential):
                return await BitwardenService._create_credit_card_item_using_server(
                    bw_organization_id=bw_organization_id,
                    collection_id=collection_id,
                    name=name,
                    credential=credential,
                )
            else:
                return await BitwardenService._create_secret_item_using_server(
                    bw_organization_id=bw_organization_id,
                    collection_id=collection_id,
                    name=name,
                    credential=credential,
                )
        except Exception as e:
            raise e

    @staticmethod
    async def _create_secret_item_using_server(
        bw_organization_id: str,
        collection_id: str,
        name: str,
        credential: SecretCredential,
    ) -> str:
        item_template = await aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/template/item", timeout=120)
        secure_note_template = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/object/template/item.securenote", timeout=120
        )

        item_template = item_template["data"]["template"]
        secure_note_template = secure_note_template["data"]["template"]

        item_template["type"] = get_bitwarden_item_type_code(BitwardenItemType.SECURE_NOTE)
        item_template["name"] = name
        item_template["collectionIds"] = [collection_id]
        item_template["organizationId"] = bw_organization_id
        item_template["secureNote"] = secure_note_template
        item_template["notes"] = json.dumps(
            {
                "secret_value": credential.secret_value,
                "secret_label": credential.secret_label,
            }
        )

        response = await aiohttp_post(f"{BITWARDEN_SERVER_BASE_URL}/object/item", data=item_template, timeout=120)
        if not response or response.get("success") is False:
            raise BitwardenCreateLoginItemError("Failed to create secret item")

        return response["data"]["id"]

    @staticmethod
    async def _get_skyvern_auth_master_password() -> str:
        master_password = settings.SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD
        if not master_password:
            secret_key = BitwardenConstants.SKYVERN_AUTH_BITWARDEN_MASTER_PASSWORD
            master_password = await get_aws_client().get_secret(secret_key)
        if not master_password:
            raise BitwardenSecretError("Skyvern auth master password is not set")
        return master_password

    @staticmethod
    async def _get_skyvern_auth_organization_id() -> str:
        bw_organization_id = settings.SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID
        if not bw_organization_id:
            secret_key = BitwardenConstants.SKYVERN_AUTH_BITWARDEN_ORGANIZATION_ID
            bw_organization_id = await get_aws_client().get_secret(secret_key)
        if not bw_organization_id:
            raise BitwardenSecretError("Skyvern auth organization ID is not set")
        return bw_organization_id

    @staticmethod
    async def _get_skyvern_auth_client_id() -> str:
        client_id = settings.SKYVERN_AUTH_BITWARDEN_CLIENT_ID
        if not client_id:
            secret_key = BitwardenConstants.SKYVERN_AUTH_BITWARDEN_CLIENT_ID
            client_id = await get_aws_client().get_secret(secret_key)
        if not client_id:
            raise BitwardenSecretError("Skyvern auth client ID is not set")
        return client_id

    @staticmethod
    async def _get_skyvern_auth_client_secret() -> str:
        client_secret = settings.SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET
        if not client_secret:
            secret_key = BitwardenConstants.SKYVERN_AUTH_BITWARDEN_CLIENT_SECRET
            client_secret = await get_aws_client().get_secret(secret_key)
        if not client_secret:
            raise BitwardenSecretError("Skyvern auth client secret is not set")
        return client_secret

    @staticmethod
    async def create_collection(
        name: str,
    ) -> str:
        """
        Create a collection in Bitwarden and return the collection ID.
        """
        try:
            master_password, bw_organization_id, _, _ = await BitwardenService._get_skyvern_auth_secrets()

            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._create_collection_using_server(bw_organization_id, name)

        except Exception as e:
            raise e

    @staticmethod
    async def _create_collection_using_server(bw_organization_id: str, name: str) -> str:
        collection_template_response = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/object/template/collection", retry=3, retry_timeout=30
        )
        collection_template = collection_template_response["data"]["template"]

        collection_template["name"] = name
        collection_template["organizationId"] = bw_organization_id
        if "groups" not in collection_template:
            collection_template["groups"] = []

        response = await aiohttp_post(
            f"{BITWARDEN_SERVER_BASE_URL}/object/org-collection?organizationId={bw_organization_id}",
            data=collection_template,
        )
        if not response or response.get("success") is False:
            bw_message = response.get("message", "Unknown error") if response else "No response from Bitwarden server"
            raise BitwardenCreateCollectionError(
                f"Failed to create Bitwarden collection for org '{name}': {bw_message}. "
                f"Ensure the Bitwarden vault is unlocked (POST {BITWARDEN_SERVER_BASE_URL}/unlock) "
                f"and the organization ID '{bw_organization_id}' is valid."
            )

        return response["data"]["id"]

    @staticmethod
    async def _get_skyvern_auth_secrets() -> Tuple[str, str, str, str]:
        master_password, bw_organization_id, client_id, client_secret = await asyncio.gather(
            BitwardenService._get_skyvern_auth_master_password(),
            BitwardenService._get_skyvern_auth_organization_id(),
            BitwardenService._get_skyvern_auth_client_id(),
            BitwardenService._get_skyvern_auth_client_secret(),
        )
        return master_password, bw_organization_id, client_id, client_secret

    @staticmethod
    async def get_items_by_item_ids(
        item_ids: list[str],
    ) -> list[CredentialItem]:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._get_items_by_item_ids_using_server(item_ids)
        except Exception as e:
            raise e

    @staticmethod
    async def _get_items_by_item_ids_using_server(item_ids: list[str]) -> list[CredentialItem]:
        responses = await asyncio.gather(
            *[
                aiohttp_get_json(f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}", retry=3, retry_timeout=30)
                for item_id in item_ids
            ]
        )
        if not responses or any(response.get("success") is False for response in responses):
            raise BitwardenGetItemError("Failed to get collection items")

        return [get_list_response_item_from_bitwarden_item(response["data"]) for response in responses]

    @staticmethod
    async def get_collection_items(
        collection_id: str,
    ) -> list[CredentialItem]:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._get_collection_items_using_server(collection_id)
        except Exception as e:
            raise e

    @staticmethod
    async def _get_collection_items_using_server(collection_id: str) -> list[CredentialItem]:
        response = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/list/object/items?collectionId={collection_id}",
            retry=3,
            retry_timeout=30,
            timeout=120,
        )
        if not response or response.get("success") is False:
            raise BitwardenGetItemError("Failed to get collection items")

        items = response["data"]["data"]
        items = map(lambda item: get_list_response_item_from_bitwarden_item(item), items)
        return list(items)

    @staticmethod
    async def get_credential_item(
        item_id: str,
    ) -> CredentialItem:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            return await BitwardenService._get_credential_item_by_id_using_server(item_id)
        except Exception as e:
            raise e

    @staticmethod
    async def _get_credential_item_by_id_using_server(item_id: str) -> CredentialItem:
        response = await aiohttp_get_json(
            f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}", retry=3, timeout=120, retry_timeout=30
        )
        if not response or response.get("success") is False:
            raise BitwardenGetItemError(f"Failed to get credential item by ID: {item_id}")

        if response["data"]["type"] == BitwardenItemType.LOGIN:
            login_item = response["data"]["login"]
            name = response["data"]["name"]
            return CredentialItem(
                item_id=item_id,
                credential_type=CredentialType.PASSWORD,
                name=name,
                credential=PasswordCredential(
                    username=login_item["username"] or "",
                    password=login_item["password"] or "",
                    totp=BitwardenService.normalize_totp_config(login_item.get("totp") or ""),
                    metadata=_password_metadata_from_bitwarden_item(response["data"]),
                ),
                login_uris=_whole_site_login_uris(login_item),
            )
        elif response["data"]["type"] == BitwardenItemType.CREDIT_CARD:
            name = response["data"]["name"]
            return CredentialItem(
                item_id=item_id,
                credential_type=CredentialType.CREDIT_CARD,
                name=name,
                credential=_credit_card_credential_from_bitwarden_item(response["data"]),
            )
        elif response["data"]["type"] == BitwardenItemType.SECURE_NOTE:
            name = response["data"]["name"]
            notes = response["data"].get("notes") or ""
            secret_value = ""
            secret_label = None
            try:
                parsed_notes = json.loads(notes)
                if isinstance(parsed_notes, dict):
                    secret_value = parsed_notes.get("secret_value", "") or ""
                    secret_label = parsed_notes.get("secret_label")
                else:
                    secret_value = notes
            except Exception:
                secret_value = notes

            return CredentialItem(
                item_id=item_id,
                credential_type=CredentialType.SECRET,
                name=name,
                credential=SecretCredential(secret_value=secret_value, secret_label=secret_label),
            )
        else:
            raise BitwardenGetItemError(f"Unsupported item type: {response['data']['type']}")

    @staticmethod
    async def delete_credential_item(
        item_id: str,
    ) -> None:
        try:
            master_password, _, _, _ = await BitwardenService._get_skyvern_auth_secrets()
            await BitwardenService._unlock_using_server(master_password)
            await BitwardenService._delete_credential_item_using_server(item_id)
        except Exception as e:
            raise e

    @staticmethod
    async def _delete_credential_item_using_server(item_id: str) -> None:
        await aiohttp_delete(f"{BITWARDEN_SERVER_BASE_URL}/object/item/{item_id}", timeout=120)
