import { Status, type ActionsApiResponse } from "@/api/types";
import { isRecord } from "@/util/utils";

import {
  isBlockItem,
  type WorkflowRunBlock,
  type WorkflowRunTimelineItem,
} from "../types/workflowRunTypes";

export type CodeBlockRecovery = "fix" | "retry" | "either";

export type CodeBlockFailureKind =
  | "declared"
  | "user-code"
  | "browser"
  | "infrastructure"
  | "limit"
  | "cancelled";

export type CodeBlockFailure = {
  kind: CodeBlockFailureKind;
  // The runner error code, or the workflow's own code for a declared error.
  code: string | null;
  title: string;
  guidance: string;
  line: number | null;
  // Which recovery the run surfaces. A sandbox fault or a busy runner says
  // nothing about the code, so offering to fix the code there sends the user
  // rewriting something that was never wrong; a NameError, conversely, will
  // raise again on every retry.
  recovery: CodeBlockRecovery;
};

export type RunCodeBlockFailure = CodeBlockFailure & {
  workflowRunBlockId: string;
};

// Only code and browser faults are claims about the page; infrastructure, limit, and cancelled
// failures have no page state worth showing.
export function failureSupportsScreenshot(failure: CodeBlockFailure): boolean {
  return failure.kind === "user-code" || failure.kind === "browser";
}

type CodeBlockFailureTemplate = Omit<CodeBlockFailure, "code" | "line">;

const INFRASTRUCTURE: CodeBlockFailureTemplate = {
  kind: "infrastructure",
  title: "The code sandbox failed before the block finished",
  guidance:
    "This is a Skyvern-side fault, not a problem with your code. Retry the run, and reach out if it keeps happening.",
  recovery: "retry",
};

// Keyed by CodeBlockRunErrorCode (codeblock/codeblock_runner.py), which the API
// returns verbatim in each block's error_codes.
const FAILURE_TEMPLATES: Record<string, CodeBlockFailureTemplate> = {
  user_code_error: {
    kind: "user-code",
    title: "The block's code raised an error",
    guidance:
      "Open the block and fix the line that raised. Rerunning without an edit fails the same way.",
    recovery: "fix",
  },
  insecure_code_detected: {
    kind: "user-code",
    title: "The code used a blocked operation",
    guidance:
      "Code blocks run in a locked-down sandbox. Rewrite the step using a supported Skyvern helper instead.",
    recovery: "fix",
  },
  unsupported_page_operation: {
    kind: "user-code",
    title: "The code called an unsupported browser operation",
    guidance:
      "Only the page operations Skyvern proxies into the sandbox are available. Use a supported page method, or move the step into a browser block.",
    recovery: "fix",
  },
  unsupported_skyvern_helper: {
    kind: "user-code",
    title: "The code called an unsupported Skyvern helper",
    guidance:
      "Only the helpers Skyvern proxies into the sandbox are available. Check the helper name against the code block reference.",
    recovery: "fix",
  },
  browser_operation_failed: {
    kind: "browser",
    title: "A browser operation failed",
    guidance:
      "The page did not respond as the code expected — the site may be blocking the browser, or the element may not have loaded. Check this block's screenshot, then retry.",
    recovery: "either",
  },
  browser_disconnected: {
    kind: "browser",
    title: "The browser disconnected mid-block",
    guidance:
      "The browser session ended while the code was running, so the block could not finish. Retry the run.",
    recovery: "retry",
  },
  timeout: {
    kind: "limit",
    title: "The code block ran out of time",
    guidance:
      "It hit the code block time limit. Shorten the work, wait on a narrower condition, or split it across blocks.",
    recovery: "either",
  },
  output_limit_exceeded: {
    kind: "limit",
    title: "The block returned too much data",
    guidance:
      "Return only the fields the next block needs instead of the whole payload.",
    recovery: "fix",
  },
  result_limit_exceeded: {
    kind: "limit",
    title: "The block returned too much data",
    guidance:
      "Return only the fields the next block needs instead of the whole payload.",
    recovery: "fix",
  },
  memory_limit_exceeded: {
    kind: "limit",
    title: "The block ran out of memory",
    guidance:
      "Process the data in smaller batches rather than holding it all at once.",
    recovery: "fix",
  },
  busy: {
    kind: "infrastructure",
    title: "The code sandbox was already busy",
    guidance:
      "Another code block held the sandbox, so this one was turned away. Retry the run.",
    recovery: "retry",
  },
  cancelled: {
    kind: "cancelled",
    title: "The code block was cancelled",
    guidance: "The run stopped before the block finished.",
    recovery: "retry",
  },
  runner_unavailable: {
    ...INFRASTRUCTURE,
    title: "The code sandbox was unreachable",
  },
  protocol_error: INFRASTRUCTURE,
  internal_error: INFRASTRUCTURE,
  child_exited: INFRASTRUCTURE,
  child_no_request: INFRASTRUCTURE,
  child_malformed_request: INFRASTRUCTURE,
  unspecified: {
    kind: "user-code",
    title: "The code block failed",
    guidance:
      "Open the block to see which line stopped the run, then edit it or retry.",
    recovery: "either",
  },
};

