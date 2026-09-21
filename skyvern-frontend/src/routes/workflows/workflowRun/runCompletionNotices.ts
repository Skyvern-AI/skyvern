/**
 * useRunCompletionToast owns final-state notices on every surface and selects
 * block-specific copy before claiming. Cancellation handlers claim their own
 * confirmation. No caller may mark a run separately from displaying its notice.
 * Claims refresh recency; the last 500 run IDs survive remounts and navigation.
 */
const MAX_NOTIFIED_RUN_IDS = 500;
const notifiedRunIds = new Set<string>();

export function claimRunCompletionNotice(
  runId: string,
  showNotice: () => void,
): boolean {
  if (notifiedRunIds.has(runId)) {
    notifiedRunIds.delete(runId);
    notifiedRunIds.add(runId);
    return false;
  }
  showNotice();
  notifiedRunIds.add(runId);
  if (notifiedRunIds.size > MAX_NOTIFIED_RUN_IDS) {
    const oldestRunId = notifiedRunIds.values().next().value;
    if (oldestRunId !== undefined) notifiedRunIds.delete(oldestRunId);
  }
  return true;
}
