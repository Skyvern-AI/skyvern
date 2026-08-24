// Frame-timing marks for the CDP screencast viewer. Ring buffer, no React, no network.
// Read in DevTools with window.__skyvernStreamStats(); enable a 5 s console report with
// localStorage.setItem("skyvern:stream-debug", "1").
//
// commit is marked when the frame's React state is scheduled, not when the browser paints,
// so parseToCommit reads low and commitToLoad reads high by the same amount; messageToLoad,
// which is the number the two of them are there to decompose, is unaffected.
const CAPACITY = 300;

type Frame = { message: number; commit?: number; load?: number };
const frames = new Map<number, Frame>();
const order: number[] = [];
let nextToken = 1;

function remember(token: number, frame: Frame) {
  frames.set(token, frame);
  order.push(token);
  while (order.length > CAPACITY) {
    frames.delete(order.shift() as number);
  }
}

export function markMessage(): number {
  const token = nextToken++;
  remember(token, { message: performance.now() });
  return token;
}

export function markCommit(token: number): void {
  const f = frames.get(token);
  if (f && f.commit === undefined) f.commit = performance.now();
}

export function markLoad(token: number): void {
  const f = frames.get(token);
  if (f && f.load === undefined) f.load = performance.now();
}

function percentiles(values: number[]) {
  if (values.length === 0) return { p50: null, p90: null };
  const sorted = [...values].sort((a, b) => a - b);
  const at = (q: number) =>
    sorted[
      Math.min(sorted.length - 1, Math.floor(q * (sorted.length - 1) + 0.5))
    ];
  return { p50: at(0.5), p90: at(0.9) };
}

export function snapshot() {
  const complete = order
    .map((t) => frames.get(t))
    .filter(
      (f): f is Required<Frame> =>
        !!f && f.commit !== undefined && f.load !== undefined,
    );
  return {
    frames: complete.length,
    parseToCommitMs: percentiles(complete.map((f) => f.commit - f.message)),
    commitToLoadMs: percentiles(complete.map((f) => f.load - f.commit)),
    messageToLoadMs: percentiles(complete.map((f) => f.load - f.message)),
  };
}

export function resetStreamStats(): void {
  frames.clear();
  order.length = 0;
  nextToken = 1;
}

export function startDebugReport(): () => void {
  if (typeof window === "undefined") return () => {};
  (
    window as unknown as { __skyvernStreamStats: typeof snapshot }
  ).__skyvernStreamStats = snapshot;
  if (window.localStorage?.getItem("skyvern:stream-debug") !== "1")
    return () => {};
  const id = window.setInterval(
    () => console.debug("[stream-stats]", snapshot()),
    5000,
  );
  return () => window.clearInterval(id);
}
