import { useParams } from "react-router-dom";
import { useWorkflowQuery } from "../../hooks/useWorkflowQuery";

function WorkflowParametersPanel() {
  const { workflowPermanentId } = useParams();

  const { data: workflow, isLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  if (isLoading || !workflow) {
    return null;
  }

  const workflowParameters = workflow.workflow_definition.parameters.filter(
    (parameter) => parameter.parameter_type === "workflow",
  );

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-lg">Workflow Parameters</h1>
        <span className="text-sm text-slate-400">
          Create placeholder values that you can link in nodes. You will be
          prompted to fill them in before running your workflow.
        </span>
      </header>
      <section className="space-y-2">
        {workflowParameters.map((parameter) => {
          return (
            <div
              key={parameter.key}
              className="flex items-center gap-4 rounded-md bg-slate-elevation1 px-3 py-2"
            >
              <span className="text-sm">{parameter.key}</span>
              <span className="text-sm text-slate-400">
                {parameter.workflow_parameter_type}
              </span>
            </div>
          );
        })}
      </section>
    </div>
  );
}

export { WorkflowParametersPanel };
