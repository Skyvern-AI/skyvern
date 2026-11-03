// Pure helpers for the studio Editor pane's block search: collecting the
// searchable targets from the canvas nodes (hidden-branch blocks included),
// the label matcher, and the jump — conditional-branch resolution plus the
// focus sequence — expressed against injectable seams so it unit-tests
// without a live React Flow instance.

import {
  type AppNode,
  isWorkflowBlockNode,
} from "@/routes/workflows/editor/nodes";
import { blockTypeFromNode } from "@/routes/workflows/editor/nodes/blockTypeFromNode";
import { isConditionalNode } from "@/routes/workflows/editor/nodes/ConditionalNode/types";
import {
  START_ANCHOR_MAX_ZOOM,
  START_ANCHOR_TOP_PX,
} from "@/routes/workflows/editor/paneFit";
import { type WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";

export type BlockSearchTarget = {
  nodeId: string;
  label: string;
  // Null when the node type has no block-type mapping (see blockTypeFromNode);
  // consumers skip the icon rather than guess one.
  blockType: WorkflowBlockType | null;
};

export function collectBlockSearchTargets(
  nodes: Array<AppNode>,
): Array<BlockSearchTarget> {
  return nodes.filter(isWorkflowBlockNode).flatMap((node) => {
    if (node.data.label.trim() === "") {
      return [];
    }
    return [
      {
        nodeId: node.id,
        label: node.data.label,
        blockType: blockTypeFromNode(node) as WorkflowBlockType | null,
      },
    ];
  });
}

// Case-insensitive substring match over block labels; an empty query keeps
// every target so the open popover lists the whole flow in canvas order.
export function filterBlockSearchTargets(
  targets: Array<BlockSearchTarget>,
  query: string,
): Array<BlockSearchTarget> {
  const needle = query.trim().toLowerCase();
  if (needle === "") {
    return targets;
  }
  return targets.filter((target) =>
    target.label.toLowerCase().includes(needle),
  );
}

export const BLOCK_JUMP_DURATION_MS = 300;

export function blockJumpDuration(): number {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return 0;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? 0
    : BLOCK_JUMP_DURATION_MS;
}

// Structural slice of React Flow's InternalNode: positionAbsolute is the
// canvas-space position even for blocks nested inside loop containers, whose
// own `position` is parent-relative.
type InternalNodeLike = {
  internals: { positionAbsolute: { x: number; y: number } };
  measured?: { width?: number; height?: number };
};

export type ConditionalBranchSwitch = {
  conditionalId: string;
  conditionalLabel: string;
  branchId: string;
};

/**
 * Conditional levels (root→leaf) whose active branch must change before
 * `targetNodeId` can be visible. Nodes in a branch chain carry the
 * (conditionalNodeId, conditionalBranchId) affinity; blocks nested deeper
 * (e.g. inside a loop within a branch) reach the chain via parentId.
 */
export function resolveConditionalBranchPath(
  nodes: Array<AppNode>,
  targetNodeId: string,
): Array<ConditionalBranchSwitch> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const path: Array<ConditionalBranchSwitch> = [];
  const visited = new Set<string>();
  let current = byId.get(targetNodeId);
  while (current && !visited.has(current.id)) {
    visited.add(current.id);
    const conditionalId = isWorkflowBlockNode(current)
      ? (current.data.conditionalNodeId ?? null)
      : null;
    if (conditionalId !== null) {
      const conditional = byId.get(conditionalId);
      if (!conditional || !isConditionalNode(conditional)) {
        break;
      }
      const branchId = isWorkflowBlockNode(current)
        ? (current.data.conditionalBranchId ?? null)
        : null;
      if (branchId !== null && conditional.data.activeBranchId !== branchId) {
        path.push({
          conditionalId: conditional.id,
          conditionalLabel: conditional.data.label,
          branchId,
        });
      }
      current = conditional;
      continue;
    }
    current = current.parentId ? byId.get(current.parentId) : undefined;
  }
  return path.reverse();
}

export const BRANCH_SETTLE_STABLE_FRAMES = 3;
export const BRANCH_SETTLE_TIMEOUT_MS = 2000;

type NodeSettleDeps = {
  getNodes: () => Array<AppNode>;
  getInternalNode: (nodeId: string) => InternalNodeLike | undefined;
  requestFrame?: (callback: () => void) => void;
  now?: () => number;
  // True while a collapse-triggered Dagre relayout is scheduled or in
  // flight (e.g. use-debounce's `.pending()`). A batched ancestor expand
  // fires that relayout asynchronously, so position can look unchanged in
  // the dead window before it lands — stability alone would resolve on
  // stale geometry. Defaults to "never pending" for callers (branch switch)
  // whose relayout isn't debounced through that path.
  isRelayoutPending?: () => boolean;
};

