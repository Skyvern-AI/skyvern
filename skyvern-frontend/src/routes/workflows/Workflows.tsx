import { getClient } from "@/api/AxiosClient";
import { WorkflowApiResponse, WorkflowRunApiResponse } from "@/api/types";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { WorkflowsBetaAlertCard } from "./WorkflowsBetaAlertCard";
import { StatusBadge } from "@/components/StatusBadge";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { cn } from "@/util/utils";

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
              <TableHead className="w-1/2">ID</TableHead>
              <TableHead className="w-1/2">Title</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={2}>Loading...</TableCell>
              </TableRow>
            ) : workflows?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={2}>No workflows found</TableCell>
              </TableRow>
            ) : (
              workflows?.map((workflow) => {
                return (
                  <TableRow
                    key={workflow.workflow_permanent_id}
                    onClick={() => {
                      navigate(`${workflow.workflow_permanent_id}`);
                    }}
                    className="cursor-pointer"
                  >
                    <TableCell className="w-1/2">
                      {workflow.workflow_permanent_id}
                    </TableCell>
                    <TableCell className="w-1/2">{workflow.title}</TableCell>
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
                href="#"
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
                  setSearchParams(params);
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink href="#">{workflowsPage}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                href="#"
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("workflowsPage", String(workflowsPage + 1));
                  setSearchParams(params);
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
              <TableHead className="w-1/3">Workflow ID</TableHead>
              <TableHead className="w-1/3">Workflow Run ID</TableHead>
              <TableHead className="w-1/3">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {workflowRunsIsLoading ? (
              <TableRow>
                <TableCell colSpan={3}>Loading...</TableCell>
              </TableRow>
            ) : workflowRuns?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3}>No workflow runs found</TableCell>
              </TableRow>
            ) : (
              workflowRuns?.map((workflowRun) => {
                return (
                  <TableRow
                    key={workflowRun.workflow_run_id}
                    onClick={() => {
                      navigate(
                        `/workflows/${workflowRun.workflow_permanent_id}/${workflowRun.workflow_run_id}`,
                      );
                    }}
                    className="cursor-pointer"
                  >
                    <TableCell className="w-1/3">
                      {workflowRun.workflow_permanent_id}
                    </TableCell>
                    <TableCell className="w-1/3">
                      {workflowRun.workflow_run_id}
                    </TableCell>
                    <TableCell className="w-1/3">
                      <StatusBadge status={workflowRun.status} />
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
                href="#"
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
                  setSearchParams(params);
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink href="#">{workflowRunsPage}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                href="#"
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("workflowRunsPage", String(workflowRunsPage + 1));
                  setSearchParams(params);
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
