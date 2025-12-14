import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import type { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";

type Props = {
  prompt: string;
  llmKey?: string | null;
  jsonSchema?: Record<string, unknown> | string | null;
  parameters?: Array<WorkflowParameter>;
};

function TextPromptBlockParameters({
  prompt,
  llmKey,
  jsonSchema,
  parameters,
}: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Prompt</h1>
          <h2 className="text-base text-slate-400">
            Instructions passed to the selected LLM
          </h2>
        </div>
        <AutoResizingTextarea value={prompt} readOnly />
      </div>
      {llmKey ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">LLM Key</h1>
          </div>
          <Input value={llmKey} readOnly />
        </div>
      ) : null}
      {jsonSchema ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">JSON Schema</h1>
            <h2 className="text-base text-slate-400">
              Expected shape of the model response
            </h2>
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={
              typeof jsonSchema === "string"
                ? jsonSchema
                : JSON.stringify(jsonSchema, null, 2)
            }
            readOnly
            minHeight="160px"
            maxHeight="400px"
          />
        </div>
      ) : null}
      {parameters && parameters.length > 0 ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Parameters</h1>
          </div>
          <div className="flex w-full flex-col gap-3">
            {parameters.map((parameter) => (
              <div
                key={parameter.key}
                className="rounded border border-slate-700/40 bg-slate-elevation3 p-3"
              >
                <p className="font-medium">{parameter.key}</p>
                {parameter.description ? (
                  <p className="text-sm text-slate-400">
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

export { TextPromptBlockParameters };
