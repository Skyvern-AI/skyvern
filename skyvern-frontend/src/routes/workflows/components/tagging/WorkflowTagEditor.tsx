import * as React from "react";
import {
  ArrowLeftIcon,
  PlusIcon,
  ReloadIcon,
  TokensIcon,
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
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import {
  MAX_TAGS_PER_WORKFLOW,
  validateTagKey,
  validateTagValue,
  type TagKey,
} from "../../types/tagTypes";
import {
  useApplyWorkflowTagsMutation,
  useDeleteWorkflowTagMutation,
} from "../../hooks/useWorkflowTagMutations";
import { TagChip } from "./TagChip";

type Props = {
  workflowPermanentId: string;
  tags: Record<string, string>;
  tagKeys: Array<TagKey>;
  // key -> existing values observed on the page, so the value step can suggest
  // existing values for the chosen key (the backend has no list-values
  // endpoint). Free-text entry still works. A Map keeps lookups safe against
  // tag keys like "constructor".
  valueSuggestions?: Map<string, Array<string>>;
};

// Inline tag editor for a workflow row: add / overwrite / remove tags. Uses a
// cmdk Command (key step -> value step) rather than native <datalist>, which is
// unreliable inside a Radix popover; this mirrors WorkflowTagFilter so key and
// value autosuggest behave the same. Adding a key that already exists overwrites
// its value (backend set-wins) = the edit path.
function WorkflowTagEditor({
  workflowPermanentId,
  tags,
  tagKeys,
  valueSuggestions,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const [selectedKey, setSelectedKey] = React.useState<string | null>(null);
  const [query, setQuery] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const applyMutation = useApplyWorkflowTagsMutation();
  const deleteMutation = useDeleteWorkflowTagMutation();
  const isPending = applyMutation.isPending || deleteMutation.isPending;

  const entries = Object.entries(tags).sort(([a], [b]) => a.localeCompare(b));

  function resetForm() {
    setSelectedKey(null);
    setQuery("");
    setError(null);
  }

  React.useEffect(() => {
    // Inline the resets (rather than calling resetForm) so the effect's only
    // dependency is `open`; setState identities are stable.
    if (!open) {
      setSelectedKey(null);
      setQuery("");
      setError(null);
    }
  }, [open]);

  function applyTag(key: string, value: string) {
    // A write is in flight; ignore further selects so quick double Enter/click
    // can't queue racing POSTs whose arrival order would decide the final tag
    // (backend is set-wins).
    if (isPending) {
      return;
    }
    const trimmedKey = key.trim();
    const trimmedValue = value.trim();

    const keyError = validateTagKey(trimmedKey);
    if (keyError) {
      setError(keyError);
      return;
    }
    const valueError = validateTagValue(trimmedValue);
    if (valueError) {
      setError(valueError);
      return;
    }
    // Own-key check: `key in tags` would treat inherited names like
    // "constructor"/"toString" (valid backend tag keys) as already present.
    const hasKey = Object.prototype.hasOwnProperty.call(tags, trimmedKey);
    const previousValue = hasKey ? tags[trimmedKey] : undefined;
    if (!hasKey && Object.keys(tags).length >= MAX_TAGS_PER_WORKFLOW) {
      setError(`A workflow can have at most ${MAX_TAGS_PER_WORKFLOW} tags.`);
      return;
    }
    // Same key, different value: the backend overwrites (set-wins); proceed but
    // tell the user it replaced the existing tag.
    const isOverwrite =
      previousValue !== undefined && previousValue !== trimmedValue;

    applyMutation.mutate(
      { workflowPermanentId, data: { tags: { [trimmedKey]: trimmedValue } } },
      {
        onSuccess: () => {
          resetForm();
          if (isOverwrite) {
            toast({
              title: "Tag overwritten",
              description: `“${trimmedKey}” changed from “${previousValue}” to “${trimmedValue}”.`,
            });
          }
        },
      },
    );
  }

  function handleRemove(key: string) {
    if (isPending) {
      return;
    }
    deleteMutation.mutate({ workflowPermanentId, key });
  }

  const normalizedQuery = query.trim().toLowerCase();
  const trimmedQuery = query.trim();

  // Rank an exact (case-insensitive) match first so cmdk highlights it by
  // default — plain Enter then commits the exact typed text, while arrow+Enter
  // still selects any other highlighted suggestion (sort is stable otherwise).
  const exactFirst = (a: string, b: string) =>
    (a.toLowerCase() === normalizedQuery ? 0 : 1) -
    (b.toLowerCase() === normalizedQuery ? 0 : 1);

  const filteredKeys = tagKeys
    .filter((tagKey) => tagKey.key.toLowerCase().includes(normalizedQuery))
    .sort((a, b) => exactFirst(a.key, b.key));
  const showUseKey =
    trimmedQuery.length > 0 && !tagKeys.some((tk) => tk.key === trimmedQuery);

  const suggestionsForKey = selectedKey
    ? (valueSuggestions?.get(selectedKey) ?? [])
    : [];
  const filteredValues = suggestionsForKey
    .filter((value) => value.toLowerCase().includes(normalizedQuery))
    .sort(exactFirst);
  const showAddValue =
    trimmedQuery.length > 0 && !suggestionsForKey.includes(trimmedQuery);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                aria-label="Edit tags"
                className="text-muted-foreground hover:text-foreground"
              >
                <TokensIcon className="h-4 w-4" />
              </Button>
            </PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent>Edit Tags</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <PopoverContent className="w-80 p-0" align="end">
        <div className="space-y-2 p-3">
          <div className="text-sm font-medium">Tags</div>
          {entries.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {entries.map(([key, value]) => (
                <TagChip
                  key={key}
                  tagKey={key}
                  value={value}
                  onRemove={() => handleRemove(key)}
                />
              ))}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">No tags yet.</div>
          )}
          {error ? (
            <div className="text-xs text-destructive">{error}</div>
          ) : null}
        </div>
        <Command shouldFilter={false}>
          {selectedKey === null ? (
            <>
              <CommandInput
                placeholder="Tag key…"
                value={query}
                onValueChange={(value) => {
                  setQuery(value);
                  setError(null);
                }}
              />
              <CommandList>
                <CommandEmpty>Type a key to add.</CommandEmpty>
                {showUseKey ? (
                  <CommandGroup>
                    <CommandItem
                      value={`__use__,${trimmedQuery}`}
                      onSelect={() => {
                        setSelectedKey(trimmedQuery);
                        setQuery("");
                      }}
                    >
                      <PlusIcon className="mr-2 h-4 w-4" />
                      Use “{trimmedQuery}”
                    </CommandItem>
                  </CommandGroup>
                ) : null}
                {filteredKeys.length > 0 ? (
                  <CommandGroup heading="Existing keys">
                    {filteredKeys.map((tagKey) => (
                      <CommandItem
                        key={tagKey.key}
                        value={tagKey.key}
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
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ) : null}
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
                placeholder="Value…"
                value={query}
                onValueChange={(value) => {
                  setQuery(value);
                  setError(null);
                }}
              />
              <CommandList>
                <CommandEmpty>Type a value to add.</CommandEmpty>
                {showAddValue ? (
                  <CommandGroup>
                    {/* cmdk item values must be unique. The comma can't appear
                        in a valid tag value, so this sentinel can never collide
                        with a real suggestion's value. */}
                    <CommandItem
                      value={`__add__,${trimmedQuery}`}
                      onSelect={() => applyTag(selectedKey, trimmedQuery)}
                    >
                      {applyMutation.isPending ? (
                        <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                      ) : (
                        <PlusIcon className="mr-2 h-4 w-4" />
                      )}
                      Add “{selectedKey}: {trimmedQuery}”
                    </CommandItem>
                  </CommandGroup>
                ) : null}
                {filteredValues.length > 0 ? (
                  <CommandGroup heading="Existing values">
                    {filteredValues.map((value) => (
                      <CommandItem
                        key={value}
                        value={value}
                        onSelect={() => applyTag(selectedKey, value)}
                      >
                        {value}
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ) : null}
              </CommandList>
            </>
          )}
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export { WorkflowTagEditor };
