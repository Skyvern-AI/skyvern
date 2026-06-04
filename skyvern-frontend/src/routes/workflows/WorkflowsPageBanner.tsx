import { Button } from "@/components/ui/button";
import { PlusIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { defaultWorkflowRequest } from "./defaultWorkflowRequest";

function WorkflowsPageBanner() {
  const createNewWorkflowMutation = useCreateWorkflowMutation();

  return (
    <div className="space-y-8 bg-slate-elevation1 p-12">
      <div className="flex justify-center text-3xl font-bold">
        <h1>Agents</h1>
      </div>
      <div className="flex justify-center gap-4">
        <ImportWorkflowButton />
        <Button
          disabled={createNewWorkflowMutation.isPending}
          onClick={() => {
            createNewWorkflowMutation.mutate(defaultWorkflowRequest);
          }}
        >
          {createNewWorkflowMutation.isPending ? (
            <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <PlusIcon className="mr-2 h-4 w-4" />
          )}
          Create Agent
        </Button>
      </div>
      <div className="flex">
        <div className="mx-auto flex flex-col gap-3">
          <div className="font-bold">
            Agents let you create complex web-agents that can:
          </div>
          <div className="flex gap-2">
            <div className="flex size-6 items-center justify-center rounded-full border border-neutral-300 bg-neutral-200 text-xs font-semibold text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-200">
              1
            </div>
            <div>Save browser sessions and re-use them in subsequent runs</div>
          </div>
          <div className="flex gap-2">
            <div className="flex size-6 items-center justify-center rounded-full border border-neutral-300 bg-neutral-200 text-xs font-semibold text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-200">
              2
            </div>
            <div>
              Connect multiple agents together to carry out complex objectives
            </div>
          </div>
          <div className="flex gap-2">
            <div className="flex size-6 items-center justify-center rounded-full border border-neutral-300 bg-neutral-200 text-xs font-semibold text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-200">
              3
            </div>
            <div>
              Allow Skyvern agents to execute non-browser tasks such as sending
              emails, or parsing PDFs
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export { WorkflowsPageBanner };
