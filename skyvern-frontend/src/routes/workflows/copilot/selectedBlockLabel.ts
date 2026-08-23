import { SELECTED_BLOCK_SEARCH_PARAM } from "@/routes/workflows/editor/hooks/useSelectedBlockUrlSync";

// Read at send time so the label is always the live selection; the URL is the
// source of truth (useSelectedBlockUrlSync mirrors the canvas selection there).
// Falls back to the router-visible search under a memory router (tests).
export function readSelectedBlockLabel(routerSearch?: string): string | null {
  const search = window.location.search || routerSearch || "";
  const label = new URLSearchParams(search).get(SELECTED_BLOCK_SEARCH_PARAM);
  return label && label.trim() !== "" ? label : null;
}
