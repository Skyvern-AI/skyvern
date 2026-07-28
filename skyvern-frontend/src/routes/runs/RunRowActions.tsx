import {
  OpenInNewWindowIcon,
  ReloadIcon,
  TokensIcon,
} from "@radix-ui/react-icons";
import { type ReactElement, type ReactNode } from "react";

import {
  RowActionsContextMenu,
  RowActionsKebab,
  type RowActionItem,
} from "@/components/RowActions";
import { RunTagPickerCommand } from "@/routes/tasks/components/tagging/RunTagPickerCommand";
import type { Tag, TagKey } from "@/routes/workflows/types/tagTypes";

type Props = {
  runId: string;
  runPath: string;
  // Set for task-family rows that support /tasks/create/retry/{id}.
  rerunPath?: string | null;
  // Workflow-run rows with tagging enabled get the Tags submenu.
  taggable?: boolean;
  currentTags?: Array<Tag>;
  tagKeys?: Array<TagKey>;
  labelSuggestions?: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  selectedCount?: number;
  onNavigate: (path: string) => void;
  // The row renderer; place the provided kebab last in the actions cell. It is
  // null while a selection is active (row menus yield to the bulk bar).
  children: (kebab: ReactNode) => ReactElement;
};

function RunRowActions({
  runId,
  runPath,
  rerunPath,
  taggable = false,
  currentTags = [],
  tagKeys = [],
  labelSuggestions = [],
  valueSuggestionsByKey,
  selectedCount = 0,
  onNavigate,
  children,
}: Props) {
  const isMultiSelect = selectedCount > 1;

  const items: Array<RowActionItem> = [];
  if (isMultiSelect) {
    items.push(
      {
        kind: "note",
        label: (
          <>
            Acts on this run only — use the Actions bar for all {selectedCount}.
          </>
        ),
      },
      { kind: "separator" },
    );
  } else {
    items.push({
      kind: "item",
      label: "Open run",
      icon: <OpenInNewWindowIcon className="mr-2 h-4 w-4" />,
      onSelect: () => onNavigate(runPath),
    });
    if (rerunPath) {
      items.push({
        kind: "item",
        label: "Rerun task",
        icon: <ReloadIcon className="mr-2 h-4 w-4" />,
        onSelect: () => onNavigate(rerunPath),
      });
    }
    if (taggable) {
      items.push({ kind: "separator" });
    }
  }
  if (taggable) {
    items.push({
      kind: "sub",
      label: "Tags",
      icon: <TokensIcon className="mr-2 h-4 w-4" />,
      contentClassName: "w-72 p-0",
      content: (
        <RunTagPickerCommand
          workflowRunId={runId}
          tagKeys={tagKeys}
          labelSuggestions={labelSuggestions}
          valueSuggestionsByKey={valueSuggestionsByKey}
          currentTags={currentTags}
        />
      ),
    });
  }

  // Row kebabs yield to the bulk Actions bar while any selection is active.
  const kebab =
    selectedCount > 0 ? null : (
      <RowActionsKebab items={items} ariaLabel={`Actions for run ${runId}`} />
    );

  return (
    <RowActionsContextMenu items={items}>
      {children(kebab)}
    </RowActionsContextMenu>
  );
}

export { RunRowActions };
