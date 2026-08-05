import * as React from "react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { type TagFilterTerm, type TagKey } from "../../types/tagTypes";
import { type TagColorMap } from "../../types/tagColors";
import { useDeleteTagKeyMutation } from "../../hooks/useWorkflowTagMutations";
import { TagFilterControl } from "./TagFilterControl";

type Props = {
  tagKeys: Array<TagKey>;
  value: Array<TagFilterTerm>;
  onChange: (terms: Array<TagFilterTerm>) => void;
  // Standalone label values observed on the page (for value-only suggestions).
  labelSuggestions?: Array<string>;
  // Grouped values observed per key (for exact suggestions after `group:`).
  valueSuggestionsByKey?: Map<string, Array<string>>;
  // (key, value) -> palette color; forwarded to the filter control to color
  // exact group:value chips.
  colors?: TagColorMap;
};

// Tag filter pill for the workflows list. Wraps the shared TagFilterControl and
// layers on destructive tag-key management (delete a group org-wide).
function WorkflowTagFilter({ value, onChange, ...controlProps }: Props) {
  const [keyToDelete, setKeyToDelete] = React.useState<TagKey | null>(null);
  const deleteKeyMutation = useDeleteTagKeyMutation();

  return (
    <>
      <TagFilterControl
        {...controlProps}
        value={value}
        onChange={onChange}
        onDeleteKey={setKeyToDelete}
      />
      <ConfirmDialog
        open={keyToDelete !== null}
        onOpenChange={(next) => {
          if (!next) {
            setKeyToDelete(null);
          }
        }}
        title={`Delete group "${keyToDelete?.key ?? ""}"?`}
        description={
          <p>
            This removes it from {keyToDelete?.workflow_count ?? 0} workflow
            {keyToDelete?.workflow_count === 1 ? "" : "s"} and from the group
            list.
          </p>
        }
        contentClassName="sm:max-w-md"
        isPending={deleteKeyMutation.isPending}
        onConfirm={() => {
          if (!keyToDelete) {
            return;
          }
          const deletedKey = keyToDelete.key;
          deleteKeyMutation.mutate(deletedKey, {
            onSuccess: () => {
              // Drop any active filter term on the now-deleted group, else
              // the list refetches with a stale ?tags= and shows empty.
              onChange(value.filter((term) => term.key !== deletedKey));
              setKeyToDelete(null);
            },
          });
        }}
      />
    </>
  );
}

export { WorkflowTagFilter };
