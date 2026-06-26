import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { stringify as convertToYAML } from "yaml";
import {
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/util/utils";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { useGlobalWorkflowsQuery } from "@/routes/workflows/hooks/useGlobalWorkflowsQuery";
import { useCreateWorkflowMutation } from "@/routes/workflows/hooks/useCreateWorkflowMutation";
import { convert } from "@/routes/workflows/editor/workflowEditorUtils";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import type { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";
import {
  getTemplatesForIntent,
  getTemplateIcon,
  getSetupTime,
} from "./templateUtils";

const SURFACE = "dashboard" as const;

const INTENT_PLACEHOLDERS: Record<string, string> = {
  fill_forms: "Describe the form you want to fill out...",
  extract_data: "What data do you want to extract and from where?",
  monitor_website: "What website changes do you want to monitor?",
  something_else: "Describe what you want to automate...",
};

const HANDOFF_TITLE_MAX_LEN = 80;

function deriveTitle(prompt: string): string {
  const collapsed = prompt.replace(/\s+/g, " ").trim();
  if (!collapsed) return "New Workflow";
  if (collapsed.length <= HANDOFF_TITLE_MAX_LEN) return collapsed;
  return `${collapsed.slice(0, HANDOFF_TITLE_MAX_LEN - 1).trimEnd()}...`;
}

type CopilotCTAStepProps = {
  selectedIntent: string;
  onBack: () => void;
  onSkip: () => void;
  onDismiss: () => void;
  /** Reports in-flight creations so the parent can ignore its dialog-level
   * close path (Escape / X), which cannot see this step's mutations. */
  onBusyChange?: (busy: boolean) => void;
};

function CopilotCTAStep({
  selectedIntent,
  onBack,
  onSkip,
  onDismiss,
  onBusyChange,
}: Readonly<CopilotCTAStepProps>) {
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const [promptText, setPromptText] = useState("");
  const [showFallback, setShowFallback] = useState(false);
  const submitRef = useRef(false);

  const { data: globalTemplates = [], isLoading: templatesLoading } =
    useGlobalWorkflowsQuery();
  const createWorkflowMutation = useCreateWorkflowMutation();

  const handoffMutation = useMutation({
    mutationFn: async (prompt: string) => {
      const client = await getClient(credentialGetter);
      const request: WorkflowCreateYAMLRequest = {
        title: deriveTitle(prompt),
        description: "",
        ai_fallback: true,
        code_version: 2,
        run_with: "agent",
        workflow_definition: {
          version: 2,
          blocks: [],
          parameters: [],
        },
      };
      const yaml = convertToYAML(request);
      const result = await client.post<WorkflowApiResponse>(
        "/workflows",
        yaml,
        { headers: { "Content-Type": "text/plain" } },
      );
      return { workflow: result.data, prompt };
    },
    onSuccess: ({ workflow, prompt }) => {
      OnboardingTelemetry.flowCompleted(SURFACE);
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      // Let the provider observe the backend first_save_at milestone so the
      // first_workflow_created funnel event fires for copilot-first users.
      queryClient.invalidateQueries({ queryKey: ["userOnboarding"] });
      onDismiss();
      navigate(
        workflowEditorPath(workflow.workflow_permanent_id, studioEnabled),
        {
          state: { copilotMessage: prompt },
        },
      );
    },
    onError: () => {
      setShowFallback(true);
    },
    onSettled: () => {
      submitRef.current = false;
    },
  });

  const busy = handoffMutation.isPending || createWorkflowMutation.isPending;
  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);
  useEffect(() => {
    return () => {
      onBusyChange?.(false);
    };
  }, [onBusyChange]);

  function handleSubmit() {
    const trimmed = promptText.trim();
    if (!trimmed || submitRef.current || handoffMutation.isPending) return;
    submitRef.current = true;
    OnboardingTelemetry.modalCopilotClicked(SURFACE, selectedIntent, trimmed);
    handoffMutation.mutate(trimmed);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  function handleTemplateSelect(template: WorkflowApiResponse) {
    if (createWorkflowMutation.isPending) return;
    OnboardingTelemetry.modalTemplateSelected(
      SURFACE,
      template.workflow_permanent_id,
      selectedIntent,
    );
    const cloned = convert({
      ...template,
      title: `${template.title} (copy)`,
    });
    // flow_completed fires from useCreateWorkflowMutation (it owns the navigation
    // that unmounts this modal); the navigation also dismisses the modal.
    createWorkflowMutation.mutate({ ...cloned, _via: "onboarding_template" });
  }

  const filteredTemplates =
    globalTemplates.length > 0
      ? getTemplatesForIntent(globalTemplates, selectedIntent)
      : [];

  if (showFallback) {
    return (
      <>
        <DialogHeader>
          <DialogTitle className="text-xl">Or pick a template</DialogTitle>
          <DialogDescription>
            Choose a pre-built workflow and customize it in the editor.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-2 gap-3 py-2">
          {templatesLoading ? (
            Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-28 rounded-lg" />
            ))
          ) : filteredTemplates.length > 0 ? (
            filteredTemplates.map((template) => {
              const Icon = getTemplateIcon(template);
              return (
                <button
                  key={template.workflow_permanent_id}
                  type="button"
                  disabled={createWorkflowMutation.isPending}
                  onClick={() => handleTemplateSelect(template)}
                  className={cn(
                    "flex flex-col items-start gap-2 rounded-lg border border-border p-4 text-left transition-colors",
                    "hover:border-primary hover:bg-primary/5",
                    createWorkflowMutation.isPending &&
                      "pointer-events-none opacity-50",
                  )}
                >
                  <Icon className="h-6 w-6 text-primary" />
                  <div className="min-w-0 self-stretch">
                    <p className="truncate text-sm font-medium">
                      {template.title}
                    </p>
                    {template.description && (
                      <p className="line-clamp-2 text-xs text-muted-foreground">
                        {template.description}
                      </p>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    ~{getSetupTime(template)} setup
                  </span>
                </button>
              );
            })
          ) : (
            <p className="col-span-2 py-8 text-center text-sm text-muted-foreground">
              No templates available yet.
            </p>
          )}
        </div>
        <DialogFooter className="gap-2 sm:gap-0">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowFallback(false)}
            disabled={createWorkflowMutation.isPending}
          >
            Back
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onSkip}
            disabled={createWorkflowMutation.isPending}
          >
            Skip
          </Button>
        </DialogFooter>
      </>
    );
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle className="text-xl">
          Describe what you want to automate
        </DialogTitle>
        <DialogDescription>
          Tell us in plain language and our AI copilot will build a workflow for
          you.
        </DialogDescription>
      </DialogHeader>
      <div className="py-2">
        <Textarea
          value={promptText}
          onChange={(e) => setPromptText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            INTENT_PLACEHOLDERS[selectedIntent] ??
            INTENT_PLACEHOLDERS.something_else
          }
          className="min-h-[100px] resize-none"
          disabled={handoffMutation.isPending}
          autoFocus
        />
        {handoffMutation.isPending && (
          <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
            <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            Setting up your workflow...
          </div>
        )}
      </div>
      <DialogFooter className="gap-2 sm:gap-0">
        <Button
          variant="ghost"
          size="sm"
          onClick={onBack}
          disabled={handoffMutation.isPending}
        >
          Back
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={onSkip}
          disabled={handoffMutation.isPending}
        >
          Skip
        </Button>
        <Button
          size="sm"
          disabled={!promptText.trim() || handoffMutation.isPending}
          onClick={handleSubmit}
        >
          {handoffMutation.isPending ? "Creating..." : "Create with AI"}
        </Button>
      </DialogFooter>
    </>
  );
}

export { CopilotCTAStep };
export type { CopilotCTAStepProps };
