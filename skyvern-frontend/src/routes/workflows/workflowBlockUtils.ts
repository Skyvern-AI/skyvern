import {
  ActionTypes,
  getReadableActionType,
  type ActionsApiResponse,
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

export function visitWorkflowBlocks(
  blocks: Array<WorkflowBlock>,
  visit: (block: WorkflowBlock) => void | false,
) {
  for (const block of blocks) {
    if (visit(block) === false) {
      return false;
    }

    if (isNestedLoopWorkflowBlock(block) && block.loop_blocks.length > 0) {
      if (visitWorkflowBlocks(block.loop_blocks, visit) === false) {
        return false;
      }
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
 * Plain-English text for a code-block step: prefer the generated title, then
 * the description, and only humanize the raw action type when neither is
 * present.
 */
export function getCodeStepPlainText(step: CodeBlockStep): string {
  const title = step.title?.trim();
  if (title) {
    return title;
  }
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
