import { DropdownWithOptions } from "@/components/DropdownWithOptions";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/util/utils";
import { Pencil1Icon } from "@radix-ui/react-icons";

type Props = {
  values: {
    name: string;
    cardNumber: string;
    cardExpirationDate: string;
    cardCode: string;
    cardBrand: string;
    cardHolderName: string;
  };
  onChange: (values: {
    name: string;
    cardNumber: string;
    cardExpirationDate: string;
    cardCode: string;
    cardBrand: string;
    cardHolderName: string;
  }) => void;
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

function formatCardNumber(cardNumber: string) {
  // put spaces every 4 digits
  return cardNumber.replace(/(\d{4})(?=\d)/g, "$1 ");
}

function formatCardExpirationDate(cardExpirationDate: string) {
  // put a slash between the month and year
  return cardExpirationDate.replace(/(\d{2})(?=\d)/g, "$1/");
}

function CreditCardCredentialContent({
  values,
  onChange,
  editMode,
  editingGroups,
  onEnableEditName,
  onEnableEditValues,
}: Props) {
  const nameReadOnly = editMode && !editingGroups?.name;
  const valuesReadOnly = editMode && !editingGroups?.values;

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
    </div>
  );
}

export { CreditCardCredentialContent };
