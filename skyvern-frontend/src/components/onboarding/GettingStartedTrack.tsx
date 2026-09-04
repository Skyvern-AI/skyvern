import {
  ArrowRightIcon,
  CheckIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  LockClosedIcon,
} from "@radix-ui/react-icons";
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  countedTrackItems,
  SECOND_AGENT_KEY,
  SELF_ATTESTED_KEYS,
  type OnboardingTrackItemV1,
  type OnboardingTrackV1,
  type TrackKey,
} from "@/routes/root/useOnboardingTrack";
import {
  DISCORD_INVITE_URL,
  DOCS_API_QUICKSTART_URL,
  DOCS_CREDENTIALS_URL,
  DOCS_MCP_URL,
  DOCS_SCHEDULING_URL,
  GITHUB_REPO_URL,
  LINKEDIN_URL,
  X_URL,
} from "@/util/externalLinks";

type FirstAgentKey =
  | "account_created"
  | "first_agent_created"
  | "first_successful_run";
type FirstAgentProgress = {
  first_agent_created: string | null;
  first_successful_run: string | null;
};
type RowLink = { label: string; to: string } | { label: string; href: string };
type RowDefinition = {
  title: string;
  why: string;
  links: RowLink[];
  howItWorks?: string;
};
type RowState = "complete" | "active" | "upcoming" | "locked";
type GettingStartedTrackProps = {
  track: OnboardingTrackV1;
  credentialUnlocked: boolean;
  intent: string | null;
  progress: FirstAgentProgress | null;
  onAttest: (key: TrackKey) => void;
  onRestore: () => void;
  isPending: boolean;
  rowDetails?: Partial<Record<TrackKey, ReactNode>>;
};

const firstAgentSteps: ReadonlyArray<{
  key: FirstAgentKey;
  title: string;
  action?: RowLink;
  howItWorks?: string;
}> = [
  { key: "account_created", title: "Account created" },
  {
    key: "first_agent_created",
    title: "Create your first agent",
    action: {
      label: "Describe your first agent",
      to: "/discover?focus=prompt",
    },
    howItWorks:
      "https://www.skyvern.com/docs/cloud/building-agents/build-an-agent",
  },
  {
    key: "first_successful_run",
    title: "Run your first agent",
    action: { label: "Run agent", to: "/agents" },
    howItWorks:
      "https://www.skyvern.com/docs/cloud/building-agents/run-an-agent",
  },
];

const rowDefinitions: Record<TrackKey, RowDefinition> = {
  second_agent_run: {
    title: "Run a second agent",
    why: "You finished your first agent. Complete one more before the offer ends to earn your bonus.",
    links: [{ label: "Run another agent", to: "/agents" }],
  },
  first_scheduled_run: {
    title: "Run your agent automatically",
    why: "Set a schedule so it runs without you",
    links: [{ label: "Open schedules", to: "/schedules" }],
    howItWorks: DOCS_SCHEDULING_URL,
  },
  first_api_run: {
    title: "Run an agent from your own code",
    why: "Start a run with an API key from any script",
    links: [{ label: "Get an API key", to: "/settings#api-keys" }],
    howItWorks: DOCS_API_QUICKSTART_URL,
  },
  mcp_installed: {
    title: "Use Skyvern from Claude, Cursor, or ChatGPT",
    why: "Install the MCP server and run agents from your AI tools",
    links: [{ label: "Set up MCP", href: DOCS_MCP_URL }],
  },
  teammate_invited: {
    title: "Bring a teammate",
    why: "Share agents and runs with your team",
    links: [{ label: "Open settings", to: "/settings" }],
  },
  credential_saved: {
    title: "Let your agent log in for you",
    why: "Save a login and 2FA code once; your agents reuse it",
    links: [{ label: "Add a credential", to: "/credentials" }],
    howItWorks: DOCS_CREDENTIALS_URL,
  },
  github_starred: {
    title: "Star Skyvern on GitHub",
    why: "Skyvern is open source",
    links: [{ label: "Star on GitHub", href: GITHUB_REPO_URL }],
  },
  discord_joined: {
    title: "Join the Discord",
    why: "Get help from the team and other builders",
    links: [{ label: "Join Discord", href: DISCORD_INVITE_URL }],
  },
  social_followed: {
    title: "Follow for updates",
    why: "Product updates as they ship",
    links: [
      { label: "Follow on X", href: X_URL },
      { label: "Follow on LinkedIn", href: LINKEDIN_URL },
    ],
  },
};

