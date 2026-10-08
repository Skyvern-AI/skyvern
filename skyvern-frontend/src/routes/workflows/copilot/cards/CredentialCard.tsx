import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { ChevronDownIcon, LockClosedIcon } from "@radix-ui/react-icons";
import { useDebounce } from "use-debounce";

import { getClient } from "@/api/AxiosClient";
import { isPasswordCredential, type CredentialApiResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  isCredentialNotFoundError,
  useCredentialQuery,
} from "@/routes/workflows/hooks/useCredentialQuery";

import {
  AppliedCheck,
  CardBody,
  CardFooter,
  CardHeader,
  CopilotCard,
  GutterRow,
} from "./cardChrome";
import {
  AttentionMarker,
  AttentionTray,
  type AttentionTrayPresentation,
} from "./AttentionTray";
import { TURN_ROW_INSET } from "./cardLayout";
import { useDemoLoginGuide } from "@/hooks/useDemoLoginGuide";

// Union of the request-policy reason tokens and the run-derived pause reasons. `type`/`reason` are the
// only fields a minimal signal guarantees; the rest is populated only by a timed pause signal.
export type CredentialRequiredReason =
  | "workflow_credential_inputs_unbound"
  | "credential_name_unresolved"
  | "credential_invention_requested"
  | "raw_secret"
  | "credential_deferred_draft"
  | "assistant_directed"
  | "missing_credential_run_failure"
  | "credential_missing_totp"
  | "credential_rejected_by_site"
  | "credential_registration";

// What Generate and save would store. Never carries a password; the server generates it.
export interface CredentialRegistrationDetails {
  username: string;
  credential_name: string;
  attempted?: boolean;
  outcome?: "rejected" | "unknown" | "created_not_connected" | null;
}

export interface CredentialRequiredFrame {
  type: "credential_required";
  reason: CredentialRequiredReason;
  turn_id?: string;
  workflow_copilot_chat_id?: string;
  // Dynamic ask text; only a richer, timed signal supplies one today.
  message?: string;
  login_page_urls?: string[];
  credential_refs?: string[];
  timeout_seconds?: number;
  expires_at?: string;
  signing_in?: boolean;
  registration?: CredentialRegistrationDetails | null;
  timestamp?: string;
}

export type CredentialCardMode = "terminal" | "inline-pause" | "auto-bound";

export type CredentialPauseOutcome =
  | "connected"
  | "skipped"
  | "timeout"
  | "signed_in";

export interface CredentialPauseHistorical {
  outcome: CredentialPauseOutcome;
  credentialId?: string;
  // Captured at connect time so the receipt names the credential without a lookup.
  name?: string;
}

// A picker row: id/name plus a secondary discriminator (the login username) so two credentials
// saved under the same name stay distinguishable.
interface PickerCredential {
  credentialId: string;
  name: string;
  secondary?: string;
}

type OrgCredentialList =
  | { status: "loading" }
  | { status: "error" }
  // forSearch is the term these rows answer, so the picker can tell whether they still answer the
  // one in the box. Offering rows fetched for an earlier term lets Enter submit an unrelated login.
  | { status: "ready"; credentials: PickerCredential[]; forSearch: string };

export interface CredentialCardProps {
  frame: CredentialRequiredFrame;
  mode: CredentialCardMode;
  resolvedOutcome?: CredentialPauseHistorical;
  // undefined = primary CTA (wiring opens the add-credential modal); a string
  // id = the user picked an already-stored credential. name rides along so the
  // receipt shows it and the resume/continue references it without a lookup.
  onConnect: (credentialId?: string, name?: string) => void;
  onSkip: () => void;
  // Opens the credential editor on the saved record an update ask names.
  onUpdateCredential?: (credential: CredentialApiResponse) => void;
  // Terminal connect auto-sends a "continue" turn; the receipt says so instead
  // of the plain "added". Defaults false so inline-pause and every other caller
  // keep the existing copy.
  continued?: boolean;
  // Bumped by the parent when a credential is created so the one-shot fetch re-runs and the new
  // credential shows up in the picker (e.g. if the resume POST failed and the ask is still live).
  reloadKey?: number;
  // "auto-bound" mode only: the credential the turn silently bound, named in the receipt.
  autoBound?: { credentialId: string; name: string };
  // "auto-bound" mode only: whether the Change affordance is live (the tail turn). A scrollback
  // receipt stays read-only so a pick can't optimistically resolve without an actual continuation.
  canChange?: boolean;
  // Set when the chat docks this ask above the composer instead of in the transcript.
  tray?: AttentionTrayPresentation;
  // Offered only when the card's browser is the one on screen, so the user signs in where the agent looks.
  signIn?: ManualSignInOffer;
  // Registration cards only: saves a server-generated password under the frame's username and name.
  onGenerate?: () => void;
}

export interface ManualSignInOffer {
  busy: boolean;
  // Set after a Done that found no sign-in cookies for the site.
  notFoundHost?: string;
  // Set after a Done whose sign-in could not be read or saved.
  saveFailed?: boolean;
  onStart: () => void;
  onDone: () => void;
}

const SIGN_IN_WHY_LINE =
  "So it can sign in on your behalf when this workflow runs — stored encrypted, never shown in chat.";

