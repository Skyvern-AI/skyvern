import { VerificationCodeBanner } from "@/components/VerificationCodeBanner";
import { statusIsFinalized } from "@/routes/tasks/types";
import { useTaskQuery } from "./hooks/useTaskQuery";
import { useQueryClient } from "@tanstack/react-query";
import { useFirstParam } from "@/hooks/useFirstParam";

function TaskRunVerificationCodeForm() {
  const queryClient = useQueryClient();
  const taskId = useFirstParam("taskId", "runId");
  const { data: task } = useTaskQuery({ id: taskId ?? undefined });

  const isTaskFinalized = task ? statusIsFinalized(task) : false;
  const isWaitingForCode =
    !isTaskFinalized && (task?.waiting_for_verification_code ?? false);

  const navigateUrl = taskId ? `/tasks/${taskId}` : undefined;

  return (
    <VerificationCodeBanner
      isWaitingForCode={isWaitingForCode}
      pollingStartedAt={task?.verification_code_polling_started_at ?? null}
      label={`Task "${taskId}"`}
      notificationTag={`2fa-required-${taskId}`}
      navigateUrl={navigateUrl}
      defaultIdentifier={task?.verification_code_identifier ?? null}
      defaultTaskId={taskId}
      onCodeSent={() => queryClient.invalidateQueries({ queryKey: ["task"] })}
    />
  );
}

export { TaskRunVerificationCodeForm };
