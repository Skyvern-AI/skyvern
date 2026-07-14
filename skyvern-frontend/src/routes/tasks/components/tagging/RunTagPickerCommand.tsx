import { toast } from "@/components/ui/use-toast";
import { TagPickerCommand } from "@/routes/workflows/components/tagging/TagPickerCommand";
import type { Tag, TagKey } from "@/routes/workflows/types/tagTypes";
import { useApplyRunTagsMutation } from "../../hooks/useRunTagMutations";
import * as React from "react";

type Props = {
  workflowRunId: string;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  currentTags?: Array<Tag>;
};

function RunTagPickerCommand({
  workflowRunId,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  currentTags,
}: Props) {
  const applyRunTagsMutation = useApplyRunTagsMutation();
  const [tagError, setTagError] = React.useState<string | null>(null);

  function applyTag(tag: Tag) {
    applyRunTagsMutation.mutate(
      {
        workflowRunId,
        data: { tags: [tag] },
      },
      {
        onSuccess: () => {
          const tagLabel =
            tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value;
          toast({ title: `Tagged run with ${tagLabel}.`, variant: "success" });
        },
      },
    );
  }

  function removeTag(tag: Tag) {
    applyRunTagsMutation.mutate(
      {
        workflowRunId,
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

  return (
    <TagPickerCommand
      tagKeys={tagKeys}
      labelSuggestions={labelSuggestions}
      valueSuggestionsByKey={valueSuggestionsByKey}
      currentTags={currentTags}
      onApply={applyTag}
      onRemove={removeTag}
      error={tagError}
      onErrorChange={setTagError}
      disabled={applyRunTagsMutation.isPending}
    />
  );
}

export { RunTagPickerCommand };
