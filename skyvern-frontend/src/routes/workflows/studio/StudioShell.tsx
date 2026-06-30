import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";

import {
  BLOCK_SIDEBAR_WIDTH_MAX,
  BLOCK_SIDEBAR_WIDTH_MIN,
  useBlockSidebarWidthStore,
} from "@/store/BlockSidebarWidthStore";
import { useStudioShellStore } from "@/store/StudioShellStore";
import { cn } from "@/util/utils";

import { useSettingsSidebarLayout } from "../editor/hooks/useSettingsSidebarLayout";

import { BrowserTab } from "./BrowserTab";
import { CopilotRail } from "./CopilotRail";
import { EditorTab, type StudioWorkspaceProps } from "./EditorTab";
import { RunTab } from "./RunTab";
import { StudioBrowserStream } from "./StudioBrowserStream";
import {
  STUDIO_COPILOT_COLLAPSE_EASE,
  STUDIO_COPILOT_RAIL_WIDTH,
  STUDIO_COPILOT_TRANSITION_EASE,
  STUDIO_COPILOT_TRANSITION_MS,
  STUDIO_COPILOT_WIDTH,
  initialStudioTab,
  studioPanelId,
  studioTabId,
} from "./constants";
import { StudioShellContext } from "./StudioShellContext";
import { StudioTopBar } from "./StudioTopBar";
import { StudioWorkflowPanels } from "./StudioWorkflowPanels";

// Keyboard arrow-key resize step for the settings separator (px).
const SETTINGS_RESIZE_STEP = 24;

