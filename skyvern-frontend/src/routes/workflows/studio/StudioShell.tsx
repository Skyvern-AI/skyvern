import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { cn } from "@/util/utils";

import { BrowserTab } from "./BrowserTab";
import { CopilotRail } from "./CopilotRail";
import { EditorTab, type StudioWorkspaceProps } from "./EditorTab";
import { RunTab } from "./RunTab";
import { StudioBrowserStream } from "./StudioBrowserStream";
import {
  STUDIO_COPILOT_RAIL_WIDTH,
  STUDIO_COPILOT_WIDTH,
  studioPanelId,
  studioTabId,
} from "./constants";
import { StudioShellContext } from "./StudioShellContext";
import { StudioTopBar } from "./StudioTopBar";
import { StudioWorkflowPanels } from "./StudioWorkflowPanels";
import { usePresence } from "./usePresence";

/**
 * Spine + Stage shell: a persistent Copilot column beside a Stage that swaps the
 * Editor and Run tabs. The Copilot is portaled in from the embedded Workspace.
 */
export function StudioShell(props: StudioWorkspaceProps) {
  const tab = useStudioShellStore((s) => s.tab);
  const setTab = useStudioShellStore((s) => s.setTab);
  const copilotCollapsed = useStudioShellStore((s) => s.copilotCollapsed);
  const setCopilotCollapsed = useStudioShellStore((s) => s.setCopilotCollapsed);
  const pipMinimized = useStudioShellStore((s) => s.pipMinimized);
  const [copilotPortalEl, setCopilotPortalEl] = useState<HTMLElement | null>(
    null,
  );

  // The live browser stream is mounted once into this detached host and
  // re-parented into the active surface, so it never remounts on tab switch.
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
  const [streamHolderEl, setStreamHolderEl] = useState<HTMLElement | null>(
    null,
  );

  // Move the persistent stream node into the showing surface (PiP / Browser tab),
  // parking it in the offscreen holder otherwise so the socket stays warm.
  useLayoutEffect(() => {
    const activeSlot =
      tab === "browser"
        ? browserStreamSlot
        : tab === "editor" && !pipMinimized
          ? editorStreamSlot
          : null;
    const dest = activeSlot ?? streamHolderEl;
    if (dest && streamHostEl.parentElement !== dest) {
      // BrowserStream re-asserts scaleViewport on its own resize, so it rescales
      // itself on re-parent to a different-sized slot — no resize nudge needed.
      dest.appendChild(streamHostEl);
    }
  }, [
    tab,
    pipMinimized,
    editorStreamSlot,
    browserStreamSlot,
    streamHolderEl,
    streamHostEl,
  ]);

  const shellContextValue = useMemo(
    () => ({ copilotPortalEl, setEditorStreamSlot, setBrowserStreamSlot }),
    [copilotPortalEl],
  );

  // Pick the initial tab from the deep link ONCE per mount; must not re-fire on
  // later URL writes (the Run tab writes ?wr=/?active=) or it fights manual switches.
  const [searchParams] = useSearchParams();
  const deepLinkRunId = searchParams.get("wr");
  const deepLinkBlockLabel = searchParams.get("bl");
  const deepLinkActive = searchParams.get("active");
  const initialTabAppliedRef = useRef(false);
  useEffect(() => {
    if (initialTabAppliedRef.current) {
      return;
    }
    initialTabAppliedRef.current = true;
    if (deepLinkRunId && deepLinkBlockLabel) {
      setTab("browser");
      return;
    }
    if (deepLinkRunId || deepLinkActive) {
      setTab("run");
      return;
    }
    setTab("editor");
  }, [deepLinkRunId, deepLinkBlockLabel, deepLinkActive, setTab]);

  const copilotWidth = copilotCollapsed
    ? STUDIO_COPILOT_RAIL_WIDTH
    : STUDIO_COPILOT_WIDTH;

  // Keep the collapsed rail mounted briefly after expanding so it can fade out.
  const railPresent = usePresence(copilotCollapsed, 150);

  return (
    <StudioShellContext.Provider value={shellContextValue}>
      <div className="flex h-full w-full flex-col">
        <StudioTopBar />
        <div
          className="grid min-h-0 flex-1"
          style={{
            gridTemplateColumns: `${copilotWidth}px minmax(0, 1fr)`,
            gridTemplateRows: "minmax(0, 1fr)",
          }}
        >
          {/* Copilot portal target. Kept mounted (parked offscreen) when
              collapsed so an in-flight Copilot stream isn't torn down. */}
          <div className="relative h-full min-w-0">
            <div
              ref={setCopilotPortalEl}
              className={
                copilotCollapsed
                  ? "h-0 w-0 overflow-hidden"
                  : "h-full w-full py-3 pl-3 duration-150 animate-in fade-in slide-in-from-left-2"
              }
            />
            {railPresent ? (
              <div
                // Fixed rail width so it doesn't stretch to the expanded
                // column while fading out on expand.
                style={{ width: STUDIO_COPILOT_RAIL_WIDTH }}
                className={cn(
                  "absolute left-0 top-0 h-full py-3 pl-3 duration-150",
                  copilotCollapsed
                    ? "animate-in fade-in"
                    : "animate-out fade-out",
                )}
              >
                <CopilotRail onExpand={() => setCopilotCollapsed(false)} />
              </div>
            ) : null}
          </div>
          <div className="relative h-full min-h-0 min-w-0 overflow-hidden">
            <div
              role="tabpanel"
              id={studioPanelId("editor")}
              aria-labelledby={studioTabId("editor")}
              className={
                tab === "editor" ? "h-full w-full" : "hidden h-full w-full"
              }
            >
              <EditorTab {...props} />
            </div>
            {/* Kept mounted (CSS-hidden) so its stream slot stays registered;
                the persistent stream node is re-parented in, not remounted. */}
            <div
              role="tabpanel"
              id={studioPanelId("browser")}
              aria-labelledby={studioTabId("browser")}
              className={
                tab === "browser" ? "h-full w-full" : "hidden h-full w-full"
              }
            >
              <BrowserTab />
            </div>
            <div
              role="tabpanel"
              id={studioPanelId("run")}
              aria-labelledby={studioTabId("run")}
              className={
                tab === "run" ? "h-full w-full" : "hidden h-full w-full"
              }
            >
              <RunTab />
            </div>
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
