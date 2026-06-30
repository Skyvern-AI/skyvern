import * as React from "react";
import {
  BookmarkFilledIcon,
  BookmarkIcon,
  CopyIcon,
  DownloadIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
  TokensIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { cn } from "@/util/utils";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useUpdateWorkflowFolderMutation } from "../hooks/useFolderMutations";
import { useApplyWorkflowTagsMutation } from "../hooks/useWorkflowTagMutations";
import { useWorkflowRowActions } from "../hooks/useWorkflowRowActions";
import { workflowEditorPath } from "../studioNavigation";
import { Tag, TagKey } from "../types/tagTypes";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { FolderPickerCommand } from "./FolderPickerCommand";
import { TagPickerCommand } from "./tagging/TagPickerCommand";

type Props = {
  workflow: WorkflowApiResponse;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  onNavigate: (path: string) => void;
  // Lets the list prune this id from its multi-select set after a single-row
  // delete, so the selection header/bulk bar can't get stuck on a gone row.
  onDeleted?: (workflowId: string) => void;
  // Single-row nav (Open in editor / Run) hides when a multi-selection is active.
  selectedCount?: number;
  // Defaults to shown; only an explicit false (cloud flag off) hides tagging.
  taggingEnabled?: boolean;
  children: React.ReactNode;
};

