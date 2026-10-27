import { useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { zodResolver } from "@hookform/resolvers/zod";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useForm } from "react-hook-form";
import * as z from "zod";
import type {
  OrganizationPhoneNumber,
  TwilioCredentialSafe,
  TwilioPhoneNumberInfo,
} from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { CredentialCombobox } from "@/routes/workflows/components/CredentialCombobox";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableMessageRow,
  TableRow,
} from "@/components/ui/table";
import { useActiveOrgId } from "@/store/ActiveOrgContext";
import {
  TakeoverRequiredError,
  useDeleteTwilioCredentialMutation,
  useDisablePhoneNumberMutation,
  useEnablePhoneNumberMutation,
  useRegisteredPhoneNumbersQuery,
  useSaveTwilioCredentialMutation,
  useTwilioCredentialQuery,
  useTwilioPhoneNumbersQuery,
} from "@/hooks/useTwilioIntegration";
const formSchema = z
  .object({
    account_sid: z
      .string()
      .trim()
      .min(1, "Account SID is required")
      .startsWith("AC", "Account SID must start with AC"),
    api_key_sid: z.string().trim(),
    api_key_secret: z.string().trim(),
    auth_token: z.string().trim().min(1, "Auth Token is required"),
  })
  .superRefine((value, context) => {
    const hasApiKeySid = value.api_key_sid.length > 0;
    const hasApiKeySecret = value.api_key_secret.length > 0;
    if (hasApiKeySid !== hasApiKeySecret) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Provide both the API Key SID and API Key Secret",
        path: hasApiKeySid ? ["api_key_secret"] : ["api_key_sid"],
      });
    }
  });
type FormData = z.infer<typeof formSchema>;
type PendingTakeover = Record<
  "phoneNumberSid" | "phoneNumber" | "phoneNumberId",
  string
> & { error: TakeoverRequiredError };
const NO_CREDENTIAL_VALUE = "__no_credential__";
type QueryState<T> = Pick<
  UseQueryResult<T>,
  "data" | "isLoading" | "isFetching" | "isError" | "error"
>;
const queryHasError = (query: QueryState<unknown>) =>
  Boolean(query.isError || query.error);
const queryIsReady = (query: QueryState<unknown>) =>
  query.data !== undefined &&
  !query.isLoading &&
  !query.isFetching &&
  !queryHasError(query);
