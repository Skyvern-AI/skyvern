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
}

export function GeoTargetSelector({
  value,
  onChange,
  className,
}: GeoTargetSelectorProps) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [results, setResults] = React.useState<GroupedSearchResults>({
    countries: [],
    subdivisions: [],
    cities: [],
  });
  const [loading, setLoading] = React.useState(false);

  const handleSearch = useDebouncedCallback(async (searchQuery: string) => {
    setLoading(true);
    try {
      const data = await searchGeoData(searchQuery);
      setResults(data);
    } catch (error) {
      console.error("Failed to search geo data", error);
    } finally {
      setLoading(false);
    }
  }, 300);

  // Initial load of countries
  React.useEffect(() => {
    if (open) {
      handleSearch("");
    }
  }, [open, handleSearch]);

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
    <Popover open={open} onOpenChange={setOpen}>
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
      <PopoverContent className="w-[400px] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Search country, state, or city..."
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

            {!loading && (
              <>
                {results.countries.length > 0 && (
                  <CommandGroup heading="Countries">
                    {results.countries.map((item) => (
                      <CommandItem
                        key={`country-${item.value.country}`}
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

                {results.subdivisions.length > 0 && (
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

                {results.cities.length > 0 && (
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
