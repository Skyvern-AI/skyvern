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
  clearable?: boolean;
  value: WorkflowModel | null;
  // --
  onChange: (value: WorkflowModel | null) => void;
};

const constants = {
  SkyvernOptimized: "Skyvern Optimized",
} as const;

const deprecatedModelNames = new Set<string>([
  "gemini-2.5-flash-lite",
  "azure/gpt-4.1",
  "azure/o3",
  "claude-haiku-4-5-20251001",
]);

function ModelSelector({
  clearable = true,
  value,
  onChange,
  className,
}: Props) {
  const credentialGetter = useCredentialGetter();

  const { data: availableModels } = useQuery<ModelsResponse>({
    queryKey: ["models"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get("/models").then((res) => res.data);
    },
  });

  const rawModels = availableModels?.models ?? {};
  const models = Object.fromEntries(
    Object.entries(rawModels).map(([modelName, label]) => [
      modelName,
      deprecatedModelNames.has(modelName) ? `${label} (deprecated)` : label,
    ]),
  );

  const visibleEntries = Object.entries(models).filter(
    ([modelName]) =>
      !deprecatedModelNames.has(modelName) || value?.model_name === modelName,
  );

  const reverseMap = visibleEntries.reduce(
    (acc, [modelName, label]) => {
      acc[label] = modelName;
      return acc;
    },
    {} as Record<string, string>,
  );
  const labels = Object.keys(reverseMap);

  const chosen = value
    ? models[value.model_name] ?? constants.SkyvernOptimized
    : constants.SkyvernOptimized;
  const choices = [constants.SkyvernOptimized, ...labels];

  return (
    <div className="flex items-center justify-between">
      <div className="flex gap-2">
        <Label className="text-xs font-normal text-slate-300">Model</Label>
        <HelpTooltip content="The LLM model to use for this block" />
      </div>
      <div className="relative flex items-center">
        <Select
          value={chosen}
          onValueChange={(v) => {
            const newValue = v === constants.SkyvernOptimized ? null : v;
            const modelName = newValue ? reverseMap[newValue] : null;
            const value = modelName ? { model_name: modelName } : null;
            onChange(value);
          }}
        >
          <SelectTrigger
            className={(className || "") + (value && clearable ? " pr-10" : "")}
          >
            <SelectValue placeholder={constants.SkyvernOptimized} />
          </SelectTrigger>
          <SelectContent>
            {choices.map((m) => (
              <SelectItem key={m} value={m}>
                {m === constants.SkyvernOptimized ? (
                  <span>Skyvern Optimized ✨</span>
                ) : (
                  m
                )}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {value && clearable && (
          <>
            <div
              className="pointer-events-none absolute right-8 top-1/2 h-5 w-px -translate-y-1/2 bg-slate-200 opacity-70 dark:bg-slate-700"
              aria-hidden="true"
            />
            <button
              type="button"
              aria-label="Clear selection"
              className="absolute right-0 z-10 flex h-9 w-8 items-center justify-center text-slate-400 hover:text-red-500 focus:outline-none"
              onClick={() => onChange(null)}
              tabIndex={0}
            >
              ×
            </button>
          </>
        )}
      </div>
    </div>
  );
}

ModelSelector.displayName = "ModelSelector";

export { ModelSelector };
