export function getSelectedRunFrameId({
  pinnedFrameId,
  running,
  showingScreenshots = false,
  debugStreamInBrowserPane = false,
  lastFrameId,
}: {
  pinnedFrameId: string | null;
  running: boolean;
  showingScreenshots?: boolean;
  debugStreamInBrowserPane?: boolean;
  lastFrameId: string | null;
}): string | null {
  const selectedId =
    running && !showingScreenshots
      ? (pinnedFrameId ?? "stream")
      : pinnedFrameId === "stream"
        ? lastFrameId
        : (pinnedFrameId ?? lastFrameId);

  if (selectedId === "stream" && debugStreamInBrowserPane) {
    return lastFrameId;
  }
  return selectedId;
}
