import { stepShowsEarlierRun } from "./docRunState";
import { useDocRun } from "./useDocRun";

/** The run a step reads: after a one-step run, a step outside it reads the latest full run. */
export function docRunFor(docRun: ReturnType<typeof useDocRun>, label: string) {
  // The earlier layer overrides owned, live, blockRun and rowsLoaded on purpose; older is the only new field.
  return stepShowsEarlierRun(docRun, label) && docRun.previous
    ? { ...docRun, ...docRun.previous, older: true }
    : { ...docRun, older: false };
}

export function useDocRunFor(label: string) {
  return docRunFor(useDocRun(), label);
}
