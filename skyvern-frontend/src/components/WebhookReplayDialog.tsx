import { useEffect, useMemo, useState } from "react";
import type { AxiosError } from "axios";
import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQuery } from "@tanstack/react-query";

type WebhookPreview = {
  run_id: string;
  run_type: string;
  default_webhook_url: string | null;
  payload: string;
  headers: Record<string, string>;
};

type WebhookReplayResult = {
  run_id: string;
  run_type: string;
  default_webhook_url: string | null;
  target_webhook_url: string | null;
  payload: string;
  headers: Record<string, string>;
  status_code: number | null;
  latency_ms: number | null;
  response_body: string | null;
  error: string | null;
};

export type WebhookReplayDialogProps = {
  runId: string;
  disabled?: boolean;
  triggerLabel?: string;
  open?: boolean;
  onOpenChange?: (nextOpen: boolean) => void;
  hideTrigger?: boolean;
};

const formatJson = (raw: string | undefined) => {
  if (!raw) {
    return "";
  }
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
};

export function WebhookReplayDialog({
  runId,
  disabled = false,
  triggerLabel = "Replay Webhook",
  open: controlledOpen,
  onOpenChange,
  hideTrigger = false,
}: WebhookReplayDialogProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const isControlled = controlledOpen !== undefined;
  const open = isControlled ? (controlledOpen as boolean) : uncontrolledOpen;
  const [targetUrl, setTargetUrl] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [replayResult, setReplayResult] = useState<WebhookReplayResult | null>(
    null,
  );

  const credentialGetter = useCredentialGetter();

  const previewQuery = useQuery<
    WebhookPreview,
    AxiosError<{ detail?: string }>
  >({
    queryKey: ["webhookReplayPreview", runId],
    enabled: open && runId.length > 0,
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get(`/internal/runs/${runId}/test-webhook`);
      return response.data as WebhookPreview;
    },
  });

  useEffect(() => {
    if (previewQuery.data) {
      setTargetUrl(previewQuery.data.default_webhook_url ?? "");
      setFormError(null);
      setReplayResult(null);
    }
  }, [previewQuery.data]);

  const replayMutation = useMutation<
    WebhookReplayResult,
    AxiosError<{ detail?: string }>,
    string | null
  >({
    mutationFn: async (overrideUrl: string | null) => {
      const client = await getClient(credentialGetter);
      const response = await client.post(
        `/internal/runs/${runId}/test-webhook`,
        {
          override_webhook_url: overrideUrl,
        },
      );
      return response.data as WebhookReplayResult;
    },
    onSuccess: (data) => {
      setReplayResult(data);
      setFormError(null);
      const isSuccessful =
        data.error === null &&
        data.status_code !== null &&
        data.status_code >= 200 &&
        data.status_code < 300;

      toast({
        variant: isSuccessful ? "success" : "destructive",
        title: isSuccessful
          ? "Webhook replay sent"
          : "Replay completed with issues",
        description:
          data.status_code !== null
            ? `Received status ${data.status_code}${
                data.latency_ms !== null ? ` in ${data.latency_ms} ms` : ""
              }.`
            : "Replay request dispatched.",
      });
    },
    onError: (error) => {
      const detail = error.response?.data?.detail ?? error.message;
      setFormError(detail);
      toast({
        variant: "destructive",
        title: "Replay failed",
        description: detail,
      });
    },
  });

  const previewErrorMessage =
    previewQuery.error?.response?.data?.detail ??
    previewQuery.error?.message ??
    null;

  const payloadText = useMemo(() => {
    const payload =
      replayResult?.payload ?? previewQuery.data?.payload ?? undefined;
    return formatJson(payload);
  }, [replayResult?.payload, previewQuery.data?.payload]);
  const defaultUrl = previewQuery.data?.default_webhook_url?.trim() ?? "";

  const handleSend = () => {
    if (!previewQuery.data || replayMutation.isPending) {
      return;
    }
    const trimmed = targetUrl.trim();

    if (!trimmed && !defaultUrl) {
      setFormError("Provide a webhook URL before sending.");
      return;
    }

    const override =
      trimmed.length > 0 && trimmed !== defaultUrl ? trimmed : null;

    setFormError(null);
    replayMutation.mutate(override);
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (isControlled) {
      onOpenChange?.(nextOpen);
    } else {
      setUncontrolledOpen(nextOpen);
    }
    if (!nextOpen) {
      setReplayResult(null);
      setFormError(null);
      setTargetUrl("");
      replayMutation.reset();
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      {!hideTrigger && (
        <DialogTrigger asChild>
          <Button variant="secondary" disabled={disabled || runId.length === 0}>
            {triggerLabel}
          </Button>
        </DialogTrigger>
      )}
      <DialogContent className="max-h-[85vh] max-w-3xl gap-3 overflow-y-auto">
        <DialogHeader className="space-y-1">
          <DialogTitle>Test Webhook</DialogTitle>
          <DialogDescription>
            Resend the payload generated for this run or override the
            destination URL before sending.
          </DialogDescription>
        </DialogHeader>
        {previewQuery.isError && (
          <div className="rounded-md border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
            {previewErrorMessage ??
              "Unable to load webhook payload for this run."}
          </div>
        )}

        <div className="space-y-3">
          <div className="space-y-2">
            <Label htmlFor="webhook-replay-url">Webhook URL</Label>
            <Input
              id="webhook-replay-url"
              placeholder="https://example.com/webhook"
              value={targetUrl}
              onChange={(event) => setTargetUrl(event.target.value)}
              disabled={replayMutation.isPending}
            />
            {formError ? (
              <p className="text-sm text-destructive">{formError}</p>
            ) : null}
            <div className="flex items-center gap-3 pt-1">
              <Button
                onClick={handleSend}
                disabled={
                  replayMutation.isPending || (!targetUrl.trim() && !defaultUrl)
                }
              >
                {replayMutation.isPending && (
                  <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                )}
                Send Payload
              </Button>
              {replayResult?.target_webhook_url && (
                <span className="text-xs text-muted-foreground">
                  Last sent to {replayResult.target_webhook_url}
                </span>
              )}
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>Payload</Label>
              {previewQuery.isLoading && !replayResult && (
                <span className="text-xs text-muted-foreground">
                  Loading preview…
                </span>
              )}
            </div>
            {previewQuery.isLoading && !replayResult ? null : (
              <CodeEditor
                language="json"
                value={payloadText || "{}"}
                readOnly
                minHeight="200px"
                maxHeight="360px"
              />
            )}
          </div>

          {replayResult && (
            <div className="max-h-[320px] space-y-3 overflow-y-auto rounded-md border border-slate-200 p-4">
              <div className="flex flex-wrap gap-4 text-sm">
                <span>
                  <span className="font-medium">Status:</span>{" "}
                  {replayResult.status_code ?? "No response"}
                </span>
                <span>
                  <span className="font-medium">Latency:</span>{" "}
                  {replayResult.latency_ms !== null
                    ? `${replayResult.latency_ms} ms`
                    : "—"}
                </span>
              </div>
              {replayResult.error && (
                <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
                  {replayResult.error}
                </div>
              )}
              {replayResult.response_body && (
                <div className="space-y-2 rounded-md border border-slate-200/80 p-2">
                  <Label>Response Body</Label>
                  <CodeEditor
                    language="json"
                    value={replayResult.response_body}
                    readOnly
                    minHeight="140px"
                    maxHeight="220px"
                  />
                </div>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
