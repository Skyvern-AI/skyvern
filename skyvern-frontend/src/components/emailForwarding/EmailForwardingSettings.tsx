import { isAxiosError } from "axios";
import { useContext, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { EnvelopeClosedIcon, PlusIcon } from "@radix-ui/react-icons";
import CloudContext from "@/store/CloudContext";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import { useScrollToHash } from "@/hooks/useScrollToHash";
import { receivedAt, type ForwardingAddress } from "@/api/emailForwarding";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  useForwardingAction,
  useForwardingAddresses,
  useForwardingCapability,
} from "@/hooks/useEmailForwarding";
import { ForwardingAddressPanel } from "./ForwardingAddressPanel";
import {
  ForwardingCopy,
  ForwardingError,
  ForwardingStatus,
} from "./ForwardingUi";

type PendingAction =
  | { kind: "rotate"; address: ForwardingAddress; managed: boolean }
  | { kind: "revoke"; address: ForwardingAddress };

function isManagedRotationUnavailable(error: unknown): boolean {
  return (
    isAxiosError(error) &&
    error.response?.status === 409 &&
    error.response.data?.detail ===
      "Not a managed address; rotate it through the standard rotate endpoint."
  );
}

export function EmailForwardingSettings() {
  const orgId = useActiveOrgId();
  return <EmailForwardingSettingsBody key={orgId || "no-active-org"} />;
}

