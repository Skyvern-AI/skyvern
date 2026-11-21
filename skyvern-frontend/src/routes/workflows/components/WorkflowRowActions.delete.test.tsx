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

import { WorkflowRowActions } from "./WorkflowRowActions";
import type { WorkflowApiResponse } from "../types/workflowTypes";
import type { PermissionsState } from "@/store/PageSlots";
import { PermissionsStateTestProvider } from "@/testUtils/permissions";

function buildWorkflow(originalCreatedBy?: string | null): WorkflowApiResponse {
  return {
    workflow_permanent_id: "wpid_1",
    title: "Scrape leads",
    is_template: false,
    folder_id: null,
    original_created_by: originalCreatedBy,
  } as unknown as WorkflowApiResponse;
}

function deletePermissions(grants: {
  any: boolean;
  own: boolean;
}): PermissionsState {
  return {
    status: "ready",
    data: {
      organization_id: "org_test",
      user_id: "user_self",
      auth_kind: "bearer",
      org_role: "org:member",
      role: "member",
      capabilities: [
        { capability: "workflow.delete", allowed: grants.any, reason: "role" },
        {
          capability: "workflow.delete.own",
          allowed: grants.own,
          reason: "role",
        },
      ],
    },
  };
}

function renderMenu(props?: {
  selectedCount?: number;
  onDeleted?: (id: string) => void;
  currentTags?: Array<{ key: string | null; value: string }>;
  originalCreatedBy?: string | null;
  permissions?: PermissionsState;
}) {
  const menu = (
    <MemoryRouter>
      <WorkflowRowActions
        workflow={buildWorkflow(props?.originalCreatedBy)}
        tagKeys={[]}
        labelSuggestions={[]}
        valueSuggestionsByKey={new Map()}
        currentTags={props?.currentTags}
        onNavigate={() => {}}
        selectedCount={props?.selectedCount ?? 1}
        onDeleted={props?.onDeleted}
        taggingEnabled
      >
        {() => <div>row</div>}
      </WorkflowRowActions>
    </MemoryRouter>
  );
  return render(
    props?.permissions ? (
      <PermissionsStateTestProvider permissions={props.permissions}>
        {menu}
      </PermissionsStateTestProvider>
    ) : (
      menu
    ),
  );
}

function deleteItem(): HTMLButtonElement {
  return screen.getByRole("button", { name: /^delete$/i }) as HTMLButtonElement;
}

afterEach(() => {
  cleanup();
  deleteWorkflow.mockClear();
  applyWorkflowTags.mockClear();
});

describe("WorkflowRowActions single-row delete", () => {
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

describe("WorkflowRowActions delete permission", () => {
  // The detail GET returns a null creator for an agent no user created (an API key).
  it.each([
    ["user_other", true, "You can only delete agents you created."],
    [null, true, "You can only delete agents you created."],
    ["user_self", false, "Your role cannot delete agents."],
  ])(
    "disables Delete with a reason (creator %s, own grant %s)",
    (originalCreatedBy, own, reason) => {
      renderMenu({
        originalCreatedBy,
        permissions: deletePermissions({ any: false, own }),
      });

      // The item stays focusable so keyboard and screen-reader users reach the reason.
      expect(deleteItem().getAttribute("aria-disabled")).toBe("true");
      const reasonId = deleteItem().getAttribute("aria-describedby");
      expect(reasonId && document.getElementById(reasonId)?.textContent).toBe(
        reason,
      );
      fireEvent.click(deleteItem());
      expect(screen.queryByRole("dialog")).toBeNull();
    },
  );

  it("keeps Delete enabled for a Builder's own agent", () => {
    renderMenu({
      originalCreatedBy: "user_self",
      permissions: deletePermissions({ any: false, own: true }),
    });

    expect(deleteItem().getAttribute("aria-disabled")).toBeNull();
  });

  it("keeps Delete enabled for any agent with workflow.delete", () => {
    renderMenu({
      originalCreatedBy: "user_other",
      permissions: deletePermissions({ any: true, own: false }),
    });

    expect(deleteItem().getAttribute("aria-disabled")).toBeNull();
  });

  it.each<[string, PermissionsState | undefined]>([
    ["no permission state (OSS)", undefined],
    ["a failed permissions read", { status: "error" }],
    ["permissions still loading", { status: "loading" }],
  ])("keeps Delete enabled with %s", (_label, permissions) => {
    renderMenu({ originalCreatedBy: "user_other", permissions });

    expect(deleteItem().getAttribute("aria-disabled")).toBeNull();
  });
});
