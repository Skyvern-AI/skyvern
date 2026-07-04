import { describe, expect, test } from "vitest";

import {
  DEFAULT_STUDIO_PANES,
  DELETED_WORKFLOW_BLOCKED_PANES,
  RUN_APPEND_PANES,
  STUDIO_STAGE_GAP_PX,
  STUDIO_STAGE_PADDING_PX,
  STUDIO_PANE_MIN_WIDTH,
  defaultPanesForWorkflowState,
  fitPanesToWidth,
  panesFitWidth,
  panesFromDeepLink,
  panesListEqual,
  panesWithoutDeletedBlocked,
  parsePanesParam,
  resolveOpenPanes,
  searchWithPanes,
  toReadableSearch,
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
      "overview",
      "copilot",
      "editor",
    ]);
  });

  test("accepts the pre-rename 'run' alias as overview, in place", () => {
    expect(parsePanesParam("copilot,run,browser")).toEqual([
      "copilot",
      "overview",
      "browser",
    ]);
  });

  test("dedupes the alias against its canonical id", () => {
    expect(parsePanesParam("run,timeline")).toEqual(["overview"]);
    expect(parsePanesParam("timeline,run")).toEqual(["overview"]);
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
  test("a run deep link opens watch-and-review: Copilot, Browser, Overview", () => {
    expect(
      panesFromDeepLink({ runId: "wr_123", active: null, blockLabel: null }),
    ).toEqual(["copilot", "browser", "overview"]);
  });

  test("an ?active= deep link opens the same run layout", () => {
    expect(
      panesFromDeepLink({ runId: null, active: "act_1", blockLabel: null }),
    ).toEqual(["copilot", "browser", "overview"]);
  });

  test("a block run opens iterate: Editor, Browser, Overview", () => {
    expect(
      panesFromDeepLink({
        runId: "wr_123",
        active: null,
        blockLabel: "block_1",
      }),
    ).toEqual(["editor", "browser", "overview"]);
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

  test("?wr= resolves to Copilot, Browser and Overview", () => {
    expect(resolveOpenPanes("?wr=wr_123")).toEqual([
      "copilot",
      "browser",
      "overview",
    ]);
  });

  test("?wr= plus ?bl= resolves to Editor, Browser and Overview", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1")).toEqual([
      "editor",
      "browser",
      "overview",
    ]);
  });

  test("?active= resolves like a run deep link", () => {
    expect(resolveOpenPanes("?active=act_1")).toEqual([
      "copilot",
      "browser",
      "overview",
    ]);
  });

  test("an explicit ?panes= wins over the deep-link params", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1&panes=copilot")).toEqual([
      "copilot",
    ]);
  });

  test("an explicit ?panes=run keeps working as the Overview pane", () => {
    expect(resolveOpenPanes("?panes=run")).toEqual(["overview"]);
    expect(resolveOpenPanes("?wr=wr_123&panes=copilot,run")).toEqual([
      "copilot",
      "overview",
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
      "overview",
    ]);
    expect(
      resolveOpenPanes("?wr=wr_123&bl=block_1", ["copilot", "editor"]),
    ).toEqual(["editor", "browser", "overview"]);
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
      fitPanesToWidth(["copilot", "editor", "browser", "overview"], 2000),
    ).toEqual(["copilot", "editor", "browser", "overview"]);
  });

  test("keeps the longest leading prefix that fits", () => {
    // copilot(260) + editor(220) + padding(24) + gap(12) = 516 fits in 600;
    // adding browser(260) + gap(12) = 788 does not.
    expect(
      fitPanesToWidth(["copilot", "editor", "browser", "overview"], 600),
    ).toEqual(["copilot", "editor"]);
  });

  test("degrades deterministically by order, not by pane size", () => {
    expect(fitPanesToWidth(["overview", "copilot", "browser"], 600)).toEqual([
      "overview",
      "copilot",
    ]);
  });

  test("always keeps the first pane even when nothing fits", () => {
    expect(fitPanesToWidth(["browser", "overview"], 100)).toEqual(["browser"]);
  });

  test("keeps an empty list empty", () => {
    expect(fitPanesToWidth([], 100)).toEqual([]);
  });
});

