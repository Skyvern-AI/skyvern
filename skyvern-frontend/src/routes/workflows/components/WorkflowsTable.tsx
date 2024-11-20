import { getClient } from "@/api/AxiosClient";
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
import { LastRunStatus } from "./LastRunStatus";
import { LastRunAtTime } from "./LastRunAtTime";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { cn } from "@/util/utils";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { WorkflowApiResponse } from "../types/workflowTypes";

function WorkflowsTable() {
  const [page, setPage] = useState(1);
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();

  const { data: workflows, isLoading } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      return client
        .get("/workflows", {
          params,
        })
        .then((response) => response.data);
    },
  });

  const skeleton = Array.from({ length: 5 }).map((_, index) => (
    <TableRow key={index}>
      <TableCell>
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-6 w-full" />
      </TableCell>
    </TableRow>
  ));

  return (
    <div className="space-y-4">
      <Table>
        <TableHeader className="bg-slate-elevation2 text-slate-400 [&_tr]:border-b-0">
          <TableRow className="rounded-lg px-6 [&_th:first-child]:pl-6 [&_th]:py-4">
            <TableHead className="text-sm text-slate-400">Title</TableHead>
            <TableHead className="text-sm text-slate-400">
              Last Run Status
            </TableHead>
            <TableHead className="text-sm text-slate-400">
              Last Run Time
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell className="h-5"></TableCell>
          </TableRow>
        </TableBody>
        <TableBody>
          {isLoading && skeleton}
          {workflows?.map((workflow) => {
            return (
              <TableRow
                key={workflow.workflow_permanent_id}
                className="cursor-pointer [&_td:first-child]:pl-6 [&_td:last-child]:pr-6 [&_td]:py-4"
                onClick={(event) => {
                  if (event.ctrlKey || event.metaKey) {
                    window.open(
                      window.location.origin +
                        `/workflows/${workflow.workflow_permanent_id}`,
                      "_blank",
                      "noopener,noreferrer",
                    );
                    return;
                  }
                  navigate(`${workflow.workflow_permanent_id}`);
                }}
              >
                <TableCell>
                  <span className="text-sm leading-5">{workflow.title}</span>
                </TableCell>
                <TableCell>
                  <LastRunStatus workflowId={workflow.workflow_permanent_id} />
                </TableCell>
                <TableCell>
                  <LastRunAtTime workflowId={workflow.workflow_permanent_id} />
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      <Pagination>
        <PaginationContent>
          <PaginationItem>
            <PaginationPrevious
              className={cn({ "cursor-not-allowed": page === 1 })}
              onClick={() => {
                setPage((prev) => Math.max(1, prev - 1));
              }}
            />
          </PaginationItem>
          <PaginationItem>
            <PaginationLink>{page}</PaginationLink>
          </PaginationItem>
          <PaginationItem>
            <PaginationNext
              onClick={() => {
                setPage((prev) => prev + 1);
              }}
            />
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    </div>
  );
}

export { WorkflowsTable };
