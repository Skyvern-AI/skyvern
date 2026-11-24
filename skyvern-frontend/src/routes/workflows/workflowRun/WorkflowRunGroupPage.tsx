import { useQueries } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getClient } from "@/api/AxiosClient";
import { isForbiddenError } from "@/api/forbidden";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { SimplePagination } from "@/components/ui/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  getActiveOrgQueryKeyScope,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";
import { downloadBlob } from "@/util/downloadBlob";
import { runWithConcurrency } from "@/util/runWithConcurrency";
import { cn } from "@/util/utils";
import {
  OUTCOME_CLASS,
  OUTCOME_LABEL,
  OUTCOME_ORDER,
  STATUS_LABEL,
  UNFINISHED_OUTCOMES,
  plural,
  useGroup,
  type GroupItem,
} from "../ember/cards/runGroup";
import {
  fetchWorkflowRunById,
  getWatchedRunRefetchInterval,
  workflowRunByIdQueryOptions,
} from "../hooks/useWorkflowRunQuery";
import {
  buildRunGroupCsv,
  cellText,
  finalOutput,
  isScrubbedKey,
  safeInputs,
  type FinalOutput,
  type GroupCsvRow,
  type GroupRunRead,
  type OutputState,
} from "./runGroupCsv";

const PAGE_SIZE = 25;
const FETCH_CONCURRENCY = 4;
const EXPORT_READ_TIMEOUT_MS = 30_000;
const OUTPUT_STATE_LABEL: Record<OutputState, string> = {
  recorded: "",
  pending: "Pending",
  unavailable: "Unavailable",
  redacted: "Redacted",
  truncated: "Truncated",
  omitted: "Not readable",
};

// A run the caller may not read, or one retention removed, is listed as omitted instead of failing the export.
function isOmittedRead(error: unknown): boolean {
  const status = isAxiosError(error) ? error.response?.status : undefined;
  return isForbiddenError(error) || status === 404 || status === 410;
}

function hasRun(item: GroupItem): boolean {
  return item.run_status !== null && !isScrubbedKey(item.key);
}

