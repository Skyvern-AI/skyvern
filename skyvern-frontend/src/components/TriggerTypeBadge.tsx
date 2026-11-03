import { TriggerType } from "@/api/types";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CalendarIcon,
  CodeIcon,
  CubeIcon,
  CursorArrowIcon,
  FileTextIcon,
  LightningBoltIcon,
  Link2Icon,
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
  [TriggerType.Mcp]: {
    icon: <CubeIcon className="size-3.5 text-emerald-400" />,
    label: "MCP",
  },
  [TriggerType.Api]: {
    icon: <CodeIcon className="size-3.5 text-amber-400" />,
    label: "API",
  },
  [TriggerType.Scheduled]: {
    icon: <CalendarIcon className="size-3.5 text-blue-400" />,
    label: "Scheduled",
  },
  [TriggerType.Webhook]: {
    icon: <Link2Icon className="size-3.5 text-cyan-400" />,
    label: "Webhook",
  },
  [TriggerType.Event]: {
    icon: <LightningBoltIcon className="size-3.5 text-violet-400" />,
    label: "Event",
  },
  [TriggerType.JobRecipeExtract]: {
    icon: <ArrowDownIcon className="size-3.5 text-teal-400" />,
    label: "Job recipe extract",
  },
  [TriggerType.JobRecipeApply]: {
    icon: <ArrowUpIcon className="size-3.5 text-orange-400" />,
    label: "Job recipe apply",
  },
  [TriggerType.SeoRecipeSubmit]: {
    icon: <FileTextIcon className="size-3.5 text-lime-400" />,
    label: "SEO recipe submit",
  },
};

function getTriggerTypeLabel(
  triggerType: TriggerType | null | undefined,
): string | undefined {
  return triggerType ? triggerConfig[triggerType]?.label : undefined;
}

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

// eslint-disable-next-line react-refresh/only-export-components -- The shared label is used outside this component.
export { getTriggerTypeLabel, TriggerTypeBadge };
