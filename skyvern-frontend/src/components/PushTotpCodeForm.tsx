import { type FormEventHandler, useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { OtpType, type OtpType as OtpTypeValue } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/components/ui/use-toast";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import {
  buildSendTotpCodeRequest,
  type SendTotpCodeRequest,
} from "./pushTotpCodeRequest";

type Props = {
  className?: string;
  defaultIdentifier?: string | null;
  defaultWorkflowRunId?: string | null;
  defaultWorkflowId?: string | null;
  defaultTaskId?: string | null;
  showAdvancedFields?: boolean;
  onSuccess?: () => void;
};

function PushTotpCodeForm({
  className,
  defaultIdentifier,
  defaultWorkflowRunId,
  defaultWorkflowId,
  defaultTaskId,
  showAdvancedFields = false,
  onSuccess,
}: Props) {
  const [identifier, setIdentifier] = useState(defaultIdentifier?.trim() ?? "");
  const [content, setContent] = useState("");
  const [workflowRunId, setWorkflowRunId] = useState(
    defaultWorkflowRunId?.trim() ?? "",
  );
  const [workflowId, setWorkflowId] = useState(defaultWorkflowId?.trim() ?? "");
  const [taskId, setTaskId] = useState(defaultTaskId?.trim() ?? "");
  const [otpType, setOtpType] = useState<OtpTypeValue>(OtpType.Totp);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const credentialGetter = useCredentialGetter();
  const { toast } = useToast();

  useEffect(() => {
    if (
      typeof defaultIdentifier === "string" &&
      defaultIdentifier.trim() !== "" &&
      identifier.trim() === ""
    ) {
      setIdentifier(defaultIdentifier.trim());
    }
  }, [defaultIdentifier, identifier]);

  useEffect(() => {
    if (
      typeof defaultWorkflowRunId === "string" &&
      defaultWorkflowRunId.trim() !== "" &&
      workflowRunId.trim() === ""
    ) {
      setWorkflowRunId(defaultWorkflowRunId.trim());
    }
  }, [defaultWorkflowRunId, workflowRunId]);

  useEffect(() => {
    if (
      typeof defaultWorkflowId === "string" &&
      defaultWorkflowId.trim() !== "" &&
      workflowId.trim() === ""
    ) {
      setWorkflowId(defaultWorkflowId.trim());
    }
  }, [defaultWorkflowId, workflowId]);

  useEffect(() => {
    if (
      typeof defaultTaskId === "string" &&
      defaultTaskId.trim() !== "" &&
      taskId.trim() === ""
    ) {
      setTaskId(defaultTaskId.trim());
    }
  }, [defaultTaskId, taskId]);

  const trimmedIdentifier = useMemo(() => identifier.trim(), [identifier]);
  const trimmedContent = useMemo(() => content.trim(), [content]);
  const trimmedWorkflowRunId = useMemo(
    () => workflowRunId.trim(),
    [workflowRunId],
  );
  const trimmedWorkflowId = useMemo(() => workflowId.trim(), [workflowId]);
  const trimmedTaskId = useMemo(() => taskId.trim(), [taskId]);

  const canSubmit = trimmedIdentifier !== "" && trimmedContent !== "";

  const mutation = useMutation({
    mutationFn: async (payload: SendTotpCodeRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.post("/credentials/totp", payload);
    },
    onSuccess: () => {
      toast({
        title: "2FA code sent",
        description: "Skyvern will process it shortly.",
      });
      setContent("");
      onSuccess?.();
    },
    onError: () => {
      toast({
        variant: "destructive",
        title: "Failed to send code",
        description: "Check the identifier and message format, then retry.",
      });
    },
  });

  const handleSubmit: FormEventHandler<HTMLFormElement> = (event) => {
    event.preventDefault();
    if (!canSubmit || mutation.isPending) {
      return;
    }

    const payload = buildSendTotpCodeRequest({
      identifier: trimmedIdentifier,
      content: trimmedContent,
      otpType,
      workflowRunId: trimmedWorkflowRunId,
      workflowId: trimmedWorkflowId,
      taskId: trimmedTaskId,
    });

    mutation.mutate(payload);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className={cn("space-y-4", className)}
      autoComplete="off"
    >
      <div className="space-y-1">
        <Label htmlFor="totp-identifier-input">Identifier</Label>
        <Input
          id="totp-identifier-input"
          placeholder="Email or phone receiving the code"
          autoComplete="off"
          value={identifier}
          onChange={(event) => setIdentifier(event.target.value)}
          disabled={mutation.isPending}
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor="totp-content-input">Verification content</Label>
        <AutoResizingTextarea
          id="totp-content-input"
          placeholder={
            otpType === OtpType.MagicLink
              ? "Paste the full email body or magic link"
              : "Paste the full email/SMS body or the 6-digit code"
          }
          value={content}
          onChange={(event) => setContent(event.target.value)}
          readOnly={mutation.isPending}
          className="min-h-[4.5rem]"
        />
        <p className="text-xs text-slate-400">
          We only store this to help the current login. Avoid pasting unrelated
          sensitive data.
        </p>
      </div>
      <div className="space-y-1">
        <Label htmlFor="totp-type-input">OTP Type</Label>
        <Select
          value={otpType}
          onValueChange={(value: OtpTypeValue) => setOtpType(value)}
          disabled={mutation.isPending}
        >
          <SelectTrigger id="totp-type-input" className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={OtpType.Totp}>Numeric code</SelectItem>
            <SelectItem value={OtpType.MagicLink}>Magic link</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {showAdvancedFields && (
        <div className="space-y-2">
          <button
            type="button"
            onClick={() => setAdvancedOpen((current) => !current)}
            className="text-xs text-blue-300 underline-offset-2 hover:text-blue-200"
          >
            {advancedOpen ? "Hide optional metadata" : "Add optional metadata"}
          </button>
          {advancedOpen && (
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <Label htmlFor="totp-workflow-run-input">Agent run ID</Label>
                <Input
                  id="totp-workflow-run-input"
                  placeholder="wr_123"
                  autoComplete="off"
                  value={workflowRunId}
                  onChange={(event) => setWorkflowRunId(event.target.value)}
                  disabled={mutation.isPending}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="totp-workflow-id-input">Agent ID</Label>
                <Input
                  id="totp-workflow-id-input"
                  placeholder="wf_123"
                  autoComplete="off"
                  value={workflowId}
                  onChange={(event) => setWorkflowId(event.target.value)}
                  disabled={mutation.isPending}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="totp-task-id-input">Task ID</Label>
                <Input
                  id="totp-task-id-input"
                  placeholder="tsk_123"
                  autoComplete="off"
                  value={taskId}
                  onChange={(event) => setTaskId(event.target.value)}
                  disabled={mutation.isPending}
                />
              </div>
            </div>
          )}
        </div>
      )}

      <Button type="submit" disabled={!canSubmit || mutation.isPending}>
        {mutation.isPending ? "Sending…" : "Send 2FA Code"}
      </Button>
    </form>
  );
}

export { PushTotpCodeForm };
