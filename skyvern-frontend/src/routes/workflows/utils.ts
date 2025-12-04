import { useLocation } from "react-router-dom";
import type { WorkflowParameter } from "./types/workflowTypes";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

type Location = ReturnType<typeof useLocation>;

export const getInitialValues = (
  location: Location,
  workflowParameters: WorkflowParameter[],
) => {
  const iv = location.state?.data
    ? location.state.data
    : workflowParameters?.reduce(
        (acc, curr) => {
          const hasDefaultValue =
            curr.default_value !== null && curr.default_value !== undefined;
          if (hasDefaultValue) {
            // Handle JSON parameters
            if (curr.workflow_parameter_type === "json") {
              if (typeof curr.default_value === "string") {
                acc[curr.key] = curr.default_value;
              } else {
                acc[curr.key] = JSON.stringify(curr.default_value, null, 2);
              }
              return acc;
            }
            if (curr.workflow_parameter_type === "boolean") {
              // Backend stores as strings, convert to boolean for frontend
              acc[curr.key] =
                curr.default_value === "true" || curr.default_value === true;
              return acc;
            }
            acc[curr.key] = curr.default_value;
            return acc;
          }
          acc[curr.key] = null;
          return acc;
        },
        {} as Record<string, unknown>,
      );

  return iv as Record<string, unknown>;
};

export interface Duration {
  hour: number;
  minute: number;
  second: number;
}

export const toDuration = (seconds: number): Duration => {
  let minutes = Math.floor(seconds / 60);
  let hours = Math.floor(minutes / 60);
  seconds = seconds % 60;
  minutes = minutes % 60;
  hours = hours % 24;

  return {
    hour: Math.floor(hours),
    minute: Math.floor(minutes),
    second: Math.floor(seconds),
  };
};

export const formatDuration = (duration: Duration): string => {
  if (duration.hour) {
    return `${duration.hour}h ${duration.minute}m ${duration.second}s`;
  } else if (duration.minute) {
    return `${duration.minute}m ${duration.second}s`;
  } else {
    return `${duration.second}s`;
  }
};

export const getOrderedBlockLabels = (workflow?: WorkflowApiResponse) => {
  if (!workflow) {
    return [];
  }

  const blockLabels = workflow.workflow_definition.blocks.map(
    (block) => block.label,
  );

  return blockLabels;
};

export const getCode = (
  orderedBlockLabels: string[],
  blockScripts?: {
    [blockName: string]: string;
  },
): string[] => {
  const blockCode: string[] = [];
  const startBlockCode = blockScripts?.__start_block__;

  if (startBlockCode) {
    blockCode.push(startBlockCode);
  }

  for (const blockLabel of orderedBlockLabels) {
    const code = blockScripts?.[blockLabel];

    if (!code) {
      continue;
    }

    blockCode.push(`${code}
`);
  }

  return blockCode;
};
