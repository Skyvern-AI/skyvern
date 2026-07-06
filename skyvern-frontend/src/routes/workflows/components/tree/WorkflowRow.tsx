import React from "react";
import {
  BookmarkFilledIcon,
  MixerHorizontalIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { Button } from "@/components/ui/button";
import { SelectionCheckboxCell } from "@/components/SelectionCheckbox";
import { TableCell, TableRow } from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { basicTimeFormat, compactLocalDateTime } from "@/util/timeFormat";
import { WorkflowApiResponse } from "../../types/workflowTypes";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";
import { workflowEditorPath } from "../../studioNavigation";
import { HighlightText } from "../HighlightText";
import { ParameterDisplayInline } from "../ParameterDisplayInline";
import { TagChipList } from "../tagging/TagChipList";
import { WorkflowRowContextMenu } from "../WorkflowRowContextMenu";
import { useWorkflowsListContext } from "./WorkflowsListContext";
import { useNavigate } from "react-router-dom";

// Pixels of left padding added per level of folder nesting. Level 0 (flat search
// results and ungrouped agents) uses the cell's default padding. depth 1 lands
// at 60px so a folder's agents line up under the folder title text (cell px-3 +
// chevron + icon + gaps).
const INDENT_STEP_PX = 28;
const INDENT_BASE_PX = 32;

function indentStyle(depth: number): React.CSSProperties | undefined {
  if (depth <= 0) {
    return undefined;
  }
  return { paddingLeft: `${INDENT_BASE_PX + depth * INDENT_STEP_PX}px` };
}

type WorkflowRowProps = {
  workflow: WorkflowApiResponse;
  // Folder nesting depth; 0 = top level / flat list.
  depth?: number;
};

function WorkflowRow({ workflow, depth = 0 }: WorkflowRowProps) {
  const {
    showCheckbox,
    columnCount,
    debouncedSearch,
    selectedCount,
    foldersMap,
    workflowTagsMap,
    tagDescriptions,
    tagColors,
    tagKeys,
    labelSuggestions,
    valueSuggestionsByKey,
    isSelected,
    indexById,
    handleSelect,
    expandedRows,
    toggleParametersExpanded,
    matchesParameter,
    handleRowClick,
    handleIconClick,
    onRowDeleted,
  } = useWorkflowsListContext();
  const studioEnabled = useWorkflowStudioEnabled();
  const navigate = useNavigate();
  // undefined (OSS / pre-load) shows tagging; only an explicit cloud `false` hides it.
  const taggingEnabled = useFeatureFlag(WORKFLOW_TAGGING_FLAG) !== false;

  const parameterItems = (workflow.workflow_definition?.parameters ?? [])
    .filter((p) => p.parameter_type !== "output")
    .map((param) => ({
      key: param.key,
      value:
        param.parameter_type === "workflow" ? (param.default_value ?? "") : "",
      description: param.description ?? null,
    }));
  const hasParameters = parameterItems.length > 0;
  const isExpanded = expandedRows.has(workflow.workflow_permanent_id);
  const isRowSelected = isSelected(workflow.workflow_permanent_id);
  const isUploading = workflow.status === "importing";
  const workflowTags = workflowTagsMap[workflow.workflow_permanent_id];
  const selectableIndex = isUploading
    ? -1
    : (indexById.get(workflow.workflow_permanent_id) ?? -1);
  const firstCellStyle = indentStyle(depth);

  if (isUploading) {
    return (
      <TableRow className="opacity-70">
        {showCheckbox && <TableCell />}
        <TableCell colSpan={2} style={firstCellStyle}>
          <div className="flex min-w-0 items-center gap-2">
            <ReloadIcon className="h-4 w-4 shrink-0 animate-spin text-blue-400" />
            <span className="truncate" title={workflow.title}>
              {workflow.title}
            </span>
          </div>
        </TableCell>
        <TableCell>
          <span className="text-muted-foreground">-</span>
        </TableCell>
        <TableCell className="text-muted-foreground">
          {compactLocalDateTime(workflow.created_at)}
        </TableCell>
        <TableCell>
          <div className="flex justify-end gap-0.5">
            <Button size="icon" variant="ghost" disabled>
              <PlayIcon className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="ghost" disabled>
              <Pencil2Icon className="h-4 w-4" />
            </Button>
          </div>
        </TableCell>
      </TableRow>
    );
  }

  return (
    <React.Fragment>
      <WorkflowRowContextMenu
        workflow={workflow}
        tagKeys={tagKeys}
        labelSuggestions={labelSuggestions}
        valueSuggestionsByKey={valueSuggestionsByKey}
        selectedCount={selectedCount}
        taggingEnabled={taggingEnabled}
        onNavigate={(path) => navigate(path)}
        onDeleted={onRowDeleted}
      >
        <TableRow
          className="group/row cursor-pointer select-none"
          data-state={isRowSelected ? "selected" : undefined}
        >
          {showCheckbox && (
            <SelectionCheckboxCell
              className="select-none"
              index={selectableIndex}
              checked={isRowSelected}
              hasSelection={selectedCount > 0}
              onSelect={handleSelect}
              ariaLabel={`Select ${workflow.title}`}
            />
          )}
          <TableCell
            style={firstCellStyle}
            onClick={(event) => {
              handleRowClick(event, workflow.workflow_permanent_id);
            }}
          >
            <div
              className="truncate font-mono text-xs text-muted-foreground"
              title={workflow.workflow_permanent_id}
            >
              <HighlightText
                text={workflow.workflow_permanent_id}
                query={debouncedSearch}
              />
            </div>
          </TableCell>
          <TableCell
            onClick={(event) => {
              handleRowClick(event, workflow.workflow_permanent_id);
            }}
          >
            <div className="flex min-w-0 flex-col gap-1">
              <div className="flex min-w-0 items-center gap-2">
                <span className="truncate" title={workflow.title}>
                  <HighlightText
                    text={workflow.title}
                    query={debouncedSearch}
                  />
                </span>
                {workflow.is_template && (
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <BookmarkFilledIcon className="h-3.5 w-3.5 shrink-0 text-blue-500" />
                      </TooltipTrigger>
                      <TooltipContent>Template</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )}
              </div>
              {taggingEnabled && workflowTags && workflowTags.length > 0 ? (
                <TagChipList
                  tags={workflowTags}
                  descriptions={tagDescriptions}
                  colors={tagColors}
                />
              ) : null}
            </div>
          </TableCell>
          <TableCell
            onClick={(event) => {
              handleRowClick(event, workflow.workflow_permanent_id);
            }}
          >
            {workflow.folder_id ? (
              <div className="flex min-w-0 items-center gap-1.5">
                <FolderIcon className="h-3.5 w-3.5 shrink-0 text-blue-400" />
                <span
                  className="truncate text-sm"
                  title={
                    foldersMap.get(workflow.folder_id)?.title ||
                    workflow.folder_id
                  }
                >
                  <HighlightText
                    text={
                      foldersMap.get(workflow.folder_id)?.title ||
                      workflow.folder_id
                    }
                    query={debouncedSearch}
                  />
                </span>
              </div>
            ) : (
              <span className="text-muted-foreground">-</span>
            )}
          </TableCell>
          <TableCell
            onClick={(event) => {
              handleRowClick(event, workflow.workflow_permanent_id);
            }}
            className="text-muted-foreground"
            title={basicTimeFormat(workflow.created_at)}
          >
            {compactLocalDateTime(workflow.created_at)}
          </TableCell>
          <TableCell>
            <div className="flex justify-end gap-0.5">
              {hasParameters && (
                <TooltipProvider>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        size="icon"
                        variant="ghost"
                        className="text-muted-foreground hover:text-foreground"
                        onClick={() =>
                          toggleParametersExpanded(
                            workflow.workflow_permanent_id,
                          )
                        }
                      >
                        <MixerHorizontalIcon className="h-4 w-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      {isExpanded ? "Hide parameters" : "Show parameters"}
                    </TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              )}
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="text-cta hover:text-cta"
                      onClick={(event) => {
                        handleIconClick(
                          event,
                          `/agents/${workflow.workflow_permanent_id}/run`,
                        );
                      }}
                    >
                      <PlayIcon className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Create New Run</TooltipContent>
                </Tooltip>
              </TooltipProvider>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      size="icon"
                      variant="ghost"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={(event) => {
                        handleIconClick(
                          event,
                          workflowEditorPath(
                            workflow.workflow_permanent_id,
                            studioEnabled,
                          ),
                        );
                      }}
                    >
                      <Pencil2Icon className="h-4 w-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Open in Editor</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>
          </TableCell>
        </TableRow>
      </WorkflowRowContextMenu>

      {isExpanded && hasParameters && (
        <TableRow>
          <TableCell
            colSpan={columnCount}
            className="bg-slate-50 dark:bg-slate-900/50"
          >
            <ParameterDisplayInline
              parameters={parameterItems}
              searchQuery={debouncedSearch}
              keywordMatchesParameter={matchesParameter}
            />
          </TableCell>
        </TableRow>
      )}
    </React.Fragment>
  );
}

export { WorkflowRow };