// Exported for reuse elsewhere instead of re-deriving reason copy.
// eslint-disable-next-line react-refresh/only-export-components
export const CREDENTIAL_WHY_LINE_BY_REASON: Record<
  CredentialRequiredReason,
  string
> = {
  workflow_credential_inputs_unbound: SIGN_IN_WHY_LINE,
  // Neutral reason when no more specific typed cause applies; older stored chats also carry it.
  assistant_directed: SIGN_IN_WHY_LINE,
  credential_name_unresolved:
    "I couldn't tell which saved credential you meant — connect or pick the right one so the workflow can sign in.",
  credential_invention_requested:
    "I can't invent login details — connect a real credential so the workflow can sign in safely.",
  raw_secret:
    "Don't paste secrets directly in chat — connect a credential so it's stored encrypted instead.",
  missing_credential_run_failure:
    "The last run stopped here because no credential was available — connect one so it can sign in automatically next time.",
  credential_deferred_draft:
    "You held off on this earlier — connect a credential now so the workflow can sign in when it runs.",
  credential_missing_totp:
    "This saved login has no 2FA method, so the workflow can't pass the verification step. Add one in the credential editor — codes never go through chat.",
  credential_rejected_by_site:
    "Update the saved password or one-time code here and I'll try the sign-in again.",
  credential_registration:
    "Generate and save creates a strong password and stores it in your credentials — it never appears in chat. Saving it does not create the account; I'll still submit the sign-up form.",
};

// Mirrors the credentials route: it caps `search` at 200 characters and pages at 100. A longer term
// is rejected outright, leaving a Retry that can never succeed.
const CREDENTIAL_SEARCH_MAX = 200;
const CREDENTIAL_PAGE_SIZE = 100;

// The box itself is capped, not just the term sent to the route: searching a silent prefix of a
// longer visible term offers rows that its hidden characters exclude. Counted in code points, as
// the route's length check is; slicing UTF-16 units can halve an emoji, which Axios cannot encode.
function clampSearch(term: string): string {
  return Array.from(term).slice(0, CREDENTIAL_SEARCH_MAX).join("");
}

// Applied to both the request and every comparison against it — trimming one side only would leave
// forSearch and the live term permanently unequal on a padded query.
function normalizeSearch(term: string): string {
  return term.trim();
}

const LIVE_REGION_SELECTOR =
  '[aria-live]:not([aria-live="off"]), [role="status"], [role="log"], [role="alert"]';
const SEARCH_FAILED_ANNOUNCEMENT = "Couldn't run that search.";

// Title and meta fit the one-line receipt; detail is the full sentence behind its chevron.
const SKIP_OUTCOME = {
  title: "Sign-in skipped",
  meta: "test may stop at login",
  detail: "Credential setup skipped — test run may stop at the login step.",
};
const UPDATE_SKIP_OUTCOME = {
  title: "Credential not updated",
  meta: "keeps its saved sign-in",
  detail: "Credential not updated — the workflow keeps its saved sign-in.",
};
const SIGNED_IN_DETAIL =
  "No password stored · your sign-in is saved as browser cookies in this profile";
const TIMEOUT_OUTCOME = {
  title: "Sign-in request timed out",
  meta: "test may stop at login",
  detail: "Credential request timed out — test run may stop at the login step.",
};

function siteFromLoginPageUrls(urls: string[] | undefined): string {
  const first = urls?.[0];
  if (!first) {
    return "the site";
  }
  try {
    return new URL(first).origin;
  } catch {
    return first;
  }
}

function formatCountdown(remainingMs: number): string {
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

// A minute-rounded phrase for the screen-reader announcement: its text only
// changes once a minute, so aria-live fires once a minute, not every second.
// Derives minutes the same way formatCountdown does (floor of whole seconds)
// so the two can never disagree at a minute boundary.
function formatCountdownAnnouncement(remainingMs: number): string {
  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 1) {
    return "Less than a minute left to connect";
  }
  return minutes === 1
    ? "1 minute left to connect"
    : `${minutes} minutes left to connect`;
}

// A missing OR malformed expires_at both fail safe as already-expired rather
// than leaving the card permanently enabled with a "NaN:NaN" countdown. This
// intentionally has no separate loading state: entering inline-pause mode
// before expires_at is populated renders "Timed out" rather than a spinner.
function computeRemainingMs(expiresAt: string): number {
  const ms = Date.parse(expiresAt) - Date.now();
  return Number.isNaN(ms) ? 0 : ms;
}

function useCountdown(expiresAt: string, active: boolean) {
  const [remainingMs, setRemainingMs] = useState(() =>
    computeRemainingMs(expiresAt),
  );

  useEffect(() => {
    if (!active) {
      return;
    }
    setRemainingMs(computeRemainingMs(expiresAt));
    const id = setInterval(() => {
      const next = computeRemainingMs(expiresAt);
      setRemainingMs(next);
      // Stop ticking once expired instead of re-rendering every second forever.
      if (next <= 0) {
        clearInterval(id);
      }
    }, 1000);
    return () => clearInterval(id);
  }, [expiresAt, active]);

  return { remainingMs, expired: remainingMs <= 0 };
}

