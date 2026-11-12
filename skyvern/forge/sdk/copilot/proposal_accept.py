import contextlib
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import structlog
import yaml
from fastapi import HTTPException, status
from pydantic import ValidationError

from skyvern.constants import DEFAULT_WORKFLOW_TITLES
from skyvern.exceptions import WorkflowNotFound
from skyvern.forge import app
from skyvern.forge.sdk.copilot.context import ProposalDisposition
from skyvern.forge.sdk.copilot.tools.workflow_update import (
    private_workflow_settings_from_proposal,
    proposal_workflow_fingerprint,
    proposal_workflow_fingerprint_matches,
)
from skyvern.forge.sdk.copilot.workflow_yaml import (
    _normalize_copilot_yaml,
    dump_workflow_yaml,
    inherited_header_settings,
    merge_private_workflow_settings,
    strip_private_workflow_settings,
)
from skyvern.forge.sdk.db.enums import WorkflowBuildSource
from skyvern.forge.sdk.db.exceptions import CopilotProposalConflictError
from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.schemas.workflow_copilot import (
    COPILOT_PROPOSAL_METADATA_KEY,
    CopilotProposalMetadata,
    WorkflowCopilotChat,
    copilot_proposal_metadata,
)
from skyvern.forge.sdk.services.request_principal import get_request_principal
from skyvern.forge.sdk.workflow.exceptions import WorkflowVersionConflict
from skyvern.forge.sdk.workflow.models.workflow import Workflow
from skyvern.schemas.workflows import WorkflowCreateYAMLRequest
from skyvern.utils.yaml_loader import safe_load_no_dates

LOG = structlog.get_logger()


def _client_sent_no_proposal_token(owner_turn_id: str | None, revision: int | None) -> bool:
    """Whether the caller predates proposal tokens entirely.

    A frontend deployed before this change never sends them, and one already open in a tab outlives
    the rollout. Such a client is acting on whatever the chat currently holds, which is how this
    endpoint behaved before tokens existed; the canonical and claim checks still apply.
    """
    return owner_turn_id is None and revision is None


async def _resolve_proposal_for_accept(
    proposal: dict[str, Any], *, organization_id: str, workflow_permanent_id: str
) -> WorkflowCreateYAMLRequest:
    data = safe_load_no_dates(proposal["_copilot_yaml"])
    if not isinstance(data, dict):
        return _normalize_copilot_yaml(proposal["_copilot_yaml"])
    submitted = private_workflow_settings_from_proposal(proposal)
    stored = await app.DATABASE.workflows.get_workflow_by_permanent_id(
        workflow_permanent_id=workflow_permanent_id, organization_id=organization_id
    )
    private_settings = merge_private_workflow_settings(
        submitted,
        inherited_settings=inherited_header_settings(stored) if stored is not None else None,
    )
    strip_private_workflow_settings(data)
    data.pop("extra_http_headers", None)
    data.pop("cdp_connect_headers", None)
    data.update({key: value for key, value in private_settings.items() if key not in data})
    yaml_request = _normalize_copilot_yaml(dump_workflow_yaml(data))
    if "workflow_definition" in proposal:
        # Header snapshots can be masked or stale; only authored YAML overrides saved headers.
        resolved = Workflow.model_validate(proposal).model_dump(mode="json")
        if (
            "max_elapsed_time_minutes" not in yaml_request.model_fields_set
            and resolved["max_elapsed_time_minutes"] is None
        ):
            # Pre-upgrade proposals used null for an omitted limit; retain save-service inheritance.
            resolved.pop("max_elapsed_time_minutes")
        if "extra_http_headers" not in yaml_request.model_fields_set:
            resolved.pop("extra_http_headers")
        if "cdp_connect_headers" not in yaml_request.model_fields_set:
            resolved.pop("cdp_connect_headers")
        if "proxy_location" not in yaml_request.model_fields_set:
            resolved.pop("proxy_location")
        if "totp_identifier" not in yaml_request.model_fields_set:
            resolved.pop("totp_identifier")
        if "totp_verification_url" not in yaml_request.model_fields_set:
            resolved.pop("totp_verification_url")
        if "webhook_callback_url" not in yaml_request.model_fields_set:
            resolved.pop("webhook_callback_url")
        resolved.update(yaml_request.model_dump(exclude_unset=True))
        if "cdp_connect_headers" in yaml_request.model_fields_set:
            resolved["cdp_connect_headers"] = yaml_request.cdp_connect_headers
        yaml_request = WorkflowCreateYAMLRequest.model_validate(resolved)

    return yaml_request


