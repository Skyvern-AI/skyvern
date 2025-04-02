import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  LightningBoltIcon,
  MagnifyingGlassIcon,
  Pencil2Icon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import { NarrativeCard } from "./components/header/NarrativeCard";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowTemplates } from "../discover/WorkflowTemplates";

const emptyWorkflowRequest: WorkflowCreateYAMLRequest = {
  title: "New Workflow",
  description: "",
  workflow_definition: {
    blocks: [],
    parameters: [],
  },
};

function Workflows() {
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;

  const { data: workflows, isLoading } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", debouncedSearch, page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("only_workflows", "true");
      params.append("title", debouncedSearch);
      return client
        .get(`/workflows`, {
          params,
        })
        .then((response) => response.data);
    },
  });

  function handleRowClick(
    event: React.MouseEvent<HTMLTableCellElement>,
    workflowPermanentId: string,
  ) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + `/workflows/${workflowPermanentId}/runs`,
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    navigate(`/workflows/${workflowPermanentId}/runs`);
  }

  function handleIconClick(
    event: React.MouseEvent<HTMLButtonElement>,
    path: string,
  ) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + path,
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    navigate(path);
  }

  return (
    <div className="space-y-10">
      <div className="flex h-32 justify-between gap-6">
        <div className="space-y-5">
          <div className="flex items-center gap-2">
            <LightningBoltIcon className="size-6" />
            <h1 className="text-2xl">Workflows</h1>
          </div>
          <p className="text-slate-300">
            Create your own complex workflows by connecting web agents together.
            Define a series of actions, set it, and forget it.
          </p>
        </div>
        <div className="flex gap-5">
          <NarrativeCard
            index={1}
            description="Save browser sessions and reuse them in subsequent runs"
          />
          <NarrativeCard
            index={2}
            description="Connect multiple agents together to carry out complex objectives"
          />
          <NarrativeCard
            index={3}
            description="Execute non-browser tasks such as sending emails"
          />
        </div>
      </div>
      <div className="space-y-4">
        <header>
          <h1 className="text-xl">My Flows</h1>
        </header>
        <div className="flex justify-between">
          <div className="relative">
            <div className="absolute left-0 top-0 flex size-9 items-center justify-center">
              <MagnifyingGlassIcon className="size-6" />
            </div>
            <Input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
              }}
              placeholder="Search by title..."
              className="w-48 pl-9 lg:w-72"
            />
          </div>
          <div className="flex gap-4">
            <ImportWorkflowButton />
            <Button
              disabled={createWorkflowMutation.isPending}
              onClick={() => {
                createWorkflowMutation.mutate(emptyWorkflowRequest);
              }}
            >
              {createWorkflowMutation.isPending ? (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <PlusIcon className="mr-2 h-4 w-4" />
              )}
              Create
            </Button>
          </div>
        </div>
        <div className="rounded-lg border">
          <Table>
            <TableHeader className="rounded-t-lg bg-slate-elevation2">
              <TableRow>
                <TableHead className="w-1/3 rounded-tl-lg text-slate-400">
                  ID
                </TableHead>
                <TableHead className="w-1/3 text-slate-400">Title</TableHead>
                <TableHead className="w-1/3 text-slate-400">
                  Created At
                </TableHead>
                <TableHead className="rounded-tr-lg"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={4}>Loading...</TableCell>
                </TableRow>
              ) : workflows?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={4}>No workflows found</TableCell>
                </TableRow>
              ) : (
                workflows?.map((workflow) => {
                  return (
                    <TableRow
                      key={workflow.workflow_permanent_id}
                      className="cursor-pointer"
                    >
                      <TableCell
                        onClick={(event) => {
                          handleRowClick(event, workflow.workflow_permanent_id);
                        }}
                      >
                        {workflow.workflow_permanent_id}
                      </TableCell>
                      <TableCell
                        onClick={(event) => {
                          handleRowClick(event, workflow.workflow_permanent_id);
                        }}
                      >
                        {workflow.title}
                      </TableCell>
                      <TableCell
                        onClick={(event) => {
                          handleRowClick(event, workflow.workflow_permanent_id);
                        }}
                        title={basicTimeFormat(workflow.created_at)}
                      >
                        {basicLocalTimeFormat(workflow.created_at)}
                      </TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-2">
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  size="icon"
                                  variant="outline"
                                  onClick={(event) => {
                                    handleIconClick(
                                      event,
                                      `/workflows/${workflow.workflow_permanent_id}/edit`,
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
                                  onClick={(event) => {
                                    handleIconClick(
                                      event,
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
                          <WorkflowActions workflow={workflow} />
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
                  onClick={() => {
                    const params = new URLSearchParams();
                    params.set("page", String(page + 1));
                    setSearchParams(params, { replace: true });
                  }}
                />
              </PaginationItem>
            </PaginationContent>
          </Pagination>
        </div>
        <WorkflowTemplates />
      </div>
    </div>
  );
}

export { Workflows };
