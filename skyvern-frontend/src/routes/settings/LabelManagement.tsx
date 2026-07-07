import * as React from "react";
import { isAxiosError } from "axios";
import {
  CheckIcon,
  Pencil1Icon,
  ReloadIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/use-toast";
import { cn } from "@/util/utils";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";
import { TagChip } from "@/routes/workflows/components/tagging/TagChip";
import { TagColorSwatchPicker } from "@/routes/workflows/components/tagging/TagColorSwatchPicker";
import { useTagValuesListQuery } from "@/routes/workflows/hooks/useTagValuesQuery";
import {
  tagErrorMessage,
  useDeleteTagValueMutation,
  useRecolorTagValueMutation,
  useRenameTagValueMutation,
} from "@/routes/workflows/hooks/useWorkflowTagMutations";
import {
  isPaletteColorName,
  paletteDotClass,
  type PaletteColorName,
} from "@/routes/workflows/types/tagColors";
import {
  validateTagValue,
  type TagValue,
} from "@/routes/workflows/types/tagTypes";

type LabelGroup = { key: string; values: Array<TagValue> };

function groupLabels(tagValues: Array<TagValue>): Array<LabelGroup> {
  const byKey = new Map<string, Array<TagValue>>();
  for (const row of tagValues) {
    const list = byKey.get(row.key) ?? [];
    list.push(row);
    byKey.set(row.key, list);
  }
  return [...byKey.entries()]
    .map(([key, values]) => ({
      key,
      values: [...values].sort((a, b) => a.value.localeCompare(b.value)),
    }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

function usageLabel(count: number): string {
  return `${count} workflow${count === 1 ? "" : "s"}`;
}

type LabelRowProps = {
  label: TagValue;
  onRequestDelete: (label: TagValue) => void;
};

function LabelRow({ label, onRequestDelete }: LabelRowProps) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState(label.value);
  const [error, setError] = React.useState<string | null>(null);
  const [colorOpen, setColorOpen] = React.useState(false);

  const renameMutation = useRenameTagValueMutation();
  const recolorMutation = useRecolorTagValueMutation();

  const count = label.workflow_count ?? 0;
  const selectedColor: PaletteColorName = isPaletteColorName(label.color)
    ? label.color
    : "gray";

  function startEditing() {
    setDraft(label.value);
    setError(null);
    setEditing(true);
  }

  function cancelEditing() {
    setEditing(false);
    setError(null);
  }

  function submitRename() {
    const trimmed = draft.trim();
    const validationError = validateTagValue(trimmed, { hasKey: true });
    if (validationError) {
      setError(validationError);
      return;
    }
    if (trimmed === label.value) {
      cancelEditing();
      return;
    }
    renameMutation.mutate(
      { key: label.key, value: label.value, newValue: trimmed },
      {
        onSuccess: () => setEditing(false),
        onError: (mutationError) => {
          if (
            isAxiosError(mutationError) &&
            mutationError.response?.status === 409
          ) {
            setError("A label with that name already exists in this group.");
            return;
          }
          toast({
            variant: "destructive",
            title: "Failed to rename label",
            description: tagErrorMessage(mutationError),
          });
        },
      },
    );
  }

  function handleRecolor(color: PaletteColorName) {
    setColorOpen(false);
    if (color === selectedColor) {
      return;
    }
    recolorMutation.mutate({ key: label.key, value: label.value, color });
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-1 py-1.5">
        <div className="flex items-center gap-2">
          <Input
            value={draft}
            autoFocus
            aria-label={`Rename label ${label.value}`}
            disabled={renameMutation.isPending}
            onChange={(event) => {
              setDraft(event.target.value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                submitRename();
              } else if (event.key === "Escape") {
                event.preventDefault();
                cancelEditing();
              }
            }}
            className="h-8 max-w-xs"
          />
          <Button
            size="sm"
            className="gap-1"
            disabled={renameMutation.isPending}
            onClick={submitRename}
          >
            {renameMutation.isPending ? (
              <ReloadIcon className="h-4 w-4 animate-spin" />
            ) : (
              <CheckIcon className="h-4 w-4" />
            )}
            Save
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={renameMutation.isPending}
            onClick={cancelEditing}
          >
            Cancel
          </Button>
        </div>
        {error ? <p className="text-xs text-destructive">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between gap-2 py-1.5">
      <div className="flex min-w-0 items-center gap-2">
        <TagChip tagKey={label.key} value={label.value} color={label.color} />
        <span className="shrink-0 text-xs text-muted-foreground">
          {usageLabel(count)}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Popover open={colorOpen} onOpenChange={setColorOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Change color for ${label.value}`}
              disabled={recolorMutation.isPending}
              className="flex h-7 w-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted disabled:opacity-50"
            >
              <span
                aria-hidden="true"
                className={cn(
                  "inline-block h-3.5 w-3.5 rounded-full",
                  paletteDotClass(label.color) || "bg-muted-foreground/40",
                )}
              />
            </button>
          </PopoverTrigger>
          <PopoverContent className="w-auto p-2" align="end">
            <TagColorSwatchPicker
              value={selectedColor}
              onChange={handleRecolor}
            />
          </PopoverContent>
        </Popover>
        <button
          type="button"
          aria-label={`Rename ${label.value}`}
          onClick={startEditing}
          className="flex h-7 w-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted"
        >
          <Pencil1Icon className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          aria-label={`Delete ${label.value}`}
          onClick={() => onRequestDelete(label)}
          className="flex h-7 w-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted hover:text-destructive"
        >
          <TrashIcon className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

function LabelManagement() {
  const taggingEnabled = useFeatureFlag(WORKFLOW_TAGGING_FLAG) !== false;
  const { data: tagValues = [], isPending } = useTagValuesListQuery({
    enabled: taggingEnabled,
  });
  const [labelToDelete, setLabelToDelete] = React.useState<TagValue | null>(
    null,
  );
  const deleteMutation = useDeleteTagValueMutation();

  const groups = React.useMemo(() => groupLabels(tagValues), [tagValues]);
  const deleteCount = labelToDelete?.workflow_count ?? 0;

  function confirmDelete() {
    if (!labelToDelete) {
      return;
    }
    deleteMutation.mutate(
      { key: labelToDelete.key, value: labelToDelete.value },
      { onSuccess: () => setLabelToDelete(null) },
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Labels</CardTitle>
          <CardDescription>
            Manage the grouped labels used to organize your workflows. Rename,
            recolor, or remove a label — changes apply everywhere it's used.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-8">
          {taggingEnabled && isPending ? (
            <div className="flex flex-col gap-3">
              <Skeleton className="h-6 w-40" />
              <Skeleton className="h-6 w-full" />
              <Skeleton className="h-6 w-full" />
            </div>
          ) : groups.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No labels yet. Grouped labels you add to workflows (group:label)
              appear here for management.
            </p>
          ) : (
            <div className="flex flex-col gap-6">
              {groups.map((group) => (
                <div key={group.key} className="flex flex-col gap-1">
                  <div className="flex items-baseline gap-2">
                    <h3 className="text-sm font-medium">{group.key}</h3>
                    <span className="text-xs text-muted-foreground">
                      {group.values.length} label
                      {group.values.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <div className="divide-y divide-border">
                    {group.values.map((label) => (
                      <LabelRow
                        key={`${label.key}:${label.value}`}
                        label={label}
                        onRequestDelete={setLabelToDelete}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={labelToDelete !== null}
        onOpenChange={(next) => {
          if (!next && !deleteMutation.isPending) {
            setLabelToDelete(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete label “{labelToDelete?.value}”?</DialogTitle>
            <DialogDescription>
              This removes it from {usageLabel(deleteCount)} and from the “
              {labelToDelete?.key}” group. This can’t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setLabelToDelete(null)}
              disabled={deleteMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              className="gap-2"
              disabled={deleteMutation.isPending}
              onClick={confirmDelete}
            >
              {deleteMutation.isPending ? (
                <ReloadIcon className="h-4 w-4 animate-spin" />
              ) : null}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export { LabelManagement };
