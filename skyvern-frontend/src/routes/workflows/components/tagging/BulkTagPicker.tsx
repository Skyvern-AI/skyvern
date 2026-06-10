import * as React from "react";
import { PlusIcon, TokensIcon } from "@radix-ui/react-icons";
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
  MAX_AUTOCOMPLETE_SUGGESTIONS,
  Tag,
  TagKey,
  parseTagInput,
  parseTypedTagQuery,
  validateTag,
} from "../../types/tagTypes";

type Props = {
  bulkCount: number;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
  disabled?: boolean;
  onApplyTag: (tag: Tag) => Promise<void>;
};

function BulkTagPicker({
  bulkCount,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
  disabled = false,
  onApplyTag,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) {
      setQuery("");
      setError(null);
    }
  }, [open]);

  function applyTag(tag: Tag) {
    const validationError = validateTag(tag);
    if (validationError) {
      setError(validationError);
      return;
    }
    setOpen(false);
    void onApplyTag(tag);
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
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button size="sm" variant="ghost" disabled={disabled}>
          <TokensIcon className="mr-1.5 h-4 w-4" />
          Tags
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 p-0" align="start">
        <div className="border-b p-3">
          <h4 className="text-sm font-medium">
            Add tag to {bulkCount} agent{bulkCount === 1 ? "" : "s"}
          </h4>
          {error ? (
            <div className="mt-1 text-xs text-destructive">{error}</div>
          ) : null}
        </div>
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Label or group:label…"
            value={query}
            onValueChange={(value) => {
              setQuery(value);
              setError(null);
            }}
          />
          <CommandList>
            {candidate !== null && (
              <CommandGroup>
                <CommandItem
                  value={`add-${trimmedQuery}`}
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
      </PopoverContent>
    </Popover>
  );
}

export { BulkTagPicker };
