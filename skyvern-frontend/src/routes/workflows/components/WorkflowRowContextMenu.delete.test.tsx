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
    ContextMenuSubTrigger: () => null,
    ContextMenuSubContent: () => null,
  };
});

const deleteWorkflow = vi.fn((opts?: { onSuccess?: () => void }) =>
  opts?.onSuccess?.(),
);
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
  useApplyWorkflowTagsMutation: () => ({ mutate: vi.fn() }),
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
}) {
  return render(
    <MemoryRouter>
      <WorkflowRowContextMenu
        workflow={buildWorkflow()}
        tagKeys={[]}
        labelSuggestions={[]}
        valueSuggestionsByKey={new Map()}
        onNavigate={() => {}}
        selectedCount={props?.selectedCount ?? 1}
        onDeleted={props?.onDeleted}
      >
        <div>row</div>
      </WorkflowRowContextMenu>
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("WorkflowRowContextMenu single-row delete", () => {
  it("notifies onDeleted with the workflow id after a successful delete", () => {
    const onDeleted = vi.fn();
    renderMenu({ selectedCount: 1, onDeleted });

    fireEvent.click(screen.getByText("Delete"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /delete/i }));

    expect(onDeleted).toHaveBeenCalledWith("wpid_1");
  });
});
