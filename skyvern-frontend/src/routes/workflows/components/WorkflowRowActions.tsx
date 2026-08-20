import * as React from "react";
import {
  BookmarkFilledIcon,
  BookmarkIcon,
  CopyIcon,
  DownloadIcon,
  Pencil2Icon,
  PlayIcon,
  TokensIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
import {
  RowActionsContextMenu,
  RowActionsKebab,
  type RowActionItem,
} from "@/components/RowActions";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { toast } from "@/components/ui/use-toast";
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
  currentTags?: Array<Tag>;
  onNavigate: (path: string) => void;
  // Lets the list prune this id from its multi-select set after a single-row
  // delete, so the selection header/bulk bar can't get stuck on a gone row.
  onDeleted?: (workflowId: string) => void;
  // Single-row nav (Open in editor / Run) hides when a multi-selection is active.
  selectedCount?: number;
  // Defaults to shown; only an explicit false (cloud flag off) hides tagging.
  taggingEnabled?: boolean;
  // The row renderer; place the provided kebab last in the actions cell. It is
  // null while a selection is active (row menus yield to the bulk bar).
  children: (kebab: React.ReactNode) => React.ReactElement;
};

function WorkflowRowActions({
  workflow,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  currentTags,
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

  function removeTag(tag: Tag) {
    applyTagsMutation.mutate(
      {
        workflowPermanentId: workflow.workflow_permanent_id,
        data: {
          tags_to_delete: [
            tag.key !== null ? { key: tag.key } : { value: tag.value },
          ],
        },
      },
      {
        onSuccess: () => {
          const tagLabel =
            tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value;
          toast({ title: `Removed ${tagLabel}.`, variant: "success" });
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

  const items: Array<RowActionItem> = [];
  if (isMultiSelect) {
    items.push(
      {
        kind: "note",
        label: (
          <>
            Acts on this agent only — use the Actions bar for all{" "}
            {selectedCount}.
          </>
        ),
      },
      { kind: "separator" },
    );
  }
  if (selectedCount <= 1) {
    items.push(
      {
        kind: "item",
        label: "Open in editor",
        icon: <Pencil2Icon className="mr-2 h-4 w-4" />,
        onSelect: () =>
          onNavigate(
            workflowEditorPath(workflow.workflow_permanent_id, studioEnabled),
          ),
      },
      {
        kind: "item",
        label: "Run",
        icon: <PlayIcon className="mr-2 h-4 w-4" />,
        onSelect: () =>
          onNavigate(`/agents/${workflow.workflow_permanent_id}/run`),
      },
      { kind: "separator" },
    );
  }
  if (taggingEnabled) {
    items.push({
      kind: "sub",
      label: "Tags",
      icon: <TokensIcon className="mr-2 h-4 w-4" />,
      contentClassName: "w-72 p-0",
      onOpenChange: (open) => {
        if (!open) {
          setTagError(null);
        }
      },
      content: (
        <TagPickerCommand
          tagKeys={tagKeys}
          labelSuggestions={labelSuggestions}
          valueSuggestionsByKey={valueSuggestionsByKey}
          currentTags={currentTags}
          error={tagError}
          onErrorChange={setTagError}
          disabled={applyTagsMutation.isPending}
          onApply={applyTag}
          onRemove={removeTag}
        />
      ),
    });
  }
  items.push(
    {
      kind: "sub",
      label: "Move to folder",
      icon: <FolderIcon className="mr-2 h-4 w-4" />,
      contentClassName: "w-72 p-0",
      content: (
        <FolderPickerCommand
          currentFolderId={workflow.folder_id ?? null}
          onSelect={(folderId) => void moveToFolder(folderId)}
        />
      ),
    },
    { kind: "separator" },
    {
      kind: "item",
      label: "Duplicate Agent",
      icon: <CopyIcon className="mr-2 h-4 w-4" />,
      onSelect: () => clone(),
      disabled: isMultiSelect,
    },
    {
      kind: "item",
      label: workflow.is_template ? "Remove from template" : "Save as template",
      icon: workflow.is_template ? (
        <BookmarkFilledIcon className="mr-2 h-4 w-4" />
      ) : (
        <BookmarkIcon className="mr-2 h-4 w-4" />
      ),
      onSelect: () => toggleTemplate(),
      disabled: isMultiSelect || isTogglingTemplate,
    },
    {
      kind: "sub",
      label: "Export",
      icon: <DownloadIcon className="mr-2 h-4 w-4" />,
      disabled: isMultiSelect,
      items: [
        { label: "YAML", onSelect: () => exportAs("yaml") },
        { label: "JSON", onSelect: () => exportAs("json") },
      ],
    },
    { kind: "separator" },
    {
      kind: "item",
      label: "Delete",
      icon: <GarbageIcon className="mr-2 h-4 w-4 text-destructive" />,
      destructive: true,
      disabled: isMultiSelect,
      onSelect: () => setDeleteOpen(true),
    },
  );

  // Row kebabs yield to the bulk Actions bar while any selection is active.
  const kebab =
    selectedCount > 0 ? null : (
      <RowActionsKebab
        items={items}
        ariaLabel={`Actions for ${workflow.title}`}
      />
    );

  return (
    <>
      <RowActionsContextMenu
        items={items}
        onOpenChange={(open) => {
          if (!open) {
            setTagError(null);
          }
        }}
      >
        {children(kebab)}
      </RowActionsContextMenu>
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete agent?"
        description={
          <p>
            The agent{" "}
            <span className="font-semibold text-primary">{workflow.title}</span>{" "}
            will be permanently deleted.
          </p>
        }
        isPending={isDeleting}
        onConfirm={() =>
          deleteWorkflow({
            onSuccess: () => {
              setDeleteOpen(false);
              onDeleted?.(workflow.workflow_permanent_id);
            },
          })
        }
      />
    </>
  );
}

export { WorkflowRowActions };