const productionOrder: readonly TrackKey[] = [
  "first_scheduled_run",
  "first_api_run",
  "mcp_installed",
  "credential_saved",
];
const intentFirstRow: Record<string, TrackKey> = {
  fill_forms: "credential_saved",
  extract_data: "first_api_run",
  monitor_website: "first_scheduled_run",
};
const communityOrder: readonly TrackKey[] = [
  "github_starred",
  "discord_joined",
  "social_followed",
];

const rowClassName =
  "grid grid-cols-[22px_1fr] items-start gap-3.5 border-b border-neutral-200 px-1 py-3 last:border-b-0 dark:border-white/[0.06] sm:grid-cols-[22px_minmax(0,1fr)_auto] sm:items-center";
const titleClassName =
  "text-sm font-medium leading-5 text-neutral-900 dark:text-neutral-50";
const mutedTitleClassName =
  "text-sm font-medium leading-5 text-neutral-500 dark:text-neutral-400";
const descriptionClassName =
  "mt-px text-xs leading-4 text-neutral-600 dark:text-neutral-400";
const mutedDescriptionClassName =
  "mt-px text-xs leading-4 text-neutral-500 dark:text-neutral-400";
const howItWorksClassName =
  "rounded-sm text-neutral-500 underline decoration-neutral-300 underline-offset-[3px] hover:text-neutral-700 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring dark:text-neutral-400 dark:decoration-white/[0.18] dark:hover:text-neutral-200";
const actionBase =
  "inline-flex h-8 shrink-0 items-center rounded-md text-[13px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring sm:whitespace-nowrap";
const filledAction = `${actionBase} gap-1.5 bg-cta px-3 font-semibold text-cta-foreground shadow-[0_1px_2px_rgba(0,0,0,0.3)] hover:bg-cta-hover`;
const quietAction = `${actionBase} gap-[5px] px-1.5 font-medium text-neutral-700 hover:text-neutral-950 dark:text-neutral-300 dark:hover:text-neutral-50`;
const upgradeAction = `${actionBase} gap-[5px] px-1.5 font-medium text-sky-700 hover:text-sky-800 dark:text-sky-300 dark:hover:text-sky-200`;

function keepGoingOrder(intent: string | null): TrackKey[] {
  const first = intentFirstRow[intent ?? ""] ?? "first_scheduled_run";
  return [first, ...productionOrder.filter((key) => key !== first)];
}

