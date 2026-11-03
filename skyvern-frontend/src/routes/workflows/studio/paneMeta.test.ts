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

  test("the run pane label is always 'Run'", () => {
    expect(paneLabel("overview")).toBe("Run");
  });
});

describe("paneAccessibleName", () => {
  test("non-run panes match their visible label", () => {
    expect(paneAccessibleName("copilot")).toBe("Copilot");
    expect(paneAccessibleName("browser")).toBe("Browser");
  });

  test("the run pane's controls keep the stable name 'Run'", () => {
    // "Past Runs" is the idle top-bar selector label.
    expect(paneAccessibleName("overview")).toBe("Run");
    expect(paneLabel("overview")).toBe("Run");
  });
});

describe("railLabel", () => {
  test("the run pane's rail tab names the inspected run with its full id", () => {
    // Untruncated on purpose: the top bar is where run ids are read and copied.
    expect(railLabel("overview", "wr_556219201027773764")).toBe(
      "View Run: wr_556219201027773764",
    );
  });

  test("falls back to the 'Past Runs' selector name with no run", () => {
    expect(railLabel("overview")).toBe("Past Runs");
    expect(railLabel("overview", null)).toBe("Past Runs");
  });

  test("other tabs match their pane's accessible name, ignoring the run id", () => {
    expect(railLabel("copilot")).toBe("Copilot");
    expect(railLabel("editor")).toBe("Editor");
    expect(railLabel("browser", "wr_5538abcdef")).toBe("Browser");
  });
});

describe("STUDIO_PANE_META", () => {
  test("keeps 'Overview' as the run pane's registry fallback name", () => {
    expect(STUDIO_PANE_META.overview.label).toBe("Overview");
  });
});
