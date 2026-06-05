import { describe, expect, it, vi } from "vitest";
import {
  buildBlankAgentBuildPath,
  navigateToBlankAgentEditor,
} from "./blankAgentNavigation";

describe("blankAgentNavigation", () => {
  it("builds the draft builder path with via and folder_id", () => {
    expect(
      buildBlankAgentBuildPath({ via: "blank", folderId: "fld_123" }),
    ).toBe("/workflows/new/build?via=blank&folder_id=fld_123");
  });

  it("navigates to the draft builder", () => {
    const navigate = vi.fn();
    navigateToBlankAgentEditor(navigate, { via: "sidebar" });
    expect(navigate).toHaveBeenCalledWith("/workflows/new/build?via=sidebar");
  });
});