function formatDate(completedAt: string): string {
  return new Date(completedAt).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function ProgressBar({ done, total }: { done: number; total: number }) {
  return (
    <div
      role="progressbar"
      aria-label="Getting started progress"
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      className="flex w-full gap-1"
    >
      {Array.from({ length: total }, (_, index) => (
        <span
          key={index}
          aria-hidden="true"
          className={`h-[3px] flex-1 rounded-full ${
            index < done
              ? "bg-sky-600 dark:bg-sky-400"
              : index === done
                ? "bg-sky-600/35 dark:bg-sky-400/35"
                : "bg-neutral-200 dark:bg-white/10"
          }`}
        />
      ))}
    </div>
  );
}

type TrackBadgeProps =
  | { kind: "num" | "next"; number: number }
  | { kind: "done" | "lock" }
  | {
      kind: "ring";
      title: string;
      isPending: boolean;
      onAttest: () => void;
    };

function TrackBadge(props: TrackBadgeProps) {
  const base =
    "mt-px flex size-[22px] shrink-0 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums sm:mt-0";
  if (props.kind === "done") {
    return (
      <span aria-hidden="true" className={`${base} bg-green-600 text-white`}>
        <CheckIcon className="size-3.5" />
      </span>
    );
  }
  if (props.kind === "lock") {
    return (
      <span
        aria-hidden="true"
        className={`${base} border border-neutral-300 text-neutral-500 dark:border-white/10`}
      >
        <LockClosedIcon className="size-3" />
      </span>
    );
  }
  if (props.kind === "ring") {
    return (
      <button
        type="button"
        aria-label={`Mark ${props.title} done`}
        aria-disabled={props.isPending}
        aria-busy={props.isPending}
        className="-m-[11px] flex size-11 cursor-pointer touch-manipulation items-center justify-center rounded-full focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring sm:-m-[5px] sm:size-8 hover:[&>span]:border-neutral-700 dark:hover:[&>span]:border-white/60"
        onClick={() => {
          if (!props.isPending) props.onAttest();
        }}
      >
        <span
          aria-hidden="true"
          className={`${base} border border-neutral-500 text-neutral-500 dark:border-white/40 ${
            props.isPending
              ? "animate-pulse opacity-60 motion-reduce:animate-none"
              : ""
          }`}
        />
      </button>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={`${base} border ${
        props.kind === "next"
          ? "border-sky-500 text-sky-700 shadow-[0_0_0_3px_rgba(56,189,248,0.12)] dark:border-sky-400 dark:text-sky-300"
          : "border-neutral-300 text-neutral-600 dark:border-white/10 dark:text-neutral-400"
      }`}
    >
      {"number" in props ? props.number : null}
    </span>
  );
}

function TrackAction({ link, primary }: { link: RowLink; primary: boolean }) {
  const className = primary ? filledAction : quietAction;
  const iconClassName = primary
    ? "size-3.5"
    : "size-3.5 text-neutral-500 dark:text-neutral-400";
  return "href" in link ? (
    <a
      href={link.href}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
    >
      {link.label}
      <ExternalLinkIcon aria-hidden="true" className={iconClassName} />
      <span className="sr-only"> (opens in new tab)</span>
    </a>
  ) : (
    <Link to={link.to} className={className}>
      {link.label}
      <ArrowRightIcon aria-hidden="true" className={iconClassName} />
    </Link>
  );
}

function RowBadge({
  item,
  index,
  state,
  isPending,
  onAttest,
}: {
  item: OnboardingTrackItemV1;
  index: number | null;
  state: RowState;
  isPending: boolean;
  onAttest: (key: TrackKey) => void;
}) {
  if (state === "complete") return <TrackBadge kind="done" />;
  if (state === "locked") return <TrackBadge kind="lock" />;
  if (SELF_ATTESTED_KEYS.has(item.key)) {
    return (
      <TrackBadge
        kind="ring"
        title={rowDefinitions[item.key].title}
        isPending={isPending}
        onAttest={() => onAttest(item.key)}
      />
    );
  }
  return (
    <TrackBadge
      kind={state === "active" ? "next" : "num"}
      number={index ?? 0}
    />
  );
}

function FirstAgentRow({
  step,
  index,
  state,
  completedAt,
  primary,
}: {
  step: (typeof firstAgentSteps)[number];
  index: number;
  state: Exclude<RowState, "locked">;
  completedAt: string | null;
  primary: boolean;
}) {
  const isDone = state === "complete";
  return (
    <li className={rowClassName}>
      {isDone ? (
        <TrackBadge kind="done" />
      ) : (
        <TrackBadge kind={state === "active" ? "next" : "num"} number={index} />
      )}
      <div className="min-w-0">
        <p className={isDone ? mutedTitleClassName : titleClassName}>
          <span className="sr-only">
            {isDone
              ? "Complete: "
              : state === "active"
                ? "Current step: "
                : "Upcoming step: "}
          </span>
          {step.title}
        </p>
        {isDone && completedAt ? (
          <p className={mutedDescriptionClassName}>
            Done <time dateTime={completedAt}>{formatDate(completedAt)}</time>
          </p>
        ) : !isDone && step.howItWorks ? (
          <p className={descriptionClassName}>
            <a
              href={step.howItWorks}
              target="_blank"
              rel="noopener noreferrer"
              className={howItWorksClassName}
            >
              How it works
              <span className="sr-only"> (opens in new tab)</span>
            </a>
          </p>
        ) : null}
      </div>
      {!isDone && step.action ? (
        <div className="col-start-2 flex flex-wrap items-center sm:col-start-auto sm:justify-end">
          <TrackAction link={step.action} primary={primary} />
        </div>
      ) : null}
    </li>
  );
}

function TrackRow({
  item,
  index,
  state,
  primary,
  onAttest,
  isPending,
  detail,
}: {
  item: OnboardingTrackItemV1;
  index: number | null;
  state: RowState;
  primary: boolean;
  onAttest: (key: TrackKey) => void;
  isPending: boolean;
  detail?: ReactNode;
}) {
  const definition = rowDefinitions[item.key];
  const isDone = state === "complete";
  const isLocked = state === "locked";
  return (
    <li className={rowClassName}>
      <RowBadge
        item={item}
        index={index}
        state={state}
        isPending={isPending}
        onAttest={onAttest}
      />
      <div className="min-w-0">
        <p
          className={isDone || isLocked ? mutedTitleClassName : titleClassName}
        >
          <span className="sr-only">
            {isDone
              ? "Complete: "
              : isLocked
                ? "Locked: "
                : state === "active"
                  ? "Current step: "
                  : "Upcoming step: "}
          </span>
          {definition.title}
        </p>
        <p
          className={
            isDone || isLocked
              ? mutedDescriptionClassName
              : descriptionClassName
          }
        >
          {item.completed_at !== null ? (
            <>
              Done{" "}
              <time dateTime={item.completed_at}>
                {formatDate(item.completed_at)}
              </time>
            </>
          ) : isLocked ? (
            "Available on Hobby and up"
          ) : (
            <>
              {definition.why}
              {definition.howItWorks ? (
                <>
                  {" "}
                  <a
                    href={definition.howItWorks}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={howItWorksClassName}
                  >
                    How it works
                    <span className="sr-only"> (opens in new tab)</span>
                  </a>
                </>
              ) : null}
            </>
          )}
        </p>
        {detail}
      </div>
      {isDone ? null : isLocked ? (
        <div className="col-start-2 flex items-center sm:col-start-auto sm:justify-end">
          <Link to="/billing" className={upgradeAction}>
            Upgrade
            <ArrowRightIcon aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
      ) : (
        <div className="col-start-2 flex flex-wrap items-center gap-1 sm:col-start-auto sm:justify-end">
          {definition.links.map((link, linkIndex) => (
            <TrackAction
              key={link.label}
              link={link}
              primary={primary && linkIndex === 0}
            />
          ))}
        </div>
      )}
    </li>
  );
}

function TrackGroup({
  label,
  meta,
  children,
}: {
  label: string;
  meta: string;
  children: ReactNode;
}) {
  return (
    <section aria-label={label}>
      <div className="flex items-baseline justify-between pb-1">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.06em] text-neutral-500 dark:text-neutral-400">
          {label}
        </h2>
        <span className="text-xs text-neutral-500 dark:text-neutral-400">
          {meta}
        </span>
      </div>
      {children}
    </section>
  );
}

function GettingStartedTrack({
  track,
  credentialUnlocked,
  intent,
  progress,
  onAttest,
  onRestore,
  isPending,
  rowDetails,
}: GettingStartedTrackProps) {
  const [firstAgentExpanded, setFirstAgentExpanded] = useState(false);
  const heading = (
    <h1
      id="getting-started-heading"
      className="text-2xl font-semibold tracking-[-0.02em]"
    >
      Getting started
    </h1>
  );

  if (track.state === "dismissed") {
    return (
      <section
        aria-labelledby="getting-started-heading"
        className="mx-auto max-w-2xl space-y-4"
      >
        {heading}
        <p className="text-sm text-muted-foreground">
          Hidden for now. Resume to see your next steps in the sidebar.
        </p>
        <Button
          type="button"
          variant={isPending ? "disabled" : "outline"}
          className="h-11 touch-manipulation"
          aria-disabled={isPending}
          aria-busy={isPending}
          onClick={() => {
            if (!isPending) onRestore();
          }}
        >
          Resume
        </Button>
      </section>
    );
  }

  const byKey = new Map(track.items.map((item) => [item.key, item]));
  const rowsFor = (keys: readonly TrackKey[]) =>
    keys.flatMap((key) => byKey.get(key) ?? []);
  const orderedKeepGoing = rowsFor([
    SECOND_AGENT_KEY,
    ...keepGoingOrder(intent),
  ]);
  const keepGoing = credentialUnlocked
    ? orderedKeepGoing
    : [
        ...orderedKeepGoing.filter((row) => row.key !== "credential_saved"),
        ...orderedKeepGoing.filter((row) => row.key === "credential_saved"),
      ];
  const community = rowsFor(communityOrder);
  const isLocked = (item: OnboardingTrackItemV1) =>
    item.key === "credential_saved" && !credentialUnlocked;
  const counted = countedTrackItems(track, credentialUnlocked);
  // Null progress = first-agent data unavailable: omit the group, count track rows only.
  const hasProgress = progress !== null;
  const firstAgentCreatedAt = progress?.first_agent_created ?? null;
  const firstSuccessfulRunAt = progress?.first_successful_run ?? null;
  const firstAgentCreated = firstAgentCreatedAt !== null;
  const firstSuccessfulRun = firstSuccessfulRunAt !== null;
  const firstAgentComplete = firstAgentCreated && firstSuccessfulRun;
  const firstAgentCurrentKey: FirstAgentKey | null = !hasProgress
    ? null
    : !firstAgentCreated
      ? "first_agent_created"
      : !firstSuccessfulRun
        ? "first_successful_run"
        : null;
  const keepGoingCurrentKey = keepGoing.find(
    (row) => row.completed_at === null && !isLocked(row),
  )?.key;
  const primaryKey: FirstAgentKey | TrackKey | null =
    firstAgentCurrentKey ?? keepGoingCurrentKey ?? null;
  const countedDone = counted.filter((row) => row.completed_at !== null).length;
  const done = hasProgress
    ? 1 + Number(firstAgentCreated) + Number(firstSuccessfulRun) + countedDone
    : countedDone;
  const total = hasProgress ? 3 + counted.length : counted.length;
  const keepGoingComplete = keepGoing.every((row) => row.completed_at !== null);
  const stateOf = (row: OnboardingTrackItemV1): RowState =>
    row.completed_at !== null
      ? "complete"
      : isLocked(row)
        ? "locked"
        : row.key === primaryKey
          ? "active"
          : "upcoming";
  let position = 0;

  return (
    <section
      aria-labelledby="getting-started-heading"
      aria-busy={isPending}
      className="mx-auto max-w-2xl space-y-7"
    >
      <span className="sr-only" aria-live="polite">
        {isPending ? "Saving your progress…" : null}
      </span>
      <header className="space-y-3">
        <div className="flex items-baseline justify-between gap-4">
          {heading}
          <p className="text-[13px] tabular-nums text-muted-foreground">
            <span className="sr-only">Progress: </span>
            {done} of {total} done
          </p>
        </div>
        <ProgressBar done={done} total={total} />
      </header>

      {hasProgress ? (
        <TrackGroup
          label="First agent"
          meta={firstAgentComplete && !firstAgentExpanded ? "Done" : "3 steps"}
        >
          {firstAgentComplete ? (
            <div className="grid grid-cols-[22px_1fr_auto] items-start gap-3.5 border-b border-neutral-200 px-1 py-2.5 dark:border-white/[0.06] sm:items-center">
              <TrackBadge kind="done" />
              <p className="min-w-0">
                <span className="text-sm font-medium text-neutral-700 dark:text-neutral-200">
                  First agent ready
                </span>
                <span className="text-xs text-neutral-500 dark:text-neutral-400">
                  {" "}
                  · Account, first agent, first run ·{" "}
                  {firstSuccessfulRunAt
                    ? formatDate(firstSuccessfulRunAt)
                    : null}
                </span>
              </p>
              <button
                type="button"
                aria-expanded={firstAgentExpanded}
                aria-controls="first-agent-steps"
                aria-label={`${firstAgentExpanded ? "Hide" : "Show"} first agent steps`}
                className="-my-1.5 flex size-11 touch-manipulation items-center justify-center rounded-md text-neutral-500 hover:text-neutral-700 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring dark:text-neutral-400 dark:hover:text-neutral-200 sm:my-0 sm:size-8"
                onClick={() => setFirstAgentExpanded((expanded) => !expanded)}
              >
                <ChevronRightIcon
                  aria-hidden="true"
                  className={`size-4 transition-transform motion-reduce:transition-none ${
                    firstAgentExpanded ? "rotate-90" : ""
                  }`}
                />
              </button>
            </div>
          ) : null}
          <ol
            id="first-agent-steps"
            hidden={firstAgentComplete && !firstAgentExpanded}
          >
            {!firstAgentComplete || firstAgentExpanded
              ? firstAgentSteps.map((step, index) => {
                  const completedAt =
                    step.key === "account_created"
                      ? null
                      : (progress?.[step.key] ?? null);
                  const stepComplete =
                    step.key === "account_created" || completedAt !== null;
                  const state: Exclude<RowState, "locked"> = stepComplete
                    ? "complete"
                    : step.key === firstAgentCurrentKey
                      ? "active"
                      : "upcoming";
                  return (
                    <FirstAgentRow
                      key={step.key}
                      step={step}
                      index={index + 1}
                      state={state}
                      completedAt={completedAt}
                      primary={step.key === primaryKey}
                    />
                  );
                })
              : null}
          </ol>
        </TrackGroup>
      ) : null}

      <TrackGroup
        label="Keep going"
        meta={keepGoingComplete ? "Done" : `${counted.length} steps`}
      >
        <ol>
          {keepGoing.map((row) => {
            const index = isLocked(row) ? null : ++position;
            return (
              <TrackRow
                key={row.key}
                item={row}
                index={index}
                state={stateOf(row)}
                primary={row.key === primaryKey}
                onAttest={onAttest}
                isPending={isPending}
                detail={rowDetails?.[row.key]}
              />
            );
          })}
        </ol>
      </TrackGroup>

      <TrackGroup label="Stay in the loop" meta="Optional">
        <ol>
          {community.map((row) => (
            <TrackRow
              key={row.key}
              item={row}
              index={null}
              state={row.completed_at !== null ? "complete" : "upcoming"}
              primary={false}
              onAttest={onAttest}
              isPending={isPending}
              detail={rowDetails?.[row.key]}
            />
          ))}
        </ol>
      </TrackGroup>
    </section>
  );
}

export { GettingStartedTrack };
export type { GettingStartedTrackProps };
