import { ExternalLinkIcon, LockClosedIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

import {
  PendingIcon,
  ProgressSegments,
  StepDot,
  type StepState,
} from "@/components/onboarding/OnboardingProgressBand";
import { Button } from "@/components/ui/button";
import {
  countedTrackItems,
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

type RowLink = { label: string; to: string } | { label: string; href: string };
type RowDefinition = {
  title: string;
  why: string;
  links: RowLink[];
  howItWorks?: string;
};
type RowState = StepState | "locked";
type GettingStartedTrackProps = {
  track: OnboardingTrackV1;
  credentialUnlocked: boolean;
  intent: string | null;
  onAttest: (key: TrackKey) => void;
  onDismiss: () => void;
  onRestore: () => void;
  isPending: boolean;
};

const rowDefinitions: Record<TrackKey, RowDefinition> = {
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

function keepGoingOrder(intent: string | null): TrackKey[] {
  const first = intentFirstRow[intent ?? ""] ?? "first_scheduled_run";
  return [first, ...productionOrder.filter((key) => key !== first)];
}

const quietButtonClassName =
  "h-11 touch-manipulation text-muted-foreground hover:text-foreground";
const quietLinkClassName =
  "inline-flex min-h-11 touch-manipulation items-center text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

function RowLinkButton({
  link,
  primary,
  isPending,
}: {
  link: RowLink;
  primary: boolean;
  isPending: boolean;
}) {
  const variant = isPending ? "disabled" : primary ? "default" : "outline";
  return (
    <Button asChild variant={variant} className="h-11 touch-manipulation">
      {"href" in link ? (
        <a href={link.href} target="_blank" rel="noopener noreferrer">
          {link.label}
          <ExternalLinkIcon aria-hidden="true" className="ml-2 size-4" />
        </a>
      ) : (
        <Link to={link.to}>{link.label}</Link>
      )}
    </Button>
  );
}

function RowBullet({
  state,
  index,
}: {
  state: RowState;
  index: number | null;
}) {
  if (index === null) {
    return (
      <span
        aria-hidden="true"
        className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
          state === "complete"
            ? "bg-badge-success text-foreground"
            : "border border-border text-muted-foreground"
        }`}
      >
        {state === "complete" ? "✓" : "•"}
      </span>
    );
  }
  if (state === "locked") {
    return (
      <span
        aria-hidden="true"
        className="flex size-6 shrink-0 items-center justify-center rounded-full border border-border text-muted-foreground"
      >
        <LockClosedIcon className="size-3" />
      </span>
    );
  }
  return <StepDot state={state} index={index} />;
}

function TrackRow({
  item,
  index,
  state,
  onAttest,
  isPending,
}: {
  item: OnboardingTrackItemV1;
  index: number | null;
  state: RowState;
  onAttest: (key: TrackKey) => void;
  isPending: boolean;
}) {
  const definition = rowDefinitions[item.key];
  const isDone = state === "complete";
  const isLocked = state === "locked";
  return (
    <li className="grid min-h-11 gap-3 rounded-md border border-border/70 bg-background/70 px-4 py-3 text-sm sm:grid-cols-[auto_1fr_auto] sm:items-center">
      <RowBullet state={state} index={index} />
      <div className="min-w-0">
        <p
          className={
            isDone || isLocked
              ? "text-muted-foreground"
              : "font-medium text-foreground"
          }
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
        <p className="text-xs text-muted-foreground">
          {item.completed_at !== null ? (
            <>
              Done{" "}
              <time dateTime={item.completed_at}>
                {new Date(item.completed_at).toLocaleDateString(undefined, {
                  month: "short",
                  day: "numeric",
                })}
              </time>
            </>
          ) : isLocked ? (
            "Available on Hobby and up"
          ) : (
            definition.why
          )}
        </p>
      </div>
      {isDone ? null : isLocked ? (
        <div className="flex items-center sm:justify-end">
          <Button asChild variant="outline" className="h-11 touch-manipulation">
            <Link to="/billing">Upgrade</Link>
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          {definition.links.map((link, linkIndex) => (
            <RowLinkButton
              key={link.label}
              link={link}
              primary={state === "active" && linkIndex === 0}
              isPending={isPending}
            />
          ))}
          {definition.howItWorks ? (
            <a
              href={definition.howItWorks}
              target="_blank"
              rel="noopener noreferrer"
              className={quietLinkClassName}
            >
              How it works
            </a>
          ) : null}
          {SELF_ATTESTED_KEYS.has(item.key) ? (
            <Button
              type="button"
              variant={isPending ? "disabled" : "ghost"}
              className={quietButtonClassName}
              aria-disabled={isPending}
              aria-busy={isPending}
              onClick={() => {
                if (!isPending) onAttest(item.key);
              }}
            >
              {isPending ? <PendingIcon /> : null}
              Mark done
            </Button>
          ) : null}
        </div>
      )}
    </li>
  );
}

function TrackGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-label={label} className="space-y-2">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </h2>
      <ol className="space-y-2">{children}</ol>
    </section>
  );
}

function GettingStartedTrack({
  track,
  credentialUnlocked,
  intent,
  onAttest,
  onDismiss,
  onRestore,
  isPending,
}: GettingStartedTrackProps) {
  const heading = (
    <h1
      id="getting-started-heading"
      className="text-2xl font-semibold tracking-tight"
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
  const keepGoing = rowsFor(keepGoingOrder(intent));
  const community = rowsFor(communityOrder);
  const isLocked = (item: OnboardingTrackItemV1) =>
    item.key === "credential_saved" && !credentialUnlocked;
  const counted = countedTrackItems(track, credentialUnlocked);
  const done = counted.filter((row) => row.completed_at !== null).length;
  const currentKey = keepGoing.find(
    (row) => row.completed_at === null && !isLocked(row),
  )?.key;
  const stateOf = (row: OnboardingTrackItemV1): RowState =>
    row.completed_at !== null
      ? "complete"
      : isLocked(row)
        ? "locked"
        : row.key === currentKey
          ? "active"
          : "upcoming";
  let position = 0;
  const numbered = (row: OnboardingTrackItemV1) =>
    isLocked(row) ? position : position++;

  return (
    <section
      aria-labelledby="getting-started-heading"
      aria-busy={isPending}
      className="mx-auto max-w-2xl space-y-6"
    >
      {isPending ? (
        <span className="sr-only" aria-live="polite">
          Saving your progress…
        </span>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          {heading}
          <p className="mt-1 text-sm text-muted-foreground">
            <span className="sr-only">Progress: </span>
            {done} of {counted.length} complete
          </p>
        </div>
        <Button
          type="button"
          variant={isPending ? "disabled" : "ghost"}
          className={quietButtonClassName}
          aria-disabled={isPending}
          aria-busy={isPending}
          onClick={() => {
            if (!isPending) onDismiss();
          }}
        >
          Hide for now
        </Button>
      </div>
      <ProgressSegments
        completedSteps={done}
        totalSteps={counted.length}
        label="Getting started progress"
      />
      <TrackGroup label="Keep going">
        {keepGoing.map((row) => (
          <TrackRow
            key={row.key}
            item={row}
            index={numbered(row)}
            state={stateOf(row)}
            onAttest={onAttest}
            isPending={isPending}
          />
        ))}
      </TrackGroup>
      <TrackGroup label="Stay in the loop">
        {community.map((row) => (
          <TrackRow
            key={row.key}
            item={row}
            index={null}
            state={row.completed_at !== null ? "complete" : "upcoming"}
            onAttest={onAttest}
            isPending={isPending}
          />
        ))}
      </TrackGroup>
    </section>
  );
}

export { GettingStartedTrack };
export type { GettingStartedTrackProps };
