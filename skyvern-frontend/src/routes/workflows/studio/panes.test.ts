import { describe, expect, test } from "vitest";

import {
  DEFAULT_STUDIO_PANES,
  RUN_APPEND_PANES,
  STUDIO_STAGE_GAP_PX,
  STUDIO_STAGE_PADDING_PX,
  STUDIO_PANE_MIN_WIDTH,
  WORKFLOW_AUTHORING_PANES,
  emberBrowserFocusSessionId,
  fitPanesToWidth,
  isAuthoringLayout,
  panesCollapsedForDoc,
  panesFitWidth,
  panesFromDeepLink,
  panesListEqual,
  panesWithoutDeletedBlocked,
  parsePanesParam,
  rememberPaneSlots,
  resolveOpenPanes,
  searchWithRunReference,
  toReadableSearch,
  togglePane,
  withPaneClosed,
  withPaneOpen,
  withPanesOpen,
  type StudioPaneId,
} from "./panes";

describe("parsePanesParam", () => {
  test("returns null when the param is absent", () => {
    expect(parsePanesParam(null)).toBeNull();
  });

  test("treats an empty value as an explicit empty list", () => {
    expect(parsePanesParam("")).toEqual([]);
  });

  test("preserves order", () => {
    expect(parsePanesParam("timeline,ember,editor")).toEqual([
      "overview",
      "ember",
      "editor",
    ]);
  });

  test("accepts the pre-rename 'run' alias as overview, in place", () => {
    expect(parsePanesParam("ember,run,browser")).toEqual([
      "ember",
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
      "ember",
      "browser",
    ]);
  });

  test("drops duplicates, keeping the first occurrence's position", () => {
    expect(parsePanesParam("browser,ember,browser")).toEqual([
      "browser",
      "ember",
    ]);
  });

  test("accepts the pre-rename copilot id as ember, forever", () => {
    expect(parsePanesParam("copilot,editor")).toEqual(["ember", "editor"]);
    expect(parsePanesParam("copilot,ember")).toEqual(["ember"]);
  });

  test("tolerates whitespace around entries", () => {
    expect(parsePanesParam(" copilot , browser ")).toEqual([
      "ember",
      "browser",
    ]);
  });
});

describe("panesFromDeepLink", () => {
  test("a run deep link opens Timeline on the left, Browser on the right", () => {
    expect(
      panesFromDeepLink({ runId: "wr_123", active: null, blockLabel: null }),
    ).toEqual(["overview", "browser"]);
  });

  test("an ?active= deep link opens the same run layout", () => {
    expect(
      panesFromDeepLink({ runId: null, active: "act_1", blockLabel: null }),
    ).toEqual(["overview", "browser"]);
  });

  test("a cold block run opens Overview and Browser", () => {
    expect(
      panesFromDeepLink({
        runId: "wr_123",
        active: null,
        blockLabel: "block_1",
      }),
    ).toEqual(["overview", "browser"]);
  });

  test("a block label without a run does not force the run layout", () => {
    expect(
      panesFromDeepLink({ runId: null, active: null, blockLabel: "block_1" }),
    ).toEqual([...DEFAULT_STUDIO_PANES]);
  });

  test("no deep link (Edit) opens only Ember and Editor", () => {
    expect(
      panesFromDeepLink({ runId: null, active: null, blockLabel: null }),
    ).toEqual(["ember", "editor"]);
  });
});

describe("resolveOpenPanes", () => {
  test("no params resolves to the default panes", () => {
    expect(resolveOpenPanes("")).toEqual(["ember", "editor"]);
  });

  test("?wr= resolves to Overview and Browser", () => {
    expect(resolveOpenPanes("?wr=wr_123")).toEqual(["overview", "browser"]);
  });

  test("?wr= plus ?bl= resolves to Overview and Browser", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1")).toEqual([
      "overview",
      "browser",
    ]);
  });

  test("?active= resolves like a run deep link", () => {
    expect(resolveOpenPanes("?active=act_1")).toEqual(["overview", "browser"]);
  });

  test("an explicit ?panes= wins over the deep-link params", () => {
    expect(resolveOpenPanes("?wr=wr_123&bl=block_1&panes=ember")).toEqual([
      "ember",
    ]);
  });

  test("an explicit ?panes=run keeps working as the Overview pane", () => {
    expect(resolveOpenPanes("?panes=run")).toEqual(["overview"]);
    expect(resolveOpenPanes("?wr=wr_123&panes=ember,run")).toEqual([
      "ember",
      "overview",
    ]);
  });

  test("an explicit empty ?panes= wins over the deep-link params", () => {
    expect(resolveOpenPanes("?wr=wr_123&panes=")).toEqual([]);
  });

  test("a link to the removed code view still opens the Overview pane", () => {
    expect(resolveOpenPanes("?view=code&panes=ember,editor")).toEqual([
      "overview",
      "ember",
      "editor",
    ]);
  });

  test("unrelated params do not affect the default", () => {
    expect(resolveOpenPanes("?cache-key-value=x")).toEqual(["ember", "editor"]);
  });
});

