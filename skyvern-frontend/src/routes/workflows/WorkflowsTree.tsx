import { getClient } from "@/api/AxiosClient";
import { GetStartedModal } from "@/components/onboarding/GetStartedModal";
import { useOnboardingStateOptional } from "@/store/onboarding/useOnboardingState";
import { OnboardingErrorBoundary } from "@/components/onboarding/OnboardingErrorBoundary";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { Button } from "@/components/ui/button";
import { SelectionHeaderCheckboxCell } from "@/components/SelectionCheckbox";
import { useRowSelection } from "@/hooks/useRowSelection";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableMessageRow,
  TableRow,
} from "@/components/ui/table";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  BULK_CONCURRENCY_LIMIT,
  runWithConcurrency,
} from "@/util/runWithConcurrency";
import { bulkResultToast } from "@/util/bulkResultToast";
import {
  BookmarkFilledIcon,
  ChevronDownIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { NarrativeCard } from "./components/header/NarrativeCard";
import { BulkActionBar } from "./components/BulkActionBar";
import { CreateFolderDialog } from "./components/CreateFolderDialog";
import { CreateFromTemplateDialog } from "./components/CreateFromTemplateDialog";
import { FolderTreeNode } from "./components/tree/FolderTreeNode";
import { WorkflowRow } from "./components/tree/WorkflowRow";
import {
  WorkflowsListContext,
  type WorkflowsListContextValue,
} from "./components/tree/WorkflowsListContext";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { useInfiniteFoldersQuery } from "./hooks/useInfiniteFoldersQuery";
import { useTagKeysQuery } from "./hooks/useTagKeysQuery";
import { useWorkflowTagsBatchQuery } from "./hooks/useWorkflowTagsBatchQuery";
import { useActiveImportsPolling } from "./hooks/useActiveImportsPolling";
import { WorkflowTagFilter } from "./components/tagging/WorkflowTagFilter";
import {
  parseTagFilter,
  serializeTagFilter,
  type TagFilterTerm,
} from "./types/tagTypes";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { useNodeCollapseStore } from "./editor/collapse/useNodeCollapseStore";
import { convert } from "./editor/workflowEditorUtils";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowTemplates } from "../discover/WorkflowTemplates";
import { Skeleton } from "@/components/ui/skeleton";
import { TableSearchInput } from "@/components/TableSearchInput";
import { useKeywordSearch } from "./hooks/useKeywordSearch";
import { useParameterExpansion } from "./hooks/useParameterExpansion";
import { Folder } from "./types/folderTypes";
import { getUniqueSlugForFolder } from "@/util/folderSlug";
import { defaultWorkflowRequest } from "./defaultWorkflowRequest";

const FOLDERS_PAGE_SIZE = 25;
const AGENTS_PAGE_SIZE = 20;

