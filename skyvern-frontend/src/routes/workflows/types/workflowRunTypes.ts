import { ActionsApiResponse, Status } from "@/api/types";
import { isTaskVariantBlock, WorkflowBlockType } from "./workflowTypes";
import { ActionItem } from "../workflowRun/WorkflowRunOverview";

export const WorkflowRunTimelineItemTypes = {
  Thought: "thought",
  Block: "block",
} as const;

export type WorkflowRunTimelineItemType =
  (typeof WorkflowRunTimelineItemTypes)[keyof typeof WorkflowRunTimelineItemTypes];

export type ObserverThought = {
  thought_id: string;
  user_input: string | null;
  observation: string | null;
  thought: string | null;
  answer: string | null;
  created_at: string;
  modified_at: string;
};

export type WorkflowRunBlock = {
  workflow_run_block_id: string;
  workflow_run_id: string;
  parent_workflow_run_block_id: string | null;
  block_type: WorkflowBlockType;
  label: string | null;
  description: string | null;
  title: string | null;
  status: Status | null;
  failure_reason: string | null;
  output: object | Array<unknown> | string | null;
  continue_on_failure: boolean;
  task_id: string | null;
  url: string | null;
  navigation_goal: string | null;
  navigation_payload: Record<string, unknown> | null;
  data_extraction_goal: string | null;
  data_schema: object | Array<unknown> | string | null;
  terminate_criterion: string | null;
  complete_criterion: string | null;
  include_action_history_in_verification: boolean | null;
  actions: Array<ActionsApiResponse> | null;
  recipients?: Array<string> | null;
  attachments?: Array<string> | null;
  subject?: string | null;
  body?: string | null;
  prompt?: string | null;
  wait_sec?: number | null;
  executed_branch_id?: string | null;
  executed_branch_expression?: string | null;
  executed_branch_result?: boolean | null;
  executed_branch_next_block?: string | null;
  created_at: string;
  modified_at: string;
  duration: number | null;

  // for loop block itself
  loop_values: Array<unknown> | null;

  // for blocks in loop
  current_value: string | null;
  current_index: number | null;

  // human interaction block
  instructions?: string | null;
  positive_descriptor?: string | null;
  negative_descriptor?: string | null;
};

export type WorkflowRunTimelineBlockItem = {
  type: "block";
  block: WorkflowRunBlock;
  children: Array<WorkflowRunTimelineItem>;
  thought: null;
  created_at: string;
  modified_at: string;
};

export type WorkflowRunTimelineThoughtItem = {
  type: "thought";
  block: null;
  children: Array<WorkflowRunTimelineItem>;
  thought: ObserverThought;
  created_at: string;
  modified_at: string;
};

export type WorkflowRunTimelineItem =
  | WorkflowRunTimelineBlockItem
  | WorkflowRunTimelineThoughtItem;

export function isThoughtItem(
  item: unknown,
): item is WorkflowRunTimelineThoughtItem {
  return (
    typeof item === "object" &&
    item !== null &&
    "type" in item &&
    item.type === "thought" &&
    "thought" in item &&
    item.thought !== null
  );
}

export function isBlockItem(
  item: unknown,
): item is WorkflowRunTimelineBlockItem {
  return (
    typeof item === "object" &&
    item !== null &&
    "type" in item &&
    item.type === "block" &&
    "block" in item &&
    item.block !== null
  );
}

export function isTaskVariantBlockItem(item: unknown) {
  return isBlockItem(item) && isTaskVariantBlock(item.block);
}

export function isWorkflowRunBlock(item: unknown): item is WorkflowRunBlock {
  return (
    typeof item === "object" &&
    item !== null &&
    "block_type" in item &&
    "workflow_run_block_id" in item
  );
}

export function isObserverThought(item: unknown): item is ObserverThought {
  return (
    typeof item === "object" &&
    item !== null &&
    "thought_id" in item &&
    "thought" in item
  );
}

export function isAction(item: unknown): item is ActionsApiResponse {
  return typeof item === "object" && item !== null && "action_id" in item;
}

export function isActionItem(item: unknown): item is ActionItem {
  return (
    typeof item === "object" &&
    item !== null &&
    "block" in item &&
    isWorkflowRunBlock(item.block) &&
    "action" in item &&
    isAction(item.action)
  );
}

export function hasExtractedInformation(
  item: unknown,
): item is { extracted_information: unknown } {
  return (
    item !== null && typeof item === "object" && "extracted_information" in item
  );
}

export function hasNavigationGoal(
  item: unknown,
): item is { navigation_goal: unknown } {
  return item !== null && typeof item === "object" && "navigation_goal" in item;
}
