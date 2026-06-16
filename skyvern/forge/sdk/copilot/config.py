"""Injectable workflow copilot configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from skyvern.config import settings


class BlockAuthoringPolicy(StrEnum):
    STANDARD = "standard"
    CODE_ONLY_BROWSER = "code_only_browser"


def normalize_block_authoring_policy(value: object) -> BlockAuthoringPolicy:
    if isinstance(value, BlockAuthoringPolicy):
        return value
    if isinstance(value, str):
        try:
            return BlockAuthoringPolicy(value)
        except ValueError:
            return BlockAuthoringPolicy.STANDARD
    return BlockAuthoringPolicy.STANDARD


def block_authoring_policy_from_code_only_mode(enabled: bool) -> BlockAuthoringPolicy:
    return BlockAuthoringPolicy.CODE_ONLY_BROWSER if enabled else BlockAuthoringPolicy.STANDARD


def download_scout_act_required_for_policy(block_authoring_policy: BlockAuthoringPolicy | str | None) -> bool:
    return normalize_block_authoring_policy(block_authoring_policy) == BlockAuthoringPolicy.CODE_ONLY_BROWSER


DEFAULT_PROMPT_TEMPLATE = "workflow-copilot-agent.j2"
DEFAULT_MAX_TURNS = 35
DEFAULT_TOKEN_BUDGET = 90_000

SCREENSHOT_DROPPED_NUDGE = (
    "Your previous screenshot was dropped from context to recover from a token-budget overflow. "
    "Do NOT reason about the page from memory. Re-take the screenshot "
    "(get_browser_screenshot) or call evaluate before deciding your next step."
)

POST_UPDATE_NUDGE = (
    "You updated the workflow but did not test it. "
    "You MUST call run_blocks_and_collect_debug (or update_and_run_blocks next time) "
    "to test at least the first block before responding to the user. "
    "This verifies the workflow actually works. "
    "Exception: if the latest user message explicitly asked for an untested draft, "
    "respond with an unvalidated draft instead of testing."
)

POST_NAVIGATE_NUDGE = (
    "You navigated to a page but did not observe its content. "
    "You MUST use evaluate, get_browser_screenshot, click, type_text, "
    "scroll, select_option, press_key, or console_messages "
    "to inspect the page before responding. Do NOT answer from memory."
)

POST_INTERMEDIATE_SUCCESS_NUDGE = (
    "STOP — do NOT respond to the user yet. "
    "Your workflow only covers a subset of what the user asked for. "
    "You MUST add the next block now: call update_and_run_blocks with the complete "
    "workflow YAML, but pass only the next 1-2 unverified block labels when the "
    "workflow has several page-changing stages. Keep later blocks in the YAML; "
    "shrink only the block_labels test frontier. "
    "Only respond to the user when every distinct action they requested is covered "
    "by a workflow block, or you have clear evidence that continuing is infeasible."
)

POST_FAILED_TEST_NUDGE = (
    "STOP — your last test run FAILED. Do NOT respond to the user yet.\n"
    "1. First, call get_run_results — pass the workflow_run_id from the prior "
    "update_and_run_blocks or run_blocks_and_collect_debug response to make the "
    "lookup unambiguous. That returns per-block failure_reason, output, and any "
    "failed-block screenshots, which is the diagnostic data you need.\n"
    "2. Then decide: if the failure looks fixable (wrong goal wording, popup "
    "blocking, timeout, element not found), adjust the workflow with a DIFFERENT "
    "approach and call update_and_run_blocks again — the tool will rerun from "
    "the earliest invalidated block so only the changed part is retested.\n"
    "3. If you have now failed multiple times with genuinely different approaches "
    "and the evidence strongly suggests the site cannot satisfy the request, "
    "respond explaining exactly what you tried and what blocked you.\n"
    "Do NOT resubmit the same workflow — you must change something substantive."
)

POST_FAILED_TEST_INSPECT_FIRST_NUDGE = (
    "STOP — your last test run FAILED and the browser is still on the page it reached. "
    "Do NOT respond to the user, and do NOT re-run a changed block goal blind.\n"
    "1. Call get_run_results (pass the workflow_run_id) for the per-block failure_reason.\n"
    '2. Then OBSERVE where it failed: call inspect_page_for_composition(target_url="current_page") '
    "(or evaluate / get_browser_screenshot) on the reached page BEFORE changing anything. A failed "
    "navigation/action often left the page in a state the block's goal did not expect — a popup or "
    "overlay, a different layout, or an effect that already applied.\n"
    "3. Use that observed evidence to decide HOW to change the workflow: keep the same block with a "
    "corrected goal, swap to a different block type, or redesign the step. Do NOT guess a new goal "
    "for the same block and re-run without first observing the page.\n"
    "Do NOT resubmit the same workflow unchanged."
)

POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE = (
    "STOP — you explored the page using direct browser tools but did NOT engage "
    "the workflow path. You MUST follow the WORKFLOW-FIRST EXECUTION PATH:\n"
    "1. If no workflow exists yet, call update_workflow with at least a navigation "
    "block for the target URL.\n"
    "2. If a workflow already exists, call run_blocks_and_collect_debug to test it.\n"
    "3. Use the test results to decide next steps.\n"
    "Do NOT make feasibility judgments from browser exploration alone — "
    "build and test workflow blocks first."
)

POST_SUSPICIOUS_SUCCESS_NUDGE = (
    "STOP — your last test run completed (status=completed) but data-producing "
    "blocks (extraction/text_prompt) produced no meaningful output "
    "(missing, empty, or all-null fields). This is NOT a success.\n"
    "1. Call get_run_results to inspect what each block actually returned.\n"
    "2. If the extraction/text_prompt block returned empty, all-null, or "
    "irrelevant data, the upstream block likely fetched an error page "
    "(e.g. 403, CAPTCHA, 'no results'), landed on the wrong page, or the "
    "data is rendered differently than expected.\n"
    "3. Use get_browser_screenshot or evaluate to inspect what the workflow "
    "browser actually sees — do NOT just retry extraction with a different prompt.\n"
    "4. Fix the root cause — do NOT declare the workflow working based on "
    "status alone. Verify the actual extracted data answers the user's question."
)

POST_REPEATED_NULL_DATA_NUDGE = (
    "STOP — you have now produced multiple consecutive test runs where "
    "extraction/text_prompt blocks returned all-null or empty data. "
    "Re-prompting the extractor is not working — the problem is almost "
    "certainly NOT how the extraction goal is worded.\n"
    "You MUST now do ONE of the following before another update_workflow call:\n"
    "1. Call get_browser_screenshot on the workflow's browser session to see "
    "exactly what page the workflow is actually loading (it may differ from "
    "what you expect — e.g. a 'no results' fallback, cookie wall, or bot block).\n"
    "2. Call evaluate with JavaScript that searches for the expected content "
    "on the workflow's browser — confirm whether the data is even present.\n"
    "3. If the page the workflow loads genuinely does not contain the data, "
    "pivot to a different URL or source entirely — do NOT keep retrying "
    "extraction against the same failing page.\n"
    "Do NOT call update_and_run_blocks again until you have concrete evidence "
    "about what the workflow browser is actually seeing."
)

POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE = (
    "STOP — this is the second run with the same frontier and the same failure "
    "signature. Re-running the same change again is unlikely to help.\n"
    "Before another update_and_run_blocks call, you MUST:\n"
    "1. Call get_run_results to inspect the full failure evidence (per-block "
    "failure_reason, action_trace, and any failed-block screenshots).\n"
    "2. If the evidence is still ambiguous, use get_browser_screenshot or evaluate "
    "to check what the workflow browser is actually seeing.\n"
    "3. Then make a materially different change — different block ordering, a "
    "different selector strategy, a different entry URL, or different parameters. "
    "Changes to wording of the same prompt do not count as materially different."
)

POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE = (
    "STOP — you have now attempted the same frontier with the same failure "
    "signature THREE times without making progress. Do NOT call "
    "update_and_run_blocks or run_blocks_and_collect_debug again on this "
    "frontier.\n"
    "Choose ONE:\n"
    "A) Finalize now with a clear blocker explanation that references the "
    "specific failure_reason and failure_categories you observed.\n"
    "B) If required user input is missing (credential, ambiguous goal, "
    "site-specific detail), respond with an ASK_QUESTION instead. Do not "
    "retry the same repair again."
)

POST_PARAMETER_BINDING_WARN_NUDGE = (
    "STOP — your last test run failed with a PARAMETER_BINDING_ERROR. "
    "This is an INTERNAL workflow configuration mismatch, not a site or "
    "selector problem.\n"
    "The workflow definition references a parameter (by Jinja key) that is "
    "not in the top-level workflow parameters list, or the list declares a "
    "parameter the blocks do not use.\n"
    "Do NOT retry with different selectors, URLs, or navigation changes — "
    "those will not help. Instead:\n"
    "1. Reconcile the workflow's top-level parameters with what the blocks "
    "actually reference via {{ parameters.<key> }}.\n"
    "2. Inline one-off literals rather than adding a parameter for each.\n"
    "3. Then call update_and_run_blocks again with a corrected YAML and, "
    "for any remaining parameters, concrete values passed via the "
    "`parameters` argument."
)

POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE = (
    "STOP — the target URL is unreachable and further retries cannot succeed. "
    "The navigation failed with a permanent error (DNS resolution, SSL/cert, "
    "or invalid URL). Do NOT retry and do NOT edit the workflow. Reply to the "
    "user now: state that the URL could not be reached, quote the exact error "
    "message from the last failure_reason, and ask them to verify the URL."
)

POST_PARAMETER_BINDING_STOP_NUDGE = (
    "STOP — you have retried the same PARAMETER_BINDING_ERROR multiple times "
    "without reconciling the workflow configuration. Do NOT call "
    "update_and_run_blocks or run_blocks_and_collect_debug again until the "
    "workflow parameters list matches the block references.\n"
    "Choose ONE:\n"
    "A) Finalize now with a blocker explanation that names the specific "
    "parameter keys that are out of sync.\n"
    "B) If you need missing values from the user (credential, identifier) "
    "to decide what belongs in the parameters list, respond with an "
    "ASK_QUESTION instead. Do not resubmit a workflow that still has the "
    "same parameter-binding drift."
)

POST_PER_TOOL_BUDGET_NUDGE = (
    "STOP — your last update_and_run_blocks call exceeded the per-tool-call "
    "time budget while still making progress. This is NOT a site failure or "
    "a wording problem — the chain you submitted is too long to complete in a "
    "single tool call.\n"
    "Do NOT retry the same chain. Do NOT change navigation_goal wording or "
    "selectors hoping it will run faster.\n"
    "1. Call get_run_results for the budgeted run. If it shows a navigation "
    "block was canceled or failed, do NOT run that same label again unchanged.\n"
    "2. If get_run_results includes a current_url, inspect the current page "
    'before another workflow mutation with inspect_page_for_composition(target_url="current_page"). '
    "Generic screenshot/evaluate reads can help answer the user, but they do not "
    "satisfy the bounded page-evidence contract for authoring or mutating blocks. "
    "If the requested answer or a no-results state is visible in bounded evidence, "
    "answer from that evidence instead of rerunning the search.\n"
    "3. If bounded evidence shows challenge_state.gates_submit_controls=true "
    "and the requested answer or no-results state is not visible, do NOT retry "
    "the same solve/wait/submit block. Treat the still-disabled submit/search "
    "control as observed anti-bot blocker evidence and report that blocker, "
    "unless you can make a materially different allowed attempt such as a "
    "different proxy/location or entrypoint.\n"
    "4. If inspection shows a real missing state change, split or replace the "
    "oversized block and shrink the requested block_labels list to the first 1-2 unverified "
    "blocks. The verified-prefix optimization will replay any earlier blocks "
    "from cached state without re-running the browser, so passing a smaller "
    "frontier is cheap.\n"
    "5. For page-state work, inspect the live page evidence already gathered "
    "before deciding what is missing. Do not add durable workflow blocks solely "
    "to rediscover page shape; use navigation only when missing state must be "
    "changed through the UI, and keep that block atomic.\n"
    "6. Test the smaller frontier. If it succeeds, extend by one block at a "
    "time on subsequent calls.\n"
    "7. If your workflow only has 1-2 blocks and one block is still hitting "
    "the budget, the single block is too ambitious — either narrow its scope, "
    "replace it with DOM/state verification plus smaller actions, or reply "
    "with a blocker explanation."
)

POST_PER_TOOL_BUDGET_STOP_NUDGE = (
    "STOP — you have now hit the per-tool-call time budget MORE THAN ONCE on this goal, "
    "including after you already shrank the frontier. The page is too heavy to finish a "
    "page-changing block within one tool call, and each retry has LESS budget than the last — "
    "running again will fail faster, not succeed.\n"
    "Do NOT call update_and_run_blocks or run_blocks_and_collect_debug again, and do NOT "
    "redesign the workflow hoping a different shape runs faster. Reply to the user now: keep "
    "the verified prefix, name the block that could not finish within the time budget, and "
    "state exactly what was and was not verified end-to-end. Do not repeat this message as the "
    "user-facing answer."
)

POST_NO_WORKFLOW_DELIVERY_NUDGE = (
    "STOP — you are telling the user you created or are showing a workflow, "
    "but no workflow update tool has succeeded in this turn. The user will see "
    "an empty proposal. You MUST either call update_and_run_blocks with a real "
    "workflow and test it, or respond with ASK_QUESTION if required input is "
    'missing. Do NOT say "Here\'s the workflow" until there is an actual '
    "workflow proposal behind the response."
)

POST_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGE = (
    "STOP — discover_workflow_entrypoint already resolved a candidate_url for this build turn, "
    "but you have not inspected the resolved page or composed from it yet. Use the resolved "
    "candidate_url as the goto_url entrypoint, inspect the page if needed, then call "
    "update_and_run_blocks. Only ask a clarifying question after using the resolved page evidence "
    "and only when a separate required non-URL input is still missing."
)

PRE_DISCOVERY_URL_QUESTION_NUDGE = (
    "STOP — you are asking the user for an entry-point URL before resolving it yourself. "
    "discover_workflow_entrypoint has not run this turn. Call "
    "discover_workflow_entrypoint(site_or_url, intent_hint) to resolve the entrypoint from the "
    "site the user named, then compose from the resolved page. Only ask for a URL if discovery "
    "runs and cannot resolve a site."
)

PROBABLE_SITE_BLOCK_STOP_NUDGE_PREFIX = (
    "STOP — the target site has failed to scrape on every attempt across "
    "multiple workflow shapes. Every run navigated successfully but the "
    'scraper could not read the page ("failed to load the website" / '
    '"page may have navigated unexpectedly"). This pattern indicates the '
    "site is either blocking automated access, genuinely unresponsive in "
    "this environment, or rendering content Skyvern cannot read reliably.\n"
    "Do NOT retry with another workflow variation. Do NOT call "
    "update_and_run_blocks or run_blocks_and_collect_debug again.\n"
    "Reply to the user now: state that the site could not be loaded after "
    "multiple attempts, quote the last failure_reason verbatim, keep the "
    "message concise, and ask "
)

POST_PROBABLE_SITE_BLOCK_STOP_NUDGE = (
    PROBABLE_SITE_BLOCK_STOP_NUDGE_PREFIX
    + "whether to try a different URL, configure a proxy, or provide an alternate entry point."
)

POST_ANTI_BOT_FAILED_TEST_NUDGE = (
    "STOP — your last test run failed due to an anti-bot/WAF block "
    "(Access Denied, CAPTCHA, human-verification, or similar challenge evidence).\n"
    "IMPORTANT: An HTTP_REQUEST or navigation block from the SAME server IP "
    "will almost certainly receive the same block. Do NOT retry with:\n"
    "- A simple wait/delay block (timing does not fix IP bans)\n"
    "- A raw HTTP_REQUEST to the same URL (same IP = same block)\n"
    "Instead, try:\n"
    "1. Set proxy_location on the workflow to route through a different IP "
    "(use `RESIDENTIAL` by default, or a US-state value like `US-CA`/`US-NY`; "
    "do NOT use bare country codes like `US` — they are not valid "
    "ProxyLocation members).\n"
    "2. If you add anti-bot handling blocks, make them conditional on visible "
    "challenge evidence (for example Access Denied, CAPTCHA, human-verification, or "
    "verify-you-are-human text). Do NOT assume every run starts on a challenge "
    "page; normal, unblocked runs should proceed directly to the requested task.\n"
    "3. If bounded page evidence says challenge_state.gates_submit_controls=true "
    "or a submit/search control is still disabled after challenge resolution was "
    "attempted. Do NOT retry the same challenge solve, blind wait, or disabled "
    "submit click. Treat it as the current blocker unless you can make a "
    "materially different allowed attempt such as a different proxy/location or "
    "entrypoint.\n"
    "4. If still blocked, explain the specific anti-bot evidence observed "
    "(for example CAPTCHA, verify-you-are-human text, Access "
    "Denied, or a blank Just-a-moment page); describe exactly what you tried; "
    "and ask whether to try a different proxy/location, entry URL, or "
    "alternate source.\n"
    "Do NOT resubmit the same workflow with trivial changes."
)

POST_FORMAT_NUDGE = (
    "Your reply reads as a progress report, not a completed proposal. "
    "If you are not ready to finalize, emit ASK_QUESTION with a specific question. "
    "Otherwise, finish the workflow and present it as a completed proposal."
)


DEFAULT_ENFORCEMENT_NUDGES: dict[str, str] = {
    "screenshot_dropped": SCREENSHOT_DROPPED_NUDGE,
    "post_update": POST_UPDATE_NUDGE,
    "post_navigate": POST_NAVIGATE_NUDGE,
    "post_intermediate_success": POST_INTERMEDIATE_SUCCESS_NUDGE,
    "post_failed_test": POST_FAILED_TEST_NUDGE,
    "post_failed_test_inspect_first": POST_FAILED_TEST_INSPECT_FIRST_NUDGE,
    "post_explore_without_workflow": POST_EXPLORE_WITHOUT_WORKFLOW_NUDGE,
    "post_suspicious_success": POST_SUSPICIOUS_SUCCESS_NUDGE,
    "post_repeated_null_data": POST_REPEATED_NULL_DATA_NUDGE,
    "post_repeated_frontier_failure_warn": POST_REPEATED_FRONTIER_FAILURE_WARN_NUDGE,
    "post_repeated_frontier_failure_stop": POST_REPEATED_FRONTIER_FAILURE_STOP_NUDGE,
    "post_parameter_binding_warn": POST_PARAMETER_BINDING_WARN_NUDGE,
    "post_non_retriable_nav_error_stop": POST_NON_RETRIABLE_NAV_ERROR_STOP_NUDGE,
    "post_parameter_binding_stop": POST_PARAMETER_BINDING_STOP_NUDGE,
    "post_per_tool_budget": POST_PER_TOOL_BUDGET_NUDGE,
    "post_per_tool_budget_stop": POST_PER_TOOL_BUDGET_STOP_NUDGE,
    "post_no_workflow_delivery": POST_NO_WORKFLOW_DELIVERY_NUDGE,
    "post_discovery_entrypoint_url_question": POST_DISCOVERY_ENTRYPOINT_URL_QUESTION_NUDGE,
    "pre_discovery_url_question": PRE_DISCOVERY_URL_QUESTION_NUDGE,
    "post_probable_site_block_stop_prefix": PROBABLE_SITE_BLOCK_STOP_NUDGE_PREFIX,
    "post_probable_site_block_stop": POST_PROBABLE_SITE_BLOCK_STOP_NUDGE,
    "post_anti_bot_failed_test": POST_ANTI_BOT_FAILED_TEST_NUDGE,
    "post_format": POST_FORMAT_NUDGE,
}


def _default_enforcement_nudges() -> dict[str, str]:
    return dict(DEFAULT_ENFORCEMENT_NUDGES)


def _default_fallback_llm_key() -> str | None:
    return settings.SECONDARY_LLM_KEY


@dataclass(slots=True)
class CopilotConfig:
    prompt_template: str = DEFAULT_PROMPT_TEMPLATE
    max_turns: int = DEFAULT_MAX_TURNS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    security_rules: str = ""
    enforcement_nudges: dict[str, str] = field(default_factory=_default_enforcement_nudges)
    fallback_llm_key: str | None = field(default_factory=_default_fallback_llm_key)
    block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.STANDARD
    impose_synthesized_code_block: bool = False

    def nudge(self, key: str) -> str:
        return self.enforcement_nudges.get(key, DEFAULT_ENFORCEMENT_NUDGES[key])
