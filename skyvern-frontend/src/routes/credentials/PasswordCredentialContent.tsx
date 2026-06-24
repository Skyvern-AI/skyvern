import { QRCodeIcon } from "@/components/icons/QRCodeIcon";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/util/utils";
import {
  CheckIcon,
  EnvelopeClosedIcon,
  EyeNoneIcon,
  EyeOpenIcon,
  MobileIcon,
  Pencil1Icon,
  ReloadIcon,
  UploadIcon,
} from "@radix-ui/react-icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { decodeQrCodeImage } from "./decodeQrCodeImage";

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
  /** Login page URL value — when onUrlChange is provided, a URL field is rendered after Name */
  url?: string;
  onUrlChange?: (url: string) => void;
  /** Show a required asterisk on the URL label */
  urlRequired?: boolean;
  /** Disable the URL input (e.g. during test) */
  urlDisabled?: boolean;
  /** Slot rendered between URL and the separator before Username (e.g. browser profile checkbox) */
  afterUrl?: React.ReactNode;
  /** Slot rendered right before the separator between Name/URL and Username/Password */
  beforeCredentialFields?: React.ReactNode;
  editMode?: boolean;
  editingGroups?: { name: boolean; values: boolean };
  onEnableEditName?: () => void;
  onEnableEditValues?: () => void;
  totpError?: string | null;
};

