import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

function IgnoreWorkflowSystemPrompt({
  ignoreWorkflowSystemPrompt,
  editable,
  onIgnoreWorkflowSystemPromptChange,
}: {
  ignoreWorkflowSystemPrompt: boolean;
  editable: boolean;
  onIgnoreWorkflowSystemPromptChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="flex min-w-0 items-center gap-2">
        <Label className="truncate text-xs font-normal text-slate-300">
          Ignore System Prompt
        </Label>
        <HelpTooltip content="When checked, this block ignores the workflow-level system prompt. Only relevant when the workflow has a workflow system prompt set." />
      </div>
      <div className="w-52 shrink-0">
        <Switch
          checked={ignoreWorkflowSystemPrompt}
          onCheckedChange={(checked) => {
            if (!editable) {
              return;
            }
            onIgnoreWorkflowSystemPromptChange(checked);
          }}
        />
      </div>
    </div>
  );
}

export { IgnoreWorkflowSystemPrompt };