describe("resolveOpenPanes with custom defaults", () => {
  test("no params resolves to the given defaults", () => {
    expect(resolveOpenPanes("", ["ember", "editor"])).toEqual([
      "ember",
      "editor",
    ]);
  });

  test("deep links are unaffected by custom defaults", () => {
    expect(resolveOpenPanes("?wr=wr_123", ["ember", "editor"])).toEqual([
      "overview",
      "browser",
    ]);
    expect(
      resolveOpenPanes("?wr=wr_123&bl=block_1", ["ember", "editor"]),
    ).toEqual(["overview", "browser"]);
  });

  test("an explicit ?panes= is never overridden by custom defaults", () => {
    expect(resolveOpenPanes("?panes=browser", ["ember", "editor"])).toEqual([
      "browser",
    ]);
    expect(resolveOpenPanes("?panes=", ["ember", "editor"])).toEqual([]);
  });
});

describe("panesListEqual", () => {
  test("compares contents and order", () => {
    expect(panesListEqual(["ember", "browser"], ["ember", "browser"])).toBe(
      true,
    );
    expect(panesListEqual(["ember", "browser"], ["browser", "ember"])).toBe(
      false,
    );
    expect(panesListEqual(["ember"], ["ember", "browser"])).toBe(false);
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
    const panes = ["ember", "browser"] as const;
    expect(panesFitWidth(panes, width(panes))).toBe(true);
    expect(panesFitWidth(panes, width(panes) - 1)).toBe(false);
  });
});

describe("panesCollapsedForDoc", () => {
  const four = ["ember", "editor", "overview", "browser"] as const;

  test("collapses nothing when the Doc keeps 360px", () => {
    expect(panesCollapsedForDoc(four, 1440)).toEqual([]);
    expect(panesCollapsedForDoc(["ember", "browser"], 300)).toEqual([]);
  });

  test("the Browser gives way first, then Run, then Ember; never the Editor", () => {
    // Ember 260 + doc 360 + run 260 + 2 gaps + padding = 928; a strip adds 48.
    expect(panesCollapsedForDoc(four, 1100)).toEqual(["browser"]);
    expect(panesCollapsedForDoc(four, 976)).toEqual(["browser"]);
    expect(panesCollapsedForDoc(four, 975)).toEqual(["browser", "overview"]);
    expect(panesCollapsedForDoc(four, 200)).toEqual([
      "browser",
      "overview",
      "ember",
    ]);
  });
});

