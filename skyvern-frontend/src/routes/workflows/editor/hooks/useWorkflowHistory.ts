import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { Edge } from "@xyflow/react";
import { usePostHog } from "posthog-js/react";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import type { AppNode } from "../nodes";
import {
  canRedo as historyCanRedo,
  canUndo as historyCanUndo,
  cloneSnapshot,
  createInitialHistoryState,
  MAX_HISTORY_ENTRIES,
  pushSnapshot,
  redo as historyRedo,
  replacePresent,
  snapshotsEqual,
  undo as historyUndo,
  type WorkflowHistoryState,
  type WorkflowSnapshot,
} from "./workflowHistoryState";

// Debounced so typing into a text field doesn't flood the stack with
// one entry per keystroke.
const CAPTURE_DEBOUNCE_MS = 300;

type UseWorkflowHistoryParams = {
  nodes: AppNode[];
  edges: Edge[];
  // Match React Flow's useNodesState / useEdgesState signatures so
  // callers can pass the setter dispatcher straight through (and use
  // the updater-function form elsewhere if they want).
  setNodes: Dispatch<SetStateAction<AppNode[]>>;
  setEdges: Dispatch<SetStateAction<Edge[]>>;
};

type UseWorkflowHistoryResult = {
  undo: () => void;
  redo: () => void;
  canUndo: boolean;
  canRedo: boolean;
};

/**
 * In-memory undo/redo for the workflow editor canvas.
 *
 * Watches `nodes`/`edges`, captures debounced snapshots onto a bounded
 * history stack, and exposes undo/redo callbacks that restore snapshots
 * via `setNodes`/`setEdges`. Not persisted - a browser refresh clears
 * the stack. See SKY-8869.
 */
