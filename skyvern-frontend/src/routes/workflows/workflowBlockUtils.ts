import {
  ActionTypes,
  getReadableActionType,
  type ActionsApiResponse,
  type ActionSummary,
  type ActionSummaryBody,
} from "@/api/types";

import {
  isNestedLoopWorkflowBlock,
  type CodeBlockStep,
  type WorkflowBlock,
  type WorkflowBlockType,
} from "./types/workflowTypes";

export function findWorkflowBlockByLabel(
  blocks: Array<WorkflowBlock>,
  label: string,
): WorkflowBlock | null {
  let found: WorkflowBlock | null = null;

  visitWorkflowBlocks(blocks, (block) => {
    if (!found && block.label === label) {
      found = block;
      return false;
    }
  });

  return found;
}

// Pre-order in reading order: the next_block_label chain, a conditional's
// branches before its merge block, a loop's body right after the loop. The
// editor serializes conditional-branch children after the top-level chain, so
// plain array order would list the merge block ahead of the branches.
export function visitWorkflowBlocks(
  blocks: Array<WorkflowBlock>,
  visit: (block: WorkflowBlock) => void | false,
): boolean {
  const byLabel = new Map(blocks.map((block) => [block.label, block]));
  const visited = new Set<string>();

  const walk = (
    label: string | null | undefined,
    stop: string | null,
  ): boolean => {
    const block = label ? byLabel.get(label) : undefined;
    if (!block || label === stop || visited.has(block.label)) {
      return true;
    }
    visited.add(block.label);
    if (visit(block) === false) {
      return false;
    }
    if (
      isNestedLoopWorkflowBlock(block) &&
      block.loop_blocks.length > 0 &&
      !visitWorkflowBlocks(block.loop_blocks, visit)
    ) {
      return false;
    }
    if (block.block_type === "conditional") {
      const merge = block.next_block_label ?? stop;
      for (const branch of block.branch_conditions) {
        if (!walk(branch.next_block_label, merge)) {
          return false;
        }
      }
    }
    return walk(block.next_block_label, stop);
  };

  // Array order seeds the walk: the head of the chain first, then whatever a
  // chain never reached (v1 fall-through, orphans) so nothing is skipped.
  for (const block of blocks) {
    if (!walk(block.label, null)) {
      return false;
    }
  }

  return true;
}

export function isBlockOfType<T extends WorkflowBlockType>(
  block: WorkflowBlock | null,
  type: T,
): block is Extract<WorkflowBlock, { block_type: T }> {
  return block?.block_type === type;
}

/**
 * Map each code block's label to its definition step outline, descending into
 * loop bodies. The run timeline carries no step outline on the runtime block, so
 * it looks steps up here by label to render them beneath the code block.
 */
export function buildCodeStepsByLabel(
  blocks: Array<WorkflowBlock>,
): Map<string, Array<CodeBlockStep>> {
  const stepsByLabel = new Map<string, Array<CodeBlockStep>>();

  visitWorkflowBlocks(blocks, (block) => {
    if (block.block_type === "code" && block.steps && block.steps.length > 0) {
      stepsByLabel.set(block.label, block.steps);
    }
  });

  return stepsByLabel;
}

/**
 * Plain-English text for a code-block step: the description, and only
 * humanize the raw action type when it is absent.
 */
export function getCodeStepPlainText(step: CodeBlockStep): string {
  const description = step.description?.trim();
  if (description) {
    return description;
  }
  return getReadableActionType(step.action_type);
}

/**
 * Resolve the definition step a recorded action belongs to by its source line.
 * A fired action carries its `code_line`; match it to the step whose
 * `line_start` equals that line, falling back to the step whose
 * `[line_start, line_end]` range contains it.
 */
export function findCodeStepForLine(
  steps: Array<CodeBlockStep>,
  codeLine: number | null,
): CodeBlockStep | null {
  if (codeLine == null) {
    return null;
  }
  const exact = steps.find((step) => step.line_start === codeLine);
  if (exact) {
    return exact;
  }
  return (
    steps.find(
      (step) =>
        step.line_start != null &&
        codeLine >= step.line_start &&
        codeLine <= (step.line_end ?? step.line_start),
    ) ?? null
  );
}

export function normalizeInlineText(
  value: string | null | undefined,
): string | null {
  const normalized = value?.replace(/\s+/g, " ").trim();
  return normalized ? normalized : null;
}

// The code-block recorder writes `description` two ways: a "<receiver>.<method> <selector>"
// trace for ordinary calls, or the author's own prompt text for page.extract/complete. Only
// the first is machine syntax, and only these three receivers are ever emitted.
const RECORDER_CALL_TEXT = /^(?:locator|page|keyboard)\.[A-Za-z_]+(?:\s|$)/;

export function isRecorderCallText(
  description: string | null | undefined,
): boolean {
  const text = normalizeInlineText(description);
  return text !== null && RECORDER_CALL_TEXT.test(text);
}

