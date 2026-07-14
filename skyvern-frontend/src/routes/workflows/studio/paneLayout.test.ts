import { describe, expect, test } from "vitest";

import {
  clampResizeDelta,
  greedyPaneOf,
  movePaneBy,
  movePaneTo,
  paneFlex,
  paneResizable,
  paneWidthsKey,
  STUDIO_PANE_DEFAULT_WIDTH,
} from "./paneLayout";
import { STUDIO_PANE_MIN_WIDTH, type StudioPaneId } from "./panes";

describe("greedyPaneOf", () => {
  test("browser wins whenever it is open", () => {
    expect(greedyPaneOf(["copilot", "browser", "editor"])).toBe("browser");
    expect(greedyPaneOf(["browser"])).toBe("browser");
  });

  test("editor takes over when the browser is closed", () => {
    expect(greedyPaneOf(["copilot", "editor", "overview"])).toBe("editor");
  });

  test("no greedy pane without browser or editor", () => {
    expect(greedyPaneOf(["copilot", "overview"])).toBeUndefined();
    expect(greedyPaneOf([])).toBeUndefined();
  });
});

describe("paneResizable", () => {
  test("every pane except the greedy one can hold a pinned width", () => {
    const panes: StudioPaneId[] = ["copilot", "editor", "browser", "overview"];
    expect(paneResizable("copilot", panes)).toBe(true);
    expect(paneResizable("editor", panes)).toBe(true);
    expect(paneResizable("overview", panes)).toBe(true);
    expect(paneResizable("browser", panes)).toBe(false);
  });

  test("without a greedy pane the last pane flexes instead of pinning", () => {
    const panes: StudioPaneId[] = ["copilot", "overview"];
    expect(paneResizable("copilot", panes)).toBe(true);
    expect(paneResizable("overview", panes)).toBe(false);
  });
});

describe("paneFlex", () => {
  const noWidths = {};

  test("browser takes all remaining space; others sit at the default width", () => {
    const panes: StudioPaneId[] = ["copilot", "browser", "editor"];
    expect(paneFlex("browser", panes, noWidths)).toBe("1 1 0%");
    expect(paneFlex("copilot", panes, noWidths)).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
    expect(paneFlex("editor", panes, noWidths)).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("editor becomes the greedy pane when the browser is closed", () => {
    const panes: StudioPaneId[] = ["copilot", "editor", "overview"];
    expect(paneFlex("editor", panes, noWidths)).toBe("1 1 0%");
    expect(paneFlex("copilot", panes, noWidths)).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("with neither greedy pane open the remaining panes share equally", () => {
    const panes: StudioPaneId[] = ["copilot", "overview"];
    expect(paneFlex("copilot", panes, noWidths)).toBe(
      `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
    expect(paneFlex("overview", panes, noWidths)).toBe(
      `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("a pinned width pins the pane; the greedy pane ignores its pin", () => {
    const panes: StudioPaneId[] = ["copilot", "browser"];
    const widths = { copilot: 412, browser: 900 };
    expect(paneFlex("copilot", panes, widths)).toBe("0 1 412px");
    expect(paneFlex("browser", panes, widths)).toBe("1 1 0%");
  });

  test("without a greedy pane the last pane ignores its pin so the row fills", () => {
    const panes: StudioPaneId[] = ["copilot", "overview"];
    const widths = { copilot: 350, timeline: 500 };
    expect(paneFlex("copilot", panes, widths)).toBe("0 1 350px");
    expect(paneFlex("overview", panes, widths)).toBe(
      `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("garbage persisted widths fall back to the default", () => {
    const panes: StudioPaneId[] = ["copilot", "browser"];
    expect(paneFlex("copilot", panes, { copilot: Number.NaN })).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
    expect(paneFlex("copilot", panes, { copilot: -50 })).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });
});

describe("clampResizeDelta", () => {
  const left = { id: "copilot" as const, width: 400 };
  const right = { id: "browser" as const, width: 600 };

  test("passes through deltas that keep both panes above their mins", () => {
    expect(clampResizeDelta(50, left, right)).toBe(50);
    expect(clampResizeDelta(-50, left, right)).toBe(-50);
  });

  test("clamps against the left pane's min width", () => {
    expect(clampResizeDelta(-500, left, right)).toBe(
      STUDIO_PANE_MIN_WIDTH.copilot - 400,
    );
  });

  test("clamps against the right pane's min width", () => {
    expect(clampResizeDelta(500, left, right)).toBe(
      600 - STUDIO_PANE_MIN_WIDTH.browser,
    );
  });
});

describe("paneWidthsKey", () => {
  test("is stable across key order and changes with any width", () => {
    expect(paneWidthsKey({ editor: 400, copilot: 300 })).toBe(
      paneWidthsKey({ copilot: 300, editor: 400 }),
    );
    expect(paneWidthsKey({ copilot: 300 })).not.toBe(
      paneWidthsKey({ copilot: 301 }),
    );
    expect(paneWidthsKey({})).toBe("");
  });
});

describe("movePaneTo / movePaneBy", () => {
  const panes: StudioPaneId[] = ["copilot", "editor", "browser"];

  test("dragged pane takes the target pane's slot (arrayMove semantics)", () => {
    expect(movePaneTo(panes, "copilot", "browser")).toEqual([
      "editor",
      "browser",
      "copilot",
    ]);
    expect(movePaneTo(panes, "browser", "copilot")).toEqual([
      "browser",
      "copilot",
      "editor",
    ]);
  });

  test("dropping on itself or on an unknown pane is a no-op", () => {
    expect(movePaneTo(panes, "copilot", "copilot")).toEqual(panes);
    expect(movePaneTo(panes, "overview", "copilot")).toEqual(panes);
    expect(movePaneTo(panes, "copilot", "overview")).toEqual(panes);
  });

  test("moves one slot left or right, clamped at the row edges", () => {
    expect(movePaneBy(panes, "editor", -1)).toEqual([
      "editor",
      "copilot",
      "browser",
    ]);
    expect(movePaneBy(panes, "editor", 1)).toEqual([
      "copilot",
      "browser",
      "editor",
    ]);
    expect(movePaneBy(panes, "copilot", -1)).toEqual(panes);
    expect(movePaneBy(panes, "browser", 1)).toEqual(panes);
  });
});
