import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import {
  useWorkflowScopeId,
  useWorkflowScopeReadOnly,
} from "../WorkflowScopeContext";

// Per-workflow collapse state, persisted to localStorage. Internal keys are
// `${workflowId}\x1f${blockLabel}` so two workflows with the same label never
// share collapse state. Excluded from `useWorkflowHistory` deliberately —
// collapse/expand should not land in the undo stack alongside structural
// edits.
// Semantics: a key is present iff the corresponding block is collapsed.
// Absent = open (the default). `expandAll` therefore deletes keys rather
// than setting them to `false`, which keeps the persisted set proportional
// to the number of currently-collapsed blocks instead of growing with
// every block the user has ever toggled.
type NodeCollapseState = {
  collapsed: Record<string, boolean>;
  toggleBlock: (workflowId: string, blockLabel: string) => void;
  // Idempotent expand: only removes the collapsed entry, never adds one.
  expandBlock: (workflowId: string, blockLabel: string) => void;
  collapseAll: (workflowId: string, labels: string[]) => void;
  expandAll: (workflowId: string) => void;
  // Moves a persisted entry from oldLabel to newLabel under the same
  // workflowId. No-op if oldLabel has no entry (block was already open).
  // Call this from the block-rename path so collapse state survives the
  // rename instead of being dropped by the next pruneStaleLabels pass.
  renameBlock: (workflowId: string, oldLabel: string, newLabel: string) => void;
  // Drops persisted entries under `workflowId` whose label isn't in
  // `validLabels`. Call on workflow load so renamed blocks don't accumulate
  // orphan keys across the session.
  pruneStaleLabels: (workflowId: string, validLabels: string[]) => void;
  // Drops every persisted entry under `workflowId`. Call from the workflow
  // delete path so localStorage doesn't accumulate keys for workflows that
  // no longer exist on the backend.
  pruneWorkflow: (workflowId: string) => void;
};

// ASCII 0x1F (Unit Separator) is a control character reserved exactly for
// this purpose; workflow ids and block labels can never legally contain it,
// so the segments are unambiguously split on read.
const COLLAPSE_KEY_SEPARATOR = "\x1f";

export function makeCollapseKey(workflowId: string, label: string): string {
  return `${workflowId}${COLLAPSE_KEY_SEPARATOR}${label}`;
}

// Local alias; keeps existing call sites in this module short.
const key = makeCollapseKey;

export function isBlockCollapsedAt(
  collapsed: Record<string, boolean>,
  workflowId: string,
  label: string,
): boolean {
  return Boolean(collapsed[key(workflowId, label)]);
}

export const useNodeCollapseStore = create<NodeCollapseState>()(
  persist(
    (set) => ({
      collapsed: {},
      toggleBlock: (workflowId, label) =>
        set((s) => {
          const k = key(workflowId, label);
          const next = { ...s.collapsed };
          if (next[k]) {
            delete next[k];
          } else {
            next[k] = true;
          }
          return { collapsed: next };
        }),
      expandBlock: (workflowId, label) =>
        set((s) => {
          const k = key(workflowId, label);
          if (!s.collapsed[k]) return s;
          const next = { ...s.collapsed };
          delete next[k];
          return { collapsed: next };
        }),
      collapseAll: (workflowId, labels) =>
        set((s) => {
          const next = { ...s.collapsed };
          let mutated = false;
          labels.forEach((l) => {
            const k = key(workflowId, l);
            if (!next[k]) {
              next[k] = true;
              mutated = true;
            }
          });
          return mutated ? { collapsed: next } : s;
        }),
      expandAll: (workflowId) =>
        set((s) => {
          const prefix = `${workflowId}${COLLAPSE_KEY_SEPARATOR}`;
          const next = { ...s.collapsed };
          let mutated = false;
          Object.keys(next).forEach((k) => {
            if (k.startsWith(prefix)) {
              delete next[k];
              mutated = true;
            }
          });
          return mutated ? { collapsed: next } : s;
        }),
      renameBlock: (workflowId, oldLabel, newLabel) =>
        set((s) => {
          const oldKey = key(workflowId, oldLabel);
          if (!s.collapsed[oldKey]) return s;
          const newKey = key(workflowId, newLabel);
          const next = { ...s.collapsed };
          delete next[oldKey];
          next[newKey] = true;
          return { collapsed: next };
        }),
      pruneStaleLabels: (workflowId, validLabels) =>
        set((s) => {
          const prefix = `${workflowId}${COLLAPSE_KEY_SEPARATOR}`;
          const valid = new Set(validLabels.map((l) => key(workflowId, l)));
          const next = { ...s.collapsed };
          let mutated = false;
          Object.keys(next).forEach((k) => {
            if (k.startsWith(prefix) && !valid.has(k)) {
              delete next[k];
              mutated = true;
            }
          });
          return mutated ? { collapsed: next } : s;
        }),
      pruneWorkflow: (workflowId) =>
        set((s) => {
          const prefix = `${workflowId}${COLLAPSE_KEY_SEPARATOR}`;
          const next = { ...s.collapsed };
          let mutated = false;
          Object.keys(next).forEach((k) => {
            if (k.startsWith(prefix)) {
              delete next[k];
              mutated = true;
            }
          });
          return mutated ? { collapsed: next } : s;
        }),
    }),
    {
      name: "skyvern:node-collapse",
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ collapsed: s.collapsed }),
      version: 1,
    },
  ),
);

export function useIsBlockCollapsed(label: string): boolean {
  const scopeId = useWorkflowScopeId();
  const readOnly = useWorkflowScopeReadOnly();
  // Missing-provider path: log loudly in dev so a forgotten
  // `<WorkflowScopeContext.Provider>` wrap is obvious during development,
  // but fall back to the shared `__global__` key in production rather
  // than throwing. A throw here would take down the whole React tree if a
  // node ever mounts a tick before the provider hydrates (route
  // transition, suspense boundary), and silent shared state across
  // workflows is the exact failure mode the dev-time warning is meant
  // to catch — with all editor entry points consistently providing the
  // scope today, a runtime escape would be the rarer failure mode.
  if (scopeId === null && process.env.NODE_ENV === "development") {
    console.warn(
      "[useNodeCollapseStore] WorkflowScopeContext provider missing; " +
        "falling back to '__global__'. Wrap the subtree in " +
        "<WorkflowScopeContext.Provider value={{ workflowId, readOnly }}>.",
    );
  }
  const workflowId = scopeId ?? "__global__";
  return useNodeCollapseStore((s) =>
    // Read-only canvases (workflow comparison/diff panels) intentionally
    // ignore the editor's persisted collapse state: their toggle controls
    // are disabled, so honoring a collapsed entry would hide diffs with
    // no way to reveal them.
    readOnly ? false : isBlockCollapsedAt(s.collapsed, workflowId, label),
  );
}
