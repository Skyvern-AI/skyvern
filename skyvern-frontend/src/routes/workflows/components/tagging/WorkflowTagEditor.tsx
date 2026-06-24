import * as React from "react";
import { PlusIcon, ReloadIcon, TokensIcon } from "@radix-ui/react-icons";
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
  MAX_AUTOCOMPLETE_SUGGESTIONS,
  MAX_TAGS_PER_WORKFLOW,
  parseTagInput,
  parseTypedTagQuery,
  serializeTagFilterTerm,
  sortTags,
  tagElementKey,
  validateTag,
  type Tag,
  type TagDeleteInput,
  type TagKey,
} from "../../types/tagTypes";
import { useApplyWorkflowTagsMutation } from "../../hooks/useWorkflowTagMutations";
import {
  randomPaletteColor,
  tagColorFor,
  type PaletteColorName,
  type TagColorMap,
} from "../../types/tagColors";
import { TagChip } from "./TagChip";
import { TagColorSwatchPicker } from "./TagColorSwatchPicker";

type Props = {
  workflowPermanentId: string;
  tags: Array<Tag>;
  // Registered groups (keys) for autocomplete; standalone labels aren't here.
  tagKeys: Array<TagKey>;
  // Standalone label values observed on the page, suggested when typing a label.
  labelSuggestions?: Array<string>;
  // Grouped values observed per key, suggested after typing `group:`.
  valueSuggestionsByKey?: Map<string, Array<string>>;
  // (key, value) -> palette color, to color existing chips and preselect the
  // swatch when re-adding an already-colored grouped tag.
  colorMap?: TagColorMap;
};