/**
 * Resolves once `nodeId` is visible, no collapse-triggered relayout is
 * pending, and its canvas position has held still for a few consecutive
 * frames. A branch switch cascades visibility (the BranchesEditor effect)
 * and then a full re-layout (Workspace's conditional-branch-changed
 * listener) on staggered timeouts, so positions read before this settles
 * are stale. Times out rather than hanging when the node never becomes
 * visible.
 */
export function waitForNodeSettle(
  nodeId: string,
  deps: NodeSettleDeps,
): Promise<void> {
  const requestFrame =
    deps.requestFrame ??
    ((callback) => window.requestAnimationFrame(() => callback()));
  const now = deps.now ?? (() => performance.now());
  const isRelayoutPending = deps.isRelayoutPending ?? (() => false);
  const startedAt = now();
  let lastX: number | null = null;
  let lastY: number | null = null;
  let stableFrames = 0;
  return new Promise((resolve) => {
    const step = () => {
      const node = deps.getNodes().find((candidate) => candidate.id === nodeId);
      const position =
        deps.getInternalNode(nodeId)?.internals.positionAbsolute ??
        node?.position ??
        null;
      const visible = node !== undefined && node.hidden !== true;
      if (
        visible &&
        position !== null &&
        position.x === lastX &&
        position.y === lastY &&
        !isRelayoutPending()
      ) {
        stableFrames += 1;
      } else {
        stableFrames = 0;
      }
      lastX = position?.x ?? null;
      lastY = position?.y ?? null;
      if (
        (visible && stableFrames >= BRANCH_SETTLE_STABLE_FRAMES) ||
        now() - startedAt >= BRANCH_SETTLE_TIMEOUT_MS
      ) {
        resolve();
        return;
      }
      requestFrame(step);
    };
    requestFrame(step);
  });
}

// "top-anchored" pans at the caller's zoom and parks the block just below the
// pane top — right for the studio, whose search palette sits outside the canvas.
// "centered" centers the block in the pane and zooms in, which is what Locate
// needs on the legacy canvas: its header floats *over* the canvas, so a
// top-anchored target lands underneath it.
export type FocusFraming = "top-anchored" | "centered";

export type FocusBlockDeps = {
  getNodes: () => Array<AppNode>;
  getInternalNode: (nodeId: string) => InternalNodeLike | undefined;
  getPaneWidth: () => number;
  // Required for "centered"; a pane of unknown height falls back to top-anchored.
  getPaneHeight?: () => number;
  // Pixels of the pane's top edge covered by chrome floating over the canvas.
  // Measured per call, never assumed: the legacy header collapses, expands, and
  // animates between the two, so a fixed offset would be wrong most of the time.
  getPaneTopInset?: () => number;
  framing?: FocusFraming;
  viewportZoom: number;
  duration: number;
  setViewport: (
    viewport: { x: number; y: number; zoom: number },
    options: { duration: number },
  ) => void;
  selectBlock: (nodeId: string) => void;
  // Gives the canvas a synchronous point to arm its relayout gate before an
  // expand schedules React effects and the delayed dimension relayout.
  beforeExpand?: (label: string) => void;
  expandBlock: (label: string) => void;
  // Writes activeBranchId into the conditional's node data — the same write
  // the branch tab click makes (the binding supplies the dirty-state guards).
  switchBranch: (conditionalId: string, branchId: string) => void;
  waitForSettle: (nodeId: string) => Promise<void>;
};

// Centers the block in the pane's *visible* band — the pane minus whatever
// chrome currently floats over its top — and zooms in to a readable 1:1.
// Only ever zooms in, so locating from an already-close view doesn't yank the
// canvas backwards. Returns null when the pane height is unknown, leaving the
// caller to fall back to the top anchor.
function centeredViewport(
  node: AppNode,
  deps: FocusBlockDeps,
  position: { x: number; y: number },
  width: number,
): { x: number; y: number; zoom: number } | null {
  const paneHeight = deps.getPaneHeight?.() ?? 0;
  if (paneHeight <= 0) {
    return null;
  }
  const internal = deps.getInternalNode(node.id);
  const height = internal?.measured?.height ?? node.measured?.height ?? 0;
  const topInset = Math.min(
    Math.max(deps.getPaneTopInset?.() ?? 0, 0),
    paneHeight,
  );
  const zoom = Math.max(deps.viewportZoom, START_ANCHOR_MAX_ZOOM);
  const bandCenterY = topInset + (paneHeight - topInset) / 2;
  return {
    x: deps.getPaneWidth() / 2 - (position.x + width / 2) * zoom,
    y: bandCenterY - (position.y + height / 2) * zoom,
    zoom,
  };
}

// Horizontally centers the block but anchors its TOP edge at the
// start-anchored-fit offset below the pane top, at the current zoom: a tall
// container lands on its header instead of vertically centering on its
// unrelated inner children.
function anchorViewportToNode(
  node: AppNode,
  deps: FocusBlockDeps,
  framing: FocusFraming = "top-anchored",
): void {
  const internal = deps.getInternalNode(node.id);
  const position = internal?.internals.positionAbsolute ?? node.position;
  const width = internal?.measured?.width ?? node.measured?.width ?? 0;
  const zoom = deps.viewportZoom;
  const centered =
    framing === "centered"
      ? centeredViewport(node, deps, position, width)
      : null;
  deps.setViewport(
    centered ?? {
      x: deps.getPaneWidth() / 2 - (position.x + width / 2) * zoom,
      y: START_ANCHOR_TOP_PX - position.y * zoom,
      zoom,
    },
    { duration: deps.duration },
  );
}

