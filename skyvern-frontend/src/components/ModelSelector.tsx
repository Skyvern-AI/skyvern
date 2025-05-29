import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectValue,
  SelectItem,
} from "@/components/ui/select";
import { getClient } from "@/api/AxiosClient";
import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { ModelsResponse } from "@/api/types";
import { WorkflowModel } from "@/routes/workflows/types/workflowTypes";

type Props = {
  className?: string;
  value: WorkflowModel | null;
  // --
  onChange: (value: WorkflowModel | null) => void;
};

function ModelSelector({ value, onChange, className }: Props) {
  const credentialGetter = useCredentialGetter();

  const { data: availableModels } = useQuery<ModelsResponse>({
    queryKey: ["models"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get("/models").then((res) => res.data);
    },
  });

  const models = availableModels?.models ?? [];

  return (
    <div className="flex items-center justify-between">
      <div className="flex gap-2">
        <Label className="text-xs font-normal text-slate-300">Model</Label>
        <HelpTooltip content="The LLM model to use for this block" />
      </div>
      <Select
        value={value?.model ?? ""}
        onValueChange={(value) => {
          onChange({ model: value });
        }}
      >
        {/* className="nopan w-52 text-xs" */}
        <SelectTrigger className={className}>
          <SelectValue placeholder="Skyvern Optimized" />
        </SelectTrigger>
        <SelectContent>
          {models.map((m) => (
            <SelectItem key={m} value={m}>
              {m}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

ModelSelector.displayName = "ModelSelector";

export { ModelSelector };
