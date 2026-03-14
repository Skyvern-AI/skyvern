import { useState } from "react";
import { LockClosedIcon, ClockIcon } from "@radix-ui/react-icons";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { PushTotpCodeForm } from "@/components/PushTotpCodeForm";
import { formatTimeRemaining } from "@/util/timeFormat";
import { useVerificationCodeAlert } from "@/hooks/useVerificationCodeAlert";

type VerificationCodeBannerProps = {
  isWaitingForCode: boolean;
  pollingStartedAt: string | null | undefined;
  label: string;
  notificationTag: string;
  navigateUrl?: string;
  defaultIdentifier: string | null | undefined;
  defaultWorkflowRunId?: string | null;
  defaultWorkflowId?: string | null;
  defaultTaskId?: string | null;
  onCodeSent?: () => void;
};

function VerificationCodeBanner({
  isWaitingForCode,
  pollingStartedAt,
  label,
  notificationTag,
  navigateUrl,
  defaultIdentifier,
  defaultWorkflowRunId,
  defaultWorkflowId,
  defaultTaskId,
  onCodeSent,
}: VerificationCodeBannerProps) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const { timeRemaining, isTimeCritical, isTimedOut } =
    useVerificationCodeAlert({
      isWaitingForCode,
      pollingStartedAt,
      label,
      notificationTag,
      navigateUrl,
    });

  if (!isWaitingForCode) return null;

  const handleSuccess = () => {
    setDialogOpen(false);
    onCodeSent?.();
  };

  return (
    <>
      {/* Slim persistent banner — Figma Option C */}
      <div className="flex items-center justify-between border-b border-amber-500/30 bg-amber-500/10 px-4 py-2.5">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <LockClosedIcon className="h-3.5 w-3.5 flex-shrink-0 text-amber-400" />
          <p className="truncate text-xs text-slate-200">
            <span className="text-slate-100">{label}</span> needs 2FA
          </p>
          {timeRemaining !== null && (
            <span
              className={`ml-2 flex items-center gap-1 text-xs font-medium ${
                isTimeCritical ? "text-red-300" : "text-slate-400"
              }`}
            >
              <ClockIcon className="h-3 w-3" />
              {formatTimeRemaining(timeRemaining)}
            </span>
          )}
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          {isTimedOut && (
            <span className="text-xs text-red-300">Timed out</span>
          )}
          <button
            type="button"
            onClick={() => setDialogOpen(true)}
            className="rounded bg-amber-500/20 px-2 py-1 text-xs font-medium text-amber-400 transition-colors hover:bg-amber-500/30"
          >
            Enter Code
          </button>
        </div>
      </div>

      {/* Dialog with PushTotpCodeForm */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Enter 2FA Verification Code</DialogTitle>
            <DialogDescription>
              Enter the code you received (6-digit code or magic link URL) to
              continue.
            </DialogDescription>
          </DialogHeader>
          <PushTotpCodeForm
            defaultIdentifier={defaultIdentifier}
            defaultWorkflowRunId={defaultWorkflowRunId}
            defaultWorkflowId={defaultWorkflowId}
            defaultTaskId={defaultTaskId}
            showAdvancedFields={false}
            onSuccess={handleSuccess}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}

export { VerificationCodeBanner };
