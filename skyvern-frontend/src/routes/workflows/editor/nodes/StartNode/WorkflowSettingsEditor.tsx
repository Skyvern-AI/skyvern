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
              <Label>AI Fallback (self-healing)</Label>
              <HelpTooltip content="If a run with code fails, fallback to AI and regenerate the code." />
              <Switch
                className="ml-auto"
                checked={data.aiFallback}
                onCheckedChange={(value) => update({ aiFallback: value })}
              />
            </div>
          </div>
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
              update({ persistBrowserSession: value })
            }
          />
        </div>
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
                Reset Profile
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Reset saved profile?</DialogTitle>
                <DialogDescription>
                  Clears the saved browser profile for this agent. The next run
                  will start from a fresh browser state. Use this if the saved
                  profile is stuck or producing errors.
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
                  Reset Profile
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
      </div>
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label>Default Browser Profile</Label>
          <HelpTooltip content="The default browser profile used when running this agent. Can be overridden per run." />
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
          <HelpTooltip content="Times out this workflow after the configured elapsed runtime. Maximum runtime is 4 hours (240 minutes)." />
        </div>
        <Input
          value={data.maxElapsedTimeMinutes ?? ""}
          placeholder="Default: 4 hours"
          type="number"
          min={1}
          max={240}
          step={1}
          onChange={(event) => {
            const rawValue = event.target.value;
            const parsedValue = Number(rawValue);
            const value =
              rawValue === "" || !Number.isFinite(parsedValue)
                ? null
                : Math.min(240, Math.max(1, Math.trunc(parsedValue)));
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
