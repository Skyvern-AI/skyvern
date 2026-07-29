import { useEffect, useMemo, useState } from "react";
import {
  ChevronRightIcon,
  Pencil1Icon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { TableCell, TableRow } from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/util/utils";
import type { Folder } from "../../types/folderTypes";
import { WorkflowApiResponse } from "../../types/workflowTypes";
import { useFolderWorkflowsQuery } from "../../hooks/useFolderWorkflowsQuery";
import { DeleteFolderButton } from "../DeleteFolderButton";
import { EditFolderDialog } from "../EditFolderDialog";
import { WorkflowRow } from "./WorkflowRow";
import { useWorkflowsListContext } from "./WorkflowsListContext";

type FolderTreeNodeProps = {
  folder: Folder;
  isExpanded: boolean;
  isActive: boolean;
  onToggle: () => void;
  onContentsChange: (
    folderId: string,
    workflows: Array<WorkflowApiResponse>,
  ) => void;
  onCreateAgentInFolder: (folderId: string) => void;
  isCreatingAgent: boolean;
};

function FolderTreeNode({
  folder,
  isExpanded,
  isActive,
  onToggle,
  onContentsChange,
  onCreateAgentInFolder,
  isCreatingAgent,
}: FolderTreeNodeProps) {
  const { showCheckbox, columnCount } = useWorkflowsListContext();
  const [isEditDialogOpen, setIsEditDialogOpen] = useState(false);

  const { data, isFetching, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useFolderWorkflowsQuery({
      folderId: folder.folder_id,
      enabled: isExpanded,
    });

  const workflows = useMemo(
    () => data?.pages.flatMap((page) => page) ?? [],
    [data],
  );

  // Report the folder's currently loaded workflows up so the parent can build a
  // single flat selection set across every expanded folder. Collapsed folders
  // contribute nothing.
  useEffect(() => {
    onContentsChange(folder.folder_id, isExpanded ? workflows : []);
  }, [folder.folder_id, isExpanded, workflows, onContentsChange]);

  useEffect(() => {
    return () => onContentsChange(folder.folder_id, []);
  }, [folder.folder_id, onContentsChange]);

  const contentColSpan = columnCount - (showCheckbox ? 1 : 0);
  const isInitialLoading = isExpanded && isFetching && workflows.length === 0;
  const isEmpty = isExpanded && !isFetching && workflows.length === 0;

  return (
    <>
      <TableRow
        className={cn(
          "group/folder cursor-pointer select-none",
          isActive && "bg-blue-50 dark:bg-blue-950/20",
        )}
        role="button"
        tabIndex={0}
        aria-expanded={isExpanded}
        aria-label={`Folder ${folder.title}, ${folder.workflow_count} ${
          folder.workflow_count === 1 ? "agent" : "agents"
        }, ${isExpanded ? "expanded" : "collapsed"}`}
        onClick={onToggle}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onToggle();
          }
        }}
      >
        {showCheckbox && <TableCell />}
        <TableCell colSpan={contentColSpan}>
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <ChevronRightIcon
                className={cn(
                  "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                  isExpanded && "rotate-90",
                )}
              />
              <FolderIcon className="h-4 w-4 shrink-0 text-blue-700 dark:text-blue-400" />
              <span className="truncate font-medium" title={folder.title}>
                {folder.title}
              </span>
              <span className="shrink-0 text-xs text-muted-foreground">
                {folder.workflow_count}{" "}
                {folder.workflow_count === 1 ? "agent" : "agents"}
              </span>
              {isExpanded && isFetching && (
                <ReloadIcon className="h-3 w-3 shrink-0 animate-spin text-muted-foreground" />
              )}
            </div>
            <div
              className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover/folder:opacity-100"
              onClick={(event) => event.stopPropagation()}
              onKeyDown={(event) => event.stopPropagation()}
            >
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => onCreateAgentInFolder(folder.folder_id)}
                      disabled={isCreatingAgent}
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-slate-500/20 hover:text-tertiary-foreground disabled:cursor-not-allowed disabled:opacity-50"
                      aria-label="New agent in folder"
                    >
                      <PlusIcon className="h-4 w-4" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>New agent here</TooltipContent>
                </Tooltip>
              </TooltipProvider>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => setIsEditDialogOpen(true)}
                      className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-slate-500/20 hover:text-tertiary-foreground"
                      aria-label="Edit folder"
                    >
                      <Pencil1Icon className="h-4 w-4" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>Edit folder</TooltipContent>
                </Tooltip>
              </TooltipProvider>
              <DeleteFolderButton
                folderId={folder.folder_id}
                folderTitle={folder.title}
              />
            </div>
          </div>
        </TableCell>
      </TableRow>

      {isInitialLoading &&
        Array.from({ length: 2 }).map((_, index) => (
          <TableRow key={`${folder.folder_id}-skeleton-${index}`}>
            {showCheckbox && <TableCell />}
            <TableCell colSpan={contentColSpan}>
              <Skeleton className="ml-12 h-5 w-2/3" />
            </TableCell>
          </TableRow>
        ))}

      {isEmpty && (
        <TableRow>
          {showCheckbox && <TableCell />}
          <TableCell colSpan={contentColSpan}>
            <span className="ml-12 text-sm text-muted-foreground">
              This folder is empty
            </span>
          </TableCell>
        </TableRow>
      )}

      {isExpanded &&
        workflows.map((workflow) => (
          <WorkflowRow
            key={workflow.workflow_permanent_id}
            workflow={workflow}
            depth={1}
          />
        ))}

      {isExpanded && hasNextPage && (
        <TableRow>
          {showCheckbox && <TableCell />}
          <TableCell colSpan={contentColSpan}>
            <Button
              variant="link"
              size="sm"
              className="ml-12 h-auto p-0 text-blue-600 dark:text-blue-400"
              disabled={isFetchingNextPage}
              onClick={(event) => {
                event.stopPropagation();
                void fetchNextPage();
              }}
            >
              {isFetchingNextPage ? (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              ) : null}
              Load more agents
            </Button>
          </TableCell>
        </TableRow>
      )}

      <EditFolderDialog
        open={isEditDialogOpen}
        onOpenChange={setIsEditDialogOpen}
        folder={folder}
      />
    </>
  );
}

export { FolderTreeNode };
