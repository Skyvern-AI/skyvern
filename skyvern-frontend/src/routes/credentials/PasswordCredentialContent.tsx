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
import { useState } from "react";
import { Link } from "react-router-dom";

type Props = {
  values: {
    name: string;
    username: string;
    password: string;
    totp: string;
    totp_type: "authenticator" | "email" | "text" | "none";
  };
  onChange: (values: {
    name: string;
    username: string;
    password: string;
    totp: string;
    totp_type: "authenticator" | "email" | "text" | "none";
  }) => void;
};

function PasswordCredentialContent({
  values: { name, username, password, totp, totp_type },
  onChange,
}: Props) {
  const [totpMethod, setTotpMethod] = useState<
    "authenticator" | "email" | "text"
  >("authenticator");
  const [showPassword, setShowPassword] = useState(false);

  // Update totp_type when totpMethod changes
  const handleTotpMethodChange = (
    method: "authenticator" | "email" | "text",
  ) => {
    setTotpMethod(method);
    onChange({
      name,
      username,
      password,
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
          onChange={(e) =>
            onChange({
              name: e.target.value,
              username,
              password,
              totp,
              totp_type,
            })
          }
        />
      </div>
      <Separator />
      <div className="flex items-center gap-12">
        <div className="w-40 shrink-0">
          <Label>Username or email</Label>
        </div>
        <Input
          value={username}
          onChange={(e) =>
            onChange({
              name,
              username: e.target.value,
              password,
              totp,
              totp_type,
            })
          }
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
            onChange={(e) =>
              onChange({
                name,
                username,
                totp,
                password: e.target.value,
                totp_type,
              })
            }
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
                <p className="text-sm text-slate-400">
                  <Link
                    to="https://meetings.hubspot.com/skyvern/demo"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline underline-offset-2"
                  >
                    Contact us to set up two-factor authentication in workflows
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
                      onChange={(e) =>
                        onChange({
                          name,
                          username,
                          password,
                          totp: e.target.value,
                          totp_type,
                        })
                      }
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