function resolveCredentialIdForEnable(
  selectedCredentialId: string | undefined,
  registeredCredentialId: string | null | undefined,
): string | null {
  return selectedCredentialId === NO_CREDENTIAL_VALUE
    ? null
    : (selectedCredentialId ?? registeredCredentialId ?? null);
}
function ConnectForm({
  accountSid = "",
  hasApiKey = false,
}: {
  accountSid?: string;
  hasApiKey?: boolean;
}) {
  const saveCredential = useSaveTwilioCredentialMutation();
  const form = useForm<FormData>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      account_sid: accountSid,
      api_key_sid: "",
      api_key_secret: "",
      auth_token: "",
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {accountSid ? "Update credentials" : "Connect Twilio"}
        </CardTitle>
        <CardDescription>
          Add an account credential to receive SMS verification codes through
          your Twilio phone numbers.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Form {...form}>
          <form
            className="space-y-4"
            onSubmit={form.handleSubmit((data) => {
              saveCredential.mutate(
                {
                  account_sid: data.account_sid,
                  api_key_sid: data.api_key_sid || null,
                  api_key_secret: data.api_key_secret || null,
                  auth_token: data.auth_token,
                },
                {
                  onSuccess: () =>
                    form.reset({
                      account_sid: data.account_sid,
                      api_key_sid: "",
                      api_key_secret: "",
                      auth_token: "",
                    }),
                },
              );
            })}
          >
            <FormField
              control={form.control}
              name="account_sid"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Account SID</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      autoComplete="off"
                      placeholder="AC..."
                      disabled={saveCredential.isPending}
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <div className="grid gap-4 sm:grid-cols-2">
              <FormField
                control={form.control}
                name="api_key_sid"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>API Key SID</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        autoComplete="off"
                        placeholder="SK..."
                        disabled={saveCredential.isPending}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="api_key_secret"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>API Key Secret</FormLabel>
                    <FormControl>
                      <Input
                        {...field}
                        type="password"
                        autoComplete="new-password"
                        disabled={saveCredential.isPending}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            {accountSid && hasApiKey ? (
              <p className="text-sm text-muted-foreground">
                This update replaces the stored credential; enter the API Key
                SID and Secret again to keep them.
              </p>
            ) : null}
            <FormField
              control={form.control}
              name="auth_token"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Auth Token</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      type="password"
                      autoComplete="new-password"
                      disabled={saveCredential.isPending}
                    />
                  </FormControl>
                  <FormDescription>
                    Twilio uses the Auth Token to sign inbound webhooks. An API
                    Key Secret cannot validate webhook signatures.
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
            <Button type="submit" disabled={saveCredential.isPending}>
              {saveCredential.isPending ? (
                <ReloadIcon className="mr-2 size-4 animate-spin" />
              ) : null}
              {accountSid ? "Save credentials" : "Connect Twilio"}
            </Button>
          </form>
        </Form>
      </CardContent>
    </Card>
  );
}
function TakeoverField({
  label,
  value,
  copyable = true,
}: {
  label: string;
  value: string | null;
  copyable?: boolean;
}) {
  return (
    <div className="grid gap-1 rounded-md border bg-muted/30 p-3">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="flex min-w-0 items-center gap-1">
        <span className="min-w-0 flex-1 truncate text-xs">
          {value || "None"}
        </span>
        {copyable && value ? (
          <CopyButton
            value={value}
            className="size-7 shrink-0"
            ariaLabel={`Copy ${label}`}
          />
        ) : null}
      </div>
    </div>
  );
}
const needsDisable = (number: OrganizationPhoneNumber) =>
  number.provider === "twilio" &&
  (number.status === "active" || number.status === "quarantined");
