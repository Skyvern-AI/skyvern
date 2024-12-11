import { FileIcon } from "@radix-ui/react-icons";
import { CodeEditor } from "../components/CodeEditor";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";

function WorkflowRunOutput() {
  const { data: workflowRun } = useWorkflowRunQuery();
  const outputs = workflowRun?.outputs;
  const fileUrls = workflowRun?.downloaded_file_urls ?? [];
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold">Workflow Run Output</h2>
      </header>
      <CodeEditor
        language="json"
        value={
          outputs ? JSON.stringify(outputs, null, 2) : "Waiting for outputs.."
        }
        readOnly
        minHeight="96px"
        maxHeight="500px"
      />
      <div className="space-y-4">
        <header>
          <h2 className="text-lg font-semibold">Downloaded Files</h2>
        </header>
        <div className="space-y-2">
          {fileUrls.length > 0 ? (
            fileUrls.map((url, index) => {
              return (
                <div key={url} title={url} className="flex gap-2">
                  <FileIcon className="size-6" />
                  <a href={url} className="underline underline-offset-4">
                    <span>{`File ${index + 1}`}</span>
                  </a>
                </div>
              );
            })
          ) : (
            <div>No files downloaded</div>
          )}
        </div>
      </div>
    </div>
  );
}

export { WorkflowRunOutput };
