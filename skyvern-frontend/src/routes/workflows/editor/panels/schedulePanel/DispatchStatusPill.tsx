import { Pill, type PillTone } from "@/components/StatusBadge";
import type { OneTimeDispatchStatus } from "@/routes/workflows/types/scheduleTypes";

const DISPLAY: Record<
  OneTimeDispatchStatus,
  { tone: PillTone; label: string }
> = {
  pending: { tone: "success", label: "Scheduled" },
  fired: { tone: "neutral", label: "Started" },
  canceled: { tone: "neutral", label: "Canceled" },
  failed: { tone: "danger", label: "Failed to start" },
};

function DispatchStatusPill({
  status,
}: Readonly<{ status: OneTimeDispatchStatus }>) {
  const { tone, label } = DISPLAY[status];
  return <Pill tone={tone}>{label}</Pill>;
}

export { DispatchStatusPill };
