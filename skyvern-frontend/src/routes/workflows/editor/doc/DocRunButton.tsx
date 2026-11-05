import { useEffect, useRef, useState } from "react";
import { DOC_RUN_AGENT_EVENT } from "./docKeys";
import { PlayIcon, ReloadIcon, StopIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, Status } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
} from "@/components/ui/popover";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";
import { useDocUnfinishedStore } from "./docUnfinished";
import { isMaskedHeaders } from "@/util/secretHeaders";
import { cn } from "@/util/utils";

import {
  getRunWorkflowRequestBody,
  type RunWorkflowFormType,
} from "../../RunWorkflowForm";
import { WorkflowParameterInput } from "../../WorkflowParameterInput";
import { RESERVED_BROWSER_TYPE_FIELD } from "../../utils";
import { useWorkflowPermanentId } from "../../WorkflowPermanentIdContext";
import type {
  WorkflowApiResponse,
  WorkflowParameter,
} from "../../types/workflowTypes";
import { claimRunCompletionNotice } from "../../workflowRun/runCompletionNotices";
import { liveSearch } from "../../studio/liveSearch";
import { searchWithoutRun } from "../../studio/runSwitchNavigation";
import { ControlTooltip } from "../../studio/ControlTooltip";
import { useStudioPanes } from "../../studio/useStudioPanes";
import { getRunBlockingTooltipText } from "../runValidation/runBlockingCopy";
import { useRunValidationStore } from "../runValidation/useRunValidationStore";
import { useStudioRunId } from "../../studio/useStudioRunId";
import { docRunEndMessage, docRunRowsSettled } from "./docRunState";
import { useDocRunStore, type DocRunInput } from "./docRunStore";
import { docLatestRunKey, useDocRun } from "./useDocRun";

class DocSaveFailedError extends Error {}

const isEmpty = (value: unknown) =>
  value === null || value === undefined || value === "";

// The agent's saved run settings, as the full run form seeds them; only the inputs come from the popover.
function savedRunValues(
  workflow: WorkflowApiResponse,
  data: Record<string, unknown>,
  browserMemoryEnabled: boolean,
): RunWorkflowFormType {
  return {
    ...data,
    webhookCallbackUrl: workflow.webhook_callback_url ?? "",
    proxyLocation: workflow.proxy_location ?? ProxyLocation.Residential,
    browserSessionId: null,
    reuseBrowserSession: null,
    browserProfileId: browserMemoryEnabled
      ? null
      : (workflow.browser_profile_id ?? null),
    startFreshBrowser: false,
    cdpAddress: null,
    maxScreenshotScrolls: workflow.max_screenshot_scrolls ?? null,
    extraHttpHeaders: workflow.extra_http_headers
      ? JSON.stringify(workflow.extra_http_headers)
      : null,
    cdpConnectHeaders:
      workflow.cdp_connect_headers &&
      !isMaskedHeaders(workflow.cdp_connect_headers)
        ? JSON.stringify(workflow.cdp_connect_headers)
        : null,
    runWith: workflow.run_with === "code" ? "code" : "agent",
    [RESERVED_BROWSER_TYPE_FIELD]: workflow.browser_type ?? null,
    aiFallback: workflow.ai_fallback ?? true,
  };
}

function DocRunInputField({
  input,
  value,
  needed,
  onChange,
  inputRef,
}: {
  input: DocRunInput;
  value: unknown;
  needed: boolean;
  onChange: (value: unknown) => void;
  inputRef: (el: HTMLElement | null) => void;
}) {
  const id = `doc-run-input-${input.key}`;
  const errorId = `${id}-error`;
  const plain = input.type === "string";
  const number = input.type === "integer" || input.type === "float";
  return (
    <div className="grid gap-1">
      <label htmlFor={id} className="text-[13px] font-medium">
        {input.key}
      </label>
      {plain || number ? (
        <Input
          id={id}
          ref={inputRef as (el: HTMLInputElement | null) => void}
          type={number ? "number" : "text"}
          step={input.type === "float" ? "any" : undefined}
          value={isEmpty(value) ? "" : String(value)}
          aria-invalid={needed || undefined}
          aria-describedby={needed ? errorId : undefined}
          onChange={(event) => {
            const raw = event.target.value;
            onChange(
              !number || raw === ""
                ? raw
                : input.type === "integer"
                  ? parseInt(raw, 10)
                  : parseFloat(raw),
            );
          }}
          className={cn(
            "h-8",
            needed && "border-[#B45B0A] dark:border-[#E79A55]",
          )}
        />
      ) : (
        <div
          id={id}
          ref={inputRef}
          tabIndex={-1}
          className={cn(
            "rounded-md outline-none",
            needed && "ring-1 ring-[#B45B0A] dark:ring-[#E79A55]",
          )}
        >
          <WorkflowParameterInput
            type={input.type}
            value={value}
            onChange={onChange}
          />
        </div>
      )}
      {needed && (
        <p id={errorId} className="text-xs text-[#B45B0A] dark:text-[#E79A55]">
          Needed to run
        </p>
      )}
    </div>
  );
}

