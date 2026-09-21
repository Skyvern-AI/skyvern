import type { WorkflowRetryPolicy } from "@/routes/workflows/types/workflowTypes";

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
      const codes = [
        ...new Set(
          (error_codes ?? []).map((code) => code.trim()).filter(Boolean),
        ),
      ];
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