function EmailForwardingSettingsBody() {
  const canList = useForwardingCapability("list");
  const canCreate = useForwardingCapability("create");
  const canUpdate = useForwardingCapability("update");
  const canRevoke = useForwardingCapability("delete");
  const { hash } = useLocation();
  useScrollToHash(canList && hash === "#email-forwarding");
  const isCloud = useContext(CloudContext);
  const addresses = useForwardingAddresses();
  const action = useForwardingAction();
  const [details, setDetails] = useState<ForwardingAddress | null>(null);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [replacement, setReplacement] = useState("");
  const [mintOpen, setMintOpen] = useState(false);
  const [matchMode, setMatchMode] =
    useState<ForwardingAddress["match_mode"]>("dedicated");
  const [allowlist, setAllowlist] = useState("");
  const [rotated, setRotated] = useState<ForwardingAddress | null>(null);
  const [rotatedFromUnboundDedicated, setRotatedFromUnboundDedicated] =
    useState(false);
  const canConfirmPending =
    pending !== null &&
    canList &&
    (pending.kind === "rotate" ? canUpdate : canRevoke);
  useEffect(() => {
    if (!canList) {
      setDetails(null);
      setPending(null);
      setMintOpen(false);
      return;
    }
    if (!canCreate) setMintOpen(false);
    if (pending && (pending.kind === "rotate" ? !canUpdate : !canRevoke)) {
      setPending(null);
    }
  }, [canList, canCreate, canUpdate, canRevoke, pending]);
  if (!canList) return null;
  const addressCount =
    addresses.data?.filter((address) => address.status !== "revoked").length ??
    0;
  const currentDetails =
    addresses.data?.find(
      (address) =>
        address.forwarding_address_id === details?.forwarding_address_id,
    ) ?? details;

  async function confirm() {
    if (
      !pending ||
      !canList ||
      (pending.kind === "rotate" ? !canUpdate : !canRevoke)
    ) {
      return;
    }
    try {
      const result = await action.mutateAsync(
        pending.kind === "rotate"
          ? { ...pending, replacement }
          : { kind: "revoke", address: pending.address },
      );
      if (result && pending.kind === "rotate") {
        setRotated(result);
        setRotatedFromUnboundDedicated(
          pending.address.match_mode === "dedicated" &&
            !pending.address.credential_id,
        );
      }
      setPending(null);
    } catch (error) {
      if (
        pending.kind === "rotate" &&
        pending.managed &&
        isManagedRotationUnavailable(error)
      ) {
        action.reset();
        setReplacement("");
        setPending({ ...pending, managed: false });
        return;
      }
      /* Keep the dialog open and display the server error. */
    }
  }

  return (
    <Card id="email-forwarding">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <EnvelopeClosedIcon />
          Email forwarding
        </CardTitle>
        <CardDescription>
          Receive verification emails without connecting a mailbox. Manage
          addresses and inspect received messages.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {addresses.isPending ? (
          <p role="status" className="text-sm text-muted-foreground">
            Loading forwarding addresses…
          </p>
        ) : addresses.isError ? (
          <ForwardingError error={addresses.error} />
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-sm text-muted-foreground">
                {addressCount} {addressCount === 1 ? "address" : "addresses"} ·{" "}
                {isCloud
                  ? "Included with Pro"
                  : "Bring your own email receiver"}
              </p>
              {isCloud && canCreate && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    action.reset();
                    setMintOpen(true);
                  }}
                >
                  <PlusIcon className="mr-2" />
                  Generate address
                </Button>
              )}
            </div>
            {!isCloud && (
              <p className="text-sm text-muted-foreground">
                Register your receiver and forwarding addresses with the{" "}
                <a
                  className="underline"
                  href="https://docs.skyvern.com/developers/credentials/handle-2fa"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  email forwarding API
                </a>
                . Manage registered addresses here.
              </p>
            )}
            {rotated && (
              <div
                role="status"
                className="rounded-md border border-warning/40 bg-warning/10 p-3 text-sm"
              >
                <p className="font-medium">Update your mail forwarding rule</p>
                <p>
                  The previous address is revoked. Replace it with the new
                  address, confirm the destination if asked, and send a test
                  email.
                </p>
                {rotatedFromUnboundDedicated && (
                  <p>
                    Update the email identifier in any credential that used the
                    old address; it was not linked to a credential, so Skyvern
                    could not update those credentials automatically.
                  </p>
                )}
                <div className="mt-2 flex items-center gap-2">
                  <code className="break-all">{rotated.email_address}</code>
                  <ForwardingCopy
                    value={rotated.email_address}
                    label="Copy replacement address"
                  />
                </div>
              </div>
            )}
            {action.isError && !pending && !mintOpen && (
              <ForwardingError error={action.error} />
            )}
            {addresses.data.length === 0 ? (
              <div className="rounded-md border border-dashed p-6 text-sm text-muted-foreground">
                {isCloud
                  ? "No forwarding addresses yet. Generate one here, or from a saved credential’s Email method to link it directly."
                  : "No forwarding addresses yet. Register an address through the email forwarding API to manage it here."}
              </div>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Address</TableHead>
                      <TableHead>Match mode</TableHead>
                      <TableHead>Linked credential</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Last received</TableHead>
                      <TableHead>
                        <span className="sr-only">Actions</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {addresses.data.map((address) => (
                      <TableRow key={address.forwarding_address_id}>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            <span className="max-w-72 break-words font-mono text-xs">
                              {address.email_address.includes("@") ? (
                                <>
                                  {address.email_address.slice(
                                    0,
                                    address.email_address.lastIndexOf("@") + 1,
                                  )}
                                  <wbr />
                                  {address.email_address.slice(
                                    address.email_address.lastIndexOf("@") + 1,
                                  )}
                                </>
                              ) : (
                                address.email_address
                              )}
                            </span>
                            <ForwardingCopy
                              value={address.email_address}
                              label={`Copy ${address.email_address}`}
                            />
                          </div>
                        </TableCell>
                        <TableCell className="text-xs">
                          {address.match_mode === "dedicated"
                            ? "Dedicated"
                            : "Original recipient"}
                        </TableCell>
                        <TableCell
                          className={
                            address.credential_id != null
                              ? "font-mono text-xs"
                              : "text-xs text-muted-foreground"
                          }
                        >
                          {address.credential_id ?? "Not linked"}
                        </TableCell>
                        <TableCell>
                          <ForwardingStatus status={address.status} />
                        </TableCell>
                        <TableCell className="text-xs">
                          {receivedAt(address.last_received_at)}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => setDetails(address)}
                            >
                              Details
                            </Button>
                            {address.status !== "revoked" && (
                              <>
                                {canUpdate && (
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    disabled={action.isPending}
                                    onClick={() => {
                                      action.reset();
                                      setReplacement("");
                                      setPending({
                                        kind: "rotate",
                                        address,
                                        managed: isCloud,
                                      });
                                    }}
                                  >
                                    Rotate
                                  </Button>
                                )}
                                {canUpdate && (
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    disabled={action.isPending}
                                    onClick={() =>
                                      action.mutate({
                                        kind:
                                          address.status === "disabled"
                                            ? "enable"
                                            : "disable",
                                        address,
                                      })
                                    }
                                  >
                                    {address.status === "disabled"
                                      ? "Enable"
                                      : "Disable"}
                                  </Button>
                                )}
                                {canRevoke && (
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    disabled={action.isPending}
                                    onClick={() => {
                                      action.reset();
                                      setPending({
                                        kind: "revoke",
                                        address,
                                      });
                                    }}
                                  >
                                    Revoke
                                  </Button>
                                )}
                              </>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </>
        )}
        <Dialog
          open={Boolean(details)}
          onOpenChange={(open) => {
            if (!open) setDetails(null);
          }}
        >
          <DialogContent className="max-h-[85vh] max-w-2xl overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Forwarding address</DialogTitle>
              <DialogDescription>
                Setup, delivery status, and received messages.
              </DialogDescription>
            </DialogHeader>
            {currentDetails && (
              <ForwardingAddressPanel address={currentDetails} />
            )}
          </DialogContent>
        </Dialog>
        <Dialog
          open={Boolean(pending) && canConfirmPending}
          onOpenChange={(open) => {
            if (!open && !action.isPending) setPending(null);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>
                {pending?.kind === "rotate"
                  ? "Rotate forwarding address"
                  : "Revoke forwarding address"}
              </DialogTitle>
              <DialogDescription>
                {pending?.kind === "rotate"
                  ? pending.address.match_mode === "dedicated" &&
                    !pending.address.credential_id
                    ? "The previous address will stop receiving mail. Update your forwarding rule and confirm the new destination afterward. Since this dedicated address is not linked to a credential, update each credential using it as an email identifier after rotation."
                    : "The previous address will stop receiving mail. Update your forwarding rule and confirm the new destination afterward."
                  : "This address will permanently stop receiving mail. Its credential binding will be removed."}
              </DialogDescription>
            </DialogHeader>
            {pending?.kind === "rotate" && !pending.managed && (
              <div className="space-y-2">
                <Label htmlFor="replacement-email">
                  Replacement forwarding address
                </Label>
                <Input
                  id="replacement-email"
                  type="email"
                  value={replacement}
                  onChange={(event) => setReplacement(event.target.value)}
                />
              </div>
            )}
            {action.isError && <ForwardingError error={action.error} />}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={action.isPending}
                onClick={() => setPending(null)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                disabled={
                  action.isPending ||
                  !canConfirmPending ||
                  (pending?.kind === "rotate" &&
                    !pending.managed &&
                    !replacement.trim())
                }
                onClick={() => void confirm()}
              >
                {action.isPending
                  ? "Updating…"
                  : pending?.kind === "rotate"
                    ? "Rotate address"
                    : "Revoke address"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <Dialog
          open={mintOpen && canList && canCreate}
          onOpenChange={(open) => {
            if (!action.isPending && canList && canCreate) setMintOpen(open);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Generate a forwarding address</DialogTitle>
              <DialogDescription>
                For a credential-bound address, generate it from the saved
                credential’s Email method.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2">
              <Label htmlFor="forwarding-match-mode">Match mode</Label>
              <Select
                value={matchMode}
                onValueChange={(value: ForwardingAddress["match_mode"]) =>
                  setMatchMode(value)
                }
              >
                <SelectTrigger id="forwarding-match-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="dedicated">Dedicated address</SelectItem>
                  <SelectItem value="original_recipient">
                    Match original recipient
                  </SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {matchMode === "dedicated"
                  ? "Use this forwarding address as the credential’s email identifier."
                  : "Share an address across mailboxes. Each credential uses the original recipient’s email identifier."}
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="forwarding-senders">
                Sender filter (optional)
              </Label>
              <Input
                id="forwarding-senders"
                value={allowlist}
                onChange={(event) => setAllowlist(event.target.value)}
                placeholder="example.com, security@example.test"
              />
              <p className="text-xs text-muted-foreground">
                Comma-separated addresses or domains. This reduces unwanted
                mail; it does not verify the sender.
              </p>
            </div>
            {action.isError && <ForwardingError error={action.error} />}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={action.isPending}
                onClick={() => setMintOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="button"
                disabled={action.isPending || !canList || !canCreate}
                onClick={async () => {
                  if (!canList || !canCreate) return;
                  try {
                    const result = await action.mutateAsync({
                      kind: "mint",
                      input: {
                        match_mode: matchMode,
                        ...(allowlist.trim()
                          ? {
                              sender_allowlist: allowlist
                                .split(",")
                                .map((item) => item.trim())
                                .filter(Boolean),
                            }
                          : {}),
                      },
                    });
                    setMintOpen(false);
                    if (result) setDetails(result);
                  } catch {
                    /* The server error is rendered above. */
                  }
                }}
              >
                {action.isPending ? "Generating…" : "Create address"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}
