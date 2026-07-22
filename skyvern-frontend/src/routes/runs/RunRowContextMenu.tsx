import { OpenInNewWindowIcon, TokensIcon } from "@radix-ui/react-icons";
import {
  Children,
  cloneElement,
  type ReactElement,
  type ReactNode,
  useState,
} from "react";

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
import { RunTagPickerCommand } from "@/routes/tasks/components/tagging/RunTagPickerCommand";
import type { Tag, TagKey } from "@/routes/workflows/types/tagTypes";
import { cn } from "@/util/utils";

type Props = {
  workflowRunId: string;
  runPath: string;
  currentTags: Array<Tag>;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  selectedCount?: number;
  onNavigate: (path: string) => void;
  children: ReactNode;
};

function RunRowContextMenu({
  workflowRunId,
  runPath,
  currentTags,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  selectedCount = 0,
  onNavigate,
  children,
}: Props) {
  const child = Children.only(children) as ReactElement<{
    className?: string;
    "data-row-active"?: string;
  }>;
  const isMultiSelect = selectedCount > 1;
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <ContextMenu onOpenChange={setMenuOpen}>
      <ContextMenuTrigger asChild>
        {cloneElement(child, {
          className: cn(child.props.className, "data-[row-active]:bg-muted/50"),
          "data-row-active": menuOpen ? "" : undefined,
        })}
      </ContextMenuTrigger>
      <ContextMenuContent className="w-56">
        {isMultiSelect ? (
          <>
            <ContextMenuLabel className="text-xs font-normal text-muted-foreground">
              Acts on this run only — use the Actions bar for all{" "}
              {selectedCount}.
            </ContextMenuLabel>
            <ContextMenuSeparator />
          </>
        ) : (
          <>
            <ContextMenuItem onSelect={() => onNavigate(runPath)}>
              <OpenInNewWindowIcon className="mr-2 h-4 w-4" />
              Open run
            </ContextMenuItem>
            <ContextMenuSeparator />
          </>
        )}
        <ContextMenuSub>
          <ContextMenuSubTrigger>
            <TokensIcon className="mr-2 h-4 w-4" />
            Tags
          </ContextMenuSubTrigger>
          <ContextMenuSubContent className="w-72 p-0">
            <RunTagPickerCommand
              workflowRunId={workflowRunId}
              tagKeys={tagKeys}
              labelSuggestions={labelSuggestions}
              valueSuggestionsByKey={valueSuggestionsByKey}
              currentTags={currentTags}
            />
          </ContextMenuSubContent>
        </ContextMenuSub>
      </ContextMenuContent>
    </ContextMenu>
  );
}

export { RunRowContextMenu };
