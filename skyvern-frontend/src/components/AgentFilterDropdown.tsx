import { useMemo, useState } from "react";
import { ChevronDownIcon } from "@radix-ui/react-icons";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useDebounce } from "use-debounce";

import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import {
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { handleInfiniteScroll } from "@/util/utils";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import {
  Command,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "./ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { Skeleton } from "./ui/skeleton";

const PAGE_SIZE = 10;
// Must not exceed the GET /runs workflow_permanent_id max_length cap (50) or requests 422.
const MAX_SELECTED_AGENTS = 50;

type Props = {
  values: Array<string>;
  onChange: (values: Array<string>) => void;
};

function AgentFilterDropdown({ values, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 300);
  const isTyping = search !== debouncedSearch;
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const activeOrgQueryKeyScope = getActiveOrgQueryKeyScope(activeOrgId);

  const {
    data,
    fetchNextPage,
    hasNextPage,
    isError,
    isFetching,
    isFetchingNextPage,
    refetch,
  } = useInfiniteQuery<Array<WorkflowApiResponse>>({
    queryKey: getOrgScopedQueryKey(
      ["workflows", "agent-filter", debouncedSearch],
      activeOrgQueryKeyScope,
    ),
    queryFn: async ({ pageParam = 1 }) => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(pageParam));
      params.append("page_size", String(PAGE_SIZE));
      params.append("only_workflows", "true");
      if (debouncedSearch) {
        params.append("search_key", debouncedSearch);
      }
      return client
        .get("/workflows", { params })
        .then((response) => response.data);
    },
    getNextPageParam: (lastPage, allPages) => {
      if (lastPage.length === PAGE_SIZE) {
        return allPages.length + 1;
      }
      return undefined;
    },
    initialPageParam: 1,
    enabled: open,
  });

  const workflows = useMemo(
    () => data?.pages.flatMap((page) => page) ?? [],
    [data],
  );
  const missingSelectedValues = useMemo(() => {
    const fetchedWorkflowIds = new Set(
      workflows.map((workflow) => workflow.workflow_permanent_id),
    );
    return values.filter((value) => !fetchedWorkflowIds.has(value));
  }, [values, workflows]);
  const isLoadingResults = isTyping || (isFetching && !isFetchingNextPage);

  function toggleWorkflow(workflowPermanentId: string) {
    if (values.includes(workflowPermanentId)) {
      onChange(values.filter((value) => value !== workflowPermanentId));
    } else if (values.length < MAX_SELECTED_AGENTS) {
      onChange([...values, workflowPermanentId]);
    }
  }

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) {
          setSearch("");
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="outline">
          Filter by Agent <ChevronDownIcon className="ml-2" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-0" align="end">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Search agents..."
            value={search}
            onValueChange={setSearch}
          />
          {values.length > 0 ? (
            <div className="flex justify-end border-b px-2 py-1">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => onChange([])}
              >
                Clear all
              </Button>
            </div>
          ) : null}
          <CommandList
            onScroll={(event) =>
              handleInfiniteScroll(
                event,
                fetchNextPage,
                hasNextPage,
                isFetchingNextPage,
              )
            }
          >
            {isLoadingResults ? (
              <div className="space-y-1 p-2">
                {Array.from({ length: 4 }).map((_, index) => (
                  <div
                    key={`skeleton-${index}`}
                    className="flex flex-col gap-1 px-2 py-1.5"
                  >
                    <Skeleton className="h-4 w-3/4" />
                    <Skeleton className="h-3 w-1/2" />
                  </div>
                ))}
              </div>
            ) : (
              <>
                {missingSelectedValues.length > 0 ? (
                  <CommandGroup heading="Selected">
                    {missingSelectedValues.map((workflowPermanentId) => (
                      <CommandItem
                        key={workflowPermanentId}
                        value={workflowPermanentId}
                        className="gap-2"
                        onSelect={() => toggleWorkflow(workflowPermanentId)}
                      >
                        <Checkbox
                          checked
                          tabIndex={-1}
                          className="pointer-events-none"
                        />
                        <span className="truncate font-mono text-xs text-muted-foreground">
                          {workflowPermanentId}
                        </span>
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ) : null}
                {isError ? (
                  <div className="flex flex-col items-center gap-2 px-3 py-4 text-sm">
                    <span>Failed to load agents.</span>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => void refetch()}
                    >
                      Try again
                    </Button>
                  </div>
                ) : workflows.length === 0 ? (
                  <div className="py-6 text-center text-sm">
                    No agents found.
                  </div>
                ) : (
                  <CommandGroup>
                    {workflows.map((workflow) => {
                      const workflowPermanentId =
                        workflow.workflow_permanent_id;
                      const isSelected = values.includes(workflowPermanentId);
                      const isDisabled =
                        !isSelected && values.length >= MAX_SELECTED_AGENTS;
                      return (
                        <CommandItem
                          key={workflowPermanentId}
                          value={workflowPermanentId}
                          className="gap-2"
                          disabled={isDisabled}
                          onSelect={() => toggleWorkflow(workflowPermanentId)}
                        >
                          <Checkbox
                            checked={isSelected}
                            tabIndex={-1}
                            className="pointer-events-none"
                          />
                          <div className="flex min-w-0 flex-col">
                            <span className="truncate font-medium">
                              {workflow.title}
                            </span>
                            <span className="truncate text-xs text-muted-foreground">
                              {workflowPermanentId}
                            </span>
                          </div>
                        </CommandItem>
                      );
                    })}
                    {isFetchingNextPage ? (
                      <div className="flex flex-col gap-1 px-3 py-2">
                        <Skeleton className="h-4 w-3/4" />
                        <Skeleton className="h-3 w-1/2" />
                      </div>
                    ) : null}
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

export { AgentFilterDropdown };
