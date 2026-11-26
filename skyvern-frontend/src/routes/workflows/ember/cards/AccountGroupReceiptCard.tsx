import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import { cn } from "@/util/utils";
import type {
  AccountGroupCancelReview,
  AccountGroupOutcome,
  AccountGroupReview,
  QuestionInteraction,
} from "../emberTypes";
import { useWorkflowRunByIdQuery } from "../../hooks/useWorkflowRunQuery";
import { runIsLogicallyFinal } from "../../workflowRun/runRetryState";
import { RunCostFactList } from "./AccountGroupReviewCard";
import { AttentionMarker } from "./AttentionTray";
import { TONE_CLASSES, type Tone } from "./receiptTone";
import {
  OUTCOME_CLASS,
  OUTCOME_LABEL,
  STATUS_LABEL,
  plural,
  useGroup,
  type Group,
} from "./runGroup";

const COUNTED_OUTCOMES: [AccountGroupOutcome, string][] = [
  ["completed", "completed"],
  ["failed", "failed"],
  ["unknown", "may have acted"],
  ["canceled", "canceled"],
];

function groupMeta(group: Group): string {
  const counts = COUNTED_OUTCOMES.map(([outcome, label]) => {
    const count = group.items.filter((item) => item.outcome === outcome).length;
    return count > 0 ? `${count} ${label}` : null;
  }).filter((part) => part !== null);
  return [STATUS_LABEL[group.status], ...counts].join(" · ");
}

export function Heading({
  tone,
  title,
  meta,
}: {
  tone: Tone;
  title: string;
  meta?: string | null;
}) {
  const classes = TONE_CLASSES[tone];
  return (
    <p className="flex min-w-0 items-start gap-2 leading-[18px]">
      <span
        aria-hidden
        className={cn("mt-[5px] size-2 shrink-0 rounded-full", classes.dot)}
      />
      <span className="min-w-0 break-words">
        <span className={cn("font-semibold", classes.title)}>{title}</span>
        {meta ? <span className="text-muted-foreground"> · {meta}</span> : null}
      </span>
    </p>
  );
}

