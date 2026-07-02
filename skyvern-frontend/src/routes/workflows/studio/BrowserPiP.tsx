import {
  ChevronDownIcon,
  EnterFullScreenIcon,
  GlobeIcon,
} from "@radix-ui/react-icons";
import { useParams } from "react-router-dom";

import { useStudioShellStore } from "@/store/StudioShellStore";
import { cn } from "@/util/utils";

import { useDebugSessionQuery } from "../hooks/useDebugSessionQuery";
import { useStudioShellContext } from "./StudioShellContext";
import { usePresence } from "./usePresence";
import { useStudioPanes } from "./useStudioPanes";

/**
 * Picture-in-picture preview of the persistent debug browser. The stream node
 * lives in the shell and is re-parented into this slot, so it never re-boots.
 */
export function BrowserPiP() {
  const minimized = useStudioShellStore((s) => s.pipMinimized);
  const setMinimized = useStudioShellStore((s) => s.setPipMinimized);
  const { openPane } = useStudioPanes();
  const { setEditorStreamSlot } = useStudioShellContext();

  const { workflowPermanentId } = useParams();
  // The session is owned (fetched) by the editor's Workspace; read it passively.
  const { data: debugSession } = useDebugSessionQuery({
    workflowPermanentId,
    enabled: false,
  });
  const sessionId = debugSession?.browser_session_id ?? null;
  const live = Boolean(sessionId);
  const stateLabel = live ? "live" : "starting";

  // Keep the outgoing element mounted briefly so it can animate out.
  const pillPresent = usePresence(minimized, 150);
  const cardPresent = usePresence(!minimized, 150);

  return (
    <>
      {pillPresent ? (
        <button
          type="button"
          onClick={() => setMinimized(false)}
          title="Show live browser"
          className={cn(
            "absolute bottom-4 right-4 z-30 inline-flex items-center gap-2 rounded-full border border-border bg-slate-elevation1 px-3 py-2 text-xs text-foreground shadow-lg duration-150 hover:bg-slate-elevation2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            minimized
              ? "animate-in fade-in zoom-in-95"
              : "animate-out fade-out zoom-out-95",
          )}
        >
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              live ? "animate-pulse bg-success" : "bg-muted-foreground/50",
            )}
          />
          <GlobeIcon className="h-3.5 w-3.5" /> Live Browser
          <span className="text-muted-foreground/70">· {stateLabel}</span>
        </button>
      ) : null}

      {cardPresent ? (
        <div
          className={cn(
            "absolute bottom-4 right-4 z-30 flex w-[22rem] flex-col overflow-hidden rounded-lg border bg-slate-elevation1 shadow-lg duration-150",
            live ? "border-studio-accent/50" : "border-border",
            minimized
              ? "animate-out fade-out zoom-out-95"
              : "animate-in fade-in zoom-in-95",
          )}
        >
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <span
              className={cn(
                "h-2 w-2 rounded-full",
                live ? "animate-pulse bg-success" : "bg-muted-foreground/50",
              )}
            />
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
              <GlobeIcon className="h-3.5 w-3.5" /> Live Browser
            </span>
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground/70">
              {stateLabel}
            </span>
            <div className="flex-1" />
            <button
              type="button"
              onClick={() => openPane("browser")}
              aria-label="Expand to Browser pane"
              title="Expand to Browser pane"
              className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <EnterFullScreenIcon className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => setMinimized(true)}
              title="Minimize"
              aria-label="Minimize live browser"
              className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <ChevronDownIcon className="h-4 w-4" />
            </button>
          </div>
          <button
            type="button"
            onClick={() => openPane("browser")}
            title="Expand to Browser pane"
            aria-label="Expand to Browser pane"
            className="relative block h-[12rem] w-full bg-slate-elevation2 text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
          >
            {sessionId ? (
              <div ref={setEditorStreamSlot} className="absolute inset-0" />
            ) : (
              <span className="flex h-full flex-col items-center justify-center gap-1 text-center text-muted-foreground">
                <GlobeIcon className="h-6 w-6" />
                <b className="text-sm font-semibold text-foreground">
                  Starting browser…
                </b>
                <i className="text-xs not-italic text-muted-foreground/70">
                  Your live browser is warming up
                </i>
              </span>
            )}
          </button>
        </div>
      ) : null}
    </>
  );
}
