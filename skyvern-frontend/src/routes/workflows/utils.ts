import { useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ProxyLocation } from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { Parameter, WorkflowParameter } from "./types/workflowTypes";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

type Location = ReturnType<typeof useLocation>;

/**
 * Internal react-hook-form field name for the browser-type SETTING. Hyphens make it an invalid
 * workflow-parameter identifier, so it can never collide with a user parameter literally named
 * `browserType`. RHF stores names that aren't valid dotted paths as flat keys, so this reads/writes
 * as a single top-level field.
 */
export const RESERVED_BROWSER_TYPE_FIELD = "__skyvern-browser-type";

/**
 * Split the reserved internal browser-type setting out of the run-form values: returns the selected
 * setting (null = Default) and the remaining values (still carrying any user parameter literally
 * named `browserType`, whose value must reach the request `data` unchanged).
 */
export function extractBrowserTypeSetting(values: Record<string, unknown>): {
  browserType: string | null;
  rest: Record<string, unknown>;
} {
  const { [RESERVED_BROWSER_TYPE_FIELD]: browserTypeSetting, ...rest } = values;
  return {
    browserType:
      browserTypeSetting === undefined || browserTypeSetting === null
        ? null
        : (browserTypeSetting as string),
    rest,
  };
}

/**
 * An attached browser owns its engine: a live session (`browserSessionId`) or a remote CDP address
 * (`browserAddress`) is the browser for the run, so the backend rejects a run-level browser_type
 * alongside it. This is the single source of truth for suppressing the Browser Type selector and
 * omitting browser_type from the request. A browser PROFILE is not an attachment.
 */
export function browserTypeSelectionDisabled(attachment: {
  browserSessionId?: string | null;
  browserAddress?: string | null;
}): boolean {
  return Boolean(
    attachment.browserSessionId?.trim() || attachment.browserAddress?.trim(),
  );
}

export type BrowserTypeOption = {
  value: string;
  label: string;
};

/**
 * Whether the Browser Type selector should render at all. The options come from `/browser_types`,
 * which is `undefined` while loading or on error and `[]` if the backend returns nothing; in every
 * one of those states the control would degrade to a lone misleading `Default` item, so both the
 * workflow-settings and run-form selectors gate on a real non-empty option set instead.
 */
export function hasBrowserTypeOptions(
  options: Array<BrowserTypeOption> | undefined,
): options is Array<BrowserTypeOption> {
  return Array.isArray(options) && options.length > 0;
}

/**
 * Selectable browser engines, served by the backend `GET /browser_types` endpoint (the workflow/run
 * `BrowserType` domain source). Fetching them keeps the picker in sync with the backend without a
 * hand-maintained frontend option list — a newly supported backend type appears here automatically.
 */
export function useBrowserTypeOptionsQuery() {
  const credentialGetter = useCredentialGetter();

  return useQuery<Array<BrowserTypeOption>>({
    queryKey: ["browser-types"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get<Array<BrowserTypeOption>>("/browser_types")
        .then((response) => response.data);
    },
    staleTime: 60 * 60 * 1000,
  });
}

/**
 * The rerun/retry navigation-state fragment carrying the executed run's browser_type — shared by
 * every Rerun/Retry caller (the studio callers via getRerunNavigationState, and the run-overview
 * recovery-guidance Retry) so the destination run form preselects what actually ran. Included only
 * when the source exposes the property, so an older backend / legacy fixture without it keeps the
 * prior navigation-state shape; the current backend always emits persisted browser_type, so a real
 * rerun carries it and an Edge override wins over the workflow default.
 */
export function rerunBrowserTypeState(source: {
  browser_type?: string | null;
}): { browserType?: string | null } {
  return "browser_type" in source
    ? { browserType: source.browser_type ?? null }
    : {};
}

type RerunNavigationSource = {
  parameters?: Record<string, unknown> | null;
  proxy_location?: ProxyLocation | null;
  webhook_callback_url?: string | null;
  max_screenshot_scrolls?: number | null;
  run_with?: string | null;
  browser_profile_id?: string | null;
  browser_type?: string | null;
};

/**
 * Resolve the browser-type a rerun/retry form should preselect. Contract: a run-level
 * browser_type of `null` means INHERIT the workflow setting — only workflow+run both null yields
 * dynamic routing, and there is no "force system default" sentinel. So a non-null executed-run value
 * wins, but a `null` OR absent rerun value falls back to the workflow's CURRENT browser_type. A source
 * run that ran with null, after the workflow later changed to Chrome, must rerun as Chrome — matching
 * how the backend resolves an omitted/null run value against the workflow default.
 */
export function resolveInitialBrowserType(
  locationState: unknown,
  workflowBrowserType?: string | null,
): string | null {
  if (
    locationState !== null &&
    typeof locationState === "object" &&
    "browserType" in locationState
  ) {
    const stateBrowserType = (locationState as { browserType?: string | null })
      .browserType;
    if (stateBrowserType != null) {
      return stateBrowserType;
    }
  }
  return workflowBrowserType ?? null;
}

export function getRerunNavigationState(workflowRun: RerunNavigationSource) {
  return {
    data: workflowRun.parameters ?? {},
    proxyLocation: workflowRun.proxy_location ?? ProxyLocation.Residential,
    webhookCallbackUrl: workflowRun.webhook_callback_url ?? "",
    maxScreenshotScrolls: workflowRun.max_screenshot_scrolls ?? null,
    runWith: workflowRun.run_with ?? "agent",
    browserProfileId: workflowRun.browser_profile_id ?? null,
    ...rerunBrowserTypeState(workflowRun),
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

export const shouldPollForGeneratedCode = (
  workflow: WorkflowApiResponse | undefined,
  isFinalized: boolean | null,
  hasPublishedCode: boolean,
) =>
  !isFinalized &&
  !hasPublishedCode &&
  Boolean(
    workflow?.workflow_definition.blocks.some(
      (block) =>
        block.label !== workflow.workflow_definition.finally_block_label,
    ),
  );

/**
 * Returns run parameter entries ordered by the workflow definition's parameter array,
 * each paired with its matched workflow-parameter definition (undefined when absent).
 * Falls back to Object.entries() ordering if no definition is available.
 */
export function getOrderedRunParameters(
  definitionParameters: Array<Parameter> | undefined,
  runParameters: Record<string, unknown>,
): Array<[string, unknown, WorkflowParameter?]> {
  const workflowDefs = (definitionParameters ?? []).filter(
    (p): p is WorkflowParameter => p.parameter_type === "workflow",
  );
  const defByKey = new Map(workflowDefs.map((p) => [p.key, p] as const));
  const attach = (
    key: string,
    value: unknown,
  ): [string, unknown, WorkflowParameter?] => [key, value, defByKey.get(key)];

  if (!definitionParameters) {
    return Object.entries(runParameters).map(([key, value]) =>
      attach(key, value),
    );
  }

  const orderedKeys = workflowDefs.map((p) => p.key);
  const seenKeys = new Set(orderedKeys);

  const ordered: Array<[string, unknown, WorkflowParameter?]> = orderedKeys
    .filter((key) => key in runParameters)
    .map((key) => attach(key, runParameters[key]));

  // Append any run parameters not in the definition (backward compat)
  for (const [key, value] of Object.entries(runParameters)) {
    if (!seenKeys.has(key)) {
      ordered.push(attach(key, value));
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