function Values({ values }: { values: Record<string, unknown> | null }) {
  if (values === null) {
    return (
      <span className="text-muted-foreground">
        {OUTPUT_STATE_LABEL.unavailable}
      </span>
    );
  }
  const entries = Object.entries(values);
  if (entries.length === 0) {
    return <span className="text-muted-foreground">None</span>;
  }
  return (
    <dl className="flex max-h-48 flex-col gap-1 overflow-auto">
      {entries.map(([key, value]) => (
        <div key={key} className="min-w-0">
          <dt className="text-xs text-muted-foreground">{key}</dt>
          <dd className="whitespace-pre-wrap break-words font-mono text-xs">
            {cellText(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Output({ output }: { output: FinalOutput }) {
  if (output.reason === "definition_changed") {
    return (
      <span className="text-muted-foreground">
        Unavailable: the workflow was edited after this run
      </span>
    );
  }
  if (output.state === "recorded") {
    return (
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-xs">
        {cellText(output.value)}
      </pre>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      <span className="text-muted-foreground">
        {OUTPUT_STATE_LABEL[output.state]}
      </span>
      {output.value !== undefined ? (
        <span className="break-words font-mono text-xs">
          {cellText(output.value)}
        </span>
      ) : null}
    </div>
  );
}

function WorkflowRunGroupPage() {
  const { groupId } = useParams();
  const query = useGroup(groupId ?? null);
  const group = query.data;
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedPage = Number.parseInt(searchParams.get("page") ?? "1", 10);
  const credentialGetter = useCredentialGetter();
  const scope = getActiveOrgQueryKeyScope(useActiveOrgId());
  const [confirmStop, setConfirmStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [exportError, setExportError] = useState(false);

  const items = group?.items ?? [];
  const pageCount = Math.max(Math.ceil(items.length / PAGE_SIZE), 1);
  const page = Number.isNaN(requestedPage)
    ? 0
    : Math.min(Math.max(requestedPage - 1, 0), pageCount - 1);
  const visible = items.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const runs = useQueries({
    queries: visible.map((item) => ({
      ...workflowRunByIdQueryOptions(
        credentialGetter,
        item.workflow_run_id,
        scope,
      ),
      refetchInterval: (query: {
        state: Parameters<typeof getWatchedRunRefetchInterval>[0];
      }) => getWatchedRunRefetchInterval(query.state),
      refetchOnMount: "always" as const,
      enabled: hasRun(item),
    })),
  });
  const unfinished = items.filter((item) =>
    UNFINISHED_OUTCOMES.has(item.outcome),
  ).length;

  const counts = OUTCOME_ORDER.map(
    (outcome) =>
      [
        outcome,
        items.filter((item) => item.outcome === outcome).length,
      ] as const,
  ).filter(([, count]) => count > 0);

  async function stop() {
    setStopping(true);
    setStopError(null);
    try {
      const client = await getClient(credentialGetter);
      await client.post(`/workflow_run_groups/${groupId}/cancel`);
      await query.refetch();
    } catch (error) {
      setStopError(
        isForbiddenError(error)
          ? "You don't have permission to stop these runs."
          : "Couldn't stop the runs. Try again.",
      );
    } finally {
      setStopping(false);
      setConfirmStop(false);
    }
  }

  async function readRun(
    item: GroupItem,
    stop: AbortSignal,
  ): Promise<GroupRunRead | null> {
    if (!hasRun(item)) {
      return null;
    }
    const controller = new AbortController();
    const abort = () => controller.abort();
    stop.addEventListener("abort", abort);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timedOut = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller.abort();
        reject(new Error("Export read timed out"));
      }, EXPORT_READ_TIMEOUT_MS);
    });
    try {
      return await Promise.race([
        fetchWorkflowRunById(
          credentialGetter,
          item.workflow_run_id,
          controller.signal,
        ),
        timedOut,
      ]);
    } finally {
      clearTimeout(timer);
      stop.removeEventListener("abort", abort);
    }
  }

  // Export reads skip the shared query cache and keep only the CSV row, so each full response is released once
  // its row is built; the first failure that is not an omitted run aborts every other read.
  async function csvRow(
    item: GroupItem,
    stop: AbortController,
  ): Promise<GroupCsvRow> {
    const row = {
      position: item.position,
      outcome: OUTCOME_LABEL[item.outcome],
      workflowRunId: hasRun(item) ? item.workflow_run_id : null,
    };
    try {
      if (stop.signal.aborted) {
        throw new Error("Export stopped");
      }
      const run = await readRun(item, stop.signal);
      return {
        ...row,
        inputs: run ? safeInputs(run) : null,
        output: finalOutput(item, run),
      };
    } catch (error) {
      if (!stop.signal.aborted && isOmittedRead(error)) {
        return { ...row, inputs: null, output: { state: "omitted" } };
      }
      stop.abort();
      throw error;
    }
  }

  async function downloadCsv() {
    if (!group) {
      return;
    }
    const snapshot = group.items;
    setDownloading(true);
    setExportError(false);
    try {
      const stop = new AbortController();
      const results = await runWithConcurrency(
        snapshot.map((item) => () => csvRow(item, stop)),
        FETCH_CONCURRENCY,
      );
      if (stop.signal.aborted) {
        throw new Error("Export read failed");
      }
      const rows = results.flatMap((result) =>
        result.status === "fulfilled" ? [result.value] : [],
      );
      downloadBlob(
        buildRunGroupCsv(rows, window.location.origin),
        `${groupId}.csv`,
        "text/csv;charset=utf-8",
      );
    } catch {
      setExportError(true);
    } finally {
      setDownloading(false);
    }
  }

  if (query.isError && !group) {
    return (
      <div className="p-6 text-rose-700 dark:text-rose-300">
        Couldn't load this run group.
      </div>
    );
  }
  if (!group) {
    return (
      <div className="p-6 text-muted-foreground" role="status">
        Loading…
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 p-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold">Run group</h1>
          <p className="text-sm text-muted-foreground">
            {group.workflow_run_group_id} · {STATUS_LABEL[group.status]} ·{" "}
            {plural(items.length, "run")}
          </p>
          <ul className="flex flex-wrap gap-x-4 text-sm" aria-label="Counts">
            {counts.map(([outcome, count]) => (
              <li
                key={outcome}
                className={OUTCOME_CLASS[outcome] ?? "text-muted-foreground"}
              >
                {count} {OUTCOME_LABEL[outcome]}
              </li>
            ))}
          </ul>
        </div>
        <div className="flex items-center gap-2">
          {group.status === "active" && unfinished > 0 ? (
            <Button
              variant="destructive"
              onClick={() => setConfirmStop(true)}
              disabled={stopping}
            >
              Stop
            </Button>
          ) : null}
          <Button
            variant="secondary"
            onClick={downloadCsv}
            disabled={downloading}
          >
            {downloading ? "Preparing CSV…" : "Download CSV"}
          </Button>
        </div>
      </header>
      {query.isError ? (
        <p className="text-sm text-rose-700 dark:text-rose-300" role="alert">
          Couldn't refresh this run group; showing the last loaded state.
        </p>
      ) : null}
      {stopError ? (
        <p className="text-sm text-rose-700 dark:text-rose-300" role="alert">
          {stopError}
        </p>
      ) : null}
      {exportError ? (
        <div
          className="flex items-center gap-2 text-sm text-rose-700 dark:text-rose-300"
          role="alert"
        >
          Couldn't read every run, so no CSV was made.
          <Button
            variant="secondary"
            size="sm"
            onClick={downloadCsv}
            disabled={downloading}
          >
            Retry
          </Button>
        </div>
      ) : null}
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Row</TableHead>
            <TableHead>Inputs</TableHead>
            <TableHead>Outcome</TableHead>
            <TableHead>Run</TableHead>
            <TableHead>Final output</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {visible.map((item, index) => {
            const run = runs[index];
            const omitted = run?.isError && isOmittedRead(run.error);
            return (
              <TableRow key={item.key} className="align-top">
                <TableCell>{item.position + 1}</TableCell>
                <TableCell className="max-w-xs">
                  {run?.isError ? (
                    <span className="text-muted-foreground">
                      {omitted ? "Not readable" : "Couldn't load"}
                    </span>
                  ) : run?.data ? (
                    <Values values={safeInputs(run.data)} />
                  ) : null}
                </TableCell>
                <TableCell
                  className={cn(
                    OUTCOME_CLASS[item.outcome] ?? "text-muted-foreground",
                  )}
                >
                  {OUTCOME_LABEL[item.outcome]}
                </TableCell>
                <TableCell>
                  {hasRun(item) ? (
                    <Link
                      className="underline underline-offset-2"
                      to={`/runs/${item.workflow_run_id}`}
                    >
                      {item.workflow_run_id}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">Not started</span>
                  )}
                </TableCell>
                <TableCell className="max-w-md">
                  {hasRun(item) && run?.isLoading ? (
                    <span className="text-muted-foreground">Loading…</span>
                  ) : run?.isError && !omitted ? (
                    <span className="text-muted-foreground">Couldn't load</span>
                  ) : (
                    <Output
                      output={
                        omitted
                          ? { state: "omitted" }
                          : finalOutput(item, run?.data)
                      }
                    />
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
      {pageCount > 1 ? (
        <SimplePagination
          page={page + 1}
          hasNext={page + 1 < pageCount}
          onPageChange={(nextPage) =>
            setSearchParams({ page: String(nextPage) })
          }
        />
      ) : null}
      <ConfirmDialog
        open={confirmStop}
        onOpenChange={setConfirmStop}
        title={`Stop ${plural(unfinished, "unfinished run")}?`}
        description="Runs that already finished keep their results."
        confirmLabel="Stop runs"
        isPending={stopping}
        onConfirm={stop}
      />
    </div>
  );
}

export { EXPORT_READ_TIMEOUT_MS, WorkflowRunGroupPage };
