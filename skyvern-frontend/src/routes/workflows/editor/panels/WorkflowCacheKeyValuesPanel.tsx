import { CrossCircledIcon, ReloadIcon } from "@radix-ui/react-icons";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Button } from "@/components/ui/button";
import { CacheKeyValuesResponse } from "@/routes/workflows/types/scriptTypes";
import { cn } from "@/util/utils";

interface Props {
  cacheKeyValues: CacheKeyValuesResponse | undefined;
  pending: boolean;
  scriptKey: string;
  onDelete: (cacheKeyValue: string) => void;
  onMouseDownCapture?: () => void;
  onPaginate: (page: number) => void;
  onSelect: (cacheKeyValue: string) => void;
}

function WorkflowCacheKeyValuesPanel({
  cacheKeyValues,
  pending,
  scriptKey,
  onDelete,
  onMouseDownCapture,
  onPaginate,
  onSelect,
}: Props) {
  const values = cacheKeyValues?.values ?? [];
  const page = cacheKeyValues?.page ?? 0;
  const pageSize = cacheKeyValues?.page_size ?? 0;
  const filteredCount = cacheKeyValues?.filtered_count ?? 0;
  const totalCount = cacheKeyValues?.total_count ?? 0;
  const totalPages = Math.ceil(filteredCount / pageSize);
  const displayPage = totalPages === 0 ? 0 : page;

  return (
    <div
      className="relative z-10 w-[44.26rem] rounded-xl border border-slate-700 bg-slate-950 p-5 shadow-xl"
      onMouseDownCapture={() => onMouseDownCapture?.()}
    >
      <div className="space-y-4">
        <header>
          <h1 className="text-lg">Code Cache</h1>
          <span className="text-sm text-slate-400">
            Given your code key,{" "}
            <code className="font-mono text-xs text-slate-200">
              {scriptKey}
            </code>
            , search for saved code using a code key value. For this code key
            there {totalCount === 1 ? "is" : "are"}{" "}
            <span className="font-bold text-slate-200">{totalCount}</span> code
            key {totalCount === 1 ? "value" : "values"}
            {filteredCount !== totalCount && (
              <>
                {" "}
                (
                <span className="font-bold text-slate-200">
                  {filteredCount}
                </span>{" "}
                filtered)
              </>
            )}
            .
          </span>
        </header>
        <div className="h-[10rem] w-full overflow-hidden overflow-y-auto border-b border-slate-700 p-1">
          {values.length ? (
            <div className="grid w-full grid-cols-[3rem_1fr_3rem] text-sm">
              {values.map((cacheKeyValue, i) => (
                <div
                  key={cacheKeyValue}
                  className={cn(
                    "col-span-3 grid w-full cursor-pointer grid-cols-subgrid rounded-md hover:bg-slate-800",
                    {
                      "bg-slate-900 hover:bg-slate-800": i % 2 === 0,
                    },
                  )}
                  onClick={() => {
                    onSelect(cacheKeyValue);
                  }}
                >
                  <div
                    className={cn(
                      "flex items-center justify-end p-1 text-slate-500",
                    )}
                  >
                    {i + 1 + (page - 1) * pageSize}
                  </div>
                  <div
                    className={cn(
                      "flex min-w-0 flex-1 items-center justify-start p-1 text-slate-300",
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
        <div className="flex items-center justify-between p-1 text-slate-500">
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