function ConnectedPanel({
  credential,
  credentialQuery,
  phoneNumbersQuery,
  registeredPhoneNumbersQuery,
}: {
  credential: TwilioCredentialSafe;
  credentialQuery: QueryState<{
    available: boolean;
    credential: TwilioCredentialSafe | null;
  }>;
  phoneNumbersQuery: QueryState<TwilioPhoneNumberInfo[]>;
  registeredPhoneNumbersQuery: QueryState<OrganizationPhoneNumber[]>;
}) {
  const [editingCredential, setEditingCredential] = useState(false);
  const [disconnectOpen, setDisconnectOpen] = useState(false);
  const [pendingTakeover, setPendingTakeover] =
    useState<PendingTakeover | null>(null);
  const [selectedCredentialIds, setSelectedCredentialIds] = useState<
    Record<string, string>
  >({});
  const deleteCredential = useDeleteTwilioCredentialMutation();
  const enablePhoneNumber = useEnablePhoneNumberMutation();
  const disablePhoneNumber = useDisablePhoneNumberMutation();
  const isUpdatingPhoneNumber =
    enablePhoneNumber.isPending || disablePhoneNumber.isPending;
  const providerNumbers = phoneNumbersQuery.data ?? [];
  const registeredPhoneNumbers = registeredPhoneNumbersQuery.data ?? [];
  const connectedNumbers = registeredPhoneNumbers.filter(needsDisable);
  const phoneNumbers: TwilioPhoneNumberInfo[] = [
    ...providerNumbers,
    ...connectedNumbers
      .filter(
        (number) =>
          !providerNumbers.some(
            (phone) => phone.phone_number_id === number.phone_number_id,
          ),
      )
      .map((number) => ({
        sid: number.phone_number_id,
        phone_number_id: number.phone_number_id,
        phone_number: number.phone_number,
        friendly_name: "Not in Twilio inventory",
        sms_capable: false,
        current_sms_url: null,
        enabled: false,
      })),
  ];
  const registryActionsReady =
    [credentialQuery, registeredPhoneNumbersQuery].every(queryIsReady) &&
    !deleteCredential.isPending &&
    !isUpdatingPhoneNumber;
  const actionsReady = registryActionsReady && queryIsReady(phoneNumbersQuery);
  const disconnectReady = registryActionsReady && connectedNumbers.length === 0;
  const maskedAccountSid = `${credential.account_sid.slice(0, 2)}••••${credential.account_sid.slice(-4)}`;
  const maskedApiKeySid = credential.api_key_sid
    ? `${credential.api_key_sid.slice(0, 2)}••••${credential.api_key_sid.slice(-4)}`
    : "Not configured";
  const enable = async (phoneNumber: TwilioPhoneNumberInfo) => {
    if (!actionsReady) return;
    const phoneNumberId = phoneNumber.phone_number_id ?? phoneNumber.sid;
    const credentialId = resolveCredentialIdForEnable(
      selectedCredentialIds[phoneNumberId],
      registeredPhoneNumbers.find(
        (registeredPhoneNumber) =>
          registeredPhoneNumber.phone_number_id === phoneNumber.phone_number_id,
      )?.credential_id,
    );
    try {
      await enablePhoneNumber.mutateAsync({
        phone_number_sid: phoneNumber.sid,
        credential_id: credentialId,
      });
    } catch (error) {
      if (error instanceof TakeoverRequiredError) {
        setPendingTakeover({
          phoneNumberSid: phoneNumber.sid,
          phoneNumber: phoneNumber.phone_number,
          phoneNumberId,
          error,
        });
      }
    }
  };
  const disable = async (phoneNumber: TwilioPhoneNumberInfo) => {
    if (!registryActionsReady || !phoneNumber.phone_number_id) return;
    try {
      await disablePhoneNumber.mutateAsync({
        phone_number_id: phoneNumber.phone_number_id,
      });
    } catch {
      // The mutation reports the API error in a toast.
    }
  };
  const confirmTakeover = async () => {
    if (!pendingTakeover || !actionsReady) return;
    const currentPhoneNumber = phoneNumbers.find(
      (phone) =>
        (phone.phone_number_id ?? phone.sid) ===
          pendingTakeover.phoneNumberId &&
        phone.phone_number === pendingTakeover.phoneNumber &&
        phone.sid === pendingTakeover.phoneNumberSid,
    );
    if (!currentPhoneNumber) {
      setPendingTakeover(null);
      return;
    }
    const credentialId = resolveCredentialIdForEnable(
      selectedCredentialIds[pendingTakeover.phoneNumberId],
      registeredPhoneNumbers.find(
        (registered) =>
          registered.phone_number_id === pendingTakeover.phoneNumberId,
      )?.credential_id,
    );
    try {
      await enablePhoneNumber.mutateAsync({
        phone_number_sid: currentPhoneNumber.sid,
        credential_id: credentialId,
        confirm_takeover: true,
      });
      setPendingTakeover(null);
    } catch (error) {
      if (error instanceof TakeoverRequiredError)
        setPendingTakeover((current) =>
          current ? { ...current, error } : current,
        );
    }
  };
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
          <div className="space-y-1.5">
            <CardTitle className="text-base">Twilio account</CardTitle>
            <CardDescription>
              Credentials stay encrypted and are never shown again.
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setEditingCredential(!editingCredential)}
          >
            {editingCredential ? "Close credentials" : "Update credentials"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setDisconnectOpen(true)}
          >
            Disconnect
          </Button>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <div className="text-xs text-muted-foreground">Account SID</div>
            <code>{maskedAccountSid}</code>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">API Key SID</div>
            <code>{maskedApiKeySid}</code>
          </div>
          <div>
            <div className="mb-1 text-xs text-muted-foreground">Auth Token</div>
            <Badge
              variant={credential.has_auth_token ? "success" : "secondary"}
            >
              {credential.has_auth_token ? "Configured" : "Not configured"}
            </Badge>
          </div>
        </CardContent>
      </Card>
      {editingCredential && (
        <ConnectForm
          accountSid={credential.account_sid}
          hasApiKey={Boolean(
            credential.api_key_sid || credential.has_api_key_secret,
          )}
        />
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Phone numbers</CardTitle>
          <CardDescription>
            Enable a number to forward inbound verification texts to Skyvern.
          </CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Phone number</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>SMS</TableHead>
                <TableHead>Webhook</TableHead>
                <TableHead>Credential</TableHead>
                <TableHead className="text-right">Enabled</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {queryHasError(phoneNumbersQuery) && (
                <TableMessageRow colSpan={6}>
                  Could not load Twilio inventory. Update your credentials or
                  try again.
                </TableMessageRow>
              )}
              {phoneNumbersQuery.isLoading ||
              registeredPhoneNumbersQuery.isLoading ? (
                <TableMessageRow colSpan={6}>
                  Loading phone numbers...
                </TableMessageRow>
              ) : queryHasError(registeredPhoneNumbersQuery) ? (
                <TableMessageRow colSpan={6}>
                  Could not load phone numbers or credential bindings.
                </TableMessageRow>
              ) : phoneNumbers.length === 0 ? (
                <TableMessageRow colSpan={6}>
                  This Twilio account has no phone numbers.
                </TableMessageRow>
              ) : (
                phoneNumbers.map((phoneNumber) => {
                  const phoneNumberId =
                    phoneNumber.phone_number_id ?? phoneNumber.sid;
                  const registered = registeredPhoneNumbers.find(
                    (number) =>
                      number.phone_number_id === phoneNumber.phone_number_id,
                  );
                  const canDisable =
                    registered !== undefined && needsDisable(registered);
                  const selectedCredentialId =
                    selectedCredentialIds[phoneNumberId] ??
                    registered?.credential_id ??
                    "";
                  return (
                    <TableRow key={phoneNumber.sid}>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <code className="whitespace-nowrap text-xs">
                            {phoneNumber.phone_number}
                          </code>
                          <CopyButton
                            value={phoneNumber.phone_number}
                            className="size-7"
                            ariaLabel={`Copy ${phoneNumber.phone_number}`}
                          />
                        </div>
                      </TableCell>
                      <TableCell>{phoneNumber.friendly_name}</TableCell>
                      <TableCell>
                        <Badge
                          variant={
                            phoneNumber.sms_capable ? "success" : "secondary"
                          }
                        >
                          {phoneNumber.sms_capable ? "Capable" : "Unavailable"}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        {phoneNumber.enabled ? (
                          <Badge variant="success">Already ours</Badge>
                        ) : phoneNumber.current_sms_url ? (
                          <div className="min-w-0 space-y-1">
                            <Badge variant="warning">Foreign</Badge>
                            <div className="max-w-40 truncate text-xs text-muted-foreground">
                              {phoneNumber.current_sms_url}
                            </div>
                          </div>
                        ) : (
                          <Badge variant="secondary">None</Badge>
                        )}
                      </TableCell>
                      <TableCell className="min-w-48">
                        <CredentialCombobox
                          value={selectedCredentialId}
                          selectedCredentialId={
                            selectedCredentialId === NO_CREDENTIAL_VALUE
                              ? undefined
                              : selectedCredentialId || undefined
                          }
                          extraOptions={[
                            {
                              value: NO_CREDENTIAL_VALUE,
                              label: "No credential",
                            },
                          ]}
                          onValueChange={(value) => {
                            setPendingTakeover(null);
                            setSelectedCredentialIds((current) => ({
                              ...current,
                              [phoneNumberId]: value,
                            }));
                          }}
                          placeholder="Select credential (optional)"
                          disabled={
                            canDisable ||
                            !phoneNumber.sms_capable ||
                            !actionsReady
                          }
                        />
                      </TableCell>
                      <TableCell className="text-right">
                        {canDisable && !phoneNumber.enabled ? (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!registryActionsReady}
                            onClick={() => void disable(phoneNumber)}
                          >
                            Disable
                          </Button>
                        ) : (
                          <Switch
                            checked={canDisable}
                            disabled={
                              canDisable
                                ? !registryActionsReady
                                : !phoneNumber.sms_capable || !actionsReady
                            }
                            onCheckedChange={() =>
                              void (canDisable
                                ? disable(phoneNumber)
                                : enable(phoneNumber))
                            }
                            aria-label={`${canDisable ? "Disable" : "Enable"} ${phoneNumber.phone_number}`}
                          />
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
      <ConfirmDialog
        open={disconnectOpen}
        onOpenChange={setDisconnectOpen}
        title="Disconnect Twilio?"
        description="This removes the stored Twilio credential. Disable all active and quarantined Twilio numbers first. Skyvern restores previous webhook settings only when the routing still points to Skyvern."
        reversible
        confirmLabel="Disconnect"
        confirmDisabled={!disconnectReady}
        isPending={deleteCredential.isPending}
        contentClassName="z-[60]"
        onConfirm={() => {
          if (!disconnectReady) return;
          deleteCredential.mutate(undefined, {
            onSuccess: () => setDisconnectOpen(false),
          });
        }}
      />
      <ConfirmDialog
        open={pendingTakeover !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingTakeover(null);
          }
        }}
        title={
          pendingTakeover
            ? `Replace the webhook for ${pendingTakeover.phoneNumber}?`
            : "Replace the current webhook?"
        }
        description="This number currently forwards to another webhook. Skyvern will replace it and restore it if you disable."
        reversible
        confirmLabel="Replace webhook"
        confirmVariant="default"
        confirmDisabled={!actionsReady}
        isPending={enablePhoneNumber.isPending}
        contentClassName="z-[60]"
        onConfirm={() => void confirmTakeover()}
      >
        {pendingTakeover ? (
          <div className="space-y-2">
            <TakeoverField
              label="current_sms_url"
              value={pendingTakeover.error.currentSmsUrl}
              copyable={false}
            />
            <TakeoverField
              label="current_sms_method"
              value={pendingTakeover.error.currentSmsMethod}
            />
            <TakeoverField
              label="current_sms_application_sid"
              value={pendingTakeover.error.currentSmsApplicationSid}
            />
          </div>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}
export function TwilioIntegrationPanel() {
  const activeOrgId = useActiveOrgId();
  const credentialQuery = useTwilioCredentialQuery();
  const credentialCapability = credentialQuery.data;
  const registeredPhoneNumbersQuery = useRegisteredPhoneNumbersQuery();
  const phoneNumbersQuery = useTwilioPhoneNumbersQuery({
    enabled:
      credentialCapability?.available === true &&
      credentialCapability.credential !== null,
  });
  if (credentialQuery.isLoading) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 p-6 text-sm text-muted-foreground">
          <ReloadIcon className="size-4 animate-spin" />
          Loading Twilio integration...
        </CardContent>
      </Card>
    );
  }
  if (queryHasError(credentialQuery) || !credentialCapability) {
    return (
      <Alert variant="destructive">
        <AlertDescription>
          Could not load the Twilio integration. Try again.
        </AlertDescription>
      </Alert>
    );
  }
  if (!credentialCapability.available) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Twilio is not available</CardTitle>
          <CardDescription>
            SMS 2FA is not available on this deployment.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!credentialCapability.credential) {
    return <ConnectForm key={activeOrgId} />;
  }
  return (
    <ConnectedPanel
      key={activeOrgId}
      credential={credentialCapability.credential}
      credentialQuery={credentialQuery}
      phoneNumbersQuery={phoneNumbersQuery}
      registeredPhoneNumbersQuery={registeredPhoneNumbersQuery}
    />
  );
}
