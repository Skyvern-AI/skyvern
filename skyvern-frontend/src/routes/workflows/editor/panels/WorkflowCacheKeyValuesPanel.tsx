import {
  Cross2Icon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CacheKeyValuesResponse } from "@/routes/workflows/types/scriptTypes";
import { cn } from "@/util/utils";

interface Props {
  cacheKeyValues: CacheKeyValuesResponse | undefined;
  filter?: string;
  pending: boolean;
  scriptKey: string;
  onClose?: () => void;
  onDelete: (cacheKeyValue: string) => void;
  onFilterChange?: (filter: string) => void;
  onMouseDownCapture?: () => void;
  onPaginate: (page: number) => void;
  onSelect: (cacheKeyValue: string) => void;
}

function WorkflowCacheKeyValuesPanel({
  cacheKeyValues,
  filter,
  pending,
  scriptKey,
  onClose,
  onDelete,
  onFilterChange,
  onMouseDownCapture,
  onPaginate,
  onSelect,
}: Props) {
  const [draftFilter, setDraftFilter] = useState(filter ?? "");
  useEffect(() => {
    setDraftFilter(filter ?? "");
  }, [filter]);
  const values = cacheKeyValues?.values ?? [];
  const page = cacheKeyValues?.page ?? 0;
  const pageSize = cacheKeyValues?.page_size ?? 0;
  const filteredCount = cacheKeyValues?.filtered_count ?? 0;
  const totalCount = cacheKeyValues?.total_count ?? 0;
  const totalPages = Math.ceil(filteredCount / pageSize);
  const displayPage = totalPages === 0 ? 0 : page;

  return (
    <div
      className="relative z-10 w-[44.26rem] rounded-xl border border-border bg-background p-5 shadow-xl"
      onMouseDownCapture={() => onMouseDownCapture?.()}
    >
      <div className="space-y-4">
        <header className="flex items-start justify-between gap-3">
          <div className="flex-1">
            <h1 className="text-lg">Code Cache</h1>
            <span className="text-sm text-muted-foreground">
              Given your code key,{" "}
              <code className="font-mono text-xs text-foreground">
                {scriptKey}
              </code>
              , search for saved code using a code key value. For this code key
              there {totalCount === 1 ? "is" : "are"}{" "}
              <span className="font-bold text-foreground">{totalCount}</span>{" "}
              code key {totalCount === 1 ? "value" : "values"}
              {filteredCount !== totalCount && (
                <>
                  {" "}
                  (
                  <span className="font-bold text-foreground">
                    {filteredCount}
                  </span>{" "}
                  filtered)
                </>
              )}
              .
            </span>
          </div>
          {onClose && (
            <Button
              size="icon"
              variant="ghost"
              aria-label="Close"
              className="size-7 shrink-0"
              onClick={onClose}
            >
              <Cross2Icon className="size-4" />
            </Button>
          )}
        </header>
        {onFilterChange && (
          <Input
            placeholder="Search or type a new cache key value, press Enter"
            value={draftFilter}
            onChange={(e) => {
              setDraftFilter(e.target.value);
              onFilterChange(e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.key !== "Enter") {
                return;
              }
              const typed = draftFilter.trim();
              if (!typed) {
                return;
              }
              const exact = cacheKeyValues?.values?.find((v) => v === typed);
              const onlyMatch =
                cacheKeyValues?.values?.length === 1
                  ? cacheKeyValues.values[0]
                  : undefined;
              onSelect(exact ?? onlyMatch ?? typed);
            }}
          />
        )}
        <div className="h-[10rem] w-full overflow-hidden overflow-y-auto border-b border-border p-1">
          {values.length ? (
            <div className="grid w-full grid-cols-[3rem_1fr_3rem] text-sm">
              {values.map((cacheKeyValue, i) => (
                <div
                  key={cacheKeyValue}
                  className={cn(
                    "col-span-3 grid w-full cursor-pointer grid-cols-subgrid rounded-md hover:bg-slate-elevation5",
                    {
                      "bg-slate-elevation1 hover:bg-slate-elevation5":
                        i % 2 === 0,
                    },
                  )}
                  onClick={() => {
                    onSelect(cacheKeyValue);
                  }}
                >
                  <div
                    className={cn(
                      "flex items-center justify-end p-1 text-muted-foreground",
                    )}
                  >
                    {i + 1 + (page - 1) * pageSize}
                  </div>
                  <div
                    className={cn(
                      "flex min-w-0 flex-1 items-center justify-start p-1 text-muted-foreground",
                    )}
                    title={cacheKeyValue}
                  >
                    <div className="overflow-hidden text-ellipsis whitespace-nowrap">
                      {cacheKeyValue}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="ml-auto"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      onDelete(cacheKeyValue);
                    }}
                  >
                    <CrossCircledIcon />
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex h-full w-full items-center justify-center">
              No cached scripts found
            </div>
          )}
        </div>
        <div className="flex items-center justify-between p-1 text-muted-foreground">
          {pending && <ReloadIcon className="size-6 animate-spin" />}
          <Pagination className="justify-end pt-2">
            <PaginationContent>
              <PaginationItem>
                <PaginationPrevious
                  className={cn({
                    "pointer-events-none opacity-50": displayPage <= 1,
                  })}
                  onClick={() => {
                    if (page <= 1) {
                      return;
                    }
                    onPaginate(page - 1);
                  }}
                />
              </PaginationItem>
              <PaginationItem>
                <div className="text-sm font-bold">
                  {displayPage} of {isNaN(totalPages) ? 0 : totalPages}
                </div>
              </PaginationItem>
              <PaginationItem>
                <PaginationNext
                  className={cn({
                    "pointer-events-none opacity-50":
                      displayPage === totalPages,
                  })}
                  onClick={() => {
                    onPaginate(page + 1);
                  }}
                />
              </PaginationItem>
            </PaginationContent>
          </Pagination>
        </div>
      </div>
    </div>
  );
}

export { WorkflowCacheKeyValuesPanel };
