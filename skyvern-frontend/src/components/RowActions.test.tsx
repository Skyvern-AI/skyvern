// @vitest-environment jsdom

import type { ReactNode } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Flatten both Radix menu families so each door's items render inline and are
// clickable; the doors stay distinguishable via testids on the menu roots.
vi.mock("@/components/ui/dropdown-menu", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  const Item = ({
    children,
    onSelect,
    disabled,
    className,
  }: {
    children?: ReactNode;
    onSelect?: () => void;
    disabled?: boolean;
    className?: string;
  }) => (
    <button disabled={disabled} onClick={onSelect} className={className}>
      {children}
    </button>
  );
  return {
    DropdownMenu: ({ children }: { children?: ReactNode }) => (
      <div data-testid="kebab-door">{children}</div>
    ),
    DropdownMenuTrigger: Pass,
    DropdownMenuContent: Pass,
    DropdownMenuItem: Item,
    DropdownMenuLabel: Pass,
    DropdownMenuSeparator: () => null,
    DropdownMenuSub: Pass,
    DropdownMenuSubTrigger: Pass,
    DropdownMenuSubContent: Pass,
  };
});

vi.mock("@/components/ui/context-menu", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  const Item = ({
    children,
    onSelect,
    disabled,
    className,
  }: {
    children?: ReactNode;
    onSelect?: () => void;
    disabled?: boolean;
    className?: string;
  }) => (
    <button disabled={disabled} onClick={onSelect} className={className}>
      {children}
    </button>
  );
  return {
    ContextMenu: ({ children }: { children?: ReactNode }) => (
      <div data-testid="context-door">{children}</div>
    ),
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

vi.mock("@/components/ui/tooltip", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Tooltip: Pass,
    TooltipProvider: Pass,
    TooltipTrigger: Pass,
    TooltipContent: () => null,
  };
});

import {
  RowActionsContextMenu,
  RowActionsKebab,
  type RowActionItem,
} from "./RowActions";

function buildItems(handlers: {
  onOpen: () => void;
  onYaml: () => void;
  onDelete: () => void;
}): Array<RowActionItem> {
  return [
    { kind: "item", label: "Open", onSelect: handlers.onOpen },
    { kind: "separator" },
    {
      kind: "sub",
      label: "Export",
      items: [{ label: "YAML", onSelect: handlers.onYaml }],
    },
    {
      kind: "item",
      label: "Delete",
      destructive: true,
      onSelect: handlers.onDelete,
    },
  ];
}

function renderDoors(items: Array<RowActionItem>) {
  return render(
    <>
      <RowActionsKebab items={items} ariaLabel="Actions for Row 1" />
      <RowActionsContextMenu items={items}>
        <div>row</div>
      </RowActionsContextMenu>
    </>,
  );
}

describe("RowActions", () => {
  it("renders one action set through both the kebab and the context menu", () => {
    renderDoors(
      buildItems({ onOpen: vi.fn(), onYaml: vi.fn(), onDelete: vi.fn() }),
    );

    const kebab = screen.getByTestId("kebab-door");
    const context = screen.getByTestId("context-door");
    for (const label of ["Open", "Export", "YAML", "Delete"]) {
      expect(within(kebab).getByText(label)).toBeTruthy();
      expect(within(context).getByText(label)).toBeTruthy();
    }
    expect(within(kebab).getByLabelText("Actions for Row 1")).toBeTruthy();
  });

  it("fires the shared handler from either door and styles destructive items", () => {
    const onOpen = vi.fn();
    const onYaml = vi.fn();
    const onDelete = vi.fn();
    renderDoors(buildItems({ onOpen, onYaml, onDelete }));

    const kebab = screen.getByTestId("kebab-door");
    const context = screen.getByTestId("context-door");
    fireEvent.click(within(kebab).getByText("Open"));
    fireEvent.click(within(context).getByText("Open"));
    expect(onOpen).toHaveBeenCalledTimes(2);

    fireEvent.click(within(kebab).getByText("YAML"));
    expect(onYaml).toHaveBeenCalledTimes(1);
    expect(within(kebab).getByText("Delete").className).toContain(
      "text-destructive",
    );
    fireEvent.click(within(context).getByText("Delete"));
    expect(onDelete).toHaveBeenCalledTimes(1);
  });
});
