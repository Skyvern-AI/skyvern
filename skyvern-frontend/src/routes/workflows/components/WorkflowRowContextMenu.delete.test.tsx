// @vitest-environment jsdom
import * as React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

// Flatten the Radix context menu so the Delete item (wrapped in a DialogTrigger)
// is directly reachable; render submenu bodies as null so the tag/folder pickers
// (which need query providers) never mount.
vi.mock("@/components/ui/context-menu", () => {
  const Pass = ({ children }: { children?: React.ReactNode }) => (
    <>{children}</>
  );
  const Item = React.forwardRef(function Item(
    { children, onSelect, ...props }: Record<string, unknown>,
    ref: React.Ref<HTMLButtonElement>,
  ) {
    return (
      <button ref={ref} onClick={onSelect as () => void} {...props}>
        {children as React.ReactNode}
      </button>
    );
  });
  return {
    ContextMenu: Pass,
    ContextMenuTrigger: Pass,
    ContextMenuContent: Pass,
    ContextMenuItem: Item,
    ContextMenuLabel: Pass,
    ContextMenuSeparator: () => null,
    ContextMenuSub: Pass,
    ContextMenuSubTrigger: Pass,
    ContextMenuSubContent: Pass,
  };
});

const deleteWorkflow = vi.fn((opts?: { onSuccess?: () => void }) =>
  opts?.onSuccess?.(),
);
const applyWorkflowTags = vi.fn();
vi.mock("../hooks/useWorkflowRowActions", () => ({
  useWorkflowRowActions: () => ({
    clone: vi.fn(),
    toggleTemplate: vi.fn(),
    exportAs: vi.fn(),
    deleteWorkflow,
    isDeleting: false,
    isTogglingTemplate: false,
  }),
}));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => false,
}));
vi.mock("../hooks/useFolderMutations", () => ({
  useUpdateWorkflowFolderMutation: () => ({ mutateAsync: vi.fn() }),
}));
vi.mock("../hooks/useWorkflowTagMutations", () => ({
  useApplyWorkflowTagsMutation: () => ({
    mutate: applyWorkflowTags,
    isPending: false,
  }),
}));
vi.mock("./FolderPickerCommand", () => ({
  FolderPickerCommand: () => null,
}));
vi.mock("./tagging/TagPickerCommand", () => ({
  TagPickerCommand: ({
    currentTags = [],
    onRemove,
  }: {
    currentTags?: Array<{ key: string | null; value: string }>;
    onRemove?: (tag: { key: string | null; value: string }) => void;
  }) => (
    <div data-testid="tag-picker">
      {currentTags.map((tag) => (
        <button
          key={`${tag.key ?? "label"}:${tag.value}`}
          onClick={() => onRemove?.(tag)}
        >
          {tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value}
        </button>
      ))}
    </div>
  ),
}));

import { WorkflowRowContextMenu } from "./WorkflowRowContextMenu";
import type { WorkflowApiResponse } from "../types/workflowTypes";

function buildWorkflow(): WorkflowApiResponse {
  return {
    workflow_permanent_id: "wpid_1",
    title: "Scrape leads",
    is_template: false,
    folder_id: null,
  } as unknown as WorkflowApiResponse;
}

function renderMenu(props?: {
  selectedCount?: number;
  onDeleted?: (id: string) => void;
  currentTags?: Array<{ key: string | null; value: string }>;
}) {
  return render(
    <MemoryRouter>
      <WorkflowRowContextMenu
        workflow={buildWorkflow()}
        tagKeys={[]}
        labelSuggestions={[]}
        valueSuggestionsByKey={new Map()}
        currentTags={props?.currentTags}
        onNavigate={() => {}}
        selectedCount={props?.selectedCount ?? 1}
        onDeleted={props?.onDeleted}
      >
        <div>row</div>
      </WorkflowRowContextMenu>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  deleteWorkflow.mockClear();
  applyWorkflowTags.mockClear();
});

describe("WorkflowRowContextMenu single-row delete", () => {
  it("notifies onDeleted with the workflow id after a successful delete", () => {
    const onDeleted = vi.fn();
    renderMenu({ selectedCount: 1, onDeleted });

    fireEvent.click(screen.getByText("Delete"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /delete/i }));

    expect(onDeleted).toHaveBeenCalledWith("wpid_1");
  });

  it("removes current tags through the apply mutation tags_to_delete payload", () => {
    renderMenu({
      currentTags: [
        { key: "env", value: "prod" },
        { key: null, value: "urgent" },
      ],
    });

    fireEvent.click(screen.getByRole("button", { name: "env: prod" }));
    fireEvent.click(screen.getByRole("button", { name: "urgent" }));

    expect(applyWorkflowTags).toHaveBeenNthCalledWith(
      1,
      {
        workflowPermanentId: "wpid_1",
        data: { tags_to_delete: [{ key: "env" }] },
      },
      expect.any(Object),
    );
    expect(applyWorkflowTags).toHaveBeenNthCalledWith(
      2,
      {
        workflowPermanentId: "wpid_1",
        data: { tags_to_delete: [{ value: "urgent" }] },
      },
      expect.any(Object),
    );
  });
});