export function AccountGroupReceiptCard({
  interaction,
  review,
  turnLive,
}: {
  interaction: QuestionInteraction;
  review: AccountGroupReview;
  turnLive: boolean;
}) {
  const query = useGroup(review.workflow_run_group_id);
  const group = query.data;
  const labels = new Map(
    review.rows.map((row) => [row.credential_id, row.label]),
  );
  const decision = review.decision;
  const singleRun = Boolean(review.single_run);
  const rowGroup = Boolean(review.row_group);
  const [showRows, setShowRows] = useState(!rowGroup);
  const reviewNoun = singleRun ? "run" : rowGroup ? "runs" : "account";

  if (interaction.status === "pending") {
    return (
      <div data-interaction-id={interaction.interaction_id}>
        <AttentionMarker
          icon={
            <QuestionMarkCircledIcon
              aria-hidden
              className="size-3.5 shrink-0"
            />
          }
          title={
            singleRun
              ? "Ember asked you to review a run"
              : `Ember asked you to review ${plural(review.rows.length, rowGroup ? "run" : "account")}`
          }
        />
      </div>
    );
  }
  if (!decision?.approved) {
    const interrupted = interaction.status === "interrupted";
    return (
      <div
        data-interaction-id={interaction.interaction_id}
        className={cn(
          "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
          TONE_CLASSES[interrupted ? "error" : "neutral"].card,
        )}
      >
        <Heading
          tone={interrupted ? "error" : "neutral"}
          title={
            interaction.status === "resolved"
              ? `You declined the ${reviewNoun} review. Nothing ran.`
              : `The ${reviewNoun} review ended. Nothing ran.`
          }
        />
        {interrupted ? (
          <p className="pl-4 text-muted-foreground">
            Send a new message to continue.
          </p>
        ) : null}
      </div>
    );
  }
  if (singleRun) {
    return (
      <div
        data-interaction-id={interaction.interaction_id}
        className={cn(
          "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
          TONE_CLASSES.answered.card,
        )}
      >
        <Heading
          tone="answered"
          title={
            review.saves_draft
              ? "You approved saving the draft and one run"
              : "You approved one run"
          }
        />
        {review.saved_version != null ? (
          <p className="pl-4 text-muted-foreground">
            Saved as version {review.saved_version}.
          </p>
        ) : null}
        {review.saves_draft &&
        review.saved_version != null &&
        review.workflow_run_id &&
        !review.run_started ? (
          <p className="pl-4 text-muted-foreground">
            Starting the run raised an error, so it may or may not have started.
          </p>
        ) : null}
        {review.workflow_run_id ? (
          <a
            className="break-words pl-4 underline-offset-2 hover:underline"
            href={`/runs/${review.workflow_run_id}`}
            target="_blank"
            rel="noreferrer"
          >
            {review.workflow_title}
            <span className="sr-only"> (opens in new tab)</span>
          </a>
        ) : turnLive ? (
          <p className="pl-4 text-muted-foreground">
            {review.saves_draft && review.saved_version == null
              ? "Saving the draft and starting the run…"
              : "Starting the run…"}
          </p>
        ) : (
          <p className="pl-4 text-muted-foreground">
            {review.saved_version != null
              ? "The run did not start."
              : "No run is linked to this approval."}
          </p>
        )}
        {review.workflow_run_id ? (
          <SingleRunStatus workflowRunId={review.workflow_run_id} />
        ) : null}
        <RunCostFactList
          cost={review.cost}
          basedOnInputs={review.based_on_inputs}
          runs={review.workflow_run_id && review.run_started !== false ? 1 : 0}
          receipt
          testedRunId={review.tested_workflow_run_id}
          className="pl-4"
        />
      </div>
    );
  }
  const approvedCount = (decision.item_keys ?? decision.credential_ids).length;
  return (
    <div
      data-interaction-id={interaction.interaction_id}
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
        TONE_CLASSES.answered.card,
      )}
    >
      <Heading
        tone="answered"
        title={`You approved ${plural(approvedCount, rowGroup ? "run" : "account run")}`}
        meta={group ? groupMeta(group) : null}
      />
      {group && rowGroup ? (
        <button
          type="button"
          className="self-start pl-4 underline underline-offset-2"
          aria-expanded={showRows}
          onClick={() => setShowRows((open) => !open)}
        >
          {showRows ? "Hide runs" : `Show ${plural(group.items.length, "run")}`}
        </button>
      ) : null}
      {group && showRows ? (
        <ul className="flex flex-col gap-0.5 pl-4">
          {group.items.map((item) => (
            <li
              key={item.key}
              className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-2"
            >
              {item.run_status === null ? (
                <span className="break-words">
                  {labels.get(item.key) ?? item.key}
                </span>
              ) : (
                <a
                  className="break-words underline-offset-2 hover:underline"
                  href={`/runs/${item.workflow_run_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {labels.get(item.key) ?? item.key}
                  <span className="sr-only"> (opens in new tab)</span>
                </a>
              )}
              <span
                className={cn(
                  "shrink-0",
                  OUTCOME_CLASS[item.outcome] ?? "text-muted-foreground",
                )}
              >
                {OUTCOME_LABEL[item.outcome]}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {review.workflow_run_group_id ? (
        <a
          className="self-start pl-4 underline underline-offset-2"
          href={`/run-groups/${review.workflow_run_group_id}`}
          target="_blank"
          rel="noreferrer"
        >
          Open results
          <span className="sr-only"> (opens in new tab)</span>
        </a>
      ) : null}
      {group && group.status !== "finished" ? (
        <p className="pl-4 text-muted-foreground">
          The chat's Stop button does not stop these runs. To stop them, ask
          Ember to cancel the group.
        </p>
      ) : null}
      {query.isError ? (
        <p className="pl-4 text-rose-700 dark:text-rose-300">
          Couldn't load run status.
        </p>
      ) : null}
      {review.workflow_run_group_id ? null : turnLive ? (
        <p className="pl-4 text-muted-foreground">Starting the runs…</p>
      ) : (
        <p className="pl-4 text-muted-foreground">
          No run group is linked to this approval. Ember's reply says whether
          the runs started.
        </p>
      )}
      <RunCostFactList
        cost={review.cost}
        basedOnInputs={review.based_on_inputs}
        runs={review.workflow_run_group_id ? approvedCount : 0}
        receipt
        className="pl-4"
      />
    </div>
  );
}

function SingleRunStatus({ workflowRunId }: { workflowRunId: string }) {
  const { data: run } = useWorkflowRunByIdQuery(workflowRunId, true);
  return (
    <>
      {run ? (
        <p className="pl-4 text-muted-foreground">Status: {run.status}</p>
      ) : null}
      {run && runIsLogicallyFinal(run) ? null : (
        <p className="pl-4 text-muted-foreground">
          The chat's Stop button does not stop this run. To stop it, cancel it
          from its run page.
        </p>
      )}
    </>
  );
}

export function AccountGroupCancelReceipt({
  interaction,
  review,
}: {
  interaction: QuestionInteraction;
  review: AccountGroupCancelReview;
}) {
  const count = review.unfinished_count ?? review.unfinished_rows.length;
  const approved = review.decision?.approved;
  const group = useGroup(approved ? review.workflow_run_group_id : null).data;
  if (interaction.status === "pending") {
    return (
      <div data-interaction-id={interaction.interaction_id}>
        <AttentionMarker
          icon={
            <QuestionMarkCircledIcon
              aria-hidden
              className="size-3.5 shrink-0"
            />
          }
          title={`Ember asked whether to stop ${plural(count, "run")}`}
        />
      </div>
    );
  }
  const interrupted = interaction.status === "interrupted";
  return (
    <div
      data-interaction-id={interaction.interaction_id}
      className={cn(
        "flex flex-col gap-1.5 rounded-lg border px-3 py-[11px] text-xs",
        TONE_CLASSES[interrupted ? "error" : "neutral"].card,
      )}
    >
      <Heading
        tone={interrupted ? "error" : "neutral"}
        title={
          approved
            ? `You asked to stop ${plural(count, "unfinished run")}`
            : interaction.status === "resolved"
              ? "You kept the runs going. Nothing was canceled."
              : "The cancel review ended. Nothing was canceled."
        }
        meta={group ? groupMeta(group) : null}
      />
      {interrupted ? (
        <p className="pl-4 text-muted-foreground">
          Send a new message to continue.
        </p>
      ) : null}
    </div>
  );
}
