import { DropdownWithOptions } from "@/components/DropdownWithOptions";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/util/utils";
import { Pencil1Icon, PlusIcon, TrashIcon } from "@radix-ui/react-icons";

export type CreditCardMetadataEntry = {
  key: string;
  value: string;
};

export type CreditCardCredentialValues = {
  name: string;
  cardNumber: string;
  cardExpirationDate: string;
  cardCode: string;
  cardBrand: string;
  cardHolderName: string;
  billingAddressLine1: string;
  billingAddressLine2: string;
  billingCity: string;
  billingState: string;
  billingStateCode: string;
  billingPostalCode: string;
  billingCountry: string;
  billingCountryCode: string;
  billingEmail: string;
  billingPhone: string;
  metadata: CreditCardMetadataEntry[];
};

type BillingFieldKey =
  | "billingAddressLine1"
  | "billingAddressLine2"
  | "billingCity"
  | "billingState"
  | "billingStateCode"
  | "billingPostalCode"
  | "billingCountry"
  | "billingCountryCode"
  | "billingEmail"
  | "billingPhone";

type Props = {
  values: CreditCardCredentialValues;
  onChange: (values: CreditCardCredentialValues) => void;
  /** Slot rendered right after Name, before the separator */
  beforeCredentialFields?: React.ReactNode;
  editMode?: boolean;
  editingGroups?: { name: boolean; values: boolean };
  onEnableEditName?: () => void;
  onEnableEditValues?: () => void;
};

const brandOptions = [
  "Visa",
  "Mastercard",
  "American Express",
  "Discover",
  "JCB",
  "Diners Club",
  "Maestro",
  "UnionPay",
  "RuPay",
  "Other",
];

const billingFields: Array<{
  label: string;
  key: BillingFieldKey;
  autoComplete?: string;
  className?: string;
}> = [
  {
    label: "Address Line 1",
    key: "billingAddressLine1",
    autoComplete: "billing address-line1",
    className: "md:col-span-3",
  },
  {
    label: "Address Line 2",
    key: "billingAddressLine2",
    autoComplete: "billing address-line2",
    className: "md:col-span-3",
  },
  { label: "City", key: "billingCity", autoComplete: "billing address-level2" },
  {
    label: "State",
    key: "billingState",
    autoComplete: "billing address-level1",
  },
  { label: "State Code", key: "billingStateCode" },
  {
    label: "Postal Code",
    key: "billingPostalCode",
    autoComplete: "billing postal-code",
  },
  {
    label: "Country",
    key: "billingCountry",
    autoComplete: "billing country-name",
  },
  {
    label: "Country Code",
    key: "billingCountryCode",
    autoComplete: "billing country",
  },
  {
    label: "Billing Email",
    key: "billingEmail",
    autoComplete: "billing email",
  },
  { label: "Billing Phone", key: "billingPhone", autoComplete: "billing tel" },
];

function formatCardNumber(cardNumber: string) {
  // put spaces every 4 digits
  return cardNumber.replace(/(\d{4})(?=\d)/g, "$1 ");
}

function formatCardExpirationDate(cardExpirationDate: string) {
  // put a slash between the month and year
  return cardExpirationDate.replace(/(\d{2})(?=\d)/g, "$1/");
}

function updateMetadataEntry(
  entries: CreditCardMetadataEntry[],
  index: number,
  field: keyof CreditCardMetadataEntry,
  value: string,
) {
  return entries.map((entry, entryIndex) =>
    entryIndex === index ? { ...entry, [field]: value } : entry,
  );
}

function removeMetadataEntry(
  entries: CreditCardMetadataEntry[],
  index: number,
) {
  if (entries.length <= 1) {
    return [{ key: "", value: "" }];
  }
  return entries.filter((_, entryIndex) => entryIndex !== index);
}