/**
 * Returns the labels of loop/conditional container ancestors of `targetNodeId`,
 * ordered root→leaf (outermost first), so callers can expand them in order.
 * Only `loop` and `conditional` RF node types are containers; other parents
 * (e.g. a block's own sub-group nodes) are skipped without being collected.
 */
export function resolveContainerAncestorLabels(
  nodes: Array<AppNode>,
  targetNodeId: string,
): Array<string> {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const labels: Array<string> = [];
  const visited = new Set<string>();
  let current = byId.get(targetNodeId);
  while (current && current.parentId && !visited.has(current.id)) {
    visited.add(current.id);
    const parent = byId.get(current.parentId);
    if (!parent) break;
    if (parent.type === "loop" || parent.type === "conditional") {
      const label = isWorkflowBlockNode(parent) ? parent.data.label : null;
      if (label) {
        labels.unshift(label);
      }
    }
    current = parent;
  }
  return labels;
}

/**
 * Latest-wins guard for concurrent focus/locate runs. Each call bumps the
 * shared counter and returns a predicate that stays true only until the next
 * call, so a superseded run can turn its pan / selection / branch writes into
 * no-ops instead of racing a later Locate to a stale block. `focusBlockTarget`
 * awaits re-layout, so a nested reveal can resolve after a quicker later one —
 * the guard, not resolution order, decides which viewport wins.
 */
export function beginFocusGeneration(counter: {
  current: number;
}): () => boolean {
  const generation = (counter.current += 1);
  return () => generation === counter.current;
}

export function invalidateFocusGeneration(counter: { current: number }): void {
  counter.current += 1;
}

/**
 * Selects the block (which the selected-block URL sync mirrors into
 * `?selected-block=`), reveals its inline editor, and top-anchors the
 * viewport on it at the current zoom. A target hidden inside inactive
 * conditional branches is first revealed level by level (root→leaf): focus
 * the conditional, switch it to the target's branch, wait for the visibility
 * cascade + re-layout to settle, then continue. If the target is still hidden
 * after branch resolution (collapsed loop/conditional ancestor), every
 * container ancestor is expanded and a second settle is awaited before
 * anchoring. Returns false when the node no longer exists or is not a workflow
 * block.
 */
export async function focusBlockTarget(
  nodeId: string,
  deps: FocusBlockDeps,
): Promise<boolean> {
  const expandContainer = (label: string) => {
    deps.beforeExpand?.(label);
    deps.expandBlock(label);
  };
  const findNode = (id: string) =>
    deps.getNodes().find((candidate) => candidate.id === id);
  const target = findNode(nodeId);
  if (!target || !isWorkflowBlockNode(target)) {
    return false;
  }

  const branchPath = resolveConditionalBranchPath(deps.getNodes(), nodeId);
  for (const { conditionalId, conditionalLabel, branchId } of branchPath) {
    const conditional = findNode(conditionalId);
    if (conditional) {
      deps.selectBlock(conditionalId);
      // A collapsed conditional renders header-only without its BranchesEditor,
      // whose mounted effect is what applies the branch visibility switch.
      expandContainer(conditionalLabel);
      anchorViewportToNode(conditional, deps);
    }
    deps.switchBranch(conditionalId, branchId);
    await deps.waitForSettle(nodeId);
  }

  const afterBranches = findNode(nodeId);
  if (!afterBranches || !isWorkflowBlockNode(afterBranches)) {
    return false;
  }

  if (afterBranches.hidden) {
    const ancestorLabels = resolveContainerAncestorLabels(
      deps.getNodes(),
      nodeId,
    );
    if (ancestorLabels.length > 0) {
      for (const label of ancestorLabels) {
        expandContainer(label);
      }
      await deps.waitForSettle(nodeId);
    }
  }

  const settled = findNode(nodeId);
  if (!settled || !isWorkflowBlockNode(settled)) {
    return false;
  }
  deps.selectBlock(nodeId);
  deps.expandBlock(settled.data.label);
  // Centering divides by the block's height, so it must be measured after the
  // expansion above commits — measuring in the same tick centers the collapsed
  // box and leaves an expanded block hanging below the middle. The top anchor
  // reads only position and width, so it is unaffected and skips the wait.
  if (deps.framing === "centered") {
    await deps.waitForSettle(nodeId);
  }
  // Only the final landing honours the caller's framing; the intermediate
  // anchors above are mid-reveal waypoints on container ancestors.
  anchorViewportToNode(findNode(nodeId) ?? settled, deps, deps.framing);
  return true;
}