export function useWorkflowHistory({
  nodes,
  edges,
  setNodes,
  setEdges,
}: UseWorkflowHistoryParams): UseWorkflowHistoryResult {
  const posthog = usePostHog();
  const historyRef = useRef<WorkflowHistoryState>(createInitialHistoryState());
  // One-shot: applySnapshot sets, capture effect consumes on next fire.
  const isApplyingRef = useRef(false);
  // Raised while internalUpdateCount > 0; consumed by the sync flush path.
  const wasInternalUpdateRef = useRef(false);
  // Set via the zustand subscribe below on any 0→1+ internalUpdateCount
  // transition. Lets us detect internal updates even when begin/end are
  // batched into a single React commit (so the capture effect wouldn't
  // otherwise observe count > 0 in isolation).
  const internalUpdateObservedRef = useRef(false);
  // Eagerly-cloned snapshot of the pre-internal-update state. Populated
  // by the subscribe callback *before* React commits the internal
  // update's nodes/edges changes, so a pending user edit that predates
  // the internal update can be pushed as its own history entry instead
  // of being merged with the internal-update state.
  const pendingSnapshotRef = useRef<WorkflowSnapshot | null>(null);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latestNodesRef = useRef(nodes);
  const latestEdgesRef = useRef(edges);

  const [flags, setFlags] = useState<{ canUndo: boolean; canRedo: boolean }>({
    canUndo: false,
    canRedo: false,
  });

  const internalUpdateCount = useWorkflowHasChangesStore(
    (state) => state.internalUpdateCount,
  );

  // useLayoutEffect with no dep array: runs synchronously after every
  // commit so a user-triggered flush (undo keydown) always sees the
  // latest values, not a stale snapshot between commit and the next
  // regular useEffect fire.
  useLayoutEffect(() => {
    latestNodesRef.current = nodes;
    latestEdgesRef.current = edges;
  });

  const refreshFlags = useCallback(() => {
    const state = historyRef.current;
    const nextCanUndo = historyCanUndo(state);
    const nextCanRedo = historyCanRedo(state);
    setFlags((prev) =>
      prev.canUndo === nextCanUndo && prev.canRedo === nextCanRedo
        ? prev
        : { canUndo: nextCanUndo, canRedo: nextCanRedo },
    );
  }, []);

  // Commit a specific snapshot into history. Routing logic:
  //   - No baseline yet → seed.
  //   - Snapshot equal to present → no-op (clear internal-update flag).
  //   - `hasChanges === false` → drift baseline (pre-first-edit gate).
  //   - Internal update in progress → drift baseline (caller contract).
  //   - Otherwise → push as an undoable entry.
  //
  // `forceAsUserEdit` bypasses the internal-update drift gate. It's used
  // when flushing a pending snapshot we know predates the internal update
  // (because it was cloned before the begin/end subscribe fired).
  const commitSnapshot = useCallback(
    (snapshot: WorkflowSnapshot, options?: { forceAsUserEdit?: boolean }) => {
      const current = historyRef.current;

      if (current.present === null) {
        historyRef.current = { past: [], present: snapshot, future: [] };
        refreshFlags();
        return;
      }

      if (snapshotsEqual(current.present, snapshot)) {
        wasInternalUpdateRef.current = false;
        return;
      }

      const storeState = useWorkflowHasChangesStore.getState();

      if (!storeState.hasChanges) {
        historyRef.current = replacePresent(current, snapshot);
        wasInternalUpdateRef.current = false;
        refreshFlags();
        return;
      }

      if (
        !options?.forceAsUserEdit &&
        (wasInternalUpdateRef.current || storeState.internalUpdateCount > 0)
      ) {
        historyRef.current = replacePresent(current, snapshot);
        wasInternalUpdateRef.current = false;
        refreshFlags();
        return;
      }

      historyRef.current = pushSnapshot(current, snapshot);
      refreshFlags();
    },
    [refreshFlags],
  );

  // Convenience wrapper: commit the CURRENT nodes/edges (via
  // latestNodesRef). Used by the normal debounce path and the
  // internal-update-exit sync flush.
  const captureIfChanged = useCallback(
    (options?: { forceAsUserEdit?: boolean }) => {
      const snapshot = cloneSnapshot(
        latestNodesRef.current,
        latestEdgesRef.current,
      );
      commitSnapshot(snapshot, options);
    },
    [commitSnapshot],
  );

  // Seed the baseline synchronously so a user who edits within the
  // 300ms debounce window has a present to undo back to.
  useEffect(() => {
    if (historyRef.current.present === null) {
      historyRef.current = {
        past: [],
        present: cloneSnapshot(latestNodesRef.current, latestEdgesRef.current),
        future: [],
      };
      refreshFlags();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Detect internal-update transitions via a direct zustand subscribe,
  // independent of React's render/batch cycle. Fires synchronously
  // inside `beginInternalUpdate()`'s setState stack - critically, BEFORE
  // the handler goes on to modify nodes/edges.
  //
  // INVARIANT: latestNodesRef must reflect the latest user-committed
  // state when this callback fires. It is maintained by a bare
  // useLayoutEffect (no dep array) which runs synchronously after every
  // React commit. This means every user edit that changes nodes/edges
  // MUST have been committed (rendered + layout-effected) before any
  // code path calls beginInternalUpdate(). If React 18 concurrent
  // features (startTransition, useDeferredValue) defer a user-edit
  // commit past a synchronous beginInternalUpdate call, the ref will
  // hold a stale value and the cloned pending snapshot will predate the
  // user's edit. All current callers of beginInternalUpdate run in
  // useEffect or event handlers (synchronous commit boundaries), so
  // this invariant holds today.
  useEffect(() => {
    return useWorkflowHasChangesStore.subscribe((state, prevState) => {
      if (state.internalUpdateCount > prevState.internalUpdateCount) {
        internalUpdateObservedRef.current = true;
        if (pendingSnapshotRef.current === null) {
          pendingSnapshotRef.current = cloneSnapshot(
            latestNodesRef.current,
            latestEdgesRef.current,
          );
        }
      }
    });
  }, []);

  const flushPendingCapture = useCallback(() => {
    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }
    if (isApplyingRef.current) return;
    if (useWorkflowHasChangesStore.getState().internalUpdateCount > 0) return;
    captureIfChanged();
  }, [captureIfChanged]);

  useEffect(() => {
    if (isApplyingRef.current) {
      isApplyingRef.current = false;
      return;
    }

    // An internal update was seen since the last capture-effect fire -
    // possibly merged into this same commit via React batching. Push
    // the pre-internal-update pending snapshot (captured eagerly by
    // the subscribe) as a proper user-edit entry before handling the
    // current state.
    if (internalUpdateObservedRef.current) {
      internalUpdateObservedRef.current = false;
      const pending = pendingSnapshotRef.current;
      pendingSnapshotRef.current = null;
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      if (pending !== null) {
        commitSnapshot(pending, { forceAsUserEdit: true });
      }
      wasInternalUpdateRef.current = true;
      // Fall through: if count is now 0, the internal-update exit
      // branch below will sync-flush the drift.
    }

    if (internalUpdateCount > 0) {
      wasInternalUpdateRef.current = true;
      return;
    }

    // Exiting an internal update: flush synchronously so the drift
    // lands before a subsequent user edit can coalesce into it.
    if (wasInternalUpdateRef.current) {
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      captureIfChanged();
      return;
    }

    if (debounceTimerRef.current !== null) {
      clearTimeout(debounceTimerRef.current);
    }
    debounceTimerRef.current = setTimeout(() => {
      debounceTimerRef.current = null;
      if (isApplyingRef.current) return;
      captureIfChanged();
    }, CAPTURE_DEBOUNCE_MS);

    return () => {
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
    };
  }, [nodes, edges, internalUpdateCount, captureIfChanged, commitSnapshot]);

  const applySnapshot = useCallback(
    (snapshotNodes: AppNode[], snapshotEdges: Edge[]) => {
      // Fresh copies - downstream mutates node.data in place.
      const cloned = cloneSnapshot(snapshotNodes, snapshotEdges);
      isApplyingRef.current = true;
      if (debounceTimerRef.current !== null) {
        clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      setNodes(cloned.nodes);
      setEdges(cloned.edges);
      // Undo/redo across a save boundary diverges the canvas from the
      // persisted state - mark dirty so the leave-page warning fires
      // and the save button activates. Worst case is a false positive
      // when the user undoes back exactly to the last-saved state, but
      // silently losing divergence is the bigger failure.
      //
      // TODO(SKY-8869): once a "last saved snapshot" is tracked in the
      // hook, compare against it here and only flip the flag when the
      // applied state actually diverges.
      useWorkflowHasChangesStore.getState().setHasChanges(true);
    },
    [setNodes, setEdges],
  );

  const undo = useCallback(() => {
    // History navigation is unsafe while an internal update is in
    // flight: the subscribe path has already queued a pending
    // pre-internal snapshot and an observed flag, and consuming them
    // after an undo would rewrite history behind the user's back
    // (clearing redo, pushing a stale entry onto past). Bail and let
    // the internal update finish first.
    if (useWorkflowHasChangesStore.getState().internalUpdateCount > 0) return;
    // Bail if any node is mid-drag. Cmd/Ctrl+Z during a drag gesture
    // would pop a snapshot while React Flow's drag controller still
    // holds a reference to the dragging node, causing a desync between
    // RF's internal drag state and the restored snapshot.
    if (latestNodesRef.current.some((n) => n.dragging)) return;
    // Flush first so an edit still pending in the debounce window isn't dropped.
    flushPendingCapture();
    const result = historyUndo(historyRef.current);
    if (result === null) return;
    historyRef.current = result.state;
    applySnapshot(result.applied.nodes, result.applied.edges);
    refreshFlags();
    posthog.capture("builder.undo_redo.used", {
      action: "undo",
      node_count: result.applied.nodes.length,
      edge_count: result.applied.edges.length,
      history_depth: result.state.past.length,
      cap_reached: result.state.past.length >= MAX_HISTORY_ENTRIES,
    });
  }, [applySnapshot, flushPendingCapture, refreshFlags, posthog]);

  const redo = useCallback(() => {
    if (useWorkflowHasChangesStore.getState().internalUpdateCount > 0) return;
    if (latestNodesRef.current.some((n) => n.dragging)) return;
    flushPendingCapture();
    const result = historyRedo(historyRef.current);
    if (result === null) return;
    historyRef.current = result.state;
    applySnapshot(result.applied.nodes, result.applied.edges);
    refreshFlags();
    posthog.capture("builder.undo_redo.used", {
      action: "redo",
      node_count: result.applied.nodes.length,
      edge_count: result.applied.edges.length,
      history_depth: result.state.past.length,
      cap_reached: result.state.past.length >= MAX_HISTORY_ENTRIES,
    });
  }, [applySnapshot, flushPendingCapture, refreshFlags, posthog]);

  return {
    undo,
    redo,
    canUndo: flags.canUndo,
    canRedo: flags.canRedo,
  };
}
