import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { EyeNoneIcon, EyeOpenIcon } from "@radix-ui/react-icons";
import { useState } from "react";

type Props = {
  values: {
    name: string;
    secretLabel: string;
    secretValue: string;
  };
  onChange: (values: {
    name: string;
    secretLabel: string;
    secretValue: string;
  }) => void;
};

function SecretCredentialContent({ values, onChange }: Props) {
  const { name, secretLabel, secretValue } = values;
  const [showSecret, setShowSecret] = useState(false);

  return (
    <div className="space-y-4">
      <div className="flex">
        <div className="w-72 shrink-0 space-y-1">
          <Label>Name</Label>
          <div className="text-sm text-slate-400">
            The name of the credential
          </div>
        </div>
        <Input
          value={name}
          onChange={(e) => onChange({ ...values, name: e.target.value })}
        />
      </div>
      <Separator />
      <div className="space-y-2">
        <Label>Secret Label (optional)</Label>
        <Input
          placeholder="e.g., API Key, Bearer Token"
          value={secretLabel}
          onChange={(e) => onChange({ ...values, secretLabel: e.target.value })}
        />
      </div>
      <div className="space-y-2">
        <Label>Secret Value</Label>
        <div className="relative w-full">
          <Input
            className="pr-9"
            type={showSecret ? "text" : "password"}
            value={secretValue}
            onChange={(e) =>
              onChange({ ...values, secretValue: e.target.value })
            }
          />
          <div
            className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center"
            onClick={() => {
              setShowSecret((value) => !value);
            }}
            aria-label="Toggle secret value visibility"
          >
            {showSecret ? (
              <EyeOpenIcon className="size-4" />
            ) : (
              <EyeNoneIcon className="size-4" />
            )}
          </div>
        </div>
        <p className="text-sm text-slate-400">
          {
            "Use in HTTP Request blocks with: {{ credential_name.secret_value }}"
          }
        </p>
      </div>
    </div>
  );
}

export { SecretCredentialContent };
