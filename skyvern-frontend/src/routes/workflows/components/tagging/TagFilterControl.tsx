import * as React from "react";
import {
  Cross2Icon,
  PlusIcon,
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
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { badgeVariants } from "@/components/ui/badge-variants";
import { cn } from "@/util/utils";
import { copyText } from "@/util/copyText";
import { toast } from "@/components/ui/use-toast";
import {
  MAX_AUTOCOMPLETE_SUGGESTIONS,
  parseTagFilterTerm,
  parseTypedTagQuery,
  termDedupeKey,
  type TagFilterTerm,
  type TagKey,
} from "../../types/tagTypes";
import {
  paletteDotClass,
  tagColorFor,
  type TagColorMap,
} from "../../types/tagColors";

type Props = {
  tagKeys: Array<TagKey>;
  value: ReadonlyArray<TagFilterTerm>;
  onChange: (terms: Array<TagFilterTerm>) => void;
  // Standalone label values observed on the page (for value-only suggestions).
  labelSuggestions?: Array<string>;
  // Grouped values observed per key (for exact suggestions after `group:`).
  valueSuggestionsByKey?: Map<string, Array<string>>;
  // Trigger button label; the count badge is appended regardless.
  triggerLabel?: string;
  // When provided, each group suggestion row renders a delete affordance wired
  // to this callback. Omit it for read-only surfaces (e.g. the analytics
  // dashboard filter) that must not expose destructive tag-key management.
  onDeleteKey?: (tagKey: TagKey) => void;
  // Restrict filtering to exact `group:value` terms only — no bare labels and
  // no group-any (`key:*`). Use on surfaces whose backend matches values
  // literally (the analytics summary endpoint), where those broader forms
  // would silently return unfiltered/empty data.
  exactValuesOnly?: boolean;
  // (key, value) -> palette color; only exact group:value chips are colored.
  // Omit on surfaces that don't load colors (chips stay neutral).
  colors?: TagColorMap;
};

// Group identity used for display + dedupe: exact terms sharing a key OR
// together (shown adjacent), everything else is its own AND conjunct.
function termGroupId(term: TagFilterTerm): string {
  if (term.key === null) {
    return `l:${term.value}`; // label
  }
  if (term.value === null) {
    return `k:${term.key}`; // group-only
  }
  return `e:${term.key}`; // exact (OR within key)
}

function sameTerm(a: TagFilterTerm, b: TagFilterTerm): boolean {
  return a.key === b.key && a.value === b.value;
}

// Read-only tag filter control: collects terms in three shapes — label
// (value-only), group (`key:*`), and exact (group:label). Backend ANDs across
// keys, ORs within a key. Destructive key management is opt-in via onDeleteKey.
function TagFilterControl({
  tagKeys,
  value,
  onChange,
  labelSuggestions = [],
  valueSuggestionsByKey,
  triggerLabel = "Tags",
  onDeleteKey,
  exactValuesOnly = false,
  colors,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");

  React.useEffect(() => {
    if (!open) {
      setQuery("");
    }
  }, [open]);

  function addTerm(term: TagFilterTerm) {
    if (value.some((existing) => sameTerm(existing, term))) {
      setQuery("");
      return;
    }
    onChange([...value, term]);
    setQuery("");
  }

  function removeTerm(target: TagFilterTerm) {
    onChange(value.filter((term) => !sameTerm(term, target)));
  }

  const knownKeys = React.useMemo(
    () => new Set(tagKeys.map((tagKey) => tagKey.key)),
    [tagKeys],
  );

  // Sort by group id then value so OR'd exact terms sit together and AND
  // separators land between distinct conjuncts.
  const sortedTerms = React.useMemo(
    () =>
      [...value].sort(
        (a, b) =>
          termGroupId(a).localeCompare(termGroupId(b)) ||
          (a.value ?? "").localeCompare(b.value ?? ""),
      ),
    [value],
  );

  function copyFilterUrl() {
    copyText(window.location.href).then((ok) => {
      toast(
        ok
          ? { title: "Filter link copied" }
          : { variant: "destructive", title: "Couldn't copy link" },
      );
    });
  }

  const trimmedQuery = query.trim();
  const normalizedQuery = trimmedQuery.toLowerCase();
  const candidate = parseTagFilterTerm(query);
  // In exact mode only fully-specified group:value terms are addable; bare
  // labels and group-any (key:null) are rejected because the backend can't
  // honor them.
  const candidateAddable =
    candidate !== null &&
    (!exactValuesOnly || (candidate.key !== null && candidate.value !== null));
  const candidateExists =
    candidate !== null && value.some((term) => sameTerm(term, candidate));
  const showAdd = candidateAddable && !candidateExists;

  const { typedKey, typedValuePartial } = parseTypedTagQuery(trimmedQuery);

  const groupSuggestions =
    typedKey === null
      ? tagKeys
          .filter((tk) => tk.key.toLowerCase().includes(normalizedQuery))
          .filter(
            (tk) =>
              !value.some((term) => term.key === tk.key && term.value === null),
          )
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const labelMatches =
    typedKey === null && !exactValuesOnly
      ? labelSuggestions
          .filter((label) => label.toLowerCase().includes(normalizedQuery))
          .filter(
            (label) =>
              !value.some((term) => term.key === null && term.value === label),
          )
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];
  const groupedValueMatches =
    typedKey !== null
      ? (valueSuggestionsByKey?.get(typedKey) ?? [])
          .filter((v) => v.toLowerCase().includes(typedValuePartial))
          .slice(0, MAX_AUTOCOMPLETE_SUGGESTIONS)
      : [];

  function termLabel(term: TagFilterTerm): React.ReactNode {
    if (term.key === null) {
      return term.value;
    }
    return (
      <>
        <span className="font-medium">{term.key}</span>
        <span className="text-muted-foreground">: </span>
        {term.value === null ? (
          <span className="italic text-muted-foreground">any</span>
        ) : (
          term.value
        )}
      </>
    );
  }

  function candidateLabel(term: TagFilterTerm): string {
    if (term.key === null) {
      return `label “${term.value}”`;
    }
    if (term.value === null) {
      return `group “${term.key}”`;
    }
    return `${term.key}: ${term.value}`;
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-2">
          <TokensIcon className="h-4 w-4" />
          {triggerLabel}
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
              {sortedTerms.map((term, index) => {
                const previous = index > 0 ? sortedTerms[index - 1] : undefined;
                const startsNewConjunct =
                  previous !== undefined &&
                  termGroupId(previous) !== termGroupId(term);
                const unknownKey =
                  term.key !== null && !knownKeys.has(term.key);
                // Only exact group:value terms carry a color, shown as a leading
                // dot; the chip surface stays neutral. A warning border (unknown
                // group) is independent of the color dot.
                const dotClass =
                  !unknownKey && term.value !== null
                    ? paletteDotClass(tagColorFor(colors, term.key, term.value))
                    : "";
                return (
                  <React.Fragment key={termDedupeKey(term)}>
                    {startsNewConjunct ? (
                      <span className="px-0.5 text-[10px] font-semibold uppercase text-muted-foreground">
                        and
                      </span>
                    ) : null}
                    <span
                      className={cn(
                        badgeVariants({ variant: "secondary" }),
                        "max-w-full gap-1 font-normal",
                        unknownKey ? "border-warning" : undefined,
                      )}
                      title={
                        unknownKey
                          ? `"${term.key}" isn't a registered group in this org.`
                          : undefined
                      }
                    >
                      {dotClass ? (
                        <span
                          aria-hidden="true"
                          className={cn(
                            "inline-block h-2 w-2 shrink-0 rounded-full",
                            dotClass,
                          )}
                        />
                      ) : null}
                      <span className="truncate">{termLabel(term)}</span>
                      <button
                        type="button"
                        aria-label={`Remove ${candidateLabel(term)}`}
                        className="ml-0.5 shrink-0 rounded-sm opacity-70 hover:opacity-100"
                        onClick={() => removeTerm(term)}
                      >
                        <Cross2Icon className="h-3 w-3" />
                      </button>
                    </span>
                  </React.Fragment>
                );
              })}
            </div>
          </div>
        ) : null}
        <Command shouldFilter={false}>
          <CommandInput
            placeholder={
              exactValuesOnly
                ? "Filter by group:value…"
                : "Filter by label or group:label…"
            }
            value={query}
            onValueChange={setQuery}
            onKeyDown={(event) => {
              if (event.key === "Enter" && candidate && showAdd) {
                event.preventDefault();
                addTerm(candidate);
              }
            }}
          />
          <CommandList>
            <CommandEmpty>
              {exactValuesOnly
                ? "Type group:value to filter."
                : "Type a label, group:*, or group:label."}
            </CommandEmpty>
            {showAdd && candidate ? (
              <CommandGroup>
                <CommandItem
                  value={`__add__,${trimmedQuery}`}
                  onSelect={() => addTerm(candidate)}
                >
                  <PlusIcon className="mr-2 h-4 w-4" />
                  Filter by {candidateLabel(candidate)}
                </CommandItem>
              </CommandGroup>
            ) : null}
            {groupSuggestions.length > 0 ? (
              <CommandGroup heading="Groups">
                {groupSuggestions.map((tagKey) => (
                  <CommandItem
                    key={tagKey.key}
                    value={`__group__,${tagKey.key}`}
                    className="justify-between gap-2"
                    // Default: filter by group (any value in it). In exact mode
                    // group-any isn't supported, so prefill `key:` and make the
                    // user pick a concrete value instead.
                    onSelect={() =>
                      exactValuesOnly
                        ? setQuery(`${tagKey.key}:`)
                        : addTerm({ key: tagKey.key, value: null })
                    }
                  >
                    <div className="flex min-w-0 flex-col">
                      <span className="truncate">
                        <span className="font-medium">{tagKey.key}</span>
                        <span className="text-muted-foreground">
                          {exactValuesOnly ? ": …" : ": any"}
                        </span>
                      </span>
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
                      {onDeleteKey ? (
                        <button
                          type="button"
                          aria-label={`Delete group ${tagKey.key}`}
                          className="rounded-sm p-1 text-muted-foreground hover:text-destructive"
                          onClick={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                            onDeleteKey(tagKey);
                          }}
                        >
                          <TrashIcon className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                    </div>
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {labelMatches.length > 0 ? (
              <CommandGroup heading="Labels">
                {labelMatches.map((label) => (
                  <CommandItem
                    key={`label:${label}`}
                    value={`__label__,${label}`}
                    onSelect={() => addTerm({ key: null, value: label })}
                  >
                    {label}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : null}
            {groupedValueMatches.length > 0 && typedKey !== null ? (
              <CommandGroup heading={`${typedKey} values`}>
                {groupedValueMatches.map((v) => (
                  <CommandItem
                    key={`gv:${v}`}
                    value={`__gv__,${v}`}
                    onSelect={() => addTerm({ key: typedKey, value: v })}
                  >
                    <span className="font-medium">{typedKey}</span>
                    <span className="text-muted-foreground">: </span>
                    {v}
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

export { TagFilterControl };
