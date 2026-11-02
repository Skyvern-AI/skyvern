import { useState } from "react";
import { CopyIcon, CheckIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { copyText } from "@/util/copyText";
import { forwardingError, type ForwardingAddress } from "@/api/emailForwarding";

export function ForwardingCopy({
  value,
  label,
}: {
  value: string;
  label: string;
}) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  return (
    <span className="inline-flex items-center gap-2">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={label}
        onClick={async () => {
          const success = await copyText(value);
          setCopied(success);
          setFailed(!success);
        }}
      >
        {copied ? <CheckIcon /> : <CopyIcon />}
        {copied ? "Copied" : "Copy"}
      </Button>
      {failed && (
        <span role="status" className="text-xs text-destructive">
          Select and copy the text manually.
        </span>
      )}
    </span>
  );
}

export function ForwardingStatus({
  status,
}: {
  status: ForwardingAddress["status"];
}) {
  return (
    <Badge
      variant={
        status === "active"
          ? "success"
          : status === "pending"
            ? "warning"
            : "secondary"
      }
      className="capitalize"
    >
      {status}
    </Badge>
  );
}

export function ForwardingError({ error }: { error: unknown }) {
  return (
    <p
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm"
    >
      {forwardingError(error)}
    </p>
  );
}
