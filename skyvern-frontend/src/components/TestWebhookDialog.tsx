import { useEffect, useState } from "react";
import { ReloadIcon, CopyIcon, CheckIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { copyText } from "@/util/copyText";

type TestWebhookRequest = {
  webhook_url: string;
  run_type: "task" | "workflow_run";
  run_id: string | null;
};

type TestWebhookResponse = {
  status_code: number | null;
  latency_ms: number;
  response_body: string;
  headers_sent: Record<string, string>;
  error: string | null;
};

type TestWebhookDialogProps = {
  runType: "task" | "workflow_run";
  runId?: string | null;
  initialWebhookUrl?: string;
  trigger?: React.ReactNode;
};

function TestWebhookDialog({
  runType,
  runId,
  initialWebhookUrl,
  trigger,
}: TestWebhookDialogProps) {
  const [open, setOpen] = useState(false);
  const [targetUrl, setTargetUrl] = useState(initialWebhookUrl || "");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<TestWebhookResponse | null>(null);
  const [signatureOpen, setSignatureOpen] = useState(false);
  const [responseOpen, setResponseOpen] = useState(false);
  const [copiedResponse, setCopiedResponse] = useState(false);
  const credentialGetter = useCredentialGetter();

  const runTest = async (url: string) => {
    setTargetUrl(url);
    if (!url.trim()) {
      toast({
        variant: "destructive",
        title: "Error",
        description: "Enter a webhook URL before testing.",
      });
      setOpen(false);
      return;
    }

    setLoading(true);
    setResult(null);
    setSignatureOpen(false);
    setResponseOpen(false);
    setCopiedResponse(false);

    try {
      const client = await getClient(credentialGetter);
      const response = await client.post<TestWebhookResponse>(
        "/internal/test-webhook",
        {
          webhook_url: url,
          run_type: runType,
          run_id: runId ?? null,
        } satisfies TestWebhookRequest,
      );

      setResult(response.data);

      if (response.data.error) {
        toast({
          variant: "destructive",
          title: "Webhook Test Failed",
          description: response.data.error,
        });
      } else if (
        response.data.status_code &&
        response.data.status_code >= 200 &&
        response.data.status_code < 300
      ) {
        toast({
          variant: "success",
          title: "Webhook Test Successful",
          description: `Received ${response.data.status_code} response in ${response.data.latency_ms}ms`,
        });
      } else if (response.data.status_code) {
        toast({
          variant: "destructive",
          title: "Webhook Test Failed",
          description: `Received ${response.data.status_code} response`,
        });
      }
    } catch (error) {
      toast({
        variant: "destructive",
        title: "Error",
        description:
          error instanceof Error ? error.message : "Failed to test webhook",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!open) {
      return;
    }

    const nextUrl = initialWebhookUrl || "";
    setTargetUrl(nextUrl);
    void runTest(nextUrl);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialWebhookUrl]);

  const handleCopyResponse = async () => {
    if (!result?.response_body) {
      return;
    }
    try {
      await copyText(result.response_body);
      setCopiedResponse(true);
      setTimeout(() => setCopiedResponse(false), 2000);
    } catch (error) {
      toast({
        variant: "destructive",
        title: "Failed to copy response",
        description:
          error instanceof Error
            ? error.message
            : "Clipboard permissions are required.",
      });
    }
  };

  const getStatusBadgeClass = (statusCode: number | null) => {
    if (!statusCode) return "bg-slate-500";
    if (statusCode >= 200 && statusCode < 300) return "bg-green-600";
    if (statusCode >= 400 && statusCode < 500) return "bg-orange-600";
    if (statusCode >= 500) return "bg-red-600";
    return "bg-blue-600";
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger || (
          <Button type="button" variant="secondary">
            Test Webhook
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Test Webhook URL</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1">
            <Label htmlFor="test-webhook-url">Testing URL</Label>
            <Input
              id="test-webhook-url"
              value={targetUrl}
              onChange={(event) => setTargetUrl(event.target.value)}
              placeholder="https://your-endpoint.com/webhook"
            />
          </div>

          {loading && !result ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <ReloadIcon className="h-4 w-4 animate-spin" />
              Sending test webhook…
            </div>
          ) : null}

          {result && (
            <div className="space-y-4 border-t pt-4">
              {result.error ? (
                <div className="rounded-md border border-red-600 bg-red-50 p-4 dark:bg-red-950">
                  <p className="text-sm font-medium text-red-900 dark:text-red-100">
                    Error
                  </p>
                  <p className="mt-1 text-sm text-red-700 dark:text-red-200">
                    {result.error}
                  </p>
                </div>
              ) : (
                <>
                  <div className="flex items-center gap-4">
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Status
                      </Label>
                      <div className="mt-1 flex items-center gap-2">
                        <span
                          className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium text-white ${getStatusBadgeClass(result.status_code)}`}
                        >
                          {result.status_code || "N/A"}
                        </span>
                      </div>
                    </div>
                    <div>
                      <Label className="text-xs text-muted-foreground">
                        Latency
                      </Label>
                      <p className="mt-1 text-sm font-medium">
                        {result.latency_ms}ms
                      </p>
                    </div>
                  </div>

                  <Collapsible
                    open={responseOpen}
                    onOpenChange={setResponseOpen}
                  >
                    <CollapsibleTrigger asChild>
                      <Button variant="outline" className="w-full">
                        {responseOpen
                          ? "Hide Response Body"
                          : "Show Response Body"}
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="mt-4 space-y-2">
                      <div className="flex items-center justify-between">
                        <Label>Response Body</Label>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={handleCopyResponse}
                        >
                          {copiedResponse ? (
                            <CheckIcon className="h-4 w-4" />
                          ) : (
                            <CopyIcon className="h-4 w-4" />
                          )}
                        </Button>
                      </div>
                      <CodeEditor
                        language="json"
                        value={result.response_body || "Empty response"}
                        readOnly
                        minHeight="100px"
                        maxHeight="300px"
                        className="w-full"
                      />
                    </CollapsibleContent>
                  </Collapsible>

                  <Collapsible
                    open={signatureOpen}
                    onOpenChange={setSignatureOpen}
                  >
                    <CollapsibleTrigger asChild>
                      <Button variant="outline" className="w-full">
                        {signatureOpen
                          ? "Hide Headers Sent"
                          : "Show Headers Sent"}
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="mt-4 space-y-4">
                      <div className="space-y-2">
                        <Label>Headers Sent</Label>
                        <div className="space-y-1 rounded-md border bg-slate-50 p-3 font-mono text-sm dark:bg-slate-950">
                          {Object.entries(result.headers_sent).map(
                            ([key, value]) => (
                              <div key={key}>
                                <span className="text-slate-600 dark:text-slate-400">
                                  {key}:
                                </span>{" "}
                                {value}
                              </div>
                            ),
                          )}
                        </div>
                      </div>
                    </CollapsibleContent>
                  </Collapsible>
                </>
              )}

              <Button
                type="button"
                onClick={() => void runTest(targetUrl)}
                disabled={loading || !targetUrl}
                variant="secondary"
                size="sm"
                className="self-start"
              >
                Retest
              </Button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export { TestWebhookDialog };
