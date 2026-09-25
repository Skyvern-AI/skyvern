import {
  isPlainMapping,
  YamlCommitError,
} from "../../workflowVersionFromSaveData";
import type {
  WorkflowRetryPolicy,
  WorkflowRetryStatus,
} from "@/routes/workflows/types/workflowTypes";

export function clampRetryInteger(
  value: string | number,
  min: number,
  max: number,
  fallback: number,
): number {
  const number =
    typeof value === "string" && value.trim() === "" ? NaN : Number(value);
  return Number.isFinite(number)
    ? Math.min(max, Math.max(min, Math.trunc(number)))
    : fallback;
}

export function normalizeRetryPolicy(
  policy: WorkflowRetryPolicy | null | undefined,
): WorkflowRetryPolicy | null {
  if (!policy) return null;
  return {
    max_retries: clampRetryInteger(policy.max_retries, 1, 5, 1),
    delay_seconds: clampRetryInteger(policy.delay_seconds, 0, 3600, 0),
    webhook_on_retry: policy.webhook_on_retry,
    retry_on: policy.retry_on.map(({ status, error_codes }) => {
      const codes = [...new Set(error_codes ?? [])];
      return codes.length ? { status, error_codes: codes } : { status };
    }),
  };
}

export function collectKnownErrorCodes(
  nodes: ReadonlyArray<{ data: Record<string, unknown> }>,
): Array<string> {
  const codes = new Set<string>();
  for (const node of nodes) {
    let mapping: unknown = node.data.errorCodeMapping;
    if (typeof mapping === "string") {
      try {
        mapping = JSON.parse(mapping) as unknown;
      } catch {
        continue;
      }
    }
    if (mapping === null || typeof mapping !== "object") continue;
    const prototype: unknown = Object.getPrototypeOf(mapping);
    if (prototype !== Object.prototype && prototype !== null) continue;
    for (const code of Object.keys(mapping)) codes.add(code);
  }
  return [...codes].sort();
}

export function validateRetryPolicyInput(
  value: unknown,
): WorkflowRetryPolicy | null {
  const fail = (message: string): never => {
    throw new YamlCommitError("retry_policy", message);
  };
  if (value === null) return null;
  if (!isPlainMapping(value)) return fail("must be a mapping or null");
  for (const key of Object.keys(value)) {
    if (
      ![
        "max_retries",
        "delay_seconds",
        "webhook_on_retry",
        "retry_on",
      ].includes(key)
    ) {
      throw new YamlCommitError(`retry_policy.${key}`, "unknown setting");
    }
  }
  const withDefaults = {
    max_retries: 1,
    delay_seconds: 0,
    webhook_on_retry: "final_only",
    ...value,
  };
  const { max_retries, delay_seconds, webhook_on_retry } = withDefaults;
  const { retry_on } = value;
  if (
    typeof max_retries !== "number" ||
    !Number.isInteger(max_retries) ||
    max_retries < 1 ||
    max_retries > 5
  )
    return fail("max_retries must be an integer from 1 to 5");
  if (
    typeof delay_seconds !== "number" ||
    !Number.isInteger(delay_seconds) ||
    delay_seconds < 0 ||
    delay_seconds > 3600
  )
    return fail("delay_seconds must be an integer from 0 to 3600");
  if (webhook_on_retry !== "final_only" && webhook_on_retry !== "every_attempt")
    return fail("invalid webhook_on_retry");
  if (!Array.isArray(retry_on) || !retry_on.length)
    return fail("retry_on must be a non-empty list");
  const statuses = new Set<string>();
  const retryOn = retry_on.map((rule: unknown, index) => {
    if (!isPlainMapping(rule)) return fail("retry_on entries must be mappings");
    for (const key of Object.keys(rule)) {
      if (key !== "status" && key !== "error_codes") {
        throw new YamlCommitError(
          `retry_policy.retry_on[${index}].${key}`,
          "unknown setting",
        );
      }
    }
    const { status, error_codes } = rule;
    if (
      typeof status !== "string" ||
      !["completed", "failed", "terminated", "canceled", "timed_out"].includes(
        status,
      )
    )
      return fail("invalid retry_on status");
    if (statuses.has(status)) return fail("retry_on statuses must be unique");
    statuses.add(status);
    if (
      error_codes !== undefined &&
      error_codes !== null &&
      (!Array.isArray(error_codes) ||
        error_codes.some((code) => typeof code !== "string"))
    )
      return fail("error_codes must be a list of strings or null");
    if (Array.isArray(error_codes)) {
      error_codes.forEach((code, codeIndex) => {
        if (code === "") {
          throw new YamlCommitError(
            `retry_policy.rules[${index}].error_codes[${codeIndex}]`,
            "must contain at least one character",
          );
        }
      });
    }
    return {
      status: status as WorkflowRetryStatus,
      ...(error_codes === undefined
        ? {}
        : {
            error_codes:
              error_codes === null
                ? null
                : [...new Set(error_codes as string[])],
          }),
    };
  });
  return { max_retries, delay_seconds, webhook_on_retry, retry_on: retryOn };
}
