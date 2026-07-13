// @vitest-environment jsdom

import type { ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import type { WorkflowApiResponse } from "../../types/workflowTypes";
import { WorkflowRow } from "./WorkflowRow";
import {
  WorkflowsListContext,
  type WorkflowsListContextValue,
} from "./WorkflowsListContext";

// Isolate WorkflowRow's tagging gate: stub the tag chips and pass the row through
// its context-menu wrapper (whose own data hooks are irrelevant here) so the
// assertions only reflect whether the row chose to render the tag chips.
vi.mock("../tagging/TagChipList", () => ({
  TagChipList: () => <span data-testid="tag-chip-list" />,
}));
vi.mock("../WorkflowRowContextMenu", () => ({
  WorkflowRowContextMenu: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => false,
}));

function buildWorkflow(): WorkflowApiResponse {
  return {
    workflow_permanent_id: "wpid_1",
    title: "Scrape leads",
    created_at: "2026-01-01T00:00:00Z",
    workflow_definition: { parameters: [] },
  } as unknown as WorkflowApiResponse;
}

const contextValue: WorkflowsListContextValue = {
  showCheckbox: false,
  columnCount: 5,
  debouncedSearch: "",
  isBulkOperating: false,
  selectedCount: 0,
  foldersMap: new Map(),
  workflowTagsMap: { wpid_1: [{ key: "env", value: "prod" }] },
  tagDescriptions: new Map(),
  tagColors: new Map(),
  tagKeys: [],
  labelSuggestions: [],
  valueSuggestionsByKey: new Map(),
  isSelected: () => false,
  indexById: new Map(),
  handleSelect: () => {},
  expandedRows: new Set(),
  toggleParametersExpanded: () => {},
  matchesParameter: () => false,
  handleRowClick: () => {},
  handleIconClick: () => {},
  onRowDeleted: () => {},
};

function renderRow(flagValue: boolean | undefined) {
  return render(
    <MemoryRouter>
      <FeatureFlagContext.Provider value={() => flagValue}>
        <WorkflowsListContext.Provider value={contextValue}>
          <table>
            <tbody>
              <WorkflowRow workflow={buildWorkflow()} />
            </tbody>
          </table>
        </WorkflowsListContext.Provider>
      </FeatureFlagContext.Provider>
    </MemoryRouter>,
  );
}

describe("WorkflowRow tagging gate", () => {
  afterEach(() => {
    cleanup();
  });

  it("hides the tag chips when WORKFLOW_TAGGING is off", () => {
    renderRow(false);

    expect(screen.queryByTestId("tag-chip-list")).toBeNull();
  });

  it("shows the tag chips when the flag is unresolved (default on)", () => {
    renderRow(undefined);

    expect(screen.getByTestId("tag-chip-list")).toBeTruthy();
  });
});
