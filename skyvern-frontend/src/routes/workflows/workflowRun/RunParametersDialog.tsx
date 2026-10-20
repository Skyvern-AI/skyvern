import { ParametersDialogBase } from "../components/ParametersDialogBase";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { Parameter } from "../types/workflowTypes";
import { getOrderedRunParameters } from "../utils";

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
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  // Reads the run through the shared by-run-id entry, so opening the dialog
  // reuses whatever the run surfaces already fetched instead of building the
  // whole run response again under a key of its own.
  const { data: run } = useWorkflowRunWithWorkflowQuery({
    workflowRunId: workflowRunId ?? undefined,
  });

  const defByKey = new Map(
    (workflow?.workflow_definition.parameters ?? []).map((p: Parameter) => [
      p.key,
      p,
    ]),
  );

  const items = getOrderedRunParameters(
    workflow?.workflow_definition.parameters,
    run?.parameters ?? {},
  ).map(([key, value]) => {
    const def = defByKey.get(key);
    const description =
      def && "description" in def ? (def.description ?? undefined) : undefined;
    const type = def ? (def.parameter_type ?? undefined) : undefined;
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
      title="Run Inputs"
      sectionLabel="Inputs for this run"
      items={items}
    />
  );
}
