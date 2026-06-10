import type { WebhookDeliveryStatus as WebhookDeliveryStatusValue } from "@/api/types";
import { cn } from "@/util/utils";

type Props = {
  webhookDeliveryStatus?: WebhookDeliveryStatusValue | null;
  webhookFailureReason?: string | null;
};

function WebhookDeliveryStatus({
  webhookDeliveryStatus,
  webhookFailureReason,
}: Props) {
  const isFailed = webhookDeliveryStatus === "failed";
  const isPending = webhookDeliveryStatus === "pending";

  if (!isFailed && !isPending) {
    return null;
  }

  const title = isFailed
    ? "Webhook Failure Reason"
    : "Webhook Delivery Pending";
  const message = isFailed
    ? (webhookFailureReason ?? "Skyvern recorded a webhook delivery failure.")
    : "Skyvern has started webhook delivery or has not recorded an automatic webhook delivery attempt yet. Delivery will retry automatically.";

  return (
    <div
      role="status"
      className={cn("rounded border bg-slate-elevation2 p-6", {
        "border-yellow-600/50": isFailed,
        "border-sky-500/50": isPending,
      })}
    >
      <h3 className="mb-4 text-lg font-bold">{title}</h3>
      <div
        className={cn("text-sm", {
          "text-yellow-600": isFailed,
          "text-slate-200": isPending,
        })}
      >
        {message}
      </div>
    </div>
  );
}

export { WebhookDeliveryStatus };
