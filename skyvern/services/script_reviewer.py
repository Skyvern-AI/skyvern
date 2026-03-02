from __future__ import annotations

import json
import re
from typing import Literal

import structlog

from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.workflow.models.block import get_all_blocks
from skyvern.schemas.scripts import ScriptBranchHit, ScriptFallbackEpisode

LOG = structlog.get_logger()

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
        "scan_form_fields",
        # Form filling
        "fill_form",
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
    ) -> dict[str, str] | None:
        """Review fallback episodes and generate updated code for affected blocks.

        Returns {block_label: updated_code} or None if review fails.
        """
        if not episodes:
            return None

        # Load the workflow definition to get navigation goals and parameter keys
        navigation_goals, all_parameter_keys = await self._load_workflow_context(
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

        # Triage failed episodes — skip non-code-fixable failures
        triaged_episodes = []
        for episode in episodes:
            if await self._triage_episode(episode, organization_id):
                triaged_episodes.append(episode)
            else:
                # Mark as reviewed so we don't re-triage on every run
                await app.DATABASE.mark_episode_reviewed(
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

        updated_blocks: dict[str, str] = {}

        for block_label, block_episodes in episodes_by_block.items():
            try:
                updated_code = await self._review_block(
                    organization_id=organization_id,
                    workflow_permanent_id=workflow_permanent_id,
                    script_revision_id=script_revision_id,
                    block_label=block_label,
                    episodes=block_episodes,
                    navigation_goal=navigation_goals.get(block_label),
                    stale_branches=stale_by_block.get(block_label),
                    all_parameter_keys=all_parameter_keys,
                    historical_episodes=history_by_block.get(block_label),
                )
                if updated_code:
                    updated_blocks[block_label] = updated_code
            except Exception:
                LOG.exception(
                    "ScriptReviewer: failed to review block",
                    block_label=block_label,
                    organization_id=organization_id,
                )

        if not updated_blocks:
            return None

        return updated_blocks

    async def review_conditional_blocks(
        self,
        organization_id: str,
        workflow_permanent_id: str,
        conditional_episodes: list[ScriptFallbackEpisode],
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
    ) -> str | None:
        """Generate Python code for a conditional block based on its expression patterns.

        Examines the branch expressions from the episode and asks the LLM to convert
        them into a Python function that evaluates the condition without an LLM call.
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

        reviewer_prompt = prompt_engine.load_prompt(
            "script-reviewer-conditional",
            block_label=block_label,
            branches=branch_info,
        )

        LOG.info(
            "ScriptReviewer: generating conditional code",
            block_label=block_label,
            num_branches=len(branch_info),
        )

        try:
            llm_response = await app.SCRIPT_REVIEWER_LLM_API_HANDLER(
                prompt=reviewer_prompt,
                prompt_name="script-reviewer-conditional",
                step=None,
                organization_id=organization_id,
            )

            code = self._extract_code_from_response(llm_response)
            if not code:
                LOG.warning(
                    "ScriptReviewer: no code extracted for conditional",
                    block_label=block_label,
                )
                return None

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
                    error=compile_error,
                )
                return None

            # Validate page API references (catch hallucinated methods)
            api_error = self._validate_page_api(code)
            if api_error:
                LOG.warning(
                    "ScriptReviewer: conditional code has invalid page API",
                    block_label=block_label,
                    error=api_error,
                )
                return None

            # Validate the function returns the expected structure
            if "next_block_label" not in code:
                LOG.warning(
                    "ScriptReviewer: conditional code missing next_block_label return",
                    block_label=block_label,
                )
                return None

            LOG.info(
                "ScriptReviewer: generated conditional code",
                block_label=block_label,
                code_length=len(code),
            )
            return code

        except Exception:
            LOG.exception(
                "ScriptReviewer: LLM call failed for conditional",
                block_label=block_label,
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
    ) -> str | None:
        """Review a single block's fallback episodes and generate updated code."""
        LOG.info(
            "ScriptReviewer: starting block review",
            block_label=block_label,
            script_revision_id=script_revision_id,
            navigation_goal=navigation_goal[:100] if navigation_goal else None,
        )

        # Load the current cached code for the block
        existing_code = await self._load_block_code(
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
        strategy = self._classify_block_strategy(
            block_label=block_label,
            navigation_goal=navigation_goal,
            existing_code=existing_code,
            episodes=episodes,
        )

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

        # Build historical episode summaries for cross-run context
        history_summaries = []
        for ep in historical_episodes or []:
            history_summaries.append(
                {
                    "error_message": ep.error_message,
                    "reviewer_output": (ep.reviewer_output or "")[:500],
                    "fallback_succeeded": ep.fallback_succeeded,
                }
            )

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
                )

                LOG.info(
                    "ScriptReviewer: LLM response received",
                    block_label=block_label,
                    attempt=attempt,
                    response_type=type(llm_response).__name__,
                    response_snippet=str(llm_response)[:200],
                )

                updated_code = self._extract_code_from_response(llm_response)
                if not updated_code:
                    LOG.warning(
                        "ScriptReviewer: no code extracted from response",
                        block_label=block_label,
                        attempt=attempt,
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

                # Validate structural regression (catch deleted branches, shrunk code)
                regression_error = self._validate_structural_regression(updated_code, existing_code)
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

                LOG.info(
                    "ScriptReviewer: generated updated code for block",
                    block_label=block_label,
                    attempt=attempt,
                    code_length=len(updated_code),
                )
                return updated_code

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
        block_label: str,
        navigation_goal: str | None,
        existing_code: str | None,
        episodes: list[ScriptFallbackEpisode],
    ) -> Literal["form_filling", "sequential", "extraction"]:
        """Classify a block's caching strategy based on its content.

        Rules (heuristic, no LLM call):
        1. If existing_code contains "page.extract(" without "page.click(" → "extraction"
        2. If existing_code already uses "page.fill_form(" → "form_filling"
        3. If navigation_goal contains form-filling keywords AND
           episodes have form_fields with 4+ fields → "form_filling"
        4. Otherwise → "sequential"
        """
        code = existing_code or ""

        # Rule 1: Extraction blocks
        if "page.extract(" in code and "page.click(" not in code:
            return "extraction"

        # Rule 2: Already uses fill_form (previous form-filling classification)
        if "page.fill_form(" in code:
            return "form_filling"

        # Rule 3: Heuristic for form-filling based on navigation goal + episode data
        goal = (navigation_goal or "").lower()
        form_keywords = {"fill", "application", "form", "submit", "apply", "sign up", "register", "signup"}
        has_form_goal = any(kw in goal for kw in form_keywords)

        # Check known form hosts in episode URLs
        form_hosts = {"lever.co", "greenhouse.io", "workday.com", "myworkdayjobs.com", "icims.com", "ashbyhq.com"}
        has_form_url = False
        for ep in episodes:
            if ep.page_url:
                if any(host in ep.page_url.lower() for host in form_hosts):
                    has_form_url = True
                    break

        # Count form fields and form actions per episode (use max across episodes)
        max_form_fields = 0
        max_form_actions = 0
        for ep in episodes:
            if isinstance(ep.agent_actions, dict):
                form_fields = ep.agent_actions.get("form_fields", [])
                if isinstance(form_fields, list):
                    max_form_fields = max(max_form_fields, len(form_fields))

                ep_form_actions = 0
                actions = ep.agent_actions.get("actions", [])
                if isinstance(actions, list):
                    for a in actions:
                        if isinstance(a, dict) and a.get("action_type") in ("input_text", "select_option"):
                            ep_form_actions += 1
                max_form_actions = max(max_form_actions, ep_form_actions)

        # Classify as form_filling if we see strong signals
        if (has_form_goal or has_form_url) and (max_form_fields >= 4 or max_form_actions >= 4):
            return "form_filling"

        return "sequential"

    async def _load_block_code(
        self,
        organization_id: str,
        script_revision_id: str | None,
        block_label: str,
    ) -> str | None:
        """Load the current cached code for a block from the database."""
        if not script_revision_id:
            return None

        try:
            script_blocks = await app.DATABASE.get_script_blocks_by_script_revision_id(
                script_revision_id=script_revision_id,
                organization_id=organization_id,
            )
            for sb in script_blocks:
                if sb.script_block_label == block_label and sb.script_file_id:
                    # Load the code from the script file
                    script_file = await app.DATABASE.get_script_file_by_id(
                        script_revision_id=script_revision_id,
                        file_id=sb.script_file_id,
                        organization_id=organization_id,
                    )
                    if script_file and script_file.artifact_id:
                        artifact = await app.DATABASE.get_artifact_by_id(
                            artifact_id=script_file.artifact_id,
                            organization_id=organization_id,
                        )
                        if artifact:
                            file_content = await app.ARTIFACT_MANAGER.retrieve_artifact(artifact)
                            if isinstance(file_content, bytes):
                                return file_content.decode("utf-8")
                            elif isinstance(file_content, str):
                                return file_content
        except Exception:
            LOG.exception(
                "ScriptReviewer: failed to load block code",
                block_label=block_label,
                script_revision_id=script_revision_id,
            )
        return None

    async def _load_workflow_context(
        self,
        organization_id: str,
        workflow_permanent_id: str,
    ) -> tuple[dict[str, str], list[str]]:
        """Load navigation goals and parameter keys for a workflow.

        Returns (goals_by_label, parameter_keys).
        """
        goals: dict[str, str] = {}
        parameter_keys: list[str] = []
        try:
            workflow = await app.DATABASE.get_workflow_by_permanent_id(
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
        return goals, parameter_keys

    def _extract_function_signature(self, code: str) -> str:
        """Extract the async function signature from existing code."""
        for line in code.split("\n"):
            stripped = line.strip()
            if stripped.startswith("async def "):
                return stripped
        return "async def block_fn(page, context):"

    def _extract_code_from_response(self, response: dict | str | list | None) -> str | None:
        """Extract Python code from the LLM response.

        Strict parsing: expects either a JSON dict with a "code" key, or a markdown
        code block. Does NOT attempt to salvage malformed responses (lists, raw
        str() coercion) — those should fail and trigger a retry.
        """
        if not response:
            return None

        # Step 1: Get the text content from the response
        text = ""
        if isinstance(response, dict):
            # Direct "code" key (expected JSON format)
            code_value = response.get("code")
            if isinstance(code_value, str) and code_value.strip():
                return code_value.strip()
            # llm_response or updated_code keys
            text = response.get("updated_code", "") or response.get("llm_response", "")
        elif isinstance(response, str):
            text = response
        elif isinstance(response, list):
            # LLM returned a list — this is malformed. Do NOT salvage with str().
            LOG.warning(
                "ScriptReviewer: LLM returned a list instead of dict/str, rejecting",
                response_type="list",
                response_length=len(response),
            )
            return None
        else:
            LOG.warning(
                "ScriptReviewer: unexpected response type, rejecting",
                response_type=type(response).__name__,
            )
            return None

        if not text:
            return None

        # Step 2: Try to parse text as JSON with a "code" key
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                code_value = parsed.get("code")
                if isinstance(code_value, str) and code_value.strip():
                    return code_value.strip()
        except (json.JSONDecodeError, ValueError):
            pass

        # Step 3: Extract from markdown code blocks
        if "```python" in text:
            start = text.index("```python") + len("```python")
            remaining = text[start:]
            end = remaining.find("```")
            if end != -1:
                code = remaining[:end].strip()
            else:
                code = remaining.strip()
            if "async def " in code:
                return code
            return None

        if "```" in text:
            start = text.index("```") + 3
            remaining = text[start:]
            end = remaining.find("```")
            if end != -1:
                code = remaining[:end].strip()
            else:
                code = remaining.strip()
            if "async def " in code:
                return code
            return None

        # Step 4: If text itself looks like a complete Python function, accept it
        if text.strip().startswith("async def "):
            return text.strip()

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