describe("pane list operations", () => {
  test("toggling a closed pane appends it (click order)", () => {
    expect(togglePane(["copilot", "browser"], "overview")).toEqual([
      "copilot",
      "browser",
      "overview",
    ]);
  });

  test("toggling an open pane splices it out, preserving the others' order", () => {
    expect(togglePane(["copilot", "overview", "browser"], "overview")).toEqual([
      "copilot",
      "browser",
    ]);
  });

  test("withPaneOpen is a no-op re-order-wise when already open", () => {
    expect(withPaneOpen(["overview", "copilot"], "copilot")).toEqual([
      "overview",
      "copilot",
    ]);
  });

  test("withPanesOpen appends only the missing panes, in the given order", () => {
    expect(
      withPanesOpen(["copilot", "browser"], ["overview", "browser"]),
    ).toEqual(["copilot", "browser", "overview"]);
  });

  test("withPaneClosed removes the pane", () => {
    expect(withPaneClosed(["copilot", "browser"], "copilot")).toEqual([
      "browser",
    ]);
  });
});

describe("toReadableSearch", () => {
  test("keeps ?panes= commas readable after an unrelated param write", () => {
    const params = new URLSearchParams("?panes=copilot,browser");
    params.set("active", "act_1");
    expect(toReadableSearch(params)).toBe(
      "?panes=copilot,browser&active=act_1",
    );
  });

  test("leaves non-comma encoding intact", () => {
    const params = new URLSearchParams("?panes=copilot,browser&bl=Block A");
    expect(toReadableSearch(params)).toBe("?panes=copilot,browser&bl=Block+A");
  });

  test("returns an empty string when no params remain", () => {
    expect(toReadableSearch(new URLSearchParams())).toBe("");
  });
});

describe("block-run starts append the run surfaces (continuity rule)", () => {
  // Full-run starts RESET to the ?wr= mapping instead (RunWorkflowForm
  // navigates to the bare deep link); only block ▶ appends.
  test("block ▶ from the workflow layout appends Overview only", () => {
    expect(
      withPanesOpen(["copilot", "browser", "editor"], RUN_APPEND_PANES),
    ).toEqual(["copilot", "browser", "editor", "overview"]);
  });

  test("a block ▶ from a Browser-less layout appends Browser then Overview", () => {
    expect(withPanesOpen(["copilot", "editor"], RUN_APPEND_PANES)).toEqual([
      "copilot",
      "editor",
      "browser",
      "overview",
    ]);
  });

  test("a block ▶ from the run layout changes nothing", () => {
    expect(
      withPanesOpen(["copilot", "browser", "overview"], RUN_APPEND_PANES),
    ).toEqual(["copilot", "browser", "overview"]);
  });

  test("a block ▶ from an empty stage opens just the run surfaces", () => {
    expect(withPanesOpen([], RUN_APPEND_PANES)).toEqual([
      "browser",
      "overview",
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
      "overview",
      "editor",
    ]);
    const params = new URLSearchParams(next);
    expect(params.get("wr")).toBe("wr_1");
    expect(params.get("panes")).toBe("overview,editor");
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
    expect(parsed).toEqual(["overview"]);
    expect(
      searchWithPanes("?panes=run", withPaneOpen(parsed!, "copilot")),
    ).toBe("?panes=overview,copilot");
  });
});

describe("panesWithoutDeletedBlocked", () => {
  test("drops the workflow-mutating panes and keeps run-view order", () => {
    expect(
      panesWithoutDeletedBlocked(["copilot", "browser", "overview", "editor"]),
    ).toEqual(["browser", "overview"]);
  });

  test("is a no-op for an already-viewing-only list", () => {
    expect(panesWithoutDeletedBlocked(["browser", "overview"])).toEqual([
      "browser",
      "overview",
    ]);
  });

  test("blocks exactly the copilot and editor panes", () => {
    expect(DELETED_WORKFLOW_BLOCKED_PANES).toEqual(["copilot", "editor"]);
  });
});
