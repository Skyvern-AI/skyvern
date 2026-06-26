import { describe, it, expect } from "vitest";
import { HINT_REGISTRY } from "./contextualHints";

describe("HINT_REGISTRY", () => {
  it("contains the three first-cut hints", () => {
    expect(HINT_REGISTRY.map((h) => h.id)).toEqual([
      "add-another-block",
      "run-recording",
      "start-template",
    ]);
  });

  it("has unique ids and unique seen keys", () => {
    const ids = new Set(HINT_REGISTRY.map((h) => h.id));
    const keys = new Set(HINT_REGISTRY.map((h) => h.seenKey));
    expect(ids.size).toBe(HINT_REGISTRY.length);
    expect(keys.size).toBe(HINT_REGISTRY.length);
  });

  it("matches both the build and edit editor paths", () => {
    const block = HINT_REGISTRY.find((h) => h.id === "add-another-block")!;
    expect(block.matchRoute("/workflows/wpid_123/build")).toBe(true);
    expect(block.matchRoute("/workflows/wpid_123/edit")).toBe(true);
    expect(block.matchRoute("/workflows")).toBe(false);
    expect(block.matchRoute("/workflows/wpid_123/runs")).toBe(false);
  });

  it("matches the runs and workflows list routes exactly", () => {
    const run = HINT_REGISTRY.find((h) => h.id === "run-recording")!;
    const tmpl = HINT_REGISTRY.find((h) => h.id === "start-template")!;
    expect(run.matchRoute("/runs")).toBe(true);
    expect(run.matchRoute("/runs/abc")).toBe(false);
    expect(tmpl.matchRoute("/workflows")).toBe(true);
    expect(tmpl.matchRoute("/workflows/wpid_1/edit")).toBe(false);
  });

  it("gates the template hint on no first save", () => {
    const tmpl = HINT_REGISTRY.find((h) => h.id === "start-template")!;
    const base = {
      tour_completed_at: "2026-06-01T00:00:00Z",
      modal_dismissed_at: null,
      first_save_at: null,
      first_run_at: null,
      ab_variant: null,
      user_intent: null,
      seen_canvas: null,
      seen_node_adder: null,
      seen_sidebar: null,
      seen_save_run: null,
    };
    expect(tmpl.prerequisite(base)).toBe(true);
    expect(
      tmpl.prerequisite({ ...base, first_save_at: "2026-06-01T00:00:00Z" }),
    ).toBe(false);
  });
});
