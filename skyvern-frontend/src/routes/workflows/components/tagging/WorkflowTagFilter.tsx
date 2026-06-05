import * as React from "react";
import {
  ArrowLeftIcon,
  CheckIcon,
  PlusIcon,
  ReloadIcon,
  TokensIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/util/utils";
import { copyText } from "@/util/copyText";
import { toast } from "@/components/ui/use-toast";
import type { TagFilterPair, TagKey } from "../../types/tagTypes";
import { useDeleteTagKeyMutation } from "../../hooks/useWorkflowTagMutations";
import { TagChip } from "./TagChip";

type Props = {
  tagKeys: Array<TagKey>;
  value: Array<TagFilterPair>;
  onChange: (pairs: Array<TagFilterPair>) => void;
  // key -> distinct values observed on the current page; used to suggest values
  // since the backend has no "list values for key" endpoint. Free-text entry
  // always works regardless. A Map (not a plain object) so a tag key like
  // "constructor" can't resolve to an inherited Object prototype member.
  valueSuggestions?: Map<string, Array<string>>;
};

// Tag filter pill for the workflows-list page. Collects `key:value` pairs; the
// backend (GET /workflows?tags=) ANDs across distinct keys and ORs within a
// key, so adding several values for one key is the OR-within-key path.
function WorkflowTagFilter({
  tagKeys,
  value,
  onChange,
  valueSuggestions,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
  const [query, setQuery] = React.useState("");
  const [keyToDelete, setKeyToDelete] = React.useState<TagKey | null>(null);
  const deleteKeyMutation = useDeleteTagKeyMutation();

  React.useEffect(() => {
    if (!open) {
      setSelectedKey(null);
      setQuery("");
    }
  }, [open]);

  function addPair(key: string, rawValue: string) {
    const trimmedKey = key.trim();
    const trimmedValue = rawValue.trim();
    // Comma is the pair separator in the serialized `tags` param, so a value
    // containing one cannot round-trip (it would split into a different
    // filter). Backend filter values can't contain commas, so reject here.
    if (!trimmedKey || !trimmedValue || trimmedValue.includes(",")) {
      return;
    }
    const exists = value.some(
      (pair) => pair.key === trimmedKey && pair.value === trimmedValue,
    );
    if (exists) {
      return;
    }
    onChange([...value, { key: trimmedKey, value: trimmedValue }]);
  }

  function removePair(target: TagFilterPair) {
    onChange(
      value.filter(
        (pair) => !(pair.key === target.key && pair.value === target.value),
      ),
    );
  }

  // Registered keys, to flag active filters whose key isn't in the org's
  // registry (e.g. a typo'd / hand-edited URL).
  const knownKeys = React.useMemo(
    () => new Set(tagKeys.map((tagKey) => tagKey.key)),
    [tagKeys],
  );

  // Stable key-then-value order so same-key pairs sit together and the "AND"
  // separators land between distinct keys.
  const sortedValue = React.useMemo(
    () =>
      [...value].sort(
        (a, b) => a.key.localeCompare(b.key) || a.value.localeCompare(b.value),
      ),
    [value],
  );

  function copyFilterUrl() {
    // copyText handles the secure-context navigator.clipboard path plus an
    // execCommand fallback, and resolves false on failure (no silent no-op).
    copyText(window.location.href).then((ok) => {
      toast(
        ok
          ? { title: "Filter link copied" }
          : { variant: "destructive", title: "Couldn't copy link" },
      );
    });
  }

  const normalizedQuery = query.trim().toLowerCase();

  const filteredKeys = tagKeys.filter((tagKey) =>
    tagKey.key.toLowerCase().includes(normalizedQuery),
  );

  const suggestionsForKey = selectedKey
    ? (valueSuggestions?.get(selectedKey) ?? [])
    : [];
  // Drop unselectable comma-containing values (they can't be applied as a
  // filter) so the suggestion list never dangles a no-op item.
  const filteredValues = suggestionsForKey.filter(
    (suggestion) =>
      !suggestion.includes(",") &&
      suggestion.toLowerCase().includes(normalizedQuery),
  );
  const trimmedQuery = query.trim();
  const showAddOption =
    trimmedQuery.length > 0 &&
    !trimmedQuery.includes(",") &&
    !suggestionsForKey.includes(trimmedQuery);

  return (
    <>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button variant="outline" size="sm" className="gap-2">
            <TokensIcon className="h-4 w-4" />
            Tags
            {value.length > 0 ? (
              <span className="flex h-5 min-w-5 items-center justify-center rounded bg-primary px-1 text-xs text-primary-foreground">
                {value.length}
              </span>
            ) : null}
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-72 p-0" align="start">
          {value.length > 0 ? (
            <div className="flex flex-col gap-2 border-b p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  Active filters
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="text-xs text-blue-500 hover:underline"
                    onClick={copyFilterUrl}
                  >
                    Copy link
                  </button>
                  <button
                    type="button"
                    className="text-xs text-blue-500 hover:underline"
                    onClick={() => onChange([])}
                  >
                    Clear all
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1">
                {sortedValue.map((pair, index) => {
                  const previous =
                    index > 0 ? sortedValue[index - 1] : undefined;
                  // Different key from the previous chip → AND; same key →
                  // adjacent (OR within a key).
                  const startsNewKey =
                    previous !== undefined && previous.key !== pair.key;
                  const unknownKey = !knownKeys.has(pair.key);
                  return (
                    <React.Fragment key={`${pair.key}:${pair.value}`}>
                      {startsNewKey ? (
                        <span className="px-0.5 text-[10px] font-semibold uppercase text-muted-foreground">
                          and
                        </span>
                      ) : null}
                      <TagChip
                        tagKey={pair.key}
                        value={pair.value}
                        description={
                          unknownKey
                            ? `"${pair.key}" isn't a registered tag key in this org.`
                            : undefined
                        }
                        className={unknownKey ? "border-warning" : undefined}
                        onRemove={() => removePair(pair)}
                      />
                    </React.Fragment>
                  );
                })}
              </div>
            </div>
          ) : null}
          <Command shouldFilter={false}>
            {selectedKey === null ? (
              <>
                <CommandInput
                  placeholder="Filter by tag key…"
                  value={query}
                  onValueChange={setQuery}
                />
                <CommandList>
                  <CommandEmpty>No tag keys found.</CommandEmpty>
                  <CommandGroup>
                    {filteredKeys.map((tagKey) => (
                      <CommandItem
                        key={tagKey.key}
                        value={tagKey.key}
                        className="justify-between gap-2"
                        onSelect={() => {
                          setSelectedKey(tagKey.key);
                          setQuery("");
                        }}
                      >
                        <div className="flex min-w-0 flex-col">
                          <span className="truncate">{tagKey.key}</span>
                          {tagKey.description ? (
                            <span className="truncate text-xs text-muted-foreground">
                              {tagKey.description}
                            </span>
                          ) : null}
                        </div>
                        <div className="flex shrink-0 items-center gap-1.5">
                          {tagKey.workflow_count > 0 ? (
                            <span className="text-xs text-muted-foreground">
                              {tagKey.workflow_count}
                            </span>
                          ) : null}
                          <button
                            type="button"
                            aria-label={`Delete tag key ${tagKey.key}`}
                            className="rounded-sm p-1 text-muted-foreground hover:text-destructive"
                            onClick={(event) => {
                              // Stop cmdk from treating this as selecting the key.
                              event.preventDefault();
                              event.stopPropagation();
                              setKeyToDelete(tagKey);
                            }}
                          >
                            <TrashIcon className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </CommandList>
              </>
            ) : (
              <>
                <div className="flex items-center gap-1 border-b px-2 py-1.5">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 shrink-0"
                    onClick={() => {
                      setSelectedKey(null);
                      setQuery("");
                    }}
                  >
                    <ArrowLeftIcon className="h-4 w-4" />
                  </Button>
                  <span className="truncate text-sm font-medium">
                    {selectedKey}
                  </span>
                </div>
                <CommandInput
                  placeholder="Add a value…"
                  value={query}
                  onValueChange={setQuery}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && trimmedQuery.length > 0) {
                      event.preventDefault();
                      addPair(selectedKey, trimmedQuery);
                      setQuery("");
                    }
                  }}
                />
                <CommandList>
                  {filteredValues.length === 0 && !showAddOption ? (
                    <CommandEmpty>Type a value to filter.</CommandEmpty>
                  ) : null}
                  {showAddOption ? (
                    <CommandGroup>
                      <CommandItem
                        value={`__add__:${trimmedQuery}`}
                        onSelect={() => {
                          addPair(selectedKey, trimmedQuery);
                          setQuery("");
                        }}
                      >
                        <PlusIcon className="mr-2 h-4 w-4" />
                        Add “{trimmedQuery}”
                      </CommandItem>
                    </CommandGroup>
                  ) : null}
                  {filteredValues.length > 0 ? (
                    <CommandGroup heading="Suggestions">
                      {filteredValues.map((suggestion) => {
                        const checked = value.some(
                          (pair) =>
                            pair.key === selectedKey &&
                            pair.value === suggestion,
                        );
                        return (
                          <CommandItem
                            key={suggestion}
                            value={suggestion}
                            onSelect={() => {
                              if (checked) {
                                removePair({
                                  key: selectedKey,
                                  value: suggestion,
                                });
                              } else {
                                addPair(selectedKey, suggestion);
                              }
                            }}
                          >
                            <CheckIcon
                              className={cn(
                                "mr-2 h-4 w-4",
                                checked ? "opacity-100" : "opacity-0",
                              )}
                            />
                            {suggestion}
                          </CommandItem>
                        );
                      })}
                    </CommandGroup>
                  ) : null}
                </CommandList>
              </>
            )}
          </Command>
        </PopoverContent>
      </Popover>
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
            <DialogTitle>Delete tag “{keyToDelete?.key}”?</DialogTitle>
            <DialogDescription>
              This removes it from {keyToDelete?.workflow_count ?? 0} workflow
              {keyToDelete?.workflow_count === 1 ? "" : "s"} and from the tag
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
                    // Drop any active filter on the now-deleted key, else the
                    // list refetches with a stale ?tags= and shows empty.
                    onChange(value.filter((pair) => pair.key !== deletedKey));
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
