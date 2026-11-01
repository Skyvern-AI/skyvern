import { useState } from "react";
import { receivedAt, type ForwardingAddress } from "@/api/emailForwarding";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ForwardingCopy, ForwardingStatus } from "./ForwardingUi";
import { ForwardingInbox } from "./ForwardingInbox";

export function ForwardingAddressPanel({
  address,
}: {
  address: ForwardingAddress;
}) {
  const [inboxOpen, setInboxOpen] = useState(false);
  return (
    <div className="space-y-3 rounded-lg border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium">
          Forward verification emails here
        </span>
        <ForwardingStatus status={address.status} />
      </div>
      <div className="flex items-center gap-2">
        <Input
          aria-label="Forwarding address"
          value={address.email_address}
          readOnly
          className="font-mono text-xs"
        />
        <ForwardingCopy
          key={address.email_address}
          value={address.email_address}
          label="Copy forwarding address"
        />
      </div>
      <p className="text-xs text-muted-foreground">
        Anyone with this address can send codes to your organization. Rotate it
        if it is shared unintentionally.
      </p>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          Last received: {receivedAt(address.last_received_at)}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setInboxOpen(true)}
        >
          {address.has_pending_confirmation
            ? "View confirmation"
            : "Open inbox"}
        </Button>
      </div>
      {address.status === "disabled" && (
        <p className="text-sm">
          Delivery is paused. Re-enable this address in Settings to receive
          codes.
        </p>
      )}
      {address.status === "revoked" && (
        <p className="text-sm">
          This address no longer accepts mail. Generate a replacement and update
          your forwarding rule.
        </p>
      )}
      <details className="text-sm">
        <summary className="cursor-pointer font-medium">
          Set up forwarding
        </summary>
        <div className="mt-3 space-y-2 text-muted-foreground">
          <p>
            <strong>Gmail:</strong> Add this address under Settings →
            Forwarding. Open this inbox to confirm the destination, then create
            a filter for verification emails.
          </p>
          <p>
            <strong>Outlook:</strong> Add a forwarding rule for the verification
            emails you want Skyvern to receive.
          </p>
          <p>
            <strong>Microsoft 365:</strong> Your administrator may need to allow
            automatic external forwarding in the outbound spam policy.
          </p>
          <p>
            <strong>Manual forwarding:</strong> Forward a verification email to
            this address while the run waits for its code.
          </p>
          <p>
            If no recent mail appears, check your forwarding rule, Gmail
            confirmation, and your Microsoft 365 policy.
          </p>
          <a
            href="https://docs.skyvern.com/developers/credentials/handle-2fa"
            target="_blank"
            rel="noopener noreferrer"
            className="underline underline-offset-2"
          >
            Forwarding setup guide
          </a>
        </div>
      </details>
      <Dialog open={inboxOpen} onOpenChange={setInboxOpen}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Forwarding inbox</DialogTitle>
            <DialogDescription className="break-all">
              {address.email_address}
            </DialogDescription>
          </DialogHeader>
          {inboxOpen && (
            <ForwardingInbox
              key={address.forwarding_address_id}
              addressId={address.forwarding_address_id}
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
