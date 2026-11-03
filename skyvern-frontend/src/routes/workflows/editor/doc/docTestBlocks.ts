import { ProxyLocation } from "@/api/types";

import type {
  WorkflowBlock,
  WorkflowSettings,
} from "../../types/workflowTypes";

export const settings = (finallyBlockLabel: string | null = null) =>
  ({
    proxyLocation: ProxyLocation.Residential,
    finallyBlockLabel,
  }) as unknown as WorkflowSettings;

type Extra = Record<string, unknown>;
export const block = (
  block_type: string,
  label: string,
  next: string | null,
  extra: Extra = {},
) =>
  ({
    label,
    block_type,
    next_block_label: next,
    continue_on_failure: false,
    model: null,
    output_parameter: {
      parameter_type: "output",
      key: `${label}_output`,
      output_parameter_id: `op-${label}`,
    },
    ...extra,
  }) as unknown as WorkflowBlock;
export const code = (label: string, next: string | null, extra: Extra = {}) =>
  block("code", label, next, { code: "", parameters: [], ...extra });
export const forLoop = (
  label: string,
  next: string | null,
  loopBlocks: WorkflowBlock[],
  extra: Extra = {},
) =>
  block("for_loop", label, next, {
    loop_over: { key: "items" },
    loop_blocks: loopBlocks,
    loop_variable_reference: "{{ items }}",
    complete_if_empty: false,
    ...extra,
  });
type Branch = { id: string; next: string | null; when?: string; exact?: true };
export const conditional = (
  label: string,
  next: string | null,
  branches: Branch[],
  extra: Extra = {},
) =>
  block("conditional", label, next, {
    branch_conditions: branches.map((b) => ({
      id: b.id,
      description: null,
      next_block_label: b.next,
      is_default: b.when === undefined,
      criteria:
        b.when === undefined
          ? null
          : {
              criteria_type: b.exact ? "jinja2_template" : "prompt",
              expression: b.when,
              description: null,
            },
    })),
    ...extra,
  });
