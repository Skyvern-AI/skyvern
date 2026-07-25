import { useEffect, useState } from "react";
import { Cross2Icon, LockClosedIcon } from "@radix-ui/react-icons";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Union of both a request-policy-time classifier's real reason tokens and a
// mid-build run-failure reason that isn't emitted by any shipped backend
// path yet. `type`/`reason` are the only fields a minimal signal guarantees;
// everything else on the frame below is populated only by a richer,
// timed pause signal and stays undefined otherwise.
export type CredentialRequiredReason =
  | "workflow_credential_inputs_unbound"
  | "credential_name_unresolved"
  | "credential_invention_requested"
  | "raw_secret"
  | "credential_deferred_draft"
  | "assistant_directed"
  | "missing_credential_run_failure";

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
  timestamp?: string;
}

export type CredentialCardMode = "terminal" | "inline-pause";

export type CredentialPauseOutcome = "connected" | "skipped" | "timeout";

export interface CredentialPauseHistorical {
  outcome: CredentialPauseOutcome;
  credentialId?: string;
}

export interface MatchingCredential {
  credentialId: string;
  name: string;
}

export interface CredentialCardProps {
  frame: CredentialRequiredFrame;
  mode: CredentialCardMode;
  matchingCredentials?: MatchingCredential[];
  resolvedOutcome?: CredentialPauseHistorical;
  // undefined = primary CTA (wiring opens the add-credential modal); a string
  // id = the user picked an already-stored credential (chip or dropdown).
  onConnect: (credentialId?: string) => void;
  onSkip: () => void;
  // Terminal connect auto-sends a "continue" turn; the receipt says so instead
  // of the plain "added". Defaults false so inline-pause and every other caller
  // keep the existing copy.
  continued?: boolean;
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
  // Lower-confidence text-marker detection of the same underlying need.
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
};

const SKIP_COPY =
  "Credential setup skipped — test run may stop at the login step";
const TIMEOUT_COPY =
  "Credential request timed out — test run may stop at the login step";

function siteFromLoginPageUrls(urls: string[] | undefined): string {
  const first = urls?.[0];
  if (!first) {
    return "the site";
  }
  try {
    return new URL(first).hostname;
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

function CredentialSystemRow({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 px-1 py-1 text-xs text-muted-foreground">
      <span className="h-1 w-1 flex-none rounded-full bg-muted-foreground dark:bg-slate-600" />
      {text}
    </div>
  );
}

export function CredentialCard({
  frame,
  mode,
  matchingCredentials = [],
  resolvedOutcome,
  onConnect,
  onSkip,
  continued = false,
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

  if (resolvedOutcome) {
    switch (resolvedOutcome.outcome) {
      case "skipped":
        return <CredentialSystemRow text={SKIP_COPY} />;
      case "timeout":
        return <CredentialSystemRow text={TIMEOUT_COPY} />;
      case "connected": {
        const name = matchingCredentials.find(
          (credential) =>
            credential.credentialId === resolvedOutcome.credentialId,
        )?.name;
        const heading = continued
          ? name
            ? `Continuing with '${name}'…`
            : "Continuing…"
          : name
            ? `Credential '${name}' added`
            : "Credential added";
        return (
          <div className="rounded-lg border border-border bg-slate-elevation2 p-3">
            <div className="flex items-center gap-2 text-xs font-semibold text-foreground">
              <span className="text-success">✓</span>
              {heading}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Stored encrypted · used to sign in on your behalf · never enters
              the chat
            </p>
          </div>
        );
      }
      default: {
        // Compile-time exhaustiveness guard: a future CredentialPauseOutcome
        // value fails to typecheck here. Renders a fallback instead of
        // throwing since `resolvedOutcome` will eventually come off the
        // network, where a crash would take down the whole chat pane.
        const _exhaustive: never = resolvedOutcome.outcome;
        void _exhaustive;
        return <CredentialSystemRow text="Credential status unavailable" />;
      }
    }
  }

  const site = siteFromLoginPageUrls(frame.login_page_urls);
  const singleMatch =
    matchingCredentials.length === 1 ? matchingCredentials[0] : null;
  const multipleMatches = matchingCredentials.length >= 2;

  return (
    <div>
      {frame.message ? (
        <p className="text-sm leading-relaxed text-foreground">
          {frame.message}
        </p>
      ) : null}
      <div
        className={`rounded-lg border border-border bg-slate-elevation2 p-3 ${frame.message ? "mt-2" : ""}`}
      >
        <div className="flex items-start gap-2">
          <span className="flex h-5 w-5 flex-none items-center justify-center rounded-md bg-warning/10 text-warning">
            <LockClosedIcon className="h-3 w-3" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-xs font-semibold text-foreground">
              Copilot needs to sign in to {site}
            </div>
            <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              {CREDENTIAL_WHY_LINE_BY_REASON[frame.reason] ?? SIGN_IN_WHY_LINE}
            </p>
            {mode === "terminal" ? (
              <p className="mt-1 text-[11px] font-medium leading-relaxed text-foreground">
                Connect a credential and I&apos;ll continue.
              </p>
            ) : null}
          </div>
          {countdownActive ? (
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
                {expired
                  ? "Timed out"
                  : formatCountdownAnnouncement(remainingMs)}
              </span>
            </>
          ) : null}
          <button
            type="button"
            aria-label="Skip for now"
            onClick={() => onSkip()}
            disabled={disabled}
            className="flex h-5 w-5 flex-none items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50 dark:text-slate-500"
          >
            <Cross2Icon className="h-3 w-3" />
          </button>
        </div>
        <div className="ml-7 mt-2.5 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onConnect(undefined)}
            className="rounded-md bg-cta px-3 py-1 text-xs font-medium text-cta-foreground hover:bg-cta-hover disabled:pointer-events-none disabled:opacity-50"
          >
            Connect credential
          </button>
          {singleMatch ? (
            <button
              type="button"
              disabled={disabled}
              onClick={() => onConnect(singleMatch.credentialId)}
              className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-50"
            >
              Use &apos;{singleMatch.name}&apos;?
            </button>
          ) : null}
          {multipleMatches ? (
            <Select
              disabled={disabled}
              onValueChange={(credentialId) => onConnect(credentialId)}
            >
              <SelectTrigger className="h-7 w-[170px] text-xs">
                <SelectValue placeholder="Use existing…" />
              </SelectTrigger>
              <SelectContent>
                {matchingCredentials.map((credential) => (
                  <SelectItem
                    key={credential.credentialId}
                    value={credential.credentialId}
                  >
                    {credential.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
        </div>
      </div>
    </div>
  );
}
