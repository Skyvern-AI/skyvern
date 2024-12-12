import { Label } from "@/components/ui/label";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { CodeEditor } from "../components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";

function WorkflowPostRunParameters() {
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();
  const parameters = workflowRun?.parameters ?? {};

  if (workflowRunIsLoading) {
    return <div>Loading workflow parameters...</div>;
  }

  return Object.entries(parameters).length > 0 ? (
    <div className="space-y-4 rounded-lg bg-slate-elevation3 px-6 py-5">
      <header>
        <h2 className="text-lg font-semibold">Input Parameter Values</h2>
      </header>
      {Object.entries(parameters).map(([key, value]) => {
        return (
          <div key={key} className="space-y-2">
            <Label className="text-lg">{key}</Label>
            {typeof value === "string" ||
            typeof value === "number" ||
            typeof value === "boolean" ? (
              <AutoResizingTextarea value={String(value)} readOnly />
            ) : (
              <CodeEditor
                value={JSON.stringify(value, null, 2)}
                readOnly
                language="json"
                minHeight="96px"
                maxHeight="500px"
              />
            )}
          </div>
        );
      })}
    </div>
  ) : (
    Object.entries(parameters).length === 0 && (
      <div>This workflow doesn't have any input parameters.</div>
    )
  );
}

export { WorkflowPostRunParameters };
