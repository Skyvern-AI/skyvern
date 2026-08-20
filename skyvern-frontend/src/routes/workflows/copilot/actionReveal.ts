// Pure scheduling math for replaying a code block's recorded actions one by
// one instead of dumping them all at once. Kept dependency-free so the
// reveal count can be derived fresh from wall-clock time on every render —
// no per-row timers to restart on collapse/expand or StrictMode remount.
const MIN_STEP_MS = 180;
const MAX_STEP_MS = 900;
const DEFAULT_STEP_MS = 350;
const MAX_TOTAL_MS = 6000;

// Cumulative end-offset (ms from reveal start) at which each action should
// be showing as done. Each duration is clamped to a sane per-step range,
// then the whole schedule is scaled down if it would exceed the total cap.
export function buildRevealOffsets(
  durations: ReadonlyArray<number | null>,
): number[] {
  const clamped = durations.map((duration) =>
    duration == null
      ? DEFAULT_STEP_MS
      : Math.min(MAX_STEP_MS, Math.max(MIN_STEP_MS, duration)),
  );
  const total = clamped.reduce((sum, step) => sum + step, 0);
  const scale = total > MAX_TOTAL_MS ? MAX_TOTAL_MS / total : 1;

  let cumulative = 0;
  return clamped.map((step) => {
    cumulative += step * scale;
    return cumulative;
  });
}

// How many actions have finished revealing by `elapsedMs`. A row is done
// once elapsed reaches its offset, so the count lands exactly at the total
// when elapsed reaches the schedule's end.
export function revealedCountAt(
  offsets: readonly number[],
  elapsedMs: number,
): number {
  if (elapsedMs < 0) return 0;
  let count = 0;
  for (const offset of offsets) {
    if (elapsedMs < offset) break;
    count += 1;
  }
  return count;
}
