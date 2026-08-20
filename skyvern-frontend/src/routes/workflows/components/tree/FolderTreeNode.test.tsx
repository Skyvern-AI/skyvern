// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Folder } from "../../types/folderTypes";
import type { WorkflowApiResponse } from "../../types/workflowTypes";
import { FolderTreeNode } from "./FolderTreeNode";
import {
  WorkflowsListContext,
  type WorkflowsListContextValue,
} from "./WorkflowsListContext";

const useFolderWorkflowsQueryMock = vi.fn();

vi.mock("../../hooks/useFolderWorkflowsQuery", () => ({
  useFolderWorkflowsQuery: (args: { folderId: string; enabled: boolean }) =>
    useFolderWorkflowsQueryMock(args),
  FOLDER_WORKFLOWS_PAGE_SIZE: 20,
}));

// Keep the leaf rendering trivial so this test isolates the folder node's
// expand/collapse and empty-state behavior.
vi.mock("./WorkflowRow", () => ({
  WorkflowRow: ({ workflow }: { workflow: WorkflowApiResponse }) => (
    <tr data-testid="workflow-row">
      <td>{workflow.title}</td>
    </tr>
  ),
}));

vi.mock("../DeleteFolderButton", () => ({
  DeleteFolderButton: () => <button type="button">delete</button>,
}));

vi.mock("../EditFolderDialog", () => ({
  EditFolderDialog: () => null,
}));

function buildFolder(overrides: Partial<Folder> = {}): Folder {
  return {
    folder_id: "fld_1",
    organization_id: "org_1",
    title: "Marketing",
    description: null,
    workflow_count: 2,
    created_at: "2026-01-01T00:00:00Z",
    modified_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function buildWorkflow(id: string, title: string): WorkflowApiResponse {
  return { workflow_permanent_id: id, title } as WorkflowApiResponse;
}

const contextValue: WorkflowsListContextValue = {
  showCheckbox: false,
  columnCount: 5,
  debouncedSearch: "",
  isBulkOperating: false,
  selectedCount: 0,
  foldersMap: new Map(),
  workflowTagsMap: {},
  workflowReliabilityMap: {},
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

function renderNode({
  isExpanded,
  onToggle = () => {},
  onCreateAgentInFolder = () => {},
  isCreatingAgent = false,
}: {
  isExpanded: boolean;
  onToggle?: () => void;
  onCreateAgentInFolder?: (folderId: string) => void;
  isCreatingAgent?: boolean;
}) {
  return render(
    <WorkflowsListContext.Provider value={contextValue}>
      <table>
        <tbody>
          <FolderTreeNode
            folder={buildFolder()}
            isExpanded={isExpanded}
            isActive={false}
            onToggle={onToggle}
            onContentsChange={() => {}}
            onCreateAgentInFolder={onCreateAgentInFolder}
            isCreatingAgent={isCreatingAgent}
          />
        </tbody>
      </table>
    </WorkflowsListContext.Provider>,
  );
}

describe("FolderTreeNode", () => {
  afterEach(() => {
    cleanup();
    useFolderWorkflowsQueryMock.mockReset();
  });

  it("shows the folder title and agent count, and hides contents while collapsed", () => {
    useFolderWorkflowsQueryMock.mockReturnValue({
      data: undefined,
      isFetching: false,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });

    renderNode({ isExpanded: false });

    expect(screen.getByText("Marketing")).toBeTruthy();
    expect(screen.getByText("2 agents")).toBeTruthy();
    expect(screen.queryByTestId("workflow-row")).toBeNull();
  });

  it("renders the folder's agents when expanded", () => {
    useFolderWorkflowsQueryMock.mockReturnValue({
      data: { pages: [[buildWorkflow("wpid_1", "Scrape leads")]] },
      isFetching: false,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });

    renderNode({ isExpanded: true });

    expect(screen.getByTestId("workflow-row")).toBeTruthy();
    expect(screen.getByText("Scrape leads")).toBeTruthy();
  });

  it("shows an empty state for an expanded folder with no agents", () => {
    useFolderWorkflowsQueryMock.mockReturnValue({
      data: { pages: [[]] },
      isFetching: false,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });

    renderNode({ isExpanded: true });

    expect(screen.getByText("This folder is empty")).toBeTruthy();
    expect(screen.queryByTestId("workflow-row")).toBeNull();
  });

  it("disables per-folder create and ignores clicks while a create is pending", () => {
    useFolderWorkflowsQueryMock.mockReturnValue({
      data: undefined,
      isFetching: false,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });
    const onCreateAgentInFolder = vi.fn();

    renderNode({
      isExpanded: false,
      onCreateAgentInFolder,
      isCreatingAgent: true,
    });
    const createButton = screen.getByLabelText(
      "New agent in folder",
    ) as HTMLButtonElement;
    fireEvent.click(createButton);

    expect(createButton.disabled).toBe(true);
    expect(onCreateAgentInFolder).not.toHaveBeenCalled();
  });

  it("toggles when the folder row is clicked", () => {
    useFolderWorkflowsQueryMock.mockReturnValue({
      data: undefined,
      isFetching: false,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });
    const onToggle = vi.fn();

    renderNode({ isExpanded: false, onToggle });
    fireEvent.click(screen.getByText("Marketing"));

    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("does not toggle the folder when a folder-action button is keyboard-activated", () => {
    useFolderWorkflowsQueryMock.mockReturnValue({
      data: undefined,
      isFetching: false,
      fetchNextPage: vi.fn(),
      hasNextPage: false,
      isFetchingNextPage: false,
    });
    const onToggle = vi.fn();

    renderNode({ isExpanded: false, onToggle });
    fireEvent.keyDown(screen.getByLabelText("New agent in folder"), {
      key: "Enter",
    });

    expect(onToggle).not.toHaveBeenCalled();
  });
});
