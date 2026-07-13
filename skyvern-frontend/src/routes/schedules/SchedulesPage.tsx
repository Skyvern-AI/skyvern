import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import { getErrorDetail } from "@/util/getErrorDetail";
import {
  ChevronDownIcon,
  CopyIcon,
  DotsHorizontalIcon,
  ExclamationTriangleIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { Tip } from "@/components/Tip";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "@/components/ui/use-toast";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { SelectionBar } from "@/components/SelectionBar";
import {
  SelectionCheckboxCell,
  SelectionHeaderCheckboxCell,
} from "@/components/SelectionCheckbox";
import { useRowSelection } from "@/hooks/useRowSelection";
import { bulkResultToast } from "@/util/bulkResultToast";
import {
  BULK_CONCURRENCY_LIMIT,
  runWithConcurrency,
} from "@/util/runWithConcurrency";
import { TableSearchInput } from "@/components/TableSearchInput";
import { Pill } from "@/components/StatusBadge";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useOrganizationSchedulesQuery } from "./useOrganizationSchedulesQuery";
import {
  useDeleteOrgScheduleMutation,
  useDisableScheduleMutation,
  useDuplicateScheduleMutation,
  useEnableScheduleMutation,
} from "./useScheduleActions";
import {
  cronToHumanReadable,
  isValidCron,
  meetsMinCronInterval,
} from "@/routes/workflows/editor/panels/schedulePanel/cronUtils";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import type { OrganizationScheduleItem } from "@/routes/workflows/types/scheduleTypes";
import { CreateOrgScheduleDialog } from "./CreateOrgScheduleDialog";

type ScheduleStatus = "active" | "paused";

const STATUS_OPTIONS: Array<{ label: string; value: ScheduleStatus }> = [
  { label: "Active", value: "active" },
  { label: "Paused", value: "paused" },
];

function StatusDisplay({ enabled }: Readonly<{ enabled: boolean }>) {
  return (
    <Pill tone={enabled ? "success" : "queued"} className="capitalize">
      {enabled ? "active" : "paused"}
    </Pill>
  );
}

const PAGE_SIZE_OPTIONS = ["10", "25", "50"];

function SchedulesPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = Number(searchParams.get("page") || "1");
  const pageSize = Number(searchParams.get("page_size") || "10");
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const [statusFilters, setStatusFilters] = useState<ScheduleStatus[]>([]);
  const [isBulkOperating, setIsBulkOperating] = useState(false);
  const [deleteDialog, setDeleteDialog] = useState<{
    open: boolean;
    schedule: OrganizationScheduleItem | null;
  }>({ open: false, schedule: null });
  const [bulkDeleteDialog, setBulkDeleteDialog] = useState<{
    open: boolean;
    count: number;
  }>({ open: false, count: 0 });
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const statusFilter =
    statusFilters.length === 1 ? statusFilters[0] : undefined;

  const { data, isLoading, isError, error } = useOrganizationSchedulesQuery({
    page,
    pageSize,
    statusFilter,
    search: debouncedSearch || undefined,
  });
  const errorDetail = getErrorDetail(error);

  const enableMutation = useEnableScheduleMutation();
  const disableMutation = useDisableScheduleMutation();
  const deleteMutation = useDeleteOrgScheduleMutation();
  const duplicateMutation = useDuplicateScheduleMutation();

  const schedules = useMemo(() => data?.schedules ?? [], [data]);
  const totalCount = data?.total_count ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));

  // meetsMinCronInterval samples up to 2000 future fires; memoize per cron string
  // so unrelated re-renders (selection, dialogs) don't re-run it for every row.
  const cronsBelowMinInterval = useMemo(() => {
    const violations = new Set<string>();
    for (const schedule of schedules) {
      if (
        isValidCron(schedule.cron_expression) &&
        !meetsMinCronInterval(schedule.cron_expression)
      ) {
        violations.add(schedule.cron_expression);
      }
    }
    return violations;
  }, [schedules]);

  const {
    selected,
    selectedItems: selectedSchedules,
    isSelected,
    allSelected,
    someSelected,
    handleSelect,
    toggleSelectAll,
    clearSelection,
    replaceSelection,
  } = useRowSelection({
    items: schedules,
    getId: (s) => s.workflow_schedule_id,
    resetKey: JSON.stringify([page, pageSize, statusFilters, debouncedSearch]),
  });

  function setPage(p: number) {
    const params = new URLSearchParams(searchParams);
    params.set("page", String(p));
    setSearchParams(params);
  }

  function setPageSize(size: string) {
    const params = new URLSearchParams(searchParams);
    params.set("page_size", size);
    params.set("page", "1");
    setSearchParams(params);
  }

  function handleDeleteConfirm() {
    if (!deleteDialog.schedule) return;
    deleteMutation.mutate(deleteDialog.schedule, {
      onSettled: () => {
        setDeleteDialog({ open: false, schedule: null });
      },
    });
  }

  async function runBulkOperation(
    items: OrganizationScheduleItem[],
    makeTask: (
      client: Awaited<ReturnType<typeof getClient>>,
      item: OrganizationScheduleItem,
    ) => Promise<void>,
    successLabel: string,
    failureLabel: string,
  ) {
    if (items.length === 0) return;
    setIsBulkOperating(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        items.map((item) => () => makeTask(client, item)),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      bulkResultToast({
        succeeded,
        total: items.length,
        results,
        successTitle: (n) =>
          `${n} schedule${n !== 1 ? "s" : ""} ${successLabel} successfully.`,
        failureTitle: (n) =>
          `Failed to ${failureLabel} ${n} schedule${n !== 1 ? "s" : ""}.`,
        partialTitle: (successCount, failedCount) =>
          `${successCount} schedule${successCount !== 1 ? "s" : ""} ${successLabel} successfully. ${failedCount} failed.`,
      });
      if (succeeded === items.length) {
        clearSelection();
      } else if (succeeded > 0) {
        // Keep only failed items selected so the user can retry
        const failedIds = new Set<string>();
        results.forEach((result, i) => {
          if (result.status === "rejected") {
            failedIds.add(items[i]!.workflow_schedule_id);
          }
        });
        replaceSelection(failedIds);
      }
      queryClient.invalidateQueries({ queryKey: ["organizationSchedules"] });
      queryClient.invalidateQueries({ queryKey: ["scheduleDetail"] });
    } finally {
      setIsBulkOperating(false);
    }
  }

  function handleBulkActivate() {
    const toActivate = selectedSchedules.filter((s) => !s.enabled);
    if (toActivate.length === 0) {
      toast({ title: "All selected schedules are already active." });
      return;
    }
    void runBulkOperation(
      toActivate,
      (client, item) =>
        client.post(
          `/workflows/${item.workflow_permanent_id}/schedules/${item.workflow_schedule_id}/enable`,
        ),
      "activated",
      "activate",
    );
  }

  function handleBulkPause() {
    const toPause = selectedSchedules.filter((s) => s.enabled);
    if (toPause.length === 0) {
      toast({ title: "All selected schedules are already paused." });
      return;
    }
    void runBulkOperation(
      toPause,
      (client, item) =>
        client.post(
          `/workflows/${item.workflow_permanent_id}/schedules/${item.workflow_schedule_id}/disable`,
        ),
      "paused",
      "pause",
    );
  }

  function handleBulkDuplicate() {
    void runBulkOperation(
      selectedSchedules,
      (client, item) =>
        client.post(`/workflows/${item.workflow_permanent_id}/schedules`, {
          cron_expression: item.cron_expression,
          timezone: item.timezone,
          enabled: item.enabled,
          parameters: item.parameters,
          name: `${item.name ?? item.workflow_title} (copy)`,
        }),
      "duplicated",
      "duplicate",
    );
  }

  function handleBulkDelete() {
    setBulkDeleteDialog({ open: true, count: selected.size });
  }

  async function handleBulkDeleteConfirm() {
    await runBulkOperation(
      selectedSchedules,
      (client, item) =>
        client.delete(
          `/workflows/${item.workflow_permanent_id}/schedules/${item.workflow_schedule_id}`,
        ),
      "deleted",
      "delete",
    );

    setBulkDeleteDialog({ open: false, count: 0 });
  }

  const showCheckbox = schedules.length > 0;
  const columnCount = showCheckbox ? 7 : 6;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl">Schedules</h1>
      </div>

      {/* Search + Filters + Create */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <TableSearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search by agent or schedule name..."
            className="w-64"
          />
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline">
                Filter by Status
                <ChevronDownIcon className="ml-2 size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {STATUS_OPTIONS.map((opt) => (
                <div
                  key={opt.value}
                  className="flex items-center gap-2 p-2 text-sm"
                >
                  <Checkbox
                    id={`schedule-status-${opt.value}`}
                    checked={statusFilters.includes(opt.value)}
                    onCheckedChange={(checked) => {
                      setStatusFilters((prev) =>
                        checked
                          ? [...prev, opt.value]
                          : prev.filter((f) => f !== opt.value),
                      );
                      setPage(1);
                    }}
                  />
                  <label htmlFor={`schedule-status-${opt.value}`}>
                    {opt.label}
                  </label>
                </div>
              ))}
              {statusFilters.length > 0 && (
                <button
                  type="button"
                  className="w-full cursor-pointer p-2 text-left text-sm text-slate-400 hover:text-slate-200"
                  onClick={() => {
                    setStatusFilters([]);
                    setPage(1);
                  }}
                >
                  Clear all
                </button>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)}>
          <PlusIcon className="mr-1.5 size-4" />
          Create Schedule
        </Button>
      </div>

      {/* Table */}
      <div className="space-y-4">
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
                    ariaLabel="Select all schedules"
                  />
                )}
                <TableHead className={showCheckbox ? "w-[28%]" : "w-[31%]"}>
                  Agent
                </TableHead>
                <TableHead className="w-[20%]">Name</TableHead>
                <TableHead className="w-[20%]">Schedule</TableHead>
                <TableHead className="w-[17%]">Next Run</TableHead>
                <TableHead className="w-[7%]">Status</TableHead>
                <TableHead className="w-[5%]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading && (
                <TableRow>
                  <TableCell colSpan={columnCount} className="py-8 text-center">
                    <ReloadIcon className="mx-auto size-5 animate-spin text-slate-400" />
                  </TableCell>
                </TableRow>
              )}
              {isError && (
                <TableRow>
                  <TableCell
                    colSpan={columnCount}
                    className="py-8 text-center text-sm text-red-400"
                  >
                    Failed to load schedules.
                    {errorDetail && (
                      <span className="block text-xs text-slate-500">
                        {errorDetail}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              )}
              {!isLoading && !isError && schedules.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={columnCount}
                    className="py-8 text-center text-sm text-slate-500"
                  >
                    No schedules found.
                  </TableCell>
                </TableRow>
              )}
              {schedules.map((schedule, index) => (
                <TableRow
                  key={schedule.workflow_schedule_id}
                  tabIndex={0}
                  aria-label={`Open schedule ${schedule.name ?? schedule.workflow_title}`}
                  data-state={
                    isSelected(schedule.workflow_schedule_id)
                      ? "selected"
                      : undefined
                  }
                  className="group/row cursor-pointer select-none hover:bg-muted/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-primary"
                  onClick={() =>
                    navigate(
                      `/schedules/${schedule.workflow_permanent_id}/${schedule.workflow_schedule_id}`,
                      { state: { workflowTitle: schedule.workflow_title } },
                    )
                  }
                  onKeyDown={(e) => {
                    if (e.target !== e.currentTarget) return;
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      navigate(
                        `/schedules/${schedule.workflow_permanent_id}/${schedule.workflow_schedule_id}`,
                        { state: { workflowTitle: schedule.workflow_title } },
                      );
                    }
                  }}
                >
                  {showCheckbox && (
                    <SelectionCheckboxCell
                      index={index}
                      checked={isSelected(schedule.workflow_schedule_id)}
                      hasSelection={selected.size > 0}
                      onSelect={handleSelect}
                      ariaLabel={`Select schedule ${schedule.name ?? schedule.workflow_title}`}
                    />
                  )}
                  <TableCell className="truncate font-medium">
                    {schedule.workflow_title}
                  </TableCell>
                  <TableCell className="truncate text-slate-400">
                    {schedule.name ?? "\u2014"}
                  </TableCell>
                  <TableCell className="text-slate-400">
                    <div className="flex items-center gap-1.5">
                      <span className="truncate">
                        {cronToHumanReadable(schedule.cron_expression)}
                      </span>
                      {cronsBelowMinInterval.has(schedule.cron_expression) && (
                        <Tip content="This schedule fires more often than the 5-minute minimum. Saving any change requires updating its cron expression first.">
                          <ExclamationTriangleIcon className="size-3.5 shrink-0 text-amber-400" />
                        </Tip>
                      )}
                    </div>
                  </TableCell>
                  <TableCell className="text-slate-400">
                    {schedule.next_run ? (
                      <span title={basicTimeFormat(schedule.next_run)}>
                        {basicLocalTimeFormat(schedule.next_run)}
                      </span>
                    ) : (
                      "\u2014"
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusDisplay enabled={schedule.enabled} />
                  </TableCell>
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    {/* Row menus yield to the bulk bar while any selection is active. */}
                    {selected.size === 0 && (
                      <DropdownMenu modal={false}>
                        <DropdownMenuTrigger asChild>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-8 text-muted-foreground hover:text-foreground"
                          >
                            <DotsHorizontalIcon className="size-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          {schedule.enabled ? (
                            <DropdownMenuItem
                              onSelect={() => disableMutation.mutate(schedule)}
                            >
                              <PauseIcon className="mr-2 size-4" />
                              Pause
                            </DropdownMenuItem>
                          ) : (
                            <DropdownMenuItem
                              onSelect={() => enableMutation.mutate(schedule)}
                            >
                              <PlayIcon className="mr-2 size-4" />
                              Activate
                            </DropdownMenuItem>
                          )}
                          <DropdownMenuItem
                            onSelect={() => duplicateMutation.mutate(schedule)}
                          >
                            <CopyIcon className="mr-2 size-4" />
                            Duplicate
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            onSelect={() =>
                              setDeleteDialog({ open: true, schedule })
                            }
                            className="text-destructive"
                          >
                            <TrashIcon className="mr-2 size-4" />
                            Delete
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {/* Pagination with Items per page */}
          <div className="grid grid-cols-3 items-center px-4 py-2">
            <div className="flex items-center gap-2">
              <span className="whitespace-nowrap text-sm text-slate-400">
                Items per page
              </span>
              <Select value={String(pageSize)} onValueChange={setPageSize}>
                <SelectTrigger className="w-[65px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PAGE_SIZE_OPTIONS.map((size) => (
                    <SelectItem key={size} value={size}>
                      {size}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="justify-self-center">
              {totalPages > 1 && (
                <Pagination>
                  <PaginationContent>
                    <PaginationItem>
                      <PaginationPrevious
                        onClick={() => setPage(Math.max(1, page - 1))}
                        className={
                          page <= 1
                            ? "pointer-events-none opacity-50"
                            : "cursor-pointer"
                        }
                      />
                    </PaginationItem>
                    {Array.from({ length: totalPages }, (_, i) => i + 1).map(
                      (p) => (
                        <PaginationItem key={p}>
                          <PaginationLink
                            onClick={() => setPage(p)}
                            isActive={p === page}
                            className="cursor-pointer"
                          >
                            {p}
                          </PaginationLink>
                        </PaginationItem>
                      ),
                    )}
                    <PaginationItem>
                      <PaginationNext
                        onClick={() => setPage(Math.min(totalPages, page + 1))}
                        className={
                          page >= totalPages
                            ? "pointer-events-none opacity-50"
                            : "cursor-pointer"
                        }
                      />
                    </PaginationItem>
                  </PaginationContent>
                </Pagination>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Multi-select bulk action bar */}
      {selectedSchedules.length > 0 && (
        <SelectionBar
          count={selectedSchedules.length}
          isOperating={isBulkOperating}
          onClear={clearSelection}
        >
          <Button
            size="sm"
            variant="ghost"
            onClick={handleBulkActivate}
            disabled={isBulkOperating}
          >
            <PlayIcon className="mr-1.5 size-3.5" />
            Activate
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={handleBulkPause}
            disabled={isBulkOperating}
          >
            <PauseIcon className="mr-1.5 size-3.5" />
            Pause
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={handleBulkDuplicate}
            disabled={isBulkOperating}
          >
            <CopyIcon className="mr-1.5 size-3.5" />
            Duplicate
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={handleBulkDelete}
            disabled={isBulkOperating}
          >
            <TrashIcon className="mr-1.5 size-3.5" />
            Delete
          </Button>
        </SelectionBar>
      )}

      {/* Bulk delete confirmation dialog */}
      <Dialog
        open={bulkDeleteDialog.open}
        onOpenChange={(open) => {
          if (!open && !isBulkOperating) {
            setBulkDeleteDialog({ open: false, count: 0 });
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {bulkDeleteDialog.count} Schedules</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete {bulkDeleteDialog.count}{" "}
              {bulkDeleteDialog.count === 1 ? "schedule" : "schedules"}? This
              action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={isBulkOperating}
              onClick={() => setBulkDeleteDialog({ open: false, count: 0 })}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={isBulkOperating}
              onClick={handleBulkDeleteConfirm}
            >
              {isBulkOperating ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation dialog */}
      <Dialog
        open={deleteDialog.open}
        onOpenChange={(open) => {
          if (!open && !deleteMutation.isPending) {
            setDeleteDialog({ open: false, schedule: null });
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Schedule</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this schedule? This action cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteDialog({ open: false, schedule: null })}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={handleDeleteConfirm}
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Create Schedule Dialog */}
      <CreateOrgScheduleDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
      />
    </div>
  );
}

export { SchedulesPage };