/** The Doc's top-bar Run: a "Run with" popover, then a real run that stays in the Doc. */
export function DocRunButton() {
  const workflowPermanentId = useWorkflowPermanentId();
  const orgId = useActiveOrgId();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const browserMemoryEnabled = Boolean(useFeatureFlag("browser_memory_v1"));
  const saveWorkflow = useWorkflowSave();
  const { resolveLivePanes, preserveNextEntry } = useStudioPanes();
  const urlRunId = useStudioRunId();
  const {
    runId,
    sharedRunId,
    run,
    blocksByLabel,
    loopsByLabel,
    live,
    owned,
    blockRun,
  } = useDocRun();
  const inputs = useDocRunStore((s) => s.inputs);
  const blockingBlocks = useRunValidationStore((s) => s.blockingBlocks);
  const docProblemCount = useRunValidationStore((s) => s.docProblemCount);
  // A step still being set up does not hold Run: pressing it reveals the problem instead.
  const unfinishedIds = useDocUnfinishedStore((s) => s.ids);
  const runBlocked =
    blockingBlocks.some((block) => !unfinishedIds.has(block.id)) &&
    docProblemCount === 0;
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [needed, setNeeded] = useState<Set<string>>(new Set());
  const fields = useRef(new Map<string, HTMLElement | null>());
  const submitRef = useRef<HTMLButtonElement>(null);

  const stayInDoc = (search: string) => {
    preserveNextEntry(search, resolveLivePanes());
    navigate({ search }, { replace: true });
  };

  const start = useMutation({
    mutationKey: ["docRun", workflowPermanentId],
    mutationFn: async (data: Record<string, unknown>) => {
      // Never run an older saved version than the doc on screen.
      if (useWorkflowHasChangesStore.getState().hasChanges) {
        try {
          await saveWorkflow.mutateAsync(undefined);
        } catch {
          throw new DocSaveFailedError();
        }
        if (useWorkflowHasChangesStore.getState().hasChanges)
          throw new Error("Save the agent before running");
      }
      const client = await getClient(credentialGetter);
      const workflow = (
        await client.get<WorkflowApiResponse>(
          `/workflows/${workflowPermanentId}`,
        )
      ).data;
      const parameters = workflow.workflow_definition.parameters.filter(
        (p): p is WorkflowParameter => p.parameter_type === "workflow",
      );
      const body = getRunWorkflowRequestBody(
        savedRunValues(workflow, data, browserMemoryEnabled),
        parameters,
        workflow.browser_profile_key,
        browserMemoryEnabled,
      );
      const response = await client.post<{ workflow_run_id: string }>(
        `/workflows/${workflowPermanentId}/run`,
        body,
      );
      return response.data.workflow_run_id;
    },
    onSuccess: (newRunId) => {
      queryClient.invalidateQueries({ queryKey: ["workflowRuns"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      if (workflowPermanentId) {
        useDocRunStore
          .getState()
          .setDocRunId(orgId, workflowPermanentId, newRunId);
        queryClient.setQueryData(docLatestRunKey(orgId, workflowPermanentId), [
          newRunId,
        ]);
      }
      // The run lives in the Doc's remembered run, not ?wr=: a ?wr= URL reloads into the run view.
      // A run already in the URL would outrank the new one, so drop it.
      if (urlRunId) stayInDoc(searchWithoutRun(liveSearch(location.search)));
    },
    onError: (
      error: Error & { response?: { data?: { detail?: unknown } } },
    ) => {
      // A failed save already showed its own reason.
      if (error instanceof DocSaveFailedError) return;
      const detail = error.response?.data?.detail;
      toast({
        variant: "destructive",
        title: "Couldn't start the run",
        description: typeof detail === "string" ? detail : error.message,
      });
    },
  });

  const stop = useMutation({
    mutationKey: ["docRunStop", runId],
    mutationFn: async (id: string) => {
      const client = await getClient(credentialGetter);
      await client.post(`/workflows/runs/${id}/cancel`);
      return id;
    },
    onSuccess: (id) => {
      queryClient.invalidateQueries({ queryKey: ["workflowRun", id] });
      queryClient.invalidateQueries({ queryKey: ["workflowRuns"] });
    },
    onError: (error: Error) =>
      toast({
        variant: "destructive",
        title: "Couldn't stop the run",
        description: error.message,
      }),
  });

  // One toast when the Doc's run settles, only for a run seen going. The run's status can land a
  // poll before its step rows do, so wait for the rows (or a few seconds) to name the right ending.
  const seenLive = useRef(new Set<string>());
  const latest = useRef({ run, blocksByLabel, loopsByLabel });
  latest.current = { run, blocksByLabel, loopsByLabel };
  useEffect(() => {
    // A one-step run keeps the step's own notice. A run named in the URL gets the studio's shared
    // notice, and Copilot reports its own test runs in the chat.
    if (
      !runId ||
      !run ||
      !owned ||
      blockRun ||
      urlRunId ||
      run.copilot_session_id
    )
      return;
    if (live) {
      seenLive.current.add(runId);
      return;
    }
    if (!seenLive.current.has(runId)) return;
    const announce = () => {
      if (!seenLive.current.delete(runId)) return;
      const now = latest.current;
      const message = docRunEndMessage(
        now.run,
        now.blocksByLabel,
        now.loopsByLabel,
      );
      if (!message) return;
      claimRunCompletionNotice(runId, () => {});
      toast({
        title: message,
        variant: message.startsWith("Run failed") ? "destructive" : undefined,
      });
    };
    // A canceled run's rows can stay "running"; its ending needs no rows to name. Otherwise wait
    // for the rows (they re-run this effect when they land), with a bounded fallback.
    if (run.status === Status.Canceled || docRunRowsSettled(blocksByLabel)) {
      announce();
      return;
    }
    const timer = window.setTimeout(announce, 5000);
    return () => window.clearTimeout(timer);
  }, [
    runId,
    run,
    live,
    owned,
    blockRun,
    urlRunId,
    blocksByLabel,
    loopsByLabel,
  ]);

  const openPopover = () => {
    const state = useDocRunStore.getState();
    if (state.stepCount === 0) {
      toast({ title: "Add a step first: type / on a line." });
      return;
    }
    // Same as the shared Run: while the Doc lists problems, a click explains and jumps to the first.
    const { docProblemCount, docShowFirstProblem } =
      useRunValidationStore.getState();
    if (docProblemCount > 0) {
      toast({
        title: `Fix ${docProblemCount} problem${docProblemCount === 1 ? "" : "s"} before running`,
      });
      docShowFirstProblem?.();
      return;
    }
    setValues((current) =>
      Object.fromEntries(
        state.inputs.map((input) => [
          input.key,
          input.key in current ? current[input.key] : input.defaultValue,
        ]),
      ),
    );
    setNeeded(new Set());
    setOpen(true);
  };

  const missing = (key: string) => {
    const input = inputs.find((i) => i.key === key);
    return Boolean(input?.required && isEmpty(values[key]));
  };

  const run_ = () => {
    const blanks = inputs.filter((i) => missing(i.key)).map((i) => i.key);
    if (blanks.length) {
      setNeeded(new Set(blanks));
      fields.current.get(blanks[0]!)?.focus();
      return;
    }
    setOpen(false);
    start.mutate(values);
  };

  const moreOptions = () => {
    setOpen(false);
    // Typed values ride in history state, never the URL.
    navigate(`/agents/${workflowPermanentId}/run`, { state: { data: values } });
  };

  const clearResults = () => {
    // Clear hides the run every browser shares, even while a ?wr= run is on screen.
    if (workflowPermanentId)
      useDocRunStore
        .getState()
        .clearDocRun(orgId, workflowPermanentId, sharedRunId ?? runId);
    if (urlRunId) stayInDoc(searchWithoutRun(liveSearch(location.search)));
  };

  const runShortcut = useRef<() => void>(() => {});
  runShortcut.current = () => {
    if (open) run_();
    else if (!start.isPending && !runBlocked && !(live && owned && runId))
      openPopover();
  };
  useEffect(() => {
    const onRun = () => runShortcut.current();
    window.addEventListener(DOC_RUN_AGENT_EVENT, onRun);
    return () => window.removeEventListener(DOC_RUN_AGENT_EVENT, onRun);
  }, []);

  if (live && owned && runId)
    return (
      <Button
        variant="destructive"
        className="h-8 px-3"
        disabled={stop.isPending}
        onClick={() => stop.mutate(runId)}
      >
        {stop.isPending ? (
          <ReloadIcon className="mr-2 size-4 animate-spin" />
        ) : (
          <StopIcon className="mr-2 size-4" />
        )}
        Stop
      </Button>
    );

  return (
    <div className="flex items-center gap-2">
      {runId && blocksByLabel.size > 0 && (
        <Button
          variant="outline"
          className="h-8 px-3 text-xs"
          onClick={clearResults}
        >
          Clear results
        </Button>
      )}
      <Popover open={open} onOpenChange={setOpen}>
        <ControlTooltip
          content={
            runBlocked ? getRunBlockingTooltipText(blockingBlocks) : "Run"
          }
          blocked={runBlocked}
        >
          <PopoverAnchor asChild>
            <Button
              className="h-8 border border-transparent px-3"
              aria-haspopup="dialog"
              aria-expanded={open}
              disabled={start.isPending || runBlocked}
              onClick={() => (open ? setOpen(false) : openPopover())}
            >
              {start.isPending ? (
                <ReloadIcon className="mr-2 size-4 animate-spin" />
              ) : (
                <PlayIcon className="mr-2 size-4" />
              )}
              Run
            </Button>
          </PopoverAnchor>
        </ControlTooltip>
        <PopoverContent
          align="end"
          role="dialog"
          aria-label="Run with"
          className="w-[320px] max-w-[calc(100vw-32px)] p-4"
          onOpenAutoFocus={(event) => {
            const first = inputs.find((i) => missing(i.key));
            const target = first
              ? fields.current.get(first.key)
              : fields.current.get(inputs[0]?.key ?? "");
            if (target) {
              event.preventDefault();
              target.focus();
            } else {
              // No inputs: Enter should run, not press whichever button comes first.
              event.preventDefault();
              submitRef.current?.focus();
            }
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              run_();
            }}
          >
            <h3 className="mb-3 text-sm font-semibold">Run with</h3>
            {inputs.length === 0 ? (
              <p className="mb-3 text-[13px] text-muted-foreground">
                No inputs are used in this doc.
              </p>
            ) : (
              <div className="mb-3 grid max-h-[50vh] gap-3 overflow-y-auto">
                {inputs.map((input) => (
                  <DocRunInputField
                    key={input.key}
                    input={input}
                    value={values[input.key]}
                    needed={
                      (needed.has(input.key) || input.required) &&
                      missing(input.key)
                    }
                    inputRef={(el) => fields.current.set(input.key, el)}
                    onChange={(value) =>
                      setValues((current) => ({
                        ...current,
                        [input.key]: value,
                      }))
                    }
                  />
                ))}
              </div>
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={moreOptions}
                className="mr-auto text-xs text-muted-foreground underline-offset-2 outline-none hover:text-foreground hover:underline focus-visible:ring-2 focus-visible:ring-ring"
              >
                More options
              </button>
              <Button
                type="button"
                variant="secondary"
                className="h-8 px-3"
                onClick={() => setOpen(false)}
              >
                Cancel
              </Button>
              <Button type="submit" ref={submitRef} className="h-8 px-3">
                Run
              </Button>
            </div>
          </form>
        </PopoverContent>
      </Popover>
    </div>
  );
}
