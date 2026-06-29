import * as React from "react";
import { PlusIcon } from "@radix-ui/react-icons";
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
  parseTagInput,
  parseTypedTagQuery,
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
}: Props) {
  const [query, setQuery] = React.useState("");
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    // Radix MenuSubContent moves focus to its container on open; re-focus the
    // input on the next frame so typing lands in the command, not the menu.
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, []);

  function applyTag(tag: Tag) {
    if (disabled) {
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
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const groupedValueMatches =
    typedKey !== null
      ? (valueSuggestionsByKey?.get(typedKey) ?? [])
          .filter((value) => value.toLowerCase().includes(typedValuePartial))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const hasItems =
    candidate !== null ||
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
        {candidate !== null && (
          <CommandGroup>
            <CommandItem
              value={`add-${trimmedQuery}`}
              disabled={disabled}
              onSelect={() => applyTag(candidate)}
            >
              <PlusIcon className="mr-2 h-4 w-4" />
              Add{" "}
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
                onSelect={() => applyTag({ key: typedKey, value })}
              >
                {value}
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
                onSelect={() => applyTag({ key: null, value })}
              >
                {value}
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