// Inline tag editor: type a bare `label` for a standalone tag or `group:label`
// for a grouped one. Re-adding overwrites that group's label (backend set-wins).
function WorkflowTagEditor({
  workflowPermanentId,
  tags,
  tagKeys,
  labelSuggestions = [],
  valueSuggestionsByKey,
  colorMap,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  // Default a new grouped tag to a random swatch (re-seeded after each add); the
  // user can override, or it syncs to the existing color when re-adding one.
  const [selectedColor, setSelectedColor] =
    React.useState<PaletteColorName>(randomPaletteColor);

  const applyMutation = useApplyWorkflowTagsMutation();
  const isPending = applyMutation.isPending;

  React.useEffect(() => {
    if (!open) {
      setQuery("");
      setError(null);
    }
  }, [open]);

  const sortedTags = React.useMemo(() => sortTags(tags), [tags]);

  function addTag(tag: Tag, color?: PaletteColorName) {
    // A write is in flight; ignore further adds so a quick double Enter/click
    // can't queue racing POSTs whose arrival order would decide the final tag.
    if (isPending) {
      return;
    }
    const validationError = validateTag(tag);
    if (validationError) {
      setError(validationError);
      return;
    }
    const exactExists = tags.some(
      (existing) => existing.key === tag.key && existing.value === tag.value,
    );
    if (exactExists) {
      setQuery("");
      setError(null);
      return;
    }
    // A grouped tag with an existing key overwrites that group's label (no new
    // identity); anything else is a new tag and counts against the cap.
    const previousInGroup =
      tag.key !== null
        ? tags.find((existing) => existing.key === tag.key)
        : undefined;
    const isOverwrite = previousInGroup !== undefined;
    if (!isOverwrite && tags.length >= MAX_TAGS_PER_WORKFLOW) {
      setError(`A workflow can have at most ${MAX_TAGS_PER_WORKFLOW} tags.`);
      return;
    }
    applyMutation.mutate(
      {
        workflowPermanentId,
        data: {
          tags: [{ key: tag.key, value: tag.value }],
          // Color is key-scoped and only applies to grouped tags.
          ...(tag.key !== null && color
            ? { colors: { [tag.key]: color } }
            : {}),
        },
      },
      {
        onSuccess: () => {
          setQuery("");
          setError(null);
          setSelectedColor(randomPaletteColor());
          if (isOverwrite && previousInGroup) {
            toast({
              title: "Tag overwritten",
              description: `“${tag.key}” changed from “${previousInGroup.value}” to “${tag.value}”.`,
            });
          }
        },
      },
    );
  }

  function removeTag(tag: Tag) {
    if (isPending) {
      return;
    }
    // Grouped tags delete by key, standalone labels by value.
    const target: TagDeleteInput =
      tag.key !== null ? { key: tag.key } : { value: tag.value };
    applyMutation.mutate({
      workflowPermanentId,
      data: { tags_to_delete: [target] },
    });
  }

  const trimmedQuery = query.trim();
  const normalizedQuery = trimmedQuery.toLowerCase();
  const candidate = parseTagInput(query);
  const candidateExists =
    candidate !== null &&
    tags.some((t) => t.key === candidate.key && t.value === candidate.value);
  const showAdd = candidate !== null && !candidateExists;
  const isGroupedCandidate = candidate !== null && candidate.key !== null;

  // Re-adding an already-colored (key, value) should keep its color, so sync the
  // swatch to it; brand-new values keep the random/user-picked selection.
  const candidateExistingColor =
    candidate !== null
      ? tagColorFor(colorMap, candidate.key, candidate.value)
      : undefined;
  React.useEffect(() => {
    if (candidateExistingColor) {
      setSelectedColor(candidateExistingColor);
    }
  }, [candidateExistingColor]);

  // When the user has typed `group:partial`, suggest existing values for that
  // group; otherwise suggest groups (to start a grouped tag) and labels.
  const { typedKey, typedValuePartial } = parseTypedTagQuery(trimmedQuery);

  const groupSuggestions =
    typedKey === null
      ? tagKeys
          .filter((tk) => tk.key.toLowerCase().includes(normalizedQuery))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const labelMatches =
    typedKey === null
      ? labelSuggestions
          .filter((value) => value.toLowerCase().includes(normalizedQuery))
          .filter(
            (value) => !tags.some((t) => t.key === null && t.value === value),
          )
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const groupedValueMatches =
    typedKey !== null
      ? (valueSuggestionsByKey?.get(typedKey) ?? [])
          .filter((value) => value.toLowerCase().includes(typedValuePartial))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];

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
          {sortedTags.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {sortedTags.map((tag) => (
                <TagChip
                  key={tagElementKey(tag)}
                  tagKey={tag.key}
                  value={tag.value}
                  color={tagColorFor(colorMap, tag.key, tag.value)}
                  onRemove={() => removeTag(tag)}
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
          <CommandInput
            placeholder="Add a tag — label or group:label…"
            value={query}
            onValueChange={(value) => {
              setQuery(value);
              setError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && candidate && showAdd) {
                event.preventDefault();
                addTag(
                  candidate,
                  isGroupedCandidate ? selectedColor : undefined,
                );
              }
            }}
          />
          {showAdd && isGroupedCandidate ? (
            <div className="border-b px-3 py-2">
              <div className="mb-1.5 text-xs text-muted-foreground">Color</div>
              <TagColorSwatchPicker
                value={selectedColor}
                onChange={setSelectedColor}
              />
            </div>
          ) : null}
          <CommandList>
            <CommandEmpty>Type a label or group:label.</CommandEmpty>
            {showAdd && candidate ? (
              <CommandGroup>
                <CommandItem
                  value={`__add__,${trimmedQuery}`}
                  onSelect={() =>
                    addTag(
                      candidate,
                      isGroupedCandidate ? selectedColor : undefined,
                    )
                  }
                >
                  {isPending ? (
                    <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <PlusIcon className="mr-2 h-4 w-4" />
                  )}
                  Add “
                  {candidate.key !== null
                    ? `${candidate.key}: ${candidate.value}`
                    : candidate.value}
                  ”
                </CommandItem>
              </CommandGroup>
            ) : null}
            {groupSuggestions.length > 0 ? (
              <CommandGroup heading="Groups">
                {groupSuggestions.map((tk) => (
                  <CommandItem
                    key={tk.key}
                    value={`__group__,${tk.key}`}
                    // Selecting a group seeds `group:` so the next keystrokes
                    // type the label.
                    onSelect={() => setQuery(`${tk.key}:`)}
                  >
                    <span className="font-medium">{tk.key}</span>
                    <span className="text-muted-foreground">:</span>
                    {tk.description ? (
                      <span className="ml-2 truncate text-xs text-muted-foreground">
                        {tk.description}
                      </span>
                    ) : null}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {labelMatches.length > 0 ? (
              <CommandGroup heading="Labels">
                {labelMatches.map((value) => (
                  <CommandItem
                    key={`label:${value}`}
                    value={`__label__,${value}`}
                    onSelect={() => addTag({ key: null, value })}
                  >
                    {value}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {groupedValueMatches.length > 0 && typedKey !== null ? (
              <CommandGroup heading={`Existing ${typedKey} values`}>
                {groupedValueMatches.map((value) => (
                  <CommandItem
                    key={`gv:${value}`}
                    value={`__gv__,${value}`}
                    onSelect={() => addTag({ key: typedKey, value })}
                  >
                    {serializeTagFilterTerm({ key: typedKey, value })}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export { WorkflowTagEditor };
