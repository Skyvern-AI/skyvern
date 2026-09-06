import {
  CheckIcon,
  CursorArrowIcon,
  PauseIcon,
  Pencil1Icon,
  PlayIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";

import { Button } from "@/components/ui/button";
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
 * How long Done waits for the backend's finalized interpretation snapshot
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
} {
  switch (kind) {
    case "credit_card":
      return {
        type: CredentialModalTypes.CREDIT_CARD,
        heading: "Add Credit Card",
        buttonLabel: "Add credit card",
      };
    case "secret":
      return {
        type: CredentialModalTypes.SECRET,
        heading: "Add Secret",
        buttonLabel: "Add secret",
      };
    case "totp":
      return {
        type: CredentialModalTypes.PASSWORD,
        defaultTotpType: "authenticator",
        heading: "Add Two-Factor Authentication",
        buttonLabel: "Add two-factor authentication",
      };
    case "magic_link":
      return {
        type: CredentialModalTypes.PASSWORD,
        defaultTotpType: "email",
        heading: "Add Magic Link",
        buttonLabel: "Add magic link",
      };
  }

  // "password", plus any kind a backend one deploy ahead sends that this bundle has never
  // seen. This runs during render, so falling off the end would throw on `.buttonLabel`
  // and unmount the editor rather than degrade the single button.
  return {
    type: CredentialModalTypes.PASSWORD,
    heading: "Add Password",
    buttonLabel: "Add password",
  };
}

