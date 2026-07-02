import { describe, expect, test } from "vitest";

import {
  DEFAULT_STUDIO_PANES,
  panesFromDeepLink,
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
