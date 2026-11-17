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
    expect(greedyPaneOf(["ember", "browser", "editor"])).toBe("browser");
    expect(greedyPaneOf(["browser"])).toBe("browser");
  });

  test("editor takes over when the browser is closed", () => {
    expect(greedyPaneOf(["ember", "editor", "overview"])).toBe("editor");
  });

  test("no greedy pane without browser or editor", () => {
    expect(greedyPaneOf(["ember", "overview"])).toBeUndefined();
    expect(greedyPaneOf([])).toBeUndefined();
  });
});

describe("paneResizable", () => {
  test("every pane except the greedy one can hold a pinned width", () => {
    const panes: StudioPaneId[] = ["ember", "editor", "browser", "overview"];
    expect(paneResizable("ember", panes)).toBe(true);
    expect(paneResizable("editor", panes)).toBe(true);
    expect(paneResizable("overview", panes)).toBe(true);
    expect(paneResizable("browser", panes)).toBe(false);
  });

  test("without a greedy pane the last pane flexes instead of pinning", () => {
    const panes: StudioPaneId[] = ["ember", "overview"];
    expect(paneResizable("ember", panes)).toBe(true);
    expect(paneResizable("overview", panes)).toBe(false);
  });
});

describe("paneFlex", () => {
  const noWidths = {};

  test("built-agent panes use 25/45/30 of usable width", () => {
    const panes: StudioPaneId[] = ["ember", "browser", "editor"];
    expect(paneFlex("browser", panes, noWidths)).toBe("1 1 0%");
    expect(paneFlex("ember", panes, noWidths)).toBe("0 1 calc(25% - 6px)");
    expect(paneFlex("editor", panes, noWidths)).toBe("0 1 calc(30% - 7.2px)");
  });

  test("blank-agent panes use 30/70 even after reordering", () => {
    for (const panes of [
      ["ember", "browser"],
      ["browser", "ember"],
    ] as StudioPaneId[][]) {
      expect(paneFlex("ember", panes, noWidths)).toBe("0 1 calc(30% - 3.6px)");
      expect(paneFlex("browser", panes, noWidths)).toBe("1 1 0%");
    }
  });

  test("reordered built-agent panes keep ratios and pinned widths win", () => {
    const panes: StudioPaneId[] = ["editor", "browser", "ember"];
    expect(paneFlex("editor", panes, {})).toBe("0 1 calc(30% - 7.2px)");
    expect(paneFlex("ember", panes, { editor: 480 })).toBe(
      "0 1 calc(25% - 6px)",
    );
    expect(paneFlex("editor", panes, { editor: 480 })).toBe("0 1 480px");
  });

  test("editor becomes the greedy pane when the browser is closed", () => {
    const panes: StudioPaneId[] = ["ember", "editor", "overview"];
    expect(paneFlex("editor", panes, noWidths)).toBe("1 1 0%");
    expect(paneFlex("ember", panes, noWidths)).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("with neither greedy pane open the remaining panes share equally", () => {
    const panes: StudioPaneId[] = ["ember", "overview"];
    expect(paneFlex("ember", panes, noWidths)).toBe(
      `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
    expect(paneFlex("overview", panes, noWidths)).toBe(
      `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("a pinned width pins the pane; the greedy pane ignores its pin", () => {
    const panes: StudioPaneId[] = ["ember", "browser"];
    const widths = { ember: 412, browser: 900 };
    expect(paneFlex("ember", panes, widths)).toBe("0 1 412px");
    expect(paneFlex("browser", panes, widths)).toBe("1 1 0%");
  });

  test("without a greedy pane the last pane ignores its pin so the row fills", () => {
    const panes: StudioPaneId[] = ["ember", "overview"];
    const widths = { ember: 350, timeline: 500 };
    expect(paneFlex("ember", panes, widths)).toBe("0 1 350px");
    expect(paneFlex("overview", panes, widths)).toBe(
      `1 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("a two-pane default gives the side pane ~35% and the browser the rest", () => {
    expect(paneFlex("editor", ["editor", "browser"], noWidths)).toBe(
      "0 1 calc(35% - 4.2px)",
    );
    expect(paneFlex("browser", ["editor", "browser"], noWidths)).toBe("1 1 0%");
    expect(paneFlex("overview", ["browser", "overview"], noWidths)).toBe(
      "0 1 calc(35% - 4.2px)",
    );
    expect(paneFlex("browser", ["browser", "overview"], noWidths)).toBe(
      "1 1 0%",
    );
  });

  test("the ~35% side split holds for any greedy two-pane row, not just the factory combos", () => {
    expect(paneFlex("editor", ["ember", "editor"], noWidths)).toBe("1 1 0%");
    expect(paneFlex("ember", ["ember", "editor"], noWidths)).toBe(
      "0 1 calc(35% - 4.2px)",
    );
  });

  test("a third pane reverts the side panes to the fixed default", () => {
    const panes: StudioPaneId[] = ["editor", "browser", "overview"];
    expect(paneFlex("editor", panes, noWidths)).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
    expect(paneFlex("overview", panes, noWidths)).toBe(
      `0 1 ${STUDIO_PANE_DEFAULT_WIDTH}px`,
    );
  });

  test("a pinned width still wins over the two-pane default", () => {
    expect(paneFlex("editor", ["editor", "browser"], { editor: 480 })).toBe(
      "0 1 480px",
    );
  });

  test("invalid widths fall back to the default", () => {
    const panes: StudioPaneId[] = ["ember", "browser", "editor"];
    expect(paneFlex("ember", panes, { ember: Number.NaN })).toBe(
      "0 1 calc(25% - 6px)",
    );
    expect(paneFlex("ember", panes, { ember: -50 })).toBe(
      "0 1 calc(25% - 6px)",
    );
  });
});

describe("clampResizeDelta", () => {
  const left = { id: "ember" as const, width: 400 };
  const right = { id: "browser" as const, width: 600 };

  test("passes through deltas that keep both panes above their mins", () => {
    expect(clampResizeDelta(50, left, right)).toBe(50);
    expect(clampResizeDelta(-50, left, right)).toBe(-50);
  });

  test("clamps against the left pane's min width", () => {
    expect(clampResizeDelta(-500, left, right)).toBe(
      STUDIO_PANE_MIN_WIDTH.ember - 400,
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
    expect(paneWidthsKey({ editor: 400, ember: 300 })).toBe(
      paneWidthsKey({ ember: 300, editor: 400 }),
    );
    expect(paneWidthsKey({ ember: 300 })).not.toBe(
      paneWidthsKey({ ember: 301 }),
    );
    expect(paneWidthsKey({})).toBe("");
  });
});

describe("movePaneTo / movePaneBy", () => {
  const panes: StudioPaneId[] = ["ember", "editor", "browser"];

  test("dragged pane takes the target pane's slot (arrayMove semantics)", () => {
    expect(movePaneTo(panes, "ember", "browser")).toEqual([
      "editor",
      "browser",
      "ember",
    ]);
    expect(movePaneTo(panes, "browser", "ember")).toEqual([
      "browser",
      "ember",
      "editor",
    ]);
  });

  test("dropping on itself or on an unknown pane is a no-op", () => {
    expect(movePaneTo(panes, "ember", "ember")).toEqual(panes);
    expect(movePaneTo(panes, "overview", "ember")).toEqual(panes);
    expect(movePaneTo(panes, "ember", "overview")).toEqual(panes);
  });

  test("moves one slot left or right, clamped at the row edges", () => {
    expect(movePaneBy(panes, "editor", -1)).toEqual([
      "editor",
      "ember",
      "browser",
    ]);
    expect(movePaneBy(panes, "editor", 1)).toEqual([
      "ember",
      "browser",
      "editor",
    ]);
    expect(movePaneBy(panes, "ember", -1)).toEqual(panes);
    expect(movePaneBy(panes, "browser", 1)).toEqual(panes);
  });
});
