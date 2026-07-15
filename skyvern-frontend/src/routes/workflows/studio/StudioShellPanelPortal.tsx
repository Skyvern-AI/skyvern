import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { useStudioShellContext } from "./StudioShellContext";

/**
 * Renders a Workspace-wired panel into the shell's stage-level overlay target,
 * so it stays visible (and dismissable) even while the Editor pane — the
 * panel's data owner — is display:none. Dismiss: backdrop click, Esc, and
 * whatever ✕ the panel itself renders.
 */
export function StudioShellPanelPortal({
  open,
  onDismiss,
  children,
}: {
  open: boolean;
  onDismiss: () => void;
  children: ReactNode;
}) {
  const { panelPortalEl } = useStudioShellContext();

  useEffect(() => {
    if (!open) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onDismiss();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onDismiss]);

  if (!open || !panelPortalEl) {
    return null;
  }
  return createPortal(
    <>
      <div
        className="pointer-events-auto absolute inset-0"
        onClick={onDismiss}
      />
      <div className="pointer-events-auto absolute right-3 top-3 max-h-[calc(100%-1.5rem)] overflow-y-auto">
        {children}
      </div>
    </>,
    panelPortalEl,
  );
}
