import { describe, expect, test } from "vitest";

import {
  DEFAULT_STUDIO_PANES,
  RUN_APPEND_PANES,
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
    expect(parsePanesParam("timeline,copilot,editor")).toEqual([
      "timeline",
      "copilot",
      "editor",
    ]);
  });

  test("accepts the pre-rename 'run' alias as timeline, in place", () => {
    expect(parsePanesParam("copilot,run,browser")).toEqual([
      "copilot",
      "timeline",
      "browser",
    ]);
  });

  test("dedupes the alias against its canonical id", () => {
    expect(parsePanesParam("run,timeline")).toEqual(["timeline"]);
    expect(parsePanesParam("timeline,run")).toEqual(["timeline"]);
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
  test("a run deep link opens watch-and-review: Copilot, Browser, Timeline", () => {
    expect(
      panesFromDeepLink({ runId: "wr_123", active: null, blockLabel: null }),
    ).toEqual(["copilot", "browser", "timeline"]);
  });

  test("an ?active= deep link opens the same run layout", () => {
    expect(
      panesFromDeepLink({ runId: null, active: "act_1", blockLabel: null }),
    ).toEqual(["copilot", "browser", "timeline"]);
  });

  test("a block run opens iterate: Editor, Browser, Timeline", () => {
    expect(
      panesFromDeepLink({
        runId: "wr_123",
        active: null,
        blockLabel: "block_1",
      }),
    ).toEqual(["editor", "browser", "timeline"]);
  });

  test("a block label without a run does not force the run layout", () => {
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

  test("?wr= resolves to Copilot, Browser and Timeline", () => {
    expect(resolveOpenPanes("?wr=wr_123")).toEqual([
      "copilot",
      "browser",
      "timeline",
    ]);
  });

  test("?wr= plus ?bl= resolves to Editor, Browser and Timeline", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1")).toEqual([
      "editor",
      "browser",
      "timeline",
    ]);
  });

  test("?active= resolves like a run deep link", () => {
    expect(resolveOpenPanes("?active=act_1")).toEqual([
      "copilot",
      "browser",
      "timeline",
    ]);
  });

  test("an explicit ?panes= wins over the deep-link params", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1&panes=copilot")).toEqual([
      "copilot",
    ]);
  });

  test("an explicit ?panes=run keeps working as the Timeline pane", () => {
    expect(resolveOpenPanes("?panes=run")).toEqual(["timeline"]);
    expect(resolveOpenPanes("?wr=wr_123&panes=copilot,run")).toEqual([
      "copilot",
      "timeline",
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
  test("an empty agent starts on prompt-and-watch: Copilot + Browser", () => {
    expect(defaultPanesForWorkflowState({ hasBlocks: false })).toEqual([
      "copilot",
      "browser",
    ]);
  });

  test("a built agent adds the Editor: Copilot + Browser + Editor", () => {
    expect(defaultPanesForWorkflowState({ hasBlocks: true })).toEqual([
      "copilot",
      "browser",
      "editor",
    ]);
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
      "copilot",
      "browser",
      "timeline",
    ]);
    expect(
      resolveOpenPanes("?wr=wr_123&bl=block_1", ["copilot", "editor"]),
    ).toEqual(["editor", "browser", "timeline"]);
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
      fitPanesToWidth(["copilot", "editor", "browser", "timeline"], 2000),
    ).toEqual(["copilot", "editor", "browser", "timeline"]);
  });

  test("keeps the longest leading prefix that fits", () => {
    // copilot(260) + editor(220) + padding(24) + gap(12) = 516 fits in 600;
    // adding browser(260) + gap(12) = 788 does not.
    expect(
      fitPanesToWidth(["copilot", "editor", "browser", "timeline"], 600),
    ).toEqual(["copilot", "editor"]);
  });

  test("degrades deterministically by order, not by pane size", () => {
    expect(fitPanesToWidth(["timeline", "copilot", "browser"], 600)).toEqual([
      "timeline",
      "copilot",
    ]);
  });

  test("always keeps the first pane even when nothing fits", () => {
    expect(fitPanesToWidth(["browser", "timeline"], 100)).toEqual(["browser"]);
  });

  test("keeps an empty list empty", () => {
    expect(fitPanesToWidth([], 100)).toEqual([]);
  });
});

describe("pane list operations", () => {
  test("toggling a closed pane appends it (click order)", () => {
    expect(togglePane(["copilot", "browser"], "timeline")).toEqual([
      "copilot",
      "browser",
      "timeline",
    ]);
  });

  test("toggling an open pane splices it out, preserving the others' order", () => {
    expect(togglePane(["copilot", "timeline", "browser"], "timeline")).toEqual([
      "copilot",
      "browser",
    ]);
  });

  test("withPaneOpen is a no-op re-order-wise when already open", () => {
    expect(withPaneOpen(["timeline", "copilot"], "copilot")).toEqual([
      "timeline",
      "copilot",
    ]);
  });

  test("withPanesOpen appends only the missing panes, in the given order", () => {
    expect(
      withPanesOpen(["copilot", "browser"], ["timeline", "browser"]),
    ).toEqual(["copilot", "browser", "timeline"]);
  });

  test("withPaneClosed removes the pane", () => {
    expect(withPaneClosed(["copilot", "browser"], "copilot")).toEqual([
      "browser",
    ]);
  });
});

describe("in-app run starts append the run surfaces (continuity rule)", () => {
  test("block ▶ from the workflow layout appends Timeline only", () => {
    expect(
      withPanesOpen(["copilot", "browser", "editor"], RUN_APPEND_PANES),
    ).toEqual(["copilot", "browser", "editor", "timeline"]);
  });

  test("a run start from a Browser-less layout appends Browser then Timeline", () => {
    expect(withPanesOpen(["copilot", "editor"], RUN_APPEND_PANES)).toEqual([
      "copilot",
      "editor",
      "browser",
      "timeline",
    ]);
  });

  test("a run start from the run layout changes nothing", () => {
    expect(
      withPanesOpen(["copilot", "browser", "timeline"], RUN_APPEND_PANES),
    ).toEqual(["copilot", "browser", "timeline"]);
  });

  test("a run start from an empty stage opens just the run surfaces", () => {
    expect(withPanesOpen([], RUN_APPEND_PANES)).toEqual([
      "browser",
      "timeline",
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
    const next = searchWithPanes("?wr=wr_1&panes=timeline", [
      "timeline",
      "editor",
    ]);
    const params = new URLSearchParams(next);
    expect(params.get("wr")).toBe("wr_1");
    expect(params.get("panes")).toBe("timeline,editor");
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

  test("an aliased ?panes=run re-serializes canonically after a write", () => {
    const parsed = parsePanesParam("run");
    expect(parsed).toEqual(["timeline"]);
    expect(
      searchWithPanes("?panes=run", withPaneOpen(parsed!, "copilot")),
    ).toBe("?panes=timeline,copilot");
  });
});