describe("fitPanesToWidth", () => {
  test("returns the list unchanged when everything fits", () => {
    expect(
      fitPanesToWidth(["ember", "editor", "browser", "overview"], 2000),
    ).toEqual(["ember", "editor", "browser", "overview"]);
  });

  test("keeps the longest leading prefix that fits", () => {
    // Ember(260) + editor(220) + padding(24) + gap(12) = 516 fits in 600;
    // adding browser(260) + gap(12) = 788 does not.
    expect(
      fitPanesToWidth(["ember", "editor", "browser", "overview"], 600),
    ).toEqual(["ember", "editor"]);
  });

  test("degrades deterministically by order, not by pane size", () => {
    expect(fitPanesToWidth(["overview", "ember", "browser"], 600)).toEqual([
      "overview",
      "ember",
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
    expect(togglePane(["ember", "browser"], "overview")).toEqual([
      "ember",
      "browser",
      "overview",
    ]);
  });

  test("toggling an open pane splices it out, preserving the others' order", () => {
    expect(togglePane(["ember", "overview", "browser"], "overview")).toEqual([
      "ember",
      "browser",
    ]);
  });

  test("closed panes keep their slots through later closes and reopen in any order", () => {
    let panes: StudioPaneId[] = ["ember", "editor", "browser"];
    let slots: StudioPaneId[] = [...panes];
    const apply = (next: StudioPaneId[]) => {
      panes = next;
      slots = rememberPaneSlots(slots, next);
    };

    apply(withPaneClosed(panes, "editor"));
    apply(withPaneClosed(panes, "browser"));
    apply(withPaneOpen(panes, "browser", slots));
    apply(withPaneOpen(panes, "editor", slots));
    expect(panes).toEqual(["ember", "editor", "browser"]);

    apply(withPaneClosed(panes, "ember"));
    apply(withPaneClosed(panes, "editor"));
    apply(withPaneOpen(panes, "editor", slots));
    apply(withPaneOpen(panes, "ember", slots));
    expect(panes).toEqual(["ember", "editor", "browser"]);
  });

  test("withPaneOpen is a no-op re-order-wise when already open", () => {
    expect(withPaneOpen(["overview", "ember"], "ember")).toEqual([
      "overview",
      "ember",
    ]);
  });

  test("withPanesOpen appends only the missing panes, in the given order", () => {
    expect(
      withPanesOpen(["ember", "browser"], ["overview", "browser"]),
    ).toEqual(["ember", "browser", "overview"]);
  });

  test("withPaneClosed removes the pane", () => {
    expect(withPaneClosed(["ember", "browser"], "ember")).toEqual(["browser"]);
  });
});

describe("toReadableSearch", () => {
  test("keeps ?panes= commas readable after an unrelated param write", () => {
    const params = new URLSearchParams("?panes=ember,browser");
    params.set("active", "act_1");
    expect(toReadableSearch(params)).toBe("?panes=ember,browser&active=act_1");
  });

  test("leaves non-comma encoding intact", () => {
    const params = new URLSearchParams("?panes=ember,browser&bl=Block A");
    expect(toReadableSearch(params)).toBe("?panes=ember,browser&bl=Block+A");
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
      withPanesOpen(["ember", "browser", "editor"], RUN_APPEND_PANES),
    ).toEqual(["ember", "browser", "editor", "overview"]);
  });

  test("a block ▶ from a Browser-less layout appends Overview then Browser", () => {
    expect(withPanesOpen(["ember", "editor"], RUN_APPEND_PANES)).toEqual([
      "ember",
      "editor",
      "overview",
      "browser",
    ]);
  });

  test("a block ▶ from the run layout changes nothing", () => {
    expect(
      withPanesOpen(["ember", "browser", "overview"], RUN_APPEND_PANES),
    ).toEqual(["ember", "browser", "overview"]);
  });

  test("a block ▶ from an empty stage opens just the run surfaces", () => {
    expect(withPanesOpen([], RUN_APPEND_PANES)).toEqual([
      "overview",
      "browser",
    ]);
  });
});

describe("panesWithoutDeletedBlocked", () => {
  test("drops the workflow-mutating panes and keeps run-view order", () => {
    expect(
      panesWithoutDeletedBlocked(["ember", "browser", "overview", "editor"]),
    ).toEqual(["browser", "overview"]);
  });

  test("is a no-op for an already-viewing-only list", () => {
    expect(panesWithoutDeletedBlocked(["browser", "overview"])).toEqual([
      "browser",
      "overview",
    ]);
  });

  test("blocks exactly the Ember and editor panes", () => {
    expect(WORKFLOW_AUTHORING_PANES).toEqual(["ember", "editor"]);
  });
});

describe("isAuthoringLayout", () => {
  test("watch-and-review (the cold run-link layout) is not authoring", () => {
    // The top bar hides its workflow-parameter "Inputs" here so it can't sit
    // next to the Run pane's own "Inputs" view.
    expect(isAuthoringLayout(RUN_APPEND_PANES)).toBe(false);
    expect(isAuthoringLayout([])).toBe(false);
  });

  test("any open editor or Ember pane makes the layout authoring", () => {
    expect(isAuthoringLayout(DEFAULT_STUDIO_PANES)).toBe(true);
    expect(isAuthoringLayout(["browser", "overview", "ember"])).toBe(true);
  });
});

describe("system run focus (?wrs=)", () => {
  test("reads both Ember markers as Ember's focus, never as a browser session id", () => {
    for (const marker of ["copilot", "ember"]) {
      expect(
        emberBrowserFocusSessionId(new URLSearchParams(`wrs=${marker}`)),
      ).toBeNull();
    }
    expect(emberBrowserFocusSessionId(new URLSearchParams("wrs=pbs_1"))).toBe(
      "pbs_1",
    );
  });

  test("does not remap deep-link panes to the run layout", () => {
    expect(
      resolveOpenPanes("?wr=wr_1&wrs=ember", ["editor", "browser"]),
    ).toEqual(["editor", "browser"]);
    expect(resolveOpenPanes("?wr=wr_1", ["editor", "browser"])).toEqual([
      "overview",
      "browser",
    ]);
  });
});

describe("searchWithRunReference", () => {
  test("injects ?wr= from the path run so /runs/{wr} resolves run-class panes", () => {
    expect(searchWithRunReference("", "wr_123")).toBe("?wr=wr_123");
    expect(resolveOpenPanes(searchWithRunReference("", "wr_123"))).toEqual(
      RUN_APPEND_PANES,
    );
  });

  test("drops a stale marker so a path run keeps its run class", () => {
    // The marker belongs to an Ember focus that is gone; leaving it would pin
    // a run the user opened to the edit layout.
    expect(searchWithRunReference("?wrs=ember", "wr_123")).toBe("?wr=wr_123");
    expect(
      resolveOpenPanes(searchWithRunReference("?wrs=ember", "wr_123")),
    ).toEqual(RUN_APPEND_PANES);
  });

  test("keeps an explicit ?wr= and other params untouched", () => {
    expect(searchWithRunReference("?wr=wr_a&bl=b", "wr_b")).toBe(
      "?wr=wr_a&bl=b",
    );
    expect(searchWithRunReference("?panes=browser", "wr_1")).toBe(
      "?panes=browser&wr=wr_1",
    );
  });

  test("no run id is a no-op (edit surfaces keep their search)", () => {
    expect(searchWithRunReference("?panes=editor", undefined)).toBe(
      "?panes=editor",
    );
  });
});
