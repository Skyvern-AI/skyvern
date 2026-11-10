import { ExternalLinkIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import { Status } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { workflowRunDetailPath } from "../../studioNavigation";
import { runIsRetryWaiting } from "../../workflowRun/runRetryState";
import type { AppNode } from "../nodes";
import { announceDoc } from "./docAnnounce";
import { docResultAge } from "./docRunState";
import { docStepCaption, docStepTitle } from "./docStepRegistry";
import type { useDocRun } from "./useDocRun";

type DocRun = ReturnType<typeof useDocRun>;

const STATUS_WORDS: Record<Status, string> = {
  [Status.Created]: "Queued",
  [Status.Queued]: "Queued",
  [Status.Running]: "Running",
  [Status.Completed]: "Completed",
  [Status.Failed]: "Failed",
  [Status.Terminated]: "Terminated",
  [Status.Canceled]: "Canceled",
  [Status.TimedOut]: "Timed out",
  [Status.Skipped]: "Skipped",
  [Status.Paused]: "Paused",
};

const shortRunId = (id: string) =>
  id.length > 12 ? `${id.slice(0, 3)}…${id.slice(-6)}` : id;

/** Which run the step results come from: its id, status, age, and which version it ran. */
export function DocRunLine({
  docRun,
  numbers,
  byId,
}: {
  docRun: DocRun;
  numbers: ReadonlyMap<string, string>;
  byId: ReadonlyMap<string, AppNode>;
}) {
  const { run, runId, live } = docRun;
  if (!run || !runId) return null;
  const nodeOf = (label: string) =>
    [...byId.values()].find((node) => node.data.label === label);
  const stepName = (label: string) => {
    const node = nodeOf(label);
    if (!node) return label;
    const number = numbers.get(node.id);
    const name = docStepCaption(node) ?? docStepTitle(node);
    return number ? `step ${number} ${name}` : name;
  };
  // A loop and the step inside it both run; the newest row is the innermost step.
  const running = live
    ? [...docRun.blocksByLabel.values()]
        .filter((block) => block.status === Status.Running && block.label)
        .sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
    : undefined;
  const ended = !live;
  const at = ended
    ? (run.finished_at ?? run.started_at ?? run.created_at)
    : (run.started_at ?? run.created_at);
  const age = typeof at === "string" ? docResultAge(at, Date.now()) : null;
  const step = docRun.blockRunTarget ? stepName(docRun.blockRunTarget) : null;
  const now = running?.label ? stepName(running.label) : null;
  const statusWord = runIsRetryWaiting(run)
    ? "Retry pending"
    : (STATUS_WORDS[run.status] ?? run.status);
  const status = [statusWord, step ?? now].filter(Boolean).join(" · ");
  const ranVersion = run.workflow.version;
  const { currentVersion } = docRun;
  const version =
    currentVersion === undefined
      ? null
      : ranVersion === currentVersion
        ? "this version"
        : `${ranVersion < currentVersion ? "an earlier" : "a newer"} version, v${ranVersion}`;
  const rest = [age && (ended ? age : `started ${age}`), version].filter(
    Boolean,
  );
  return (
    <div
      className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted-foreground"
      data-testid="doc-run-line"
    >
      <span>
        {docRun.blockRun ? "Step run" : "Run"}{" "}
        <span className="font-mono text-[12px] text-foreground" title={runId}>
          {shortRunId(runId)}
        </span>
        {" · "}
        <span aria-live="polite">{status}</span>
        {rest.map((part) => ` · ${part}`)}
      </span>
      <CopyButton
        value={runId}
        ariaLabel="Copy run id"
        onCopied={() => announceDoc("Run id copied")}
        className="size-6"
      />
      <Link
        to={workflowRunDetailPath(runId)}
        aria-label={`Open run ${runId}`}
        className="inline-flex items-center gap-1 text-xs underline-offset-2 outline-none hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring"
      >
        Open run
        <ExternalLinkIcon className="size-3" />
      </Link>
    </div>
  );
}
