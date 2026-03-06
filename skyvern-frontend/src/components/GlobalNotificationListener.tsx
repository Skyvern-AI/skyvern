import { useVerificationCodeAlert } from "@/hooks/useVerificationCodeAlert";
import {
  useNotificationStream,
  type VerificationRequest,
} from "@/hooks/useNotificationStream";
import { enable2faNotifications } from "@/util/env";

function notificationLabel(req: VerificationRequest): string {
  if (req.task_id) return `Task "${req.task_id}"`;
  if (req.workflow_run_id) return `Workflow run "${req.workflow_run_id}"`;
  return "Run";
}

function notificationNavigateUrl(req: VerificationRequest): string | undefined {
  if (req.workflow_run_id) return `/runs/${req.workflow_run_id}/overview`;
  if (req.task_id) return `/runs/${req.task_id}/actions`;
  return undefined;
}

/**
 * Invisible component that fires toast / browser notification / sound
 * for a single active 2FA request. No banner UI — that lives on the
 * per-page detail components only.
 */
function VerificationCodeHandler({ req }: { req: VerificationRequest }) {
  const key = req.task_id ?? req.workflow_run_id ?? "";
  useVerificationCodeAlert({
    isWaitingForCode: true,
    pollingStartedAt: req.polling_started_at ?? null,
    label: notificationLabel(req),
    notificationTag: `2fa-required-${key}`,
    navigateUrl: notificationNavigateUrl(req),
  });
  return null;
}

function GlobalNotificationListener() {
  if (!enable2faNotifications) return null;
  return <GlobalNotificationListenerInner />;
}

function GlobalNotificationListenerInner() {
  const { verificationRequests } = useNotificationStream();

  if (verificationRequests.length === 0) return null;

  return (
    <>
      {verificationRequests.map((req) => {
        const key = req.task_id ?? req.workflow_run_id ?? "";
        return <VerificationCodeHandler key={key} req={req} />;
      })}
    </>
  );
}

export { GlobalNotificationListener };