function clampSettingsWidth(next: number): number {
  return Math.min(
    BLOCK_SIDEBAR_WIDTH_MAX,
    Math.max(BLOCK_SIDEBAR_WIDTH_MIN, Math.round(next)),
  );
}

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
  const [settingsPortalEl, setSettingsPortalEl] = useState<HTMLElement | null>(
    null,
  );
  const [settingsRailPortalEl, setSettingsRailPortalEl] =
    useState<HTMLElement | null>(null);

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
  const [runStreamSlot, setRunStreamSlot] = useState<HTMLElement | null>(null);
  const [streamHolderEl, setStreamHolderEl] = useState<HTMLElement | null>(
    null,
  );

  // Move the persistent stream node into the showing surface (PiP / Browser tab /
  // Run tab for a block run), parking it in the offscreen holder otherwise so the
  // socket stays warm. runStreamSlot is registered only for a block run, so a full
  // run on the Run tab falls through to the holder and keeps its own RunLiveStream.
  useLayoutEffect(() => {
    const activeSlot =
      tab === "browser"
        ? browserStreamSlot
        : tab === "editor" && !pipMinimized
          ? editorStreamSlot
          : tab === "run"
            ? runStreamSlot
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
    runStreamSlot,
    streamHolderEl,
    streamHostEl,
  ]);

  const shellContextValue = useMemo(
    () => ({
      copilotPortalEl,
      settingsPortalEl,
      settingsRailPortalEl,
      setEditorStreamSlot,
      setBrowserStreamSlot,
      setRunStreamSlot,
    }),
    [copilotPortalEl, settingsPortalEl, settingsRailPortalEl],
  );

  // Pick the initial tab from the deep link ONCE per mount; must not re-fire on
  // later URL writes (the Run tab writes ?wr=/?active=) or it fights manual switches.
  const [searchParams] = useSearchParams();
  const deepLinkRunId = searchParams.get("wr");
  const deepLinkActive = searchParams.get("active");
  const initialTabAppliedRef = useRef(false);
  useEffect(() => {
    if (initialTabAppliedRef.current) {
      return;
    }
    initialTabAppliedRef.current = true;
    setTab(initialStudioTab({ runId: deepLinkRunId, active: deepLinkActive }));
  }, [deepLinkRunId, deepLinkActive, setTab]);

  const copilotWidth = copilotCollapsed
    ? STUDIO_COPILOT_RAIL_WIDTH
    : STUDIO_COPILOT_WIDTH;

  // Settings sidebar as a third grid column (mirrors the Copilot): the panel is
  // portaled in from FlowRenderer, but its column width lives here so opening,
  // collapsing, and resizing it reflow the Stage instead of overlaying it.
  const { open: settingsOpen, collapsed: settingsCollapsed } =
    useSettingsSidebarLayout();
  const committedSettingsWidth = useBlockSidebarWidthStore((s) => s.width);
  const setBlockSidebarWidth = useBlockSidebarWidthStore((s) => s.setWidth);
  const [settingsDragWidth, setSettingsDragWidth] = useState<number | null>(
    null,
  );
  const isResizingSettings = settingsDragWidth !== null;
  const expandedSettingsWidth = settingsDragWidth ?? committedSettingsWidth;
  // The settings panel lives in the (CSS-hidden but still mounted) Editor tab,
  // so only reserve its column there — otherwise it'd float over Browser/Run.
  const settingsColumnActive = settingsOpen && tab === "editor";
  const settingsColumnWidth = !settingsColumnActive
    ? 0
    : settingsCollapsed
      ? STUDIO_COPILOT_RAIL_WIDTH
      : expandedSettingsWidth;
  // The body is interactive only on the Editor tab while expanded, the rail only
  // while collapsed there. Mark the other (still-mounted, clipped) subtree inert
  // so keyboard/screen-reader users can't reach off-screen controls.
  const settingsBodyInert = !(settingsColumnActive && !settingsCollapsed);
  const settingsRailInert = !(settingsColumnActive && settingsCollapsed);

  // Teardown for an in-progress drag, invoked on drop AND on unmount, so the
  // window listeners + body cursor lock never leak if the shell unmounts mid-drag.
  const settingsDragTeardownRef = useRef<(() => void) | null>(null);
  useEffect(() => () => settingsDragTeardownRef.current?.(), []);

  const handleSettingsResizePointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = committedSettingsWidth;
      // Dragging left (toward the Stage) widens the right-docked panel.
      const widthAt = (clientX: number) =>
        clampSettingsWidth(startWidth + startX - clientX);
      const previousCursor = document.body.style.cursor;
      const previousUserSelect = document.body.style.userSelect;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      const handleMove = (moveEvent: PointerEvent) => {
        setSettingsDragWidth(widthAt(moveEvent.clientX));
      };
      function handleUp(upEvent?: PointerEvent) {
        window.removeEventListener("pointermove", handleMove);
        window.removeEventListener("pointerup", handleUp);
        document.body.style.cursor = previousCursor;
        document.body.style.userSelect = previousUserSelect;
        settingsDragTeardownRef.current = null;
        if (upEvent) {
          setBlockSidebarWidth(widthAt(upEvent.clientX));
        }
        setSettingsDragWidth(null);
      }
      settingsDragTeardownRef.current = () => handleUp();
      window.addEventListener("pointermove", handleMove);
      window.addEventListener("pointerup", handleUp);
    },
    [committedSettingsWidth, setBlockSidebarWidth],
  );

  const handleSettingsResizeKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        setBlockSidebarWidth(
          clampSettingsWidth(committedSettingsWidth + SETTINGS_RESIZE_STEP),
        );
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        setBlockSidebarWidth(
          clampSettingsWidth(committedSettingsWidth - SETTINGS_RESIZE_STEP),
        );
      }
    },
    [committedSettingsWidth, setBlockSidebarWidth],
  );

  return (
    <StudioShellContext.Provider value={shellContextValue}>
      <div className="flex h-full w-full flex-col">
        <StudioTopBar />
        <div
          className="grid min-h-0 flex-1"
          style={{
            gridTemplateColumns: `${copilotWidth}px minmax(0, 1fr) ${settingsColumnWidth}px`,
            gridTemplateRows: "minmax(0, 1fr)",
            // Dropped mid-drag so a settings resize tracks the handle 1:1.
            transition: isResizingSettings
              ? "none"
              : `grid-template-columns ${STUDIO_COPILOT_TRANSITION_MS}ms ${
                  copilotCollapsed || settingsCollapsed
                    ? STUDIO_COPILOT_COLLAPSE_EASE
                    : STUDIO_COPILOT_TRANSITION_EASE
                }`,
          }}
        >
          {/* Copilot portal target. Kept mounted (parked offscreen) when
              collapsed so an in-flight Copilot stream isn't torn down. */}
          <div className="relative h-full min-w-0 overflow-hidden">
            {/* Fixed width so the chat doesn't reflow; the widening column clip
                reveals it left→right, so no opacity fade is needed. */}
            <div
              ref={setCopilotPortalEl}
              style={{ width: STUDIO_COPILOT_WIDTH }}
              className={cn(
                "h-full py-3 pl-3",
                copilotCollapsed && "pointer-events-none",
              )}
            />
            {/* Rail renders only while collapsed and unmounts immediately on
                expand, so the collapsed UI can't linger over the content the
                widening column is revealing (the open-flicker). */}
            {copilotCollapsed ? (
              <div
                style={{ width: STUDIO_COPILOT_RAIL_WIDTH }}
                className="absolute left-0 top-0 h-full py-3 pl-3 duration-300 animate-in fade-in"
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
          {/* Settings portal target. The agent/block settings panel is portaled
              in here from FlowRenderer (it needs React Flow context), so it lives
              as a grid column that reflows the Stage like the Copilot does. The
              inner target is fixed at the expanded width and right-pinned so the
              open/collapse clip never reflows the panel body. */}
          <div className="relative h-full min-w-0 overflow-hidden">
            {/* Expanded panel: fixed width, right-pinned, clipped by the column.
                pr-3 mirrors the Copilot's outer inset (gap to the window edge). */}
            <div
              ref={setSettingsPortalEl}
              style={{ width: expandedSettingsWidth }}
              className="absolute inset-y-0 right-0 py-3 pr-3"
              // inert removes the clipped body from a11y tree + tab order when
              // it's hidden (off the Editor tab, or collapsed under the rail).
              {...(settingsBodyInert ? { inert: "" } : {})}
            />
            {/* Collapsed rail: a self-contained card overlay (mirrors CopilotRail).
                Kept mounted so its portal target is stable; only interactive while
                collapsed so it never blocks the expanded panel underneath. */}
            <div
              style={{ width: STUDIO_COPILOT_RAIL_WIDTH }}
              className="pointer-events-none absolute inset-y-0 right-0 py-3 pr-3"
            >
              <div
                ref={setSettingsRailPortalEl}
                className={cn(
                  "h-full w-full",
                  settingsRailInert
                    ? "pointer-events-none"
                    : "pointer-events-auto",
                )}
                {...(settingsRailInert ? { inert: "" } : {})}
              />
            </div>
            {settingsColumnActive && !settingsCollapsed ? (
              <div
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize settings"
                aria-valuenow={expandedSettingsWidth}
                aria-valuemin={BLOCK_SIDEBAR_WIDTH_MIN}
                aria-valuemax={BLOCK_SIDEBAR_WIDTH_MAX}
                tabIndex={0}
                onPointerDown={handleSettingsResizePointerDown}
                onKeyDown={handleSettingsResizeKeyDown}
                className="absolute inset-y-0 left-0 z-40 w-3 cursor-col-resize focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            ) : null}
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
