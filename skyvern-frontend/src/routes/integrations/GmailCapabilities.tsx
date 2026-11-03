import { useId } from "react";
import {
  CheckIcon,
  PlusIcon,
  ReloadIcon,
  UpdateIcon,
} from "@radix-ui/react-icons";
import type { GoogleOAuthCredential } from "@/api/types";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  googleCredentialGmailCapabilities,
  type GmailCapability,
} from "@/hooks/useGoogleOAuthCredentials";
import { cn } from "@/util/utils";

const CAPABILITIES = [
  {
    id: "read",
    label: "Read inbox",
    description: "Find verification codes and sign-in links.",
  },
  {
    id: "send",
    label: "Send email",
    description: "Let agents send email from this address.",
  },
] as const;

const PILL =
  "inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-0.5 text-xs";
const PILL_BUTTON =
  "transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

type PillsProps = {
  credential: GoogleOAuthCredential;
  needsReconnect?: boolean;
  pendingAction?: GmailCapability | "reconnect" | null;
  disabled?: boolean;
  onEnable: (capability: GmailCapability) => void;
  onReconnect?: () => void;
};

function GmailCapabilityPills({
  credential,
  needsReconnect = false,
  pendingAction = null,
  disabled = false,
  onEnable,
  onReconnect,
}: PillsProps) {
  const name = credential.credential_name;
  const isBusy = disabled || pendingAction !== null;

  if (needsReconnect) {
    return (
      <button
        type="button"
        aria-label={`Reconnect ${name}`}
        disabled={isBusy}
        onClick={onReconnect}
        className={cn(
          PILL,
          PILL_BUTTON,
          "border-amber-500/30 bg-amber-500/10 text-amber-700 hover:bg-amber-500/20 dark:text-amber-500",
        )}
      >
        {pendingAction === "reconnect" ? (
          <ReloadIcon className="size-3 animate-spin" />
        ) : (
          <UpdateIcon className="size-3" />
        )}
        Reconnect
      </button>
    );
  }

  const enabled = googleCredentialGmailCapabilities(credential);
  return (
    <>
      {CAPABILITIES.map(({ id, label }) => {
        if (enabled[id]) {
          return (
            <span
              key={id}
              className={cn(
                PILL,
                "border-green-500/20 bg-green-500/10 text-green-700 dark:text-green-400",
              )}
            >
              <CheckIcon className="size-3" />
              {label}
            </span>
          );
        }
        // The server refuses to add sending to a connection with no recorded account.
        const needsReconnectFirst = id === "send" && !credential.email_address;
        return (
          <button
            key={id}
            type="button"
            aria-label={`Enable ${label.toLowerCase()} for ${name}`}
            title={
              needsReconnectFirst
                ? "Reconnect this account before enabling sending."
                : undefined
            }
            disabled={isBusy || needsReconnectFirst}
            onClick={() => onEnable(id)}
            className={cn(
              PILL,
              PILL_BUTTON,
              "border-dashed border-neutral-400 bg-transparent text-neutral-700 hover:border-neutral-600 hover:bg-neutral-100 dark:border-slate-600 dark:text-slate-300 dark:hover:border-slate-400 dark:hover:bg-slate-800/60 dark:hover:text-slate-100",
            )}
          >
            {pendingAction === id ? (
              <ReloadIcon className="size-3 animate-spin" />
            ) : (
              <PlusIcon className="size-3" />
            )}
            {label}
          </button>
        );
      })}
    </>
  );
}

type CheckboxesProps = {
  value: Record<GmailCapability, boolean>;
  onChange: (value: Record<GmailCapability, boolean>) => void;
};

function GmailCapabilityCheckboxes({ value, onChange }: CheckboxesProps) {
  const idPrefix = useId();

  return (
    <fieldset className="space-y-2">
      <legend className="mb-2 text-sm font-medium">
        What can Skyvern do with this account?
      </legend>
      {CAPABILITIES.map(({ id, label, description }) => (
        <Label
          key={id}
          htmlFor={`${idPrefix}-${id}`}
          className="flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 font-normal leading-normal"
        >
          <Checkbox
            id={`${idPrefix}-${id}`}
            className="mt-0.5"
            checked={value[id]}
            onCheckedChange={(checked) =>
              onChange({ ...value, [id]: checked === true })
            }
          />
          <span>
            <span className="block text-sm font-medium">{label}</span>
            <span className="block text-xs text-muted-foreground">
              {description}
            </span>
          </span>
        </Label>
      ))}
      <p className="text-xs text-muted-foreground">
        Google asks you to approve only what you tick. You can enable the other
        one later.
      </p>
    </fieldset>
  );
}

export { GmailCapabilityCheckboxes, GmailCapabilityPills };
