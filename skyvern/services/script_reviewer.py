from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from typing import Literal, Sequence

import json_repair
import libcst as cst
import structlog

from skyvern.core.script_generations.generate_script import (
    MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
    MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB,
)
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.workflow.models.block import get_all_blocks
from skyvern.forge.sdk.workflow.models.parameter import is_sensitive_workflow_parameter
from skyvern.schemas.scripts import ScriptBranchHit, ScriptFallbackEpisode

LOG = structlog.get_logger()

# A literal qualifies for substring-match (vs exact-match) only if it has
# whitespace AND is at least 8 characters long. Whitespace is the primary
# discriminator (structural Python identifiers — dict keys, kwargs, status
# enums — never contain whitespace); the length floor only suppresses
# trivially-short whitespace-bearing strings.
PROSE_LITERAL_MIN_LEN = 8


async def load_filtered_run_param_values(workflow_run_id: str) -> dict[str, str]:
    """Load run-parameter values for the reviewer's hardcoded-value detector.

    Pipeline: fetch ``(WorkflowParameter, RunParameter)`` tuples from the DB,
    drop secret/credential params via the unified ``is_sensitive_workflow_parameter``
    helper, return ``{key -> value}``. Never raises (DB errors are logged and
    swallowed so the reviewer flow continues).
    """
    try:
        param_tuples = await app.DATABASE.workflow_runs.get_workflow_run_parameters(
            workflow_run_id=workflow_run_id,
        )
    except Exception:
        LOG.warning(
            "Failed to load run parameter values",
            workflow_run_id=workflow_run_id,
            exc_info=True,
        )
        return {}
    result: dict[str, str] = {}
    for wf_param, run_param in param_tuples:
        if run_param.value is None or not str(run_param.value).strip():
            continue
        if is_sensitive_workflow_parameter(wf_param):
            continue
        result[wf_param.key] = str(run_param.value)
    return result


@dataclasses.dataclass(frozen=True)
class BlockReviewResult:
    """Result of reviewing a single block, carrying the code and LLM artifacts."""

    code: str
    original_prompt: str
    """The initial prompt sent to the LLM, containing full fallback episode context,
    DOM snapshots, and review instructions."""
    final_prompt: str
    """The prompt that produced the accepted code. Same as original_prompt on first
    attempt; on retry this is the retry prompt with validation error context."""
    llm_response_raw: str


async def store_review_artifacts(
    organization_id: str,
    script_id: str,
    script_version: int,
    review_results: dict[str, BlockReviewResult],
) -> None:
    """Store reviewer prompt/response artifacts for each reviewed block.

    Failures are logged as warnings but never propagate — artifact persistence
    must not block the review pipeline.
    """
    for block_label, result in review_results.items():
        try:
            await app.ARTIFACT_MANAGER.create_script_file_artifact(
                organization_id=organization_id,
                script_id=script_id,
                script_version=script_version,
                file_path=f"review/{block_label}_prompt.txt",
                data=result.original_prompt.encode("utf-8"),
            )
            # Store retry prompt separately if the reviewer retried (validation failure)
            if result.final_prompt != result.original_prompt:
                await app.ARTIFACT_MANAGER.create_script_file_artifact(
                    organization_id=organization_id,
                    script_id=script_id,
                    script_version=script_version,
                    file_path=f"review/{block_label}_retry_prompt.txt",
                    data=result.final_prompt.encode("utf-8"),
                )
            await app.ARTIFACT_MANAGER.create_script_file_artifact(
                organization_id=organization_id,
                script_id=script_id,
                script_version=script_version,
                file_path=f"review/{block_label}_response.json",
                data=result.llm_response_raw.encode("utf-8"),
            )
        except Exception:
            LOG.warning(
                "Failed to store reviewer artifacts",
                block_label=block_label,
                script_id=script_id,
                exc_info=True,
            )


# Exhaustive allowlist of valid page.* API references.
# Anything not in this set that appears as `page.<name>` in generated code is an error.
_ALLOWED_PAGE_API: frozenset[str] = frozenset(
    {
        # Navigation
        "goto",
        "reload_page",
        # Interaction
        "click",
        "hover",
        "fill",
        "fill_autocomplete",
        "select_option",
        "upload_file",
        "type",
        "scroll",
        "keypress",
        "move",
        "drag",
        "left_mouse",
        # Data extraction & classification
        "extract",
        "validate",
        "classify",
        "extract_form_fields",
        # Form filling
        "fill_form",
        "dynamic_field_map",
        "fill_from_mapping",
        "validate_mapping",
        # Lifecycle
        "complete",
        "wait",
        "solve_captcha",
        "download_file",
        "null_action",
        "terminate",
        "verification_code",
        # AI fallback
        "element_fallback",
        "prompt",
        # Quality
        "structural_validate",
        "quality_audit",
        # Properties (accessed as page.url, not page.url())
        "url",
    }
)


