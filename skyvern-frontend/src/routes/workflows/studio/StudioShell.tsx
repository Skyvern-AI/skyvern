import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useParams } from "react-router-dom";
import { Cross2Icon } from "@radix-ui/react-icons";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { RecordingPanel } from "@/routes/workflows/editor/recording/RecordingPanel";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { cn } from "@/util/utils";

import { deriveDropIndicator } from "../editor/sortable/dropIndicator";
import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";

import { BrowserPaneActions, BrowserPaneViewPills } from "./BrowserPaneHeader";
import { CopilotActiveDot, CopilotPaneControls } from "./CopilotPaneHeader";
import { EditorPaneModeToggle } from "./EditorPaneHeader";
import { BrowserTab } from "./BrowserTab";
import { EditorTab, type StudioWorkspaceProps } from "./EditorTab";
import { RunTab } from "./RunTab";
import { RunPaneActions, RunPaneViewToggles } from "./runview/RunPaneHeader";
import { StudioBrowserStream } from "./StudioBrowserStream";
import { StudioCoachMark } from "./StudioCoachMark";
import { studioPanelId, studioTabId } from "./constants";
import {
  clampResizeDelta,
  movePaneBy,
  movePaneTo,
  paneFlex,
  paneResizable,
  type PaneWidths,
} from "./paneLayout";
import { STUDIO_PANE_META } from "./paneMeta";
import {
  panesListEqual,
  STUDIO_PANE_MIN_WIDTH,
  STUDIO_STAGE_GAP_PX,
  type StudioPaneId,
} from "./panes";
import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import {
  StudioPaneCompactContext,
  StudioShellContext,
  StudioWorkflowDeletedContext,
} from "./StudioShellContext";
import { StudioStageLauncher } from "./StudioStageLauncher";
import { StudioTopBar } from "./StudioTopBar";
import { StudioWorkflowPanels } from "./StudioWorkflowPanels";
import { useStudioPanes } from "./useStudioPanes";

// Below this header width, pane header chrome (view pills, badges) collapses
// to icons — same idea as the run hero, measured per pane, not per viewport.
const PANE_HEADER_COMPACT_BELOW_PX = 480;

// Keyboard step (px) for divider arrow-key resizing.
const DIVIDER_KEY_STEP_PX = 24;

const PANE_DRAG_MIME = "application/x-skyvern-studio-pane";

type PaneReorder = {
  draggingId: StudioPaneId | null;
  // Which edge of this pane the dragged pane would land on; static per drag
  // (arrayMove semantics, same as the editor's block drag).
  placement: "above" | "below" | null;
  onStart: () => void;
  onEnd: () => void;
  onDrop: () => void;
  onMove: (direction: -1 | 1) => void;
};

