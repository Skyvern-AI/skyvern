import { LightningBoltIcon } from "@radix-ui/react-icons";

import { Tip } from "@/components/Tip";
import { Status } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { StatusFilterDropdown } from "@/components/StatusFilterDropdown";
import { Button } from "@/components/ui/button";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  MagnifyingGlassIcon,
  MixerHorizontalIcon,
  Pencil2Icon,
  PlayIcon,
} from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowRunsQuery } from "./hooks/useWorkflowRunsQuery";
import { WorkflowActions } from "./WorkflowActions";
import { Input } from "@/components/ui/input";
import { useDebounce } from "use-debounce";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { RunParametersDialog } from "./workflowRun/RunParametersDialog";

function WorkflowPage() {
  const { workflowPermanentId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);
  const navigate = useNavigate();

  const PAGE_SIZE = 10;
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const [openRunParams, setOpenRunParams] = useState<string | null>(null);

  const { data: workflowRuns, isLoading } = useWorkflowRunsQuery({
    workflowPermanentId,
    statusFilters,
    page,
    search: debouncedSearch,
    refetchOnMount: "always",
  });

  useEffect(() => {
    if (!isLoading && workflowRuns && workflowRuns.length === 0 && page > 1) {
      const params = new URLSearchParams();
      params.set("page", String(page - 1));
      setSearchParams(params, { replace: true });
    }
  }, [workflowRuns, isLoading, page, setSearchParams]);

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  if (!workflowPermanentId) {
    return null; // this should never happen
  }

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="flex flex-col gap-2">
          {workflowIsLoading ? (
            <>
              <Skeleton className="h-7 w-56" />
              <Skeleton className="h-7 w-56" />
            </>
          ) : (
            <>
              <h1 className="text-lg font-semibold">{workflow?.title}</h1>
              <h2 className="text-sm">{workflowPermanentId}</h2>
            </>
          )}
        </div>
        <div className="flex gap-2">
          {workflow && (
            <WorkflowActions
              workflow={workflow}
              onSuccessfullyDeleted={() => navigate("/workflows")}
            />
          )}
          <Button asChild variant="secondary">
            <Link to={`/workflows/${workflowPermanentId}/debug`}>
              <Pencil2Icon className="mr-2 size-4" />
              Edit
            </Link>
          </Button>
          <Button asChild>
            <Link to={`/workflows/${workflowPermanentId}/run`}>
              <PlayIcon className="mr-2 size-4" />
              Run
            </Link>
          </Button>
        </div>
      </header>
      <div className="space-y-4">
        <header>
          <h1 className="text-2xl">Past Runs</h1>
        </header>
        <div className="flex items-center justify-between gap-4">
          <div className="relative">
            <div className="absolute left-0 top-0 flex h-9 w-9 items-center justify-center">
              <MagnifyingGlassIcon className="size-5" />
            </div>
            <Input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                const params = new URLSearchParams(searchParams);
                params.set("page", "1");
                setSearchParams(params, { replace: true });
              }}
              placeholder="Search runs by parameter..."
              className="w-48 pl-9 lg:w-72"
            />
          </div>
          <StatusFilterDropdown
            values={statusFilters}
            onChange={setStatusFilters}
          />
        </div>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-1/3">ID</TableHead>
                <TableHead className="w-1/3">Status</TableHead>
                <TableHead className="w-1/3">Created At</TableHead>
                <TableHead className="w-0"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={3}>Loading...</TableCell>
                </TableRow>
              ) : workflowRuns?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={3}>No workflow runs found</TableCell>
                </TableRow>
              ) : (
                workflowRuns?.map((workflowRun) => {
                  const workflowRunId =
                    workflowRun.script_run === true ? (
                      <div className="flex items-center gap-2">
                        <Tip content="Ran with code">
                          <LightningBoltIcon className="text-[gold]" />
                        </Tip>
                        <span>{workflowRun.workflow_run_id ?? ""}</span>
                      </div>
                    ) : (
                      workflowRun.workflow_run_id ?? ""
                    );

                  return (
                    <TableRow
                      key={workflowRun.workflow_run_id}
                      onClick={(event) => {
                        if (event.ctrlKey || event.metaKey) {
                          window.open(
                            window.location.origin +
                              `/workflows/${workflowPermanentId}/${workflowRun.workflow_run_id}/overview`,
                            "_blank",
                            "noopener,noreferrer",
                          );
                          return;
                        }
                        navigate(
                          `/workflows/${workflowPermanentId}/${workflowRun.workflow_run_id}/overview`,
                        );
                      }}
                      className="cursor-pointer"
                    >
                      <TableCell>{workflowRunId}</TableCell>
                      <TableCell>
                        <StatusBadge status={workflowRun.status} />
                      </TableCell>
                      <TableCell
                        title={basicTimeFormat(workflowRun.created_at)}
                      >
                        {basicLocalTimeFormat(workflowRun.created_at)}
                      </TableCell>
                      <TableCell>
                        <div className="flex justify-end">
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  size="icon"
                                  variant="outline"
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setOpenRunParams(
                                      workflowRun.workflow_run_id ?? null,
                                    );
                                  }}
                                >
                                  <MixerHorizontalIcon className="h-4 w-4" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>View Parameters</TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
          <RunParametersDialog
            open={openRunParams !== null}
            onOpenChange={(open) => {
              if (!open) setOpenRunParams(null);
            }}
            workflowPermanentId={workflowPermanentId}
            workflowRunId={openRunParams}
          />
          <Pagination className="pt-2">
            <PaginationContent>
              <PaginationItem>
                <PaginationPrevious
                  className={cn({ "cursor-not-allowed": page === 1 })}
                  onClick={() => {
                    if (page === 1) {
                      return;
                    }
                    const params = new URLSearchParams();
                    params.set("page", String(Math.max(1, page - 1)));
                    setSearchParams(params, { replace: true });
                  }}
                />
              </PaginationItem>
              <PaginationItem>
                <PaginationLink>{page}</PaginationLink>
              </PaginationItem>
              <PaginationItem>
                <PaginationNext
                  className={cn({
                    "cursor-not-allowed":
                      workflowRuns !== undefined &&
                      workflowRuns.length < PAGE_SIZE,
                  })}
                  onClick={() => {
                    if (workflowRuns && workflowRuns.length < PAGE_SIZE) {
                      return;
                    }
                    const params = new URLSearchParams();
                    params.set("page", String(page + 1));
                    setSearchParams(params, { replace: true });
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

export { WorkflowPage };