function PasswordCredentialContent({
  values,
  onChange,
  url,
  onUrlChange,
  urlRequired,
  urlDisabled,
  afterUrl,
  beforeCredentialFields,
  editMode,
  editingGroups,
  onEnableEditName,
  onEnableEditValues,
  totpError,
}: Props) {
  const { name, username, password, totp, totp_type, totp_identifier } = values;
  const nameReadOnly = editMode && !editingGroups?.name;
  const valuesReadOnly = editMode && !editingGroups?.values;

  const [totpMethod, setTotpMethod] = useState<
    "authenticator" | "email" | "text"
  >(
    totp_type === "email" || totp_type === "text" ? totp_type : "authenticator",
  );
  const [totpAccordionValue, setTotpAccordionValue] = useState<string>("");
  const [showPassword, setShowPassword] = useState(false);
  const [qrCodeScanError, setQrCodeScanError] = useState<string | null>(null);
  const [isScanningQrCode, setIsScanningQrCode] = useState(false);
  const qrCodeInputRef = useRef<HTMLInputElement>(null);

  // Sync totpMethod and auto-expand accordion when totp_type prop changes
  // (e.g. edit data arriving after mount)
  useEffect(() => {
    setTotpMethod(
      totp_type === "email" || totp_type === "text"
        ? totp_type
        : "authenticator",
    );
    if (totp_type && totp_type !== "none") {
      setTotpAccordionValue("two-factor-authentication");
    }
  }, [totp_type]);
  const prevUsernameRef = useRef(username);
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

  // Keep totp_identifier in sync ONLY when the user renames their username
  // and the identifier was previously auto-filled to match that username.
  // Method-change auto-fill lives in handleTotpMethodChange — that path is
  // the only place we know the change came from the user (not from data
  // hydration, which would silently overwrite a saved identifier).
  useEffect(() => {
    const prevUsername = prevUsernameRef.current;

    if (totpMethod === "email") {
      // prevUsername !== "" guards against an empty-string false-positive
      // during initial hydration (where prev and identifier are both "").
      const usernameChanged = username !== prevUsername;
      const identifierMatchedPrevUsername =
        prevUsername !== "" && totp_identifier === prevUsername;
      if (usernameChanged && identifierMatchedPrevUsername) {
        updateValues({ totp_identifier: username });
      }
    }

    prevUsernameRef.current = username;
  }, [totpMethod, totp_identifier, updateValues, username]);

  // User explicitly switched the 2FA method. Apply method-specific identifier
  // defaults here (rather than in a useEffect) so data-hydration setTotpMethod
  // calls don't accidentally trigger them.
  const handleTotpMethodChange = (
    method: "authenticator" | "email" | "text",
  ) => {
    onEnableEditValues?.();
    const prevMethod = totpMethod;
    setTotpMethod(method);
    setQrCodeScanError(null);

    const updates: Partial<Props["values"]> = {
      totp: method === "authenticator" ? totp : "",
      totp_type: method,
    };

    if (method === "email" && prevMethod !== "email") {
      // Always reseed to username — whatever the previous method left in
      // the field (a phone number from text, or nothing) is unlikely to be
      // a valid email identifier.
      updates.totp_identifier = username;
    }

    if (method === "text" && prevMethod !== "text") {
      // Always clear — an email or username-shaped value from the previous
      // method isn't a valid phone identifier, and we can't infer the user's
      // phone number from email-mode data.
      updates.totp_identifier = "";
    }

    updateValues(updates);
  };

  const handleTotpAccordionValueChange = (value: string) => {
    setTotpAccordionValue(value);
    if (
      value === "two-factor-authentication" &&
      totp_type === "none" &&
      !valuesReadOnly
    ) {
      handleTotpMethodChange(totpMethod);
    }
  };

  const handleAuthenticatorTotpChange = (value: string) => {
    onEnableEditValues?.();
    setQrCodeScanError(null);
    setTotpMethod("authenticator");
    updateValues({ totp: value, totp_type: "authenticator" });
  };

  const handleQrCodeFileChange = async (
    event: React.ChangeEvent<HTMLInputElement>,
  ) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) {
      return;
    }

    onEnableEditValues?.();
    setIsScanningQrCode(true);
    setQrCodeScanError(null);
    try {
      const qrCodeValue = await decodeQrCodeImage(file);
      setTotpMethod("authenticator");
      updateValues({ totp: qrCodeValue, totp_type: "authenticator" });
    } catch (caught) {
      setQrCodeScanError(
        caught instanceof Error
          ? caught.message
          : "Unable to scan that QR code. Paste the setup key instead.",
      );
    } finally {
      setIsScanningQrCode(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-12">
        <div className="w-40 shrink-0">
          <Label>Name</Label>
        </div>
        <div className="relative w-full">
          <Input
            value={name}
            onChange={(e) => updateValues({ name: e.target.value })}
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

      {onUrlChange !== undefined && (
        <>
          <Separator />
          <div className="flex items-center gap-12">
            <div className="w-40 shrink-0">
              <Label>
                Login Page URL
                {urlRequired && <span className="text-destructive"> *</span>}
              </Label>
            </div>
            <Input
              value={url ?? ""}
              onChange={(e) => onUrlChange(e.target.value)}
              placeholder="https://example.com/login"
              disabled={urlDisabled}
            />
          </div>
        </>
      )}
      {afterUrl}
      <Separator />
      <div className="flex items-center gap-12">
        <div className="w-40 shrink-0">
          <Label>Username or Email</Label>
        </div>
        <div className="relative w-full">
          <Input
            value={username}
            onChange={(e) => updateValues({ username: e.target.value })}
            readOnly={valuesReadOnly}
            className={cn({ "pr-9 opacity-70": valuesReadOnly })}
          />
          {valuesReadOnly && (
            <button
              type="button"
              className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
              onClick={onEnableEditValues}
              aria-label="Edit credential values"
            >
              <Pencil1Icon className="size-4" />
            </button>
          )}
        </div>
      </div>
      <div className="flex items-center gap-12">
        <div className="w-40 shrink-0">
          <Label>Password</Label>
        </div>
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
          <div className="relative w-full">
            <Input
              className="pr-9"
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(e) => updateValues({ password: e.target.value })}
              placeholder={editMode ? "••••••••" : undefined}
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
        )}
      </div>
      <Separator />
      <Accordion
        type="single"
        collapsible
        value={totpAccordionValue}
        onValueChange={handleTotpAccordionValueChange}
      >
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
                    "relative flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-transparent bg-slate-elevation1 hover:bg-slate-elevation3",
                    {
                      "border-blue-400 bg-slate-elevation3 ring-1 ring-blue-400/60":
                        totpMethod === "authenticator",
                    },
                  )}
                  onClick={() => handleTotpMethodChange("authenticator")}
                >
                  {totpMethod === "authenticator" && (
                    <span className="absolute right-3 top-3 flex size-5 items-center justify-center rounded-full bg-blue-500 text-white">
                      <CheckIcon className="size-3" />
                    </span>
                  )}
                  <QRCodeIcon className="h-6 w-6" />
                  <Label className="cursor-pointer text-center">
                    Authenticator App
                  </Label>
                </div>
                <div
                  className={cn(
                    "relative flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-transparent bg-slate-elevation1 hover:bg-slate-elevation3",
                    {
                      "border-blue-400 bg-slate-elevation3 ring-1 ring-blue-400/60":
                        totpMethod === "email",
                    },
                  )}
                  onClick={() => handleTotpMethodChange("email")}
                >
                  {totpMethod === "email" && (
                    <span className="absolute right-3 top-3 flex size-5 items-center justify-center rounded-full bg-blue-500 text-white">
                      <CheckIcon className="size-3" />
                    </span>
                  )}
                  <EnvelopeClosedIcon className="h-6 w-6" />
                  <Label className="cursor-pointer text-center">Email</Label>
                </div>
                <div
                  className={cn(
                    "relative flex cursor-pointer items-center justify-center gap-2 rounded-lg border border-transparent bg-slate-elevation1 hover:bg-slate-elevation3",
                    {
                      "border-blue-400 bg-slate-elevation3 ring-1 ring-blue-400/60":
                        totpMethod === "text",
                    },
                  )}
                  onClick={() => handleTotpMethodChange("text")}
                >
                  {totpMethod === "text" && (
                    <span className="absolute right-3 top-3 flex size-5 items-center justify-center rounded-full bg-blue-500 text-white">
                      <CheckIcon className="size-3" />
                    </span>
                  )}
                  <MobileIcon className="h-6 w-6" />
                  <Label className="cursor-pointer text-center">
                    Text Message
                  </Label>
                </div>
              </div>
              {(totpMethod === "text" || totpMethod === "email") && (
                <>
                  <div className="space-y-2">
                    <div className="flex items-center gap-12">
                      <div className="w-40 shrink-0">
                        <Label>{totpIdentifierLabel}</Label>
                      </div>
                      <div className="relative w-full">
                        <Input
                          value={totp_identifier}
                          onChange={(e) =>
                            updateValues({ totp_identifier: e.target.value })
                          }
                          readOnly={valuesReadOnly}
                          className={cn({ "pr-9 opacity-70": valuesReadOnly })}
                        />
                        {valuesReadOnly && (
                          <button
                            type="button"
                            className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center text-muted-foreground hover:text-foreground"
                            onClick={onEnableEditValues}
                            aria-label="Edit credential values"
                          >
                            <Pencil1Icon className="size-4" />
                          </button>
                        )}
                      </div>
                    </div>
                    <p className="mt-1 text-sm text-slate-400">
                      {totpIdentifierHelper}
                    </p>
                  </div>
                  <p className="text-sm text-slate-400">
                    <Link
                      to="https://www.skyvern.com/contact"
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
                        <span className="text-destructive"> *</span>
                      </Label>
                    </div>
                    {valuesReadOnly ? (
                      <div className="relative w-full">
                        <Input
                          value="••••••••"
                          readOnly
                          className="pr-9 opacity-70"
                        />
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
                      <div className="flex w-full gap-2">
                        <Input
                          value={totp}
                          onChange={(e) =>
                            handleAuthenticatorTotpChange(e.target.value)
                          }
                          placeholder="e.g. JBSWY3DPEHPK3PXP"
                          aria-invalid={Boolean(totpError)}
                          className={cn(
                            totpError &&
                              "border-destructive bg-destructive/10 focus-visible:ring-destructive/30",
                          )}
                        />
                        <input
                          ref={qrCodeInputRef}
                          type="file"
                          accept="image/*"
                          className="sr-only"
                          aria-label="Upload QR code image"
                          onChange={(event) =>
                            void handleQrCodeFileChange(event)
                          }
                        />
                        <Button
                          type="button"
                          variant="secondary"
                          className="shrink-0"
                          disabled={isScanningQrCode}
                          onClick={() => qrCodeInputRef.current?.click()}
                        >
                          {isScanningQrCode ? (
                            <ReloadIcon className="mr-2 size-4 animate-spin" />
                          ) : (
                            <UploadIcon className="mr-2 size-4" />
                          )}
                          Scan QR
                        </Button>
                      </div>
                    )}
                  </div>
                  {(totpError || qrCodeScanError) && (
                    <div className="space-y-1 text-xs text-destructive">
                      {totpError && <p>{totpError}</p>}
                      {qrCodeScanError && <p>{qrCodeScanError}</p>}
                    </div>
                  )}
                  <p className="text-sm text-slate-400">
                    You need to find the authenticator key from the website
                    where you are using the credential. Here are some guides
                    from popular password managers:{"  "}
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
