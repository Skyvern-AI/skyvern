import { describe, expect, test } from "vitest";

import {
  DEFAULT_STUDIO_PANES,
  STUDIO_STAGE_GAP_PX,
  STUDIO_STAGE_PADDING_PX,
  STUDIO_PANE_MIN_WIDTH,
  defaultPanesForWorkflowState,
  fitPanesToWidth,
  panesFitWidth,
  panesFromDeepLink,
  panesListEqual,
  parsePanesParam,
  resolveOpenPanes,
  searchWithPanes,
  togglePane,
  withPaneClosed,
  withPaneOpen,
  withPanesOpen,
} from "./panes";

describe("parsePanesParam", () => {
  test("returns null when the param is absent", () => {
    expect(parsePanesParam(null)).toBeNull();
  });

  test("treats an empty value as an explicit empty list", () => {
    expect(parsePanesParam("")).toEqual([]);
  });

  test("preserves order", () => {
    expect(parsePanesParam("run,copilot,editor")).toEqual([
      "run",
      "copilot",
      "editor",
    ]);
  });

  test("drops unknown values", () => {
    expect(parsePanesParam("copilot,bogus,browser")).toEqual([
      "copilot",
      "browser",
    ]);
  });

  test("drops duplicates, keeping the first occurrence's position", () => {
    expect(parsePanesParam("browser,copilot,browser")).toEqual([
      "browser",
      "copilot",
    ]);
  });

  test("tolerates whitespace around entries", () => {
    expect(parsePanesParam(" copilot , browser ")).toEqual([
      "copilot",
      "browser",
    ]);
  });
});

describe("panesFromDeepLink", () => {
  test("a run deep link opens the Run pane", () => {
    expect(
      panesFromDeepLink({ runId: "wr_123", active: null, blockLabel: null }),
    ).toEqual(["run"]);
  });

  test("a pinned-item deep link opens the Run pane", () => {
    expect(
      panesFromDeepLink({ runId: null, active: "act_1", blockLabel: null }),
    ).toEqual(["run"]);
  });

  test("a block run opens Run and Browser together", () => {
    expect(
      panesFromDeepLink({
        runId: "wr_123",
        active: null,
        blockLabel: "block_1",
      }),
    ).toEqual(["run", "browser"]);
  });

  test("a block label without a run does not force the Run pane", () => {
    expect(
      panesFromDeepLink({ runId: null, active: null, blockLabel: "block_1" }),
    ).toEqual([...DEFAULT_STUDIO_PANES]);
  });

  test("no deep link falls back to the default panes", () => {
    expect(
      panesFromDeepLink({ runId: null, active: null, blockLabel: null }),
    ).toEqual(["copilot", "browser"]);
  });
});

describe("resolveOpenPanes", () => {
  test("no params resolves to the default panes", () => {
    expect(resolveOpenPanes("")).toEqual(["copilot", "browser"]);
  });

  test("?wr= resolves to the Run pane", () => {
    expect(resolveOpenPanes("?wr=wr_123")).toEqual(["run"]);
  });

  test("?wr= plus ?bl= resolves to Run and Browser", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1")).toEqual([
      "run",
      "browser",
    ]);
  });

  test("?active= resolves to the Run pane", () => {
    expect(resolveOpenPanes("?active=act_1")).toEqual(["run"]);
  });

  test("an explicit ?panes= wins over the deep-link params", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1&panes=copilot")).toEqual([
      "copilot",
    ]);
  });

  test("an explicit empty ?panes= wins over the deep-link params", () => {
    expect(resolveOpenPanes("?wr=wr_123&panes=")).toEqual([]);
  });

  test("unrelated params do not affect the default", () => {
    expect(resolveOpenPanes("?embed=true&cache-key-value=x")).toEqual([
      "copilot",
      "browser",
    ]);
  });
});

describe("defaultPanesForWorkflowState", () => {
  test("an agent with runs starts on Copilot + Browser", () => {
    expect(
      defaultPanesForWorkflowState({ hasRuns: true, hasBlocks: true }),
    ).toEqual(["copilot", "browser"]);
  });

  test("a never-run agent starts on Copilot + Editor", () => {
    expect(
      defaultPanesForWorkflowState({ hasRuns: false, hasBlocks: true }),
    ).toEqual(["copilot", "editor"]);
  });

  test("a known runs signal outranks the blocks heuristic", () => {
    expect(
      defaultPanesForWorkflowState({ hasRuns: true, hasBlocks: false }),
    ).toEqual(["copilot", "browser"]);
  });

  test("unknown runs falls back to blocks: an empty agent starts on the editor", () => {
    expect(
      defaultPanesForWorkflowState({ hasRuns: undefined, hasBlocks: false }),
    ).toEqual(["copilot", "editor"]);
  });

  test("unknown runs with blocks keeps the legacy default", () => {
    expect(
      defaultPanesForWorkflowState({ hasRuns: undefined, hasBlocks: true }),
    ).toEqual([...DEFAULT_STUDIO_PANES]);
  });
});

