// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FeatureFlagContext } from "@/hooks/useFeatureFlag";
import type { WorkflowApiResponse } from "../../types/workflowTypes";
import { WorkflowRow } from "./WorkflowRow";
import {
  WorkflowsListContext,
  type WorkflowsListContextValue,
} from "./WorkflowsListContext";

// Isolate WorkflowRow's gating: stub the tagging children (and the unrelated
// row actions) so the assertions only reflect whether the row chose to render
// tagging UI, not the children's own internals.
vi.mock("../tagging/TagChipList", () => ({
  TagChipList: () => <span data-testid="tag-chip-list" />,
}));
vi.mock("../tagging/WorkflowTagEditor", () => ({
  WorkflowTagEditor: () => <span data-testid="workflow-tag-editor" />,
}));
vi.mock("../WorkflowFolderSelector", () => ({
  WorkflowFolderSelector: () => <span data-testid="folder-selector" />,
}));
vi.mock("../../WorkflowActions", () => ({
  WorkflowActions: () => <span data-testid="workflow-actions" />,
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
};

function renderRow(flagValue: boolean | undefined) {
  return render(
    <FeatureFlagContext.Provider value={() => flagValue}>
      <WorkflowsListContext.Provider value={contextValue}>
        <table>
          <tbody>
            <WorkflowRow workflow={buildWorkflow()} />
          </tbody>
        </table>
      </WorkflowsListContext.Provider>
    </FeatureFlagContext.Provider>,
  );
}

describe("WorkflowRow tagging gate", () => {
  afterEach(() => {
    cleanup();
  });

  it("hides tag chips and the tag editor when WORKFLOW_TAGGING is off", () => {
    renderRow(false);

    expect(screen.queryByTestId("tag-chip-list")).toBeNull();
    expect(screen.queryByTestId("workflow-tag-editor")).toBeNull();
  });

  it("shows tag chips and the tag editor when the flag is unresolved (default on)", () => {
    renderRow(undefined);

    expect(screen.getByTestId("tag-chip-list")).toBeTruthy();
    expect(screen.getByTestId("workflow-tag-editor")).toBeTruthy();
  });
});