function WorkflowsTree() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 250);
  const [isBulkOperating, setIsBulkOperating] = useState(false);
  const [bulkDeleteDialog, setBulkDeleteDialog] = useState<{
    open: boolean;
    targets: string[];
  }>({ open: false, targets: [] });
  // Folder slug from query param (e.g., /workflows?folder=my-folder-name).
  // In the tree this is the "active" folder: highlighted, auto-expanded on load,
  // and the default target for Create / Import.
  const folderSlug = searchParams.get("folder");

  // Tag filter from query param (e.g., /workflows?tags=env:prod,env:staging).
  // The backend accepts both comma-separated pairs and repeated `tags` params,
  // so read every occurrence. The serialized form is canonical (sorted) and is
  // used for both the request and the query key so cache hits are
  // order-independent.
  const tagFilterParam = searchParams.getAll("tags").join(",");
  const tagFilters = useMemo(
    () => parseTagFilter(tagFilterParam),
    [tagFilterParam],
  );
  const serializedTagFilter = serializeTagFilter(tagFilters);

  const setTagFilters = useCallback(
    (terms: TagFilterTerm[]) => {
      const params = new URLSearchParams(searchParams);
      const serialized = serializeTagFilter(terms);
      if (serialized) {
        params.set("tags", serialized);
      } else {
        params.delete("tags");
      }
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);

  // Template dialog state
  const [isTemplateDialogOpen, setIsTemplateDialogOpen] = useState(false);

  // Tree expansion state and the contents reported by each expanded folder, used
  // to build one flat selection set spanning every open folder.
  const [expandedFolderIds, setExpandedFolderIds] = useState<Set<string>>(
    new Set(),
  );
  const [folderContents, setFolderContents] = useState<
    Map<string, Array<WorkflowApiResponse>>
  >(new Map());

  // Poll for active imports
  const { activeImports, startPolling } = useActiveImportsPolling();

  // Fetch folders (paginated, recent-first) for the tree.
  const {
    data: foldersData,
    isLoading: isFoldersLoading,
    hasNextPage: hasNextFolders,
    fetchNextPage: fetchNextFolders,
    isFetchingNextPage: isFetchingNextFolders,
  } = useInfiniteFoldersQuery({ page_size: FOLDERS_PAGE_SIZE });

  const allFolders = useMemo(
    () => foldersData?.pages.flatMap((folderPage) => folderPage) ?? [],
    [foldersData],
  );

  // Create a memoized map of slugs to folders to avoid O(n²) lookups
  const slugToFolderMap = useMemo(() => {
    const map = new Map<string, Folder>();
    for (const folder of allFolders) {
      const slug = getUniqueSlugForFolder(folder, allFolders);
      map.set(slug, folder);
    }
    return map;
  }, [allFolders]);

  // Folder state - derived from URL query param.
  // Look up the folder by matching the slug (handles collision suffixes like my-folder-2)
  const resolvedFolderIdBySlug = useRef<Map<string, string>>(new Map());
  // Once every page has loaded, a lookup miss is a real deletion/rename rather
  // than pagination not having reached the active folder yet.
  const foldersListComplete = !isFoldersLoading && !hasNextFolders;
  const selectedFolderId = useMemo(() => {
    if (!folderSlug) return null;
    const matchingFolder = slugToFolderMap.get(folderSlug);
    if (matchingFolder) {
      resolvedFolderIdBySlug.current.set(folderSlug, matchingFolder.folder_id);
      return matchingFolder.folder_id;
    }
    if (foldersListComplete) {
      resolvedFolderIdBySlug.current.delete(folderSlug);
      return null;
    }
    // The folders list is paginated, so a refetch can transiently drop the
    // active folder; reuse the last resolution instead of flapping to null.
    return resolvedFolderIdBySlug.current.get(folderSlug) ?? null;
  }, [folderSlug, slugToFolderMap, foldersListComplete]);

  // Clear folder param if the folder slug is invalid (folder deleted/renamed).
  useEffect(() => {
    if (
      folderSlug &&
      !selectedFolderId &&
      allFolders.length > 0 &&
      foldersListComplete
    ) {
      const params = new URLSearchParams(searchParams);
      params.delete("folder");
      setSearchParams(params, { replace: true });
    }
  }, [
    folderSlug,
    selectedFolderId,
    allFolders.length,
    foldersListComplete,
    searchParams,
    setSearchParams,
  ]);

  // Update folder query param (active folder). Does not reset pagination — the
  // folder no longer filters the agent list, it only highlights/targets.
  const setSelectedFolderId = (folderId: string | null) => {
    const params = new URLSearchParams(searchParams);
    if (folderId) {
      const folder = allFolders.find((f) => f.folder_id === folderId);
      if (folder) {
        const slug = getUniqueSlugForFolder(folder, allFolders);
        params.set("folder", slug);
        setSearchParams(params, { replace: true });
        return;
      }
    }
    params.delete("folder");
    setSearchParams(params, { replace: true });
  };

  // Auto-expand a deep-linked / active folder exactly once so a manual collapse
  // afterwards sticks.
  const deepLinkExpandedRef = useRef<string | null>(null);
  useEffect(() => {
    if (selectedFolderId && deepLinkExpandedRef.current !== selectedFolderId) {
      deepLinkExpandedRef.current = selectedFolderId;
      setExpandedFolderIds((prev) => {
        if (prev.has(selectedFolderId)) return prev;
        const next = new Set(prev);
        next.add(selectedFolderId);
        return next;
      });
    }
  }, [selectedFolderId]);

  const toggleFolderExpanded = useCallback((folderId: string) => {
    setExpandedFolderIds((prev) => {
      const next = new Set(prev);
      if (next.has(folderId)) {
        next.delete(folderId);
      } else {
        next.add(folderId);
      }
      return next;
    });
  }, []);

  const registerFolderContents = useCallback(
    (folderId: string, workflows: Array<WorkflowApiResponse>) => {
      setFolderContents((prev) => {
        const existing = prev.get(folderId);
        if ((existing?.length ?? 0) === 0 && workflows.length === 0) {
          return prev;
        }
        const next = new Map(prev);
        if (workflows.length === 0) {
          next.delete(folderId);
        } else {
          next.set(folderId, workflows);
        }
        return next;
      });
    },
    [],
  );

  const handleCreateAgentInFolder = useCallback(
    (folderId: string) => {
      if (createWorkflowMutation.isPending) {
        return;
      }
      createWorkflowMutation.mutate({
        ...defaultWorkflowRequest,
        folder_id: folderId,
        _via: "blank",
      });
    },
    [createWorkflowMutation],
  );

  // Create folders map for O(1) lookup
  const foldersMap = useMemo(() => {
    return new Map(allFolders.map((f) => [f.folder_id, f]));
  }, [allFolders]);

  // Infinite "Load more" list. Folders no longer filter this query, so it
  // returns every agent (foldered + ungrouped); ungrouped are derived below and
  // accumulate across pages, which keeps "Load more" coherent for the root list
  // (a page-number pager over the mixed list would scatter ungrouped agents).
  const {
    data: workflowsData,
    isLoading: isWorkflowsLoading,
    fetchNextPage: fetchMoreAgents,
    hasNextPage: hasMoreAgents,
    isFetchingNextPage: isFetchingMoreAgents,
    isPlaceholderData: isAgentsPlaceholderData,
  } = useInfiniteQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", "list", debouncedSearch, serializedTagFilter],
    queryFn: async ({ pageParam }) => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(pageParam));
      params.append("page_size", String(AGENTS_PAGE_SIZE));
      params.append("only_workflows", "true");
      if (debouncedSearch) {
        params.append("search_key", debouncedSearch);
      }
      if (serializedTagFilter) {
        params.append("tags", serializedTagFilter);
      }
      return client
        .get(`/workflows`, { params })
        .then((response) => response.data);
    },
    getNextPageParam: (lastPage, allPages) =>
      lastPage.length === AGENTS_PAGE_SIZE ? allPages.length + 1 : undefined,
    initialPageParam: 1,
    placeholderData: (previousData) => previousData,
  });

  const workflows = useMemo(
    () => workflowsData?.pages.flat() ?? [],
    [workflowsData],
  );

  // unfiltered "owns any workflow" check; the filtered/paginated list above can read empty for a user who has workflows
  const { data: ownedWorkflows = [], isLoading: ownedWorkflowsLoading } =
    useQuery<Array<WorkflowApiResponse>>({
      queryKey: ["workflows", "exists"],
      queryFn: async () => {
        const client = await getClient(credentialGetter);
        const params = new URLSearchParams();
        params.append("page", "1");
        params.append("page_size", "1");
        params.append("only_workflows", "true");
        return client
          .get(`/workflows`, { params })
          .then((response) => response.data);
      },
    });

  const onboarding = useOnboardingStateOptional();

  // Tag-key registry: supplies chip-hover descriptions and the filter's key list.
  const { data: tagKeys = [] } = useTagKeysQuery();
  const tagDescriptions = useMemo(
    () =>
      new Map(
        tagKeys.map((tagKey): [string, string | null] => [
          tagKey.key,
          tagKey.description,
        ]),
      ),
    [tagKeys],
  );

  // One batch fetch of current tags for every workflow on screen (no N+1) -
  // the main page list plus the contents of every expanded folder.
  const workflowIds = useMemo(() => {
    const ids = new Set<string>();
    for (const workflow of workflows) {
      ids.add(workflow.workflow_permanent_id);
    }
    for (const contents of folderContents.values()) {
      for (const workflow of contents) {
        ids.add(workflow.workflow_permanent_id);
      }
    }
    return Array.from(ids);
  }, [workflows, folderContents]);
  const { data: workflowTagsMap = {} } = useWorkflowTagsBatchQuery(workflowIds);

  // Tags observed on the page for editor/filter suggestions: grouped values per
  // key plus standalone labels. Maps avoid prototype-key collisions.
  const { valueSuggestionsByKey, labelSuggestions } = useMemo(() => {
    const collected = new Map<string, Set<string>>();
    const labels = new Set<string>();
    for (const tags of Object.values(workflowTagsMap)) {
      for (const tag of tags) {
        if (tag.key === null) {
          labels.add(tag.value);
          continue;
        }
        let values = collected.get(tag.key);
        if (!values) {
          values = new Set<string>();
          collected.set(tag.key, values);
        }
        values.add(tag.value);
      }
    }
    const byKey = new Map<string, string[]>();
    for (const [key, values] of collected) {
      byKey.set(key, [...values].sort());
    }
    return {
      valueSuggestionsByKey: byKey,
      labelSuggestions: [...labels].sort(),
    };
  }, [workflowTagsMap]);

  const { matchesParameter } = useKeywordSearch(debouncedSearch);
  const { expandedRows, toggleExpanded: toggleParametersExpanded } =
    useParameterExpansion();

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

  // A search term or tag filter flattens the tree back into a plain, complete
  // result list (folder hierarchy is not meaningful while filtering).
  const isFilterActive =
    debouncedSearch.trim().length > 0 || serializedTagFilter.length > 0;

  const importingOnly = useMemo(
    () => activeImports.filter((imp) => imp.status === "importing"),
    [activeImports],
  );

  const ungroupedWorkflows = useMemo(
    () => workflows.filter((workflow) => !workflow.folder_id),
    [workflows],
  );

  // Flat (filter) mode: the loaded list with importing placeholders pinned on top.
  const displayWorkflows = useMemo(() => {
    if (importingOnly.length > 0) {
      return [...importingOnly, ...workflows];
    }
    return workflows;
  }, [importingOnly, workflows]);

  // Tree mode: ungrouped agents render at the root, below the folders.
  const treeUngroupedWorkflows = useMemo(() => {
    if (importingOnly.length > 0) {
      return [...importingOnly, ...ungroupedWorkflows];
    }
    return ungroupedWorkflows;
  }, [importingOnly, ungroupedWorkflows]);

  // The set of workflows currently on screen, top-to-bottom, so checkbox /
  // shift-click selection works across folders and the ungrouped section.
  const visibleWorkflows = useMemo(() => {
    if (isFilterActive) {
      return displayWorkflows;
    }
    const result: Array<WorkflowApiResponse> = [];
    for (const folder of allFolders) {
      if (expandedFolderIds.has(folder.folder_id)) {
        const contents = folderContents.get(folder.folder_id);
        if (contents) {
          result.push(...contents);
        }
      }
    }
    result.push(...treeUngroupedWorkflows);
    return result;
  }, [
    isFilterActive,
    displayWorkflows,
    allFolders,
    expandedFolderIds,
    folderContents,
    treeUngroupedWorkflows,
  ]);

  const selectionItems = useMemo(
    () =>
      visibleWorkflows.filter((workflow) => workflow.status !== "importing"),
    [visibleWorkflows],
  );

  const showCheckbox = isFilterActive
    ? selectionItems.length > 0
    : allFolders.length > 0 || selectionItems.length > 0;
  const columnCount = showCheckbox ? 6 : 5;

  const {
    selected,
    selectedItems: selectedWorkflows,
    isSelected,
    allSelected,
    someSelected,
    indexById: selectableIndexById,
    handleSelect,
    toggleSelectAll,
    clearSelection,
    replaceSelection,
  } = useRowSelection({
    items: selectionItems,
    getId: (workflow) => workflow.workflow_permanent_id,
    resetKey: JSON.stringify([debouncedSearch, serializedTagFilter]),
    // The shift-click anchor is a positional index into the visible rows, so it
    // must reset whenever that order changes (expand/collapse, lazy load, load
    // more) or a stale anchor would select the wrong range.
    anchorResetKey: JSON.stringify(
      selectionItems.map((workflow) => workflow.workflow_permanent_id),
    ),
  });

  const handleFolderClick = (folderId: string) => {
    const willExpand = !expandedFolderIds.has(folderId);
    toggleFolderExpanded(folderId);
    if (willExpand) {
      setSelectedFolderId(folderId);
      return;
    }
    // Collapsing hides this folder's rows; drop any of them from the selection
    // so the selection set always matches what the user can see and act on.
    const contents = folderContents.get(folderId);
    if (contents && contents.length > 0) {
      const next = new Set(selected);
      let changed = false;
      for (const workflow of contents) {
        if (next.delete(workflow.workflow_permanent_id)) {
          changed = true;
        }
      }
      if (changed) {
        replaceSelection(next);
      }
    }
    if (selectedFolderId === folderId) {
      setSelectedFolderId(null);
    }
  };

  async function handleBulkMoveToFolder(folderId: string | null) {
    if (selectedWorkflows.length === 0) {
      return;
    }

    setIsBulkOperating(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        selectedWorkflows.map(
          (workflow) => () =>
            client.put(`/workflows/${workflow.workflow_permanent_id}/folder`, {
              folder_id: folderId,
            }),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      bulkResultToast({
        succeeded,
        total: selectedWorkflows.length,
        results,
        successTitle: (count) =>
          folderId
            ? `Moved ${count} agent${count !== 1 ? "s" : ""} to folder.`
            : `Removed ${count} agent${count !== 1 ? "s" : ""} from folder.`,
        failureTitle: (count) =>
          folderId
            ? `Failed to move ${count} agent${count !== 1 ? "s" : ""} to folder.`
            : `Failed to remove ${count} agent${count !== 1 ? "s" : ""} from folder.`,
        partialTitle: (successCount, failedCount) =>
          folderId
            ? `Moved ${successCount} agent${successCount !== 1 ? "s" : ""} to folder. ${failedCount} failed.`
            : `Removed ${successCount} agent${successCount !== 1 ? "s" : ""} from folder. ${failedCount} failed.`,
      });
      if (succeeded === selectedWorkflows.length) {
        clearSelection();
      }
      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
        queryClient.invalidateQueries({ queryKey: ["folders"] });
      }
    } finally {
      setIsBulkOperating(false);
    }
  }

  async function handleBulkDeleteConfirm() {
    // Snapshot from dialog-open time; the live selection may have changed since.
    const targets = bulkDeleteDialog.targets;
    if (targets.length === 0) {
      return;
    }

    setIsBulkOperating(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        targets.map(
          (workflowId) => () => client.delete(`/workflows/${workflowId}`),
        ),
        BULK_CONCURRENCY_LIMIT,
      );

      const succeededIds: string[] = [];
      const failedIds = new Set<string>();
      results.forEach((result, index) => {
        const workflowId = targets[index]!;
        if (result.status === "fulfilled") {
          succeededIds.push(workflowId);
          useNodeCollapseStore.getState().pruneWorkflow(workflowId);
        } else {
          failedIds.add(workflowId);
        }
      });

      const succeeded = succeededIds.length;
      const failed = failedIds.size;

      bulkResultToast({
        succeeded,
        total: targets.length,
        results,
        successTitle: (count) =>
          `${count} agent${count !== 1 ? "s" : ""} deleted successfully.`,
        failureTitle: (count) =>
          `Failed to delete ${count} agent${count !== 1 ? "s" : ""}.`,
        partialTitle: (successCount, failedCount) =>
          `Deleted ${successCount} agent${successCount !== 1 ? "s" : ""}. ${failedCount} failed.`,
      });
      // Deletions shift row indices; replace/clear also resets the shift anchor.
      if (failed === 0) {
        clearSelection();
      } else {
        replaceSelection(failedIds);
      }

      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
        queryClient.invalidateQueries({ queryKey: ["folders"] });
      }
    } finally {
      setIsBulkOperating(false);
      setBulkDeleteDialog({ open: false, targets: [] });
    }
  }

  const listContextValue: WorkflowsListContextValue = {
    showCheckbox,
    columnCount,
    debouncedSearch,
    isBulkOperating,
    selectedCount: selectedWorkflows.length,
    foldersMap,
    workflowTagsMap,
    tagDescriptions,
    tagKeys,
    labelSuggestions,
    valueSuggestionsByKey,
    isSelected,
    indexById: selectableIndexById,
    handleSelect,
    expandedRows,
    toggleParametersExpanded,
    matchesParameter,
    handleRowClick,
    handleIconClick,
  };

  const showFlatInitialSkeleton =
    isFilterActive && isWorkflowsLoading && displayWorkflows.length === 0;
  const showTreeInitialSkeleton =
    !isFilterActive &&
    (isFoldersLoading || isWorkflowsLoading) &&
    allFolders.length === 0 &&
    treeUngroupedWorkflows.length === 0;
  const isEmpty =
    !isFilterActive &&
    !showTreeInitialSkeleton &&
    allFolders.length === 0 &&
    treeUngroupedWorkflows.length === 0;

  function renderSkeletonRows() {
    return Array.from({ length: 10 }).map((_, index) => (
      <TableRow key={`skeleton-${index}`}>
        {showCheckbox && (
          <TableCell>
            <Skeleton className="h-4 w-4" />
          </TableCell>
        )}
        <TableCell>
          <Skeleton className="h-5 w-full" />
        </TableCell>
        <TableCell>
          <Skeleton className="h-5 w-full" />
        </TableCell>
        <TableCell>
          <Skeleton className="h-5 w-20" />
        </TableCell>
        <TableCell>
          <Skeleton className="h-5 w-32" />
        </TableCell>
        <TableCell>
          <div className="flex justify-end gap-2">
            <Skeleton className="h-8 w-8 rounded" />
            <Skeleton className="h-8 w-8 rounded" />
            <Skeleton className="h-8 w-8 rounded" />
            <Skeleton className="h-8 w-8 rounded" />
          </div>
        </TableCell>
      </TableRow>
    ));
  }

  function renderLoadMoreAgentsRow() {
    if (!hasMoreAgents) {
      return null;
    }
    return (
      <TableRow>
        {showCheckbox && <TableCell />}
        <TableCell colSpan={showCheckbox ? columnCount - 1 : columnCount}>
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0 text-blue-600 dark:text-blue-400"
            disabled={isFetchingMoreAgents || isAgentsPlaceholderData}
            onClick={() => void fetchMoreAgents()}
          >
            {isFetchingMoreAgents ? (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            ) : null}
            Load more agents
          </Button>
        </TableCell>
      </TableRow>
    );
  }

  function renderTableBody() {
    if (isFilterActive) {
      if (showFlatInitialSkeleton) {
        return renderSkeletonRows();
      }
      if (displayWorkflows.length === 0) {
        return (
          <TableMessageRow colSpan={columnCount}>
            No agents found
          </TableMessageRow>
        );
      }
      return (
        <>
          {displayWorkflows.map((workflow) => (
            <WorkflowRow
              key={workflow.workflow_permanent_id}
              workflow={workflow}
            />
          ))}
          {renderLoadMoreAgentsRow()}
        </>
      );
    }

    if (showTreeInitialSkeleton) {
      return renderSkeletonRows();
    }

    if (isEmpty) {
      return (
        <TableMessageRow colSpan={columnCount}>
          <div className="flex flex-col items-center gap-3 py-6">
            <FolderIcon className="h-8 w-8 text-blue-400 opacity-50" />
            <p className="text-sm text-muted-foreground">
              No folders or agents yet. Create a folder to organize your work,
              or create your first agent.
            </p>
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-blue-600 dark:text-blue-400"
              onClick={() => setIsCreateFolderOpen(true)}
            >
              <PlusIcon className="mr-1 h-4 w-4" />
              Create folder
            </Button>
          </div>
        </TableMessageRow>
      );
    }

    return (
      <>
        {allFolders.map((folder) => (
          <FolderTreeNode
            key={folder.folder_id}
            folder={folder}
            isExpanded={expandedFolderIds.has(folder.folder_id)}
            isActive={selectedFolderId === folder.folder_id}
            onToggle={() => handleFolderClick(folder.folder_id)}
            onContentsChange={registerFolderContents}
            onCreateAgentInFolder={handleCreateAgentInFolder}
            isCreatingAgent={createWorkflowMutation.isPending}
          />
        ))}
        {hasNextFolders && (
          <TableRow>
            {showCheckbox && <TableCell />}
            <TableCell colSpan={showCheckbox ? columnCount - 1 : columnCount}>
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-blue-600 dark:text-blue-400"
                disabled={isFetchingNextFolders}
                onClick={() => void fetchNextFolders()}
              >
                {isFetchingNextFolders ? (
                  <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                ) : null}
                Load more folders
              </Button>
            </TableCell>
          </TableRow>
        )}
        {treeUngroupedWorkflows.map((workflow) => (
          <WorkflowRow
            key={workflow.workflow_permanent_id}
            workflow={workflow}
          />
        ))}
        {renderLoadMoreAgentsRow()}
      </>
    );
  }

  return (
    <div className="space-y-10">
      <div className="flex h-32 justify-between gap-6">
        <div className="space-y-5">
          <div className="flex items-center gap-2">
            <h1 className="text-2xl">Agents</h1>
          </div>
          <p className="text-sm leading-6 text-muted-foreground">
            Create your own complex agents by connecting web agents together.
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
        <header className="flex items-center justify-between">
          <h1 className="text-xl">My Agents</h1>
          {selectedFolderId && (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-blue-600 dark:text-blue-400"
              onClick={() => setSelectedFolderId(null)}
            >
              Clear folder selection
            </Button>
          )}
        </header>
        <div className="flex justify-between">
          <div className="flex items-center gap-3">
            <TableSearchInput
              value={search}
              onChange={(value) => {
                setSearch(value);
              }}
              placeholder="Search by title or input..."
              className="w-48 lg:w-72"
            />
            <WorkflowTagFilter
              tagKeys={tagKeys}
              value={tagFilters}
              onChange={setTagFilters}
              labelSuggestions={labelSuggestions}
              valueSuggestionsByKey={valueSuggestionsByKey}
            />
          </div>
          <div className="flex items-center gap-4">
            <Link
              to="/discover"
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              Or start from a description →
            </Link>
            <Button
              variant="secondary"
              onClick={() => setIsCreateFolderOpen(true)}
            >
              <FolderIcon className="mr-2 h-4 w-4" />
              New Folder
            </Button>
            <ImportWorkflowButton
              onImportStart={startPolling}
              selectedFolderId={selectedFolderId}
            />
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button disabled={createWorkflowMutation.isPending}>
                  {createWorkflowMutation.isPending ? (
                    <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <PlusIcon className="mr-2 h-4 w-4" />
                  )}
                  Create
                  <ChevronDownIcon className="ml-2 h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onSelect={() => {
                    createWorkflowMutation.mutate({
                      ...defaultWorkflowRequest,
                      folder_id: selectedFolderId,
                      _via: "blank",
                    });
                  }}
                >
                  <PlusIcon className="mr-2 h-4 w-4" />
                  Blank Agent
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => setIsTemplateDialogOpen(true)}
                >
                  <BookmarkFilledIcon className="mr-2 h-4 w-4" />
                  From Template
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        <WorkflowsListContext.Provider value={listContextValue}>
          <div className="overflow-hidden rounded-lg border border-border">
            <Table className="table-fixed">
              <TableHeader>
                <TableRow className="group/header">
                  {showCheckbox && (
                    <SelectionHeaderCheckboxCell
                      className="w-[3%]"
                      allSelected={allSelected}
                      someSelected={someSelected}
                      hasSelection={selected.size > 0}
                      onToggleAll={toggleSelectAll}
                      ariaLabel="Select all agents"
                    />
                  )}
                  <TableHead className={showCheckbox ? "w-[22%]" : "w-[25%]"}>
                    ID
                  </TableHead>
                  <TableHead className={showCheckbox ? "w-[27%]" : "w-[30%]"}>
                    Title
                  </TableHead>
                  <TableHead className="w-[15%]">Folder</TableHead>
                  <TableHead className="w-[15%]">Created At</TableHead>
                  <TableHead className="w-[15%] text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>{renderTableBody()}</TableBody>
            </Table>
          </div>
        </WorkflowsListContext.Provider>

        {selectedWorkflows.length > 0 && (
          <BulkActionBar
            selectedWorkflows={selectedWorkflows}
            isOperating={isBulkOperating}
            onOperatingChange={setIsBulkOperating}
            onClearSelection={clearSelection}
            onDeleteRequest={() =>
              setBulkDeleteDialog({
                open: true,
                targets: selectedWorkflows.map(
                  (workflow) => workflow.workflow_permanent_id,
                ),
              })
            }
            onMoveToFolder={handleBulkMoveToFolder}
            tagKeys={tagKeys}
            labelSuggestions={labelSuggestions}
            valueSuggestionsByKey={valueSuggestionsByKey}
          />
        )}

        <Dialog
          open={bulkDeleteDialog.open}
          onOpenChange={(open) => {
            if (!open && !isBulkOperating) {
              setBulkDeleteDialog({ open: false, targets: [] });
            }
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                Delete {bulkDeleteDialog.targets.length} Agent
                {bulkDeleteDialog.targets.length === 1 ? "" : "s"}
              </DialogTitle>
              <DialogDescription>
                Are you sure you want to delete{" "}
                {bulkDeleteDialog.targets.length}{" "}
                {bulkDeleteDialog.targets.length === 1 ? "agent" : "agents"}?
                This action cannot be undone.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="secondary"
                disabled={isBulkOperating}
                onClick={() =>
                  setBulkDeleteDialog({ open: false, targets: [] })
                }
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={isBulkOperating}
                onClick={() => {
                  void handleBulkDeleteConfirm();
                }}
              >
                {isBulkOperating ? "Deleting..." : "Delete"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

        {/* Folder Dialogs */}
        <CreateFolderDialog
          open={isCreateFolderOpen}
          onOpenChange={setIsCreateFolderOpen}
        />

        {/* Template Dialog */}
        <CreateFromTemplateDialog
          open={isTemplateDialogOpen}
          onOpenChange={setIsTemplateDialogOpen}
          onSelectTemplate={(template) => {
            const clonedWorkflow = convert({
              ...template,
              title: `${template.title} (copy)`,
            });
            createWorkflowMutation.mutate({
              ...clonedWorkflow,
              folder_id: selectedFolderId,
            });
          }}
        />

        <div data-hint="start-template">
          <WorkflowTemplates />
        </div>

        {onboarding ? (
          <OnboardingErrorBoundary
            onError={() => OnboardingTelemetry.modalRenderError("dashboard")}
          >
            <GetStartedModal
              hasWorkflows={ownedWorkflows.length > 0}
              isLoading={ownedWorkflowsLoading}
            />
          </OnboardingErrorBoundary>
        ) : null}
      </div>
    </div>
  );
}

export { WorkflowsTree };
