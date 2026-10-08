import { useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useLogging } from "@/hooks/useLogging";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";
import { getCopilotFailureLogFields } from "./copilotFailureLogFields";

type Props = {
  chatId: string;
  organizationId?: string | null;
  captureProductEvent?: (
    event: string,
    properties: Record<string, unknown>,
  ) => void;
  waitForAccept: (chatId: string) => Promise<void>;
  // This chat's Turn off is already running, including one started before this chip mounted.
  pendingFromChat: boolean;
  onPendingChange: (chatId: string, pending: boolean) => void;
  onTurnedOff: (chatId: string) => void;
};

export function AutoAcceptChip({
  chatId,
  organizationId,
  captureProductEvent,
  waitForAccept,
  pendingFromChat,
  onPendingChange,
  onTurnedOff,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const logging = useLogging();
  const workflowPermanentId = useWorkflowPermanentId();
  const [pending, setPending] = useState(false);
  const busy = pending || pendingFromChat;

  const turnOff = async () => {
    if (busy) {
      return;
    }
    setPending(true);
    onPendingChange(chatId, true);
    try {
      await waitForAccept(chatId);
      const client = await getClient(credentialGetter, "sans-api-v1");
      await client.post("/workflow/copilot/disable-auto-accept", {
        workflow_copilot_chat_id: chatId,
      });
      captureProductEvent?.("copilot.auto_accept.toggled", {
        org_id: organizationId,
        workflow_permanent_id: workflowPermanentId,
        enabled: false,
      });
      onTurnedOff(chatId);
    } catch (error) {
      console.error("Failed to turn off auto-accept:", error);
      const waitingForAccept =
        error instanceof Error &&
        error.message === "Wait for the Copilot change to finish";
      if (!waitingForAccept) {
        const fields = getCopilotFailureLogFields(error);
        const status = fields.http_status;
        const clientError =
          typeof status === "number" && status >= 400 && status < 500;
        logging[clientError ? "warn" : "error"]("Copilot request failed", {
          operation: "proposal_sync",
          workflow_permanent_id: workflowPermanentId,
          chat_id: chatId,
          ...fields,
        });
      }
      toast({
        title: "Auto-accept is still on",
        description:
          waitingForAccept && error instanceof Error
            ? error.message
            : "Could not turn it off. Please try again.",
        variant: "destructive",
      });
    } finally {
      setPending(false);
      onPendingChange(chatId, false);
    }
  };

  return (
    <button
      type="button"
      onClick={turnOff}
      // Not `disabled`: that drops keyboard focus, and a failed request would leave the user nowhere.
      aria-disabled={busy}
      className="flex min-w-0 items-center gap-1.5 rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-disabled:opacity-50"
    >
      <span
        className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-400"
        aria-hidden="true"
      />
      <span className="truncate">Auto-accepting</span>
      <span className="shrink-0 text-foreground underline underline-offset-2">
        Turn off
      </span>
    </button>
  );
}