// The inline engine and older runs persist prose without an error code, so the
// reason text is the only signal. These patterns match the wording emitted by
// skyvern/forge/sdk/workflow/models/block.py and codeblock/workflow.py; keep
// them in step with those strings.
const REASON_PATTERNS: Array<{ pattern: RegExp; code: string }> = [
  { pattern: /insecure code detected/i, code: "insecure_code_detected" },
  {
    pattern: /unsupported browser operation/i,
    code: "unsupported_page_operation",
  },
  {
    pattern: /unsupported Skyvern helper/i,
    code: "unsupported_skyvern_helper",
  },
  { pattern: /browser operation failed/i, code: "browser_operation_failed" },
  { pattern: /browser disconnected/i, code: "browser_disconnected" },
  { pattern: /timed out|TimeoutError/i, code: "timeout" },
  { pattern: /already executing another CodeBlock/i, code: "busy" },
  { pattern: /runner is unavailable/i, code: "runner_unavailable" },
  {
    pattern: /runner failed before completing|sandbox process exited/i,
    code: "internal_error",
  },
  {
    pattern: /exceeded the configured size limit/i,
    code: "output_limit_exceeded",
  },
  {
    pattern: /exceeded the configured memory limit/i,
    code: "memory_limit_exceeded",
  },
  { pattern: /was cancelled/i, code: "cancelled" },
];

// Two ErrorCode states the inline engine reports as prose with no error code of
// its own, and which the generic templates would describe misleadingly.
const REASON_ONLY_FAILURES: Array<{
  pattern: RegExp;
  template: CodeBlockFailureTemplate;
}> = [
  {
    pattern: /ErrorCode is not declared in the effective error_code_mapping/i,
    template: {
      kind: "user-code",
      title: "The block raised an undeclared error code",
      guidance:
        "ErrorCode() was raised with a code that is not in the workflow's error code mapping, so it could not be reported as a workflow error. Declare the code in the mapping, or raise one that is already declared.",
      recovery: "fix",
    },
  },
  {
    pattern: /CodeBlock raised a declared error/i,
    template: {
      kind: "declared",
      title: "The block raised a declared error",
      guidance:
        "The block stopped deliberately on an error code from the workflow's error code mapping.",
      recovery: "retry",
    },
  },
];

