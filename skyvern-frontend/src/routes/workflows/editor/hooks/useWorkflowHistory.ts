import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { Edge } from "@xyflow/react";
import { usePostHog } from "posthog-js/react";

import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { captureRecordBrowserUndoAfterRecordingIfRecent } from "@/util/recordBrowserTelemetry";
import type { AppNode } from "../nodes";
import { isDndDragInFlight } from "../sortable/dndDragActivity";
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
  /**
   * Coalesce the next post-commit capture into a single user-edit entry,
   * bypassing the debounce. Call this from within the same event handler
   * that issues an atomic multi-step mutation (e.g. a drop reorder that
   * rewires edges, re-layouts, and flips hasChanges) so the composite
   * mutation lands as ONE undo step instead of a debounced tail that
   * could merge with later edits or be preceded by intermediate frames.
   *
   * Any edit still sitting in the debounce window is flushed first so a
   * pre-drop pending edit becomes its own history entry rather than
   * being coalesced into the reorder snapshot.
   */
  captureImmediately: () => void;
  canUndo: boolean;
  canRedo: boolean;
  /**
   * Increments every time a snapshot is applied (undo/redo). Consumers
   * pass this to `FlowRenderer` so the canvas can force a `doLayout` pass
   * after restoration — without it, expand/collapse state can land at
   * stale positions (e.g. children rendered against a closed-loop layout)
   * because the restored `measured` is stripped and the dimension-change
   * re-layout path can race the new render.
   */
  historyApplyTrigger: number;
};

/**
 * In-memory undo/redo for the workflow editor canvas.
 *
 * Watches `nodes`/`edges`, captures debounced snapshots onto a bounded
 * history stack, and exposes undo/redo callbacks that restore snapshots
 * via `setNodes`/`setEdges`. Not persisted - a browser refresh clears
 * the stack.
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
  // Raised by captureImmediately(); consumed on the next capture-effect
  // fire to force a synchronous user-edit push for atomic composite
  // mutations like a reorder drop.
  const immediateCaptureRequestedRef = useRef(false);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latestNodesRef = useRef(nodes);
  const latestEdgesRef = useRef(edges);

  const [flags, setFlags] = useState<{ canUndo: boolean; canRedo: boolean }>({
    canUndo: false,
    canRedo: false,
  });
  const [historyApplyTrigger, setHistoryApplyTrigger] = useState(0);

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

  // Caller contract: invoke this from within the same event handler that
  // dispatches the atomic mutation (setNodes + setEdges + setHasChanges,
  // all synchronously enqueued). The pending debounced edit — if any — is
  // flushed first so it becomes its own history entry rather than being
  // absorbed into the composite snapshot. The flag is then consumed by the
  // capture effect on the NEXT commit so the post-mutation state lands as
  // a single push without waiting on the 300 ms debounce.
  const captureImmediately = useCallback(() => {
    flushPendingCapture();
    immediateCaptureRequestedRef.current = true;
  }, [flushPendingCapture]);

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

    // Immediate-capture path: a composite mutation committed
    // atomically from an event handler (e.g. a reorder drop that rewires
    // edges + re-layouts + flips hasChanges) should land as ONE user-edit
    // entry without waiting for the debounce to settle. Bypassing the
    // debounce also prevents the reorder from merging with subsequent
    // keystrokes that happen to fall inside the 300ms window.
    if (immediateCaptureRequestedRef.current) {
      immediateCaptureRequestedRef.current = false;
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
      // TODO: once a "last saved snapshot" is tracked in the
      // hook, compare against it here and only flip the flag when the
      // applied state actually diverges.
      useWorkflowHasChangesStore.getState().setHasChanges(true);
      // Bump so FlowRenderer can force a fresh doLayout pass against the
      // restored nodes. Without this, an undo across a loop/conditional
      // expand or collapse can land children at the prior container's
      // position because `measured` is stripped from snapshots and the
      // dimension-change re-layout path can race the new render.
      setHistoryApplyTrigger((n) => n + 1);
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
    // Same desync risk for dnd-kit's keyboard/pointer drag: the DragOverlay
    // holds activeDragId until onDragEnd/Cancel; popping history mid-gesture
    // strands the overlay against a restored graph.
    if (isDndDragInFlight()) return;
    // Flush first so an edit still pending in the debounce window isn't dropped.
    flushPendingCapture();
    const presentBeforeUndo = historyRef.current.present;
    const nodesBeforeUndo = presentBeforeUndo?.nodes.length ?? 0;
    const result = historyUndo(historyRef.current);
    if (result === null) return;
    historyRef.current = result.state;
    applySnapshot(result.applied.nodes, result.applied.edges);
    refreshFlags();
    captureRecordBrowserUndoAfterRecordingIfRecent(
      nodesBeforeUndo - result.applied.nodes.length,
    );
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
    if (isDndDragInFlight()) return;
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

  return useMemo(
    () => ({
      undo,
      redo,
      captureImmediately,
      canUndo: flags.canUndo,
      canRedo: flags.canRedo,
      historyApplyTrigger,
    }),
    [
      undo,
      redo,
      captureImmediately,
      flags.canUndo,
      flags.canRedo,
      historyApplyTrigger,
    ],
  );
}
