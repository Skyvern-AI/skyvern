import { RunEngine } from "@/api/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";
import { cn } from "@/util/utils";

type EngineOption = {
  value: RunEngine;
  label: string;
  badge?: string;
  badgeVariant?: "default" | "success" | "warning";
};

type Props = {
  value: RunEngine | null;
  onChange: (value: RunEngine) => void;
  className?: string;
  availableEngines?: Array<RunEngine>;
};

const allEngineOptions: Array<EngineOption> = [
  {
    value: RunEngine.SkyvernV1,
    label: "Skyvern 1.0",
    badge: "Recommended",
    badgeVariant: "success",
  },
  {
    value: RunEngine.SkyvernV2,
    label: "Skyvern 2.0",
    badge: "Multi-Goal",
    badgeVariant: "warning",
  },
  {
    value: RunEngine.OpenaiCua,
    label: "OpenAI CUA",
  },
  {
    value: RunEngine.AnthropicCua,
    label: "Anthropic CUA",
  },
];

// Default engines for blocks that don't support V2 mode
const defaultEngines: Array<RunEngine> = [
  RunEngine.SkyvernV1,
  RunEngine.OpenaiCua,
  RunEngine.AnthropicCua,
];

function BadgeLabel({ option }: { option: EngineOption }) {
  return (
    <div className="flex items-center gap-2">
      <span>{option.label}</span>
      {option.badge && (
        <span
          className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", {
            "bg-green-500/20 text-green-400": option.badgeVariant === "success",
            "bg-amber-500/20 text-amber-400": option.badgeVariant === "warning",
            "bg-slate-500/20 text-slate-400":
              option.badgeVariant === "default" || !option.badgeVariant,
          })}
        >
          {option.badge}
        </span>
      )}
    </div>
  );
}

function RunEngineSelector({
  value,
  onChange,
  className,
  availableEngines,
}: Props) {
  const engines = availableEngines ?? defaultEngines;
  const engineOptions = allEngineOptions.filter((opt) =>
    engines.includes(opt.value),
  );

  const selectedOption = engineOptions.find(
    (opt) => opt.value === (value ?? RunEngine.SkyvernV1),
  );

  return (
    <Select value={value ?? RunEngine.SkyvernV1} onValueChange={onChange}>
      <SelectTrigger className={className}>
        <SelectValue>
          {selectedOption && <BadgeLabel option={selectedOption} />}
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        {engineOptions.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            <BadgeLabel option={option} />
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { RunEngineSelector };