describe("resolveOpenPanes with custom defaults", () => {
  test("no params resolves to the given defaults", () => {
    expect(resolveOpenPanes("", ["copilot", "editor"])).toEqual([
      "copilot",
      "editor",
    ]);
  });

  test("deep links are unaffected by custom defaults", () => {
    expect(resolveOpenPanes("?wr=wr_123", ["copilot", "editor"])).toEqual([
      "run",
    ]);
    expect(
      resolveOpenPanes("?wr=wr_123&bl=block_1", ["copilot", "editor"]),
    ).toEqual(["run", "browser"]);
  });

  test("an explicit ?panes= is never overridden by custom defaults", () => {
    expect(resolveOpenPanes("?panes=browser", ["copilot", "editor"])).toEqual([
      "browser",
    ]);
    expect(resolveOpenPanes("?panes=", ["copilot", "editor"])).toEqual([]);
  });
});

describe("panesListEqual", () => {
  test("compares contents and order", () => {
    expect(panesListEqual(["copilot", "browser"], ["copilot", "browser"])).toBe(
      true,
    );
    expect(panesListEqual(["copilot", "browser"], ["browser", "copilot"])).toBe(
      false,
    );
    expect(panesListEqual(["copilot"], ["copilot", "browser"])).toBe(false);
    expect(panesListEqual([], [])).toBe(true);
  });
});

describe("panesFitWidth", () => {
  const width = (panes: Parameters<typeof panesFitWidth>[0]) =>
    panes.reduce((sum, id) => sum + STUDIO_PANE_MIN_WIDTH[id], 0) +
    STUDIO_STAGE_PADDING_PX +
    STUDIO_STAGE_GAP_PX * (panes.length - 1);

  test("an empty list always fits", () => {
    expect(panesFitWidth([], 0)).toBe(true);
  });

  test("fits exactly at the min-width sum plus stage chrome", () => {
    const panes = ["copilot", "browser"] as const;
    expect(panesFitWidth(panes, width(panes))).toBe(true);
    expect(panesFitWidth(panes, width(panes) - 1)).toBe(false);
  });
});

describe("fitPanesToWidth", () => {
  test("returns the list unchanged when everything fits", () => {
    expect(
      fitPanesToWidth(["copilot", "editor", "browser", "run"], 2000),
    ).toEqual(["copilot", "editor", "browser", "run"]);
  });

  test("keeps the longest leading prefix that fits", () => {
    // copilot(260) + editor(220) + padding(24) + gap(12) = 516 fits in 600;
    // adding browser(260) + gap(12) = 788 does not.
    expect(
      fitPanesToWidth(["copilot", "editor", "browser", "run"], 600),
    ).toEqual(["copilot", "editor"]);
  });

  test("degrades deterministically by order, not by pane size", () => {
    expect(fitPanesToWidth(["run", "copilot", "browser"], 600)).toEqual([
      "run",
      "copilot",
    ]);
  });

  test("always keeps the first pane even when nothing fits", () => {
    expect(fitPanesToWidth(["browser", "run"], 100)).toEqual(["browser"]);
  });

  test("keeps an empty list empty", () => {
    expect(fitPanesToWidth([], 100)).toEqual([]);
  });
});

describe("pane list operations", () => {
  test("toggling a closed pane appends it (click order)", () => {
    expect(togglePane(["copilot", "browser"], "run")).toEqual([
      "copilot",
      "browser",
      "run",
    ]);
  });

  test("toggling an open pane splices it out, preserving the others' order", () => {
    expect(togglePane(["copilot", "run", "browser"], "run")).toEqual([
      "copilot",
      "browser",
    ]);
  });

  test("withPaneOpen is a no-op re-order-wise when already open", () => {
    expect(withPaneOpen(["run", "copilot"], "copilot")).toEqual([
      "run",
      "copilot",
    ]);
  });

  test("withPanesOpen appends only the missing panes, in the given order", () => {
    expect(withPanesOpen(["copilot", "browser"], ["run", "browser"])).toEqual([
      "copilot",
      "browser",
      "run",
    ]);
  });

  test("withPaneClosed removes the pane", () => {
    expect(withPaneClosed(["copilot", "browser"], "copilot")).toEqual([
      "browser",
    ]);
  });
});

describe("searchWithPanes", () => {
  test("adds ?panes= to an empty search, with readable commas", () => {
    expect(searchWithPanes("", ["copilot", "browser"])).toBe(
      "?panes=copilot,browser",
    );
  });

  test("preserves unrelated params and replaces an existing ?panes=", () => {
    const next = searchWithPanes("?wr=wr_1&panes=run", ["run", "editor"]);
    const params = new URLSearchParams(next);
    expect(params.get("wr")).toBe("wr_1");
    expect(params.get("panes")).toBe("run,editor");
  });

  test("serializes an empty list as an explicit empty value", () => {
    const params = new URLSearchParams(searchWithPanes("?wr=wr_1", []));
    expect(params.get("panes")).toBe("");
    expect(params.get("wr")).toBe("wr_1");
  });

  test("round-trips through resolveOpenPanes", () => {
    const next = searchWithPanes("?wr=wr_1", ["browser", "copilot"]);
    expect(resolveOpenPanes(next)).toEqual(["browser", "copilot"]);
  });
});
