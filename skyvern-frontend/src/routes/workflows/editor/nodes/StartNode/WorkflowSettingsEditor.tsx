import { usePageSlots } from "@/store/PageSlots";
import { useWorkflowScopeReadOnly } from "../../WorkflowScopeContext";
import { RetryPolicyEditor } from "./RetryPolicyEditor";
import { collectKnownErrorCodes } from "./retryPolicyUtils";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type SyntheticEvent,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { ProxyLocation, type ModelsResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { cn } from "@/util/utils";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { BrowserProfileSelector } from "@/routes/workflows/components/BrowserProfileSelector";
import { HelpTooltip } from "@/components/HelpTooltip";
import { KeyValueInput } from "@/components/KeyValueInput";
import { ModelSelector } from "@/components/ModelSelector";
import { getModelDisplayLabel } from "@/components/modelLabels";
import { ProxySelector } from "@/components/ProxySelector";
import { TestWebhookDialog } from "@/components/TestWebhookDialog";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

import {
  hasBrowserTypeOptions,
  useBrowserTypeOptionsQuery,
} from "@/routes/workflows/utils";
import { useResetProfileMutation } from "@/routes/workflows/hooks/useResetProfileMutation";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { updateWorkflowBrowserSessionReuse } from "@/store/WorkflowSettingsStore";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { BrowserProfileSettingsSection } from "./BrowserProfileSettingsSection";

import {
  buildGroupSummaries,
  type SummarySegment,
} from "./agentSettingsSummary";
import { placeholders } from "../../helpContent";
import { useUpdate } from "../../useUpdate";
import { getWorkflowBlocks } from "../../workflowEditorUtils";
import { type AppNode } from "..";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "../Taskv2Node/types";
import {
  isWorkflowStartNodeData,
  type StartNode,
  type WorkflowStartNodeData,
} from "./types";

const PREVENT_OVERLAPPING_RUNS_TOOLTIP =
  "Queues new runs of this agent until any in-progress run finishes. Does not affect block ordering inside a single run; blocks always execute in declared order. Use this when concurrent runs would collide on shared state, such as the same credentials, browser session, or downstream account.";

const MASK_SECRETS_TOOLTIP =
  "Mask secret values in this workflow's runs. Secrets are hidden while they are typed (screenshots, recordings, live browser view) and redacted from stored artifacts, network logs, and LLM prompts. Turning this on can make debugging harder because secret values are hidden.";

const SEQUENTIAL_KEY_TOOLTIP =
  "Scope the run queue. Runs with the same key are queued together; runs with different keys can still execute in parallel. Templated against agent inputs, for example {{ account_id }} to serialize per account.";

const BROWSER_PROFILE_KEY_TOOLTIP =
  "Template for separating saved browser profiles. Use + to insert an agent input, or type a static key. Runs with the same rendered value reuse the same saved profile.";

const PIN_SAVED_SESSION_IP_TOOLTIP =
  "Pin this workflow's saved sessions to a consistent proxy IP across runs, so restored logins are not invalidated by IP changes. Requires the Residential (ISP) proxy location. With a Browser Profile Key, each saved profile keeps its own IP.";

const WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES = 4 * 60;
const WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES = 8 * 60;

function SummaryLine({ segments }: { segments: Array<SummarySegment> }) {
  return (
    <span
      className="block max-w-full break-words text-xs font-normal text-muted-foreground"
      title={segments
        .map((segment) => segment.title ?? segment.text)
        .join(" · ")}
    >
      {segments.map((segment, index) => (
        <span key={index}>
          {index > 0 ? " · " : ""}
          <span
            className={
              segment.changed ? "font-medium text-foreground" : undefined
            }
          >
            {segment.text}
          </span>
        </span>
      ))}
    </span>
  );
}

// Only the controls lock; group triggers stay clickable so values remain readable.
function SettingsFields({
  locked,
  className,
  children,
}: {
  locked: boolean;
  className?: string;
  children: ReactNode;
}) {
  // Portal controls remain in this React subtree even outside the disabled fieldset.
  const preventLockedInteraction = (event: SyntheticEvent) => {
    if (!locked) return;
    event.preventDefault();
    event.stopPropagation();
  };
  return (
    <fieldset
      className={cn("m-0 min-w-0 space-y-4 border-0 p-0", className)}
      disabled={locked}
      {...{ inert: locked ? "" : undefined }}
      onPointerDownCapture={preventLockedInteraction}
      onPointerUpCapture={preventLockedInteraction}
      onClickCapture={preventLockedInteraction}
      onKeyDownCapture={preventLockedInteraction}
      onChangeCapture={preventLockedInteraction}
    >
      {children}
    </fieldset>
  );
}

function SettingsGroup({
  id,
  title,
  summary,
  locked,
  children,
}: {
  id: string;
  title: string;
  summary: Array<SummarySegment>;
  locked: boolean;
  children: ReactNode;
}) {
  return (
    <AccordionItem value={id} data-testid={`settings-group-${id}`}>
      <AccordionTrigger className="min-w-0 items-start py-3 hover:no-underline [&>svg]:mt-0.5">
        <span className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left">
          <span className="text-sm font-medium">{title}</span>
          <SummaryLine segments={summary} />
        </span>
      </AccordionTrigger>
      <AccordionContent className="pl-6 pr-1 pt-1">
        <SettingsFields locked={locked}>{children}</SettingsFields>
      </AccordionContent>
    </AccordionItem>
  );
}

function WorkflowSettingsEditor({
  blockId,
  locked = false,
}: {
  blockId: string;
  locked?: boolean;
}) {
  // Subscribe to the start node's data slice. The sidebar mount lives
  // outside the per-node renderer, and the body also subscribes to
  // useNodes()/useEdges() for terminal-block enumeration; a one-time
  // getNode() snapshot would re-render with stale data after useUpdate
  // commits typed input (model selector, proxy, run mode, etc. would snap
  // back).
  const nodeSlice = useNodesData<StartNode>(blockId);
  if (
    !nodeSlice ||
    nodeSlice.type !== "start" ||
    !isWorkflowStartNodeData(nodeSlice.data)
  ) {
    return null;
  }
  return (
    <WorkflowSettingsEditorBody
      blockId={blockId}
      data={nodeSlice.data}
      forceLocked={locked}
    />
  );
}

function WorkflowSettingsEditorBody({
  blockId,
  data,
  forceLocked,
}: {
  blockId: string;
  data: WorkflowStartNodeData;
  forceLocked: boolean;
}) {
  const workflowPermanentId = useWorkflowPermanentId();
  const { workflowCuratorSettings: CuratorSettings } = usePageSlots();
  const nodes = useNodes<AppNode>();
  const readOnly = useWorkflowScopeReadOnly();
  const storeLocked = useWorkflowYamlEditorStore(selectEditorMutationLocked);
  const mutationLocked = storeLocked || forceLocked;
  const knownErrorCodes = useMemo(() => collectKnownErrorCodes(nodes), [nodes]);
  const edges = useEdges();
  const update = useUpdate<StartNode["data"]>({
    id: blockId,
    editable: true,
  });
  const browserMemoryEnabled = useFeatureFlag("browser_memory_v1");
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const { data: browserTypeOptions } = useBrowserTypeOptionsQuery();
  const persistBrowserSessionEnabled = data.persistBrowserSession;
  const credentialGetter = useCredentialGetter();
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const { data: availableModels } = useQuery<ModelsResponse>({
    queryKey: ["models"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get("/models").then((res) => res.data);
    },
    enabled: !scopeReadOnly,
  });
  const summaries = buildGroupSummaries(data, {
    modelLabel: getModelDisplayLabel(
      availableModels?.models,
      data.model?.model_name,
    ),
    browserTypeOptions,
  });

  const [localWebhookUrl, setLocalWebhookUrl] = useState(
    data.webhookCallbackUrl,
  );
  const prevWebhookUrl = useRef(data.webhookCallbackUrl);
  const [isResetProfileDialogOpen, setIsResetProfileDialogOpen] =
    useState(false);
  const resetProfileMutation = useResetProfileMutation({
    workflowPermanentId,
    onSuccess: () => setIsResetProfileDialogOpen(false),
  });

  useEffect(() => {
    const parentChanged = data.webhookCallbackUrl !== prevWebhookUrl.current;
    const isExternalChange =
      parentChanged && localWebhookUrl === prevWebhookUrl.current;
    if (isExternalChange) {
      setLocalWebhookUrl(data.webhookCallbackUrl);
    }
    prevWebhookUrl.current = data.webhookCallbackUrl;
  }, [data.webhookCallbackUrl, localWebhookUrl]);

  const terminalBlockLabels = useMemo(() => {
    return getWorkflowBlocks(nodes, edges)
      .filter((block) => (block.next_block_label ?? null) === null)
      .map((block) => block.label);
  }, [nodes, edges]);
  const terminalBlockLabelSet = useMemo(
    () => new Set(terminalBlockLabels),
    [terminalBlockLabels],
  );
  useEffect(() => {
    if (
      !mutationLocked &&
      data.finallyBlockLabel &&
      !terminalBlockLabelSet.has(data.finallyBlockLabel)
    ) {
      update({ finallyBlockLabel: null }, { source: "workflow" });
    }
  }, [data.finallyBlockLabel, terminalBlockLabelSet, update, mutationLocked]);

  return (
    <div data-testid="workflow-settings-block-form" className="nopan nodrag">
      {CuratorSettings && workflowPermanentId && (
        <SettingsFields locked={mutationLocked} className="mb-2">
          <CuratorSettings
            workflowPermanentId={workflowPermanentId}
            readOnly={readOnly || !data.editable || mutationLocked}
          />
        </SettingsFields>
      )}
      <Accordion type="multiple" className="-mt-2">
        <SettingsGroup
          id="model"
          title="Model"
          summary={summaries.model}
          locked={mutationLocked}
        >
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label>Model</Label>
              <HelpTooltip content="The default LLM used for every block in this agent that doesn't override it." />
            </div>
            <ModelSelector
              hideLabel
              className="nopan w-52 text-xs"
              value={data.model}
              onChange={(value) => update({ model: value })}
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>Agent System Prompt</Label>
              <HelpTooltip content="Applied to every LLM call in this agent, including any sub-agents." />
            </div>
            <WorkflowBlockInputTextarea
              name="workflowSystemPrompt"
              nodeId={blockId}
              onChange={(value) =>
                update({
                  workflowSystemPrompt: value.length ? value : null,
                })
              }
              value={data.workflowSystemPrompt ?? ""}
              placeholder="e.g. Format all dates as YYYY-MM-DD and all currency values as USD with two decimals."
              className="nopan text-xs"
            />
          </div>
        </SettingsGroup>
        <SettingsGroup
          id="runs"
          title="Runs"
          summary={summaries.runs}
          locked={mutationLocked}
        >
          <RetryPolicyEditor
            value={data.retryPolicy ?? null}
            onChange={(retryPolicy) => update({ retryPolicy })}
            knownErrorCodes={knownErrorCodes}
            readOnly={readOnly || !data.editable}
          />
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>Max Run Time (minutes)</Label>
              <HelpTooltip content="Times out this workflow after the configured elapsed runtime. Leave blank to use the platform default of 4 hours. Maximum is 8 hours." />
            </div>
            <Input
              value={data.maxElapsedTimeMinutes ?? ""}
              placeholder={`Default: ${WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES} minutes`}
              type="number"
              min={1}
              max={WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES}
              step={1}
              onChange={(event) => {
                const rawValue = event.target.value;
                const parsedValue = Number(rawValue);
                const value =
                  rawValue === "" || !Number.isFinite(parsedValue)
                    ? null
                    : Math.min(
                        WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES,
                        Math.max(1, Math.trunc(parsedValue)),
                      );
                update({ maxElapsedTimeMinutes: value });
              }}
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>Execute on Any Outcome</Label>
              <HelpTooltip content="Select a block that will always run after the agent completes, whether it succeeds, fails, or terminates early. Useful for cleanup tasks like logging out." />
            </div>
            <Select
              value={data.finallyBlockLabel ?? "none"}
              onValueChange={(value) =>
                update({ finallyBlockLabel: value === "none" ? null : value })
              }
            >
              <SelectTrigger>
                <SelectValue placeholder="None" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                {terminalBlockLabels.map((label) => (
                  <SelectItem key={label} value={label}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </SettingsGroup>
        <SettingsGroup
          id="safety"
          title="Safety"
          summary={summaries.safety}
          locked={mutationLocked}
        >
          <div className="flex flex-col gap-4">
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Label>Mask Secrets</Label>
                <HelpTooltip content={MASK_SECRETS_TOOLTIP} />
                <Switch
                  className="ml-auto"
                  checked={data.maskSecrets}
                  onCheckedChange={(value) => update({ maskSecrets: value })}
                />
              </div>
            </div>
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Label>Prevent Overlapping Runs</Label>
                <HelpTooltip content={PREVENT_OVERLAPPING_RUNS_TOOLTIP} />
                <Switch
                  className="ml-auto"
                  checked={data.runSequentially}
                  onCheckedChange={(value) =>
                    update({
                      runSequentially: value,
                      sequentialKey: value ? data.sequentialKey : null,
                    })
                  }
                />
              </div>
            </div>
            {data.runSequentially && (
              <div className="flex flex-col gap-4 rounded-md bg-slate-elevation4 p-4 pl-4">
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label>Sequential Key (optional)</Label>
                    <HelpTooltip content={SEQUENTIAL_KEY_TOOLTIP} />
                  </div>
                  <WorkflowBlockInputTextarea
                    name="sequentialKey"
                    nodeId={blockId}
                    onChange={(value) => {
                      const v = value.length ? value : null;
                      update({ sequentialKey: v });
                    }}
                    value={data.sequentialKey ?? ""}
                    placeholder={placeholders["sequentialKey"]}
                    className="nopan text-xs"
                  />
                </div>
              </div>
            )}
          </div>
        </SettingsGroup>
        <SettingsGroup
          id="browser"
          title="Browser"
          summary={summaries.browser}
          locked={mutationLocked}
        >
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label>Proxy Location</Label>
              <HelpTooltip content="Route Skyvern through one of our available proxies." />
            </div>
            <ProxySelector
              value={data.proxyLocation ?? ProxyLocation.Residential}
              onChange={(value) => update({ proxyLocation: value })}
            />
          </div>
          {hasBrowserTypeOptions(browserTypeOptions) ? (
            <div className="space-y-2">
              <div className="flex gap-2">
                <Label>Browser Type</Label>
                <HelpTooltip content="Browser engine used for this agent's runs. Leave as Default to let Skyvern choose. A workflow-run setting overrides this. Note: Google Chrome does not support the captcha-solver extension." />
              </div>
              <Select
                value={data.browserType ?? "default"}
                onValueChange={(value) =>
                  update({ browserType: value === "default" ? null : value })
                }
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Browser Type" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="default">Default</SelectItem>
                  {browserTypeOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : null}
          <div className="space-y-2">
            <div className="flex items-start gap-4">
              <div className="flex-1 space-y-1">
                <div className="flex items-center gap-2">
                  <Label>Reuse browser session</Label>
                  <HelpTooltip content="Consecutive runs of this workflow continue in one live browser — cookies, tabs, and logged-in state carry over. If the browser dies, the next run starts a fresh one from the saved profile. Turning this on also saves the browser profile. Without credentials, a profile key, or a sequential key, all runs share one browser and its signed-in state — treat the workflow as single-account." />
                </div>
                <p className="text-xs text-muted-foreground">
                  Consecutive runs continue in the same live browser.
                </p>
              </div>
              <Switch
                className="shrink-0"
                checked={data.reuseBrowserSession}
                onCheckedChange={(value) =>
                  updateWorkflowBrowserSessionReuse(
                    value,
                    data.persistBrowserSession,
                    update,
                  )
                }
              />
            </div>
            {!browserMemoryEnabled && (
              <div className="flex items-center gap-2">
                <Label>Save &amp; reuse browser profile</Label>
                <HelpTooltip content="Persist session information across agent runs" />
                <Switch
                  className="ml-auto"
                  checked={persistBrowserSessionEnabled}
                  disabled={data.reuseBrowserSession}
                  onCheckedChange={(value) =>
                    update({
                      persistBrowserSession: value,
                      pinSavedSessionIp: value ? data.pinSavedSessionIp : false,
                      browserProfileKey: value ? data.browserProfileKey : null,
                    })
                  }
                />
              </div>
            )}
            {browserMemoryEnabled && (
              <BrowserProfileSettingsSection
                blockId={blockId}
                data={data}
                update={update}
                agentName={workflow?.title ?? undefined}
              />
            )}
            {!browserMemoryEnabled && persistBrowserSessionEnabled && (
              <div className="flex flex-col gap-3 rounded-md bg-slate-elevation4 p-4 pl-4">
                <div className="flex items-center gap-2">
                  <Label>Keep Same IP Across Runs</Label>
                  <HelpTooltip content={PIN_SAVED_SESSION_IP_TOOLTIP} />
                  <Switch
                    className="ml-auto"
                    checked={data.pinSavedSessionIp}
                    onCheckedChange={(value) =>
                      update({ pinSavedSessionIp: value })
                    }
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <Label>Browser Profile Key (optional)</Label>
                    <HelpTooltip content={BROWSER_PROFILE_KEY_TOOLTIP} />
                  </div>
                  <div className="flex flex-col gap-2">
                    <WorkflowBlockInputTextarea
                      name="browserProfileKey"
                      nodeId={blockId}
                      onChange={(value) => {
                        update({
                          browserProfileKey: value.length ? value : null,
                        });
                      }}
                      value={data.browserProfileKey ?? ""}
                      placeholder="{{ credential_id }}"
                      className="nopan text-xs"
                      data-testid="browser-profile-key-template"
                    />
                    <p className="text-xs text-muted-foreground">
                      Use + to insert an input like {"{{ credential_id }}"}.
                      Leave empty to use one saved profile for this agent.
                    </p>
                    {!data.runSequentially && data.browserProfileKey && (
                      <p className="text-xs text-amber-700 dark:text-amber-300">
                        Overlapping runs with the same rendered key can
                        overwrite the same saved profile.
                      </p>
                    )}
                    {data.browserProfileId && (
                      <p className="text-xs text-amber-700 dark:text-amber-300">
                        Starting Browser Profile bypasses saved-session loading
                        and write-back. Leave it empty when separating saved
                        profiles.
                      </p>
                    )}
                  </div>
                </div>
              </div>
            )}
            {!browserMemoryEnabled &&
              persistBrowserSessionEnabled &&
              workflowPermanentId && (
                <Dialog
                  open={isResetProfileDialogOpen}
                  onOpenChange={setIsResetProfileDialogOpen}
                >
                  <DialogTrigger asChild>
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      className="nopan"
                    >
                      <ReloadIcon className="mr-2 h-3 w-3" />
                      Reset Saved Profile
                    </Button>
                  </DialogTrigger>
                  <DialogContent>
                    <DialogHeader>
                      <DialogTitle>Reset saved profile?</DialogTitle>
                      <DialogDescription>
                        Clears the default saved browser profile for this agent.
                        The next unsegmented run starts from a fresh browser
                        state. Segmented saved profiles are kept.
                      </DialogDescription>
                    </DialogHeader>
                    <DialogFooter>
                      <DialogClose asChild>
                        <Button variant="secondary">Cancel</Button>
                      </DialogClose>
                      <Button
                        variant="destructive"
                        onClick={() => resetProfileMutation.mutate()}
                        disabled={resetProfileMutation.isPending}
                      >
                        {resetProfileMutation.isPending && (
                          <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                        )}
                        Reset Saved Profile
                      </Button>
                    </DialogFooter>
                  </DialogContent>
                </Dialog>
              )}
          </div>
          {!browserMemoryEnabled && (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Label>Starting Browser Profile</Label>
                <HelpTooltip content="Optional browser profile to load at run start. Leave this empty when you want saved-session persistence to decide the browser state." />
              </div>
              <BrowserProfileSelector
                value={data.browserProfileId}
                onChange={(value) => update({ browserProfileId: value })}
                compact
              />
            </div>
          )}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>Extra HTTP Headers</Label>
              <HelpTooltip content="Specify some self-defined HTTP requests headers" />
            </div>
            <KeyValueInput
              value={
                data.extraHttpHeaders &&
                typeof data.extraHttpHeaders === "object"
                  ? JSON.stringify(data.extraHttpHeaders)
                  : (data.extraHttpHeaders ?? null)
              }
              onChange={(val) => {
                const v =
                  val === null
                    ? "{}"
                    : typeof val === "string"
                      ? val.trim()
                      : JSON.stringify(val);
                const normalized = v === "" ? "{}" : v;
                if (normalized === data.extraHttpHeaders) {
                  return;
                }
                update({ extraHttpHeaders: normalized });
              }}
              addButtonText="Add Header"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>Max Screenshot Scrolls</Label>
              <HelpTooltip
                content={`The maximum number of scrolls for the post action screenshot. Default is ${MAX_SCREENSHOT_SCROLLS_DEFAULT}. If it's set to 0, it will take the current viewport screenshot.`}
              />
            </div>
            <Input
              value={data.maxScreenshotScrolls ?? ""}
              placeholder={`Default: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
              type="number"
              onChange={(event) => {
                const value =
                  event.target.value === "" ? null : Number(event.target.value);
                update({ maxScreenshotScrolls: value });
              }}
            />
          </div>
        </SettingsGroup>
        <SettingsGroup
          id="webhook"
          title="Webhook"
          summary={summaries.webhook}
          locked={mutationLocked}
        >
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label>Webhook Callback URL</Label>
              <HelpTooltip content="The URL of a webhook endpoint to send the agent results" />
            </div>
            <div className="flex flex-col gap-2">
              <Input
                className="w-full"
                value={localWebhookUrl}
                placeholder="https://"
                onChange={(event) => {
                  setLocalWebhookUrl(event.target.value);
                  update({ webhookCallbackUrl: event.target.value });
                }}
              />
              <TestWebhookDialog
                runType="workflow_run"
                runId={null}
                initialWebhookUrl={localWebhookUrl || undefined}
                autoRunOnOpen={false}
                trigger={
                  <Button
                    type="button"
                    variant="secondary"
                    className="self-start"
                    disabled={!localWebhookUrl}
                  >
                    Test Webhook
                  </Button>
                }
              />
            </div>
          </div>
        </SettingsGroup>
      </Accordion>
    </div>
  );
}

export { WorkflowSettingsEditor };
