import { useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type Props = {
  chatId: string;
  waitForAccept: (chatId: string) => Promise<void>;
  // This chat's Turn off is already running, including one started before this chip mounted.
  pendingFromChat: boolean;
  onPendingChange: (chatId: string, pending: boolean) => void;
  onTurnedOff: (chatId: string) => void;
};

export function AutoAcceptChip({
  chatId,
  waitForAccept,
  pendingFromChat,
  onPendingChange,
  onTurnedOff,
}: Props) {
  const credentialGetter = useCredentialGetter();
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
      onTurnedOff(chatId);
    } catch (error) {
      console.error("Failed to turn off auto-accept:", error);
      toast({
        title: "Auto-accept is still on",
        description:
          error instanceof Error &&
          error.message === "Wait for the Copilot change to finish"
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
