import * as React from "react";
import { CheckIcon, PlusIcon } from "@radix-ui/react-icons";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  MAX_AUTOCOMPLETE_SUGGESTIONS,
  Tag,
  TagKey,
  isUserWritableTagKey,
  parseTagInput,
  parseTypedTagQuery,
  sortTags,
  tagElementKey,
  validateTag,
} from "../../types/tagTypes";

type Props = {
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  onApply: (tag: Tag) => void;
  error?: string | null;
  onErrorChange?: (error: string | null) => void;
  // While a bulk apply is in flight, freeze the picker so a second pick can't
  // start a competing apply for the same selection.
  disabled?: boolean;
  // Set by pickers that support removal (single rows and the run bulk union).
  // The agent bulk Actions surface omits these and stays add-only.
  currentTags?: Array<Tag>;
  onRemove?: (tag: Tag) => void;
};

// The cmdk body of the tag picker: suggestions plus an "Add label/group:label"
// affordance. Wrapper-agnostic so it can live in a Radix menu submenu (the row
// context menu and the bulk Actions menu) without duplicating the cmdk.
function TagPickerCommand({
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  onApply,
  error,
  onErrorChange,
  disabled,
  currentTags,
  onRemove,
}: Props) {
  const [query, setQuery] = React.useState("");
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    // Radix MenuSubContent moves focus to its container on open; re-focus the
    // input on the next frame so typing lands in the command, not the menu.
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, []);

  const sortedCurrentTags = React.useMemo(
    () =>
      onRemove
        ? sortTags(
            (currentTags ?? []).filter((tag) => isUserWritableTagKey(tag.key)),
          )
        : [],
    [currentTags, onRemove],
  );
  const currentTagByKey = React.useMemo(() => {
    const byKey = new Map<string, Tag>();
    for (const tag of sortedCurrentTags) {
      byKey.set(tagElementKey(tag), tag);
    }
    return byKey;
  }, [sortedCurrentTags]);

  function selectTag(tag: Tag) {
    if (disabled) {
      return;
    }
    const currentTag = currentTagByKey.get(tagElementKey(tag));
    if (currentTag) {
      onRemove?.(currentTag);
      return;
    }
    const validationError = validateTag(tag);
    if (validationError) {
      onErrorChange?.(validationError);
      return;
    }
    onApply(tag);
  }

  const trimmedQuery = query.trim();
  const normalizedQuery = trimmedQuery.toLowerCase();
  const candidate = parseTagInput(query);
  const candidateAddable =
    candidate !== null && isUserWritableTagKey(candidate.key);
  const candidateIsCurrent =
    candidate !== null &&
    candidateAddable &&
    currentTagByKey.has(tagElementKey(candidate));
  const { typedKey, typedValuePartial } = parseTypedTagQuery(trimmedQuery);

  const groupSuggestions =
    typedKey === null
      ? tagKeys
          .filter((tk) => isUserWritableTagKey(tk.key))
          .filter((tk) => tk.key.toLowerCase().includes(normalizedQuery))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const labelMatches =
    typedKey === null
      ? labelSuggestions
          .filter((value) => value.toLowerCase().includes(normalizedQuery))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const groupedValueMatches =
    typedKey !== null && isUserWritableTagKey(typedKey)
      ? (valueSuggestionsByKey?.get(typedKey) ?? [])
          .filter((value) => value.toLowerCase().includes(typedValuePartial))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const hasItems =
    sortedCurrentTags.length > 0 ||
    candidateAddable ||
    groupSuggestions.length > 0 ||
    labelMatches.length > 0 ||
    groupedValueMatches.length > 0;

  return (
    <Command
      shouldFilter={false}
      onKeyDown={(event) => {
        // Let Escape bubble so the popover/menu can close; keep every other key
        // from reaching the parent menu's typeahead so cmdk can handle it.
        if (event.key !== "Escape") {
          event.stopPropagation();
        }
      }}
    >
      <CommandInput
        ref={inputRef}
        placeholder="Label or group:label…"
        value={query}
        onValueChange={(value) => {
          setQuery(value);
          onErrorChange?.(null);
        }}
      />
      {error ? (
        <div className="border-b px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      ) : null}
      <CommandList>
        {sortedCurrentTags.length > 0 && (
          <CommandGroup heading="Current">
            {sortedCurrentTags.map((tag) => (
              <CommandItem
                key={tagElementKey(tag)}
                value={`current-${tagElementKey(tag)}`}
                disabled={disabled}
                onSelect={() => selectTag(tag)}
              >
                <span>
                  {tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value}
                </span>
                <CheckIcon className="ml-auto h-4 w-4 text-blue-700 dark:text-blue-400" />
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {candidateAddable && candidate !== null && (
          <CommandGroup>
            <CommandItem
              value={`${candidateIsCurrent ? "remove" : "add"}-${trimmedQuery}`}
              disabled={disabled}
              onSelect={() => selectTag(candidate)}
            >
              {candidateIsCurrent ? (
                <CheckIcon className="mr-2 h-4 w-4 text-blue-700 dark:text-blue-400" />
              ) : (
                <PlusIcon className="mr-2 h-4 w-4" />
              )}
              {candidateIsCurrent ? "Remove " : "Add "}
              {candidate.key !== null
                ? `${candidate.key}: ${candidate.value}`
                : candidate.value}
            </CommandItem>
          </CommandGroup>
        )}
        {typedKey !== null && groupedValueMatches.length > 0 && (
          <CommandGroup heading={`${typedKey}:`}>
            {groupedValueMatches.map((value) => (
              <CommandItem
                key={value}
                value={`${typedKey}:${value}`}
                disabled={disabled}
                onSelect={() => selectTag({ key: typedKey, value })}
              >
                {value}
                {currentTagByKey.has(
                  tagElementKey({ key: typedKey, value }),
                ) ? (
                  <CheckIcon className="ml-auto h-4 w-4 text-blue-700 dark:text-blue-400" />
                ) : null}
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {groupSuggestions.length > 0 && (
          <CommandGroup heading="Groups">
            {groupSuggestions.map((tk) => (
              <CommandItem
                key={tk.key}
                value={`group-${tk.key}`}
                disabled={disabled}
                onSelect={() => setQuery(`${tk.key}: `)}
              >
                {tk.key}:
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {labelMatches.length > 0 && (
          <CommandGroup heading="Labels">
            {labelMatches.map((value) => (
              <CommandItem
                key={value}
                value={`label-${value}`}
                disabled={disabled}
                onSelect={() => selectTag({ key: null, value })}
              >
                {value}
                {currentTagByKey.has(tagElementKey({ key: null, value })) ? (
                  <CheckIcon className="ml-auto h-4 w-4 text-blue-700 dark:text-blue-400" />
                ) : null}
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {!hasItems && (
          <CommandEmpty>Type a label or group:label to add.</CommandEmpty>
        )}
      </CommandList>
    </Command>
  );
}

export { TagPickerCommand };
