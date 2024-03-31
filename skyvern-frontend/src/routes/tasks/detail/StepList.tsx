import { client } from "@/api/AxiosClient";
import { StepApiResponse } from "@/api/types";
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
import { useQuery } from "@tanstack/react-query";
import { useParams, useSearchParams } from "react-router-dom";
import { StepListSkeleton } from "./StepListSkeleton";
import { TaskStatusBadge } from "@/components/TaskStatusBadge";
import { basicTimeFormat } from "@/util/timeFormat";

function StepList() {
  const { taskId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;

  const {
    data: steps,
    isFetching,
    isError,
    error,
  } = useQuery<Array<StepApiResponse>>({
    queryKey: ["task", taskId, "steps", page],
    queryFn: async () => {
      return client
        .get(`/tasks/${taskId}/steps`, {
          params: {
            page,
          },
        })
        .then((response) => response.data);
    },
  });

  if (isFetching) {
    return <StepListSkeleton />;
  }

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  if (!steps) {
    return <div>No steps found</div>;
  }

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-1/3">Order</TableHead>
            <TableHead className="w-1/3">Status</TableHead>
            <TableHead className="w-1/3">Created At</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {steps.length === 0 ? (
            <TableRow>
              <TableCell colSpan={3}>No tasks found</TableCell>
            </TableRow>
          ) : (
            steps.map((step) => {
              return (
                <TableRow key={step.step_id} className="cursor-pointer w-4">
                  <TableCell className="w-1/3">{step.order}</TableCell>
                  <TableCell className="w-1/3">
                    <TaskStatusBadge status={step.status} />
                  </TableCell>
                  <TableCell className="w-1/3">
                    {basicTimeFormat(step.created_at)}
                  </TableCell>
                </TableRow>
              );
            })
          )}
        </TableBody>
      </Table>
      <Pagination>
        <PaginationContent>
          <PaginationItem>
            <PaginationPrevious
              href="#"
              onClick={() => {
                const params = new URLSearchParams();
                params.set("page", String(Math.max(1, page - 1)));
                setSearchParams(params);
              }}
            />
          </PaginationItem>
          <PaginationItem>
            <PaginationLink href="#">{page}</PaginationLink>
          </PaginationItem>
          <PaginationItem>
            <PaginationNext
              href="#"
              onClick={() => {
                const params = new URLSearchParams();
                params.set("page", String(page + 1));
                setSearchParams(params);
              }}
            />
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    </>
  );
}

export { StepList };
