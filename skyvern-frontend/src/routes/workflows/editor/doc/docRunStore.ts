import { create } from "zustand";

import type { WorkflowParameterValueType } from "../../types/workflowTypes";

export type DocRunInput = {
  key: string;
  type: WorkflowParameterValueType;
  defaultValue: unknown;
  required: boolean;
};

const STORAGE_PREFIX = "skyvern.docRun.";
const DISMISSED_PREFIX = "skyvern.docRunCleared.";
// An undefined organization is a single-organization (open source) install, as the query keys treat it.
const storageKey = (orgId: string | undefined, workflowPermanentId: string) =>
  `${STORAGE_PREFIX}${orgId ?? "local"}:${workflowPermanentId}`;

const dismissedKey = (orgId: string | undefined, workflowPermanentId: string) =>
  `${DISMISSED_PREFIX}${orgId ?? "local"}:${workflowPermanentId}`;

function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, runId: string | null) {
  try {
    if (runId) window.localStorage.setItem(key, runId);
    else window.localStorage.removeItem(key);
  } catch {
    // Private windows and blocked storage: the run still shows for this visit.
  }
}

type DocRunStore = {
  // Published by the mounted Doc editor; the top-bar Run reads them.
  inputs: DocRunInput[];
  stepCount: number;
  // Whether a Doc editor is on screen; the top bar's Run belongs to it only then.
  mounted: boolean;
  publish: (
    state: Pick<DocRunStore, "inputs" | "stepCount" | "mounted">,
  ) => void;
  // The Doc's latest run per organization and agent, so results survive reopening the Doc.
  runIds: Record<string, string | null>;
  docRunId: (
    orgId: string | undefined,
    workflowPermanentId: string,
  ) => string | null;
  setDocRunId: (
    orgId: string | undefined,
    workflowPermanentId: string,
    runId: string | null,
  ) => void;
  // The run this browser cleared; a newer run is not it, so it shows again.
  dismissed: Record<string, string | null>;
  clearDocRun: (
    orgId: string | undefined,
    workflowPermanentId: string,
    runId: string | undefined,
  ) => void;
};

export const useDocRunStore = create<DocRunStore>((set, get) => ({
  inputs: [],
  stepCount: 0,
  mounted: false,
  publish: (state) => set(state),
  runIds: {},
  docRunId: (orgId, workflowPermanentId) => {
    const key = storageKey(orgId, workflowPermanentId);
    const known = get().runIds[key];
    return known !== undefined ? known : readStored(key);
  },
  setDocRunId: (orgId, workflowPermanentId, runId) => {
    const key = storageKey(orgId, workflowPermanentId);
    writeStored(key, runId);
    set((state) => ({ runIds: { ...state.runIds, [key]: runId } }));
  },
  dismissed: {},
  clearDocRun: (orgId, workflowPermanentId, runId) => {
    get().setDocRunId(orgId, workflowPermanentId, null);
    if (!runId) return;
    const key = dismissedKey(orgId, workflowPermanentId);
    writeStored(key, runId);
    set((state) => ({ dismissed: { ...state.dismissed, [key]: runId } }));
  },
}));

/** The run this browser cleared for this agent, or null. */
export function useDismissedDocRunId(
  orgId: string | undefined,
  workflowPermanentId: string | undefined,
): string | null {
  return useDocRunStore((state) => {
    if (!workflowPermanentId) return null;
    const key = dismissedKey(orgId, workflowPermanentId);
    const known = state.dismissed[key];
    return known !== undefined ? known : readStored(key);
  });
}

/** The Doc's run for this agent in this organization, or null. Re-renders when it changes. */
export function useStoredDocRunId(
  orgId: string | undefined,
  workflowPermanentId: string | undefined,
): string | null {
  return useDocRunStore((state) => {
    if (!workflowPermanentId) return null;
    const key = storageKey(orgId, workflowPermanentId);
    const known = state.runIds[key];
    return known !== undefined ? known : readStored(key);
  });
}

const stepRunKey = (runId: string) => `skyvern.docStepRun.${runId}`;

/** A one-step (Play) run started in this browser; the Doc does not treat it as the agent's latest run. */
export function markStepRun(runId: string) {
  writeStored(stepRunKey(runId), "1");
}

export function isStepRun(runId: string): boolean {
  return readStored(stepRunKey(runId)) !== null;
}
