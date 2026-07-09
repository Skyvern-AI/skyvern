import * as React from "react";
import { ReloadIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
      <Dialog
        open={keyToDelete !== null}
        onOpenChange={(next) => {
          if (!next && !deleteKeyMutation.isPending) {
            setKeyToDelete(null);
          }
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete group “{keyToDelete?.key}”?</DialogTitle>
            <DialogDescription>
              This removes it from {keyToDelete?.workflow_count ?? 0} workflow
              {keyToDelete?.workflow_count === 1 ? "" : "s"} and from the group
              list. This can’t be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setKeyToDelete(null)}
              disabled={deleteKeyMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              className="gap-2"
              disabled={deleteKeyMutation.isPending}
              onClick={() => {
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
            >
              {deleteKeyMutation.isPending ? (
                <ReloadIcon className="h-4 w-4 animate-spin" />
              ) : null}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export { WorkflowTagFilter };
