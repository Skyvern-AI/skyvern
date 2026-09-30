import type { ReactNode } from "react";

import { RunEngine } from "@/api/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";
import { BadgeLabel, type BadgeVariant } from "./BadgeLabel";

const DEFAULT_ENGINE_VALUE = "default";

type EngineOption = {
  value: RunEngine | typeof DEFAULT_ENGINE_VALUE;
  label: ReactNode;
  badge?: string;
  badgeVariant?: BadgeVariant;
};

type Props = {
  value: RunEngine | null;
  onChange: (value: RunEngine | null) => void;
  className?: string;
  availableEngines?: Array<RunEngine>;
  effectiveDefaultEngine?: RunEngine | null;
};

const engineOptions: Array<EngineOption & { value: RunEngine }> = [
  {
    value: RunEngine.SkyvernV1,
    label: "Skyvern 1.0",
    badge: "Legacy",
    badgeVariant: "default",
  },
  {
    value: RunEngine.SkyvernV2,
    label: "Skyvern 2.0",
    badge: "Legacy",
    badgeVariant: "default",
  },
  {
    value: RunEngine.SkyvernV3,
    label: "Skyvern 3.0",
    badge: "Recommended",
    badgeVariant: "success",
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

const defaultEngines: Array<RunEngine> = [
  RunEngine.SkyvernV1,
  RunEngine.SkyvernV3,
  RunEngine.OpenaiCua,
  RunEngine.AnthropicCua,
];

function defaultOption(effectiveDefaultEngine: RunEngine | null): EngineOption {
  return {
    value: DEFAULT_ENGINE_VALUE,
    label: (
      <span className="flex items-center gap-1.5">
        <span>Default</span>
        <span className="text-xs text-muted-foreground">
          {effectiveDefaultEngine === RunEngine.SkyvernV3
            ? "runs on Skyvern 3.0"
            : "follows engine routing"}
        </span>
      </span>
    ),
  };
}

function RunEngineSelector({
  value,
  onChange,
  className,
  availableEngines,
  effectiveDefaultEngine = null,
}: Props) {
  // Without a chosen-engine default, a skyvern-1.0 block is the routed Default, so it cannot be pinned.
  const engines = (availableEngines ?? defaultEngines).filter(
    (engine) => engine !== RunEngine.SkyvernV1 || effectiveDefaultEngine,
  );
  const visibleEngines =
    value && !engines.includes(value) ? [...engines, value] : engines;
  const options: Array<EngineOption> = [
    defaultOption(effectiveDefaultEngine),
    ...engineOptions.filter((opt) => visibleEngines.includes(opt.value)),
  ];
  const selectValue = value ?? DEFAULT_ENGINE_VALUE;
  const selectedOption = options.find((opt) => opt.value === selectValue);

  return (
    <Select
      value={selectValue}
      onValueChange={(next) =>
        onChange(next === DEFAULT_ENGINE_VALUE ? null : (next as RunEngine))
      }
    >
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
        {options.map((option) => (
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
