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

import { RecordingPanel } from "@/routes/workflows/editor/recording/RecordingPanel";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { cn } from "@/util/utils";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";

import { BrowserPaneActions, BrowserPaneViewPills } from "./BrowserPaneHeader";
import { BrowserTab } from "./BrowserTab";
import { EditorTab, type StudioWorkspaceProps } from "./EditorTab";
import { RunTab } from "./RunTab";
import { RunPaneActions, RunPaneStatusBadge } from "./runview/RunPaneHeader";
import { StudioBrowserStream } from "./StudioBrowserStream";
import { StudioCoachMark } from "./StudioCoachMark";
import { studioPanelId, studioTabId } from "./constants";
import { STUDIO_PANE_META } from "./paneMeta";
import { STUDIO_PANE_MIN_WIDTH, type StudioPaneId } from "./panes";
import { StudioPaneDefaultsProvider } from "./StudioPaneDefaults";
import { useStudioPaneDefaults } from "./StudioPaneDefaultsContext";
import {
  StudioPaneCompactContext,
  StudioShellContext,
} from "./StudioShellContext";
import { StudioSpine } from "./StudioSpine";
import { StudioStageLauncher } from "./StudioStageLauncher";
import { StudioTopBar } from "./StudioTopBar";
import { StudioWorkflowPanels } from "./StudioWorkflowPanels";
import { useStudioPanes } from "./useStudioPanes";

// The Copilot pane holds a ceiling so a lone chat doesn't stretch across the
// whole stage; the shared floors live in panes.ts next to the fit math.
const COPILOT_MAX_WIDTH = 440;

// Below this header width, pane header chrome (view pills, badges) collapses
// to icons — same idea as the run hero, measured per pane, not per viewport.
const PANE_HEADER_COMPACT_BELOW_PX = 480;

function StudioPane({
  id,
  open,
  order,
  onClose,
  headerExtras,
  headerActions,
  children,
}: {
  id: StudioPaneId;
  open: boolean;
  order: number | undefined;
  onClose: () => void;
  // Rendered after the pane label (badges, view pills).
  headerExtras?: ReactNode;
  // Rendered right-aligned, before the close button.
  headerActions?: ReactNode;
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
  return (
    <section
      id={studioPanelId(id)}
      role="region"
      aria-label={label}
      style={{
        order,
        minWidth: STUDIO_PANE_MIN_WIDTH[id],
        maxWidth: id === "copilot" ? COPILOT_MAX_WIDTH : undefined,
      }}
      className={cn(
        "min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1",
        open
          ? "flex duration-200 motion-safe:animate-in motion-safe:fade-in"
          : "hidden",
      )}
    >
      <div
        ref={headerRef}
        className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-3"
      >
        <Icon className="size-3.5 shrink-0 text-studio-accent" aria-hidden />
        <span className="min-w-0 truncate text-xs font-medium text-foreground">
          {label}
        </span>
        <StudioPaneCompactContext.Provider value={compact}>
          {headerExtras}
          <span className="min-w-0 flex-1" />
          {headerActions}
        </StudioPaneCompactContext.Provider>
        <button
          type="button"
          onClick={onClose}
          title={`Close ${label}`}
          aria-label={`Close ${label} pane`}
          className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <Cross2Icon className="size-3.5" />
        </button>
      </div>
      <div className="min-h-0 min-w-0 flex-1">{children}</div>
    </section>
  );
}

/**
 * Spine + panes shell: one vertical spine whose tabs (Copilot, Editor, Browser,
 * Timeline) each toggle a pane; open panes share the stage side by side in click
 * order (?panes=). The Copilot chat is portaled into its pane from Workspace.
 */
export function StudioShell(props: StudioWorkspaceProps) {
  return (
    <StudioPaneDefaultsProvider
      hasBlocks={props.workflow.workflow_definition.blocks.length > 0}
    >
      <StudioStage {...props} />
    </StudioPaneDefaultsProvider>
  );
}

function StudioStage(props: StudioWorkspaceProps) {
  const { panes, closePane, openPane } = useStudioPanes();
  const { registerStageElement } = useStudioPaneDefaults();
  const { workflowPermanentId } = useParams();
  const isRecording = useRecordingStore((s) => s.isRecording);
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const browserSessionId = debugSession?.browser_session_id ?? null;
  const pipMinimized = useStudioShellStore((s) => s.pipMinimized);
  const [copilotPortalEl, setCopilotPortalEl] = useState<HTMLElement | null>(
    null,
  );

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
  const timelineOpen = panes.includes("timeline");

  // Move the persistent stream node into the highest-priority open surface:
  // Browser pane > Timeline pane with a live block run (runStreamSlot registers
  // only then) > Editor PiP > offscreen park, which keeps the socket warm.
  useLayoutEffect(() => {
    const activeSlot = browserOpen
      ? browserStreamSlot
      : timelineOpen && runStreamSlot
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
    timelineOpen,
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
      setEditorStreamSlot,
      setBrowserStreamSlot,
      setRunStreamSlot,
    }),
    [copilotPortalEl],
  );

  // The ✕ unmounts with its pane, so hand focus back to the pane's spine tab.
  const closeWithFocus = (id: StudioPaneId) => {
    closePane(id);
    document.getElementById(studioTabId(id))?.focus();
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
      order: index >= 0 ? index : undefined,
      onClose: () => closeWithFocus(id),
    };
  };

  return (
    <StudioShellContext.Provider value={shellContextValue}>
      <div className="flex h-full w-full flex-col">
        <StudioTopBar />
        <div className="flex min-h-0 min-w-0 flex-1">
          <StudioSpine />
          {/* Panes keep a fixed DOM order (stable mounts for the canvas, chat and
              stream slots); the CSS order carries the click order instead, so
              screen-reader/Tab order stays the fixed order, not the visual one. */}
          <div
            ref={registerStageElement}
            className="relative flex min-h-0 min-w-0 flex-1 gap-3 overflow-hidden p-3"
          >
            <StudioPane {...paneProps("copilot")}>
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
            </StudioPane>
            <StudioPane {...paneProps("editor")}>
              <EditorTab {...props} />
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
              {...paneProps("timeline")}
              headerExtras={<RunPaneStatusBadge />}
              headerActions={<RunPaneActions />}
            >
              <RunTab />
            </StudioPane>
            {panes.length === 0 ? <StudioStageLauncher /> : null}
            <StudioCoachMark />
            <StudioWorkflowPanels />
          </div>
        </div>
        <div
          ref={setStreamHolderEl}
          aria-hidden
          className="h-0 w-0 overflow-hidden"
        />
        {createPortal(<StudioBrowserStream />, streamHostEl)}
      </div>
    </StudioShellContext.Provider>
  );
}
