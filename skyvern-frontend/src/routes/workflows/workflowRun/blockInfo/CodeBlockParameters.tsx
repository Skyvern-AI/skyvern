import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import type { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";

type Props = {
  code: string;
  parameters?: Array<WorkflowParameter>;
};

function CodeBlockParameters({ code, parameters }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Code</h1>
          <h2 className="text-base text-muted-foreground">
            The Python snippet executed for this block
          </h2>
        </div>
        <CodeEditor
          className="w-full"
          language="python"
          value={code}
          readOnly
          minHeight="160px"
          maxHeight="400px"
        />
      </div>
      {parameters && parameters.length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Parameters</h1>
            <h2 className="text-base text-muted-foreground">
              Inputs passed to this code block
            </h2>
          </div>
          <div className="flex w-full flex-col gap-3">
            {parameters.map((parameter) => (
              <div
                key={parameter.key}
                className="rounded border border-border/40 bg-slate-elevation3 p-3"
              >
                <p className="font-medium">{parameter.key}</p>
                {parameter.description ? (
                  <p className="text-sm text-muted-foreground">
                    {parameter.description}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export { CodeBlockParameters };
