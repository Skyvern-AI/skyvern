import { useState, type ReactNode } from "react";
import { CodeIcon, ExternalLinkIcon } from "@radix-ui/react-icons";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useRegisteredPhoneNumbersQuery } from "@/hooks/useTwilioIntegration";

const NO_PHONE_NUMBER = "__none__";

type Props = {
  value: string | null;
  onChange: (phoneNumber: string | null) => void;
  disabled?: boolean;
  renderManualInput?: () => ReactNode;
};

export function TwilioPhoneNumberPicker({
  value,
  onChange,
  disabled = false,
  renderManualInput,
}: Readonly<Props>) {
  const phoneNumbersQuery = useRegisteredPhoneNumbersQuery();
  const activePhoneNumbers = (phoneNumbersQuery.data ?? []).filter(
    (phoneNumber) => phoneNumber.status === "active",
  );
  const hasUnknownCurrentValue =
    value !== null &&
    !activePhoneNumbers.some(
      (phoneNumber) => phoneNumber.phone_number === value,
    );

  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const noConnectedNumbers =
    phoneNumbersQuery.isError || activePhoneNumbers.length === 0;
  const manual = Boolean(
    renderManualInput &&
    (manualOverride ?? (hasUnknownCurrentValue || noConnectedNumbers)),
  );

  if (phoneNumbersQuery.isLoading) {
    return <Skeleton className="h-9 w-full" />;
  }

  const emptyState = (
    <div className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
      {phoneNumbersQuery.isError ? (
        <>Could not load registered SMS 2FA numbers.</>
      ) : (
        <>
          No SMS 2FA number is connected. Configure one on the{" "}
          <a
            href="/integrations"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 font-medium text-foreground underline underline-offset-2"
          >
            Integrations page
            <ExternalLinkIcon className="size-3" />
          </a>
          .
        </>
      )}
    </div>
  );

  return (
    <div className="space-y-2">
      {manual ? (
        renderManualInput?.()
      ) : noConnectedNumbers && !hasUnknownCurrentValue ? null : (
        <Select
          value={value ?? NO_PHONE_NUMBER}
          onValueChange={(nextValue) =>
            onChange(nextValue === NO_PHONE_NUMBER ? null : nextValue)
          }
          disabled={disabled}
        >
          <SelectTrigger className="nopan text-xs">
            <SelectValue placeholder="Select an SMS 2FA number" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NO_PHONE_NUMBER}>No SMS number</SelectItem>
            {hasUnknownCurrentValue && value !== null ? (
              <SelectItem value={value}>Manual: {value}</SelectItem>
            ) : null}
            {activePhoneNumbers.map((phoneNumber) => (
              <SelectItem
                key={phoneNumber.phone_number_id}
                value={phoneNumber.phone_number}
              >
                <span className="font-mono">{phoneNumber.phone_number}</span>
                {phoneNumber.provider === "skyvern" ? (
                  <span className="ml-2 text-muted-foreground">
                    Skyvern number
                  </span>
                ) : null}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {noConnectedNumbers && (!hasUnknownCurrentValue || renderManualInput)
        ? emptyState
        : null}
      {renderManualInput && (
        <button
          type="button"
          className="inline-flex size-6 items-center justify-center rounded border text-muted-foreground hover:text-foreground"
          aria-label={
            manual ? "Choose a connected number" : "Enter number manually"
          }
          aria-pressed={manual}
          onClick={() => setManualOverride(!manual)}
        >
          <CodeIcon className="size-3.5" />
        </button>
      )}
      {hasUnknownCurrentValue ? (
        <p className="text-xs text-muted-foreground">
          Registered SMS 2FA numbers deliver codes automatically. For this
          number, push codes through the 2FA API or UI.
        </p>
      ) : null}
    </div>
  );
}
