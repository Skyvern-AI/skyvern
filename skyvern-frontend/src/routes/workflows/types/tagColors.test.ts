import { describe, expect, it } from "vitest";
import type { TagValue } from "./tagTypes";
import {
  TAG_COLOR_PALETTE,
  buildTagColorMap,
  isPaletteColorName,
  paletteDotClass,
  randomPaletteColor,
  tagColorFor,
  tagColorKey,
} from "./tagColors";

// The palette is the frontend mirror of the backend TAG_COLOR_PALETTE in
// skyvern/forge/sdk/workflow/models/validators.py — names AND order must match
// (order is the random-pick pool). Any drift would render an uncolored chip or
// let the editor offer a swatch the backend would 422.
const BACKEND_PALETTE = [
  "gray",
  "red",
  "orange",
  "amber",
  "yellow",
  "green",
  "teal",
  "blue",
  "cyan",
  "indigo",
  "purple",
  "pink",
];

describe("tag color palette", () => {
  it("mirrors the backend palette names and order exactly", () => {
    expect([...TAG_COLOR_PALETTE]).toEqual(BACKEND_PALETTE);
  });

  it("recognizes palette names and rejects non-members", () => {
    expect(isPaletteColorName("blue")).toBe(true);
    expect(isPaletteColorName("BLUE")).toBe(false);
    expect(isPaletteColorName("chartreuse")).toBe(false);
    expect(isPaletteColorName(undefined)).toBe(false);
    expect(isPaletteColorName(null)).toBe(false);
  });

  it("returns the solid dot class for a palette name and '' otherwise", () => {
    expect(paletteDotClass("green")).toBe("bg-green-500");
    expect(paletteDotClass("not-a-color")).toBe("");
    expect(paletteDotClass(null)).toBe("");
    expect(paletteDotClass(undefined)).toBe("");
  });

  it("always picks a palette member at random", () => {
    for (let i = 0; i < 50; i++) {
      expect(isPaletteColorName(randomPaletteColor())).toBe(true);
    }
  });
});

describe("buildTagColorMap", () => {
  it("joins (key, value) rows and drops out-of-palette colors", () => {
    const rows: Array<TagValue> = [
      { key: "env", value: "prod", color: "blue" },
      { key: "env", value: "staging", color: "amber" },
      { key: "team", value: "growth", color: "not-a-color" },
    ];
    const map = buildTagColorMap(rows);
    expect(map.get(tagColorKey("env", "prod"))).toBe("blue");
    expect(map.get(tagColorKey("env", "staging"))).toBe("amber");
    expect(map.has(tagColorKey("team", "growth"))).toBe(false);
  });

  it("does not resolve prototype keys (real Map, not object)", () => {
    const map = buildTagColorMap([
      { key: "constructor", value: "x", color: "red" },
    ]);
    expect(map.get(tagColorKey("toString", "x"))).toBeUndefined();
    expect(map.get(tagColorKey("constructor", "x"))).toBe("red");
  });
});

describe("tagColorFor", () => {
  const map = buildTagColorMap([{ key: "env", value: "prod", color: "blue" }]);

  it("returns the color for a grouped tag", () => {
    expect(tagColorFor(map, "env", "prod")).toBe("blue");
  });

  it("never colors a standalone label (null key)", () => {
    expect(tagColorFor(map, null, "prod")).toBeUndefined();
  });

  it("returns undefined when the map is absent or has no entry", () => {
    expect(tagColorFor(undefined, "env", "prod")).toBeUndefined();
    expect(tagColorFor(map, "env", "dev")).toBeUndefined();
  });
});
