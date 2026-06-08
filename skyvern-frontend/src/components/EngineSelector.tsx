import { RunEngine } from "@/api/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";
import { BadgeLabel, type BadgeVariant } from "./BadgeLabel";

type EngineOption = {
  value: RunEngine;
  label: string;
  badge?: string;
  badgeVariant?: BadgeVariant;
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
    badge: "Legacy",
    badgeVariant: "default",
  },
  {
    value: RunEngine.OpenaiCua,
    label: "OpenAI CUA",
    badge: "Enterprise",
    badgeVariant: "warning",
  },
  {
    value: RunEngine.AnthropicCua,
    label: "Anthropic CUA",
    badge: "Enterprise",
    badgeVariant: "warning",
  },
  {
    value: RunEngine.YutoriNavigator,
    label: "Yutori Navigator",
    badge: "Deprecated",
    badgeVariant: "default",
  },
];

// Default engines for blocks that don't support V2 mode
const defaultEngines: Array<RunEngine> = [
  RunEngine.SkyvernV1,
  RunEngine.OpenaiCua,
  RunEngine.AnthropicCua,
];

function RunEngineSelector({
  value,
  onChange,
  className,
  availableEngines,
}: Props) {
  const engines = availableEngines ?? defaultEngines;
  const visibleEngines =
    value && !engines.includes(value) ? [...engines, value] : engines;
  const engineOptions = allEngineOptions.filter((opt) =>
    visibleEngines.includes(opt.value),
  );

  const selectedOption = engineOptions.find(
    (opt) => opt.value === (value ?? RunEngine.SkyvernV1),
  );

  return (
    <Select value={value ?? RunEngine.SkyvernV1} onValueChange={onChange}>
      <SelectTrigger className={className}>
        <SelectValue>
          {selectedOption && (
            <BadgeLabel
              label={selectedOption.label}
              badge={selectedOption.badge}
              badgeVariant={selectedOption.badgeVariant}
            />
          )}
        </SelectValue>
      </SelectTrigger>
      <SelectContent>
        {engineOptions.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            <BadgeLabel
              label={option.label}
              badge={option.badge}
              badgeVariant={option.badgeVariant}
            />
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { RunEngineSelector };