def _accepted_workflow_title(
    frozen_title: str, canonical: Workflow | None, metadata: CopilotProposalMetadata | None
) -> str:
    """The title an Accept writes: a rename that landed since publication outranks the frozen one."""
    live_title = canonical.title if canonical is not None else None
    if not live_title or live_title == frozen_title:
        return frozen_title
    if metadata is not None and metadata.canonical_title is not None:
        renamed_since_publication = live_title != metadata.canonical_title
    else:
        # A proposal published before the title was recorded has nothing to compare against, so
        # only a placeholder it froze yields to the live name.
        renamed_since_publication = frozen_title in DEFAULT_WORKFLOW_TITLES and (
            live_title not in DEFAULT_WORKFLOW_TITLES
        )
    return live_title if renamed_since_publication else frozen_title


async def accept_copilot_proposal(
    organization: Organization,
    chat: WorkflowCopilotChat,
    *,
    owner_turn_id: str | None,
    revision: int | None,
    auto_accept: bool | None,
) -> tuple[Workflow, tuple[str, ...]]:
    """Stamp v1, write a new copilot-attributed version and clear the proposal; refusals raise HTTPException."""

    def log_accept_refused(status_code: int) -> None:
        LOG.info(
            "Copilot proposal accept refused",
            organization_id=organization.organization_id,
            workflow_permanent_id=chat.workflow_permanent_id,
            workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
            status_code=status_code,
        )

    proposal = chat.proposed_workflow
    if not proposal:
        log_accept_refused(status.HTTP_400_BAD_REQUEST)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No proposed workflow to apply")

    metadata = copilot_proposal_metadata(proposal)
    if COPILOT_PROPOSAL_METADATA_KEY in proposal and metadata is None:
        log_accept_refused(status.HTTP_409_CONFLICT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Copilot proposal metadata is invalid; reload required",
        )
    if metadata is not None:
        untokenized_client = _client_sent_no_proposal_token(owner_turn_id, revision)
        if not untokenized_client and (owner_turn_id != metadata.owner_turn_id or revision != metadata.revision):
            log_accept_refused(status.HTTP_409_CONFLICT)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Copilot proposal changed; reload required"
            )
        if metadata.claim_is_live(datetime.now(UTC)):
            log_accept_refused(status.HTTP_409_CONFLICT)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Copilot proposal is already being accepted"
            )
    # A lapsed claim is being taken over, so the disposition to restore on failure is the
    # reviewable one this accept started from, never the dead claim's own marker.
    original_disposition: ProposalDisposition = (
        cast(ProposalDisposition, metadata.disposition)
        if metadata is not None and metadata.disposition != "accepting"
        else "review_untested"
    )

    if not proposal.get("_copilot_yaml"):
        log_accept_refused(status.HTTP_400_BAD_REQUEST)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Proposed workflow has no copilot YAML to apply",
        )

    # Claim before the canonical check and the write: a history read that overtakes this POST must
    # already see a write in flight, or it reports the proposal unsaved and releases the Save hold.
    base_version: int | None = None
    try:
        if metadata is None:
            # A tokenless proposal has nothing to claim, so it gets a token that is claimed from the start.
            claimed, base_version = await app.DATABASE.workflow_params.claim_untokened_workflow_copilot_candidate(
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                expected_proposal=proposal,
                owner_turn_id=owner_turn_id or uuid.uuid4().hex,
            )
        else:
            claimed = await app.DATABASE.workflow_params.claim_workflow_copilot_candidate(
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                expected_owner_turn_id=metadata.owner_turn_id,
                expected_revision=metadata.revision,
            )
    except CopilotProposalConflictError:
        log_accept_refused(status.HTTP_409_CONFLICT)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Copilot proposal changed; reload required")
    claim = copilot_proposal_metadata(claimed.proposed_workflow)
    if claim is None:
        raise RuntimeError("Copilot proposal claim returned no ownership token")

    workflow_changed = False
    try:
        if metadata is None and base_version is None:
            # With no saved version at the claim there is nothing to hold a later save against.
            workflow_changed = True
            log_accept_refused(status.HTTP_409_CONFLICT)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow changed after this proposal")
        canonical = await app.DATABASE.workflows.get_workflow_by_permanent_id(
            workflow_permanent_id=chat.workflow_permanent_id,
            organization_id=organization.organization_id,
        )
        if canonical is None or (
            metadata is not None
            and not proposal_workflow_fingerprint_matches(canonical, metadata.canonical_fingerprint)
        ):
            workflow_changed = True
            log_accept_refused(status.HTTP_409_CONFLICT)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow changed after this proposal")
        try:
            yaml_request = await _resolve_proposal_for_accept(
                proposal,
                organization_id=organization.organization_id,
                workflow_permanent_id=chat.workflow_permanent_id,
            )
        except (yaml.YAMLError, ValidationError) as e:
            log_accept_refused(status.HTTP_400_BAD_REQUEST)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Proposed copilot YAML is invalid: {e}",
            )
        live_title = _accepted_workflow_title(yaml_request.title, canonical, metadata)
        if live_title != yaml_request.title:
            LOG.info(
                "copilot_accept_title_refreshed",
                workflow_permanent_id=chat.workflow_permanent_id,
                frozen_title=yaml_request.title,
            )
            yaml_request.title = live_title

        # The proposal can name another workflow's seed profile or session, so it is checked like a direct save.
        await app.AGENT_FUNCTION.enforce_folder_access_for_references(
            get_request_principal(), "workflow.run", yaml_request.model_dump(exclude={"folder_id"})
        )
        try:
            new_workflow, changed_fields = await app.WORKFLOW_SERVICE.create_workflow_from_request(
                organization=organization,
                request=yaml_request,
                workflow_permanent_id=chat.workflow_permanent_id,
                edited_by="copilot",
                build_source=WorkflowBuildSource.copilot,
                validate_code_block_templates=False,
                expected_version=canonical.version if metadata is not None else base_version,
                return_write_result=True,
            )
        except (WorkflowVersionConflict, WorkflowNotFound) as e:
            # A save or delete landed after the claim; this is the refusal the client reads as "nothing written".
            workflow_changed = True
            log_accept_refused(status.HTTP_409_CONFLICT)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Workflow changed after this proposal"
            ) from e
    except Exception:
        with contextlib.suppress(Exception):
            # A failed release must not replace the refusal the caller needs to see.
            await app.DATABASE.workflow_params.release_workflow_copilot_candidate_claim(
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                expected_owner_turn_id=claim.owner_turn_id,
                expected_revision=claim.revision,
                expected_claimed_at=claim.claimed_at,
                disposition=original_disposition,
                # A tokenless proposal the workflow moved past keeps its minted token, whose empty fingerprint
                # hides it, so a second Accept cannot claim against the newer save and write over it.
                restore_untokened=metadata is None and not workflow_changed,
            )
        raise

    # Best-effort: a 500 here would invite a retry that creates a duplicate version. The version has
    # landed, so a second attempt records the acceptance rather than re-offering the proposal.
    for attempt in range(2):
        try:
            await app.DATABASE.workflow_params.clear_workflow_copilot_candidate(
                organization_id=organization.organization_id,
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                expected_owner_turn_id=claim.owner_turn_id,
                expected_revision=claim.revision,
                expected_disposition="accepting",
                expected_claimed_at=claim.claimed_at,
                auto_accept=auto_accept,
                # A server-minted legacy claim is not a receipt unless the client can match it to a turn.
                record_acceptance=metadata is not None or bool(owner_turn_id),
            )
            break
        except Exception as e:
            LOG.warning(
                "Failed to clear copilot proposal after applying it; new workflow version was created",
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                new_workflow_id=new_workflow.workflow_id,
                attempt=attempt + 1,
                exc_info=True,
            )
            if isinstance(e, CopilotProposalConflictError):
                # The claim moved to another holder; a second attempt cannot match it.
                break

    if metadata is not None:
        try:
            # Read back, since the returned version can predate the row's final modified_at.
            accepted = await app.DATABASE.workflows.get_workflow_by_permanent_id(
                workflow_permanent_id=chat.workflow_permanent_id, organization_id=organization.organization_id
            )
            # Another chat can rewrite this version in place under the same id; a waiting card must not run that.
            if (
                accepted is not None
                and accepted.workflow_id == new_workflow.workflow_id
                and proposal_workflow_fingerprint(accepted) == proposal_workflow_fingerprint(new_workflow)
                and accepted.cdp_connect_headers == new_workflow.cdp_connect_headers
            ):
                await app.DATABASE.workflow_params.record_copilot_draft_acceptance(
                    organization.organization_id,
                    chat.workflow_copilot_chat_id,
                    owner_turn_id=metadata.owner_turn_id,
                    revision=metadata.revision,
                    workflow_id=accepted.workflow_id,
                    version=accepted.version,
                    modified_at=accepted.modified_at,
                )
        except Exception:
            LOG.warning(
                "Failed to record the accepted version on the chat's waiting run cards",
                workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
                exc_info=True,
            )

    LOG.info(
        "Copilot proposal accepted",
        organization_id=organization.organization_id,
        workflow_permanent_id=chat.workflow_permanent_id,
        workflow_copilot_chat_id=chat.workflow_copilot_chat_id,
    )
    return new_workflow, changed_fields
