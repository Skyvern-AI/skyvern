import * as React from "react";
import { CaretSortIcon, CheckIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
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
import { GeoTarget } from "@/api/types";
import { formatGeoTargetCompact } from "@/util/geoData";
import {
  GroupedSearchResults,
  searchGeoData,
  SearchResultItem,
} from "@/util/geoSearch";
import { useDebouncedCallback } from "use-debounce";

interface GeoTargetSelectorProps {
  value: GeoTarget | null;
  onChange: (value: GeoTarget) => void;
  className?: string;
  allowGranularSearch?: boolean;
  modalPopover?: boolean;
}

export function GeoTargetSelector({
  value,
  onChange,
  className,
  allowGranularSearch = true,
  modalPopover = false,
}: GeoTargetSelectorProps) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [results, setResults] = React.useState<GroupedSearchResults>({
    countries: [],
    subdivisions: [],
    cities: [],
  });
  const [loading, setLoading] = React.useState(false);
  // Monotonic id for the latest in-flight search. Stale completions whose
  // id no longer matches are ignored so a slow prior request cannot clobber
  // results from a newer one (e.g. reopen after a granular search).
  const searchIdRef = React.useRef(0);

  const runSearch = React.useCallback(
    async (searchQuery: string) => {
      const id = ++searchIdRef.current;
      setLoading(true);
      try {
        const data = await searchGeoData(searchQuery, {
          includeGranularResults: allowGranularSearch,
        });
        if (searchIdRef.current === id) {
          setResults(data);
        }
      } catch (error) {
        if (searchIdRef.current === id) {
          console.error("Failed to search geo data", error);
        }
      } finally {
        if (searchIdRef.current === id) {
          setLoading(false);
        }
      }
    },
    [allowGranularSearch],
  );

  const handleSearch = useDebouncedCallback(runSearch, 300);

  // Reset query, drop any stale granular results, and reload countries each
  // time the popover opens. The search runs without debounce so the list
  // matches the empty input immediately rather than after a 300ms window.
  React.useEffect(() => {
    if (open) {
      handleSearch.cancel();
      setQuery("");
      setResults({ countries: [], subdivisions: [], cities: [] });
      runSearch("");
    }
  }, [open, handleSearch, runSearch]);

  const onInput = (val: string) => {
    setQuery(val);
    handleSearch(val);
  };

  const handleSelect = (item: SearchResultItem) => {
    onChange(item.value);
    setOpen(false);
  };

  const isSelected = (itemValue: GeoTarget) => {
    if (!value) return false;
    return (
      value.country === itemValue.country &&
      value.subdivision === itemValue.subdivision &&
      value.city === itemValue.city &&
      Boolean(value.isISP) === Boolean(itemValue.isISP)
    );
  };

  return (
    <Popover open={open} onOpenChange={setOpen} modal={modalPopover}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className={cn("w-full justify-between", className)}
        >
          <span className="truncate">
            {value ? formatGeoTargetCompact(value) : "Select proxy location..."}
          </span>
          <CaretSortIcon className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="z-[100] w-[400px] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder={
              allowGranularSearch
                ? "Type a country, state, or city..."
                : "Search country..."
            }
            value={query}
            onValueChange={onInput}
          />
          <CommandList>
            {loading && (
              <div className="py-6 text-center text-sm text-muted-foreground">
                Loading...
              </div>
            )}
            {!loading &&
              results.countries.length === 0 &&
              results.subdivisions.length === 0 &&
              results.cities.length === 0 && (
                <CommandEmpty>No location found.</CommandEmpty>
              )}

            {!loading && allowGranularSearch && query.trim().length < 2 && (
              <div className="border-b px-3 py-2 text-xs text-muted-foreground">
                Type to search by state or city.
              </div>
            )}

            {!loading && (
              <>
                {results.countries.length > 0 && (
                  <CommandGroup heading="Countries">
                    {results.countries.map((item) => (
                      <CommandItem
                        key={`country-${item.value.country}${item.value.isISP ? "-isp" : ""}`}
                        value={JSON.stringify(item.value)}
                        onSelect={() => handleSelect(item)}
                      >
                        <span className="mr-2 text-lg">{item.icon}</span>
                        <span>{item.label}</span>
                        <CheckIcon
                          className={cn(
                            "ml-auto h-4 w-4",
                            isSelected(item.value)
                              ? "opacity-100"
                              : "opacity-0",
                          )}
                        />
                      </CommandItem>
                    ))}
                  </CommandGroup>
                )}

                {allowGranularSearch && results.subdivisions.length > 0 && (
                  <CommandGroup heading="States / Regions">
                    {results.subdivisions.map((item) => (
                      <CommandItem
                        key={`sub-${item.value.country}-${item.value.subdivision}`}
                        value={JSON.stringify(item.value)}
                        onSelect={() => handleSelect(item)}
                      >
                        <span className="mr-2 text-lg">{item.icon}</span>
                        <div className="flex flex-col">
                          <span>{item.label}</span>
                          <span className="text-xs text-muted-foreground">
                            {item.description}
                          </span>
                        </div>
                        <CheckIcon
                          className={cn(
                            "ml-auto h-4 w-4",
                            isSelected(item.value)
                              ? "opacity-100"
                              : "opacity-0",
                          )}
                        />
                      </CommandItem>
                    ))}
                  </CommandGroup>
                )}

                {allowGranularSearch && results.cities.length > 0 && (
                  <CommandGroup heading="Cities">
                    {results.cities.map((item) => (
                      <CommandItem
                        key={`city-${item.value.country}-${item.value.subdivision}-${item.value.city}`}
                        value={JSON.stringify(item.value)}
                        onSelect={() => handleSelect(item)}
                      >
                        <span className="mr-2 text-lg">{item.icon}</span>
                        <div className="flex flex-col">
                          <span>{item.label}</span>
                          <span className="text-xs text-muted-foreground">
                            {item.description}
                          </span>
                        </div>
                        <CheckIcon
                          className={cn(
                            "ml-auto h-4 w-4",
                            isSelected(item.value)
                              ? "opacity-100"
                              : "opacity-0",
                          )}
                        />
                      </CommandItem>
                    ))}
                  </CommandGroup>
                )}
              </>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
