import { createContext, useContext, type MouseEvent } from "react";
import type { ParameterDisplayItem } from "../ParameterDisplayInline";
import type { Folder } from "../../types/folderTypes";
import type { Tag, TagKey } from "../../types/tagTypes";
import type { TagColorMap } from "../../types/tagColors";

// Shared, read-only state and handlers consumed by every workflow row, whether
// the row is rendered flat (search results) or nested under a folder node. Lives
// in context so the folder tree can render rows arbitrarily deep without each
// level re-threading the same dozen props.
export interface WorkflowsListContextValue {
  showCheckbox: boolean;
  columnCount: number;
  debouncedSearch: string;
  isBulkOperating: boolean;
  selectedCount: number;
  foldersMap: ReadonlyMap<string, Folder>;
  workflowTagsMap: Record<string, Array<Tag>>;
  tagDescriptions: Map<string, string | null>;
  // (key, value) -> palette color for grouped tag chips; undefined until loaded.
  tagColors: TagColorMap | undefined;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey: Map<string, Array<string>>;
  isSelected: (id: string) => boolean;
  indexById: ReadonlyMap<string, number>;
  handleSelect: (index: number, shiftKey: boolean) => void;
  expandedRows: ReadonlySet<string>;
  toggleParametersExpanded: (id: string) => void;
  matchesParameter: (parameter: ParameterDisplayItem) => boolean;
  handleRowClick: (
    event: MouseEvent<HTMLTableCellElement>,
    workflowPermanentId: string,
  ) => void;
  handleIconClick: (event: MouseEvent<HTMLButtonElement>, path: string) => void;
  // Prune a single-row-deleted id from the selection so the header/bulk bar
  // can't get stuck on a gone row.
  onRowDeleted: (id: string) => void;
}

const WorkflowsListContext = createContext<WorkflowsListContextValue | null>(
  null,
);

function useWorkflowsListContext(): WorkflowsListContextValue {
  const context = useContext(WorkflowsListContext);
  if (!context) {
    throw new Error(
      "useWorkflowsListContext must be used within a WorkflowsListContext.Provider",
    );
  }
  return context;
}

export { WorkflowsListContext, useWorkflowsListContext };
