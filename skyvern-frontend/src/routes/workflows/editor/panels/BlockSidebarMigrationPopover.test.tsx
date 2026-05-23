// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useBlockSidebarOnboardingStore } from "@/store/BlockSidebarOnboardingStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { BlockSidebarMigrationPopover } from "./BlockSidebarMigrationPopover";

vi.mock("../hooks/useWorkflowEditorMode", () => ({
  useWorkflowEditorMode: () => "edit",
}));

beforeEach(() => {
  localStorage.clear();
  useBlockSidebarOnboardingStore.getState().reset();
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
});

afterEach(() => {
  cleanup();
});

describe("BlockSidebarMigrationPopover", () => {
  test("renders nothing when the user has already dismissed", () => {
    useBlockSidebarOnboardingStore.getState().markSeen();
    useWorkflowPanelStore.getState().setSelectedBlockId("block-1");
    const { container } = render(<BlockSidebarMigrationPopover />);
    expect(container.innerHTML).toBe("");
  });

  test("renders nothing when no block is selected", () => {
    const { container } = render(<BlockSidebarMigrationPopover />);
    expect(container.innerHTML).toBe("");
  });

  test("renders the migration copy when first-edit and a block is selected", () => {
    useWorkflowPanelStore.getState().setSelectedBlockId("block-1");
    render(<BlockSidebarMigrationPopover />);
    expect(screen.getByText(/configuration moved here/i)).toBeDefined();
  });

  test("clicking 'Got it' marks seen and hides the popover", () => {
    useWorkflowPanelStore.getState().setSelectedBlockId("block-1");
    render(<BlockSidebarMigrationPopover />);
    fireEvent.click(screen.getByRole("button", { name: /got it/i }));
    expect(useBlockSidebarOnboardingStore.getState().hasSeenMigration).toBe(
      true,
    );
    expect(screen.queryByText(/configuration moved here/i)).toBeNull();
  });
});
