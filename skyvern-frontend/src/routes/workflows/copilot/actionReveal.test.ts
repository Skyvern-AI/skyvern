import { describe, expect, it } from "vitest";

import { buildRevealOffsets, revealedCountAt } from "./actionReveal";

describe("buildRevealOffsets", () => {
  it("clamps a duration below the floor up to 180ms", () => {
    expect(buildRevealOffsets([10])).toEqual([180]);
  });

  it("clamps a duration above the ceiling down to 900ms", () => {
    expect(buildRevealOffsets([5000])).toEqual([900]);
  });

  it("defaults a null duration to 350ms", () => {
    expect(buildRevealOffsets([null])).toEqual([350]);
  });

  it("returns cumulative offsets for a mix of durations", () => {
    expect(buildRevealOffsets([200, 300, null])).toEqual([200, 500, 850]);
  });

  it("scales the whole schedule down to the 6s cap once the clamped total exceeds it", () => {
    const durations = new Array(30).fill(1000);
    const offsets = buildRevealOffsets(durations);
    expect(offsets).toHaveLength(30);
    expect(offsets[offsets.length - 1]).toBe(6000);
    // Each duration clamps to 900ms before scaling; scaling down a uniform
    // input yields a uniform step, so every offset is an even multiple.
    expect(offsets[0]).toBe(200);
    expect(offsets[1]).toBe(400);
  });

  it("does not scale when the clamped total is under the cap", () => {
    const offsets = buildRevealOffsets([900, 900, 900]);
    expect(offsets).toEqual([900, 1800, 2700]);
  });

  it("returns an empty schedule for no actions", () => {
    expect(buildRevealOffsets([])).toEqual([]);
  });
});

describe("revealedCountAt", () => {
  const offsets = [200, 400, 600];

  it("reveals nothing at elapsed 0", () => {
    expect(revealedCountAt(offsets, 0)).toBe(0);
  });

  it("reveals the rows whose offset has passed, mid-schedule", () => {
    expect(revealedCountAt(offsets, 300)).toBe(1);
  });

  it("reveals everything once elapsed reaches the final offset", () => {
    expect(revealedCountAt(offsets, 600)).toBe(3);
  });

  it("reveals everything once elapsed exceeds the total", () => {
    expect(revealedCountAt(offsets, 10_000)).toBe(3);
  });

  it("reveals nothing for negative elapsed", () => {
    expect(revealedCountAt(offsets, -50)).toBe(0);
  });
});
