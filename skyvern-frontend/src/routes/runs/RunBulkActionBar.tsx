import { ChevronDownIcon, TokensIcon } from "@radix-ui/react-icons";
import { useMemo, useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { SelectionBar } from "@/components/SelectionBar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuPortal,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { invalidateRunTagQueries } from "@/routes/tasks/hooks/useRunTagMutations";
import { TagPickerCommand } from "@/routes/workflows/components/tagging/TagPickerCommand";
import {
  isUserWritableTagKey,
  sortTags,
  tagElementKey,
  type Tag,
  type TagKey,
} from "@/routes/workflows/types/tagTypes";
import { bulkResultToast } from "@/util/bulkResultToast";
import {
  BULK_CONCURRENCY_LIMIT,
  runWithConcurrency,
} from "@/util/runWithConcurrency";
import { useQueryClient } from "@tanstack/react-query";

type Props = {
  selectedRunIds: Array<string>;
  runTagsMap: Record<string, Array<Tag>>;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  onClearSelection: () => void;
};

function RunBulkActionBar({
  selectedRunIds,
  runTagsMap,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  onClearSelection,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [isOperating, setIsOperating] = useState(false);
  const [tagError, setTagError] = useState<string | null>(null);
  const currentTags = useMemo(() => {
    const unique = new Map<string, Tag>();
    for (const runId of selectedRunIds) {
      for (const tag of runTagsMap[runId] ?? []) {
        if (isUserWritableTagKey(tag.key)) {
          unique.set(tagElementKey(tag), tag);
        }
      }
    }
    return sortTags([...unique.values()]);
  }, [runTagsMap, selectedRunIds]);

  async function updateTags(tag: Tag, remove: boolean) {
    if (isOperating || !isUserWritableTagKey(tag.key)) {
      return;
    }
    const tagKey = tagElementKey(tag);
    const targetIds = remove
      ? selectedRunIds.filter((runId) =>
          (runTagsMap[runId] ?? []).some(
            (current) => tagElementKey(current) === tagKey,
          ),
        )
      : selectedRunIds;
    if (targetIds.length === 0) {
      return;
    }

    setIsOperating(true);
    try {
      const client = await getClient(credentialGetter);
      const data = remove
        ? {
            tags_to_delete: [
              tag.key !== null ? { key: tag.key } : { value: tag.value },
            ],
          }
        : { tags: [tag] };
      const results = await runWithConcurrency(
        targetIds.map(
          (runId) => () => client.post(`/runs/${runId}/tags`, data),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter(
        (result) => result.status === "fulfilled",
      ).length;
      const label = tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value;
      bulkResultToast({
        succeeded,
        total: targetIds.length,
        results,
        successTitle: (count) =>
          remove
            ? `Removed ${label} from ${count} run${count === 1 ? "" : "s"}.`
            : `Tagged ${count} run${count === 1 ? "" : "s"} with ${label}.`,
        failureTitle: (count) =>
          `Failed to ${remove ? "remove tags from" : "tag"} ${count} run${count === 1 ? "" : "s"}.`,
        partialTitle: (successCount, failedCount) =>
          `${remove ? "Updated" : "Tagged"} ${successCount} run${successCount === 1 ? "" : "s"}. ${failedCount} failed.`,
      });
      if (succeeded > 0) {
        invalidateRunTagQueries(queryClient);
      }
    } finally {
      setIsOperating(false);
    }
  }

  return (
    <SelectionBar
      count={selectedRunIds.length}
      isOperating={isOperating}
      onClear={onClearSelection}
    >
      <DropdownMenu
        onOpenChange={(open) => {
          if (!open) {
            setTagError(null);
          }
        }}
      >
        <DropdownMenuTrigger asChild>
          <Button size="sm" variant="ghost" disabled={isOperating}>
            Actions
            <ChevronDownIcon className="ml-1.5 h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" side="top" className="w-56">
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <TokensIcon className="mr-2 h-4 w-4" />
              Tags
            </DropdownMenuSubTrigger>
            <DropdownMenuPortal>
              <DropdownMenuSubContent className="w-72 p-0">
                <TagPickerCommand
                  tagKeys={tagKeys}
                  labelSuggestions={labelSuggestions}
                  valueSuggestionsByKey={valueSuggestionsByKey}
                  currentTags={currentTags}
                  onApply={(tag) => void updateTags(tag, false)}
                  onRemove={(tag) => void updateTags(tag, true)}
                  error={tagError}
                  onErrorChange={setTagError}
                  disabled={isOperating}
                />
              </DropdownMenuSubContent>
            </DropdownMenuPortal>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>
    </SelectionBar>
  );
}

export { RunBulkActionBar };
