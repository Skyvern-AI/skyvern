import { describe, expect, test } from "vitest";

import {
  STUDIO_PANE_META,
  paneAccessibleName,
  paneLabel,
  railLabel,
} from "./paneMeta";

describe("paneLabel", () => {
  test("non-run panes keep their registry label", () => {
    expect(paneLabel("copilot")).toBe("Copilot");
    expect(paneLabel("editor")).toBe("Editor");
    expect(paneLabel("browser")).toBe("Browser");
  });

  test("the run pane reads 'Run' when no run is inspected", () => {
    expect(paneLabel("overview")).toBe("Run");
    expect(paneLabel("overview", null)).toBe("Run");
    expect(paneLabel("overview", undefined)).toBe("Run");
  });

  test("the run pane head-truncates the inspected run id", () => {
    expect(paneLabel("overview", "wr_5538abcdef")).toBe("Run: wr_5538…");
  });

  test("a run id short enough to fit is shown without an ellipsis", () => {
    expect(paneLabel("overview", "wr_12")).toBe("Run: wr_12");
  });
});

describe("paneAccessibleName", () => {
  test("non-run panes match their visible label", () => {
    expect(paneAccessibleName("copilot")).toBe("Copilot");
    expect(paneAccessibleName("browser")).toBe("Browser");
  });

  test("the run pane's controls keep the stable name 'Run'", () => {
    // The pane's own controls (region/close/drag) announce "Run", matching the
    // "Run: wr_…" content; "Past Runs" is the rail selector's name (railLabel).
    expect(paneAccessibleName("overview")).toBe("Run");
    expect(paneLabel("overview", "wr_5538abcdef")).toBe("Run: wr_5538…");
  });
});

describe("railLabel", () => {
  test("the run pane's rail tab is the 'Past Runs' selector", () => {
    expect(railLabel("overview")).toBe("Past Runs");
  });

  test("other tabs match their pane's accessible name", () => {
    expect(railLabel("copilot")).toBe("Copilot");
    expect(railLabel("editor")).toBe("Editor");
    expect(railLabel("browser")).toBe("Browser");
  });
});

describe("STUDIO_PANE_META", () => {
  test("keeps 'Overview' as the run pane's registry fallback name", () => {
    expect(STUDIO_PANE_META.overview.label).toBe("Overview");
  });
});