function WorkflowRowContextMenu({
  workflow,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  onNavigate,
  onDeleted,
  selectedCount = 0,
  taggingEnabled = true,
  children,
}: Props) {
  const studioEnabled = useWorkflowStudioEnabled();
  const updateFolderMutation = useUpdateWorkflowFolderMutation();
  const applyTagsMutation = useApplyWorkflowTagsMutation();
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const [menuOpen, setMenuOpen] = React.useState(false);
  const [tagError, setTagError] = React.useState<string | null>(null);
  // Right-click still targets the single clicked row; until SKY-11504 lets it act
  // on the whole selection, steer multi-select to the bulk Actions bar.
  const isMultiSelect = selectedCount > 1;

  const {
    clone,
    toggleTemplate,
    exportAs,
    deleteWorkflow,
    isDeleting,
    isTogglingTemplate,
  } = useWorkflowRowActions(workflow);

  function applyTag(tag: Tag) {
    applyTagsMutation.mutate(
      {
        workflowPermanentId: workflow.workflow_permanent_id,
        data: { tags: [tag] },
      },
      {
        onSuccess: () => {
          const tagLabel =
            tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value;
          toast({ title: `Tagged with ${tagLabel}.`, variant: "success" });
        },
      },
    );
  }

  async function moveToFolder(folderId: string | null) {
    try {
      await updateFolderMutation.mutateAsync({
        workflowPermanentId: workflow.workflow_permanent_id,
        data: { folder_id: folderId },
      });
      toast({
        title: folderId ? "Moved to folder." : "Removed from folder.",
        variant: "success",
      });
    } catch (error) {
      toast({
        variant: "destructive",
        title: folderId
          ? "Failed to move agent to folder"
          : "Failed to remove agent from folder",
        description: error instanceof Error ? error.message : undefined,
      });
    }
  }

  // Keep the row highlighted while its context menu is open (the cursor leaves it).
  // Children.only throws a clear error if the trigger ever wraps more than one node.
  const child = React.Children.only(children) as React.ReactElement<{
    className?: string;
  }>;
  const rowProps = {
    className: cn(child.props.className, "data-[row-active]:bg-muted/50"),
    "data-row-active": menuOpen ? "" : undefined,
  };

  return (
    <Dialog
      open={deleteOpen}
      onOpenChange={(open) => {
        if (!isDeleting) {
          setDeleteOpen(open);
        }
      }}
    >
      <ContextMenu
        onOpenChange={(open) => {
          setMenuOpen(open);
          if (!open) {
            setTagError(null);
          }
        }}
      >
        <ContextMenuTrigger asChild>
          {React.cloneElement(child, rowProps)}
        </ContextMenuTrigger>
        <ContextMenuContent className="w-56">
          {isMultiSelect && (
            <>
              <ContextMenuLabel className="text-xs font-normal text-muted-foreground">
                Acts on this agent only — use the Actions bar for all{" "}
                {selectedCount}.
              </ContextMenuLabel>
              <ContextMenuSeparator />
            </>
          )}
          {selectedCount <= 1 && (
            <>
              <ContextMenuItem
                onSelect={() =>
                  onNavigate(
                    workflowEditorPath(
                      workflow.workflow_permanent_id,
                      studioEnabled,
                    ),
                  )
                }
              >
                <Pencil2Icon className="mr-2 h-4 w-4" />
                Open in editor
              </ContextMenuItem>
              <ContextMenuItem
                onSelect={() =>
                  onNavigate(`/workflows/${workflow.workflow_permanent_id}/run`)
                }
              >
                <PlayIcon className="mr-2 h-4 w-4" />
                Run
              </ContextMenuItem>
              <ContextMenuSeparator />
            </>
          )}
          {taggingEnabled ? (
            <ContextMenuSub
              onOpenChange={(open) => {
                if (!open) {
                  setTagError(null);
                }
              }}
            >
              <ContextMenuSubTrigger>
                <TokensIcon className="mr-2 h-4 w-4" />
                Tags
              </ContextMenuSubTrigger>
              <ContextMenuSubContent className="w-72 p-0">
                <TagPickerCommand
                  tagKeys={tagKeys}
                  labelSuggestions={labelSuggestions}
                  valueSuggestionsByKey={valueSuggestionsByKey}
                  error={tagError}
                  onErrorChange={setTagError}
                  onApply={applyTag}
                />
              </ContextMenuSubContent>
            </ContextMenuSub>
          ) : null}
          <ContextMenuSub>
            <ContextMenuSubTrigger>
              <FolderIcon className="mr-2 h-4 w-4" />
              Move to folder
            </ContextMenuSubTrigger>
            <ContextMenuSubContent className="w-72 p-0">
              <FolderPickerCommand
                currentFolderId={workflow.folder_id ?? null}
                onSelect={(folderId) => void moveToFolder(folderId)}
              />
            </ContextMenuSubContent>
          </ContextMenuSub>
          <ContextMenuSeparator />
          <ContextMenuItem onSelect={() => clone()} disabled={isMultiSelect}>
            <CopyIcon className="mr-2 h-4 w-4" />
            Clone
          </ContextMenuItem>
          <ContextMenuItem
            onSelect={() => toggleTemplate()}
            disabled={isMultiSelect || isTogglingTemplate}
          >
            {workflow.is_template ? (
              <BookmarkFilledIcon className="mr-2 h-4 w-4" />
            ) : (
              <BookmarkIcon className="mr-2 h-4 w-4" />
            )}
            {workflow.is_template ? "Remove from template" : "Save as template"}
          </ContextMenuItem>
          <ContextMenuSub>
            <ContextMenuSubTrigger disabled={isMultiSelect}>
              <DownloadIcon className="mr-2 h-4 w-4" />
              Export
            </ContextMenuSubTrigger>
            <ContextMenuSubContent>
              <ContextMenuItem onSelect={() => exportAs("yaml")}>
                YAML
              </ContextMenuItem>
              <ContextMenuItem onSelect={() => exportAs("json")}>
                JSON
              </ContextMenuItem>
            </ContextMenuSubContent>
          </ContextMenuSub>
          <ContextMenuSeparator />
          <DialogTrigger asChild>
            <ContextMenuItem
              className="text-destructive focus:text-destructive"
              disabled={isMultiSelect}
            >
              <GarbageIcon className="mr-2 h-4 w-4 text-destructive" />
              Delete
            </ContextMenuItem>
          </DialogTrigger>
        </ContextMenuContent>
      </ContextMenu>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Are you sure?</DialogTitle>
          <DialogDescription>
            The agent{" "}
            <span className="font-semibold text-primary">{workflow.title}</span>{" "}
            will be deleted.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            variant="secondary"
            onClick={() => setDeleteOpen(false)}
            disabled={isDeleting}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() =>
              deleteWorkflow({
                onSuccess: () => {
                  setDeleteOpen(false);
                  onDeleted?.(workflow.workflow_permanent_id);
                },
              })
            }
            disabled={isDeleting}
          >
            {isDeleting && <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { WorkflowRowContextMenu };
