import { useContext, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  useForwardingAction,
  useForwardingAddresses,
  useForwardingCapability,
} from "@/hooks/useEmailForwarding";
import CloudContext from "@/store/CloudContext";
import { ForwardingAddressPanel } from "./ForwardingAddressPanel";
import { ForwardingCopy, ForwardingError } from "./ForwardingUi";

export function CredentialForwardingAddress({
  credentialId,
  identifier,
  savedTotpType = "none",
  disabled = false,
  onPendingChange,
  onChange,
}: {
  credentialId?: string;
  identifier: string;
  savedTotpType?: string;
  disabled?: boolean;
  onPendingChange?: (pending: boolean) => void;
  onChange: (address: string) => void;
}) {
  const isCloud = useContext(CloudContext);
  const selfHostedGuidance =
    "For self-hosted forwarding, register a receiver and address through the email forwarding API, then enter that address as this credential’s email identifier.";
  const addresses = useForwardingAddresses(Boolean(credentialId));
  const action = useForwardingAction();
  const canList = useForwardingCapability("list");
  const canCreate = useForwardingCapability("create");
  const canRevoke = useForwardingCapability("delete");
  const [revokeOpen, setRevokeOpen] = useState(false);
  useEffect(() => {
    if (!canList || !canRevoke) setRevokeOpen(false);
  }, [canList, canRevoke]);
  const current = addresses.data?.find(
    (address) =>
      address.match_mode === "dedicated" &&
      address.status !== "revoked" &&
      address.email_address === identifier.trim().toLowerCase(),
  );
  if (!canList) return null;
  if (!credentialId)
    return (
      <p className="text-sm text-muted-foreground">
        {isCloud
          ? "To use forwarding, click Email again to clear it, then save this credential. Then edit the credential and choose Email to generate a Skyvern forwarding address."
          : selfHostedGuidance}
      </p>
    );
  if (addresses.isPending)
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Loading forwarding addresses…
      </p>
    );
  if (addresses.isError) return <ForwardingError error={addresses.error} />;
  if (current?.credential_id && current.credential_id !== credentialId)
    return (
      <p role="alert" className="text-sm text-destructive">
        This forwarding address is bound to another credential. Use an unbound
        forwarding address or the address generated for this credential.
      </p>
    );
  if (current)
    return (
      <ForwardingAddressPanel
        key={current.forwarding_address_id}
        address={current}
      />
    );
  const previous = addresses.data?.find(
    (address) =>
      address.match_mode === "dedicated" &&
      address.status !== "revoked" &&
      address.credential_id === credentialId,
  );
  if (previous)
    return (
      <div className="space-y-3 rounded-lg border border-dashed p-4">
        <p className="text-sm font-medium">Previously generated address</p>
        <p className="text-sm text-muted-foreground">
          This credential now uses a different email identifier. To restore
          forwarding, enter this address above and save the credential. To
          generate a replacement, revoke the previous address first.
        </p>
        <div className="flex items-center gap-2">
          <code className="break-all text-xs">{previous.email_address}</code>
          <ForwardingCopy
            value={previous.email_address}
            label="Copy previous forwarding address"
          />
        </div>
        {canRevoke && (
          <Button
            type="button"
            variant="outline"
            disabled={disabled || action.isPending}
            onClick={() => {
              action.reset();
              setRevokeOpen(true);
            }}
          >
            Revoke previous address
          </Button>
        )}
        <Dialog
          open={revokeOpen && canList && canRevoke}
          onOpenChange={(open) => {
            if (!action.isPending && canList && canRevoke) setRevokeOpen(open);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Revoke previous forwarding address</DialogTitle>
              <DialogDescription>
                Mail sent to this address will be permanently dropped. Update
                your forwarding rule after generating a replacement.
              </DialogDescription>
            </DialogHeader>
            {action.isError && <ForwardingError error={action.error} />}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={action.isPending}
                onClick={() => setRevokeOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                disabled={action.isPending || !canList || !canRevoke}
                onClick={async () => {
                  if (!canList || !canRevoke) return;
                  try {
                    await action.mutateAsync({
                      kind: "revoke",
                      address: previous,
                    });
                    setRevokeOpen(false);
                  } catch {
                    /* The server error is rendered in the dialog. */
                  }
                }}
              >
                {action.isPending ? "Revoking…" : "Revoke address"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    );
  if (!isCloud)
    return (
      <p className="text-sm text-muted-foreground">{selfHostedGuidance}</p>
    );
  if (savedTotpType !== "none")
    return (
      <p className="text-sm text-muted-foreground">
        Turn off the current two-factor method and save this credential. Then
        edit it, choose Email, and generate a forwarding address.
      </p>
    );
  if (!canCreate) return null;
  return (
    <div className="space-y-2 rounded-lg border border-dashed p-4">
      <p className="text-sm text-muted-foreground">
        Receive verification emails without connecting your mailbox. Generating
        an address saves this email method to the credential.
      </p>
      {action.isError && <ForwardingError error={action.error} />}
      <Button
        type="button"
        variant="outline"
        disabled={disabled || action.isPending}
        onClick={async () => {
          if (!canList || !canCreate) return;
          onPendingChange?.(true);
          try {
            const result = await action.mutateAsync({
              kind: "mint",
              input: { credential_id: credentialId, match_mode: "dedicated" },
            });
            if (result) {
              onChange(result.email_address);
            }
          } catch {
            /* The mutation error is rendered above. */
          } finally {
            onPendingChange?.(false);
          }
        }}
      >
        {action.isPending
          ? "Generating…"
          : "Generate a Skyvern forwarding address"}
      </Button>
    </div>
  );
}