export function StudioPane({
  id,
  open,
  order,
  flex,
  reorder,
  onClose,
  headerExtras,
  headerActions,
  iconBadge,
  children,
}: {
  id: StudioPaneId;
  open: boolean;
  order: number | undefined;
  flex: string | undefined;
  reorder: PaneReorder;
  onClose: () => void;
  // Rendered after the pane label (badges, view pills).
  headerExtras?: ReactNode;
  // Rendered right-aligned, before the close button.
  headerActions?: ReactNode;
  // Presence-badge overlay on the pane icon (e.g. the Copilot active dot).
  iconBadge?: ReactNode;
  children: ReactNode;
}) {
  const { label, icon: Icon } = STUDIO_PANE_META[id];
  const headerRef = useRef<HTMLDivElement>(null);
  const hasChrome = headerExtras != null || headerActions != null;
  const [compact, setCompact] = useState(false);
  // Layout effect + an immediate measure so a narrow pane never paints one
  // frame of full-width labels before the observer's first callback.
  useLayoutEffect(() => {
    const el = headerRef.current;
    if (!hasChrome || !el || typeof ResizeObserver === "undefined") {
      setCompact(false);
      return;
    }
    const apply = (width: number) => {
      // A closed pane measures 0 (display:none); keep its last real state.
      if (width > 0) {
        setCompact(width < PANE_HEADER_COMPACT_BELOW_PX);
      }
    };
    apply(el.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      apply(entries[0]?.contentRect.width ?? 0);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasChrome]);

  const isDragSource = reorder.draggingId === id;
  const reorderActive = reorder.draggingId !== null;
  // Header buttons (pills, actions, close) must keep working normally: a drag
  // that starts on one is cancelled before it begins.
  const pointerOnControl = useRef(false);
  // Chromium aborts a native drag when the DOM changes inside the dragstart
  // task, and dragstart is a discrete event (sync React flush) — so revealing
  // the drop overlays must wait for the next task.
  const dragEngageTimer = useRef<number | null>(null);
  useEffect(() => {
    return () => {
      if (dragEngageTimer.current !== null) {
        window.clearTimeout(dragEngageTimer.current);
      }
    };
  }, []);
  const [dropHover, setDropHover] = useState(false);
  useEffect(() => {
    if (!reorderActive) {
      setDropHover(false);
    }
  }, [reorderActive]);
  const showDropIndicator =
    dropHover && !isDragSource && reorder.placement !== null;

  return (
    <section
      id={studioPanelId(id)}
      role="region"
      aria-label={label}
      style={{ order, minWidth: STUDIO_PANE_MIN_WIDTH[id], flex }}
      className={cn(
        "relative min-h-0 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1",
        open
          ? "flex duration-200 motion-safe:animate-in motion-safe:fade-in"
          : "hidden",
        isDragSource && "opacity-60 motion-safe:transition-opacity",
      )}
    >
      <div
        ref={headerRef}
        role="group"
        tabIndex={0}
        draggable
        aria-label={`${label} pane header`}
        aria-keyshortcuts="Control+Shift+ArrowLeft Control+Shift+ArrowRight"
        onPointerDownCapture={(event) => {
          pointerOnControl.current =
            event.target instanceof Element &&
            event.target.closest("button, a, input, select, textarea") !== null;
        }}
        onDragStart={(event) => {
          if (pointerOnControl.current) {
            event.preventDefault();
            return;
          }
          // Firefox refuses to start a drag without setData; the drop side
          // also checks this type so foreign drags can't trigger a reorder.
          event.dataTransfer.setData(PANE_DRAG_MIME, id);
          event.dataTransfer.effectAllowed = "move";
          dragEngageTimer.current = window.setTimeout(() => {
            dragEngageTimer.current = null;
            reorder.onStart();
          }, 0);
        }}
        onDragEnd={() => {
          // An instantly-cancelled drag can fire dragend before the engage
          // timer; clearing it keeps the overlays from sticking on.
          if (dragEngageTimer.current !== null) {
            window.clearTimeout(dragEngageTimer.current);
            dragEngageTimer.current = null;
          }
          reorder.onEnd();
        }}
        onKeyDown={(event) => {
          if (!(event.metaKey || event.ctrlKey) || !event.shiftKey) {
            return;
          }
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
            return;
          }
          event.preventDefault();
          reorder.onMove(event.key === "ArrowLeft" ? -1 : 1);
        }}
        className="flex h-9 shrink-0 cursor-grab select-none items-center gap-2 border-b border-border px-3 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring active:cursor-grabbing"
      >
        {/* The drag hint lives on the grip (icon + label) only, so the header's
            buttons keep their own tooltips instead of inheriting this one. */}
        <span
          className="relative shrink-0"
          title={`Drag to reorder the ${label} pane (or Ctrl/Cmd+Shift+←/→)`}
        >
          <Icon className="size-3.5 text-muted-foreground" aria-hidden />
          {iconBadge}
        </span>
        <span
          className="min-w-0 truncate text-xs font-medium text-foreground"
          title={`Drag to reorder the ${label} pane (or Ctrl/Cmd+Shift+←/→)`}
        >
          {label}
        </span>
        <StudioPaneCompactContext.Provider value={compact}>
          {headerExtras}
          <span className="min-w-0 flex-1" />
          {headerActions}
        </StudioPaneCompactContext.Provider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={onClose}
              aria-label={`Close ${label} pane`}
              className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <Cross2Icon className="size-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Close {label}</TooltipContent>
        </Tooltip>
      </div>
      <div className="min-h-0 min-w-0 flex-1">{children}</div>
      {/* Full-pane drop surface while a header drag is live; sits over iframe /
          canvas content that would otherwise swallow dragover events. */}
      {reorderActive ? (
        <div
          data-testid="pane-drop-overlay"
          className={cn(
            "absolute inset-0 z-30 rounded-lg",
            showDropIndicator &&
              "outline-dashed outline-2 -outline-offset-2 outline-blue-500/60",
          )}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
          }}
          onDragEnter={(event) => {
            event.preventDefault();
            setDropHover(true);
          }}
          onDragLeave={() => setDropHover(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDropHover(false);
            if (!event.dataTransfer.types.includes(PANE_DRAG_MIME)) {
              return;
            }
            reorder.onDrop();
          }}
        >
          {showDropIndicator ? (
            <div
              data-testid="pane-drop-indicator"
              data-placement={reorder.placement}
              aria-hidden
              className={cn(
                "pointer-events-none absolute inset-y-2 w-1 rounded-full bg-blue-500 shadow-[0_0_6px_rgba(59,130,246,0.6)] motion-safe:animate-in motion-safe:fade-in",
                reorder.placement === "above" ? "left-1" : "right-1",
              )}
            />
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/**
 * Pointer-drag divider between two open panes. It is exactly the inter-pane
 * gap (STUDIO_STAGE_GAP_PX wide), so the fit math in panes.ts still holds.
 * During the drag it writes flex pins straight to the pane elements (no React
 * re-render per frame); the result commits to the store on release.
 */
function StudioPaneDivider({
  leftId,
  rightId,
  order,
  panes,
  onCommit,
  onReset,
}: {
  leftId: StudioPaneId;
  rightId: StudioPaneId;
  order: number;
  panes: readonly StudioPaneId[];
  onCommit: (widths: PaneWidths) => void;
  onReset: () => void;
}) {
  const [active, setActive] = useState(false);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    left: HTMLElement;
    right: HTMLElement;
    leftStart: number;
    rightStart: number;
    leftFlexBefore: string;
    rightFlexBefore: string;
    leftPinned: boolean;
    rightPinned: boolean;
    lastDelta: number;
  } | null>(null);

  const paneElements = () => {
    const left = document.getElementById(studioPanelId(leftId));
    const right = document.getElementById(studioPanelId(rightId));
    return left && right ? { left, right } : null;
  };

  // A pane close/navigation can unmount the divider mid-drag; put back the
  // cursor and transitions it was holding onto.
  useEffect(() => {
    return () => {
      const drag = dragRef.current;
      if (drag) {
        dragRef.current = null;
        drag.left.style.transition = "";
        drag.right.style.transition = "";
        document.body.style.cursor = "";
      }
    };
  }, []);

  const beginDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || dragRef.current) {
      return;
    }
    const els = paneElements();
    if (!els) {
      return;
    }
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      left: els.left,
      right: els.right,
      leftStart: els.left.offsetWidth,
      rightStart: els.right.offsetWidth,
      leftFlexBefore: els.left.style.flex,
      rightFlexBefore: els.right.style.flex,
      leftPinned: paneResizable(leftId, panes),
      rightPinned: paneResizable(rightId, panes),
      lastDelta: 0,
    };
    // Freeze any width transition so the per-frame pins land instantly.
    els.left.style.transition = "none";
    els.right.style.transition = "none";
    document.body.style.cursor = "col-resize";
    setActive(true);
  };

  const moveDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    const delta = Math.round(
      clampResizeDelta(
        event.clientX - drag.startX,
        { id: leftId, width: drag.leftStart },
        { id: rightId, width: drag.rightStart },
      ),
    );
    drag.lastDelta = delta;
    // Only non-greedy neighbors get pinned; a greedy neighbor keeps flexing so
    // the row always fills (the neighbors' total is preserved either way).
    if (drag.leftPinned) {
      drag.left.style.flex = `0 1 ${drag.leftStart + delta}px`;
    }
    if (drag.rightPinned) {
      drag.right.style.flex = `0 1 ${drag.rightStart - delta}px`;
    }
  };

  const endDrag = () => {
    const drag = dragRef.current;
    if (!drag) {
      return;
    }
    dragRef.current = null;
    drag.left.style.transition = "";
    drag.right.style.transition = "";
    document.body.style.cursor = "";
    setActive(false);
    if (drag.lastDelta === 0) {
      // Nothing moved: put back whatever React had written so the DOM and the
      // (unchanged) store stay in agreement.
      drag.left.style.flex = drag.leftFlexBefore;
      drag.right.style.flex = drag.rightFlexBefore;
      return;
    }
    const widths: PaneWidths = {};
    if (drag.leftPinned) {
      widths[leftId] = drag.leftStart + drag.lastDelta;
    }
    if (drag.rightPinned) {
      widths[rightId] = drag.rightStart - drag.lastDelta;
    }
    if (Object.keys(widths).length === 0) {
      return;
    }
    onCommit(widths);
  };

  const keyResize = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    event.preventDefault();
    const els = paneElements();
    if (!els) {
      return;
    }
    const delta = Math.round(
      clampResizeDelta(
        event.key === "ArrowLeft" ? -DIVIDER_KEY_STEP_PX : DIVIDER_KEY_STEP_PX,
        { id: leftId, width: els.left.offsetWidth },
        { id: rightId, width: els.right.offsetWidth },
      ),
    );
    if (delta === 0) {
      return;
    }
    const widths: PaneWidths = {};
    if (paneResizable(leftId, panes)) {
      widths[leftId] = els.left.offsetWidth + delta;
    }
    if (paneResizable(rightId, panes)) {
      widths[rightId] = els.right.offsetWidth - delta;
    }
    if (Object.keys(widths).length === 0) {
      return;
    }
    onCommit(widths);
  };

  const leftLabel = STUDIO_PANE_META[leftId].label;
  const rightLabel = STUDIO_PANE_META[rightId].label;
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${leftLabel} and ${rightLabel}`}
      tabIndex={0}
      title="Drag to resize · double-click to reset all widths · arrow keys to adjust"
      style={{ order, width: STUDIO_STAGE_GAP_PX }}
      onPointerDown={beginDrag}
      onPointerMove={moveDrag}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onLostPointerCapture={endDrag}
      onDoubleClick={onReset}
      onKeyDown={keyResize}
      className="group relative shrink-0 cursor-col-resize touch-none focus-visible:outline-none"
    >
      <span
        aria-hidden
        className={cn(
          "absolute inset-y-3 left-1/2 w-0.5 -translate-x-1/2 rounded-full motion-safe:transition-colors",
          active
            ? "bg-muted-foreground"
            : "bg-transparent group-hover:bg-border group-focus-visible:bg-ring",
        )}
      />
    </div>
  );
}

/**
 * Panes shell: the top bar's pane toggles (Copilot, Editor, Browser, Overview)
 * each toggle a pane; open panes share the stage side by side in click order
 * (?panes=). The Copilot chat is portaled into its pane from Workspace.
 */
export function StudioShell(props: StudioWorkspaceProps) {
  return (
    <StudioWorkflowDeletedContext.Provider
      value={props.workflow.deleted_at ?? null}
    >
      <StudioPaneDefaultsProvider
        hasBlocks={props.workflow.workflow_definition.blocks.length > 0}
      >
        <StudioStage {...props} />
      </StudioPaneDefaultsProvider>
    </StudioWorkflowDeletedContext.Provider>
  );
}

/**
 * Pane body while the source agent is deleted: the pane's normal content would
 * operate on the missing workflow (Copilot chats, editor saves, debug
 * browsers), so it never mounts — runs stay viewable from the run panes.
 */
function WorkflowDeletedPaneNotice() {
  return (
    <div className="flex h-full items-center justify-center p-4">
      <p className="max-w-xs text-center text-sm text-muted-foreground">
        Source agent deleted — this run is view-only.
      </p>
    </div>
  );
}

function StudioStage(props: StudioWorkspaceProps) {
  const { panes, closePane, openPane, setPanesOrder } = useStudioPanes();
  const { registerStageElement } = useStudioPaneDefaults();
  const { workflowPermanentId } = useParams();
  const workflowDeleted = Boolean(props.workflow.deleted_at);
  const isRecording = useRecordingStore((s) => s.isRecording);
  // The title store is normally seeded by the embedded Workspace's canvas,
  // which never mounts for a deleted agent — seed it here instead.
  const initializeTitle = useWorkflowTitleStore((s) => s.initializeTitle);
  const initialTitle = props.initialTitle;
  useEffect(() => {
    if (workflowDeleted) {
      initializeTitle(initialTitle);
    }
  }, [workflowDeleted, initialTitle, initializeTitle]);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const browserSessionId = debugSession?.browser_session_id ?? null;
  const pipMinimized = useStudioShellStore((s) => s.pipMinimized);
  const paneWidths = useStudioShellStore((s) => s.paneWidths);
  const setPaneWidths = useStudioShellStore((s) => s.setPaneWidths);
  const resetPaneWidths = useStudioShellStore((s) => s.resetPaneWidths);
  const [draggingPaneId, setDraggingPaneId] = useState<StudioPaneId | null>(
    null,
  );
  const [copilotPortalEl, setCopilotPortalEl] = useState<HTMLElement | null>(
    null,
  );
  const [panelPortalEl, setPanelPortalEl] = useState<HTMLElement | null>(null);

  // The live browser stream is mounted once into this detached host and
  // re-parented into the owning pane, so it never remounts on pane changes.
  const [streamHostEl] = useState(() => {
    const el = document.createElement("div");
    el.className = "h-full w-full";
    return el;
  });
  const [editorStreamSlot, setEditorStreamSlot] = useState<HTMLElement | null>(
    null,
  );
  const [browserStreamSlot, setBrowserStreamSlot] =
    useState<HTMLElement | null>(null);
  const [runStreamSlot, setRunStreamSlot] = useState<HTMLElement | null>(null);
  const [streamHolderEl, setStreamHolderEl] = useState<HTMLElement | null>(
    null,
  );

  const browserOpen = panes.includes("browser");
  const editorOpen = panes.includes("editor");
  const overviewOpen = panes.includes("overview");

  // Move the persistent stream node into the highest-priority open surface:
  // Browser pane > Overview pane with a live block run (runStreamSlot registers
  // only then) > Editor PiP > offscreen park, which keeps the socket warm.
  useLayoutEffect(() => {
    const activeSlot = browserOpen
      ? browserStreamSlot
      : overviewOpen && runStreamSlot
        ? runStreamSlot
        : editorOpen && !pipMinimized
          ? editorStreamSlot
          : null;
    const dest = activeSlot ?? streamHolderEl;
    if (dest && streamHostEl.parentElement !== dest) {
      // BrowserStream re-asserts scaleViewport on its own resize, so it rescales
      // itself on re-parent to a different-sized slot — no resize nudge needed.
      dest.appendChild(streamHostEl);
    }
  }, [
    browserOpen,
    editorOpen,
    overviewOpen,
    pipMinimized,
    editorStreamSlot,
    browserStreamSlot,
    runStreamSlot,
    streamHolderEl,
    streamHostEl,
  ]);

  const shellContextValue = useMemo(
    () => ({
      copilotPortalEl,
      panelPortalEl,
      setEditorStreamSlot,
      setBrowserStreamSlot,
      setRunStreamSlot,
    }),
    [copilotPortalEl, panelPortalEl],
  );

  // The ✕ unmounts with its pane, so hand focus back to the pane's toggle.
  const closeWithFocus = (id: StudioPaneId) => {
    closePane(id);
    document.getElementById(studioTabId(id))?.focus();
  };

  // Reorder is otherwise invisible to assistive tech (only CSS order moves),
  // so voice the result through the polite live region below.
  const [reorderAnnouncement, setReorderAnnouncement] = useState("");
  const commitOrder = (movedId: StudioPaneId, next: StudioPaneId[]) => {
    if (panesListEqual(next, panes)) {
      return;
    }
    setPanesOrder(next);
    setReorderAnnouncement(
      `${STUDIO_PANE_META[movedId].label} pane moved to position ${
        next.indexOf(movedId) + 1
      } of ${next.length}`,
    );
  };

  // Recording lives in the Browser pane (the live stream is there) with the
  // live-drafts panel taking over the Copilot pane. Once a commit is in flight
  // or its blocks are landing, reveal the Editor pane (it shows the loading
  // overlay). Gated on lifecycle transitions so manual pane changes made
  // mid-recording are preserved.
  const isCommitting = useRecordingStore((s) => s.isCommitting);
  const recordedBlocksPending = useRecordedBlocksStore(
    (s) => (s.blocks?.length ?? 0) > 0,
  );
  const processingRecording = isCommitting || recordedBlocksPending;
  const prevIsRecordingRef = useRef(false);
  const prevProcessingRef = useRef(false);
  useEffect(() => {
    const wasRecording = prevIsRecordingRef.current;
    const wasProcessing = prevProcessingRef.current;
    if (processingRecording && !wasProcessing) {
      openPane("editor");
    } else if (isRecording && !wasRecording) {
      // Recording or finalizing → live browser + drafts.
      openPane("copilot");
      openPane("browser");
    } else if (!isRecording && wasRecording && !processingRecording) {
      // Recording ended (commit or discard) → back to the canvas.
      openPane("editor");
    }
    prevIsRecordingRef.current = isRecording;
    prevProcessingRef.current = processingRecording;
  }, [isRecording, processingRecording, openPane]);

  const paneProps = (id: StudioPaneId) => {
    const index = panes.indexOf(id);
    return {
      id,
      open: index >= 0,
      // Panes take even slots and the dividers between them take odd slots.
      order: index >= 0 ? index * 2 : undefined,
      flex: index >= 0 ? paneFlex(id, panes, paneWidths) : undefined,
      onClose: () => closeWithFocus(id),
      reorder: {
        draggingId: draggingPaneId,
        placement:
          draggingPaneId === null
            ? null
            : (deriveDropIndicator({
                order: [...panes],
                activeId: draggingPaneId,
                overId: id,
              })?.placement ?? null),
        onStart: () => setDraggingPaneId(id),
        onEnd: () => setDraggingPaneId(null),
        onDrop: () => {
          if (draggingPaneId !== null && draggingPaneId !== id) {
            commitOrder(draggingPaneId, movePaneTo(panes, draggingPaneId, id));
          }
          setDraggingPaneId(null);
        },
        onMove: (direction: -1 | 1) =>
          commitOrder(id, movePaneBy(panes, id, direction)),
      },
    };
  };

  return (
    <StudioShellContext.Provider value={shellContextValue}>
      {/* Studio-scoped tooltip timing: the app has no global provider, so this
          only affects Radix tooltips inside the shell. */}
      <TooltipProvider delayDuration={200} skipDelayDuration={300}>
        <div className="flex h-full w-full flex-col">
          <StudioTopBar />
          <div className="flex min-h-0 min-w-0 flex-1">
            {/* Panes keep a fixed DOM order (stable mounts for the canvas, chat and
              stream slots); the CSS order carries the layout order instead, so
              screen-reader/Tab order stays the fixed order, not the visual one.
              Reordering (drag or keyboard) only rewrites CSS order — panes and
              the stream singleton never remount. */}
            <div
              ref={registerStageElement}
              className="relative flex min-h-0 min-w-0 flex-1 overflow-hidden p-3"
            >
              <StudioPane
                {...paneProps("copilot")}
                headerActions={<CopilotPaneControls />}
                iconBadge={<CopilotActiveDot />}
              >
                {workflowDeleted ? (
                  <WorkflowDeletedPaneNotice />
                ) : (
                  <div className="relative h-full w-full">
                    {/* Copilot portal target. Kept mounted while the pane is closed
                    — or while recording, when the live-drafts panel covers it —
                    so an in-flight Copilot turn isn't torn down. */}
                    <div
                      ref={setCopilotPortalEl}
                      className={cn(
                        "h-full w-full",
                        isRecording && "pointer-events-none",
                      )}
                    />
                    {isRecording && browserSessionId ? (
                      <div className="absolute inset-0 duration-150 animate-in fade-in slide-in-from-left-2">
                        <RecordingPanel browserSessionId={browserSessionId} />
                      </div>
                    ) : null}
                  </div>
                )}
              </StudioPane>
              <StudioPane
                {...paneProps("editor")}
                headerExtras={<EditorPaneModeToggle />}
              >
                {/* The embedded Workspace boots debug sessions, copilot chats
                    and block-script queries against the workflow — none of
                    which exist once the agent is deleted, so it never mounts. */}
                {workflowDeleted ? (
                  <WorkflowDeletedPaneNotice />
                ) : (
                  <EditorTab {...props} />
                )}
              </StudioPane>
              {/* Kept mounted (CSS-hidden) so its stream slot stays registered;
                the persistent stream node is re-parented in, not remounted. */}
              <StudioPane
                {...paneProps("browser")}
                headerExtras={<BrowserPaneViewPills />}
                headerActions={<BrowserPaneActions />}
              >
                <BrowserTab />
              </StudioPane>
              <StudioPane
                {...paneProps("overview")}
                headerExtras={<RunPaneViewToggles />}
                headerActions={<RunPaneActions />}
              >
                <RunTab />
              </StudioPane>
              {/* Dividers are the inter-pane gaps; stateless, so unlike the panes
                they can re-render freely as the open list changes. */}
              {panes.slice(1).map((rightId, index) => (
                <StudioPaneDivider
                  key={`${panes[index]}:${rightId}`}
                  leftId={panes[index]!}
                  rightId={rightId}
                  order={index * 2 + 1}
                  panes={panes}
                  onCommit={setPaneWidths}
                  onReset={resetPaneWidths}
                />
              ))}
              {panes.length === 0 ? <StudioStageLauncher /> : null}
              <StudioCoachMark />
              <StudioWorkflowPanels />
              {/* Overlay target for Workspace-wired panels (pointer-events off
                  while empty so the stage stays clickable). */}
              <div
                ref={setPanelPortalEl}
                className="pointer-events-none absolute inset-0 z-40"
              />
              <span
                role="status"
                aria-live="polite"
                aria-atomic="true"
                className="sr-only"
              >
                {reorderAnnouncement}
              </span>
            </div>
          </div>
          <div
            ref={setStreamHolderEl}
            aria-hidden
            className="h-0 w-0 overflow-hidden"
          />
          {createPortal(<StudioBrowserStream />, streamHostEl)}
        </div>
      </TooltipProvider>
    </StudioShellContext.Provider>
  );
}
