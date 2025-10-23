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
  FileIcon,
  LightningBoltIcon,
  MagnifyingGlassIcon,
  MixerHorizontalIcon,
  Pencil2Icon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import { NarrativeCard } from "./components/header/NarrativeCard";
import { WorkflowParametersDialog } from "./components/WorkflowParametersDialog";
import { FolderCard } from "./components/FolderCard";
import { CreateFolderDialog } from "./components/CreateFolderDialog";
import { ViewAllFoldersDialog } from "./components/ViewAllFoldersDialog";
import { WorkflowFolderSelector } from "./components/WorkflowFolderSelector";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { useFoldersQuery } from "./hooks/useFoldersQuery";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowTemplates } from "../discover/WorkflowTemplates";

const emptyWorkflowRequest: WorkflowCreateYAMLRequest = {
  title: "New Workflow",
  description: "",
  ai_fallback: true,
  run_with: "agent",
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
  const [openWorkflowId, setOpenWorkflowId] = useState<string | null>(null);
  const [debouncedSearch] = useDebounce(search, 500);
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;

  // Folder state
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);
  const [isViewAllFoldersOpen, setIsViewAllFoldersOpen] = useState(false);

  // Fetch folders
  const { data: allFolders = [] } = useFoldersQuery({ page_size: 10 });

  // Sort folders by modified date (most recent first) and get top 5
  const recentFolders = useMemo(() => {
    return [...allFolders]
      .sort(
        (a, b) =>
          new Date(b.modified_at).getTime() - new Date(a.modified_at).getTime()
      )
      .slice(0, 5);
  }, [allFolders]);

  const { data: workflows = [], isLoading } = useQuery<
    Array<WorkflowApiResponse>
  >({
    queryKey: ["workflows", debouncedSearch, page, itemsPerPage, selectedFolderId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(itemsPerPage));
      params.append("only_workflows", "true");
      if (debouncedSearch) {
        params.append("search_key", debouncedSearch);
      }
      if (selectedFolderId) {
        params.append("folder_id", selectedFolderId);
      }
      return client
        .get(`/workflows`, {
          params,
        })
        .then((response) => response.data);
    },
  });

  const { data: nextPageWorkflows } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", debouncedSearch, page + 1, itemsPerPage, selectedFolderId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page + 1));
      params.append("page_size", String(itemsPerPage));
      params.append("only_workflows", "true");
      if (debouncedSearch) {
        params.append("search_key", debouncedSearch);
      }
      if (selectedFolderId) {
        params.append("folder_id", selectedFolderId);
      }
      return client
        .get(`/workflows`, {
          params,
        })
        .then((response) => response.data);
    },
    enabled: workflows.length === itemsPerPage,
  });

  const isNextDisabled =
    isLoading || !nextPageWorkflows || nextPageWorkflows.length === 0;

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

  function setParamPatch(patch: Record<string, string>) {
    const params = new URLSearchParams(searchParams);
    Object.entries(patch).forEach(([k, v]) => params.set(k, v));
    setSearchParams(params, { replace: true });
  }

  function handlePreviousPage() {
    if (page === 1) return;
    setParamPatch({ page: String(page - 1) });
  }

  function handleNextPage() {
    if (isNextDisabled) return;
    setParamPatch({ page: String(page + 1) });
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
        {/* Folders Section */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <h2 className="text-lg font-semibold">Folders</h2>
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-blue-600 dark:text-blue-400"
                onClick={() => setIsCreateFolderOpen(true)}
              >
                + New folder
              </Button>
            </div>
            {allFolders.length > 5 && (
              <Button
                variant="link"
                size="sm"
                className="text-blue-600 dark:text-blue-400"
                onClick={() => setIsViewAllFoldersOpen(true)}
              >
                View all
              </Button>
            )}
          </div>

          {recentFolders.length > 0 && (
            <div className="grid grid-cols-5 gap-4">
              {recentFolders.map((folder) => (
                <FolderCard
                  key={folder.folder_id}
                  folder={folder}
                  isSelected={selectedFolderId === folder.folder_id}
                  onClick={() =>
                    setSelectedFolderId(
                      selectedFolderId === folder.folder_id
                        ? null
                        : folder.folder_id
                    )
                  }
                />
              ))}
            </div>
          )}
        </div>

        {/* Workflows Section */}
        <header className="flex items-center justify-between">
          <h1 className="text-xl">My Flows</h1>
          {selectedFolderId && (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-blue-600 dark:text-blue-400"
              onClick={() => setSelectedFolderId(null)}
            >
              View all workflows
            </Button>
          )}
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
                setParamPatch({ page: "1" });
              }}
              placeholder="Search by title or parameter..."
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
                <TableHead className="w-1/4 rounded-tl-lg text-slate-400">
                  ID
                </TableHead>
                <TableHead className="w-1/4 text-slate-400">Title</TableHead>
                <TableHead className="w-1/6 text-slate-400">Folder</TableHead>
                <TableHead className="w-1/6 text-slate-400">
                  Created At
                </TableHead>
                <TableHead className="rounded-tr-lg"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={5}>Loading...</TableCell>
                </TableRow>
              ) : workflows?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5}>No workflows found</TableCell>
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
                      >
                        {workflow.folder_id ? (
                          <div className="flex items-center gap-1.5">
                            <FileIcon className="h-3.5 w-3.5 text-blue-400" />
                            <span className="text-sm">
                              {allFolders.find((f) => f.folder_id === workflow.folder_id)?.title || workflow.folder_id}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400">-</span>
                        )}
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
                          <WorkflowFolderSelector
                            workflowId={workflow.workflow_permanent_id}
                            currentFolderId={workflow.folder_id}
                          />
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  size="icon"
                                  variant="outline"
                                  onClick={(event) => {
                                    handleIconClick(
                                      event,
                                      `/workflows/${workflow.workflow_permanent_id}/debug`,
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
                          <TooltipProvider>
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  size="icon"
                                  variant="outline"
                                  onClick={() =>
                                    setOpenWorkflowId(
                                      workflow.workflow_permanent_id,
                                    )
                                  }
                                  disabled={
                                    !workflow.workflow_definition.parameters.some(
                                      (p) => p.parameter_type !== "output",
                                    )
                                  }
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
          <WorkflowParametersDialog
            open={openWorkflowId !== null}
            onOpenChange={(open) => {
              if (!open) setOpenWorkflowId(null);
            }}
            workflowId={openWorkflowId}
            workflows={workflows}
          />
          <div className="relative px-3 py-3">
            <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
              <span className="text-slate-400">Items per page</span>
              <select
                className="h-9 rounded-md border border-slate-300 bg-background px-3"
                value={itemsPerPage}
                onChange={(e) => {
                  const next = Number(e.target.value);
                  const params = new URLSearchParams(searchParams);
                  params.set("page_size", String(next));
                  params.set("page", "1");
                  setSearchParams(params, { replace: true });
                }}
              >
                <option value={5}>5</option>
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
              </select>
            </div>
            <Pagination className="pt-0">
              <PaginationContent>
                <PaginationItem>
                  <PaginationPrevious
                    className={cn({
                      "cursor-not-allowed opacity-50": page === 1,
                    })}
                    onClick={handlePreviousPage}
                  />
                </PaginationItem>
                <PaginationItem>
                  <PaginationLink>{page}</PaginationLink>
                </PaginationItem>
                <PaginationItem>
                  <PaginationNext
                    className={cn({
                      "cursor-not-allowed opacity-50": isNextDisabled,
                    })}
                    onClick={handleNextPage}
                  />
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          </div>
        </div>

        {/* Folder Dialogs */}
        <CreateFolderDialog
          open={isCreateFolderOpen}
          onOpenChange={setIsCreateFolderOpen}
        />
        <ViewAllFoldersDialog
          open={isViewAllFoldersOpen}
          onOpenChange={setIsViewAllFoldersOpen}
          selectedFolderId={selectedFolderId}
          onFolderSelect={setSelectedFolderId}
        />

        <WorkflowTemplates />
      </div>
    </div>
  );
}

export { Workflows };