// Searchable picker over the whole org credential list. Built on the cmdk primitives (same pattern as
// the workflow-builder CredentialCombobox) rather than importing it, since that one pulls react-query
// and the copilot chat's test harness has no QueryClientProvider.
function CredentialPicker({
  credentials,
  suggestedIds,
  disabled,
  onPick,
  search,
  onSearchChange,
  searchFailed,
  searchPending,
  resultsTruncated,
  onRetrySearch,
  triggerLabel = "Saved logins",
  triggerClassName = "gap-1",
  // Popover content matches the trigger width by default; a content-width trigger (e.g. "Change")
  // needs an explicit width here or the dropdown collapses to the label and hides its search/rows.
  contentClassName = "w-[var(--radix-popover-trigger-width)] min-w-[240px]",
}: {
  credentials: PickerCredential[];
  // Copilot's suggested candidates for this sign-in (only inline-pause frames carry them; terminal
  // asks deliberately don't stamp payload candidates, so they show a plain list).
  suggestedIds?: string[];
  disabled?: boolean;
  onPick: (credentialId: string, name: string) => void;
  // The org list is one unpaginated page, so searching it reaches the server and cmdk's own filter
  // is off: it would re-filter a server result on a narrower field set and hide matched rows.
  search: string;
  onSearchChange: (search: string) => void;
  // The rows below are the previous query's result when a search fails, so the list says so instead
  // of letting them stand as the answer.
  searchFailed: boolean;
  // True while the rows on hand answer an earlier term than the one in the box.
  searchPending: boolean;
  // The response filled a whole page, so there may be more matches than are listed.
  resultsTruncated: boolean;
  onRetrySearch: () => void;
  triggerLabel?: string;
  triggerClassName?: string;
  contentClassName?: string;
}) {
  const [open, setOpen] = useState(false);
  const suggestedSet = new Set(suggestedIds ?? []);
  const suggested = credentials.filter((c) => suggestedSet.has(c.credentialId));
  const rest = credentials.filter((c) => !suggestedSet.has(c.credentialId));
  const renderItem = (credential: PickerCredential) => (
    <CommandItem
      key={credential.credentialId}
      // cmdk filters on value; append username + id so identical names stay findable.
      value={`${credential.name} ${credential.secondary ?? ""} ${credential.credentialId}`}
      onSelect={() => {
        // A pick after the pause expires would submit an already-rejected resume token; the popover
        // closes on `disabled` above, this guards a click already in flight.
        if (disabled) return;
        setOpen(false);
        onPick(credential.credentialId, credential.name);
      }}
    >
      <div className="flex min-w-0 flex-col">
        <span className="truncate">{credential.name}</span>
        {credential.secondary ? (
          <span className="truncate text-[11px] text-muted-foreground">
            {credential.secondary}
          </span>
        ) : null}
      </div>
    </CommandItem>
  );
  return (
    <Popover
      open={open && !disabled}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        // Closing drops the term, as CredentialCombobox does. A zero-match term left behind keeps
        // the card in search mode, and on the auto-bound receipt that hides its only route to
        // adding a credential.
        if (!nextOpen) {
          onSearchChange("");
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button
          type="button"
          role="combobox"
          aria-expanded={open}
          variant="outline"
          size="sm"
          disabled={disabled}
          className={triggerClassName}
        >
          <span className="truncate">{triggerLabel}</span>
          <ChevronDownIcon className="size-4 shrink-0 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className={`${contentClassName} p-0`} align="start">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Search credentials..."
            value={search}
            onValueChange={onSearchChange}
          />
          <CommandList>
            {searchFailed ? (
              <div className="px-2 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
                Couldn&apos;t run that search.{" "}
                <button
                  type="button"
                  onClick={onRetrySearch}
                  className="font-medium text-foreground underline"
                >
                  Retry
                </button>
              </div>
            ) : searchPending ? (
              <div className="px-2 py-1.5 text-[11px] text-muted-foreground">
                Searching…
              </div>
            ) : (
              <CommandEmpty>No credentials found.</CommandEmpty>
            )}
            {resultsTruncated && !searchPending && !searchFailed ? (
              <div className="px-2 py-1.5 text-[11px] leading-relaxed text-muted-foreground">
                Showing the first {CREDENTIAL_PAGE_SIZE}. Narrow the search if
                yours isn&apos;t listed.
              </div>
            ) : null}
            {suggested.length > 0 ? (
              // Copilot's suggestion(s) pinned first; cmdk highlights the first item, so one Enter
              // accepts it. The full org list stays below to override.
              <CommandGroup heading="Suggested">
                {suggested.map(renderItem)}
              </CommandGroup>
            ) : null}
            <CommandGroup
              heading={suggested.length > 0 ? "All credentials" : undefined}
            >
              {rest.map(renderItem)}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

// Read from the page rather than inferred from `mode`: an inline pause restored after a reload
// renders outside the chat's live region, so the mode alone cannot say whether one surrounds us.
function useInsideLiveRegion() {
  const rootRef = useRef<HTMLDivElement>(null);
  const [insideLiveRegion, setInsideLiveRegion] = useState(false);
  useLayoutEffect(() => {
    setInsideLiveRegion(
      Boolean(rootRef.current?.parentElement?.closest(LIVE_REGION_SELECTOR)),
    );
  }, []);
  return [rootRef, insideLiveRegion] as const;
}

function PauseCountdown({
  remainingMs,
  expired,
}: {
  remainingMs: number;
  expired: boolean;
}) {
  return (
    <>
      <span
        aria-hidden="true"
        className="flex-none text-[11px] tabular-nums text-muted-foreground"
      >
        {expired ? "Timed out" : formatCountdown(remainingMs)}
      </span>
      {/* Separate from the visible per-second display: this text only
          changes once a minute, so screen readers aren't spammed. */}
      <span className="sr-only" aria-live="polite">
        {expired ? "Timed out" : formatCountdownAnnouncement(remainingMs)}
      </span>
    </>
  );
}

function SkipButton({
  onSkip,
  disabled,
}: {
  onSkip: () => void;
  disabled: boolean;
}) {
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      aria-label="Skip for now"
      onClick={() => onSkip()}
      disabled={disabled}
    >
      Skip
    </Button>
  );
}

// Reasons whose card asks to fix the one credential the turn already chose.
// eslint-disable-next-line react-refresh/only-export-components
export const UPDATE_ASK_REASONS: readonly string[] = [
  "credential_missing_totp",
  "credential_rejected_by_site",
];

// Asks to fix the one credential already in use, so it offers no picker and no way to add another.
function CredentialUpdateAsk({
  frame,
  credentialId,
  onUpdateCredential,
  onSkip,
  tray,
}: {
  frame: CredentialRequiredFrame;
  credentialId: string;
  onUpdateCredential?: (credential: CredentialApiResponse) => void;
  onSkip: () => void;
  tray?: AttentionTrayPresentation;
}) {
  const { remainingMs, expired } = useCountdown(frame.expires_at ?? "", true);
  const [rootRef, insideLiveRegion] = useInsideLiveRegion();
  const credentialQuery = useCredentialQuery(credentialId, { retry: false });
  const credential = credentialQuery.data;
  const site = siteFromLoginPageUrls(frame.login_page_urls);
  const rejected = frame.reason === "credential_rejected_by_site";
  const notFound =
    credentialQuery.isError && isCredentialNotFoundError(credentialQuery.error);
  const loadFailure = credentialQuery.isError
    ? notFound
      ? "This saved login no longer exists."
      : "Couldn't load this saved login."
    : null;
  const status = credential ? "" : (loadFailure ?? "Loading saved login…");

  return (
    <AskChrome
      tray={tray}
      rootRef={rootRef}
      message={frame.message}
      title={
        rejected
          ? `Update ${credential ? `'${credential.name}'` : "your saved login"} to sign in to ${site}`
          : credential
            ? `Add 2FA to '${credential.name}' to sign in to ${site}`
            : `Add 2FA to your saved login for ${site}`
      }
      countdown={<PauseCountdown remainingMs={remainingMs} expired={expired} />}
      lines={[
        <span
          key="why"
          className="text-[11px] leading-relaxed text-muted-foreground"
        >
          {CREDENTIAL_WHY_LINE_BY_REASON[frame.reason]}
        </span>,
      ]}
      footer={
        <>
          <Button
            type="button"
            size="sm"
            disabled={expired || !credential || !onUpdateCredential}
            onClick={() => credential && onUpdateCredential?.(credential)}
          >
            {rejected ? "Update credential" : "Add 2FA method"}
          </Button>
          {status ? (
            <span className="min-w-0 text-xs text-muted-foreground">
              {status}
            </span>
          ) : null}
          {loadFailure && !notFound ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={expired}
              onClick={() => void credentialQuery.refetch()}
            >
              Retry
            </Button>
          ) : null}
          <div className="ml-auto">
            <SkipButton onSkip={onSkip} disabled={expired} />
          </div>
        </>
      }
      announcer={
        // Mounted for the card's lifetime so only its text changes; inside an outer live region
        // the visible status is already announced.
        <span
          className="sr-only"
          role={insideLiveRegion ? undefined : "status"}
        >
          {insideLiveRegion ? "" : status}
        </span>
      }
    />
  );
}

// One unresolved ask, drawn either as a transcript card or as the tray docked above the composer.
// The tray leaves the assistant's words to the transcript marker, which sits where the ask was raised.
function AskChrome({
  tray,
  rootRef,
  message,
  title,
  countdown,
  lines,
  footer,
  announcer,
}: {
  tray?: AttentionTrayPresentation;
  rootRef: RefObject<HTMLDivElement>;
  message?: string;
  title: string;
  countdown: ReactNode;
  lines: ReactNode[];
  footer: ReactNode;
  // Outside the collapsible body, so a minimized tray still announces a failure.
  announcer: ReactNode;
}) {
  if (tray) {
    return (
      <div ref={rootRef}>
        <AttentionTray
          aria-label="Sign-in request"
          title={title}
          wrapTitle
          meta={countdown}
          collapsedTitle="Copilot needs to sign in"
          collapsedMeta={countdown}
          collapsed={tray.collapsed}
          onCollapsedChange={tray.onCollapsedChange}
          minimizeLabel="Minimize sign-in request"
          upNext={tray.upNext}
        >
          <div className="flex min-h-0 flex-col gap-1 overflow-y-auto px-3 pb-2.5 pt-1">
            {lines.map((line, index) => (
              <div key={index}>{line}</div>
            ))}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border px-3 py-1.5">
            {footer}
          </div>
        </AttentionTray>
        {announcer}
      </div>
    );
  }
  return (
    <div ref={rootRef}>
      <AskMessage message={message} />
      <CopilotCard>
        <CardHeader
          icon={<LockClosedIcon className="h-3.5 w-3.5 text-warning" />}
          wrapTitle
          title={title}
          right={countdown}
        />
        <CardBody>
          {lines.map((line, index) => (
            <GutterRow key={index}>{line}</GutterRow>
          ))}
        </CardBody>
        <CardFooter className="flex flex-wrap items-center gap-2">
          {footer}
        </CardFooter>
      </CopilotCard>
      {announcer}
    </div>
  );
}

// Where a docked ask was raised: the assistant's words for it, then a pointer to the tray.
export function CredentialAskMarker({ message }: { message?: string }) {
  return (
    <div>
      <AskMessage message={message} />
      <AttentionMarker
        icon={<LockClosedIcon aria-hidden className="size-3.5 shrink-0" />}
        title="Copilot needs to sign in"
      />
    </div>
  );
}

// The assistant's own words for the ask, set on the turn's text edge rather than the card's.
function AskMessage({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <p
      className={`${TURN_ROW_INSET} pb-2 text-sm leading-relaxed text-foreground`}
    >
      {message}
    </p>
  );
}

function ResolvedCredentialCard({
  tone,
  title,
  meta,
  detail,
}: {
  tone: "done" | "warn";
  title: string;
  meta?: string;
  detail?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <CopilotCard>
      <CardHeader
        icon={
          tone === "warn" ? (
            <span
              aria-hidden="true"
              className="text-xs font-bold text-amber-700 dark:text-amber-300"
            >
              !
            </span>
          ) : (
            <AppliedCheck />
          )
        }
        title={title}
        meta={meta}
        expanded={expanded}
        onToggle={detail ? () => setExpanded((value) => !value) : undefined}
      />
      {expanded && detail ? (
        <CardBody>
          <GutterRow>
            <span className="text-[11px] leading-relaxed text-muted-foreground">
              {detail}
            </span>
          </GutterRow>
        </CardBody>
      ) : null}
    </CopilotCard>
  );
}

export function CredentialCard(props: Readonly<CredentialCardProps>) {
  const updateTargetId = UPDATE_ASK_REASONS.includes(props.frame.reason)
    ? props.frame.credential_refs?.[0]
    : undefined;
  if (
    updateTargetId &&
    props.mode === "inline-pause" &&
    !props.resolvedOutcome
  ) {
    return (
      <CredentialUpdateAsk
        frame={props.frame}
        credentialId={updateTargetId}
        onUpdateCredential={props.onUpdateCredential}
        onSkip={props.onSkip}
        tray={props.tray}
      />
    );
  }
  return <CredentialAskCard {...props} />;
}

function CredentialAskCard({
  frame,
  mode,
  resolvedOutcome,
  onConnect,
  onSkip,
  continued = false,
  reloadKey,
  autoBound,
  canChange = false,
  tray,
  signIn,
  onGenerate,
}: Readonly<CredentialCardProps>) {
  // Terminal mode never expires by design: its signal carries no timeout/expiry
  // semantics at all, so there is nothing to compare "now" against. Only a
  // richer, timed pause signal has real expiry data, hence gating disablement
  // on inline-pause exclusively.
  const countdownActive = mode === "inline-pause" && !resolvedOutcome;
  const { remainingMs, expired } = useCountdown(
    frame.expires_at ?? "",
    countdownActive,
  );
  const disabled = countdownActive && expired;
  useDemoLoginGuide(
    frame.login_page_urls?.[0],
    !resolvedOutcome && mode !== "auto-bound" && !disabled,
  );

  const credentialGetter = useCredentialGetter();
  const [orgCredentials, setOrgCredentials] = useState<OrgCredentialList>({
    status: "loading",
  });
  const [retryKey, setRetryKey] = useState(0);
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 300);
  // The route builds its ILIKE pattern from the literal value, so " Acme " matches no credential
  // named "Acme" and an all-space term matches almost nothing. Trimmed once here and used for the
  // request, the fetch dependency and forSearch alike, matching CredentialCombobox.
  const searchTerm = normalizeSearch(debouncedSearch);
  // Which term's fetch failed, rather than a bare "a search failed": clearing the box searches for
  // the empty term, and a boolean keyed on a non-empty term reads that failure as no failure, which
  // leaves the rows permanently out of step with the box and the picker gone with no way back.
  const [failedSearch, setFailedSearch] = useState<string | null>(null);
  const [rootRef, insideLiveRegion] = useInsideLiveRegion();
  const fetchGeneration = useRef(0);
  // Any credential ask (terminal or inline-pause) lists every org login credential so the user can
  // pick or create. credential_type=password since the card only ever asks for a sign-in; the API
  // returns them most-recent-first (created_at desc), used as-is. Direct getClient fetch, not
  // useCredentialsQuery: the copilot chat's test harness has no QueryClientProvider. Re-fetches when
  // the parent bumps reloadKey (a credential was just created) so the new one appears in the picker.
  // page_size 100 with no pagination, so the picker's search goes to the route's `search` param
  // rather than filtering the page locally: a >100-credential org would otherwise get a confident
  // empty result for a login it owns.
  const isAsk = !resolvedOutcome;
  // An auto-bound receipt only needs the org list when its Change affordance is live; a read-only
  // scrollback receipt must not each fire its own 100-credential fetch.
  const needsCredentialList = isAsk && (mode !== "auto-bound" || canChange);
  useEffect(() => {
    if (!needsCredentialList) return;
    // Generation guard: a fetch superseded by a reloadKey change (or a slow one) must not set state
    // after a newer fetch has started, so a stale response can't overwrite the current list.
    const generation = ++fetchGeneration.current;
    void (async () => {
      try {
        const client = await getClient(credentialGetter);
        const res = await client.get<CredentialApiResponse[]>("/credentials", {
          params: {
            page: 1,
            page_size: CREDENTIAL_PAGE_SIZE,
            credential_type: "password",
            ...(searchTerm ? { search: searchTerm } : {}),
          },
        });
        if (fetchGeneration.current !== generation) return;
        setFailedSearch(null);
        setOrgCredentials({
          status: "ready",
          forSearch: searchTerm,
          credentials: (Array.isArray(res.data) ? res.data : []).map(
            (credential) => ({
              credentialId: credential.credential_id,
              name: credential.name,
              secondary:
                credential.credential &&
                isPasswordCredential(credential.credential)
                  ? credential.credential.username
                  : undefined,
            }),
          ),
        });
      } catch (error) {
        if (fetchGeneration.current === generation) {
          // A failed re-fetch (reloadKey bumped, canChange flipped, a search) must keep a list the
          // user can already pick from; only a fetch with nothing to show becomes the error state.
          setOrgCredentials((current) =>
            current.status === "ready" ? current : { status: "error" },
          );
          setFailedSearch(searchTerm);
          // Log only the message: an AxiosError serializes its request config into the console.
          console.error(
            "Failed to load credentials:",
            error instanceof Error ? error.message : String(error),
          );
        }
      }
    })();
  }, [needsCredentialList, reloadKey, retryKey, searchTerm, credentialGetter]);

  const retryCredentialList = () => {
    setOrgCredentials({ status: "loading" });
    setRetryKey((key) => key + 1);
  };
  // Re-runs the current term without dropping to "loading": that would unmount the picker the user
  // is typing in, along with the term itself.
  const retrySearch = () => setRetryKey((key) => key + 1);
  const changeSearch = (term: string) => setSearch(clampSearch(term));

  // The picker offers only rows that answer the term now in the box. A row fetched for an earlier
  // term is offered neither while the next request is in flight (cmdk highlights the first row, so
  // Enter would submit an unrelated login) nor after a failed one.
  const readyList =
    orgCredentials.status === "ready" ? orgCredentials.credentials : [];
  const searchPending =
    orgCredentials.status === "ready" &&
    orgCredentials.forSearch !== normalizeSearch(search);
  // Only while the box still holds the failed term (typing past it is a new search, not a standing
  // complaint about an abandoned one) AND the rows do not already answer it: a re-fetch that failed
  // over rows already matching the box left nothing stale, so it stays a log line.
  const searchFailed =
    searchPending &&
    failedSearch !== null &&
    failedSearch === normalizeSearch(search);
  const pickable = searchPending ? [] : readyList;
  // A full page means the route had more to give, so the list says so rather than reading as the
  // complete set of matches.
  const resultsTruncated = pickable.length >= CREDENTIAL_PAGE_SIZE;
  // Mounted whenever the picker is mid-search, not only when it has rows: unmounting on an empty
  // interim result closes the dropdown and drops the user's focus mid-edit.
  const pickerEngaged = Boolean(search) || searchPending || searchFailed;
  const retryButton = (
    <Button
      type="button"
      size="sm"
      variant="outline"
      disabled={disabled}
      onClick={retryCredentialList}
    >
      Retry
    </Button>
  );

  if (resolvedOutcome) {
    switch (resolvedOutcome.outcome) {
      case "skipped":
        return (
          <ResolvedCredentialCard
            tone="warn"
            {...(frame.reason === "credential_rejected_by_site"
              ? UPDATE_SKIP_OUTCOME
              : SKIP_OUTCOME)}
          />
        );
      case "timeout":
        return frame.registration?.outcome === "created_not_connected" ? (
          <ResolvedCredentialCard
            tone="warn"
            title={`Saved as ${frame.registration.credential_name}, not connected`}
            detail="The request ended before this login was connected. It is on the Credentials page."
          />
        ) : (
          <ResolvedCredentialCard tone="warn" {...TIMEOUT_OUTCOME} />
        );
      case "signed_in":
        return (
          <ResolvedCredentialCard
            tone="done"
            title={
              resolvedOutcome.name
                ? `Signed in, saved as '${resolvedOutcome.name}'`
                : "Signed in, saved as a browser profile"
            }
            detail={SIGNED_IN_DETAIL}
          />
        );
      case "connected": {
        const name = resolvedOutcome.name;
        // A save from the editor does not prove 2FA was added; the retried step says whether it was.
        const heading = continued
          ? name
            ? `Continuing with '${name}'…`
            : "Continuing…"
          : frame.reason === "credential_missing_totp"
            ? `Saved ${name ? `'${name}'` : "the credential"}, retrying the verification step`
            : frame.reason === "credential_rejected_by_site"
              ? `Saved ${name ? `'${name}'` : "the credential"}, signing in again`
              : name
                ? `Credential '${name}' added`
                : "Credential added";
        return (
          <ResolvedCredentialCard
            tone="done"
            title={heading}
            detail="Stored encrypted · used to sign in on your behalf · never enters the chat"
          />
        );
      }
      default: {
        // Compile-time exhaustiveness guard: a future CredentialPauseOutcome
        // value fails to typecheck here. Renders a fallback instead of
        // throwing since `resolvedOutcome` will eventually come off the
        // network, where a crash would take down the whole chat pane.
        const _exhaustive: never = resolvedOutcome.outcome;
        void _exhaustive;
        return (
          <ResolvedCredentialCard
            tone="warn"
            title="Credential status unavailable"
          />
        );
      }
    }
  }

  if (mode === "auto-bound" && autoBound) {
    // Change re-picks only OTHER credentials (re-picking the bound one fires a redundant continuation).
    // When none are pickable — still loading, or the bound one is the org's only credential — fall back
    // to adding a new credential; a failed fetch also offers Retry so other saved logins stay reachable.
    const others = pickable.filter(
      (credential) => credential.credentialId !== autoBound.credentialId,
    );
    return (
      <div ref={rootRef}>
        <AutoBoundReceipt
          name={autoBound.name}
          canChange={canChange}
          change={
            canChange ? (
              // An active search stays mounted on zero results: unmounting the picker would discard
              // the term the user is still typing and read as "you own nothing by that name".
              others.length > 0 || pickerEngaged ? (
                <CredentialPicker
                  credentials={others}
                  onPick={(credentialId, name) => onConnect(credentialId, name)}
                  search={search}
                  onSearchChange={changeSearch}
                  searchFailed={searchFailed}
                  searchPending={searchPending}
                  resultsTruncated={resultsTruncated}
                  onRetrySearch={retrySearch}
                  triggerLabel="Change"
                  contentClassName="w-[240px]"
                />
              ) : (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => onConnect(undefined)}
                >
                  Change
                </Button>
              )
            ) : null
          }
          loadError={
            canChange && orgCredentials.status === "error" ? (
              <div className="flex flex-wrap items-center gap-2 pl-7">
                <span className="text-xs text-muted-foreground">
                  Couldn&apos;t load your other saved logins.
                </span>
                {retryButton}
              </div>
            ) : null
          }
        />
        {/* Mounted for the card's lifetime so only its text changes: a live region that appears
            already holding its text is read as ordinary new content and never announced. It carries
            the search failure too, whose visible text sits in a portaled popover no region reaches. */}
        <span
          className="sr-only"
          role={insideLiveRegion ? undefined : "status"}
        >
          {searchFailed
            ? SEARCH_FAILED_ANNOUNCEMENT
            : !insideLiveRegion &&
                canChange &&
                orgCredentials.status === "error"
              ? "Couldn't load your other saved logins."
              : ""}
        </span>
      </div>
    );
  }

  const site = siteFromLoginPageUrls(frame.login_page_urls);
  const registration = frame.registration ?? null;
  const offerGenerate = Boolean(
    registration && onGenerate && !registration.attempted,
  );
  const registrationLines = registration
    ? [
        <span key="destination" className="text-[11px] leading-relaxed">
          Sign-up page: {frame.login_page_urls?.[0] ?? site}
        </span>,
        <span key="username" className="text-[11px] leading-relaxed">
          Username: {registration.username}
        </span>,
        <span key="saved-as" className="text-[11px] leading-relaxed">
          Saved as: {registration.credential_name}
        </span>,
        ...(registration.outcome
          ? [
              <span key="outcome" className="text-[11px] font-medium">
                {registration.outcome === "rejected"
                  ? "Nothing was saved — the credential couldn't be created. Pick or add a login instead."
                  : registration.outcome === "created_not_connected"
                    ? `Saved as ${registration.credential_name}, not connected.`
                    : "The vault didn't confirm the save. Check your credentials before adding another."}
              </span>,
            ]
          : []),
      ]
    : [];
  const connectButton = (
    <Button
      type="button"
      size="sm"
      variant={
        (signIn && frame.signing_in) || offerGenerate ? "outline" : "default"
      }
      disabled={disabled}
      onClick={() => onConnect(undefined)}
      data-tour="credential-connect"
    >
      Connect credential
    </Button>
  );
  if (signIn && frame.signing_in) {
    const notFound = signIn.saveFailed
      ? "Couldn't save your sign-in. Click Done to try again, or connect a credential."
      : signIn.notFoundHost
        ? `No sign-in found for ${signIn.notFoundHost}. Sign in, then click Done again.`
        : "";
    return (
      <AskChrome
        tray={tray}
        rootRef={rootRef}
        message={frame.message}
        title={`Sign in to ${site} in the browser`}
        countdown={
          <PauseCountdown remainingMs={remainingMs} expired={expired} />
        }
        lines={[
          <span
            key="how"
            className="text-[11px] leading-relaxed text-muted-foreground"
          >
            Use the browser to sign in, including any verification code, then
            click Done. Skyvern saves the sign-in as a browser profile; no
            password is stored.
          </span>,
          ...(notFound
            ? [
                <span key="not-found" className="text-[11px] font-medium">
                  {notFound}
                </span>,
              ]
            : []),
        ]}
        footer={
          <>
            <Button
              type="button"
              size="sm"
              disabled={disabled || signIn.busy}
              onClick={signIn.onDone}
            >
              {signIn.busy ? "Saving sign-in…" : "Done"}
            </Button>
            {connectButton}
            <div className="ml-auto">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => onSkip()}
                disabled={disabled || signIn.busy}
              >
                Cancel
              </Button>
            </div>
          </>
        }
        announcer={
          <span
            className="sr-only"
            role={insideLiveRegion ? undefined : "status"}
          >
            {insideLiveRegion ? "" : notFound}
          </span>
        }
      />
    );
  }
  return (
    <AskChrome
      tray={tray}
      rootRef={rootRef}
      message={frame.message}
      title={
        registration
          ? `Create a login for ${site}`
          : `Copilot needs to sign in to ${site}`
      }
      countdown={
        countdownActive ? (
          <PauseCountdown remainingMs={remainingMs} expired={expired} />
        ) : null
      }
      lines={[
        <span
          key="why"
          className="text-[11px] leading-relaxed text-muted-foreground"
        >
          {CREDENTIAL_WHY_LINE_BY_REASON[frame.reason] ?? SIGN_IN_WHY_LINE}
        </span>,
        ...registrationLines,
        ...(mode === "terminal"
          ? [
              <span
                key="continue"
                className="text-[11px] font-medium leading-relaxed"
              >
                Connect a credential and I&apos;ll continue.
              </span>,
            ]
          : []),
      ]}
      footer={
        <>
          {offerGenerate ? (
            <Button
              type="button"
              size="sm"
              disabled={disabled}
              onClick={() => onGenerate?.()}
            >
              Generate and save
            </Button>
          ) : null}
          {connectButton}
          {signIn ? (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={disabled || signIn.busy}
              onClick={signIn.onStart}
            >
              Sign in myself
            </Button>
          ) : null}
          {orgCredentials.status === "ready" &&
          (pickable.length > 0 || pickerEngaged) ? (
            <CredentialPicker
              credentials={pickable}
              suggestedIds={frame.credential_refs}
              disabled={disabled}
              onPick={(credentialId, name) => onConnect(credentialId, name)}
              search={search}
              onSearchChange={changeSearch}
              searchFailed={searchFailed}
              searchPending={searchPending}
              resultsTruncated={resultsTruncated}
              onRetrySearch={retrySearch}
            />
          ) : null}
          {orgCredentials.status === "loading" ? (
            <span className="text-xs text-muted-foreground">
              Loading saved logins…
            </span>
          ) : null}
          {orgCredentials.status === "error" ? (
            <>
              <span className="text-xs text-muted-foreground">
                Couldn&apos;t load your saved logins.
              </span>
              {retryButton}
            </>
          ) : null}
          <div className="ml-auto">
            <SkipButton onSkip={onSkip} disabled={disabled} />
          </div>
        </>
      }
      announcer={
        // Mounted for the card's lifetime so only its text changes: a live region that appears
        // already holding its text is read as ordinary new content and never announced. Inside an
        // outer live region it takes no role and repeats nothing that region already contains,
        // only the search failure, whose visible text sits in a portaled popover.
        <span
          className="sr-only"
          role={insideLiveRegion ? undefined : "status"}
        >
          {searchFailed
            ? SEARCH_FAILED_ANNOUNCEMENT
            : insideLiveRegion
              ? ""
              : orgCredentials.status === "loading"
                ? "Loading saved logins…"
                : orgCredentials.status === "error"
                  ? "Couldn't load your saved logins."
                  : ""}
        </span>
      }
    />
  );
}

function AutoBoundReceipt({
  name,
  canChange,
  change,
  loadError,
}: {
  name: string;
  canChange: boolean;
  change: ReactNode;
  loadError: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <CopilotCard>
      <CardHeader
        icon={<AppliedCheck />}
        title={<span title={name}>Using credential &apos;{name}&apos;</span>}
        actions={change}
        expanded={expanded}
        onToggle={() => setExpanded((value) => !value)}
      />
      {expanded || loadError ? (
        <CardBody>
          {expanded ? (
            <GutterRow>
              <span className="text-[11px] leading-relaxed text-muted-foreground">
                {canChange
                  ? "Auto-selected to sign in on your behalf — Change it if this isn't right."
                  : "Auto-selected to sign in on your behalf."}
              </span>
            </GutterRow>
          ) : null}
          {loadError}
        </CardBody>
      ) : null}
    </CopilotCard>
  );
}
