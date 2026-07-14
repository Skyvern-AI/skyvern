import { useLocation } from "react-router-dom";
import {
  ProxyLocation,
  type WorkflowRunStatusApiResponseWithWorkflow,
} from "@/api/types";
import type { Parameter, WorkflowParameter } from "./types/workflowTypes";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

type Location = ReturnType<typeof useLocation>;

export function getRerunNavigationState(
  workflowRun: WorkflowRunStatusApiResponseWithWorkflow,
) {
  return {
    data: workflowRun.parameters ?? {},
    proxyLocation: workflowRun.proxy_location ?? ProxyLocation.Residential,
    webhookCallbackUrl: workflowRun.webhook_callback_url ?? "",
    maxScreenshotScrolls: workflowRun.max_screenshot_scrolls ?? null,
    runWith: workflowRun.run_with ?? "agent",
    browserProfileId: workflowRun.browser_profile_id ?? null,
  };
}

/**
 * Keep JSON workflow parameters as editable strings in react-hook-form state.
 * Parsed arrays/objects from API re-runs are stringified so CodeEditor state
 * matches what the user sees (SKY-10854).
 */
export function normalizeJsonParameterFormValue(value: unknown): unknown {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

/**
 * Parse a JSON workflow parameter for the run API. Only string values are
 * passed through JSON.parse — already-parsed arrays/objects are returned as-is.
 * Single-item arrays must not go through JSON.parse(Array) which coerces to a
 * bare scalar (SKY-10854).
 */
export function parseJsonWorkflowParameterValue(value: unknown): unknown {
  if (value === null || value === undefined) {
    return value;
  }
  if (typeof value !== "string") {
    return value;
  }
  const trimmed = value.trim();
  if (trimmed === "") {
    return value;
  }
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

/**
 * Validate JSON workflow parameter form values. `null` is valid JSON and is the
 * default for params without a saved default; only empty/unparseable input fails.
 */
export function validateJsonWorkflowParameterValue(
  value: unknown,
): true | string {
  if (value === null || value === undefined) {
    return true;
  }
  if (typeof value !== "string") {
    return true;
  }
  const trimmed = value.trim();
  if (trimmed === "") {
    return "This field is required";
  }
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return "Invalid JSON";
  }
}

function normalizeWorkflowParameterFormValue(
  parameter: WorkflowParameter,
  value: unknown,
): unknown {
  if (parameter.workflow_parameter_type === "json") {
    return normalizeJsonParameterFormValue(value);
  }
  return value;
}

const getDefaultFormValueForParameter = (
  parameter: WorkflowParameter,
): unknown => {
  const hasDefaultValue =
    parameter.default_value !== null && parameter.default_value !== undefined;
  if (!hasDefaultValue) {
    return undefined;
  }
  if (parameter.workflow_parameter_type === "json") {
    if (typeof parameter.default_value === "string") {
      return parameter.default_value;
    }
    return JSON.stringify(parameter.default_value, null, 2);
  }
  if (parameter.workflow_parameter_type === "boolean") {
    return (
      parameter.default_value === "true" || parameter.default_value === true
    );
  }
  return parameter.default_value;
};

const getDefaultsFromWorkflowParameters = (
  workflowParameters: WorkflowParameter[],
): Record<string, unknown> => {
  return workflowParameters.reduce(
    (acc, parameter) => {
      const defaultValue = getDefaultFormValueForParameter(parameter);
      acc[parameter.key] =
        defaultValue !== undefined
          ? defaultValue
          : parameter.workflow_parameter_type === "string"
            ? ""
            : null;
      return acc;
    },
    {} as Record<string, unknown>,
  );
};

export const getInitialValues = (
  location: Location,
  workflowParameters: WorkflowParameter[],
  lastRunValues?: Record<string, unknown> | null,
): Record<string, unknown> => {
  if (location.state?.data) {
    const raw = {
      ...(location.state.data as Record<string, unknown>),
    };
    for (const parameter of workflowParameters) {
      if (Object.prototype.hasOwnProperty.call(raw, parameter.key)) {
        raw[parameter.key] = normalizeWorkflowParameterFormValue(
          parameter,
          raw[parameter.key],
        );
      }
    }
    return raw;
  }

  const defaults = getDefaultsFromWorkflowParameters(workflowParameters);

  if (!lastRunValues) {
    return defaults;
  }

  return workflowParameters.reduce<Record<string, unknown>>(
    (acc, parameter) => {
      acc[parameter.key] = Object.prototype.hasOwnProperty.call(
        lastRunValues,
        parameter.key,
      )
        ? normalizeWorkflowParameterFormValue(
            parameter,
            lastRunValues[parameter.key],
          )
        : defaults[parameter.key];
      return acc;
    },
    {},
  );
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

/**
 * Returns run parameter entries ordered by the workflow definition's parameter array.
 * Falls back to Object.entries() if no definition is available.
 */
export function getOrderedRunParameters(
  definitionParameters: Array<Parameter> | undefined,
  runParameters: Record<string, unknown>,
): Array<[string, unknown]> {
  if (!definitionParameters) {
    return Object.entries(runParameters);
  }

  const orderedKeys = definitionParameters
    .filter((p) => p.parameter_type === "workflow")
    .map((p) => p.key);

  const seenKeys = new Set(orderedKeys);

  const ordered: Array<[string, unknown]> = orderedKeys
    .filter((key) => key in runParameters)
    .map((key) => [key, runParameters[key]]);

  // Append any run parameters not in the definition (backward compat)
  for (const [key, value] of Object.entries(runParameters)) {
    if (!seenKeys.has(key)) {
      ordered.push([key, value]);
    }
  }

  return ordered;
}

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
