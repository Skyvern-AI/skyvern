import { useCallback, useEffect, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import { FocusScope } from "@radix-ui/react-focus-scope";
import { ReloadIcon } from "@radix-ui/react-icons";

import {
  commitYamlDraft,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { discardRestoredWorkflowSave } from "@/store/WorkflowHasChangesStore";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";

import { CodeEditor } from "../components/CodeEditor";
import { YamlModeToggle } from "./YamlModeToggle";
import { WorkflowScopeContext } from "./WorkflowScopeContext";
import type { WorkflowApiResponse } from "../types/workflowTypes";

type Props = {
  workflowId: string;
  // "fullscreen" (legacy editor): modal overlay covering the whole editor —
  // dialog semantics + focus trap. "pane" (studio): swaps the Editor pane's
  // content; sibling panes stay usable, so no modal semantics or trap.
  variant?: "fullscreen" | "pane";
};

function WorkflowSavePendingNotice() {
  const credentialGetter = useCredentialGetter();
  const [checking, setChecking] = useState(false);
  const pending = useWorkflowYamlEditorStore((state) => {
    const workflowPermanentId = state.editorOwner?.workflowPermanentId;
    return workflowPermanentId
      ? state.pendingSaves[workflowPermanentId]
      : undefined;
  });
  if (!pending?.slow && !pending?.reloadProtectionUnavailable) return null;
  return (
    <div
      role="status"
      className="flex items-center gap-3 border-b border-border bg-slate-elevation2 px-4 py-2 text-sm"
    >
      {pending.reloadProtectionUnavailable ? (
        <span>
          Reload protection for this save is unavailable because session storage
          is unavailable.
        </span>
      ) : null}
      {pending.slow ? (
        <>
          <span>
            {pending.restored
              ? "A previous save is unconfirmed. Reload to check it or discard the pending save."
              : "Still saving this workflow. You can keep waiting or reload."}
          </span>
          {pending.restored ? (
            <Button
              variant="outline"
              size="sm"
              disabled={checking}
              onClick={async () => {
                setChecking(true);
                try {
                  const client = await getClient(credentialGetter);
                  const { data } = await client.get<WorkflowApiResponse>(
                    `/workflows/${pending.owner.workflowPermanentId}`,
                  );
                  discardRestoredWorkflowSave(pending.owner, data);
                } catch {
                  toast({
                    title: "Could not check the pending save",
                    description:
                      "The workflow is still locked. Try again or reload.",
                    variant: "destructive",
                  });
                } finally {
                  setChecking(false);
                }
              }}
            >
              Discard pending save
            </Button>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            onClick={() => window.location.reload()}
          >
            Reload
          </Button>
        </>
      ) : null}
    </div>
  );
}

// YAML editing surface, shown while the store is active. Switching back to
// Visual commits the draft into the graph; broken YAML blocks the switch.
function WorkflowYamlEditor({ workflowId, variant = "fullscreen" }: Props) {
  const draft = useWorkflowYamlEditorStore((s) => s.draft);
  const error = useWorkflowYamlEditorStore((s) => s.error);
  const stale = useWorkflowYamlEditorStore((s) => s.stale);
  const enterYamlMode = useWorkflowYamlEditorStore((s) => s.enterYamlMode);
  const copilotActive = useWorkflowYamlEditorStore(
    (s) => s.copilotAcceptance !== null,
  );
  const committing = useWorkflowYamlEditorStore(
    (s) => s.committing || s.commitInProgress,
  );
  const setDraft = useWorkflowYamlEditorStore((s) => s.setDraft);
  const fullscreen = variant === "fullscreen";
  const editorViewRef = useRef<EditorView | null>(null);
  const onEditorView = useCallback((view: EditorView) => {
    editorViewRef.current = view;
  }, []);

  useEffect(() => {
    const flushDraft = () => {
      const view = editorViewRef.current;
      if (view) {
        const text = view.state.doc.toString();
        if (text !== useWorkflowYamlEditorStore.getState().draft)
          setDraft(text);
      }
    };
    useWorkflowYamlEditorStore.setState({ flushDraft });
    return () => {
      if (useWorkflowYamlEditorStore.getState().flushDraft === flushDraft) {
        useWorkflowYamlEditorStore.setState({ flushDraft: null });
      }
    };
  }, [setDraft]);

  const switchToVisual = () => {
    void commitYamlDraft(false);
  };

  const surface = (
    <div
      {...(fullscreen
        ? { role: "dialog", "aria-modal": true }
        : { role: "region" })}
      aria-label="Workflow YAML"
      className={cn(
        "absolute inset-0 flex flex-col bg-slate-elevation1",
        // The pane variant sits under stage-level overlays (Inputs/Schedule
        // panels at z-40) but above everything inside the Editor pane.
        fullscreen ? "z-50" : "z-30",
      )}
      onKeyDown={(event) => {
        // Standard dialog escape hatch; commit-on-switch means Escape
        // behaves exactly like the Visual toggle (invalid YAML stays open).
        if (event.key === "Escape" && !committing) {
          event.stopPropagation();
          switchToVisual();
        }
      }}
    >
      <div className="flex items-center justify-between gap-3 border-b border-border bg-slate-elevation2 px-4 py-2">
        <div className="flex min-w-0 items-center gap-3">
          <span className="shrink-0 text-sm">Workflow YAML</span>
          <span className="text-xs text-muted-foreground dark:text-slate-500">
            This document includes workflow settings. Switch to Visual to apply,
            then Save. A null code_version keeps the saved value.
          </span>
          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer whitespace-nowrap">
              Null values
            </summary>
            <p className="mt-2 max-w-xl">
              Null keeps mask_secrets, cdp_connect_headers, and code_version
              unchanged. Use false to disable mask_secrets and {"{}"} to clear
              CDP headers. Null resets ai_fallback to true;
              persist_browser_session, reuse_browser_session,
              pin_saved_session_ip, run_sequentially, adaptive_caching, and
              generate_script_on_terminal to false; run_with to agent; cache_key
              to default. proxy_location: null clears the workflow proxy
              setting. Runs use the deployment default: residential or no proxy,
              depending on runtime configuration. Other nullable settings clear
              on null; title cannot be null.
            </p>
          </details>
          {committing ? (
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              <ReloadIcon className="size-3 animate-spin" />
              Applying…
            </span>
          ) : null}
        </div>
        {/* The studio's pane variant hosts this toggle in the pane header. */}
        {fullscreen ? (
          <YamlModeToggle
            mode="code"
            onVisual={switchToVisual}
            disabled={committing}
          />
        ) : null}
      </div>
      <WorkflowSavePendingNotice />
      {error ? (
        <div
          role="alert"
          className="border-b border-red-300 bg-red-100 px-4 py-2 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/60 dark:text-red-200"
        >
          <strong className="font-semibold">
            {stale ? "YAML draft out of date:" : "Invalid YAML:"}
          </strong>{" "}
          {error}
          {stale ? (
            <Button
              variant="outline"
              size="sm"
              className="ml-3"
              disabled={committing || copilotActive || !enterYamlMode}
              onClick={() => enterYamlMode?.()}
            >
              Reopen YAML and discard draft
            </Button>
          ) : null}
        </div>
      ) : null}
      <div className="min-h-0 flex-1">
        <WorkflowScopeContext.Provider value={{ workflowId, readOnly: false }}>
          <CodeEditor
            key={workflowId}
            deferKey="workflowYamlDraft"
            language="yaml"
            value={draft}
            onChange={setDraft}
            onEditorView={onEditorView}
            readOnly={committing}
            fullHeight
            lineWrap={false}
            className="h-full"
            ariaLabel="Workflow YAML"
            autoFocus
          />
        </WorkflowScopeContext.Provider>
      </div>
    </div>
  );

  if (!fullscreen) {
    return surface;
  }

  return (
    // Trap focus inside the editor while it's open (the canvas is visually
    // covered but still in the tab order); CodeEditor autoFocus lands the
    // caret, so prevent FocusScope from stealing focus to the toggle.
    <FocusScope
      asChild
      loop
      trapped
      onMountAutoFocus={(e) => e.preventDefault()}
    >
      {surface}
    </FocusScope>
  );
}

export { WorkflowYamlEditor, WorkflowSavePendingNotice };
