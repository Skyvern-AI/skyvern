import type {
  Status,
  WorkflowRunAttempt,
  WorkflowRunRetryFields,
} from "@/api/types";
import {
  statusIsCancellable,
  statusIsFinalized,
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import type { WorkflowRunTimelineItem } from "../types/workflowRunTypes";
import { normalizeUtcTimestamp } from "@/util/timeFormat";

type RunLike = { status: Status } & WorkflowRunRetryFields;

export function getRunAttempt(run: WorkflowRunRetryFields): number {
  return run.attempt ?? 1;
}
export function runIsLogicallyFinal(run: RunLike): boolean {
  return statusIsFinalized(run) && !run.retry_pending;
}
export function runIsLogicallyActive(run: RunLike): boolean {
  return statusIsNotFinalized(run) || !!run.retry_pending;
}
export function runIsRetryWaiting(run: RunLike): boolean {
  return statusIsFinalized(run) && !!run.retry_pending;
}
/**
 * Queued and running attempts execute. Created has not entered the execution
 * queue yet; paused, skipped, and retry-waiting runs are idle.
 */
export function runIsExecuting(run: RunLike): boolean {
  return statusIsRunningOrQueued(run) && !runIsRetryWaiting(run);
}
export function runIsCancellable(run: RunLike): boolean {
  return statusIsCancellable(run) || runIsRetryWaiting(run);
}
export function getRunAttemptKey(
  run: { workflow_run_id: string } & WorkflowRunRetryFields,
): string {
  return `${run.workflow_run_id}:${getRunAttempt(run)}`;
}
export function resolveTimelineItemAttempt(
  item: WorkflowRunTimelineItem,
  attempts: Array<WorkflowRunAttempt>,
): number {
  const explicitAttempt = item.attempt ?? item.block?.attempt_number;
  if (explicitAttempt != null && explicitAttempt > 1) return explicitAttempt;
  // Nested Task V2 items carry the child run's attempt 1, even when their
  // enclosing run is retrying. Only that ambiguous default uses parent windows.
  if (attempts.length > 1) {
    const createdAt =
      item.block?.created_at ?? item.thought?.created_at ?? item.created_at;
    const created = Date.parse(normalizeUtcTimestamp(createdAt));
    for (let index = 0; index < attempts.length; index++) {
      const attempt = attempts[index]!;
      const next = attempts[index + 1];
      const start = attempt.started_at
        ? Date.parse(normalizeUtcTimestamp(attempt.started_at))
        : NaN;
      const end = next?.started_at
        ? Date.parse(normalizeUtcTimestamp(next.started_at))
        : Infinity;
      if (created >= start && created < end) return attempt.attempt_number;
    }
  }
  return 1;
}
