import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronUpIcon,
  CursorArrowIcon,
  DotsHorizontalIcon,
  EnterFullScreenIcon,
  ExitFullScreenIcon,
  LockClosedIcon,
  Pencil1Icon,
  ReloadIcon,
  StopIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import {
  forwardRef,
  type ForwardedRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useShallow } from "zustand/react/shallow";

import { Button } from "@/components/ui/button";
import { SuggestionCard } from "@/components/SuggestionCard";
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
import { useRecordingElapsedSeconds } from "@/hooks/useRecordingElapsedSeconds";
import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { CredentialsModal } from "@/routes/credentials/CredentialsModal";
import {
  CredentialModalTypes,
  type CredentialModalType,
} from "@/routes/credentials/useCredentialModalState";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";
import {
  applyDraftStepOverlays,
  useRecordingStore,
  type RecordingActionKind,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";
import { captureRecordBrowser } from "@/util/recordBrowserTelemetry";
import { formatRecordingClock } from "@/util/recordingClock";
import { cn } from "@/util/utils";
import { buildDraftStepTitlePatch } from "./recordingDraftStepEdits";

const KIND_LABELS: Record<RecordingActionKind, string> = {
  click: "Click",
  hover: "Hover",
  input_text: "Input text",
  url_change: "Navigation",
  wait: "Wait",
};

/**
 * How long Stop waits for the backend's finalized interpretation snapshot
 * (flushed on end-exfiltration) before committing whatever drafts we have.
 */
const FINALIZE_TIMEOUT_MS = 5000;

function shortUrl(url: string | null | undefined): string {
  if (!url) {
    return "";
  }
  try {
    const parsed = new URL(url);
    return parsed.host + (parsed.pathname === "/" ? "" : parsed.pathname);
  } catch {
    return url;
  }
}

function formatDraftStepDisplayTitle(step: RecordingDraftStep): string {
  if (step.title?.trim()) {
    return step.title.trim();
  }

  if (step.action_kind === "url_change" || step.block_type === "goto_url") {
    const destination = shortUrl(step.url);
    if (destination) {
      return `Go to ${destination}`;
    }
  }

  if (step.label.startsWith("goto_")) {
    const slug = step.label.slice("goto_".length).replace(/_/g, ".");
    return slug ? `Go to ${slug}` : "Go to page";
  }

  return step.label;
}

function credentialPromptForKind(
  kind: NonNullable<RecordingDraftStep["credential_kind"]>,
): {
  type: CredentialModalType;
  defaultTotpType?: "authenticator" | "email";
  heading: string;
  buttonLabel: string;
  suggestionTitle: string;
  suggestionDescription: string;
} {
  switch (kind) {
    case "credit_card":
      return {
        type: CredentialModalTypes.CREDIT_CARD,
        heading: "Add Credit Card",
        buttonLabel: "Add credit card",
        suggestionTitle: "Use this card automatically",
        suggestionDescription:
          "Add this credit card to Credentials so Skyvern can use it securely when the workflow runs.",
      };
    case "secret":
      return {
        type: CredentialModalTypes.SECRET,
        heading: "Add Secret",
        buttonLabel: "Add secret",
        suggestionTitle: "Reuse this secret securely",
        suggestionDescription:
          "Add this secret to Credentials so Skyvern can use it securely when the workflow runs.",
      };
    case "totp":
      return {
        type: CredentialModalTypes.PASSWORD,
        defaultTotpType: "authenticator",
        heading: "Add Two-Factor Authentication",
        buttonLabel: "Add two-factor authentication",
        suggestionTitle: "Complete two-factor authentication",
        suggestionDescription:
          "Add this authentication method to Credentials so Skyvern can complete sign-in when the workflow runs.",
      };
    case "magic_link":
      return {
        type: CredentialModalTypes.PASSWORD,
        defaultTotpType: "email",
        heading: "Add Magic Link",
        buttonLabel: "Add magic link",
        suggestionTitle: "Complete magic-link sign-in",
        suggestionDescription:
          "Add this email sign-in method to Credentials so Skyvern can use it when the workflow runs.",
      };
  }

  // "password", plus any kind a backend one deploy ahead sends that this bundle has never
  // seen. This runs during render, so falling off the end would throw on `.buttonLabel`
  // and unmount the editor rather than degrade the single button.
  return {
    type: CredentialModalTypes.PASSWORD,
    heading: "Add Password",
    buttonLabel: "Add password",
    suggestionTitle: "Sign in automatically",
    suggestionDescription:
      "Add this password to Credentials so Skyvern can use it securely when the workflow runs.",
  };
}

function DraftStepCard({
  step,
  index,
  baselineMs,
  onDelete,
  onRename,
}: {
  step: RecordingDraftStep;
  index: number;
  baselineMs: number | null;
  onDelete: () => void;
  onRename: (value: string) => void;
}) {
  const beginDraftEdit = useRecordingStore((state) => state.beginDraftEdit);
  const endDraftEdit = useRecordingStore((state) => state.endDraftEdit);
  const [isEditing, setIsEditing] = useState(false);
  const displayTitle = formatDraftStepDisplayTitle(step);
  const [draftTitle, setDraftTitle] = useState(displayTitle);

  useEffect(() => {
    if (!isEditing) {
      return;
    }
    beginDraftEdit();
    return () => {
      endDraftEdit();
    };
  }, [isEditing, beginDraftEdit, endDraftEdit]);

  useEffect(() => {
    if (!isEditing) {
      setDraftTitle(displayTitle);
    }
  }, [displayTitle, isEditing]);

  const saveTitle = () => {
    const trimmed = draftTitle.trim();
    if (trimmed && trimmed !== displayTitle) {
      onRename(trimmed);
    }
    setIsEditing(false);
  };

  const relativeSeconds =
    baselineMs !== null &&
    step.timestamp_start !== null &&
    step.timestamp_start !== undefined
      ? (step.timestamp_start - baselineMs) / 1000
      : null;
  const meta = shortUrl(step.url);

  return (
    <div className="group relative flex flex-col gap-2 rounded-[10px] p-3 transition-colors hover:bg-slate-elevation3">
      <div className="flex items-start gap-2.5">
        <span className="mt-px flex h-[22px] w-[22px] flex-none items-center justify-center rounded-full bg-slate-elevation5 text-[11px] font-semibold text-foreground">
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          {isEditing ? (
            <input
              autoFocus
              className="w-full rounded border bg-background px-1.5 py-0.5 text-[13px] font-medium text-foreground outline-none focus:ring-1 focus:ring-ring"
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              onBlur={saveTitle}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  saveTitle();
                }
                if (e.key === "Escape") {
                  setDraftTitle(displayTitle);
                  setIsEditing(false);
                }
              }}
            />
          ) : (
            <div
              className="cursor-text rounded text-[13px] font-medium leading-snug text-foreground hover:bg-slate-elevation5 hover:shadow-[0_0_0_4px_hsl(var(--slate-elevation-5))]"
              role="button"
              tabIndex={0}
              title="Click to edit"
              onClick={() => setIsEditing(true)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  setIsEditing(true);
                }
              }}
            >
              {displayTitle}
            </div>
          )}
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
            {KIND_LABELS[step.action_kind] ?? step.action_kind}
            {relativeSeconds !== null &&
              ` · ${formatRecordingClock(relativeSeconds)}`}
            {meta && (
              <>
                {" · "}
                <span className="font-mono">{meta}</span>
              </>
            )}
            {step.status === "interpreting" && (
              <span className="animate-pulse text-yellow-500">
                {" · refining…"}
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-none gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          <button
            type="button"
            title="Edit title"
            className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-slate-elevation5 hover:text-foreground"
            onClick={() => setIsEditing(true)}
          >
            <Pencil1Icon className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            title="Delete block"
            className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-slate-elevation5 hover:text-red-700 dark:hover:text-red-400"
            onClick={onDelete}
          >
            <TrashIcon className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}

function InterpretingRow({
  index,
  label,
  title,
}: {
  index: number;
  label: string;
  title?: string;
}) {
  return (
    <div className="flex items-start gap-2.5 p-3">
      <span className="mt-px flex h-[22px] w-[22px] flex-none items-center justify-center rounded-full bg-slate-elevation5 text-[11px] font-semibold text-muted-foreground">
        {index + 1}
      </span>
      <div className="min-w-0 flex-1">
        {title ? (
          <div className="truncate text-[13px] font-medium leading-snug text-foreground">
            {title}
          </div>
        ) : (
          <div className="mb-1.5 h-3 w-3/5 animate-pulse rounded bg-slate-elevation5" />
        )}
        <div className="text-[11px] text-yellow-500">{label}</div>
      </div>
    </div>
  );
}

type Props = {
  browserSessionId: string | null;
  expanded?: boolean;
  collapsed?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  onCollapsedChange?: (collapsed: boolean) => void;
  onBackToChat?: () => void;
  portalTarget?: HTMLElement | null;
  suggestionPortalTarget?: HTMLElement | null;
};

function RecordingPanelImpl(
  {
    browserSessionId,
    expanded = false,
    collapsed = false,
    onExpandedChange,
    onCollapsedChange,
    onBackToChat,
    portalTarget,
    suggestionPortalTarget,
  }: Props,
  forwardedRef: ForwardedRef<HTMLDivElement>,
) {
  const setRecordedBlocks = useRecordedBlocksStore(
    (state) => state.setRecordedBlocks,
  );
  // Captured once on mount via getState() — deliberately not a subscription:
  // recording starters stash their insertion point in the workflow panel store
  // right before recording begins, and later panel-state changes (e.g. the user
  // browsing the node library mid-recording) must not move the commit target.
  const [insertionPointState] = useState(() => {
    const data = useWorkflowPanelStore.getState().workflowPanelState.data;
    if (!data) {
      captureRecordBrowser("record_browser.missing_insertion_point");
    }
    return {
      insertionPoint: {
        previous: data?.previous ?? null,
        next: data?.next ?? null,
        parent: data?.parent,
        connectingEdgeType: data?.connectingEdgeType ?? "default",
      },
      isValid: data !== null && data !== undefined,
    };
  });
  const insertionPoint = insertionPointState.insertionPoint;
  const insertionPointMissing = !insertionPointState.isValid;
  // The debug session's browser_session_id resolves asynchronously; Stop can
  // be reachable before it does (isRecording lives in the in-memory
  // useRecordingStore and can already be true when this component remounts),
  // so gate on it the same way as insertionPointMissing rather than letting
  // the mutation guard throw.
  const browserSessionMissing = !browserSessionId;
  const [confirmDiscardOpen, setConfirmDiscardOpen] = useState(false);
  const [credentialModal, setCredentialModal] = useState<{
    type: CredentialModalType;
    testUrl: string | null;
    url: string | null;
    stepId: string;
    defaultTotpType?: "authenticator" | "email";
    heading: string;
  } | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  useImperativeHandle(forwardedRef, () => panelRef.current as HTMLDivElement);
  const feedRef = useRef<HTMLDivElement | null>(null);
  const [feedPinned, setFeedPinned] = useState(true);
  const committedRef = useRef(false);

  // Slice the frequently-changing fields with useShallow so live-recording
  // updates (optimistic appends, exposedEventCount ticks) only re-render when a
  // field this panel reads actually changes, not on every exfiltrated event.
  const {
    draftSteps,
    deletedStepIds,
    stepPatches,
    dismissedCredentialStepIds,
    sessionRevision,
    optimisticSteps: rawOptimisticSteps,
    workflowPermanentId,
    interpretationPending,
    interpretationFinalized,
    finishRequested,
    isCommitting,
    exposedEventCount,
  } = useRecordingStore(
    useShallow((state) => ({
      draftSteps: state.draftSteps,
      deletedStepIds: state.deletedStepIds,
      stepPatches: state.stepPatches,
      dismissedCredentialStepIds: state.dismissedCredentialStepIds,
      sessionRevision: state.sessionRevision,
      optimisticSteps: state.optimisticSteps,
      workflowPermanentId: state.workflowPermanentId,
      interpretationPending: state.interpretationPending,
      interpretationFinalized: state.interpretationFinalized,
      finishRequested: state.finishRequested,
      isCommitting: state.isCommitting,
      exposedEventCount: state.exposedEventCount,
    })),
  );

  const interpretationEnabled = workflowPermanentId !== null;

  const visibleSteps = useMemo(
    () => applyDraftStepOverlays(draftSteps, deletedStepIds, stepPatches),
    [draftSteps, deletedStepIds, stepPatches],
  );

  // Surface optimistic placeholders whenever interpretation is enabled (not just
  // after the first snapshot), so the first steps appear without a round-trip.
  // When interpretation is disabled they would accumulate unreconciled, so hide.
  const optimisticSteps = interpretationEnabled ? rawOptimisticSteps : [];
  const actionCount = visibleSteps.length + optimisticSteps.length;

  // Step times are remote-browser clocks; anchor to the first step instead of
  // the operator's local clock to dodge skew.
  const baselineMs = useMemo(() => {
    for (const step of visibleSteps) {
      if (step.timestamp_start !== null && step.timestamp_start !== undefined) {
        return step.timestamp_start;
      }
    }
    return null;
  }, [visibleSteps]);

  const processRecordingMutation = useProcessRecordingMutation({
    browserSessionId,
    onSuccess: (result, owner) => {
      if (
        !owner.active ||
        useWorkflowYamlEditorStore.getState().editorOwner !== owner
      )
        return;
      setRecordedBlocks(result, insertionPoint, owner);
      useRecordingStore.getState().setIsRecording(false);
    },
  });

  const mutationIsError = processRecordingMutation.isError;
  useEffect(() => {
    if (mutationIsError) {
      // Allow Stop to retry with the drafts we still hold.
      committedRef.current = false;
    }
  }, [mutationIsError]);

  const commit = () => {
    if (
      committedRef.current ||
      insertionPointMissing ||
      browserSessionMissing
    ) {
      return;
    }
    committedRef.current = true;
    processRecordingMutation.mutate({
      draftSteps: useRecordingStore.getState().getFinalDraftSteps(),
    });
  };
  const commitRef = useRef(commit);
  commitRef.current = commit;

  // Stop flow: requestFinish stops exfiltration; the backend flushes a final
  // interpretation snapshot, then we commit (or commit anyway on timeout).
  // commitRef keeps the timeout callback stable; mutationIsError clears
  // committedRef so the timeout can fire again on retry.
  useEffect(() => {
    if (!finishRequested || committedRef.current || browserSessionMissing) {
      return;
    }
    if (interpretationFinalized || sessionRevision === 0) {
      commitRef.current();
      return;
    }
    const timeout = setTimeout(() => commitRef.current(), FINALIZE_TIMEOUT_MS);
    return () => clearTimeout(timeout);
  }, [
    finishRequested,
    interpretationFinalized,
    sessionRevision,
    browserSessionMissing,
  ]);

  // This feed follows independently from the outer Copilot transcript. Reading
  // older recorded actions disengages the follower until the user returns to
  // the bottom; new chat messages never move this scroll position.
  useEffect(() => {
    const feed = feedRef.current;
    if (!feed || !feedPinned) {
      return;
    }
    if (typeof feed.scrollTo === "function") {
      feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
    } else {
      feed.scrollTop = feed.scrollHeight;
    }
  }, [actionCount, interpretationPending, feedPinned]);

  const elapsedSeconds = useRecordingElapsedSeconds();

  const discard = () => {
    const store = useRecordingStore.getState();
    captureRecordBrowser("record_browser.cancelled", {
      event_count_at_cancel: store.getEventCount(),
      seconds_recording: store.getSecondsRecording(),
    });
    setConfirmDiscardOpen(false);
    store.setIsRecording(false);
    store.reset();
  };

  const onDiscardClick = () => {
    if (visibleSteps.length > 0 || exposedEventCount > 0) {
      setConfirmDiscardOpen(true);
    } else {
      discard();
    }
  };

  const onStopClick = () => {
    if (insertionPointMissing || browserSessionMissing) {
      return;
    }
    if (finishRequested) {
      // A previous commit attempt failed; retry directly.
      commit();
      return;
    }
    useRecordingStore.getState().requestFinish();
  };

  const isFinishing = finishRequested || isCommitting;
  const showInterpretationFallbackNote =
    !interpretationEnabled && exposedEventCount > 0;
  const headerTitle = isFinishing
    ? "Finishing recording…"
    : "Copilot is following along";

  // Focusing and filling one field both carry its credential kind, so group by
  // kind and site to show one suggestion per credential.
  const credentialSuggestionGroups = new Map<
    string,
    { step: RecordingDraftStep; stepIds: string[] }
  >();
  for (const step of visibleSteps) {
    if (
      !step.credential_kind ||
      dismissedCredentialStepIds.includes(step.step_id)
    ) {
      continue;
    }
    const key = `${step.credential_kind}|${shortUrl(step.url)}`;
    const group = credentialSuggestionGroups.get(key);
    if (group) {
      group.stepIds.push(step.step_id);
    } else {
      credentialSuggestionGroups.set(key, { step, stepIds: [step.step_id] });
    }
  }
  const credentialSuggestions = [...credentialSuggestionGroups.values()].map(
    ({ step, stepIds }) => {
      const kind = step.credential_kind!;
      const prompt = credentialPromptForKind(kind);
      const meta = shortUrl(step.url);
      return (
        <SuggestionCard
          key={step.step_id}
          title={prompt.suggestionTitle}
          description={prompt.suggestionDescription}
          detail={
            meta ? (
              <div className="flex w-fit max-w-full items-center gap-1.5 rounded-md border border-border bg-background/50 px-2 py-1 font-mono text-[10px] text-muted-foreground">
                <LockClosedIcon className="h-3 w-3 flex-none" />
                <span className="truncate">{meta}</span>
              </div>
            ) : undefined
          }
          actions={
            <>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  const store = useRecordingStore.getState();
                  stepIds.forEach((id) => store.dismissCredentialPrompt(id));
                }}
              >
                Skip
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() =>
                  setCredentialModal({
                    type: prompt.type,
                    defaultTotpType: prompt.defaultTotpType,
                    heading: prompt.heading,
                    testUrl: step.url ?? null,
                    url: step.url ?? null,
                    stepId: step.step_id,
                  })
                }
              >
                {prompt.buttonLabel}
              </Button>
            </>
          }
        />
      );
    },
  );

  const credentialSuggestionList = credentialSuggestions.length ? (
    <div data-testid="recording-credential-suggestions" className="space-y-3">
      {credentialSuggestions}
    </div>
  ) : null;

  const panel = (
    <div
      ref={panelRef}
      data-testid="recording-chapter"
      className={cn(
        "flex w-full flex-col overflow-hidden border bg-slate-elevation2",
        expanded
          ? "h-full rounded-none border-x-0"
          : collapsed
            ? "rounded-lg"
            : "h-[min(28rem,62vh)] min-h-72 rounded-lg",
      )}
    >
      {expanded ? (
        <div className="flex h-10 flex-none items-center border-b px-2.5">
          <button
            type="button"
            onClick={onBackToChat}
            className="flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronLeftIcon className="size-3.5" aria-hidden="true" />
            Back to chat
          </button>
          <span className="ml-auto rounded bg-red-500/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.12em] text-red-600 dark:text-red-400">
            {isFinishing ? "Finishing" : "Live"}
          </span>
        </div>
      ) : null}

      <div className="flex flex-none items-center gap-2.5 border-b px-3 py-2.5">
        <span className="flex size-7 flex-none items-center justify-center rounded-md bg-red-500/10">
          {isFinishing ? (
            <ReloadIcon className="size-3.5 animate-spin text-red-500 motion-reduce:animate-none" />
          ) : (
            <span className="size-2.5 animate-pulse rounded-full bg-red-500 motion-reduce:animate-none" />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-foreground">
            {headerTitle}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
            {isFinishing
              ? "Saving the last recorded actions"
              : `${actionCount} captured action${actionCount === 1 ? "" : "s"}`}
          </div>
        </div>
        {!expanded ? (
          <button
            type="button"
            aria-label={
              collapsed ? "Show recorded actions" : "Hide recorded actions"
            }
            onClick={() => onCollapsedChange?.(!collapsed)}
            className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {collapsed ? (
              <ChevronDownIcon className="size-3.5" />
            ) : (
              <ChevronUpIcon className="size-3.5" />
            )}
          </button>
        ) : null}
        <button
          type="button"
          aria-label={
            expanded ? "Return recording to chat" : "Expand recording"
          }
          onClick={() => onExpandedChange?.(!expanded)}
          className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {expanded ? (
            <ExitFullScreenIcon className="size-3.5" />
          ) : (
            <EnterFullScreenIcon className="size-3.5" />
          )}
        </button>
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label="Recording options"
              disabled={isFinishing && !mutationIsError}
              className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            >
              <DotsHorizontalIcon className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              className="text-red-700 focus:text-red-700 dark:text-red-400 dark:focus:text-red-400"
              onSelect={onDiscardClick}
            >
              <TrashIcon className="mr-2 size-4" />
              Discard recording
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {!collapsed ? (
        <div className="relative min-h-0 flex-1">
          <div
            ref={feedRef}
            data-testid="recording-action-feed"
            className="flex h-full flex-col gap-1 overflow-y-auto px-2 pb-3 pt-2"
            onScroll={(event) => {
              const feed = event.currentTarget;
              setFeedPinned(
                feed.scrollHeight - feed.scrollTop - feed.clientHeight <= 24,
              );
            }}
          >
            {visibleSteps.length === 0 &&
            optimisticSteps.length === 0 &&
            !interpretationPending ? (
              <div className="flex flex-col items-center justify-center gap-2.5 px-5 py-10 text-center text-xs leading-relaxed text-muted-foreground">
                <CursorArrowIcon className="h-5 w-5" />
                <>
                  Start demonstrating in the browser.
                  <br />
                  Copilot will narrate what it understands here.
                </>
              </div>
            ) : (
              visibleSteps.map((step, index) => (
                <DraftStepCard
                  key={step.step_id}
                  step={step}
                  index={index}
                  baselineMs={baselineMs}
                  onDelete={() =>
                    useRecordingStore.getState().deleteDraftStep(step.step_id)
                  }
                  onRename={(value) =>
                    useRecordingStore
                      .getState()
                      .patchDraftStep(
                        step.step_id,
                        buildDraftStepTitlePatch(step, value),
                      )
                  }
                />
              ))
            )}
            {suggestionPortalTarget === undefined
              ? credentialSuggestionList
              : null}
            {optimisticSteps.map((step, i) => (
              <InterpretingRow
                key={step.local_id}
                index={visibleSteps.length + i}
                label={isFinishing ? "Finalizing…" : "Interpreting…"}
                title={step.title}
              />
            ))}
            {interpretationPending && optimisticSteps.length === 0 && (
              <InterpretingRow
                index={visibleSteps.length}
                label={isFinishing ? "Finalizing…" : "Interpreting…"}
              />
            )}
            {showInterpretationFallbackNote && (
              <div className="px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                {exposedEventCount} interaction
                {exposedEventCount === 1 ? "" : "s"} captured — workflow steps
                will be generated when you stop recording.
              </div>
            )}
          </div>
          {!feedPinned ? (
            <button
              type="button"
              onClick={() => {
                const feed = feedRef.current;
                if (!feed) return;
                setFeedPinned(true);
                if (typeof feed.scrollTo === "function") {
                  feed.scrollTo({
                    top: feed.scrollHeight,
                    behavior: "smooth",
                  });
                } else {
                  feed.scrollTop = feed.scrollHeight;
                }
              }}
              className="absolute bottom-2 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border bg-slate-elevation3 px-2.5 py-1 text-[10px] text-foreground shadow-md hover:bg-slate-elevation4"
            >
              <ChevronDownIcon className="size-3" />
              Latest action
            </button>
          ) : null}
        </div>
      ) : null}

      {insertionPointMissing && (
        <div className="flex-none border-t px-3.5 py-2 text-[11px] leading-relaxed text-red-700 dark:text-red-400">
          Could not determine where to insert workflow steps. Discard and choose
          Record task from the workflow editor again.
        </div>
      )}

      <div className="flex flex-none items-center gap-2 border-t px-3 py-2.5">
        <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
          {isFinishing ? (
            <ReloadIcon className="size-3.5 shrink-0 animate-spin motion-reduce:animate-none" />
          ) : (
            <span className="size-1.5 shrink-0 rounded-full bg-red-500" />
          )}
          <span className="truncate">
            {isFinishing
              ? "Finishing recording…"
              : `Recording · ${formatRecordingClock(elapsedSeconds)}`}
          </span>
        </div>
        {isFinishing && !mutationIsError ? (
          <span className="ml-auto text-[10px] text-muted-foreground">
            Continues automatically
          </span>
        ) : (
          <Button
            size="sm"
            variant={mutationIsError ? "default" : "destructive"}
            className="ml-auto h-8"
            disabled={
              insertionPointMissing ||
              browserSessionMissing ||
              (isFinishing && !mutationIsError)
            }
            onClick={onStopClick}
          >
            {mutationIsError ? (
              <ReloadIcon className="mr-1.5 size-3.5" />
            ) : (
              <StopIcon className="mr-1.5 size-3.5" />
            )}
            {mutationIsError ? "Try again" : "Stop recording"}
          </Button>
        )}
      </div>

      {confirmDiscardOpen && (
        <Dialog open onOpenChange={setConfirmDiscardOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Discard task recording?</DialogTitle>
              <DialogDescription>
                {visibleSteps.length > 0
                  ? `You have ${visibleSteps.length} captured workflow step${
                      visibleSteps.length === 1 ? "" : "s"
                    } that will be lost if you discard.`
                  : "Your captured browser interactions will be lost if you discard."}{" "}
                Are you sure you want to discard this task recording?
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setConfirmDiscardOpen(false)}
              >
                Keep recording task
              </Button>
              <Button variant="destructive" onClick={discard}>
                Discard
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {credentialModal ? (
        <CredentialsModal
          isOpen
          overrideType={credentialModal.type}
          defaultTestUrl={credentialModal.testUrl ?? undefined}
          defaultTotpType={credentialModal.defaultTotpType}
          heading={credentialModal.heading}
          onOpenChange={(open) => {
            if (!open) {
              setCredentialModal(null);
            }
          }}
          onCredentialCreated={(credentialId) => {
            const store = useRecordingStore.getState();
            // Binds the vault entry to the step so the backend emits a login block
            // (password/totp/magic link) or a credential-bound action block.
            store.patchDraftStep(credentialModal.stepId, {
              credential_id: credentialId,
            });
            if (credentialModal.url) {
              store.dismissCredentialPromptsForUrl(credentialModal.url);
            } else {
              store.dismissCredentialPrompt(credentialModal.stepId);
            }
            setCredentialModal(null);
          }}
        />
      ) : null}
    </div>
  );

  if (portalTarget === null) {
    return null;
  }
  return (
    <>
      {portalTarget ? createPortal(panel, portalTarget) : panel}
      {suggestionPortalTarget
        ? createPortal(credentialSuggestionList, suggestionPortalTarget)
        : null}
    </>
  );
}

const RecordingPanel = forwardRef<HTMLDivElement, Props>(RecordingPanelImpl);

export { RecordingPanel };
