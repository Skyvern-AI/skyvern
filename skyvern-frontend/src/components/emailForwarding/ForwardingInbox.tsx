import { useContext, useState } from "react";
import { receivedAt } from "@/api/emailForwarding";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useForwardingInbox } from "@/hooks/useEmailForwarding";
import CloudContext from "@/store/CloudContext";
import { ForwardingCopy, ForwardingError } from "./ForwardingUi";

function safeLink(value: string): URL | null {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url : null;
  } catch {
    return null;
  }
}

export function ForwardingInbox({ addressId }: { addressId: string }) {
  const isCloud = useContext(CloudContext);
  const [offset, setOffset] = useState(0);
  const inbox = useForwardingInbox(addressId, offset);
  const olderDisabled = inbox.isFetching || !inbox.data?.hasMore;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          Newest first · text only
          {isCloud && " · content retained for up to 30 days"}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={inbox.isFetching}
          onClick={() => void inbox.refetch()}
        >
          Refresh
        </Button>
      </div>
      {inbox.isPending ? (
        <p role="status">Loading received emails…</p>
      ) : inbox.isError ? (
        <ForwardingError error={inbox.error} />
      ) : inbox.data.emails.length === 0 ? (
        <div className="rounded-md border border-dashed p-6 text-sm text-muted-foreground">
          No emails on this page. Send a test forward, or check whether Gmail
          confirmation is pending. Microsoft 365 may require your administrator
          to allow external forwarding.
        </div>
      ) : (
        <div className="space-y-3">
          {inbox.data.emails.map((email) => {
            const confirmation = email.category === "provider_confirmation";
            const accepted = email.category === "otp" || confirmation;
            const dmarcVerdicts = Object.entries(email.verdicts ?? {})
              .filter(
                ([key]) =>
                  key.replace(/[-_]/g, "").toLowerCase() === "dmarcverdict",
              )
              .map(([, value]) => value.toLowerCase());
            const failedAuthentication = dmarcVerdicts.includes("fail");
            const passedAuthentication =
              dmarcVerdicts.includes("pass") && !failedAuthentication;
            const code =
              confirmation && passedAuthentication
                ? email.body_text.match(/\b\d{6,10}\b/)?.[0]
                : undefined;
            return (
              <article
                key={email.inbound_email_id}
                className="space-y-3 rounded-md border p-4"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="font-medium">
                    {email.subject || "No subject"}
                  </h3>
                  <Badge variant={confirmation ? "warning" : "secondary"}>
                    {email.category.replace(/_/g, " ")}
                  </Badge>
                </div>
                <p className="break-all text-xs text-muted-foreground">
                  {email.from_address} · {receivedAt(email.created_at)}
                </p>
                {confirmation && failedAuthentication && (
                  <p role="alert" className="text-sm text-destructive">
                    This message failed sender authentication. Do not use it to
                    confirm forwarding.
                  </p>
                )}
                {confirmation &&
                  !failedAuthentication &&
                  !passedAuthentication && (
                    <p role="status" className="text-sm text-muted-foreground">
                      Sender authentication could not be verified. Confirm
                      forwarding from your email provider’s settings instead of
                      using this message.
                    </p>
                  )}
                {confirmation && passedAuthentication && (
                  <div className="rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
                    <p className="font-medium">
                      Confirm your forwarding destination
                    </p>
                    <p>
                      Copy the code into your email provider, or review and open
                      its confirmation link below.
                    </p>
                    {code && (
                      <div className="mt-2 flex items-center gap-3">
                        <code className="font-mono text-base">{code}</code>
                        <ForwardingCopy
                          value={code}
                          label={`Copy confirmation code ${code}`}
                        />
                      </div>
                    )}
                  </div>
                )}
                <p className="whitespace-pre-wrap break-words text-sm [overflow-wrap:anywhere]">
                  {email.body_text}
                </p>
                {email.links.length > 0 && (
                  <div className="space-y-2 border-t pt-3">
                    <p className="text-xs text-muted-foreground">
                      {accepted
                        ? "Review the full destination before opening a link."
                        : "Links in dropped messages are disabled."}
                    </p>
                    {email.links.map((link, index) => {
                      const url = safeLink(link.url);
                      const trustedConfirmationHost =
                        confirmation &&
                        passedAuthentication &&
                        url?.protocol === "https:" &&
                        (url.hostname === "mail.google.com" ||
                          url.hostname === "mail-settings.google.com");
                      const allowed =
                        accepted &&
                        url &&
                        (!confirmation || trustedConfirmationHost);
                      return allowed ? (
                        <a
                          key={`${link.url}-${index}`}
                          href={url.href}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="block break-all text-sm text-primary underline underline-offset-2"
                        >
                          {url.href}
                        </a>
                      ) : (
                        <p
                          key={index}
                          className="break-all text-xs text-muted-foreground"
                        >
                          {!accepted
                            ? "Disabled link"
                            : confirmation
                              ? "Unverified confirmation link"
                              : "Unsupported link"}
                          : {url?.href ?? link.url}
                        </p>
                      );
                    })}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
      <div className="flex items-center justify-between">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={offset === 0 || inbox.isFetching}
          onClick={() => setOffset(Math.max(0, offset - 20))}
        >
          Newer
        </Button>
        <span className="text-xs text-muted-foreground">
          Page {offset / 20 + 1}
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={olderDisabled}
          onClick={() => setOffset(offset + 20)}
        >
          Older
        </Button>
      </div>
    </div>
  );
}
