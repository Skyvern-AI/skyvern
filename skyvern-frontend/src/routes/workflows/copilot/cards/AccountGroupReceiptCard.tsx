import { QuestionMarkCircledIcon } from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import type {
  AccountGroupCancelReview,
  AccountGroupOutcome,
  AccountGroupReview,
  QuestionInteraction,
} from "../workflowCopilotTypes";
import { AttentionMarker } from "./AttentionTray";
import { TONE_CLASSES, type Tone } from "./receiptTone";

const POLL_MS = 5000;

type GroupStatus = "active" | "cancel_requested" | "finished";

interface GroupItem {
  key: string;
  workflow_run_id: string;
  run_status: string | null;
  outcome: AccountGroupOutcome;
}

interface Group {
  status: GroupStatus;
  items: GroupItem[];
}

const OUTCOME_LABEL: Record<AccountGroupOutcome, string> = {
  pending: "Waiting",
  in_progress: "Running",
  completed: "Completed",
  failed: "Failed before acting",
  unknown: "Unknown, may have acted",
  canceled: "Canceled",
};

const OUTCOME_CLASS: Partial<Record<AccountGroupOutcome, string>> = {
  failed: "text-rose-700 dark:text-rose-300",
  unknown: "text-amber-700 dark:text-yellow-400",
};

const STATUS_LABEL: Record<GroupStatus, string> = {
  active: "running",
  cancel_requested: "stopping",
  finished: "finished",
};

const COUNTED_OUTCOMES: [AccountGroupOutcome, string][] = [
  ["completed", "completed"],
  ["failed", "failed"],
  ["unknown", "may have acted"],
  ["canceled", "canceled"],
];

function plural(count: number, noun: string): string {
  return count === 1 ? `1 ${noun}` : `${count} ${noun}s`;
}

function groupMeta(group: Group): string {
  const counts = COUNTED_OUTCOMES.map(([outcome, label]) => {
    const count = group.items.filter((item) => item.outcome === outcome).length;
    return count > 0 ? `${count} ${label}` : null;
  }).filter((part) => part !== null);
  return [STATUS_LABEL[group.status], ...counts].join(" · ");
}

function isClientError(error: unknown): boolean {
  const status = isAxiosError(error) ? error.response?.status : undefined;
  return status !== undefined && status >= 400 && status < 500;
}

function useGroup(groupId: string | null) {
  const credentialGetter = useCredentialGetter();
  return useQuery<Group>({
    queryKey: ["workflowRunGroup", groupId],
    enabled: groupId !== null,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<Group>(
        `/workflow_run_groups/${groupId}`,
      );
      return response.data;
    },
    retry: (failureCount, error) => !isClientError(error) && failureCount < 3,
    refetchInterval: (query) =>
      query.state.data?.status === "finished" ||
      isClientError(query.state.error)
        ? false
        : POLL_MS,
  });
}

function Heading({
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

  if (interaction.status === "pending") {
    return (
      <div data-interaction-id={interaction.interaction_id}>
        <AttentionMarker
          icon={
            <QuestionMarkCircledIcon
              aria-hidden
              className="size-3.5 shrink-0 text-amber-500"
            />
          }
          title={`Copilot asked you to review ${plural(review.rows.length, "account")}`}
          hint="answer below"
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
              ? "You declined the account review. Nothing ran."
              : "The account review ended. Nothing ran."
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
        title={`You approved ${plural(decision.credential_ids.length, "account run")}`}
        meta={group ? groupMeta(group) : null}
      />
      {group ? (
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
      {group && group.status !== "finished" ? (
        <p className="pl-4 text-muted-foreground">
          The chat's Stop button does not stop these runs. To stop them, ask
          Copilot to cancel the group.
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
          No run group is linked to this approval. Copilot's reply says whether
          the runs started.
        </p>
      )}
    </div>
  );
}

export function AccountGroupCancelReceipt({
  interaction,
  review,
}: {
  interaction: QuestionInteraction;
  review: AccountGroupCancelReview;
}) {
  const count = review.unfinished_rows.length;
  const approved = review.decision?.approved;
  const group = useGroup(approved ? review.workflow_run_group_id : null).data;
  if (interaction.status === "pending") {
    return (
      <div data-interaction-id={interaction.interaction_id}>
        <AttentionMarker
          icon={
            <QuestionMarkCircledIcon
              aria-hidden
              className="size-3.5 shrink-0 text-amber-500"
            />
          }
          title={`Copilot asked whether to stop ${plural(count, "account run")}`}
          hint="answer below"
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
            ? `You asked to stop ${plural(count, "unfinished account run")}`
            : interaction.status === "resolved"
              ? "You kept the account runs going. Nothing was canceled."
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
