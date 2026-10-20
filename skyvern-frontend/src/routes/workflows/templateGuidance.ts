import { workflowBlockTitle } from "./editor/nodes/types";
import {
  WorkflowParameterValueType,
  type WorkflowApiResponse,
  type WorkflowBlock,
} from "./types/workflowTypes";

export const TEMPLATE_VIA = "template";
const MAX_STEP_LENGTH = 110;
const MAX_STEPS = 8;
export const TEMPLATE_GUIDANCE_MESSAGE_ID = "template-guidance";
export const TEMPLATE_INPUTS_MESSAGE_ID = "template-inputs";

function describeBlock(block: WorkflowBlock): string {
  const fields = block as unknown as Record<string, unknown>;
  const field = [
    "navigation_goal",
    "data_extraction_goal",
    "prompt",
    "goal",
    "url",
  ].find((name) => typeof fields[name] === "string" && fields[name]);
  const goal = field ? String(fields[field]).trim() : "";
  const text = goal
    ? field === "url"
      ? `Open ${goal}`
      : (goal.split(/(?<=[.!?])\s/)[0] ?? goal)
    : workflowBlockTitle[block.block_type];
  const oneLine = text.replace(/\s+/g, " ");
  const step =
    oneLine.length > MAX_STEP_LENGTH
      ? `${oneLine.slice(0, MAX_STEP_LENGTH)}…`
      : oneLine;
  return block.block_type === "for_loop" ? `Repeat: ${step}` : step;
}

function buildSteps(blocks: Array<WorkflowBlock>): string {
  if (blocks.length === 0) return "";
  const shown = blocks
    .slice(0, MAX_STEPS)
    .map((block, index) => `${index + 1}. ${describeBlock(block)}`);
  const more =
    blocks.length > MAX_STEPS ? [`…and ${blocks.length - MAX_STEPS} more`] : [];
  return `**What it does**\n\n${[...shown, ...more].join("\n")}`;
}

export function buildTemplateGuidanceMessage(
  workflow: Pick<
    WorkflowApiResponse,
    "title" | "description" | "workflow_definition"
  >,
): string {
  const inputs = workflow.workflow_definition.parameters.filter(
    (parameter) =>
      parameter.parameter_type === "workflow" ||
      parameter.parameter_type === "credential",
  );
  const isCredential = (parameter: (typeof inputs)[number]) =>
    parameter.parameter_type === "credential" ||
    (parameter.parameter_type === "workflow" &&
      parameter.workflow_parameter_type ===
        WorkflowParameterValueType.CredentialId);
  const hasCredential = inputs.some(isCredential);
  const hasOther = inputs.some((parameter) => !isCredential(parameter));
  const steps = buildSteps(workflow.workflow_definition.blocks);
  const intro = [
    `I copied **${workflow.title}** into your agents.`,
    workflow.description,
    steps,
  ]
    .filter(Boolean)
    .join("\n\n");
  if (inputs.length === 0) {
    return `${intro}\n\nIt has no inputs to fill in, so you can run it as is.`;
  }
  const ask = !hasCredential
    ? "fill in the inputs below, or tell me the values here."
    : `fill in the inputs below. Choose or add credentials in the card, and never share a password in chat.${
        hasOther ? " Other values you can also tell me here." : ""
      }`;
  return `${intro}\n\n**To run it**, ${ask}`;
}

export function withoutTemplateViaParam(search: string): string {
  const params = new URLSearchParams(search);
  if (params.get("via") !== TEMPLATE_VIA) {
    return search;
  }
  params.delete("via");
  const next = params.toString();
  return next ? `?${next}` : "";
}
