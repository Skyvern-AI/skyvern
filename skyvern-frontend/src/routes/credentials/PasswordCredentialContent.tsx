import { QRCodeIcon } from "@/components/icons/QRCodeIcon";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/util/utils";
import {
  EnvelopeClosedIcon,
  EyeNoneIcon,
  EyeOpenIcon,
  MobileIcon,
} from "@radix-ui/react-icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

type Props = {
  values: {
    name: string;
    username: string;
    password: string;
    totp: string;
    totp_type: "authenticator" | "email" | "text" | "none";
    totp_identifier: string;
  };
  onChange: (values: {
    name: string;
    username: string;
    password: string;
    totp: string;
    totp_type: "authenticator" | "email" | "text" | "none";
    totp_identifier: string;
  }) => void;
};

function PasswordCredentialContent({ values, onChange }: Props) {
  const { name, username, password, totp, totp_type, totp_identifier } = values;
  const [totpMethod, setTotpMethod] = useState<
    "authenticator" | "email" | "text"
  >(
    totp_type === "email" || totp_type === "text" ? totp_type : "authenticator",
  );
  const [showPassword, setShowPassword] = useState(false);
  const prevUsernameRef = useRef(username);
  const prevTotpMethodRef = useRef<typeof totpMethod>(totpMethod);
  const totpIdentifierLabel =
    totpMethod === "text"
      ? "TOTP Identifier (Phone)"
      : "TOTP Identifier (Username or Email)";
  const totpIdentifierHelper =
    totpMethod === "text"
      ? "Phone number used to receive 2FA codes."
      : "Email address used to receive 2FA codes.";

  const updateValues = useCallback(
    (updates: Partial<Props["values"]>): void => {
      onChange({
        name,
        username,
        password,
        totp,
        totp_type,
        totp_identifier,
        ...updates,
      });
    },
    [name, onChange, password, totp, totp_identifier, totp_type, username],
  );

  useEffect(() => {
    const prevUsername = prevUsernameRef.current;
    const prevMethod = prevTotpMethodRef.current;

    if (totpMethod === "email") {
      const usernameChanged = username !== prevUsername;
      const identifierBlank = totp_identifier.trim() === "";
      const identifierMatchedPrevUsername = totp_identifier === prevUsername;
      const methodChanged = prevMethod !== "email";

      if (
        identifierBlank ||
        methodChanged ||
        (usernameChanged && identifierMatchedPrevUsername)
      ) {
        updateValues({ totp_identifier: username });
      }
    }

    if (totpMethod === "text" && prevMethod !== "text") {
      const wasAutoFilled = totp_identifier === prevUsername;
      if (wasAutoFilled || totp_identifier.trim() === "") {
        updateValues({ totp_identifier: "" });
      }
    }

    prevUsernameRef.current = username;
    prevTotpMethodRef.current = totpMethod;
  }, [totpMethod, totp_identifier, updateValues, username]);

  // Update totp_type when totpMethod changes
  const handleTotpMethodChange = (
    method: "authenticator" | "email" | "text",
  ) => {
    setTotpMethod(method);
    updateValues({
      totp: method === "authenticator" ? totp : "",
      totp_type: method,
    });
  };

  return (
    <div className="space-y-5">
      <div className="flex">
        <div className="w-72 shrink-0 space-y-1">
          <Label>Name</Label>
          <div className="text-sm text-slate-400">
            The name of the credential
          </div>
        </div>
        <Input
          value={name}
          onChange={(e) => updateValues({ name: e.target.value })}
        />
      </div>
      <Separator />
      <div className="flex items-center gap-12">
        <div className="w-40 shrink-0">
          <Label>Username or Email</Label>
        </div>
        <Input
          value={username}
          onChange={(e) => updateValues({ username: e.target.value })}
        />
      </div>
      <div className="flex items-center gap-12">
        <div className="w-40 shrink-0">
          <Label>Password</Label>
        </div>
        <div className="relative w-full">
          <Input
            className="pr-9"
            type={showPassword ? "text" : "password"}
            value={password}
            onChange={(e) => updateValues({ password: e.target.value })}
          />
          <div
            className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center"
            onClick={() => {
              setShowPassword((value) => !value);
            }}
            aria-label="Toggle password visibility"
          >
            {showPassword ? (
              <EyeOpenIcon className="size-4" />
            ) : (
              <EyeNoneIcon className="size-4" />
            )}
          </div>
        </div>
      </div>
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="two-factor-authentication" className="border-b-0">
          <AccordionTrigger className="py-2">
            Two-Factor Authentication
          </AccordionTrigger>
          <AccordionContent>
            <div className="space-y-4">
              <p className="text-sm text-slate-400">
                Set up Skyvern to automatically retrieve two-factor
                authentication codes.
              </p>
              <div className="grid h-36 grid-cols-3 gap-4">
                <div
                  className={cn(
                    "flex cursor-pointer items-center justify-center gap-2 rounded-lg bg-slate-elevation1 hover:bg-slate-elevation3",
                    {
                      "bg-slate-elevation3": totpMethod === "authenticator",
                    },
                  )}
                  onClick={() => handleTotpMethodChange("authenticator")}
                >
                  <QRCodeIcon className="h-6 w-6" />
                  <Label>Authenticator App</Label>
                </div>
                <div
                  className={cn(
                    "flex cursor-pointer items-center justify-center gap-2 rounded-lg bg-slate-elevation1 hover:bg-slate-elevation3",
                    {
                      "bg-slate-elevation3": totpMethod === "email",
                    },
                  )}
                  onClick={() => handleTotpMethodChange("email")}
                >
                  <EnvelopeClosedIcon className="h-6 w-6" />
                  <Label>Email</Label>
                </div>
                <div
                  className={cn(
                    "flex cursor-pointer items-center justify-center gap-2 rounded-lg bg-slate-elevation1 hover:bg-slate-elevation3",
                    {
                      "bg-slate-elevation3": totpMethod === "text",
                    },
                  )}
                  onClick={() => handleTotpMethodChange("text")}
                >
                  <MobileIcon className="h-6 w-6" />
                  <Label>Text Message</Label>
                </div>
              </div>
              {(totpMethod === "text" || totpMethod === "email") && (
                <>
                  <div className="space-y-2">
                    <div className="flex items-center gap-12">
                      <div className="w-40 shrink-0">
                        <Label>{totpIdentifierLabel}</Label>
                      </div>
                      <Input
                        value={totp_identifier}
                        onChange={(e) =>
                          updateValues({ totp_identifier: e.target.value })
                        }
                      />
                    </div>
                    <p className="mt-1 text-sm text-slate-400">
                      {totpIdentifierHelper}
                    </p>
                  </div>
                  <p className="text-sm text-slate-400">
                    <Link
                      to="https://meetings.hubspot.com/skyvern/demo"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2"
                    >
                      Contact us to set up two-factor authentication in
                      workflows
                    </Link>{" "}
                    or{" "}
                    <Link
                      to="https://www.skyvern.com/docs/running-tasks/advanced-features#time-based-one-time-password-totp"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2"
                    >
                      see our documentation on how to set up two-factor
                      authentication in workflows
                    </Link>{" "}
                    to get started.
                  </p>
                </>
              )}
              {totpMethod === "authenticator" && (
                <div className="space-y-4">
                  <div className="flex items-center gap-12">
                    <div className="w-40 shrink-0">
                      <Label className="whitespace-nowrap">
                        Authenticator Key
                      </Label>
                    </div>
                    <Input
                      value={totp}
                      onChange={(e) => updateValues({ totp: e.target.value })}
                    />
                  </div>
                  <p className="text-sm text-slate-400">
                    You need to find the authenticator secret from the website
                    where you are using the credential. Here are some guides
                    from popular authenticator apps:{"  "}
                    <Link
                      to="https://bitwarden.com/help/integrated-authenticator/#manually-enter-a-secret"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2"
                    >
                      Bitwarden
                    </Link>
                    {", "}
                    <Link
                      to="https://support.1password.com/one-time-passwords#on-1passwordcom"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2"
                    >
                      1Password
                    </Link>
                    {", and "}
                    <Link
                      to="https://support.lastpass.com/s/document-item?language=en_US&bundleId=lastpass&topicId=LastPass/create-totp-vault.html&_LANG=enus"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline underline-offset-2"
                    >
                      LastPass
                    </Link>
                    {"."}
                  </p>
                </div>
              )}
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { PasswordCredentialContent };
