import { Skeleton } from "@/components/ui/skeleton";
import { useGlobalWorkflowsQuery } from "../workflows/hooks/useGlobalWorkflowsQuery";
import {
  useLocalWorkflowTemplates,
  getCurrentRepo,
  getCurrentTemplatesDir,
  WorkflowTemplate,
} from "./hooks/useLocalWorkflowTemplates";
import { RepoSettings } from "./RepoSettings";
import { WorkflowTemplatePreview } from "./WorkflowTemplatePreview";
import { useNavigate } from "react-router-dom";
import { WorkflowTemplateCard } from "./WorkflowTemplateCard";
import testImg from "@/assets/promptBoxBg.png";
import { TEMPORARY_TEMPLATE_IMAGES } from "./TemporaryTemplateImages";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { WorkflowApiResponse } from "../workflows/types/workflowTypes";
import { useState } from "react";

function WorkflowTemplates() {
  const { data: workflowTemplates, isLoading: workflowsLoading } =
    useGlobalWorkflowsQuery();
  const {
    data: githubTemplates,
    isLoading: templatesLoading,
    refetch,
  } = useLocalWorkflowTemplates();
  const navigate = useNavigate();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  // Get current repository and directory
  const currentRepo = getCurrentRepo();
  const currentDir = getCurrentTemplatesDir();

  // State for template preview modal
  const [previewTemplate, setPreviewTemplate] =
    useState<WorkflowTemplate | null>(null);
  const [isPreviewOpen, setIsPreviewOpen] = useState(false);

  // Mutation to save template as workflow
  const saveTemplateMutation = useMutation({
    mutationFn: async (yamlContent: string) => {
      const client = await getClient(credentialGetter);
      return client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yamlContent,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      toast({
        variant: "success",
        title: "Template saved",
        description: "Template has been saved to your workflows",
      });
      navigate(`/workflows/${response.data.workflow_permanent_id}/edit`);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Failed to save template",
        description: error.message || "An error occurred",
      });
    },
  });

  // Helper functions for template preview
  const openTemplatePreview = (template: WorkflowTemplate) => {
    setPreviewTemplate(template);
    setIsPreviewOpen(true);
  };

  const closeTemplatePreview = () => {
    setIsPreviewOpen(false);
    setPreviewTemplate(null);
  };

  const savePreviewedTemplate = () => {
    if (previewTemplate) {
      saveTemplateMutation.mutate(previewTemplate.content);
      closeTemplatePreview();
    }
  };

  if (workflowsLoading || templatesLoading) {
    return (
      <div className="space-y-5">
        <h1 className="text-xl">Explore Workflows</h1>
        <div className="flex gap-6">
          <Skeleton className="h-56 w-56 rounded-xl" />
          <Skeleton className="h-56 w-56 rounded-xl" />
          <Skeleton className="h-56 w-56 rounded-xl" />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <h1 className="text-xl">Explore Workflows</h1>

      {/* GitHub Templates Section */}
      <div className="space-y-4">
        <div className="flex min-h-[32px] items-start justify-between">
          <h2 className="text-lg font-medium text-slate-300">
            Community Templates
          </h2>
          <RepoSettings
            currentRepo={currentRepo}
            currentDir={currentDir}
            onSettingsChange={() => refetch()}
          />
        </div>

        {githubTemplates && githubTemplates.length > 0 ? (
          <div className="flex gap-6 overflow-x-auto pb-2">
            {githubTemplates.map((template) => (
              <WorkflowTemplateCard
                key={template.name}
                title={template.title}
                description={template.description}
                image={testImg}
                showSaveButton={true}
                onSave={() => saveTemplateMutation.mutate(template.content)}
                onClick={() => openTemplatePreview(template)}
              />
            ))}
          </div>
        ) : templatesLoading ? (
          <div className="flex gap-6">
            {[1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-48 w-80" />
            ))}
          </div>
        ) : (
          <div className="py-4 text-center text-slate-500">
            <p>No templates found in the configured repository.</p>
            <p className="mt-1 text-sm">
              Check your repository and directory settings above.
            </p>
          </div>
        )}
      </div>

      {/* Your Workflows Section */}
      {/* Your Workflows Section */}
      {workflowTemplates && workflowTemplates.length > 0 && (
        <div className="space-y-4">
          <h2 className="text-lg font-medium text-slate-300">Your Workflows</h2>
          <div className="flex gap-6 overflow-x-auto pb-2">
            {workflowTemplates.map((workflow) => (
              <WorkflowTemplateCard
                key={workflow.workflow_permanent_id}
                title={workflow.title}
                image={
                  TEMPORARY_TEMPLATE_IMAGES[workflow.workflow_permanent_id] ??
                  testImg
                }
                onClick={() => {
                  navigate(`/workflows/${workflow.workflow_permanent_id}/edit`);
                }}
              />
            ))}
          </div>
        </div>
      )}

      {/* Template Preview Modal */}
      <WorkflowTemplatePreview
        template={previewTemplate}
        isOpen={isPreviewOpen}
        onClose={closeTemplatePreview}
        onSave={savePreviewedTemplate}
      />
    </div>
  );
}

export { WorkflowTemplates };
