import * as React from "react";
import {
  Cross2Icon,
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
import { useDeleteTagKeyMutation } from "../../hooks/useWorkflowTagMutations";

type Props = {
  tagKeys: Array<TagKey>;
  value: Array<TagFilterTerm>;
  onChange: (terms: Array<TagFilterTerm>) => void;
  // Standalone label values observed on the page (for value-only suggestions).
  labelSuggestions?: Array<string>;
  // Grouped values observed per key (for exact suggestions after `group:`).
  valueSuggestionsByKey?: Map<string, Array<string>>;
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

// Tag filter pill for the workflows list. Collects terms in three shapes — label
// (value-only), group (`key:*`), and exact (group:label). Backend ANDs, ORs per key.
function WorkflowTagFilter({
  tagKeys,
  value,
  onChange,
  labelSuggestions = [],
  valueSuggestionsByKey,
}: Props) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [keyToDelete, setKeyToDelete] = React.useState<TagKey | null>(null);
  const deleteKeyMutation = useDeleteTagKeyMutation();

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
  const candidateExists =
    candidate !== null && value.some((term) => sameTerm(term, candidate));
  const showAdd = candidate !== null && !candidateExists;

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
    typedKey === null
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
                {sortedTerms.map((term, index) => {
                  const previous =
                    index > 0 ? sortedTerms[index - 1] : undefined;
                  const startsNewConjunct =
                    previous !== undefined &&
                    termGroupId(previous) !== termGroupId(term);
                  const unknownKey =
                    term.key !== null && !knownKeys.has(term.key);
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
              placeholder="Filter by label or group:label…"
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
                Type a label, group:*, or group:label.
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
                      // Filter by group (any value in it). Type `group:value`
                      // for an exact match instead.
                      onSelect={() => addTerm({ key: tagKey.key, value: null })}
                    >
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate">
                          <span className="font-medium">{tagKey.key}</span>
                          <span className="text-muted-foreground">: any</span>
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
                        <button
                          type="button"
                          aria-label={`Delete group ${tagKey.key}`}
                          className="rounded-sm p-1 text-muted-foreground hover:text-destructive"
                          onClick={(event) => {
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
