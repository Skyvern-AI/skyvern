import { ChevronDownIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { useDebounce } from "use-debounce";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import { useBrowserProfileQuery } from "@/routes/browserProfiles/hooks/useBrowserProfileQuery";
import { cn, handleInfiniteScroll } from "@/util/utils";

import { useInfiniteBrowserProfilesQuery } from "../hooks/useInfiniteBrowserProfilesQuery";

const PAGE_SIZE = 20;

type Props = {
  value: string | null;
  onChange: (value: string | null) => void;
  placeholder?: string;
  compact?: boolean;
};

function BrowserProfileSelector({
  value,
  onChange,
  placeholder,
  compact = false,
}: Props) {
  const triggerHeightClass = compact ? "h-9" : "h-10";
  const triggerInnerPaddingClass = compact ? "px-3 py-1" : "px-3 py-2";
  const [open, setOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearch] = useDebounce(searchQuery, 300);
  const listRef = useRef<HTMLDivElement>(null);
  const isTyping = searchQuery !== debouncedSearch;

  // Keep wheel events inside the list instead of letting React Flow zoom the
  // canvas underneath the popover.
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const handler = (e: WheelEvent) => e.stopPropagation();
    el.addEventListener("wheel", handler, { passive: true });
    return () => el.removeEventListener("wheel", handler);
  }, [open]);

  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isFetching,
    isError,
  } = useInfiniteBrowserProfilesQuery({
    page_size: PAGE_SIZE,
    searchKey: debouncedSearch,
    enabled: open,
    managed: false,
  });

  const profiles = useMemo(
    () => data?.pages.flatMap((page) => page) ?? [],
    [data],
  );

  const hasValue = Boolean(value && value !== "");

  const profileInList = useMemo(
    () =>
      hasValue
        ? profiles.find((profile) => profile.browser_profile_id === value)
        : undefined,
    [hasValue, profiles, value],
  );

  // Fall back to a single-profile fetch when the saved value isn't in the
  // current paginated results (e.g., the user opens an existing workflow with
  // a profile selected that lives on a later page).
  const { data: selectedProfile } = useBrowserProfileQuery(
    hasValue && !profileInList ? (value ?? undefined) : undefined,
  );

  const selectedDisplay = profileInList ?? selectedProfile;

  // Reset the previous search whenever the dropdown is reopened so the user
  // always starts from the unfiltered list.
  useEffect(() => {
    if (!open) {
      setSearchQuery("");
    }
  }, [open]);

  const handleSelect = (profileId: string | null) => {
    onChange(profileId);
    setOpen(false);
  };

  const triggerLabel = hasValue
    ? (selectedDisplay?.name ?? value ?? "")
    : (placeholder ?? "Select a browser profile");

  if (isError) {
    return (
      <div
        className={cn(
          "flex w-full cursor-not-allowed items-center rounded-md border border-input bg-transparent text-sm opacity-50 shadow-sm",
          triggerHeightClass,
        )}
      >
        <div
          className={cn(
            "min-w-0 flex-1 truncate text-left",
            triggerInnerPaddingClass,
          )}
        >
          <span className="text-muted-foreground">
            Failed to load browser profiles
          </span>
        </div>
        <div className="flex items-center pr-2">
          <ChevronDownIcon className="size-4 text-slate-500" />
        </div>
      </div>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <div
          role="button"
          tabIndex={0}
          className={cn(
            "nopan flex w-full cursor-pointer items-center rounded-md border border-input bg-transparent text-sm shadow-sm",
            triggerHeightClass,
          )}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              setOpen((prev) => !prev);
            }
          }}
        >
          <div
            className={cn(
              "min-w-0 flex-1 truncate text-left",
              triggerInnerPaddingClass,
            )}
          >
            {hasValue ? (
              <span className="text-slate-200">{triggerLabel}</span>
            ) : (
              <span className="text-muted-foreground">{triggerLabel}</span>
            )}
          </div>
          <div className="flex items-center pr-2">
            <ChevronDownIcon
              className={`size-4 text-slate-500 transition-transform ${open ? "rotate-180" : ""}`}
            />
          </div>
        </div>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={4}
        className="nopan w-[var(--radix-popover-trigger-width)] overflow-hidden rounded-md border border-slate-600 bg-slate-800 p-0 shadow-lg"
      >
        <div className="border-b border-slate-600 px-3 py-2">
          <input
            autoFocus
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search browser profiles..."
            className="w-full bg-transparent text-sm text-slate-200 placeholder:text-muted-foreground focus-visible:outline-none"
          />
        </div>
        <div
          ref={listRef}
          className="max-h-[300px] overflow-y-auto"
          onScroll={(e) =>
            handleInfiniteScroll(
              e,
              fetchNextPage,
              hasNextPage,
              isFetchingNextPage,
            )
          }
        >
          <button
            type="button"
            onClick={() => handleSelect(null)}
            className={`flex w-full items-center px-3 py-2 text-left text-sm transition-colors hover:bg-slate-700 ${
              !hasValue ? "bg-slate-700" : ""
            }`}
          >
            <span className="text-slate-200">None</span>
          </button>
          {(isFetching || isTyping) && profiles.length === 0 ? (
            <>
              {Array.from({ length: 5 }).map((_, index) => (
                <div
                  key={`skeleton-${index}`}
                  className="flex w-full flex-col gap-1 px-3 py-2"
                >
                  <Skeleton className="h-3.5 w-3/4" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
              ))}
            </>
          ) : profiles.length === 0 ? (
            <div className="px-3 py-3 text-xs text-slate-500">
              {debouncedSearch
                ? `No browser profiles match "${debouncedSearch}".`
                : "No browser profiles found."}
            </div>
          ) : (
            <>
              {profiles.map((profile) => {
                const isSelected = value === profile.browser_profile_id;
                return (
                  <button
                    key={profile.browser_profile_id}
                    type="button"
                    onClick={() => handleSelect(profile.browser_profile_id)}
                    className={`flex w-full flex-col gap-0.5 px-3 py-2 text-left text-sm transition-colors hover:bg-slate-700 ${
                      isSelected ? "bg-slate-700" : ""
                    }`}
                  >
                    <span className="font-medium text-slate-200">
                      {profile.name}
                    </span>
                    {profile.description && (
                      <span className="truncate text-xs text-slate-400">
                        {profile.description}
                      </span>
                    )}
                  </button>
                );
              })}
              {isFetchingNextPage && (
                <div className="flex items-center justify-center py-2">
                  <ReloadIcon className="h-3 w-3 animate-spin text-slate-400" />
                </div>
              )}
            </>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export { BrowserProfileSelector };
