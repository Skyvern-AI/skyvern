import * as React from "react";
import { DotsHorizontalIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuLabel,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { DialogTrigger } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";

// One action set, three doors (SKY-12955): a surface defines its row actions
// once and renders them through both the always-visible kebab and the
// right-click accelerator, so the two menus can't drift. The third door (bulk
// selection -> SelectionBar) stays separate; kebabs should yield to it while a
// selection is active.
type RowActionItem =
  | {
      kind: "item";
      label: React.ReactNode;
      icon?: React.ReactNode;
      onSelect?: () => void;
      destructive?: boolean;
      disabled?: boolean;
      // Wraps the item in a DialogTrigger so a confirm dialog owned by an
      // ancestor <Dialog> opens reliably from either menu.
      dialogTrigger?: boolean;
    }
  | { kind: "note"; label: React.ReactNode }
  | { kind: "separator" }
  | {
      kind: "sub";
      label: React.ReactNode;
      icon?: React.ReactNode;
      disabled?: boolean;
      // Plain nested actions and/or a door-agnostic panel (e.g. a Command).
      items?: Array<{ label: React.ReactNode; onSelect: () => void }>;
      content?: React.ReactNode;
      contentClassName?: string;
      onOpenChange?: (open: boolean) => void;
    };

type MenuKit = {
  Item: React.ComponentType<{
    className?: string;
    disabled?: boolean;
    onSelect?: (event: Event) => void;
    children?: React.ReactNode;
  }>;
  Label: React.ComponentType<{
    className?: string;
    children?: React.ReactNode;
  }>;
  Separator: React.ComponentType<{ className?: string }>;
  Sub: React.ComponentType<{
    onOpenChange?: (open: boolean) => void;
    children?: React.ReactNode;
  }>;
  SubTrigger: React.ComponentType<{
    disabled?: boolean;
    children?: React.ReactNode;
  }>;
  SubContent: React.ComponentType<{
    className?: string;
    children?: React.ReactNode;
  }>;
};

const dropdownKit: MenuKit = {
  Item: DropdownMenuItem,
  Label: DropdownMenuLabel,
  Separator: DropdownMenuSeparator,
  Sub: DropdownMenuSub,
  SubTrigger: DropdownMenuSubTrigger,
  SubContent: DropdownMenuSubContent,
};

const contextKit: MenuKit = {
  Item: ContextMenuItem,
  Label: ContextMenuLabel,
  Separator: ContextMenuSeparator,
  Sub: ContextMenuSub,
  SubTrigger: ContextMenuSubTrigger,
  SubContent: ContextMenuSubContent,
};

function RowActionMenuItems({
  items,
  kit,
}: {
  items: Array<RowActionItem>;
  kit: MenuKit;
}) {
  return (
    <>
      {items.map((item, index) => {
        switch (item.kind) {
          case "separator":
            return <kit.Separator key={index} />;
          case "note":
            return (
              <kit.Label
                key={index}
                className="text-xs font-normal text-muted-foreground"
              >
                {item.label}
              </kit.Label>
            );
          case "sub":
            return (
              <kit.Sub key={index} onOpenChange={item.onOpenChange}>
                <kit.SubTrigger disabled={item.disabled}>
                  {item.icon}
                  {item.label}
                </kit.SubTrigger>
                <kit.SubContent className={item.contentClassName}>
                  {item.content}
                  {item.items?.map((subItem, subIndex) => (
                    <kit.Item
                      key={subIndex}
                      onSelect={() => subItem.onSelect()}
                    >
                      {subItem.label}
                    </kit.Item>
                  ))}
                </kit.SubContent>
              </kit.Sub>
            );
          case "item": {
            const { onSelect } = item;
            const menuItem = (
              <kit.Item
                disabled={item.disabled}
                onSelect={onSelect ? () => onSelect() : undefined}
                className={cn(
                  item.destructive && "text-destructive focus:text-destructive",
                )}
              >
                {item.icon}
                {item.label}
              </kit.Item>
            );
            return (
              <React.Fragment key={index}>
                {item.dialogTrigger ? (
                  <DialogTrigger asChild>{menuItem}</DialogTrigger>
                ) : (
                  menuItem
                )}
              </React.Fragment>
            );
          }
        }
      })}
    </>
  );
}

type RowActionsKebabProps = {
  items: Array<RowActionItem>;
  // e.g. "Actions for {row name}" — the kebab is icon-only.
  ariaLabel: string;
  className?: string;
};

// Click handlers stop propagation because the kebab sits inside rows whose
// cells navigate on click (portaled menu clicks still bubble in the React tree).
function RowActionsKebab({
  items,
  ariaLabel,
  className,
}: RowActionsKebabProps) {
  return (
    <DropdownMenu modal={false}>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label={ariaLabel}
                className={cn(
                  "text-muted-foreground hover:text-foreground",
                  className,
                )}
                onClick={(event) => event.stopPropagation()}
              >
                <DotsHorizontalIcon className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>Actions</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DropdownMenuContent
        align="end"
        className="w-56"
        onClick={(event) => event.stopPropagation()}
      >
        <RowActionMenuItems items={items} kit={dropdownKit} />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

type RowActionsContextMenuProps = {
  items: Array<RowActionItem>;
  onOpenChange?: (open: boolean) => void;
  children: React.ReactNode;
};

function RowActionsContextMenu({
  items,
  onOpenChange,
  children,
}: RowActionsContextMenuProps) {
  const [menuOpen, setMenuOpen] = React.useState(false);
  // Keep the row highlighted while its context menu is open (the cursor leaves
  // it). Children.only throws clearly if the trigger wraps more than one node.
  const child = React.Children.only(children) as React.ReactElement<{
    className?: string;
    "data-row-active"?: string;
  }>;
  return (
    <ContextMenu
      onOpenChange={(open) => {
        setMenuOpen(open);
        onOpenChange?.(open);
      }}
    >
      <ContextMenuTrigger asChild>
        {React.cloneElement(child, {
          className: cn(child.props.className, "data-[row-active]:bg-muted/50"),
          "data-row-active": menuOpen ? "" : undefined,
        })}
      </ContextMenuTrigger>
      <ContextMenuContent className="w-56">
        <RowActionMenuItems items={items} kit={contextKit} />
      </ContextMenuContent>
    </ContextMenu>
  );
}

export { RowActionsContextMenu, RowActionsKebab };
export type { RowActionItem };
