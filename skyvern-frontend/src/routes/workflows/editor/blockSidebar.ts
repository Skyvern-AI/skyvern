import type { WorkflowEditorMode } from "./hooks/useWorkflowEditorMode";

export const BLOCK_SIDEBAR_WIDTH_VAR = "--block-sidebar-w";
export const BLOCK_SIDEBAR_RIGHT_GAP = "1.5rem";
export const BLOCK_SIDEBAR_HORIZONTAL_GUTTER = "3rem";
const DEFAULT_ROOT_FONT_SIZE_PX = 16;

export const HEADER_RIGHT_INSET_CLOSED = "right-6";
export const HEADER_RIGHT_INSET_OPEN =
  "right-[calc(var(--block-sidebar-w)+3rem)]";

function parseRemValue(value: string): number | null {
  if (!value.endsWith("rem")) {
    return null;
  }

  const rem = Number.parseFloat(value);
  return Number.isFinite(rem) && rem > 0 ? rem : null;
}

export function getBlockSidebarGutterPx(element: HTMLElement | null): number {
  const rem = parseRemValue(BLOCK_SIDEBAR_HORIZONTAL_GUTTER) ?? 3;
  // The CSS inset uses rems; derive the numeric Resizable gutter from the
  // active root font size so browser/user scaling keeps both paths in sync.
  const ownerDocument =
    element?.ownerDocument ??
    (typeof document === "undefined" ? null : document);
  const rootFontSize = ownerDocument?.defaultView?.getComputedStyle(
    ownerDocument.documentElement,
  ).fontSize;
  const rootFontSizePx = Number.parseFloat(rootFontSize ?? "");

  return (
    rem *
    (Number.isFinite(rootFontSizePx)
      ? rootFontSizePx
      : DEFAULT_ROOT_FONT_SIZE_PX)
  );
}

export function getContainedBlockSidebarWidth(
  widthPx: number,
  containerWidthPx: number | null,
  gutterPx: number = getBlockSidebarGutterPx(null),
): number {
  const roundedWidth = Math.max(0, Math.round(widthPx));
  if (containerWidthPx === null || containerWidthPx <= 0) {
    return roundedWidth;
  }

  // Parent shell clips overflow, so keep persisted widths contained before
  // they reach re-resizable (which only supports numeric px constraints).
  const containedMaxWidth = Math.max(0, containerWidthPx - gutterPx);
  return Math.min(roundedWidth, containedMaxWidth);
}

export function isBlockSidebarOpen(
  mode: WorkflowEditorMode,
  selectedBlockId: string | null,
  // The right rail also renders when the workflow panel is open on the
  // nodeLibrary content (NodeAdder click → "+ block" library). Without
  // this, the header/right-side overlays would anchor to `right-6`
  // while the library is visibly open and slide under the panel.
  // Build mode has no block-config sidebar, so only the library counts.
  isNodeLibraryOpen: boolean = false,
): boolean {
  if (mode === "build") return isNodeLibraryOpen;
  return selectedBlockId !== null || isNodeLibraryOpen;
}
