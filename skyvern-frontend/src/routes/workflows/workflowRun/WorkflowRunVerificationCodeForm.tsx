import { useEffect, useState, useMemo } from "react";
import { LockClosedIcon, ClockIcon } from "@radix-ui/react-icons";
import { PushTotpCodeForm } from "@/components/PushTotpCodeForm";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useQueryClient } from "@tanstack/react-query";

// Default polling timeout in minutes (matches backend VERIFICATION_CODE_POLLING_TIMEOUT_MINS)
const VERIFICATION_CODE_TIMEOUT_MINS = 15;

function formatTimeRemaining(seconds: number): string {
  if (seconds <= 0) return "0:00";
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

function WorkflowRunVerificationCodeForm() {
  const queryClient = useQueryClient();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();
  const [timeRemaining, setTimeRemaining] = useState<number | null>(null);
  const [hasNotified, setHasNotified] = useState(false);

  const isWaitingForCode = workflowRun?.waiting_for_verification_code ?? false;
  const verificationCodeIdentifier =
    workflowRun?.verification_code_identifier ?? null;
  const pollingStartedAt = workflowRun?.verification_code_polling_started_at;
  const workflowRunId = workflowRun?.workflow_run_id;
  const workflowId = workflowRun?.workflow?.workflow_permanent_id;
  const workflowTitle = workflowRun?.workflow?.title;

  // Calculate initial time remaining and update every second
  useEffect(() => {
    if (!isWaitingForCode || !pollingStartedAt) {
      setTimeRemaining(null);
      setHasNotified(false);
      return;
    }

    const startTime = new Date(pollingStartedAt).getTime();
    const timeoutMs = VERIFICATION_CODE_TIMEOUT_MINS * 60 * 1000;

    const updateTimer = () => {
      const now = Date.now();
      const elapsed = now - startTime;
      const remaining = Math.max(0, Math.ceil((timeoutMs - elapsed) / 1000));
      setTimeRemaining(remaining);
    };

    // Initial update
    updateTimer();

    // Update every second
    const interval = setInterval(updateTimer, 1000);

    return () => clearInterval(interval);
  }, [isWaitingForCode, pollingStartedAt]);

  // Send browser notification when 2FA is needed
  useEffect(() => {
    if (!isWaitingForCode || hasNotified) {
      return;
    }

    // Request notification permission if not already granted
    if (Notification.permission === "default") {
      Notification.requestPermission();
    }

    // Show notification if permission is granted
    if (Notification.permission === "granted") {
      try {
        const notification = new Notification("2FA Code Required", {
          body: `Workflow "${workflowTitle ?? "Run"}" needs a verification code to continue.`,
          icon: "/favicon.png",
          tag: `2fa-required-${workflowRunId}`,
          requireInteraction: true,
        });

        notification.onclick = () => {
          window.focus();
          notification.close();
        };

        setHasNotified(true);
      } catch (error) {
        console.error("Failed to create notification:", error);
      }
    }

    // Play notification sound
    try {
      const audio = new Audio("/dragon-cry.mp3");
      audio.play().catch((error) => {
        console.error("Failed to play notification sound:", error);
      });
    } catch (error) {
      console.error("Failed to create audio:", error);
    }
  }, [isWaitingForCode, hasNotified, workflowRunId, workflowTitle]);

  const handleSuccess = () => {
    // Invalidate the query to refresh the workflow run status
    queryClient.invalidateQueries({
      queryKey: ["workflowRun"],
    });
  };

  const isTimeCritical = useMemo(() => {
    return timeRemaining !== null && timeRemaining <= 60;
  }, [timeRemaining]);

  if (!isWaitingForCode) {
    return null;
  }

  return (
    <div className="space-y-4 rounded-lg border border-amber-500/50 bg-amber-500/10 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <LockClosedIcon className="h-5 w-5 text-amber-400" />
          <h3 className="font-semibold text-amber-200">
            2FA Verification Required
          </h3>
        </div>
        {timeRemaining !== null && (
          <div
            className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-sm font-medium ${
              isTimeCritical
                ? "bg-red-500/20 text-red-300"
                : "bg-slate-700/50 text-slate-300"
            }`}
          >
            <ClockIcon className="h-4 w-4" />
            <span>
              {formatTimeRemaining(timeRemaining)}
              {isTimeCritical && " remaining"}
            </span>
          </div>
        )}
      </div>

      <p className="text-sm text-slate-300">
        This workflow is waiting for a 2FA verification code. Enter the code you
        received (6-digit code or magic link URL) to continue the run.
      </p>

      <PushTotpCodeForm
        className="mt-4"
        defaultIdentifier={verificationCodeIdentifier}
        defaultWorkflowRunId={workflowRunId}
        defaultWorkflowId={workflowId}
        showAdvancedFields={false}
        onSuccess={handleSuccess}
      />

      {timeRemaining !== null && timeRemaining <= 0 && (
        <div className="rounded-md bg-red-500/20 p-3 text-sm text-red-300">
          The verification code polling has timed out. The workflow run may have
          failed. Please check the status.
        </div>
      )}
    </div>
  );
}

export { WorkflowRunVerificationCodeForm };