function DraftStepCard({
  step,
  index,
  baselineMs,
  onDelete,
  onRename,
  showCredentialPrompt,
  addCredentialLabel,
  onAddCredentials,
  onDismissCredential,
}: {
  step: RecordingDraftStep;
  index: number;
  baselineMs: number | null;
  onDelete: () => void;
  onRename: (value: string) => void;
  showCredentialPrompt: boolean;
  addCredentialLabel: string;
  onAddCredentials: () => void;
  onDismissCredential: () => void;
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
      {showCredentialPrompt && (
        <div className="flex flex-wrap items-center gap-2 pl-8">
          <Button
            type="button"
            size="sm"
            className="h-7"
            onClick={onAddCredentials}
          >
            {addCredentialLabel}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7"
            onClick={onDismissCredential}
          >
            Dismiss
          </Button>
        </div>
      )}
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
};

function RecordingPanel({ browserSessionId }: Props) {
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
  // The debug session's browser_session_id resolves asynchronously; Done can
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
  const feedEndRef = useRef<HTMLDivElement | null>(null);
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
    manualCapturePaused,
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
      manualCapturePaused: state.manualCapturePaused,
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
    onSuccess: (result) => {
      setRecordedBlocks(result, insertionPoint);
      useRecordingStore.getState().setIsRecording(false);
    },
  });

  const mutationIsPending = processRecordingMutation.isPending;
  const setIsCommitting = useRecordingStore((state) => state.setIsCommitting);
  useEffect(() => {
    setIsCommitting(mutationIsPending);
  }, [mutationIsPending, setIsCommitting]);

  const mutationIsError = processRecordingMutation.isError;
  useEffect(() => {
    if (mutationIsError) {
      // Allow Done to retry with the drafts we still hold.
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

  // Done flow: requestFinish stops exfiltration; the backend flushes a final
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

  // keep the newest block in view
  useEffect(() => {
    feedEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [actionCount, interpretationPending]);

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

  const onDoneClick = () => {
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
    ? "Finishing recording"
    : manualCapturePaused
      ? "Recording paused"
      : "Recording browser";

  return (
    <div className="flex h-full w-full flex-col overflow-hidden rounded-xl border bg-slate-elevation2">
      {/* header */}
      <div className="flex flex-none items-center gap-2.5 border-b px-3.5 py-3">
        <span
          className={cn("h-2.5 w-2.5 flex-none rounded-full bg-red-500", {
            "animate-pulse": !isFinishing && !manualCapturePaused,
            "opacity-60": isFinishing || manualCapturePaused,
          })}
        />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-foreground">
            {headerTitle}
          </div>
          <div className="font-mono text-[11.5px] text-muted-foreground">
            {formatRecordingClock(elapsedSeconds)} · {actionCount} action
            {actionCount === 1 ? "" : "s"}
          </div>
        </div>
      </div>

      {/* feed */}
      <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2 pb-3 pt-2">
        {visibleSteps.length === 0 &&
        optimisticSteps.length === 0 &&
        !interpretationPending ? (
          <div className="flex flex-col items-center justify-center gap-2.5 px-5 py-10 text-center text-xs leading-relaxed text-muted-foreground">
            <CursorArrowIcon className="h-5 w-5" />
            {manualCapturePaused ? (
              <>
                Capture is paused.
                <br />
                Resume to continue recording browser actions.
              </>
            ) : (
              <>
                Interact with the browser.
                <br />
                Your clicks, typing and navigation will appear here as blocks.
              </>
            )}
          </div>
        ) : (
          visibleSteps.map((step, index) => (
            <DraftStepCard
              key={step.step_id}
              step={step}
              index={index}
              baselineMs={baselineMs}
              showCredentialPrompt={
                Boolean(step.credential_kind) &&
                !dismissedCredentialStepIds.includes(step.step_id)
              }
              addCredentialLabel={
                step.credential_kind
                  ? credentialPromptForKind(step.credential_kind).buttonLabel
                  : "Add to credentials"
              }
              onAddCredentials={() => {
                if (!step.credential_kind) {
                  return;
                }
                const prompt = credentialPromptForKind(step.credential_kind);
                setCredentialModal({
                  type: prompt.type,
                  defaultTotpType: prompt.defaultTotpType,
                  heading: prompt.heading,
                  testUrl: step.url ?? null,
                  url: step.url ?? null,
                  stepId: step.step_id,
                });
              }}
              onDismissCredential={() =>
                useRecordingStore
                  .getState()
                  .dismissCredentialPrompt(step.step_id)
              }
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
            {exposedEventCount === 1 ? "" : "s"} captured — blocks will be
            generated when you finish.
          </div>
        )}
        <div ref={feedEndRef} />
      </div>

      {insertionPointMissing && (
        <div className="flex-none border-t px-3.5 py-2 text-[11px] leading-relaxed text-red-700 dark:text-red-400">
          Could not determine where to insert recorded blocks. Discard and start
          recording from the workflow editor again.
        </div>
      )}

      {/* controls */}
      <div className="flex flex-none items-center gap-2 border-t px-3 py-2.5">
        <Button
          variant="outline"
          size="icon"
          title={manualCapturePaused ? "Resume capture" : "Pause capture"}
          className="h-8 w-8"
          disabled={isCommitting || isFinishing}
          onClick={() =>
            useRecordingStore
              .getState()
              .setManualCapturePaused(!manualCapturePaused)
          }
        >
          {manualCapturePaused ? (
            <PlayIcon className="h-4 w-4" />
          ) : (
            <PauseIcon className="h-4 w-4" />
          )}
        </Button>
        <Button
          variant="outline"
          size="icon"
          title="Discard recording"
          className="h-8 w-8 hover:border-red-500/40 hover:text-red-700 dark:hover:text-red-400"
          disabled={isCommitting}
          onClick={onDiscardClick}
        >
          <TrashIcon className="h-4 w-4" />
        </Button>
        <Button
          size="sm"
          className="ml-auto h-8"
          disabled={
            insertionPointMissing ||
            browserSessionMissing ||
            (isFinishing && !mutationIsError)
          }
          onClick={onDoneClick}
        >
          <CheckIcon className="mr-1.5 h-4 w-4" />
          {mutationIsError ? "Retry" : isFinishing ? "Processing…" : "Done"}
        </Button>
      </div>

      {confirmDiscardOpen && (
        <Dialog open onOpenChange={setConfirmDiscardOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Discard recording?</DialogTitle>
              <DialogDescription>
                {visibleSteps.length > 0
                  ? `You have ${visibleSteps.length} recorded block${
                      visibleSteps.length === 1 ? "" : "s"
                    } that will be lost if you discard.`
                  : "Your recorded interactions will be lost if you discard."}{" "}
                Are you sure you want to discard the recording?
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                variant="outline"
                onClick={() => setConfirmDiscardOpen(false)}
              >
                Keep recording
              </Button>
              <Button variant="destructive" onClick={discard}>
                Discard recording
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
}

export { RecordingPanel };