// Task V3 stamps every persisted action's `description` as "task_v3 <tool> <argument>", where the
// argument is usually a model-authored CSS selector (skyvern/webeye/actions/actions.py
// TASK_V3_ACTION_DESCRIPTION_PREFIX). Like the recorder trace above it is machine syntax, so it
// belongs on hover and in the inspector, never on a row's main line.
const TASK_V3_CALL_PREFIX = "task_v3 ";

export function taskV3CallText(
  description: string | null | undefined,
): string | null {
  const text = normalizeInlineText(description);
  if (text === null || !text.startsWith(TASK_V3_CALL_PREFIX)) {
    return null;
  }
  return normalizeInlineText(text.slice(TASK_V3_CALL_PREFIX.length));
}

export type ActionSummarySource = Partial<
  Pick<
    ActionsApiResponse,
    "action_type" | "reasoning" | "intention" | "response" | "text"
  >
>;

export function getActionInputValue(
  action: ActionSummarySource,
): string | null {
  // Script-generated input text lives in response, not text.
  if (action.action_type === ActionTypes.InputText) {
    return action.text ?? action.response ?? null;
  }
  return action.text ?? null;
}

/**
 * What the action actually did, when the run recorded it: a navigation's landing and HTTP status, a
 * recorded call's return value, the exception that ended a code block.
 *
 * `response` doubles as the stored input on input-text actions — a cached run writes the same answer
 * to both — so it is not echoed as an outcome there. Every other action type legitimately records
 * its result in `response` even when that equals `text`.
 */
export function getActionOutcome(action: ActionSummarySource): string | null {
  if (
    action.action_type === ActionTypes.InputText &&
    typeof action.response === "string" &&
    action.response === getActionInputValue(action)
  ) {
    return null;
  }
  return action.response ?? null;
}

/**
 * The reader-facing text for one action: what it meant to do, and what came of it.
 *
 * The body is the most specific account of the intent the row carries — the model's own reasoning,
 * else the deterministic intention the agent recorded, else the value it typed. A Task V3 turn
 * often emits no prose at all, and only `intention` and `response` carry a navigation's URL:
 * `GotoUrlAction.url` is subclass-only and never reaches the client.
 *
 * A recorded outcome is returned alongside that body rather than behind it, so a card never shows
 * the plan in place of the effect — an action whose intention reads "Tried to navigate to X" and
 * whose response reads "HTTP 404, dead end" must not render as the first alone.
 *
 * Returns null when the action carries neither, so a caller that already shows the action type does
 * not print it twice.
 */
export function getActionSummary(
  action: ActionSummarySource,
): ActionSummary | null {
  const candidates: Array<[string | null | undefined, boolean]> = [
    [action.reasoning, true],
    [action.intention, true],
    [action.text, false],
  ];
  let body: ActionSummaryBody | null = null;
  for (const [value, isProse] of candidates) {
    const text = value?.trim();
    if (text) {
      body = { text, isProse };
      break;
    }
  }
  const recorded = normalizeInlineText(getActionOutcome(action));
  const outcome =
    recorded !== null && recorded !== normalizeInlineText(body?.text)
      ? recorded
      : null;
  if (body === null && outcome === null) {
    return null;
  }
  return { body, outcome };
}

/**
 * Reader-facing text for one recorded action, in descending order of specificity: the definition
 * step it fired from, whatever prose the action carries, the author's own prompt, then the one
 * recorder argument worth reading.
 *
 * Returns null when nothing beats the action's own type name, so a caller that already renders
 * the type does not print it twice. A raw Playwright selector is never returned.
 */
export function describeRecordedAction(
  action: ActionsApiResponse,
  matchedStep: CodeBlockStep | null,
): string | null {
  // Line numbers drift when a code block is edited between runs, so a step only names
  // this action when their kinds also agree; otherwise a stale outline labels the wrong step.
  if (matchedStep && matchedStep.action_type === action.action_type) {
    const stepText = normalizeInlineText(getCodeStepPlainText(matchedStep));
    if (stepText) {
      return stepText;
    }
  }

  const authored =
    normalizeInlineText(action.reasoning) ??
    normalizeInlineText(action.text) ??
    normalizeInlineText(action.response);
  if (authored) {
    return authored;
  }

  if (!isRecorderCallText(action.description)) {
    const prompt = normalizeInlineText(action.description);
    if (prompt) {
      return prompt;
    }
  }

  // Only base-Action fields are readable here: the timeline serializes actions as
  // `list[Action]`, so subclass-only fields (url, keys) never reach the client.
  if (action.action_type === ActionTypes.DownloadFile) {
    const fileName = normalizeInlineText(action.file_name);
    if (fileName) {
      return `Download ${fileName}`;
    }
  }

  // `_describe` builds "<receiver>.<method> <argument>"; for a navigation the argument is the
  // destination, which is the one recorder argument worth reading.
  if (action.action_type === ActionTypes.GotoUrl) {
    const target = normalizeInlineText(action.description)
      ?.split(/\s+/)
      .slice(1)
      .join(" ");
    if (target) {
      return `Open ${target}`;
    }
  }

  return null;
}
