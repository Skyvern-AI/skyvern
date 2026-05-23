import { describe, expect, test } from "vitest";

import {
  isBlockSidebarOpen,
  HEADER_RIGHT_INSET_CLOSED,
  HEADER_RIGHT_INSET_OPEN,
  BLOCK_SIDEBAR_WIDTH_VAR,
  BLOCK_SIDEBAR_RIGHT_GAP,
} from "./blockSidebar";

describe("isBlockSidebarOpen", () => {
  test("returns true when in edit mode with a selected block", () => {
    expect(isBlockSidebarOpen("edit", "block-123")).toBe(true);
  });

  test("returns false when no block is selected", () => {
    expect(isBlockSidebarOpen("edit", null)).toBe(false);
  });

  test("returns false in build mode with a selected block and library closed", () => {
    // build mode has no block-config sidebar; selection alone does not open the rail
    expect(isBlockSidebarOpen("build", "block-123")).toBe(false);
  });

  test("returns false in build mode with no selection and library closed", () => {
    expect(isBlockSidebarOpen("build", null)).toBe(false);
  });

  test("treats nodeLibrary-open as sidebar-open in edit mode", () => {
    expect(isBlockSidebarOpen("edit", null, true)).toBe(true);
    expect(isBlockSidebarOpen("edit", null, false)).toBe(false);
  });

  test("treats nodeLibrary-open as sidebar-open in build mode", () => {
    // build-mode BlockConfigSidebar renders the library; header insets must track it
    expect(isBlockSidebarOpen("build", null, true)).toBe(true);
    expect(isBlockSidebarOpen("build", "block-123", true)).toBe(true);
  });
});

describe("right-inset class constants", () => {
  test("closed inset is right-6 (matches the header's left-6 baseline)", () => {
    expect(HEADER_RIGHT_INSET_CLOSED).toBe("right-6");
  });

  test("open inset reads sidebar width from CSS var + 1.5rem gap + 1.5rem outer", () => {
    expect(HEADER_RIGHT_INSET_OPEN).toBe(
      "right-[calc(var(--block-sidebar-w)+3rem)]",
    );
  });

  test("the CSS var name and gap are exported as a single source of truth", () => {
    expect(BLOCK_SIDEBAR_WIDTH_VAR).toBe("--block-sidebar-w");
    expect(BLOCK_SIDEBAR_RIGHT_GAP).toBe("1.5rem");
  });
});
