import { isAxiosError } from "axios";
import { useCallback, useEffect, useRef, useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { getSseClient } from "@/api/sse";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

import type {
  WorkflowCopilotActiveDemonstration,
  WorkflowCopilotDemonstrationActionUpdate,
  WorkflowCopilotDemonstrationControlRequest,
  WorkflowCopilotDemonstrationControlResult,
  WorkflowCopilotDemonstrationInsertion,
  WorkflowCopilotDemonstrationSensitiveFieldUpdate,
  WorkflowCopilotDemonstrationUpdate,
} from "./workflowCopilotTypes";

type DemonstrationPhase = "idle" | "recording" | "stopped";

type DemonstrationAction = Pick<
  WorkflowCopilotDemonstrationActionUpdate,
  "seq" | "label"
>;

type DemonstrationDone = {
  demonstrationId: string;
  actions: DemonstrationAction[];
};

type UseDemonstrationRecorderOptions = {
  workflowPermanentId: string | undefined;
  browserSessionId: string | null | undefined;
  onDone: (demonstration: DemonstrationDone) => boolean | Promise<boolean>;
  onStartFailed: () => void;
};

export function useDemonstrationRecorder({
  workflowPermanentId,
  browserSessionId,
  onDone,
  onStartFailed,
}: UseDemonstrationRecorderOptions) {
  const credentialGetter = useCredentialGetter();
  const [demonstrationId, setDemonstrationId] = useState<string | null>(null);
  const [phase, setPhase] = useState<DemonstrationPhase>("idle");
  const [actions, setActions] = useState<DemonstrationAction[]>([]);
  const actionsRef = useRef<DemonstrationAction[]>([]);
  const [actionCount, setActionCount] = useState(0);
  const [sensitiveField, setSensitiveField] =
    useState<WorkflowCopilotDemonstrationSensitiveFieldUpdate | null>(null);
  const [busy, setBusy] = useState(false);
  const [canFillCredentials, setCanFillCredentials] = useState(false);
  const [hasSkippedSensitiveField, setHasSkippedSensitiveField] =
    useState(false);
  const [limitReached, setLimitReached] = useState(false);
  const [browserRestarted, setBrowserRestarted] = useState(false);
  const busyRef = useRef(false);
  const streamControllerRef = useRef<AbortController | null>(null);
  const demonstrationIdRef = useRef<string | null>(null);
  const phaseRef = useRef<DemonstrationPhase>("idle");
  const insertionRef = useRef<WorkflowCopilotDemonstrationInsertion>();
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;
  const onStartFailedRef = useRef(onStartFailed);
  onStartFailedRef.current = onStartFailed;
  const sessionRef = useRef({ workflowPermanentId, browserSessionId });

  const updateBusy = useCallback((next: boolean) => {
    busyRef.current = next;
    setBusy(next);
  }, []);

  const updatePhase = useCallback((next: DemonstrationPhase) => {
    phaseRef.current = next;
    setPhase(next);
  }, []);

  const updateDemonstrationId = useCallback((next: string | null) => {
    demonstrationIdRef.current = next;
    setDemonstrationId(next);
  }, []);

  const clear = useCallback(() => {
    streamControllerRef.current?.abort();
    streamControllerRef.current = null;
    updateDemonstrationId(null);
    updatePhase("idle");
    actionsRef.current = [];
    setActions([]);
    setActionCount(0);
    setSensitiveField(null);
    setCanFillCredentials(false);
    setHasSkippedSensitiveField(false);
    setLimitReached(false);
    setBrowserRestarted(false);
    updateBusy(false);
    insertionRef.current = undefined;
  }, [updateBusy, updateDemonstrationId, updatePhase]);

  const handleStreamFrame = useCallback(
    (frame: WorkflowCopilotDemonstrationUpdate): boolean => {
      switch (frame.type) {
        case "demonstration_started":
          updateDemonstrationId(frame.demonstration_id);
          setActionCount(frame.action_count);
          setCanFillCredentials(frame.can_fill_credentials);
          updatePhase("recording");
          updateBusy(false);
          return false;
        case "demonstration_action":
          actionsRef.current = [
            ...actionsRef.current.filter((action) => action.seq !== frame.seq),
            frame,
          ].sort((left, right) => left.seq - right.seq);
          setActions(actionsRef.current);
          setActionCount((current) => Math.max(current, frame.seq));
          setSensitiveField(null);
          return false;
        case "demonstration_sensitive_field":
          {
            const seq =
              Math.max(0, ...actionsRef.current.map((action) => action.seq)) +
              1;
            const fieldLabel =
              frame.field === "other" ? "sensitive field" : frame.field;
            actionsRef.current = [
              ...actionsRef.current,
              {
                seq,
                label: `Fill ${fieldLabel} (value not recorded)`,
              },
            ];
            setActions(actionsRef.current);
            setActionCount((current) => Math.max(current, seq));
          }
          if (frame.field === "other") {
            setHasSkippedSensitiveField(true);
          }
          setSensitiveField(frame);
          return false;
        case "demonstration_notice":
          if (frame.level === "error") {
            toast({
              title: "Could not update the demonstration",
              description: frame.message,
              variant: "destructive",
            });
          }
          return false;
        case "demonstration_stopped":
          if (frame.demonstration_id) {
            updateDemonstrationId(frame.demonstration_id);
          }
          setActionCount(frame.action_count);
          setSensitiveField(null);
          setHasSkippedSensitiveField(false);
          if (frame.reason === "time_limit") setLimitReached(true);
          if (frame.reason === "event_limit") {
            setLimitReached(true);
            toast({
              title: "This demonstration reached its size limit",
              description:
                "Build the workflow from the captured actions, or discard the demonstration.",
            });
          }
          updatePhase("stopped");
          return true;
      }
    },
    [updateBusy, updateDemonstrationId, updatePhase],
  );

  const openStream = useCallback(
    async (resumeId?: string) => {
      if (!workflowPermanentId || !browserSessionId) return;
      streamControllerRef.current?.abort();
      const controller = new AbortController();
      streamControllerRef.current = controller;
      updateBusy(true);
      if (resumeId) updateDemonstrationId(resumeId);
      try {
        const client = await getSseClient(credentialGetter);
        if (controller.signal.aborted) return;
        await client.postStreaming<WorkflowCopilotDemonstrationUpdate>(
          "/workflow/copilot/demonstration/record",
          {
            workflow_permanent_id: workflowPermanentId,
            browser_session_id: browserSessionId,
            ...(resumeId ? { demonstration_id: resumeId } : {}),
            ...(!resumeId && insertionRef.current
              ? { insertion: insertionRef.current }
              : {}),
          },
          handleStreamFrame,
          { signal: controller.signal },
        );
      } catch (error) {
        if (controller.signal.aborted) return;
        if (demonstrationIdRef.current) {
          updatePhase("stopped");
        } else {
          updatePhase("idle");
          if (!resumeId) onStartFailedRef.current();
        }
        toast({
          title: resumeId
            ? "Could not resume the demonstration"
            : "Could not start the demonstration",
          description:
            error instanceof Error ? error.message : "Please try again.",
          variant: "destructive",
        });
      } finally {
        if (streamControllerRef.current === controller) {
          streamControllerRef.current = null;
          updateBusy(false);
        }
      }
    },
    [
      browserSessionId,
      credentialGetter,
      handleStreamFrame,
      updateBusy,
      updateDemonstrationId,
      updatePhase,
      workflowPermanentId,
    ],
  );

  const start = useCallback(
    ({
      insertion,
    }: { insertion?: WorkflowCopilotDemonstrationInsertion } = {}) => {
      if (busyRef.current) return;
      actionsRef.current = [];
      setActions([]);
      setActionCount(0);
      setSensitiveField(null);
      setCanFillCredentials(false);
      setHasSkippedSensitiveField(false);
      setLimitReached(false);
      insertionRef.current = insertion;
      void openStream();
    },
    [openStream],
  );

  const resume = useCallback(
    (id: string) => {
      if (busyRef.current) return;
      void openStream(id);
    },
    [openStream],
  );

  const sendControl = useCallback(
    async (
      request: Omit<
        WorkflowCopilotDemonstrationControlRequest,
        "demonstration_id"
      >,
      id = demonstrationIdRef.current,
    ) => {
      if (!id) throw new Error("No demonstration is active.");
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response =
        await client.post<WorkflowCopilotDemonstrationControlResult>(
          "/workflow/copilot/demonstration/control",
          { demonstration_id: id, ...request },
        );
      return response.data;
    },
    [credentialGetter],
  );

  const runControl = useCallback(
    async (
      request: Omit<
        WorkflowCopilotDemonstrationControlRequest,
        "demonstration_id"
      >,
    ) => {
      if (busyRef.current) return;
      updateBusy(true);
      try {
        await sendControl(request);
      } catch (error) {
        toast({
          title: "Could not update the demonstration",
          description:
            error instanceof Error ? error.message : "Please try again.",
          variant: "destructive",
        });
      } finally {
        updateBusy(false);
      }
    },
    [sendControl, updateBusy],
  );

  const navigate = useCallback(
    (url: string) => runControl({ action: "navigate", url }),
    [runControl],
  );

  const fillCredential = useCallback(
    (credentialId: string) => {
      if (
        !sensitiveField ||
        (sensitiveField.field !== "password" && sensitiveField.field !== "totp")
      )
        return;
      void runControl({
        action: "fill_credential",
        credential_id: credentialId,
        selector: sensitiveField.selector,
        field: sensitiveField.field,
      });
    },
    [runControl, sensitiveField],
  );

  const dismissSensitiveField = useCallback(() => {
    setSensitiveField(null);
  }, []);

  const done = useCallback(async () => {
    const id = demonstrationIdRef.current;
    if (!id || busyRef.current) return;
    updateBusy(true);
    try {
      if (phaseRef.current === "recording") {
        const stopped = await sendControl({ action: "stop" }, id);
        streamControllerRef.current?.abort();
        streamControllerRef.current = null;
        setSensitiveField(null);
        updatePhase("stopped");
        setHasSkippedSensitiveField(false);
        if (stopped?.action_count === 0) {
          setActionCount(0);
          toast({ title: "No actions captured yet" });
          updateBusy(false);
          return;
        }
      }
      const recordedActions = actionsRef.current;
      // The send resolves only when the whole turn ends; the recording reads as finished from here.
      updatePhase("idle");
      const sent = await onDoneRef.current({
        demonstrationId: id,
        actions: recordedActions,
      });
      if (sent) {
        clear();
      } else {
        updatePhase("stopped");
        updateBusy(false);
      }
    } catch (error) {
      toast({
        title: "Could not finish the demonstration",
        description:
          error instanceof Error ? error.message : "Please try again.",
        variant: "destructive",
      });
      if (phaseRef.current === "idle") updatePhase("stopped");
      updateBusy(false);
    }
  }, [clear, sendControl, updateBusy, updatePhase]);

  const cancel = useCallback(async (): Promise<boolean> => {
    const id = demonstrationIdRef.current;
    if (!id || busyRef.current) return false;
    updateBusy(true);
    try {
      await sendControl({ action: "cancel" }, id);
    } catch (error) {
      const status = isAxiosError(error) ? error.response?.status : undefined;
      if (status === 404 || status === 409) {
        clear();
        return true;
      }
      toast({
        title: "Could not discard the demonstration",
        description:
          error instanceof Error ? error.message : "Please try again.",
        variant: "destructive",
      });
      updateBusy(false);
      return false;
    }
    clear();
    return true;
  }, [clear, sendControl, updateBusy]);

  useEffect(() => {
    const previous = sessionRef.current;
    const sameWorkflow = previous.workflowPermanentId === workflowPermanentId;
    // A stream readiness gap reports a null session, so only a different real session is a restart.
    sessionRef.current = {
      workflowPermanentId,
      browserSessionId:
        browserSessionId ?? (sameWorkflow ? previous.browserSessionId : null),
    };
    const id = demonstrationIdRef.current;
    const restarted =
      !!browserSessionId && browserSessionId !== previous.browserSessionId;
    // Done keeps the recording (phase idle, busy) until its send settles.
    if (sameWorkflow && id && phaseRef.current === "idle" && busyRef.current) {
      if (restarted) setBrowserRestarted(true);
      return;
    }
    if (sameWorkflow && id && phaseRef.current !== "idle") {
      // Early returns register no cleanup, and an aborted stream skips its busy reset once its ref is gone.
      const resuming =
        streamControllerRef.current !== null &&
        phaseRef.current !== "recording";
      streamControllerRef.current?.abort();
      streamControllerRef.current = null;
      if (resuming) updateBusy(false);
      setSensitiveField(null);
      if (!restarted) {
        updatePhase("stopped");
        return;
      }
      // The recording is bound to the old session: build or discard only.
      const wasRecording = phaseRef.current === "recording";
      setHasSkippedSensitiveField(false);
      setBrowserRestarted(true);
      updatePhase("stopped");
      if (wasRecording) {
        updateBusy(true);
        void sendControl({ action: "stop", stop_reason: "browser_gone" }, id)
          .catch(() => undefined)
          .finally(() => {
            if (demonstrationIdRef.current === id) updateBusy(false);
          });
      }
      return;
    }
    clear();
    if (!workflowPermanentId || !browserSessionId) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response =
          await client.get<WorkflowCopilotActiveDemonstration | null>(
            "/workflow/copilot/demonstration/active",
            {
              params: {
                workflow_permanent_id: workflowPermanentId,
                browser_session_id: browserSessionId,
              },
              signal: controller.signal,
            },
          );
        if (
          !response.data ||
          typeof response.data.demonstration_id !== "string" ||
          typeof response.data.action_count !== "number" ||
          (response.data.status !== "recording" &&
            response.data.status !== "stopping" &&
            response.data.status !== "stopped") ||
          controller.signal.aborted
        )
          return;
        updateDemonstrationId(response.data.demonstration_id);
        setActionCount(response.data.action_count);
        actionsRef.current = response.data.action_labels.map(
          (label, index) => ({
            seq: index + 1,
            label,
          }),
        );
        setActions(actionsRef.current);
        setCanFillCredentials(response.data.can_fill_credentials);
        setLimitReached(!response.data.can_resume);
        insertionRef.current = response.data.insertion ?? undefined;
        updatePhase("stopped");
      } catch {
        return;
      }
    })();
    return () => {
      controller.abort();
      streamControllerRef.current?.abort();
    };
  }, [
    browserSessionId,
    clear,
    credentialGetter,
    sendControl,
    updateBusy,
    updateDemonstrationId,
    updatePhase,
    workflowPermanentId,
  ]);

  useEffect(
    () => () => {
      streamControllerRef.current?.abort();
    },
    [],
  );

  return {
    demonstrationId,
    phase,
    actions,
    actionCount,
    sensitiveField,
    canFillCredentials,
    hasSkippedSensitiveField,
    limitReached,
    browserRestarted,
    busy,
    start,
    resume,
    navigate,
    fillCredential,
    dismissSensitiveField,
    done,
    cancel,
  };
}
