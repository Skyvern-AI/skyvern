import { ReloadIcon } from "@radix-ui/react-icons";
import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { BrowserProfileSelector } from "@/routes/workflows/components/BrowserProfileSelector";
import { HelpTooltip } from "@/components/HelpTooltip";
import { KeyValueInput } from "@/components/KeyValueInput";
import { ModelSelector } from "@/components/ModelSelector";
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
import { Badge } from "@/components/ui/badge";
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

import { useResetProfileMutation } from "@/routes/workflows/hooks/useResetProfileMutation";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";

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

const SEQUENTIAL_KEY_TOOLTIP =
  "Scope the run queue. Runs with the same key are queued together; runs with different keys can still execute in parallel. Templated against agent inputs, for example {{ account_id }} to serialize per account.";

const BROWSER_PROFILE_KEY_TOOLTIP =
  "Template for separating saved browser profiles. Use + to insert an agent input, or type a static key. Runs with the same rendered value reuse the same saved profile.";

const PIN_SAVED_SESSION_IP_TOOLTIP =
  "Pin this workflow's saved sessions to a consistent proxy IP across runs, so restored logins are not invalidated by IP changes. Requires the Residential (ISP) proxy location. With a Browser Profile Key, each saved profile keeps its own IP.";

const WORKFLOW_RUN_DEFAULT_MAX_ELAPSED_TIME_MINUTES = 4 * 60;
const WORKFLOW_RUN_MAX_ELAPSED_TIME_MINUTES = 8 * 60;

function WorkflowSettingsEditor({ blockId }: { blockId: string }) {
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
  return <WorkflowSettingsEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function WorkflowSettingsEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: WorkflowStartNodeData;
}) {
  const { workflowPermanentId } = useParams();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const update = useUpdate<StartNode["data"]>({ id: blockId, editable: true });
  const studioEnabled = useWorkflowStudioEnabled();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  // Self-healing is restricted to copilot-authored workflows; hide the toggle
  // elsewhere so it never reads as a switch that silently does nothing.
  // copilot_authored is lineage-derived server-side — the current version's
  // created_by/edited_by get re-stamped by user saves and are not durable.
  const copilotAuthored = workflow?.copilot_authored === true;

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
      data.finallyBlockLabel &&
      !terminalBlockLabelSet.has(data.finallyBlockLabel)
    ) {
      update({ finallyBlockLabel: null });
    }
  }, [data.finallyBlockLabel, terminalBlockLabelSet, update]);

  return (
    <div data-testid="workflow-settings-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label>Model</Label>
          <HelpTooltip content="The default LLM used for every block in this agent that doesn't override it." />
        </div>
        <ModelSelector
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
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label>Proxy Location</Label>
          <HelpTooltip content="Route Skyvern through one of our available proxies." />
        </div>
        <ProxySelector
          value={data.proxyLocation}
          onChange={(value) => update({ proxyLocation: value })}
        />
      </div>
      <div className="flex flex-col gap-4 rounded-md bg-slate-elevation5 p-4 pl-4">
        <div className="flex flex-col gap-4">
          <div className="flex justify-between">
            <div className="flex items-center gap-2">
              <Label>Run With</Label>
              <HelpTooltip content="If code has been generated and saved from a previously successful run, set this to 'Code' to use that code when executing the agent. To avoid using code, set this to 'Skyvern Agent'." />
            </div>
            <Select
              value={data.runWith || "agent"}
              onValueChange={(value) => update({ runWith: value })}
            >
              <SelectTrigger className="w-48">
                <SelectValue placeholder="Run Method" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="agent">Skyvern Agent</SelectItem>
                <SelectItem value="code">Code</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label>AI Fallback (cached scripts)</Label>
              <HelpTooltip content="If a cached-script run fails, fall back to AI and regenerate the script." />
              <Switch
                className="ml-auto"
                checked={data.aiFallback}
                onCheckedChange={(value) => update({ aiFallback: value })}
              />
            </div>
          </div>
          {studioEnabled && copilotAuthored && (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <Label>Code Block Self-Healing</Label>
                <Badge variant="warning" className="px-1.5 py-0.5 text-[10px]">
                  Beta
                </Badge>
                <HelpTooltip content="If a code block fails on a changed page, an AI agent takes over the live browser to finish that block's goal, then the run continues." />
                <Switch
                  className="ml-auto"
                  checked={data.enableSelfHealing}
                  onCheckedChange={(value) =>
                    update({ enableSelfHealing: value })
                  }
                />
              </div>
            </div>
          )}
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label>Code Key (optional)</Label>
              <HelpTooltip content="A static or dynamic key for directing code generation." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => {
                const v = value.length ? value : null;
                update({ scriptCacheKey: v });
              }}
              value={data.scriptCacheKey ?? ""}
              placeholder={placeholders["scripts"]["scriptKey"]}
              className="nopan text-xs"
            />
          </div>
        </div>
      </div>
      <div className="flex flex-col gap-4">
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
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label>Save &amp; Reuse Session</Label>
          <HelpTooltip content="Persist session information across agent runs" />
          <Switch
            className="ml-auto"
            checked={data.persistBrowserSession}
            onCheckedChange={(value) =>
              update({
                persistBrowserSession: value,
                pinSavedSessionIp: value ? data.pinSavedSessionIp : false,
                browserProfileKey: value ? data.browserProfileKey : null,
              })
            }
          />
        </div>
        {data.persistBrowserSession && (
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
                  Use + to insert an input like {"{{ credential_id }}"}. Leave
                  empty to use one saved profile for this agent.
                </p>
                {!data.runSequentially && data.browserProfileKey && (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    Overlapping runs with the same rendered key can overwrite
                    the same saved profile.
                  </p>
                )}
                {data.browserProfileId && (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    Starting Browser Profile bypasses saved-session loading and
                    write-back. Leave it empty when separating saved profiles.
                  </p>
                )}
              </div>
            </div>
          </div>
        )}
        {data.persistBrowserSession && workflowPermanentId && (
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
                  Clears the default saved browser profile for this agent. The
                  next unsegmented run starts from a fresh browser state.
                  Segmented saved profiles are kept.
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
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label>Extra HTTP Headers</Label>
          <HelpTooltip content="Specify some self-defined HTTP requests headers" />
        </div>
        <KeyValueInput
          value={
            data.extraHttpHeaders && typeof data.extraHttpHeaders === "object"
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
    </div>
  );
}

export { WorkflowSettingsEditor };