const EXCEPTION_NAME_PATTERNS = [
  /CodeBlock failed with ([A-Za-z_][A-Za-z0-9_]*)/,
  /Reason: ([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b/,
];

function exceptionName(reason: string): string | null {
  for (const pattern of EXCEPTION_NAME_PATTERNS) {
    const match = reason.match(pattern);
    if (match?.[1] && match[1] !== "ErrorCode") {
      return match[1];
    }
  }
  return null;
}

function lineFromReason(reason: string): number | null {
  const match = reason.match(/\bat line (\d+)\b/);
  if (!match?.[1]) {
    return null;
  }
  const line = Number.parseInt(match[1], 10);
  return Number.isFinite(line) && line > 0 ? line : null;
}

export function actionCodeLine(action: ActionsApiResponse): number | null {
  const output = action.output;
  if (!output || typeof output !== "object" || Array.isArray(output)) {
    return null;
  }
  const codeLine = (output as Record<string, unknown>).code_line;
  return typeof codeLine === "number" && codeLine > 0 ? codeLine : null;
}

/**
 * The line the code stopped on, taken from the failed recorded action. Actions
 * arrive newest-first, so the first failed one carrying a line is the one that
 * stopped the block.
 */
export function failingCodeLineFromActions(
  actions: Array<ActionsApiResponse> | null | undefined,
): number | null {
  const failingAction = (actions ?? []).find(
    (action) =>
      action.status === Status.Failed && actionCodeLine(action) !== null,
  );
  return failingAction ? actionCodeLine(failingAction) : null;
}

function templateFor(
  errorCodes: Array<string>,
  reason: string,
  output: WorkflowRunBlock["output"],
): { template: CodeBlockFailureTemplate; code: string | null } | null {
  const typedDeclaredError =
    isRecord(output) && Array.isArray(output.errors)
      ? output.errors.find(
          (error) =>
            isRecord(error) &&
            error.error_type === "USER_DEFINED_ERROR" &&
            typeof error.error_code === "string" &&
            errorCodes.includes(error.error_code),
        )
      : null;
  const typedDeclaredCode =
    isRecord(typedDeclaredError) &&
    typeof typedDeclaredError.error_code === "string"
      ? typedDeclaredError.error_code
      : null;
  // The backend's USER_DEFINED_ERROR discriminator is authoritative. The
  // second slot preserves older secure-runner rows, where the runner enum is
  // first and an accepted workflow code is appended without that discriminator.
  const [runnerCode, appendedDeclaredCode] = errorCodes;
  const declaredCode =
    typedDeclaredCode ??
    (runnerCode && runnerCode in FAILURE_TEMPLATES
      ? appendedDeclaredCode
      : undefined);
  if (declaredCode) {
    return {
      code: declaredCode,
      template: {
        kind: "declared",
        title: `The workflow reported "${declaredCode}"`,
        guidance:
          "This code is declared in the workflow's error code mapping, so the block stopped deliberately. The reason below is the one the block reported.",
        recovery: "retry",
      },
    };
  }

  if (
    runnerCode &&
    runnerCode !== "unspecified" &&
    runnerCode in FAILURE_TEMPLATES
  ) {
    return { code: runnerCode, template: FAILURE_TEMPLATES[runnerCode]! };
  }

  const reasonOnly = REASON_ONLY_FAILURES.find(({ pattern }) =>
    pattern.test(reason),
  );
  if (reasonOnly) {
    return { code: null, template: reasonOnly.template };
  }

  const matched = REASON_PATTERNS.find(({ pattern }) => pattern.test(reason));
  if (
    !runnerCode &&
    matched &&
    FAILURE_TEMPLATES[matched.code]?.recovery === "retry" &&
    exceptionName(reason)
  ) {
    return { code: null, template: FAILURE_TEMPLATES.user_code_error! };
  }
  if (matched) {
    return { code: matched.code, template: FAILURE_TEMPLATES[matched.code]! };
  }

  return runnerCode
    ? { code: runnerCode, template: FAILURE_TEMPLATES.unspecified! }
    : { code: null, template: FAILURE_TEMPLATES.unspecified! };
}

/**
 * Classify a failed code block into the error state the UI presents: a plain
 * headline, one actionable next step, and which recovery affordances make sense.
 * Returns null for anything that is not a failed code block.
 */
export function describeCodeBlockFailure(
  block: Pick<
    WorkflowRunBlock,
    "block_type" | "failure_reason" | "error_codes" | "actions" | "output"
  >,
): CodeBlockFailure | null {
  if (block.block_type !== "code") {
    return null;
  }
  const reason = block.failure_reason?.trim() ?? "";
  const errorCodes = (block.error_codes ?? []).filter(
    (code) => typeof code === "string" && code.length > 0,
  );
  if (!reason && errorCodes.length === 0) {
    return null;
  }

  const resolved = templateFor(errorCodes, reason, block.output);
  if (!resolved) {
    return null;
  }
  const { template, code } = resolved;
  const line =
    lineFromReason(reason) ?? failingCodeLineFromActions(block.actions);
  // A named exception is the clearest headline there is, and it also settles a
  // reason the generic templates can only call "the code block failed".
  const raised = template.kind === "user-code" ? exceptionName(reason) : null;

  return {
    ...template,
    title: raised ? `The block's code raised ${raised}` : template.title,
    code,
    line,
  };
}

/**
 * The code block behind a run's failure, if one is. The run reason is the block
 * reason wrapped by each enclosing block ("code block failed. failure reason:
 * …"), so containment ties the two together. Continued failures cannot end the
 * run, and a later finally failure is only the culprit when no body block
 * matches.
 */
export function findRunCodeBlockFailure(
  runFailureReason: string | null | undefined,
  timeline: Array<WorkflowRunTimelineItem> | null | undefined,
  finallyBlockLabel?: string | null,
): RunCodeBlockFailure | null {
  const runReason = runFailureReason?.trim();
  if (!runReason || !timeline) {
    return null;
  }
  const findCulprit = (
    items: Array<WorkflowRunTimelineItem>,
    includeFinally: boolean,
  ): WorkflowRunBlock | null => {
    for (const item of items) {
      if (
        isBlockItem(item) &&
        item.block.block_type === "code" &&
        !item.block.continue_on_failure &&
        (includeFinally || item.block.label !== finallyBlockLabel) &&
        item.block.failure_reason &&
        runReason.includes(item.block.failure_reason.trim())
      ) {
        return item.block;
      }
      const childCulprit = findCulprit(item.children, includeFinally);
      if (childCulprit) {
        return childCulprit;
      }
    }
    return null;
  };
  const culprit = findCulprit(timeline, false) ?? findCulprit(timeline, true);
  if (culprit === null) {
    return null;
  }
  const failure = describeCodeBlockFailure(culprit);
  return failure === null
    ? null
    : { ...failure, workflowRunBlockId: culprit.workflow_run_block_id };
}
