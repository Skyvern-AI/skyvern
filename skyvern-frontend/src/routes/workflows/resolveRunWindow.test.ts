import { describe, expect, it } from "vitest";

import { resolveRunWindow } from "./resolveRunWindow";

const NOW = new Date("2026-06-08T12:00:00.000Z");

describe("resolveRunWindow", () => {
  it("returns an empty window when no period is set (pure-OSS default)", () => {
    expect(resolveRunWindow(new URLSearchParams(), NOW)).toEqual({});
  });

  it("maps a preset to a midnight-snapped start with no upper bound", () => {
    expect(resolveRunWindow(new URLSearchParams("period=7d"), NOW)).toEqual({
      createdAtStart: "2026-06-01T00:00:00.000Z",
    });
  });

  it("maps 30d to 30 calendar days (table approximation of the billing period)", () => {
    expect(resolveRunWindow(new URLSearchParams("period=30d"), NOW)).toEqual({
      createdAtStart: "2026-05-09T00:00:00.000Z",
    });
  });

  it("resolves a valid custom range to inclusive whole-day bounds", () => {
    expect(
      resolveRunWindow(
        new URLSearchParams("period=custom&from=2026-05-01&to=2026-05-03"),
        NOW,
      ),
    ).toEqual({
      createdAtStart: "2026-05-01T00:00:00.000Z",
      createdAtEnd: "2026-05-04T00:00:00.000Z",
    });
  });

  it("returns an empty window for an invalid custom range", () => {
    expect(
      resolveRunWindow(
        new URLSearchParams("period=custom&from=2026-05-10&to=2026-05-01"),
        NOW,
      ),
    ).toEqual({});
  });

  it("returns an empty window for an unknown preset", () => {
    expect(resolveRunWindow(new URLSearchParams("period=14d"), NOW)).toEqual(
      {},
    );
  });
});
