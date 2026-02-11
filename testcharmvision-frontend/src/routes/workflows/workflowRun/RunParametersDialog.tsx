import { ParametersDialogBase } from "../components/ParametersDialogBase";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useQuery } from "@tanstack/react-query";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { WorkflowRunStatusApiResponse } from "@/api/types";
import { Parameter } from "../types/workflowTypes";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workflowPermanentId?: string;
  workflowRunId: string | null;
};

export function RunParametersDialog({
  open,
  onOpenChange,
  workflowPermanentId,
  workflowRunId,
}: Props) {
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const credentialGetter = useCredentialGetter();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const { data: run } = useQuery<WorkflowRunStatusApiResponse>({
    queryKey: ["workflowRun", workflowPermanentId, workflowRunId, "dialog"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      const isGlobalWorkflow = globalWorkflows?.some(
        (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
      );
      if (isGlobalWorkflow) {
        params.set("template", "true");
      }
      return client
        .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}`, {
          params,
        })
        .then((r) => r.data);
    },
    enabled: !!workflowPermanentId && !!workflowRunId && !!globalWorkflows,
  });

  const defByKey = new Map(
    (workflow?.workflow_definition.parameters ?? []).map((p: Parameter) => [
      p.key,
      p,
    ]),
  );

  const items = Object.entries(run?.parameters ?? {}).map(([key, value]) => {
    const def = defByKey.get(key);
    const description =
      def && "description" in def ? def.description ?? undefined : undefined;
    const type = def ? def.parameter_type ?? undefined : undefined;
    const displayValue =
      value === null || value === undefined
        ? ""
        : typeof value === "string"
          ? value
          : JSON.stringify(value);
    return {
      id: key,
      key,
      description,
      type,
      value: displayValue,
    };
  });

  return (
    <ParametersDialogBase
      open={open}
      onOpenChange={onOpenChange}
      title="Run Parameters"
      sectionLabel="Input parameters for this run"
      items={items}
    />
  );
}
