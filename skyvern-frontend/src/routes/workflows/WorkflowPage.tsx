import { getClient } from "@/api/AxiosClient";
import { WorkflowRunApiResponse } from "@/api/types";
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
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { useQuery } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";

function WorkflowPage() {
  const credentialGetter = useCredentialGetter();
  const { workflowPermanentId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const navigate = useNavigate();

  const { data: workflowRuns, isLoading } = useQuery<
    Array<WorkflowRunApiResponse>
  >({
    queryKey: ["workflowRuns", workflowPermanentId, page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      return client
        .get(`/workflows/${workflowPermanentId}/runs`, {
          params,
        })
        .then((response) => response.data);
    },
  });

  if (!workflowPermanentId) {
    return null; // this should never happen
  }

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <h1 className="text-lg font-semibold">{workflowPermanentId}</h1>
        <Button asChild>
          <Link to="run">Create New Run</Link>
        </Button>
      </header>
      <div>
        <header>
          <h1 className="text-lg font-semibold">Past Runs</h1>
        </header>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-1/2">ID</TableHead>
              <TableHead className="w-1/2">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={2}>Loading...</TableCell>
              </TableRow>
            ) : workflowRuns?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={2}>No workflow runs found</TableCell>
              </TableRow>
            ) : (
              workflowRuns?.map((workflowRun) => (
                <TableRow
                  key={workflowRun.workflow_run_id}
                  onClick={() => {
                    navigate(`${workflowRun.workflow_run_id}`);
                  }}
                  className="cursor-pointer"
                >
                  <TableCell>{workflowRun.workflow_run_id}</TableCell>
                  <TableCell>
                    <StatusBadge status={workflowRun.status} />
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
        <Pagination className="pt-2">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                href="#"
                className={cn({ "cursor-not-allowed": page === 1 })}
                onClick={() => {
                  if (page === 1) {
                    return;
                  }
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
      </div>
    </div>
  );
}

export { WorkflowPage };
