import { useId, useState } from "react";
import { Cross2Icon, ChevronDownIcon } from "@radix-ui/react-icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { TemplateModeToggle } from "@/routes/workflows/components/TemplateModeToggle";

type Props = {
  value: Array<string>;
  onChange: (codes: Array<string>) => void;
  knownErrorCodes: Array<string>;
  readOnly: boolean;
};

export function ErrorCodeChipInput({
  value,
  onChange,
  knownErrorCodes,
  readOnly,
}: Props) {
  const id = useId();
  const [custom, setCustom] = useState(false);
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const available = knownErrorCodes.filter((code) => !value.includes(code));
  function addCode(raw: string) {
    const code = raw.trim();
    if (!readOnly && code && !value.includes(code)) onChange([...value, code]);
    setDraft("");
  }
  return (
    <fieldset disabled={readOnly} className="min-w-0 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id}>Error codes</Label>
        <TemplateModeToggle
          pressed={custom}
          pickerTitle="Choose an error code"
          onToggle={setCustom}
        />
      </div>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {value.map((code) => (
            <Badge key={code} variant="secondary" className="max-w-full gap-1">
              <span className="break-all">{code}</span>
              <button
                type="button"
                aria-label={`Remove error code ${code}`}
                disabled={readOnly}
                onClick={() => onChange(value.filter((item) => item !== code))}
              >
                <Cross2Icon className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      {custom ? (
        <Input
          id={id}
          value={draft}
          disabled={readOnly}
          placeholder={value.length ? "Enter an error code" : "Any error code"}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => addCode(draft)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              addCode(draft);
            }
          }}
        />
      ) : (
        <Popover open={open && !readOnly} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              id={id}
              type="button"
              variant="outline"
              disabled={readOnly}
              role="combobox"
              aria-expanded={open}
              className="w-full justify-between text-xs"
            >
              {value.length ? "Add an error code" : "Any error code"}
              <ChevronDownIcon className="ml-2 size-4 shrink-0" />
            </Button>
          </PopoverTrigger>
          <PopoverContent
            className="w-[--radix-popover-trigger-width] p-0"
            align="start"
          >
            <Command>
              <CommandInput placeholder="Search error codes" />
              <CommandList>
                <CommandEmpty>
                  {knownErrorCodes.length
                    ? "No error codes available."
                    : "No error codes are defined in this workflow yet."}
                </CommandEmpty>
                <CommandGroup>
                  {available.map((code) => (
                    <CommandItem
                      key={code}
                      value={code}
                      onSelect={() => {
                        addCode(code);
                        setOpen(false);
                      }}
                    >
                      {code}
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      )}
    </fieldset>
  );
}
