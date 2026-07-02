import { useLayoutEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Cross2Icon } from "@radix-ui/react-icons";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { cn } from "@/util/utils";

import { BrowserTab } from "./BrowserTab";
import { EditorTab, type StudioWorkspaceProps } from "./EditorTab";
import { RunTab } from "./RunTab";
import { StudioBrowserStream } from "./StudioBrowserStream";
import { studioPanelId, studioTabId } from "./constants";
import { STUDIO_PANE_META } from "./paneMeta";
import { type StudioPaneId } from "./panes";
import { StudioShellContext } from "./StudioShellContext";
import { StudioSpine } from "./StudioSpine";
import { StudioTopBar } from "./StudioTopBar";
import { StudioWorkflowPanels } from "./StudioWorkflowPanels";
import { useStudioPanes } from "./useStudioPanes";

// Width floors from the approved mock; the Copilot pane also holds a ceiling so
// a lone chat doesn't stretch across the whole stage.
const PANE_MIN_WIDTH: Record<StudioPaneId, number> = {
  copilot: 260,
  editor: 220,
  browser: 260,
  run: 220,
};
const COPILOT_MAX_WIDTH = 440;

function StudioPane({
  id,
  open,
  order,
  onClose,
  children,
}: {
  id: StudioPaneId;
  open: boolean;
  order: number | undefined;
  onClose: () => void;
  children: ReactNode;
}) {
  const { label, icon: Icon } = STUDIO_PANE_META[id];
  return (
    <section
      id={studioPanelId(id)}
      role="region"
      aria-label={label}
      style={{
        order,
        minWidth: PANE_MIN_WIDTH[id],
        maxWidth: id === "copilot" ? COPILOT_MAX_WIDTH : undefined,
      }}
      className={cn(
        "min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-border bg-slate-elevation1",
        open
          ? "flex duration-200 motion-safe:animate-in motion-safe:fade-in"
          : "hidden",
      )}
    >
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-3">
        <Icon className="size-3.5 shrink-0 text-studio-accent" aria-hidden />
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
          {label}
        </span>
        <button
          type="button"
          onClick={onClose}
          title={`Close ${label}`}
          aria-label={`Close ${label} pane`}
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
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
 * Run) each toggle a pane; open panes share the stage side by side in click
 * order (?panes=). The Copilot chat is portaled into its pane from Workspace.
 */
export function StudioShell(props: StudioWorkspaceProps) {
  const { panes, closePane } = useStudioPanes();
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
  const runOpen = panes.includes("run");

  // Move the persistent stream node into the highest-priority open surface:
  // Browser pane > Run pane with a live block run (runStreamSlot registers only
  // then) > Editor PiP > offscreen park, which keeps the socket warm.
  useLayoutEffect(() => {
    const activeSlot = browserOpen
      ? browserStreamSlot
      : runOpen && runStreamSlot
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
    runOpen,
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
          <div className="relative flex min-h-0 min-w-0 flex-1 gap-3 overflow-hidden p-3">
            <StudioPane {...paneProps("copilot")}>
              {/* Copilot portal target. Kept mounted while the pane is closed so
                  an in-flight Copilot turn isn't torn down. */}
              <div ref={setCopilotPortalEl} className="h-full w-full" />
            </StudioPane>
            <StudioPane {...paneProps("editor")}>
              <EditorTab {...props} />
            </StudioPane>
            {/* Kept mounted (CSS-hidden) so its stream slot stays registered;
                the persistent stream node is re-parented in, not remounted. */}
            <StudioPane {...paneProps("browser")}>
              <BrowserTab />
            </StudioPane>
            <StudioPane {...paneProps("run")}>
              <RunTab />
            </StudioPane>
            {panes.length === 0 ? (
              <div className="flex flex-1 items-center justify-center">
                <p className="max-w-[17rem] text-center text-sm text-muted-foreground">
                  No panes open. Open one from the rail on the left.
                </p>
              </div>
            ) : null}
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