function CreditCardCredentialContent({
  values,
  onChange,
  beforeCredentialFields,
  editMode,
  editingGroups,
  onEnableEditName,
  onEnableEditValues,
}: Props) {
  const nameReadOnly = editMode && !editingGroups?.name;
  const valuesReadOnly = editMode && !editingGroups?.values;
  const optionalFieldClassName = cn({ "opacity-70": valuesReadOnly });

  return (
    <div className="space-y-4">
      <div className="flex">
        <div className="w-72 shrink-0 space-y-1">
          <div>Name</div>
          <div className="text-sm text-slate-400">
            The name of the credential
          </div>
        </div>
        <div className="relative w-full">
          <Input
            value={values.name}
            onChange={(e) => onChange({ ...values, name: e.target.value })}
            readOnly={nameReadOnly}
            className={cn({ "pr-9 opacity-70": nameReadOnly })}
          />
          {nameReadOnly && (
            <button
              type="button"
              className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
              onClick={onEnableEditName}
              aria-label="Edit name"
            >
              <Pencil1Icon className="size-4" />
            </button>
          )}
        </div>
      </div>
      {beforeCredentialFields}
      <Separator />
      <div className="space-y-2">
        <Label>Cardholder Name</Label>
        {valuesReadOnly ? (
          <div className="relative w-full">
            <Input value="••••••••" readOnly className="pr-9 opacity-70" />
            <button
              type="button"
              className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
              onClick={onEnableEditValues}
              aria-label="Edit credential values"
            >
              <Pencil1Icon className="size-4" />
            </button>
          </div>
        ) : (
          <Input
            value={values.cardHolderName}
            onChange={(e) =>
              onChange({ ...values, cardHolderName: e.target.value })
            }
            placeholder={editMode ? "••••••••" : undefined}
          />
        )}
      </div>
      <div className="space-y-2">
        <Label>Number</Label>
        {valuesReadOnly ? (
          <div className="relative w-full">
            <Input value="••••••••" readOnly className="pr-9 opacity-70" />
            <button
              type="button"
              className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
              onClick={onEnableEditValues}
              aria-label="Edit credential values"
            >
              <Pencil1Icon className="size-4" />
            </button>
          </div>
        ) : (
          <Input
            value={values.cardNumber}
            onChange={(event) => {
              onChange({
                ...values,
                cardNumber: formatCardNumber(event.target.value),
              });
            }}
            pattern="[0-9]{13,19}"
            placeholder={editMode ? "••••••••" : "XXXX XXXX XXXX XXXX"}
            maxLength={19}
            autoComplete="cc-number"
          />
        )}
      </div>
      <div className="space-y-2">
        <Label>Brand</Label>
        <DropdownWithOptions
          options={brandOptions.map((brand) => ({
            label: brand,
            value: brand,
          }))}
          value={values.cardBrand}
          onChange={(value) => onChange({ ...values, cardBrand: value })}
          placeholder="Select Brand"
          disabled={valuesReadOnly}
        />
      </div>
      <div className="flex gap-2">
        <div className="space-y-2">
          <Label>Expiration</Label>
          {valuesReadOnly ? (
            <div className="relative w-full">
              <Input value="••••••••" readOnly className="pr-9 opacity-70" />
              <button
                type="button"
                className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
                onClick={onEnableEditValues}
                aria-label="Edit credential values"
              >
                <Pencil1Icon className="size-4" />
              </button>
            </div>
          ) : (
            <Input
              value={values.cardExpirationDate}
              onChange={(event) => {
                onChange({
                  ...values,
                  cardExpirationDate: formatCardExpirationDate(
                    event.target.value,
                  ),
                });
              }}
              placeholder={editMode ? "••••••••" : "MM/YY"}
              pattern="[0-9]{2}/[0-9]{2}"
              maxLength={5}
            />
          )}
        </div>
        <div className="space-y-2">
          <Label>CVV</Label>
          {valuesReadOnly ? (
            <div className="relative w-full">
              <Input value="••••••••" readOnly className="pr-9 opacity-70" />
              <button
                type="button"
                className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
                onClick={onEnableEditValues}
                aria-label="Edit credential values"
              >
                <Pencil1Icon className="size-4" />
              </button>
            </div>
          ) : (
            <Input
              value={values.cardCode}
              onChange={(event) => {
                onChange({ ...values, cardCode: event.target.value });
              }}
              placeholder={editMode ? "••••••••" : "XXX"}
              pattern="[0-9]{3,4}"
              maxLength={4}
            />
          )}
        </div>
      </div>
      <Separator />
      <div className="space-y-3">
        <div className="text-sm font-medium">Billing Details</div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          {billingFields.map((field) => (
            <div className={cn("space-y-2", field.className)} key={field.key}>
              <Label>{field.label}</Label>
              <Input
                value={values[field.key]}
                onChange={(e) =>
                  onChange({ ...values, [field.key]: e.target.value })
                }
                readOnly={valuesReadOnly}
                className={optionalFieldClassName}
                autoComplete={field.autoComplete}
              />
            </div>
          ))}
        </div>
      </div>
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm font-medium">Metadata</div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={valuesReadOnly}
            onClick={() =>
              onChange({
                ...values,
                metadata: [...values.metadata, { key: "", value: "" }],
              })
            }
          >
            <PlusIcon className="mr-1.5 size-4" />
            Add
          </Button>
        </div>
        <div className="space-y-2">
          {values.metadata.map((entry, index) => (
            <div
              className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_2.25rem] gap-2"
              key={index}
            >
              <Input
                value={entry.key}
                onChange={(e) =>
                  onChange({
                    ...values,
                    metadata: updateMetadataEntry(
                      values.metadata,
                      index,
                      "key",
                      e.target.value,
                    ),
                  })
                }
                readOnly={valuesReadOnly}
                className={optionalFieldClassName}
                placeholder="Key"
                aria-label={`Metadata key ${index + 1}`}
              />
              <Input
                value={entry.value}
                onChange={(e) =>
                  onChange({
                    ...values,
                    metadata: updateMetadataEntry(
                      values.metadata,
                      index,
                      "value",
                      e.target.value,
                    ),
                  })
                }
                readOnly={valuesReadOnly}
                className={optionalFieldClassName}
                placeholder="Value"
                aria-label={`Metadata value ${index + 1}`}
              />
              <Button
                type="button"
                size="icon"
                variant="ghost"
                disabled={valuesReadOnly}
                onClick={() =>
                  onChange({
                    ...values,
                    metadata: removeMetadataEntry(values.metadata, index),
                  })
                }
                aria-label={`Remove metadata row ${index + 1}`}
              >
                <TrashIcon className="size-4" />
              </Button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export { CreditCardCredentialContent };