class ScriptReviewer:
    """Reviews fallback episodes and proposes updated cached scripts with new branches.

    Key design principles:
    1. Existing code is NOT wrong — it worked for previous runs.
       The reviewer ADDS branches, never removes existing paths.
    2. Generates full function code — simpler than diffs/patches.
    3. Validates output — compile() check before persisting.
    4. Creates new versions — old versions remain as rollback targets.
    """

    async def review_fallback_episodes(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        script_revision_id: str | None,
        episodes: list[ScriptFallbackEpisode],
        stale_branches: list[ScriptBranchHit] | None = None,
        historical_episodes: list[ScriptFallbackEpisode] | None = None,
        run_parameter_values: dict[str, str] | None = None,
        user_instructions: str | None = None,
    ) -> dict[str, BlockReviewResult] | None:
        """Review fallback episodes and generate updated code for affected blocks.

        Returns {block_label: BlockReviewResult} or None if review fails.
        """
        if not episodes:
            return None

        # Load the workflow definition to get navigation goals, parameter keys, and block criteria
        navigation_goals, all_parameter_keys, block_criteria = await self._load_workflow_context(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )

        # Group stale branches by block label
        stale_by_block: dict[str, list[ScriptBranchHit]] = {}
        for branch in stale_branches or []:
            if branch.block_label not in stale_by_block:
                stale_by_block[branch.block_label] = []
            stale_by_block[branch.block_label].append(branch)

        # Group historical episodes by block label
        history_by_block: dict[str, list[ScriptFallbackEpisode]] = {}
        for ep in historical_episodes or []:
            if ep.block_label not in history_by_block:
                history_by_block[ep.block_label] = []
            history_by_block[ep.block_label].append(ep)

        # Batch-load parameter values for historical episodes so the reviewer
        # can detect per-run values (e.g., different provider names across runs).
        # Passed explicitly to _review_block to avoid implicit instance state.
        historical_run_params: dict[str, dict[str, str]] = {}
        if historical_episodes:
            unique_run_ids = list({ep.workflow_run_id for ep in historical_episodes if ep.workflow_run_id})[:20]

            async def _load(run_id: str) -> tuple[str, dict[str, str]]:
                return run_id, await load_filtered_run_param_values(run_id)

            results = await asyncio.gather(*[_load(rid) for rid in unique_run_ids])
            historical_run_params = {rid: params for rid, params in results if params}

        # Triage failed episodes — skip non-code-fixable failures.
        # When user provides explicit instructions, skip triage entirely.
        if user_instructions:
            triaged_episodes = list(episodes)
        else:
            triaged_episodes = []
            for episode in episodes:
                if await self._triage_episode(episode, organization_id):
                    triaged_episodes.append(episode)
                else:
                    # Mark as reviewed so we don't re-triage on every run
                    await app.DATABASE.scripts.mark_episode_reviewed(
                        episode_id=episode.episode_id,
                        organization_id=organization_id,
                        reviewer_output="TRIAGE: not_code_fixable — skipped",
                    )
                    LOG.info(
                        "ScriptReviewer: skipping non-code-fixable episode",
                        episode_id=episode.episode_id,
                        block_label=episode.block_label,
                    )

        if not triaged_episodes:
            return None

        # Group episodes by block label
        episodes_by_block: dict[str, list[ScriptFallbackEpisode]] = {}
        for episode in triaged_episodes:
            if episode.block_label not in episodes_by_block:
                episodes_by_block[episode.block_label] = []
            episodes_by_block[episode.block_label].append(episode)

        updated_blocks: dict[str, BlockReviewResult] = {}

        for block_label, block_episodes in episodes_by_block.items():
            try:
                result = await self._review_block(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    script_revision_id=script_revision_id,
                    block_label=block_label,
                    episodes=block_episodes,
                    navigation_goal=navigation_goals.get(block_label),
                    stale_branches=stale_by_block.get(block_label),
                    all_parameter_keys=all_parameter_keys,
                    historical_episodes=history_by_block.get(block_label),
                    run_parameter_values=run_parameter_values,
                    user_instructions=user_instructions,
                    historical_run_params=historical_run_params,
                    block_criteria=block_criteria.get(block_label),
                )
                if result:
                    updated_blocks[block_label] = result
            except Exception:
                LOG.exception(
                    "ScriptReviewer: failed to review block",
                    block_label=block_label,
                    organization_id=organization_id,
                )

        if not updated_blocks:
            return None

        return updated_blocks

    async def review_with_user_instructions(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        script_revision_id: str | None,
        user_instructions: str,
        episodes: list[ScriptFallbackEpisode] | None = None,
        run_parameter_values: dict[str, str] | None = None,
    ) -> dict[str, BlockReviewResult] | None:
        """Review script blocks using user-provided instructions.

        When episodes are available, they are included as context for the LLM.
        When no episodes are available, the reviewer works from the existing code
        and the user's instructions alone.

        Returns {block_label: BlockReviewResult} or None if review fails.
        """
        if episodes:
            return await self.review_fallback_episodes(
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
                script_revision_id=script_revision_id,
                episodes=episodes,
                run_parameter_values=run_parameter_values,
                user_instructions=user_instructions,
            )

        # No episodes — review all blocks with user instructions only
        navigation_goals, all_parameter_keys, block_criteria = await self._load_workflow_context(
            organization_id=organization_id,
            workflow_permanent_id=workflow_permanent_id,
        )
        block_codes = await self._load_all_block_codes(
            organization_id=organization_id,
            script_revision_id=script_revision_id,
        )
        if not block_codes:
            LOG.warning(
                "ScriptReviewer: no blocks found for instruction-only review",
                organization_id=organization_id,
                workflow_permanent_id=workflow_permanent_id,
            )
            return None

        updated_blocks: dict[str, BlockReviewResult] = {}
        for block_label, existing_code in block_codes.items():
            try:
                result = await self._review_block(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    script_revision_id=script_revision_id,
                    block_label=block_label,
                    episodes=[],
                    navigation_goal=navigation_goals.get(block_label),
                    all_parameter_keys=all_parameter_keys,
                    run_parameter_values=run_parameter_values,
                    user_instructions=user_instructions,
                    preloaded_code=existing_code,
                    block_criteria=block_criteria.get(block_label),
                )
                if result:
                    updated_blocks[block_label] = result
            except Exception:
                LOG.exception(
                    "ScriptReviewer: failed to review block with instructions",
                    block_label=block_label,
                    organization_id=organization_id,
                )

        return updated_blocks if updated_blocks else None

    async def review_conditional_blocks(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        conditional_episodes: list[ScriptFallbackEpisode],
        run_parameter_values: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Review conditional blocks that ran via agent and generate Python code if possible.

        For each conditional block, examines the branch expressions and determines if
        they can be expressed as pure Python (no LLM needed). If so, generates a cached
        function that evaluates the condition and returns the branch decision.

        Returns {block_label: generated_code} for blocks that were successfully converted.
        """
        if not conditional_episodes:
            return None

        # Group episodes by block label — keep only the latest per block
        latest_by_block: dict[str, ScriptFallbackEpisode] = {}
        for ep in conditional_episodes:
            existing = latest_by_block.get(ep.block_label)
            if not existing or (ep.created_at and existing.created_at and ep.created_at > existing.created_at):
                latest_by_block[ep.block_label] = ep

        updated_blocks: dict[str, str] = {}
        for block_label, episode in latest_by_block.items():
            try:
                code = await self._generate_conditional_code(
                    block_label=block_label,
                    episode=episode,
                    organization_id=organization_id,
                    run_parameter_values=run_parameter_values,
                )
                if code:
                    updated_blocks[block_label] = code
            except Exception:
                LOG.exception(
                    "ScriptReviewer: failed to generate conditional code",
                    block_label=block_label,
                )

        return updated_blocks if updated_blocks else None

    async def _generate_conditional_code(
        self,
        block_label: str,
        episode: ScriptFallbackEpisode,
        organization_id: str,
        run_parameter_values: dict[str, str] | None = None,
    ) -> str | None:
        """Generate Python code for a conditional block based on its expression patterns.

        Examines the branch expressions from the episode and asks the LLM to convert
        them into a Python function that evaluates the condition without an LLM call.
        Uses a retry loop (max 2 attempts) to recover from validation errors.
        """
        if not isinstance(episode.agent_actions, dict):
            return None

        expressions = episode.agent_actions.get("expressions", [])
        if not expressions:
            return None

        # Build the prompt for the LLM
        branch_info = []
        for expr in expressions:
            branch_info.append(
                {
                    "original_expression": expr.get("original_expression"),
                    "rendered_expression": expr.get("rendered_expression"),
                    "result": expr.get("result"),
                    "is_default": expr.get("is_default", False),
                    "next_block_label": expr.get("next_block_label"),
                }
            )

        current_prompt = prompt_engine.load_prompt(
            "script-reviewer-conditional",
            block_label=block_label,
            branches=branch_info,
        )

        LOG.info(
            "ScriptReviewer: generating conditional code",
            block_label=block_label,
            num_branches=len(branch_info),
        )

        function_signature = "async def block_fn(page, context):"
        max_attempts = 2

        for attempt in range(1, max_attempts + 1):
            try:
                llm_response = await app.SCRIPT_REVIEWER_LLM_API_HANDLER(
                    prompt=current_prompt,
                    prompt_name="script-reviewer-conditional",
                    step=None,
                    organization_id=organization_id,
                    raw_response=True,
                )

                code = self._extract_code_from_response(
                    llm_response,
                    block_label=block_label,
                    prompt_name="script-reviewer-conditional",
                )
                if not code:
                    LOG.warning(
                        "ScriptReviewer: no code extracted for conditional",
                        block_label=block_label,
                        attempt=attempt,
                        prompt_name="script-reviewer-conditional",
                    )
                    if attempt >= max_attempts:
                        return None
                    continue

                # LLM may signal that the condition can't be expressed as code
                if code.strip() == "CANNOT_CONVERT":
                    LOG.info(
                        "ScriptReviewer: LLM says conditional cannot be converted to code",
                        block_label=block_label,
                    )
                    return None

                # Validate it compiles
                compile_error = self._get_compile_error(code)
                if compile_error:
                    LOG.warning(
                        "ScriptReviewer: conditional code failed compile",
                        block_label=block_label,
                        attempt=attempt,
                        error=compile_error,
                        prompt_name="script-reviewer-conditional",
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(code, compile_error, function_signature)
                    continue

                # Validate page API references (catch hallucinated methods)
                api_error = self._validate_page_api(code)
                if api_error:
                    LOG.warning(
                        "ScriptReviewer: conditional code has invalid page API",
                        block_label=block_label,
                        attempt=attempt,
                        error=api_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(code, api_error, function_signature)
                    continue

                # Validate the function returns the expected structure
                if "next_block_label" not in code:
                    LOG.warning(
                        "ScriptReviewer: conditional code missing next_block_label return",
                        block_label=block_label,
                        attempt=attempt,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(
                            code,
                            "Generated code must return a dict with 'next_block_label' and 'branch_index' keys.",
                            function_signature,
                        )
                    continue

                # Validate returned branch values match the branch definitions
                branch_error = self._validate_branch_returns(code, branch_info)
                if branch_error is not None:
                    LOG.warning(
                        "ScriptReviewer: conditional code has invalid branch returns",
                        block_label=block_label,
                        attempt=attempt,
                        error=branch_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(code, branch_error, function_signature)
                    continue

                # Validate no hardcoded parameter values
                hardcoded_error = self._validate_no_hardcoded_values(code, run_parameter_values)
                if hardcoded_error is not None:
                    LOG.warning(
                        "ScriptReviewer: conditional code has hardcoded parameter values",
                        block_label=block_label,
                        attempt=attempt,
                        error=hardcoded_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(code, hardcoded_error, function_signature)
                    continue

                LOG.info(
                    "ScriptReviewer: generated conditional code",
                    block_label=block_label,
                    attempt=attempt,
                    code_length=len(code),
                )
                return code

            except Exception:
                LOG.exception(
                    "ScriptReviewer: LLM call failed for conditional",
                    block_label=block_label,
                    attempt=attempt,
                )
                if attempt >= max_attempts:
                    return None
                continue

        LOG.warning(
            "ScriptReviewer: all attempts failed for conditional code",
            block_label=block_label,
            max_attempts=max_attempts,
        )
        return None

    async def _triage_episode(
        self,
        episode: ScriptFallbackEpisode,
        organization_id: str,
    ) -> bool:
        """Determine if a failed fallback episode is code-fixable.

        Returns True if the episode should be passed to the reviewer.
        Successful fallback episodes always pass (existing behavior).
        """
        # Successful fallbacks always go to review (existing behavior)
        if episode.fallback_succeeded is not False:
            return True

        # Build triage prompt with rich failure context
        triage_prompt = prompt_engine.load_prompt(
            "script-failure-triage",
            block_label=episode.block_label,
            error_message=episode.error_message,
            page_url=episode.page_url,
            page_text_snapshot=(episode.page_text_snapshot or "")[:3000],
            agent_actions=episode.agent_actions if isinstance(episode.agent_actions, dict) else None,
        )

        try:
            response = await app.SECONDARY_LLM_API_HANDLER(
                prompt=triage_prompt,
                prompt_name="script-failure-triage",
                step=None,
                organization_id=organization_id,
                raw_response=True,
            )

            # raw_response=True returns a dict from model_dump(); extract the text content
            if isinstance(response, dict):
                choices = response.get("choices") or []
                response_text = choices[0]["message"]["content"] if choices else ""
            else:
                response_text = str(response) if response else ""
            response_upper = response_text.upper()
            is_code_fixable = "CODE_FIXABLE" in response_upper and "NOT_CODE_FIXABLE" not in response_upper
            LOG.info(
                "ScriptReviewer: triage result",
                episode_id=episode.episode_id,
                code_fixable=is_code_fixable,
                triage_response=response_text[:200] if response_text else None,
            )
            return is_code_fixable
        except Exception:
            LOG.warning(
                "ScriptReviewer: triage LLM call failed, allowing episode through",
                episode_id=episode.episode_id,
                exc_info=True,
            )
            # On triage failure, let the episode through to avoid silently dropping data
            return True

    async def _review_block(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        script_revision_id: str | None,
        block_label: str,
        episodes: list[ScriptFallbackEpisode],
        navigation_goal: str | None = None,
        stale_branches: list[ScriptBranchHit] | None = None,
        all_parameter_keys: list[str] | None = None,
        historical_episodes: list[ScriptFallbackEpisode] | None = None,
        run_parameter_values: dict[str, str] | None = None,
        user_instructions: str | None = None,
        preloaded_code: str | None = None,
        historical_run_params: dict[str, dict[str, str]] | None = None,
        block_criteria: dict[str, str | dict[str, str] | None] | None = None,
    ) -> BlockReviewResult | None:
        """Review a single block's fallback episodes and generate updated code.

        Returns a BlockReviewResult with the code, prompt, and raw LLM response,
        or None if review fails.
        """
        LOG.info(
            "ScriptReviewer: starting block review",
            block_label=block_label,
            script_revision_id=script_revision_id,
            navigation_goal=navigation_goal[:100] if navigation_goal else None,
        )

        # Use pre-loaded code if available, otherwise fetch from artifact store
        existing_code = preloaded_code or await self._load_block_code(
            organization_id=organization_id,
            script_revision_id=script_revision_id,
            block_label=block_label,
        )

        if not existing_code:
            LOG.warning(
                "ScriptReviewer: no existing code found for block",
                block_label=block_label,
                script_revision_id=script_revision_id,
            )
            return None

        LOG.info(
            "ScriptReviewer: loaded existing code",
            block_label=block_label,
            code_length=len(existing_code),
        )

        # Use provided navigation goal, or fall back to a generic description
        if not navigation_goal:
            navigation_goal = "Complete the navigation task for this block"

        # Infer function signature from existing code
        function_signature = self._extract_function_signature(existing_code)

        # Classify strategy for this block
        strategy = self._classify_block_strategy(existing_code=existing_code)

        # Choose template based on strategy
        if strategy == "extraction":
            template = "script-reviewer-extraction"
        elif strategy == "form_filling":
            template = "script-reviewer-form-filling"
        else:
            template = "script-reviewer"

        LOG.info(
            "ScriptReviewer: classified block strategy",
            block_label=block_label,
            strategy=strategy,
            template=template,
        )

        # Build stale branch info for the template
        stale_branch_info = [
            {
                "branch_key": b.branch_key,
                "last_hit_at": b.last_hit_at.isoformat(),
                "hit_count": b.hit_count,
            }
            for b in (stale_branches or [])
        ]

        # Extract parameter keys from {{ param }} placeholders in the navigation goal
        # and merge with workflow-level parameter keys for a complete list.
        goal_param_keys = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", navigation_goal))
        parameter_keys = sorted(goal_param_keys | set(all_parameter_keys or []))

        # Build historical episode summaries for cross-run context.
        # Include per-run parameter values so the reviewer can detect that
        # different runs had different names/IDs (→ selectors must be dynamic).
        history_summaries = []
        for ep in historical_episodes or []:
            summary: dict[str, object] = {
                "error_message": ep.error_message,
                "reviewer_output": (ep.reviewer_output or "")[:500],
                "fallback_succeeded": ep.fallback_succeeded,
            }
            ep_params = (historical_run_params or {}).get(ep.workflow_run_id)
            if ep_params:
                summary["run_parameters"] = ep_params
            history_summaries.append(summary)

        # Extract block criteria for the template
        terminate_criterion = None
        complete_criterion = None
        error_code_mapping = None
        if block_criteria:
            terminate_criterion = block_criteria.get("terminate_criterion")
            complete_criterion = block_criteria.get("complete_criterion")
            error_code_mapping = block_criteria.get("error_code_mapping")

        # Build the reviewer prompt
        reviewer_prompt = prompt_engine.load_prompt(
            template=template,
            navigation_goal=navigation_goal,
            existing_code=existing_code,
            episodes=[
                {
                    "block_label": ep.block_label,
                    "fallback_type": ep.fallback_type,
                    "error_message": ep.error_message,
                    "classify_result": ep.classify_result,
                    "agent_actions": ep.agent_actions,
                    "page_url": ep.page_url,
                    "page_text_snapshot": (ep.page_text_snapshot or "")[:3000],
                }
                for ep in episodes
            ],
            function_signature=function_signature,
            stale_branches=stale_branch_info,
            parameter_keys=parameter_keys,
            historical_episodes=history_summaries,
            run_parameter_values=run_parameter_values,
            user_instructions=user_instructions,
            terminate_criterion=terminate_criterion,
            complete_criterion=complete_criterion,
            error_code_mapping=error_code_mapping,
        )

        LOG.info(
            "ScriptReviewer: calling LLM",
            block_label=block_label,
            prompt_length=len(reviewer_prompt),
        )

        # Call the LLM to generate updated code with retry on compile errors.
        max_attempts = 3
        current_prompt = reviewer_prompt

        for attempt in range(1, max_attempts + 1):
            try:
                llm_response = await app.SCRIPT_REVIEWER_LLM_API_HANDLER(
                    prompt=current_prompt,
                    prompt_name=template,
                    step=None,
                    organization_id=organization_id,
                    raw_response=True,
                )

                LOG.info(
                    "ScriptReviewer: LLM response received",
                    block_label=block_label,
                    attempt=attempt,
                    prompt_name=template,
                    response_type=type(llm_response).__name__,
                    response_snippet=str(llm_response)[:200],
                )

                updated_code = self._extract_code_from_response(
                    llm_response,
                    block_label=block_label,
                    prompt_name=template,
                )
                if not updated_code:
                    LOG.warning(
                        "ScriptReviewer: no code extracted from response",
                        block_label=block_label,
                        attempt=attempt,
                        prompt_name=template,
                        response_snippet=str(llm_response)[:500],
                    )
                    if attempt >= max_attempts:
                        return None
                    continue

                # Validate the code compiles
                compile_error = self._get_compile_error(updated_code)
                if compile_error is not None:
                    LOG.warning(
                        "ScriptReviewer: compile error, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        prompt_name=template,
                        error=compile_error,
                        code_snippet=updated_code[:300],
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, compile_error, function_signature)
                    continue

                # Validate page API references (catch hallucinated methods like page.completed)
                api_error = self._validate_page_api(updated_code)
                if api_error is not None:
                    LOG.warning(
                        "ScriptReviewer: invalid page API, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=api_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, api_error, function_signature)
                    continue

                # Validate method kwargs (catch invented kwargs like classify(description=...))
                kwargs_error = self._validate_method_kwargs(updated_code)
                if kwargs_error is not None:
                    LOG.warning(
                        "ScriptReviewer: invalid method kwargs, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=kwargs_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, kwargs_error, function_signature)
                    continue

                # Validate type annotations (catch undefined names like Run.RunContext)
                annotation_error = self._validate_type_annotations(updated_code)
                if annotation_error is not None:
                    LOG.warning(
                        "ScriptReviewer: invalid type annotation, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=annotation_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, annotation_error, function_signature)
                    continue

                # Validate classify handling (every classify must have an else branch)
                classify_error = self._validate_classify_handling(updated_code)
                if classify_error is not None:
                    # Try auto-fixing by injecting missing else branches
                    fixed_code = self._auto_fix_missing_else(updated_code, navigation_goal or "")
                    revalidation_error = (
                        self._validate_classify_handling(fixed_code) if fixed_code else "auto-fix returned None"
                    )
                    LOG.info(
                        "ScriptReviewer: auto-fix attempt",
                        block_label=block_label,
                        auto_fix_result="success" if fixed_code and not revalidation_error else "failed",
                        revalidation_error=revalidation_error,
                        original_code_tail=updated_code[-500:] if updated_code else None,
                        fixed_code_tail=fixed_code[-500:] if fixed_code else None,
                    )
                    if fixed_code and not revalidation_error:
                        LOG.info(
                            "ScriptReviewer: auto-fixed missing else branch",
                            block_label=block_label,
                            attempt=attempt,
                        )
                        updated_code = fixed_code
                    else:
                        LOG.warning(
                            "ScriptReviewer: classify validation error, retrying",
                            block_label=block_label,
                            attempt=attempt,
                            error=classify_error,
                        )
                        if attempt < max_attempts:
                            current_prompt = self._build_retry_prompt(updated_code, classify_error, function_signature)
                        continue

                # Validate parameter references (catch invented context.parameters['...'] keys)
                param_error = self._validate_parameter_references(updated_code, parameter_keys)
                if param_error is not None:
                    LOG.warning(
                        "ScriptReviewer: invalid parameter reference, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=param_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, param_error, function_signature)
                    continue

                # Validate parameter references are preserved from existing code.
                # Unlike _validate_structural_regression, this is NOT skipped when user_instructions
                # is set — dropping parameter refs is never intentional and always causes runtime failures.
                preservation_error = self._validate_parameter_preservation(updated_code, existing_code, parameter_keys)
                if preservation_error is not None:
                    LOG.warning(
                        "ScriptReviewer: parameter preservation regression, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=preservation_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, preservation_error, function_signature)
                    continue

                # Validate structural regression (catch deleted branches, shrunk code).
                # Skip when user provides explicit instructions — they may request deletions.
                regression_error = (
                    None if user_instructions else self._validate_structural_regression(updated_code, existing_code)
                )
                if regression_error is not None:
                    LOG.warning(
                        "ScriptReviewer: structural regression detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=regression_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, regression_error, function_signature)
                    continue

                # Validate bare terminate calls (must be inside if/elif, never unconditional)
                terminate_error = self._validate_bare_terminate(updated_code)
                if terminate_error is not None:
                    LOG.warning(
                        "ScriptReviewer: bare terminate detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=terminate_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, terminate_error, function_signature)
                    continue

                # Validate no hardcoded parameter values (catch leaked run-specific data)
                hardcoded_error = self._validate_no_hardcoded_values(updated_code, run_parameter_values)
                if hardcoded_error is not None:
                    LOG.warning(
                        "ScriptReviewer: hardcoded parameter value detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=hardcoded_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, hardcoded_error, function_signature)
                    continue

                # Validate ai='proactive' misuse (should be 'fallback' on interaction methods)
                proactive_error = self._validate_proactive_misuse(updated_code)
                if proactive_error is not None:
                    LOG.warning(
                        "ScriptReviewer: proactive misuse detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=proactive_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, proactive_error, function_signature)
                    continue

                # Validate missing selectors on ai='fallback' interaction methods
                missing_selector_error = self._validate_missing_selectors(updated_code)
                if missing_selector_error is not None:
                    LOG.warning(
                        "ScriptReviewer: missing selector detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=missing_selector_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(
                            updated_code, missing_selector_error, function_signature
                        )
                    continue

                # Validate fragile auto-generated selectors
                fragile_error = self._validate_fragile_selectors(updated_code)
                if fragile_error is not None:
                    LOG.warning(
                        "ScriptReviewer: fragile selector detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=fragile_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, fragile_error, function_signature)
                    continue

                # Validate hardcoded run-specific data in selectors/prompts
                run_data_error = self._validate_hardcoded_run_data(updated_code)
                if run_data_error is not None:
                    LOG.warning(
                        "ScriptReviewer: hardcoded run data detected, retrying",
                        block_label=block_label,
                        attempt=attempt,
                        error=run_data_error,
                    )
                    if attempt < max_attempts:
                        current_prompt = self._build_retry_prompt(updated_code, run_data_error, function_signature)
                    continue

                LOG.info(
                    "ScriptReviewer: generated updated code for block",
                    block_label=block_label,
                    attempt=attempt,
                    code_length=len(updated_code),
                )
                return BlockReviewResult(
                    code=updated_code,
                    original_prompt=reviewer_prompt,
                    final_prompt=current_prompt,
                    llm_response_raw=json.dumps(
                        llm_response if not isinstance(llm_response, str) else {"raw": llm_response},
                        default=str,
                    ),
                )

            except Exception:
                LOG.exception(
                    "ScriptReviewer: LLM call failed",
                    block_label=block_label,
                    attempt=attempt,
                )
                if attempt >= max_attempts:
                    return None
                continue

        LOG.warning(
            "ScriptReviewer: all attempts failed compile check",
            block_label=block_label,
            max_attempts=max_attempts,
        )
        return None

    def _classify_block_strategy(
        self,
        existing_code: str | None,
    ) -> Literal["form_filling", "sequential", "extraction"]:
        """Classify a block's caching strategy based on its existing code.

        Rules (heuristic, no LLM call):
        1. If existing_code contains "page.extract(" without "page.click(" → "extraction"
        2. If existing_code already uses "page.fill_form(" → "form_filling"
           (preserve classification for blocks already using the form-filling API)
        3. Otherwise → "sequential"

        Note: form_filling is NOT inferred from page content or navigation goals.
        The form-filling template produces a FIELD_MAP dict response format that is
        incompatible with blocks that mix clicks, navigation, and downloads. Only
        blocks explicitly using page.fill_form() (static form scripts) should use
        the form-filling template.
        """
        code = existing_code or ""

        # Rule 1: Extraction blocks
        if "page.extract(" in code and "page.click(" not in code:
            return "extraction"

        # Rule 2: Already uses fill_form (preserve existing form-filling classification)
        if "page.fill_form(" in code:
            return "form_filling"

        return "sequential"

    async def _load_block_code(
        self,
        organization_id: str,
        script_revision_id: str | None,
        block_label: str,
    ) -> str | None:
        """Load the current cached code for a block by extracting it from main.py.

        main.py is the single source of truth for all cached block functions.
        Each block is identified by its @skyvern.cached(cache_key='<label>') decorator.
        """
        if not script_revision_id:
            return None

        return await self._extract_block_from_main_py(
            organization_id=organization_id,
            script_revision_id=script_revision_id,
            block_label=block_label,
        )

    async def _extract_block_from_main_py(
        self,
        organization_id: str,
        script_revision_id: str,
        block_label: str,
    ) -> str | None:
        """Extract a block function from main.py when no separate block file exists."""
        from skyvern.services.workflow_script_service import extract_single_cached_block, load_main_py_content

        try:
            content = await load_main_py_content(script_revision_id, organization_id)
            if not content:
                return None
            return extract_single_cached_block(content, block_label)
        except Exception:
            LOG.warning("Failed to extract block from main.py", block_label=block_label, exc_info=True)
        return None

    async def _load_all_block_codes(
        self,
        organization_id: str,
        script_revision_id: str | None,
    ) -> dict[str, str]:
        """Load code for all blocks by extracting from main.py.

        Returns {block_label: code} for all @skyvern.cached functions in main.py.
        """
        from skyvern.services.workflow_script_service import extract_cached_blocks_from_source, load_main_py_content

        if not script_revision_id:
            return {}
        try:
            content = await load_main_py_content(script_revision_id, organization_id)
            if not content:
                return {}
            return extract_cached_blocks_from_source(content)
        except Exception:
            LOG.exception(
                "ScriptReviewer: failed to load all block codes from main.py",
                script_revision_id=script_revision_id,
            )
            return {}

    async def _load_workflow_context(
        self,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> tuple[dict[str, str], list[str], dict[str, dict[str, str | dict[str, str] | None]]]:
        """Load navigation goals, parameter keys, and block criteria for a workflow.

        Returns (goals_by_label, parameter_keys, block_criteria_by_label).
        block_criteria_by_label maps block_label -> {
            "terminate_criterion": str | None,
            "complete_criterion": str | None,
            "error_code_mapping": dict[str, str] | None,
        }
        """
        goals: dict[str, str] = {}
        parameter_keys: list[str] = []
        block_criteria: dict[str, dict[str, str | dict[str, str] | None]] = {}
        try:
            workflow = await app.DATABASE.workflows.get_workflow_by_permanent_id(
                workflow_permanent_id=workflow_permanent_id,
                organization_id=organization_id,
            )
            if workflow and workflow.workflow_definition:
                all_blocks = get_all_blocks(workflow.workflow_definition.blocks)
                for block in all_blocks:
                    if not block.label:
                        continue
                    goal = getattr(block, "navigation_goal", None) or getattr(block, "data_extraction_goal", None)
                    if goal:
                        goals[block.label] = goal
                    # Collect termination/completion criteria and error code mappings
                    terminate_criterion = getattr(block, "terminate_criterion", None)
                    complete_criterion = getattr(block, "complete_criterion", None)
                    error_code_mapping = getattr(block, "error_code_mapping", None)
                    if terminate_criterion or complete_criterion or error_code_mapping:
                        block_criteria[block.label] = {
                            "terminate_criterion": terminate_criterion,
                            "complete_criterion": complete_criterion,
                            "error_code_mapping": error_code_mapping,
                        }
                # Collect parameter keys from workflow definition
                for param in workflow.workflow_definition.parameters:
                    if param.key:
                        parameter_keys.append(param.key)
        except Exception:
            LOG.warning(
                "ScriptReviewer: failed to load workflow context",
                workflow_permanent_id=workflow_permanent_id,
                exc_info=True,
            )
        return goals, parameter_keys, block_criteria

    def _extract_function_signature(self, code: str) -> str:
        """Extract the async function signature from existing code."""
        for line in code.split("\n"):
            stripped = line.strip()
            if stripped.startswith("async def "):
                return stripped
        return "async def block_fn(page, context):"

    def _response_to_text(self, response: object) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, list):
            LOG.warning(
                "ScriptReviewer: LLM returned a list, rejecting",
                response_length=len(response),
            )
            return ""
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices:
                content = choices[0].get("message", {}).get("content") if isinstance(choices[0], dict) else None
                if isinstance(content, str):
                    return content
            try:
                return json.dumps(response)
            except (TypeError, ValueError):
                return ""
        return ""

    def _extract_code_from_response(
        self,
        response: object,
        block_label: str | None = None,
        prompt_name: str | None = None,
    ) -> str | None:
        if not response:
            return None
        text = self._response_to_text(response).strip()
        if not text:
            return None

        if text == "CANNOT_CONVERT":
            return text

        if "```" in text:
            start = text.index("```") + 3
            remaining = text[start:]
            end = remaining.find("```")
            code = remaining[:end] if end != -1 else remaining
            for tag in ("python", "py", "json"):
                if code.startswith(tag) and len(code) > len(tag) and code[len(tag)] in (" ", "\n", "\r"):
                    code = code[len(tag) :]
                    break
            code = code.strip()
            if code.startswith("{"):
                # Fenced content is a JSON object — let json_repair handle it
                # below so well-formed and malformed-Mode-A shapes both extract.
                text = code
            elif "async def " in code:
                return code
            else:
                return None

        if text.startswith("async def "):
            return text

        try:
            parsed = json_repair.loads(text)
        except Exception:
            parsed = None

        if isinstance(parsed, dict):
            code_value = parsed.get("code")
            if isinstance(code_value, str) and code_value.strip():
                code_str = code_value.strip()
                if self._get_compile_error(code_str) is None:
                    return code_str
                if code_str.startswith("async def ") and code_str.count("(") > code_str.count(")") and len(parsed) > 1:
                    # When the LLM emits a Python type annotation inside a JSON string with
                    # unescaped quotes — e.g. `{"code": "async def fn(page: T", "ctx": "U):..."}` —
                    # json_repair splits each `<param>: <Type>` pair into its own dict entry.
                    # Re-joining as `, key: value` reverses the split for the type-annotation
                    # pattern. compile-checked below; falls back to the truncated code on miss.
                    pieces = [code_str]
                    bail = False
                    for k, v in parsed.items():
                        if k == "code":
                            continue
                        if not isinstance(v, str):
                            bail = True
                            break
                        pieces.append(f", {k}: {v}")
                    if not bail:
                        reconstructed = "".join(pieces)
                        log_preview = [str(k)[:50] for k in list(parsed.keys())[:5]]
                        if self._get_compile_error(reconstructed) is None:
                            LOG.info(
                                "ScriptReviewer: malformed-dict recovery applied",
                                block_label=block_label,
                                prompt_name=prompt_name,
                                dict_key_count=len(parsed),
                                dict_key_preview=log_preview,
                            )
                            return reconstructed
                        LOG.warning(
                            "ScriptReviewer: malformed-dict recovery failed compile",
                            block_label=block_label,
                            prompt_name=prompt_name,
                            dict_key_count=len(parsed),
                            dict_key_preview=log_preview,
                        )
                return code_str
            for v in parsed.values():
                if isinstance(v, str) and v.strip().startswith("async def "):
                    return v.strip()

        return None

    def _get_compile_error(self, code: str) -> str | None:
        """Try to compile the code. Returns error message or None if valid.

        Handles Jinja template expressions ({{ ... }} and {% ... %}) that may
        appear inside string literals in extraction prompts by temporarily
        replacing them with placeholders before compiling.
        """
        try:
            # Replace Jinja expressions with valid Python placeholders for compile check.
            # Use a bare identifier (no quotes) to avoid breaking string literals
            # that contain Jinja: "{{ foo }}" → "__JINJA__" instead of """__JINJA__"""
            sanitized = re.sub(r"\{\{.*?\}\}", "__JINJA__", code)
            sanitized = re.sub(r"\{%.*?%\}", "# __JINJA_BLOCK__", sanitized)
            compile(sanitized, "<script-reviewer-output>", "exec")
            return None
        except SyntaxError as e:
            return f"{e.msg} (line {e.lineno}, col {e.offset})"

    @staticmethod
    def _find_call_end(lines: list[str], start_line: int) -> int:
        """Find the line where a multi-line function call ends (matching closing paren).

        Given a line like `result = await page.classify(`, counts open/close parens
        to find the line with the matching `)`. Returns start_line if the call is
        on a single line.
        """
        depth = 0
        for i in range(start_line, len(lines)):
            depth += lines[i].count("(") - lines[i].count(")")
            if depth <= 0:
                return i
        return start_line

    # Max lines to scan between page.classify() and the if chain before giving up.
    _MAX_PRE_IF_SCAN = 8

    def _validate_classify_handling(self, code: str) -> str | None:
        """Validate that every page.classify() call is followed by an if/elif/else chain with an else branch.

        Uses indentation-aware parsing: only considers if/elif/else at the same
        indent level, skipping deeper-indented body lines.

        Returns an error message if validation fails, or None if valid.
        """
        lines = code.split("\n")
        classify_lines = []
        for i, line in enumerate(lines):
            if "page.classify(" in line and not line.lstrip().startswith("#"):
                classify_lines.append(i)

        if not classify_lines:
            return None

        for classify_line_idx in classify_lines:
            found_else = False
            if_indent: int | None = None
            pre_if_lines = 0

            # Skip past multi-line classify() call
            scan_start = self._find_call_end(lines, classify_line_idx) + 1

            for j in range(scan_start, len(lines)):
                stripped = lines[j].strip()
                if not stripped or stripped.startswith("#"):
                    continue

                indent = len(lines[j]) - len(lines[j].lstrip())

                if if_indent is None:
                    # Looking for the first `if` after the classify call
                    if stripped.startswith("if ") or stripped.startswith("if("):
                        if_indent = indent
                    else:
                        # Not an if yet — could be variable assignments between
                        # classify() and the if chain. Keep scanning a few lines.
                        pre_if_lines += 1
                        if pre_if_lines > self._MAX_PRE_IF_SCAN:
                            break  # Too far from classify, not our pattern
                        continue
                else:
                    if indent > if_indent:
                        # Body of the current branch — skip
                        continue
                    elif indent == if_indent:
                        if stripped.startswith("elif "):
                            continue
                        elif stripped.startswith("else:"):
                            found_else = True
                            break
                        else:
                            # Left the if chain without finding else
                            break
                    else:
                        # Dedented past the if chain
                        break

            if if_indent is not None and not found_else:
                return (
                    f"page.classify() on line {classify_line_idx + 1} is missing an `else` branch. "
                    "Every page.classify() MUST have an else branch that calls "
                    "page.element_fallback(). Add an else branch."
                )

        return None

    # Regex to match `page.<name>` references (method calls or property access).
    # Excludes comment lines.
    _PAGE_API_RE = re.compile(r"\bpage\.(\w+)")

    def _validate_page_api(self, code: str) -> str | None:
        """Validate that all page.<name> references use real SkyvernPage methods/properties.

        Returns an error message listing invalid references, or None if all are valid.
        """
        invalid: set[str] = set()
        for line in code.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for match in self._PAGE_API_RE.finditer(line):
                name = match.group(1)
                if name not in _ALLOWED_PAGE_API:
                    invalid.add(name)
        if not invalid:
            return None
        sorted_invalid = sorted(invalid)
        return (
            f"Invalid page API reference(s): {', '.join(f'page.{n}' for n in sorted_invalid)}. "
            f"These do NOT exist on SkyvernPage. Valid methods/properties include: "
            f"page.goto(), page.click(), page.fill(), page.extract(), page.classify(), "
            f"page.complete(), page.element_fallback(), page.url (property). "
            f"Remove or replace the invalid references."
        )

    # Allowlists of valid keyword arguments for key page methods.
    # Anything not in the set triggers a retry so the LLM fixes its code.
    _METHOD_KWARGS: dict[str, frozenset[str]] = {
        "classify": frozenset({"options", "url_patterns", "text_patterns"}),
        "click": frozenset({"selector", "prompt", "ai", "intention", "data", "timeout"}),
        "fill": frozenset(
            {"selector", "value", "ai", "prompt", "intention", "data", "totp_identifier", "totp_url", "timeout"}
        ),
        "fill_autocomplete": frozenset(
            {
                "selector",
                "value",
                "ai",
                "prompt",
                "intention",
                "data",
                "option_selector",
                "totp_identifier",
                "totp_url",
                "timeout",
            }
        ),
        "select_option": frozenset({"selector", "value", "ai", "prompt", "intention", "data", "timeout"}),
        "extract": frozenset({"prompt", "schema", "error_code_mapping", "intention", "data"}),
        "validate": frozenset({"prompt", "model"}),
        "element_fallback": frozenset({"navigation_goal", "max_steps"}),
        "hover": frozenset({"selector", "prompt", "ai", "timeout", "hold_seconds", "intention"}),
        "scroll": frozenset({"direction", "amount", "selector"}),
        "keypress": frozenset({"keys", "prompt"}),
        "wait": frozenset({"timeout_ms"}),
        "terminate": frozenset({"errors"}),
        "complete": frozenset(set()),
        "goto": frozenset({"url", "timeout"}),
    }

    # Regex to match page.method_name(... kwarg_name=...) patterns
    _KWARG_RE = re.compile(r"\bpage\.(\w+)\s*\(")
    _KWARG_NAME_RE = re.compile(r"(?<!['\"])\b(\w+)\s*=(?!=)")
    # Strip string literals so kwargs inside strings (e.g. selector='input[name="x"]')
    # are not mistaken for method kwargs.
    _STRING_RE = re.compile(r"""(?:'''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'[^']*'|"[^"]*")""")

    def _validate_method_kwargs(self, code: str) -> str | None:
        """Validate that page method calls don't use invented keyword arguments.

        Catches errors like page.classify(description=...) where 'description' is
        not a valid parameter. Returns an error message or None if all are valid.
        """
        errors: list[str] = []
        lines = code.split("\n")

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue

            for match in self._KWARG_RE.finditer(line):
                method_name = match.group(1)
                allowed_kwargs = self._METHOD_KWARGS.get(method_name)
                if allowed_kwargs is None:
                    continue  # method not in our validation list

                # Extract the call region (from '(' to matching ')')
                call_start = match.end() - 1
                call_text = line[call_start:]

                # Also check continuation lines (for multi-line calls)
                depth = 0
                for j in range(i, min(i + 10, len(lines))):
                    scan_line = lines[j] if j > i else call_text
                    for ch in scan_line:
                        if ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                    call_text = call_text + "\n" + lines[j] if j > i else call_text
                    if depth <= 0:
                        break

                # Strip string literals to avoid matching kwargs inside strings
                # e.g. selector='input[name="email"]' — "name" is inside a string, not a kwarg
                sanitized = self._STRING_RE.sub("''", call_text)

                # Find keyword arguments in the sanitized call text
                for kwarg_match in self._KWARG_NAME_RE.finditer(sanitized):
                    kwarg_name = kwarg_match.group(1)
                    # Skip Python builtins and common false positives
                    if kwarg_name in (
                        "await",
                        "True",
                        "False",
                        "None",
                        "not",
                        "and",
                        "or",
                        "in",
                        "is",
                        "if",
                        "else",
                        "for",
                        "type",
                    ):
                        continue
                    if kwarg_name not in allowed_kwargs:
                        errors.append(
                            f"page.{method_name}() does not accept '{kwarg_name}=' (line {i + 1}). "
                            f"Valid kwargs: {', '.join(sorted(allowed_kwargs))}"
                        )

        if not errors:
            return None
        return "; ".join(errors[:3])  # Limit to first 3 errors

    # Known imports provided by the skyvern SDK at script load time.
    _KNOWN_NAMES: frozenset[str] = frozenset(
        {
            "SkyvernPage",
            "RunContext",
            "skyvern",
            "asyncio",
            "pydantic",
            "BaseModel",
            "Field",
            "Any",
            "datetime",
            "re",
            "json",
            "math",
            "typing",
        }
    )

    # Pattern: `async def fn_name(page: SkyvernPage, context: <annotation>):`
    _ANNOTATION_RE = re.compile(r":\s*([A-Z]\w+(?:\.\w+)*)")

    def _validate_type_annotations(self, code: str) -> str | None:
        """Validate that type annotations in function signatures use imported names.

        Catches errors like `context: Run.RunContext` where `Run` is not imported.
        Returns an error message or None if all annotations are valid.
        """
        # Collect all imported names
        imported_names: set[str] = set()
        for line in code.split("\n"):
            stripped = line.strip()
            if stripped.startswith("import "):
                # e.g. "import datetime" -> "datetime"
                for part in stripped[len("import ") :].split(","):
                    name = part.strip().split(" as ")[-1].strip()
                    if name:
                        imported_names.add(name)
            elif stripped.startswith("from "):
                # e.g. "from skyvern import RunContext, SkyvernPage"
                if " import " in stripped:
                    imports_part = stripped.split(" import ", 1)[1]
                    for part in imports_part.split(","):
                        name = part.strip().split(" as ")[-1].strip()
                        if name:
                            imported_names.add(name)

        # Add known built-in names
        all_known = imported_names | self._KNOWN_NAMES

        # Check function signature annotations
        errors: list[str] = []
        for line in code.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("async def "):
                continue
            # Find all type annotations in the signature
            for match in self._ANNOTATION_RE.finditer(stripped):
                annotation = match.group(1)
                # Get the root name (e.g., "Run" from "Run.RunContext")
                root_name = annotation.split(".")[0]
                if root_name not in all_known:
                    errors.append(
                        f"Type annotation '{annotation}' references undefined name '{root_name}'. "
                        f"Use 'RunContext' instead of 'Run.RunContext' — RunContext is imported directly."
                    )

        if not errors:
            return None
        return "; ".join(errors[:3])

    # Regex to find context.parameters['key'] or context.parameters["key"]
    _PARAM_REF_RE = re.compile(r"""context\.parameters\[['"](\w+)['"]\]""")

    def _find_param_refs_excluding_comments(self, code: str) -> list[str]:
        """Extract parameter reference keys from code, skipping comment lines."""
        refs: list[str] = []
        for line in code.split("\n"):
            if line.lstrip().startswith("#"):
                continue
            for match in self._PARAM_REF_RE.finditer(line):
                refs.append(match.group(1))
        return refs

    def _validate_parameter_references(self, code: str, parameter_keys: list[str]) -> str | None:
        """Validate that context.parameters['key'] references use known parameter keys.

        Catches KeyError crashes at runtime when the LLM invents parameter names.
        Returns an error message or None if all references are valid.
        """
        if not parameter_keys:
            return None  # No parameter keys to validate against

        valid_keys = set(parameter_keys)
        invalid: list[str] = []

        for line in code.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for match in self._PARAM_REF_RE.finditer(line):
                key = match.group(1)
                if key not in valid_keys:
                    invalid.append(key)

        if not invalid:
            return None

        unique_invalid = sorted(set(invalid))
        return (
            f"Invalid context.parameters references: {', '.join(repr(k) for k in unique_invalid)}. "
            f"Valid parameter keys are: {', '.join(repr(k) for k in sorted(valid_keys))}. "
            f"For fields without a matching parameter, use ai='proactive' with a descriptive prompt "
            f"instead of context.parameters['invented_name']."
        )

    def _validate_parameter_preservation(
        self, new_code: str, existing_code: str | None, parameter_keys: list[str]
    ) -> str | None:
        """Ensure parameter references from existing code are preserved in updated code.

        Catches the case where the LLM drops value=context.parameters['key'] references
        when rewriting block code (e.g., adding classify branches), replacing them with
        ai='proactive' fill() calls that have no value and silently become no-ops.
        """
        if not existing_code or not parameter_keys:
            return None

        old_params = set(self._find_param_refs_excluding_comments(existing_code))
        new_params = set(self._find_param_refs_excluding_comments(new_code))

        # Only flag parameters that are in the valid keys list (ignore spurious refs)
        valid_old = old_params & set(parameter_keys)
        dropped = valid_old - new_params

        if not dropped:
            return None

        return (
            f"Parameter references dropped: {', '.join(f'context.parameters[{k!r}]' for k in sorted(dropped))}. "
            f"The existing code referenced these workflow parameters but the updated code does not. "
            f"Every page.fill() or page.fill_autocomplete() for a field that maps to a workflow parameter "
            f"MUST include value=context.parameters['key']. Do NOT replace with ai='proactive' — "
            f"parameter values from context.parameters are deterministic; AI-generated values are not."
        )

    def _validate_structural_regression(self, new_code: str, existing_code: str | None) -> str | None:
        """Compare new code against existing code to catch regressions.

        Checks:
        1. Code length didn't shrink by more than 50%
        2. Count of page.* calls didn't decrease
        3. Function signature is preserved
        4. Existing classify options weren't deleted

        Returns an error message or None if the new code is acceptable.
        """
        if not existing_code:
            return None  # No existing code to compare against

        # Check 1: Code length regression
        old_len = len(existing_code.strip())
        new_len = len(new_code.strip())
        if old_len > 100 and new_len < old_len * 0.5:
            return (
                f"Code shrank from {old_len} to {new_len} characters ({new_len * 100 // old_len}% of original). "
                f"This likely means existing branches were deleted. "
                f"Keep ALL existing code and only ADD new branches for the new page variant."
            )

        # Check 2: page.* call count didn't decrease
        old_page_calls = len(self._PAGE_API_RE.findall(existing_code))
        new_page_calls = len(self._PAGE_API_RE.findall(new_code))
        if old_page_calls > 2 and new_page_calls < old_page_calls * 0.6:
            return (
                f"Number of page.* calls dropped from {old_page_calls} to {new_page_calls}. "
                f"Existing page interactions should be preserved. "
                f"Keep ALL existing code and only ADD new branches."
            )

        # Check 3: Function signature preserved
        old_sig = self._extract_function_signature(existing_code)
        new_sig = self._extract_function_signature(new_code)
        # Compare function name and params, ignoring whitespace differences
        old_fn_name = old_sig.split("(")[0].strip() if "(" in old_sig else old_sig
        new_fn_name = new_sig.split("(")[0].strip() if "(" in new_sig else new_sig
        if old_fn_name != new_fn_name:
            return (
                f"Function name changed from '{old_fn_name}' to '{new_fn_name}'. "
                f"The function name must remain the same. Use: {old_sig}"
            )

        # Check 4: Classify options preserved
        classify_option_re = re.compile(r"""['"](\w+)['"]\s*:\s*['"]""")
        old_options = set(classify_option_re.findall(existing_code))
        new_options = set(classify_option_re.findall(new_code))
        # Filter to only options that look like classify keys (not generic dict keys)
        # by checking they appear near page.classify
        deleted_options = old_options - new_options
        if deleted_options and "page.classify(" in existing_code:
            return (
                f"Classify options deleted: {', '.join(sorted(deleted_options))}. "
                f"Do NOT remove existing classify branches — they handle page variants "
                f"from previous runs. Add new options alongside existing ones."
            )

        return None

    def _validate_bare_terminate(self, code: str) -> str | None:
        """Validate that page.terminate() is never called unconditionally.

        Every page.terminate() call must be inside an if/elif branch (guarded by
        a classify or extract result). A terminate at function-body level (not
        inside any conditional) is rejected.

        Uses libcst.parse_module to walk the tree and check that every
        page.terminate() Expr node has a cst.If ancestor.

        Note: the prompt distinguishes classify-else (should use element_fallback)
        from extract-else (terminate is acceptable). This validator enforces only
        the structural rule; else-branch semantics are left to the LLM prompt.

        Returns an error message or None if valid.
        """
        # Fast short-circuit: skip libcst parsing when terminate is not present
        if "terminate" not in code:
            return None

        try:
            tree = cst.parse_module(code)
        except cst.ParserSyntaxError:
            return None  # compile check handles syntax errors separately

        # Walk the CST looking for page.terminate() calls not inside an If node.
        # Each top-level FunctionDef is validated independently.  Nested function
        # definitions are intentionally not recursed into — generated script blocks
        # are always a single top-level async function with no inner defs.
        # libcst uses a single FunctionDef for both sync and async.
        for stmt in tree.body:
            func_def = stmt if isinstance(stmt, cst.FunctionDef) else None
            if func_def is None:
                continue
            bare = ScriptReviewer._find_bare_terminate_in_body(func_def.body.body, inside_conditional=False)
            if bare is not None:
                return bare

        return None

    @staticmethod
    def _find_bare_terminate_in_body(
        stmts: Sequence[cst.BaseStatement],
        inside_conditional: bool,
    ) -> str | None:
        """Recursively check statements for bare page.terminate() calls.

        Returns an error message if a bare terminate is found, None otherwise.

        Note: the prompt distinguishes classify-else (should use element_fallback)
        from extract-else (terminate is acceptable). This validator only enforces
        the structural rule (terminate must be inside *some* conditional). Finer
        classify-vs-extract else-branch enforcement is left to the LLM prompt.
        """

        def _unwrap_body(suite: cst.BaseSuite | cst.Else | cst.Finally | None) -> Sequence[cst.BaseStatement]:
            """Extract the statement list from an IndentedBlock, Else, or Finally."""
            if suite is None:
                return ()
            if isinstance(suite, (cst.Else, cst.Finally)):
                suite = suite.body
            if isinstance(suite, cst.IndentedBlock):
                return suite.body
            return ()

        def _check_bodies(bodies: list[Sequence[cst.BaseStatement]], cond: bool) -> str | None:
            """Check multiple statement lists, returning the first error."""
            for body in bodies:
                err = ScriptReviewer._find_bare_terminate_in_body(body, inside_conditional=cond)
                if err:
                    return err
            return None

        for stmt in stmts:
            # In libcst, expression statements are wrapped in SimpleStatementLine
            if isinstance(stmt, cst.SimpleStatementLine):
                for small_stmt in stmt.body:
                    if isinstance(small_stmt, cst.Expr) and ScriptReviewer._is_terminate_call(small_stmt.value):
                        if not inside_conditional:
                            return (
                                "page.terminate() must be inside an if/elif branch — unconditional terminate rejected"
                            )

            # Recurse into if/elif/else bodies
            if isinstance(stmt, cst.If):
                # The if and elif bodies are "inside a conditional".
                # cst.If.orelse is If (elif) | Else (else) | None.
                # Collect all branch bodies via the elif/else chain.
                branch_bodies: list[Sequence[cst.BaseStatement]] = [_unwrap_body(stmt.body)]
                orelse: cst.If | cst.Else | None = stmt.orelse
                while orelse is not None:
                    if isinstance(orelse, cst.If):
                        branch_bodies.append(_unwrap_body(orelse.body))
                        orelse = orelse.orelse
                    elif isinstance(orelse, cst.Else):
                        branch_bodies.append(_unwrap_body(orelse.body))
                        orelse = None
                    else:
                        break
                err = _check_bodies(branch_bodies, cond=True)
                if err:
                    return err

            elif isinstance(stmt, (cst.For, cst.While)):
                bodies: list[Sequence[cst.BaseStatement]] = [_unwrap_body(stmt.body)]
                if stmt.orelse is not None:
                    bodies.append(_unwrap_body(stmt.orelse))
                err = _check_bodies(bodies, cond=inside_conditional)
                if err:
                    return err

            elif isinstance(stmt, cst.With):
                err = _check_bodies([_unwrap_body(stmt.body)], cond=inside_conditional)
                if err:
                    return err

            elif isinstance(stmt, cst.Try):
                # except handler bodies inherit inside_conditional from the
                # enclosing scope (not set to True) — terminate in an except
                # block is "something went wrong" error handling, which should
                # use element_fallback, not terminate.
                handler_bodies = [_unwrap_body(h.body) for h in stmt.handlers]
                err = _check_bodies(
                    [_unwrap_body(stmt.body), *handler_bodies, _unwrap_body(stmt.orelse), _unwrap_body(stmt.finalbody)],
                    cond=inside_conditional,
                )
                if err:
                    return err

            elif isinstance(stmt, cst.TryStar):
                handler_bodies = [_unwrap_body(h.body) for h in stmt.handlers]
                err = _check_bodies(
                    [_unwrap_body(stmt.body), *handler_bodies, _unwrap_body(stmt.orelse), _unwrap_body(stmt.finalbody)],
                    cond=inside_conditional,
                )
                if err:
                    return err

            # Python 3.10+ match/case — each case body is conditional
            elif isinstance(stmt, cst.Match):
                err = _check_bodies([_unwrap_body(case.body) for case in stmt.cases], cond=True)
                if err:
                    return err

            # FunctionDef stmts are intentionally not recursed into —
            # generated scripts never contain nested function definitions.
        return None

    @staticmethod
    def _is_terminate_call(node: cst.BaseExpression) -> bool:
        """Check if a CST expression node is a page.terminate(...) call.

        Handles both ``await page.terminate(...)`` and bare ``page.terminate(...)``.
        Generated scripts always use ``await``, but we check both defensively.

        The caller passes ``small_stmt.value`` (the inner expression of a
        ``cst.Expr`` small statement), so this method receives a ``cst.Await``
        or ``cst.Call`` node, never a ``cst.Expr`` wrapper.
        """
        call = node.expression if isinstance(node, cst.Await) else node
        if not isinstance(call, cst.Call):
            return False
        func = call.func
        return (
            isinstance(func, cst.Attribute)
            and isinstance(func.attr, cst.Name)
            and func.attr.value == "terminate"
            and isinstance(func.value, cst.Name)
            and func.value.value == "page"
        )

    @staticmethod
    def _collect_code_literals(code: str) -> tuple[set[str], set[str]]:
        """Walk ``code`` via libcst and return ``(exact_literals, prose_literals)``.

        Handles single-, double-, and triple-quoted ``cst.SimpleString`` nodes via
        ``evaluated_value``; for ``cst.FormattedString`` (f-strings) concatenates
        the text segments and skips interpolation expressions (their values come
        from ``context.parameters[...]`` at runtime, not from literals). Comments
        are automatically excluded — libcst doesn't expose them as strings.

        On parse failure (the LLM emitted broken Python), returns empty sets so
        the validator skips silently rather than crashing the review pipeline.
        """
        exact: set[str] = set()
        prose: set[str] = set()
        try:
            module = cst.parse_module(code)
        except cst.ParserSyntaxError:
            return exact, prose

        def _add(value: str) -> None:
            if not value:
                return
            exact.add(value)
            if len(value) >= PROSE_LITERAL_MIN_LEN and any(c.isspace() for c in value):
                prose.add(value)

        class _Collector(cst.CSTVisitor):
            def visit_SimpleString(self, node: cst.SimpleString) -> None:
                try:
                    value = node.evaluated_value
                except Exception:
                    return
                if isinstance(value, str):
                    _add(value)

            def visit_FormattedString(self, node: cst.FormattedString) -> bool:
                # Concatenate the f-string's text parts only. Interpolation
                # expressions reference runtime values (``context.parameters[...]``),
                # not literals, so we skip them by returning False (no recursion).
                _add("".join(part.value for part in node.parts if isinstance(part, cst.FormattedStringText)))
                return False

        module.visit(_Collector())
        return exact, prose

    def _validate_no_hardcoded_values(
        self,
        code: str,
        run_parameter_values: dict[str, str] | None,
    ) -> str | None:
        """Detect hardcoded parameter values in generated code.

        Checks if any workflow parameter value from the current run appears as a
        string literal in the code. Catches cases where the LLM copies a run-
        specific value (e.g., a customer email) instead of referencing
        ``context.parameters['key']``.

        Two match modes minimize false positives:
        - **exact match** runs against every string literal — catches cases where
          the value is the entire literal (``selector="invoice_12345"``).
        - **substring match** runs only against *prose* literals (whitespace +
          ≥ ``PROSE_LITERAL_MIN_LEN`` chars) — catches values embedded inside
          longer click prompts (``"Should I download invoice 12345?"``) without
          falsely flagging short structural tokens like ``"next_block_label"``.

        Param values shorter than ``MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB`` or
        longer than ``MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB`` are skipped so the
        validator never flags values the generator wouldn't have parameterized.

        Returns an error message, or None if no hardcoded values are found.
        """
        if not run_parameter_values:
            return None

        exact_literals, prose_literals = self._collect_code_literals(code)

        hardcoded: list[tuple[str, str]] = []
        for param_key, param_value in run_parameter_values.items():
            if len(param_value) < MIN_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB:
                continue
            if len(param_value) > MAX_PARAM_VALUE_LENGTH_FOR_PROMPT_SUB:
                continue
            if param_value in exact_literals or any(param_value in literal for literal in prose_literals):
                hardcoded.append((param_key, param_value))

        if not hardcoded:
            return None

        examples = "; ".join(f"'{value[:50]}' should be context.parameters['{key}']" for key, value in hardcoded[:3])
        return (
            f"CRITICAL: Generated code contains hardcoded parameter values that are specific to this run. "
            f"These values will break when the workflow runs with different parameters. "
            f"Found {len(hardcoded)} hardcoded value(s): {examples}. "
            f"Replace ALL hardcoded parameter values with context.parameters['key'] references."
        )

    # Methods whose primary purpose is interaction — ai='proactive' on these
    # defeats caching by always invoking the LLM even when the selector works.
    _INTERACTION_METHODS: frozenset[str] = frozenset({"click", "fill", "fill_autocomplete", "type", "select_option"})

    # Regex to find page.<method>( calls
    _PAGE_CALL_RE: re.Pattern[str] = re.compile(r"""\bpage\.(\w+)\s*\(""")

    def _validate_proactive_misuse(self, code: str) -> str | None:
        """Flag ai='proactive' on interaction methods (click, fill, type, select_option).

        Using ai='proactive' means the LLM is always invoked even when the selector
        works, defeating the zero-LLM-cost goal of caching. These should almost always
        use ai='fallback' instead.

        Returns an error message or None if no issues found.
        """
        issues: list[str] = []
        lines = code.split("\n")
        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            if stripped.startswith("#"):
                i += 1
                continue
            match = self._PAGE_CALL_RE.search(lines[i])
            if match and match.group(1) in self._INTERACTION_METHODS:
                # Gather the full call (may span multiple lines)
                call_text = lines[i]
                end_line = self._find_call_end(lines, i)
                if end_line > i:
                    call_text = "\n".join(lines[i : end_line + 1])
                if re.search(r"""\bai\s*=\s*['"]proactive['"]""", call_text):
                    issues.append(f"page.{match.group(1)}() on line {i + 1}")
                i = end_line + 1
            else:
                i += 1

        if not issues:
            return None

        return (
            f"ai='proactive' used on interaction methods: {', '.join(issues[:5])}. "
            f"Using ai='proactive' on interaction methods ({'/'.join(sorted(self._INTERACTION_METHODS))}) means the LLM is "
            f"ALWAYS invoked even when the selector works, defeating the zero-LLM-cost "
            f"goal of caching. Change to ai='fallback' — this tries the selector first "
            f"and only invokes the LLM if the selector fails."
        )

    def _validate_missing_selectors(self, code: str) -> str | None:
        """Flag interaction methods that lack a selector= argument.

        Two cases are flagged:
        1. ai='fallback' but no selector — the CSS-try block is skipped entirely and
           AI fires as the primary path on every run, burning LLM tokens silently.
        2. No ai= argument at all and no selector — the call has no deterministic path
           and no explicit AI strategy, so it silently burns tokens with no fallback
           episode created.

        ai='proactive' without a selector is intentional (AI always generates the value)
        and is NOT flagged here.

        Returns an error message or None if no issues found.
        """
        issues: list[str] = []
        lines = code.split("\n")
        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            if stripped.startswith("#"):
                i += 1
                continue
            match = self._PAGE_CALL_RE.search(lines[i])
            if match and match.group(1) in self._INTERACTION_METHODS:
                end_line = self._find_call_end(lines, i)
                call_text = "\n".join(lines[i : end_line + 1]) if end_line > i else lines[i]
                has_selector = bool(re.search(r"""\bselector\s*=""", call_text))
                has_any_ai = bool(re.search(r"""\bai\s*=""", call_text))
                has_proactive = bool(re.search(r"""\bai\s*=\s*['"]proactive['"]""", call_text))
                # Flag if no selector AND (explicit fallback OR no ai argument at all).
                # ai='proactive' without selector is fine — intentional AI-driven fill.
                if not has_selector and has_any_ai and not has_proactive:
                    issues.append(f"page.{match.group(1)}() on line {i + 1}")
                elif not has_selector and not has_any_ai:
                    issues.append(f"page.{match.group(1)}() on line {i + 1} (no ai= argument)")
                i = end_line + 1
            else:
                i += 1

        if not issues:
            return None

        return (
            f"Missing selector on interaction methods: {', '.join(issues[:5])}. "
            f"Interaction methods without a selector= argument have no deterministic path — "
            f"they silently invoke the LLM on every run, burning tokens with no fallback "
            f"episode created. Add a selector= argument with a stable CSS selector "
            f"(aria-label, placeholder, name, role, :has-text()) and set ai='fallback' "
            f"so the element is found without an LLM call."
        )

    # Known auto-generated ID patterns from popular web frameworks.
    # These IDs change across deployments/sessions and break cached selectors.
    _FRAGILE_ID_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"#dnn_\w+"),  # DotNetNuke
        re.compile(r"#ember[\-_]?\d+"),  # Ember.js
        re.compile(r"#react-select-\d+"),  # React Select
        re.compile(r"\[data-reactid=['\"][\d.]+['\"]\]"),  # React (legacy)
        re.compile(r"#ext-gen-?\d+"),  # ExtJS
        re.compile(r"\.css-[a-z0-9]{4,}"),  # CSS-in-JS (Emotion, styled-components)
        re.compile(r"\.MuiButton-root|\.Mui\w+-\w+", re.IGNORECASE),  # Material UI
        re.compile(r"#__next\w+"),  # Next.js internal
        re.compile(r"\[data-v-[a-f0-9]+\]"),  # Vue scoped styles
    ]

    # Regex to find selector= string values in page.* calls.
    # Handles nested quotes: selector='a:has-text("X")' or selector="a:has-text('X')"
    # Uses [^'"] / [^"'] instead of [^'] / [^"] to prevent backtracking ambiguity (CodeQL py/redos).
    _SELECTOR_SINGLE_RE: re.Pattern[str] = re.compile(r"""\bselector\s*=\s*f?'([^'"]*(?:"[^"]*"[^'"]*)*)'""")
    _SELECTOR_DOUBLE_RE: re.Pattern[str] = re.compile(r'''\bselector\s*=\s*f?"([^"']*(?:'[^']*'[^"']*)*)"''')

    def _find_selector_values(self, text: str) -> list[str]:
        """Extract selector string values from text (single or multi-line), handling nested quotes."""
        results = []
        for m in self._SELECTOR_SINGLE_RE.finditer(text):
            results.append(m.group(1))
        for m in self._SELECTOR_DOUBLE_RE.finditer(text):
            results.append(m.group(1))
        return results

    def _validate_fragile_selectors(self, code: str) -> str | None:
        """Flag selectors using auto-generated IDs from web frameworks.

        Auto-generated IDs (e.g., #dnn_ctl00_xxx, #ember-123, .css-1a2b3c) change
        across deployments and are a leading cause of selector breakage and AI fallbacks.

        Uses multi-line call gathering so selectors split across lines are still caught.

        Returns an error message or None if no issues found.
        """
        issues: list[str] = []
        lines = code.split("\n")
        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            if stripped.startswith("#"):
                i += 1
                continue
            # Gather full call text for page.* calls that may span multiple lines
            match = self._PAGE_CALL_RE.search(lines[i])
            if match:
                end_line = self._find_call_end(lines, i)
                call_text = "\n".join(lines[i : end_line + 1]) if end_line > i else lines[i]
                line_num = i + 1  # report the starting line
            else:
                call_text = lines[i]
                line_num = i + 1
            for selector_val in self._find_selector_values(call_text):
                for pattern in self._FRAGILE_ID_PATTERNS:
                    if pattern.search(selector_val):
                        issues.append(
                            f"line {line_num}: selector='{selector_val[:60]}' matches fragile pattern {pattern.pattern}"
                        )
                        break  # one match per selector is enough
            i = end_line + 1 if match and end_line > i else i + 1

        if not issues:
            return None

        return (
            f"Fragile auto-generated selectors detected: {'; '.join(issues[:3])}. "
            f"These IDs are generated by web frameworks (DotNetNuke, Ember, React, MUI, etc.) "
            f"and change across deployments. Replace with stable selectors: "
            f"aria-label, placeholder, name, role, data-testid, or :has-text() with stable text. "
            f"If no stable selector exists, use ai='fallback' with a descriptive prompt and NO selector."
        )

    # Regex for dates in common formats (MM/DD/YYYY, M/D/YYYY, YYYY-MM-DD)
    # TODO: Could tighten month/day ranges to reduce false positives on URL-like strings,
    # but in practice LLM-generated selectors rarely contain such values.
    _DATE_RE: re.Pattern[str] = re.compile(r"\b(?:\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\b")

    # Regex for email addresses in text_patterns (PII that should not be in cached scripts)
    _EMAIL_RE: re.Pattern[str] = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    # Regex for :has-text("X") where X is very short (1-2 chars) — likely hardcoded run data
    _SHORT_HAS_TEXT_RE: re.Pattern[str] = re.compile(r""":has-text\(\s*['"](.{1,2})['"]\s*\)""")

    # TODO: prompt extraction regex won't handle triple-quoted or multi-line prompt values.
    # Acceptable for now since LLM-generated code rarely uses triple-quoted prompts.
    _PROMPT_RE: re.Pattern[str] = re.compile(r"""\bprompt\s*=\s*(?:f?['"])(.*?)(?:['"])""", re.DOTALL)

    def _validate_hardcoded_run_data(self, code: str) -> str | None:
        """Flag selectors and prompts containing hardcoded run-specific data.

        Catches:
        1. Date literals (MM/DD/YYYY, YYYY-MM-DD) in selector= or prompt= values
           that should use context.parameters instead
        2. Very short :has-text() values (1-2 chars) that are likely meaningless
           data from the original recording (e.g., a:has-text("6"))

        Uses multi-line call gathering so selectors/prompts split across lines are still caught.

        Returns an error message or None if no issues found.
        """
        issues: list[str] = []
        lines = code.split("\n")

        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            if stripped.startswith("#"):
                i += 1
                continue

            # Gather full call text for page.* calls that may span multiple lines
            match = self._PAGE_CALL_RE.search(lines[i])
            if match:
                end_line = self._find_call_end(lines, i)
                call_text = "\n".join(lines[i : end_line + 1]) if end_line > i else lines[i]
            else:
                call_text = lines[i]
            line_num = i + 1  # report the starting line

            # Check selectors for hardcoded dates
            for selector_val in self._find_selector_values(call_text):
                # Check for dates in selectors
                date_match = self._DATE_RE.search(selector_val)
                if date_match:
                    issues.append(
                        f"line {line_num}: selector contains hardcoded date '{date_match.group()}' — "
                        f"use context.parameters for date values"
                    )

                # Check for very short :has-text() values
                for ht_match in self._SHORT_HAS_TEXT_RE.finditer(selector_val):
                    short_text = ht_match.group(1)
                    # Allow common stable short texts
                    if short_text.lower() not in {"ok", "no", "x", "✓", "→", "←"}:
                        issues.append(
                            f'line {line_num}: selector has :has-text("{short_text}") — '
                            f"a 1-2 character :has-text() value is almost certainly "
                            f"hardcoded data from the recording run, not a stable selector"
                        )

            # Check prompts for hardcoded dates (only in prompt= kwargs, not general strings)
            prompt_match = self._PROMPT_RE.search(call_text)
            if prompt_match:
                prompt_val = prompt_match.group(1)
                date_match = self._DATE_RE.search(prompt_val)
                if date_match:
                    issues.append(
                        f"line {line_num}: prompt contains hardcoded date '{date_match.group()}' — "
                        f"use a parameter reference like context.parameters['download_start_date']"
                    )
            i = end_line + 1 if match and end_line > i else i + 1

        # Check text_patterns in page.classify() calls for PII (email addresses).
        # text_patterns are baked into the script and stored in S3/DB — PII should not leak.
        tp_start = code.find("text_patterns")
        while tp_start != -1:
            # Find the closing brace of the text_patterns dict
            brace_depth = 0
            tp_end = tp_start
            for j in range(tp_start, len(code)):
                if code[j] == "{":
                    brace_depth += 1
                elif code[j] == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        tp_end = j + 1
                        break
            # Guard: if no brace was found (e.g. text_patterns in a comment or
            # variable name), advance past this occurrence to avoid infinite loop
            if tp_end == tp_start:
                tp_end = tp_start + len("text_patterns")
            tp_block = code[tp_start:tp_end]
            tp_line = code[:tp_start].count("\n") + 1
            # Check for email addresses
            for email_match in self._EMAIL_RE.finditer(tp_block):
                issues.append(
                    f"line {tp_line}: text_patterns contains email address '{email_match.group()}' — "
                    f"remove PII from text_patterns, use generic page indicators instead"
                )
                break  # one email per text_patterns block is enough
            tp_start = code.find("text_patterns", tp_end)

        if not issues:
            return None

        return (
            f"Hardcoded run-specific data detected: {'; '.join(issues[:3])}. "
            f"Dates, invoice numbers, email addresses, and other per-run or per-user values must NOT "
            f"be hardcoded in selectors, prompts, or text_patterns. Use context.parameters['key'] for "
            f"dynamic values, or use generic page structure indicators (form labels, button text, "
            f"navigation links) in text_patterns."
        )

    # Regexes for extracting branch return values from generated conditional code.
    _BRANCH_LABEL_STR_RE: re.Pattern[str] = re.compile(r"""["']next_block_label["']\s*:\s*["']([^"']+)["']""")
    _BRANCH_LABEL_NONE_RE: re.Pattern[str] = re.compile(r"""["']next_block_label["']\s*:\s*None""")
    _BRANCH_INDEX_RE: re.Pattern[str] = re.compile(r"""["']branch_index["']\s*:\s*(-?\d+)""")

    @staticmethod
    def _validate_branch_returns(code: str, branches: list[dict]) -> str | None:
        """Validate that returned next_block_label and branch_index values match branch definitions.

        Checks every literal next_block_label and branch_index in return statements
        against the set of values from the branch definitions. Catches cases where the
        LLM invents labels (e.g. None when no branch has a null target) or uses invalid
        indices (e.g. -1).

        Returns an error message if invalid values are found, None if all values are valid.
        """
        if not branches:
            return None

        # Build valid sets from branch definitions
        valid_labels: set[str | None] = set()
        valid_indices: set[int] = set()
        for i, branch in enumerate(branches):
            valid_labels.add(branch.get("next_block_label"))
            valid_indices.add(i)

        # Extract literal return values from code (skip comments)
        found_labels: list[str | None] = []
        found_indices: list[int] = []
        for line in code.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for m in ScriptReviewer._BRANCH_LABEL_STR_RE.finditer(line):
                found_labels.append(m.group(1))
            if ScriptReviewer._BRANCH_LABEL_NONE_RE.search(line):
                found_labels.append(None)
            for m in ScriptReviewer._BRANCH_INDEX_RE.finditer(line):
                found_indices.append(int(m.group(1)))

        # If no literals found (e.g. uses variables), we can't validate statically
        if not found_labels and not found_indices:
            return None

        invalid_labels = [label for label in found_labels if label not in valid_labels]
        invalid_indices = [idx for idx in found_indices if idx not in valid_indices]

        errors: list[str] = []
        if invalid_labels:
            label_strs = [repr(label) for label in invalid_labels]
            errors.append(
                f"next_block_label values {label_strs} do not match any branch. "
                f"Valid labels: {sorted(str(lbl) for lbl in valid_labels if lbl is not None)}"
            )
        if invalid_indices:
            errors.append(
                f"branch_index values {invalid_indices} are not valid. Valid indices: {sorted(valid_indices)}"
            )

        if not errors:
            return None

        return (
            "Generated code returns branch values that don't match the branch definitions. "
            + " ".join(errors)
            + " Each return statement must use a next_block_label and branch_index from the branch definitions."
        )

    def _auto_fix_missing_else(self, code: str, navigation_goal: str) -> str | None:
        """Auto-inject missing `else` branches after page.classify() if/elif chains.

        When the LLM generates a classify with if/elif but no else, this inserts
        an else branch with element_fallback. Returns the fixed code, or None
        if the code structure is too complex to safely auto-fix.
        """
        lines = code.split("\n")
        classify_line_indices = []
        for i, line in enumerate(lines):
            if "page.classify(" in line and not line.lstrip().startswith("#"):
                classify_line_indices.append(i)

        if not classify_line_indices:
            return None

        # Process in reverse order so line insertions don't shift earlier indices
        for classify_line_idx in reversed(classify_line_indices):
            if_indent: int | None = None
            last_branch_body_end: int | None = None
            pre_if_lines = 0

            # Skip past multi-line classify() call
            scan_start = self._find_call_end(lines, classify_line_idx) + 1

            for j in range(scan_start, len(lines)):
                stripped = lines[j].strip()
                if not stripped or stripped.startswith("#"):
                    continue

                indent = len(lines[j]) - len(lines[j].lstrip())

                if if_indent is None:
                    if stripped.startswith("if ") or stripped.startswith("if("):
                        if_indent = indent
                        last_branch_body_end = j
                    else:
                        # Not an if yet — could be variable assignments between
                        # classify() and the if chain. Keep scanning.
                        pre_if_lines += 1
                        if pre_if_lines > self._MAX_PRE_IF_SCAN:
                            break
                        continue
                else:
                    if indent > if_indent:
                        last_branch_body_end = j
                    elif indent == if_indent:
                        if stripped.startswith("elif "):
                            last_branch_body_end = j
                        elif stripped.startswith("else:"):
                            break  # Already has else — no fix needed
                        else:
                            # End of if/elif chain without else — insert here
                            if last_branch_body_end is not None:
                                indent_str = " " * if_indent
                                body_indent = " " * (if_indent + 4)
                                goal = (
                                    navigation_goal.replace("\\", "\\\\")
                                    .replace('"', '\\"')
                                    .replace("\n", " ")
                                    .replace("\r", " ")[:200]
                                )
                                else_block = [
                                    f"{indent_str}else:",
                                    f'{body_indent}await page.element_fallback(navigation_goal="{goal}")',
                                ]
                                lines = (
                                    lines[: last_branch_body_end + 1] + else_block + lines[last_branch_body_end + 1 :]
                                )
                            break
                    else:
                        # Dedented past the if chain — insert else before this line
                        if if_indent is not None and last_branch_body_end is not None:
                            indent_str = " " * if_indent
                            body_indent = " " * (if_indent + 4)
                            goal = (
                                navigation_goal.replace("\\", "\\\\")
                                .replace('"', '\\"')
                                .replace("\n", " ")
                                .replace("\r", " ")[:200]
                            )
                            else_block = [
                                f"{indent_str}else:",
                                f'{body_indent}await page.element_fallback(navigation_goal="{goal}")',
                            ]
                            lines = lines[: last_branch_body_end + 1] + else_block + lines[last_branch_body_end + 1 :]
                        break
            else:
                # For loop exhausted — if/elif chain runs to end of function
                # without an else. Insert else at the end.
                if if_indent is not None and last_branch_body_end is not None:
                    indent_str = " " * if_indent
                    body_indent = " " * (if_indent + 4)
                    goal = (
                        navigation_goal.replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("\n", " ")
                        .replace("\r", " ")[:200]
                    )
                    else_block = [
                        f"{indent_str}else:",
                        f'{body_indent}await page.element_fallback(navigation_goal="{goal}")',
                    ]
                    lines = lines[: last_branch_body_end + 1] + else_block + lines[last_branch_body_end + 1 :]

        return "\n".join(lines)

    def _build_retry_prompt(self, failed_code: str, error: str, function_signature: str = "") -> str:
        """Build a retry prompt that includes the failed code and error."""
        sig = function_signature or self._extract_function_signature(failed_code)
        return (
            "The Python code you generated has an error. "
            "Fix the error and return the corrected complete function.\n\n"
            f"## Error\n{error}\n\n"
            f"## Your Previous Output (has error)\n```python\n{failed_code}\n```\n\n"
            f'Return a JSON object with a single key `"code"` containing the complete updated function as a string.\n'
            f"The function must start with `{sig}` and contain the updated implementation.\n"
            f"Use `\\n` for newlines inside the string value. Example:\n\n"
            f'```json\n{{"code": "{sig}\\n    # fixed code here\\n    await page.complete()"}}\n```'
        )
