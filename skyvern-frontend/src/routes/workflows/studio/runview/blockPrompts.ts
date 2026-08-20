import {
  type WorkflowBlock,
  type WorkflowBlockType,
} from "../../types/workflowTypes";
import { visitWorkflowBlocks } from "../../workflowBlockUtils";

export type BlockPromptField = { fieldLabel: string; prompt: string };
export type BlockPrompt = {
  blockLabel: string;
  blockType: WorkflowBlockType;
  fields: BlockPromptField[];
};

function getBlockFields(block: WorkflowBlock): BlockPromptField[] {
  const fields: BlockPromptField[] = [];
  const push = (fieldLabel: string, value: unknown) => {
    if (typeof value === "string" && value.trim().length > 0) {
      fields.push({ fieldLabel, prompt: value });
    }
  };

  if ("navigation_goal" in block) {
    push("Navigation goal", block.navigation_goal);
  }
  if ("data_extraction_goal" in block) {
    push("Extraction goal", block.data_extraction_goal);
  }
  if ("prompt" in block) {
    push("Prompt", block.prompt);
  }
  if ("instructions" in block) {
    push("Instructions", block.instructions);
  }
  if ("complete_criterion" in block) {
    push("Completion criterion", block.complete_criterion);
  }
  if ("terminate_criterion" in block) {
    push("Termination criterion", block.terminate_criterion);
  }

  return fields;
}

// Conditional-branch and while-loop natural-language criteria are intentionally excluded.
export function collectBlockPrompts(blocks: WorkflowBlock[]): BlockPrompt[] {
  const out: BlockPrompt[] = [];
  visitWorkflowBlocks(blocks, (block) => {
    const fields = getBlockFields(block);
    if (fields.length > 0) {
      out.push({
        blockLabel: block.label,
        blockType: block.block_type,
        fields,
      });
    }
  });
  return out;
}
