import { getClient } from "@/api/AxiosClient";
import { WorkflowApiResponse, WorkflowRunApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  CounterClockwiseClockIcon,
  Pencil2Icon,
  PlayIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkflowsBetaAlertCard } from "./WorkflowsBetaAlertCard";
import { WorkflowTitle } from "./WorkflowTitle";

function Workflows() {
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const workflowsPage = searchParams.get("workflowsPage")
    ? Number(searchParams.get("workflowsPage"))
    : 1;
  const workflowRunsPage = searchParams.get("workflowRunsPage")
    ? Number(searchParams.get("workflowRunsPage"))
    : 1;

  const { data: workflows, isLoading } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", workflowsPage],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(workflowsPage));
      params.append("only_workflows", "true");
      return client
        .get(`/workflows`, {
          params,
        })
        .then((response) => response.data);
    },
  });

  const { data: workflowRuns, isLoading: workflowRunsIsLoading } = useQuery<
    Array<WorkflowRunApiResponse>
  >({
    queryKey: ["workflowRuns", workflowRunsPage],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(workflowRunsPage));
      return client
        .get("/workflows/runs", {
          params,
        })
        .then((response) => response.data);
    },
  });

  if (workflows?.length === 0 && workflowsPage === 1) {
    return <WorkflowsBetaAlertCard />;
  }

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Workflows</h1>
      </header>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-1/4">ID</TableHead>
              <TableHead className="w-1/4">Status</TableHead>
              <TableHead className="w-1/4">Created At</TableHead>
              <TableHead className="w-1/4"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={3}>Loading...</TableCell>
              </TableRow>
            ) : workflows?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3}>No workflows found</TableCell>
              </TableRow>
            ) : (
              workflows?.map((workflow) => {
                return (
                  <TableRow key={workflow.workflow_permanent_id}>
                    <TableCell className="w-1/3">
                      {workflow.workflow_permanent_id}
                    </TableCell>
                    <TableCell className="w-1/3">{workflow.title}</TableCell>
                    <TableCell className="w-1/3">
                      {basicTimeFormat(workflow.created_at)}
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-2">
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                size="icon"
                                variant="outline"
                                onClick={() => {
                                  navigate(
                                    `/workflows/${workflow.workflow_permanent_id}/runs`,
                                  );
                                }}
                              >
                                <CounterClockwiseClockIcon className="h-4 w-4" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>View Past Runs</TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                size="icon"
                                variant="outline"
                                onClick={() => {
                                  navigate(
                                    `/workflows/${workflow.workflow_permanent_id}`,
                                  );
                                }}
                              >
                                <Pencil2Icon className="h-4 w-4" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Open in Editor</TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                size="icon"
                                variant="outline"
                                onClick={() => {
                                  navigate(
                                    `/workflows/${workflow.workflow_permanent_id}/run`,
                                  );
                                }}
                              >
                                <PlayIcon className="h-4 w-4" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Create New Run</TooltipContent>
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
        <Pagination className="pt-2">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                className={cn({ "cursor-not-allowed": workflowsPage === 1 })}
                onClick={() => {
                  if (workflowsPage === 1) {
                    return;
                  }
                  const params = new URLSearchParams();
                  params.set(
                    "workflowsPage",
                    String(Math.max(1, workflowsPage - 1)),
                  );
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink>{workflowsPage}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("workflowsPage", String(workflowsPage + 1));
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
      <header>
        <h1 className="text-2xl font-semibold">Workflow Runs</h1>
      </header>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-1/5">Workflow Run ID</TableHead>
              <TableHead className="w-1/5">Workflow ID</TableHead>
              <TableHead className="w-1/5">Workflow Title</TableHead>
              <TableHead className="w-1/5">Status</TableHead>
              <TableHead className="w-1/5">Created At</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {workflowRunsIsLoading ? (
              <TableRow>
                <TableCell colSpan={5}>Loading...</TableCell>
              </TableRow>
            ) : workflowRuns?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5}>No workflow runs found</TableCell>
              </TableRow>
            ) : (
              workflowRuns?.map((workflowRun) => {
                return (
                  <TableRow
                    key={workflowRun.workflow_run_id}
                    onClick={(event) => {
                      if (event.ctrlKey || event.metaKey) {
                        window.open(
                          window.location.origin +
                            `/workflows/${workflowRun.workflow_permanent_id}/${workflowRun.workflow_run_id}`,
                          "_blank",
                          "noopener,noreferrer",
                        );
                        return;
                      }
                      navigate(
                        `/workflows/${workflowRun.workflow_permanent_id}/${workflowRun.workflow_run_id}`,
                      );
                    }}
                    className="cursor-pointer"
                  >
                    <TableCell className="w-1/5">
                      {workflowRun.workflow_run_id}
                    </TableCell>
                    <TableCell className="w-1/5">
                      {workflowRun.workflow_permanent_id}
                    </TableCell>
                    <TableCell className="w-1/5">
                      <WorkflowTitle
                        workflowPermanentId={workflowRun.workflow_permanent_id}
                      />
                    </TableCell>
                    <TableCell className="w-1/5">
                      <StatusBadge status={workflowRun.status} />
                    </TableCell>
                    <TableCell className="w-1/5">
                      {basicTimeFormat(workflowRun.created_at)}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
        <Pagination className="pt-2">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                className={cn({ "cursor-not-allowed": workflowRunsPage === 1 })}
                onClick={() => {
                  if (workflowRunsPage === 1) {
                    return;
                  }
                  const params = new URLSearchParams();
                  params.set(
                    "workflowRunsPage",
                    String(Math.max(1, workflowRunsPage - 1)),
                  );
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink>{workflowRunsPage}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("workflowRunsPage", String(workflowRunsPage + 1));
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
    </div>
  );
}

export { Workflows };
