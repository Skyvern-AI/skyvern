import * as React from "react";
import { isAxiosError } from "axios";
import {
  CheckIcon,
  Pencil1Icon,
  PlusIcon,
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
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Search } from "@/components/ui/search";
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
  useCreateTagValueMutation,
  useDeleteTagValueMutation,
  useRecolorTagValueMutation,
  useRenameTagValueMutation,
} from "@/routes/workflows/hooks/useWorkflowTagMutations";
import {
  isPaletteColorName,
  paletteDotClass,
  randomPaletteColor,
  type PaletteColorName,
} from "@/routes/workflows/types/tagColors";
import {
  isSystemTagKey,
  validateTagKey,
  validateTagValue,
  type TagValue,
} from "@/routes/workflows/types/tagTypes";

type LabelGroup = { key: string; values: Array<TagValue> };

function groupLabels(tagValues: Array<TagValue>): Array<LabelGroup> {
  const byKey = new Map<string, Array<TagValue>>();
  // Reserved skyvern.* labels are system-managed (not user-editable), so they
  // don't belong on a management surface; they stay visible on runs/filters.
  for (const row of tagValues.filter((row) => !isSystemTagKey(row.key))) {
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

// Case-insensitive filter: a query matching a group name keeps the whole group;
// otherwise groups are narrowed to their matching values, and emptied groups drop.
function filterGroups(
  groups: Array<LabelGroup>,
  query: string,
): Array<LabelGroup> {
  const q = query.trim().toLowerCase();
  if (!q) {
    return groups;
  }
  return groups
    .map((group) =>
      group.key.toLowerCase().includes(q)
        ? group
        : {
            key: group.key,
            values: group.values.filter((row) =>
              row.value.toLowerCase().includes(q),
            ),
          },
    )
    .filter((group) => group.values.length > 0);
}

function usageLabel(count: number): string {
  return `${count} agent${count === 1 ? "" : "s"}`;
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

  const iconButtonClass =
    "flex h-7 w-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted " +
    "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

  return (
    <div className="group -mx-2 flex items-center justify-between gap-2 rounded-sm px-2 py-1.5 hover:bg-muted/40">
      <div className="flex min-w-0 items-center gap-2">
        <TagChip
          tagKey={label.key}
          value={label.value}
          color={label.color}
          hideKey
        />
        <span className="shrink-0 text-xs text-muted-foreground">
          {usageLabel(count)}
        </span>
      </div>
      {/* Revealed on hover AND focus-within (opacity only, so buttons stay
          tabbable and the row height never shifts). */}
      <div
        className={cn(
          "flex shrink-0 items-center gap-1 opacity-0 transition-opacity",
          "group-focus-within:opacity-100 group-hover:opacity-100",
          colorOpen && "opacity-100",
        )}
      >
        <Popover open={colorOpen} onOpenChange={setColorOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={`Change color for ${label.value}`}
              disabled={recolorMutation.isPending}
              className={cn(iconButtonClass, "disabled:opacity-50")}
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
          className={iconButtonClass}
        >
          <Pencil1Icon className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          aria-label={`Delete ${label.value}`}
          onClick={() => onRequestDelete(label)}
          className={cn(iconButtonClass, "hover:text-destructive")}
        >
          <TrashIcon className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}

type CreateLabelDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existingGroups: Array<string>;
};

function CreateLabelDialog({
  open,
  onOpenChange,
  existingGroups,
}: CreateLabelDialogProps) {
  const [group, setGroup] = React.useState("");
  const [value, setValue] = React.useState("");
  const [color, setColor] = React.useState<PaletteColorName>("gray");
  const [error, setError] = React.useState<string | null>(null);
  const createMutation = useCreateTagValueMutation();

  React.useEffect(() => {
    if (open) {
      setGroup("");
      setValue("");
      setColor(randomPaletteColor());
      setError(null);
    }
  }, [open]);

  const trimmedGroup = group.trim().toLowerCase();
  const suggestions = existingGroups
    .filter(
      (key) => key.toLowerCase().includes(trimmedGroup) && key !== group.trim(),
    )
    .slice(0, 6);

  function submit() {
    const groupError = validateTagKey(group);
    if (groupError) {
      setError(groupError);
      return;
    }
    const valueError = validateTagValue(value, { hasKey: true });
    if (valueError) {
      setError(valueError);
      return;
    }
    createMutation.mutate(
      { key: group.trim(), value: value.trim(), color },
      {
        onSuccess: () => onOpenChange(false),
        onError: (mutationError) => {
          if (
            isAxiosError(mutationError) &&
            mutationError.response?.status === 409
          ) {
            setError("That label already exists in this group.");
            return;
          }
          setError(tagErrorMessage(mutationError));
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!createMutation.isPending) {
          onOpenChange(next);
        }
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New label</DialogTitle>
          <DialogDescription>
            Labels are grouped as group:label. A new label counts 0 agents until
            you tag one with it.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-label-group">Group</Label>
            <Input
              id="new-label-group"
              value={group}
              placeholder="e.g. env"
              disabled={createMutation.isPending}
              onChange={(event) => {
                setGroup(event.target.value);
                setError(null);
              }}
            />
            {suggestions.length > 0 ? (
              <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                <span className="text-xs text-muted-foreground">Existing:</span>
                {suggestions.map((key) => (
                  <button
                    key={key}
                    type="button"
                    className="rounded-sm bg-muted px-1.5 py-0.5 text-xs hover:bg-muted/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    onClick={() => {
                      setGroup(key);
                      setError(null);
                    }}
                  >
                    {key}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-label-value">Label</Label>
            <Input
              id="new-label-value"
              value={value}
              placeholder="e.g. production"
              disabled={createMutation.isPending}
              onChange={(event) => {
                setValue(event.target.value);
                setError(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  submit();
                }
              }}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Color</Label>
            <TagColorSwatchPicker value={color} onChange={setColor} />
          </div>
          {error ? <p className="text-xs text-destructive">{error}</p> : null}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={createMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            className="gap-2"
            onClick={submit}
            disabled={createMutation.isPending}
          >
            {createMutation.isPending ? (
              <ReloadIcon className="h-4 w-4 animate-spin" />
            ) : null}
            Create label
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function LabelManagement() {
  const taggingEnabled = useFeatureFlag(WORKFLOW_TAGGING_FLAG) !== false;
  const { data: tagValues = [], isPending } = useTagValuesListQuery({
    enabled: taggingEnabled,
  });
  const [query, setQuery] = React.useState("");
  const [createOpen, setCreateOpen] = React.useState(false);
  const [labelToDelete, setLabelToDelete] = React.useState<TagValue | null>(
    null,
  );
  const deleteMutation = useDeleteTagValueMutation();

  const groups = React.useMemo(() => groupLabels(tagValues), [tagValues]);
  const visibleGroups = React.useMemo(
    () => filterGroups(groups, query),
    [groups, query],
  );
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
            Manage the grouped labels used to organize your agents. Create,
            rename, recolor, or remove a label — changes apply everywhere it's
            used.
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
            <div className="flex flex-col items-start gap-3">
              <p className="text-sm text-muted-foreground">
                No labels yet. Create one here, or add grouped labels
                (group:label) to agents and they appear here for management.
              </p>
              <Button className="gap-1" onClick={() => setCreateOpen(true)}>
                <PlusIcon className="h-4 w-4" />
                New label
              </Button>
            </div>
          ) : (
            <div className="flex flex-col gap-6">
              <div className="flex items-center justify-between gap-3">
                <Search
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search labels…"
                  label="Search labels"
                  className="h-10 w-72"
                />
                <Button className="gap-1" onClick={() => setCreateOpen(true)}>
                  <PlusIcon className="h-4 w-4" />
                  New label
                </Button>
              </div>
              {visibleGroups.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No labels match “{query.trim()}”.
                </p>
              ) : (
                visibleGroups.map((group) => (
                  <div key={group.key} className="flex flex-col gap-1.5">
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
                ))
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <CreateLabelDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        existingGroups={groups.map((group) => group.key)}
      />

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
