import { TokensIcon } from "@radix-ui/react-icons";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { TagChipList } from "@/routes/workflows/components/tagging/TagChipList";
import { useTagKeysQuery } from "@/routes/workflows/hooks/useTagKeysQuery";
import { useTagValuesQuery } from "@/routes/workflows/hooks/useTagValuesQuery";
import {
  isUserWritableTagKey,
  type Tag,
} from "@/routes/workflows/types/tagTypes";
import { WORKFLOW_TAGGING_FLAG } from "@/util/featureFlags";
import { cn } from "@/util/utils";
import { useApplyRunTagsMutation } from "../../hooks/useRunTagMutations";
import { useRunTagSuggestionsQuery } from "../../hooks/useRunTagSuggestionsQuery";
import { useRunTagsQuery } from "../../hooks/useRunTagsQuery";
import { RunTagPickerCommand } from "./RunTagPickerCommand";

type Props = {
  workflowRunId: string | null | undefined;
  className?: string;
};

function RunTagsEditor({ workflowRunId, className }: Props) {
  const taggingEnabled = useFeatureFlag(WORKFLOW_TAGGING_FLAG) !== false;
  const { data: runTags = [] } = useRunTagsQuery(workflowRunId, {
    enabled: taggingEnabled,
  });
  const { data: tagKeys = [] } = useTagKeysQuery({ enabled: taggingEnabled });
  const { data: tagColors } = useTagValuesQuery({ enabled: taggingEnabled });
  const { data: suggestions } = useRunTagSuggestionsQuery({
    enabled: taggingEnabled,
  });
  const applyRunTagsMutation = useApplyRunTagsMutation();
  const tagDescriptions = useMemo(
    () =>
      new Map(
        tagKeys.map((tagKey): [string, string | null] => [
          tagKey.key,
          tagKey.description,
        ]),
      ),
    [tagKeys],
  );
  const suggestionKeys = useMemo(
    () =>
      (suggestions?.keys ?? []).map((key) => ({
        key,
        description: null,
        workflow_count: 0,
      })),
    [suggestions?.keys],
  );

  if (!taggingEnabled || !workflowRunId) {
    return null;
  }

  function removeTag(tag: Tag) {
    if (!workflowRunId || !isUserWritableTagKey(tag.key)) {
      return;
    }
    applyRunTagsMutation.mutate({
      workflowRunId,
      data: {
        tags_to_delete: [
          tag.key !== null ? { key: tag.key } : { value: tag.value },
        ],
      },
    });
  }

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <TagChipList
        tags={runTags}
        descriptions={tagDescriptions}
        colors={tagColors}
        maxVisible={20}
        onRemove={removeTag}
      />
      <Popover>
        <PopoverTrigger asChild>
          <Button size="sm" variant="secondary" aria-label="Manage tags">
            <TokensIcon className="mr-2 h-4 w-4" />
            Tags
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-72 p-0" align="start">
          <RunTagPickerCommand
            workflowRunId={workflowRunId}
            tagKeys={suggestionKeys}
            labelSuggestions={suggestions?.labels ?? []}
            valueSuggestionsByKey={suggestions?.valuesByKey}
            currentTags={runTags}
          />
        </PopoverContent>
      </Popover>
    </div>
  );
}

export { RunTagsEditor };
