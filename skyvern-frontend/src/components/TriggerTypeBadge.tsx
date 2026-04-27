import { TriggerType } from "@/api/types";
import {
  CalendarIcon,
  CursorArrowIcon,
  LightningBoltIcon,
} from "@radix-ui/react-icons";
import { Tip } from "@/components/Tip";

type Props = {
  triggerType: TriggerType | null | undefined;
};

const triggerConfig: Record<
  TriggerType,
  { icon: React.ReactNode; label: string }
> = {
  [TriggerType.Manual]: {
    icon: <CursorArrowIcon className="size-3.5 text-slate-400" />,
    label: "Manual",
  },
  [TriggerType.Api]: {
    icon: <LightningBoltIcon className="size-3.5 text-amber-400" />,
    label: "API",
  },
  [TriggerType.Scheduled]: {
    icon: <CalendarIcon className="size-3.5 text-blue-400" />,
    label: "Scheduled",
  },
};

function TriggerTypeBadge({ triggerType }: Props) {
  if (!triggerType) {
    return null;
  }

  const config = triggerConfig[triggerType];
  if (!config) {
    return null;
  }

  return (
    <Tip content={config.label}>
      <span className="inline-flex shrink-0 items-center">{config.icon}</span>
    </Tip>
  );
}

export { TriggerTypeBadge };
