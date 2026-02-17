import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogHeader,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  useCredentialModalState,
  CredentialModalTypes,
} from "./useCredentialModalState";
import type { CredentialModalType } from "./useCredentialModalState";
import { PasswordCredentialContent } from "./PasswordCredentialContent";
import { SecretCredentialContent } from "./SecretCredentialContent";
import { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@/components/ui/button";
import { CreditCardCredentialContent } from "./CreditCardCredentialContent";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CreateCredentialRequest,
  CredentialApiResponse,
  isPasswordCredential,
  isCreditCardCredential,
  isSecretCredential,
  TestCredentialStatusResponse,
  TestLoginResponse,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { AxiosError } from "axios";
import {
  CheckCircledIcon,
  CrossCircledIcon,
  InfoCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { getHostname } from "@/util/getHostname";

const PASSWORD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  username: "",
  password: "",
  totp: "",
  totp_type: "none" as "none" | "authenticator" | "email" | "text",
  totp_identifier: "",
};

const CREDIT_CARD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  cardNumber: "",
  cardExpirationDate: "",
  cardCode: "",
  cardBrand: "",
  cardHolderName: "",
};

const SECRET_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  secretLabel: "",
  secretValue: "",
};

// Function to generate a unique credential name
function generateDefaultCredentialName(existingNames: string[]): string {
  const baseName = "credentials";

  // Check if "credentials" is available
  if (!existingNames.includes(baseName)) {
    return baseName;
  }

  // Find the next available number
  let counter = 1;
  while (existingNames.includes(`${baseName}_${counter}`)) {
    counter++;
  }

  return `${baseName}_${counter}`;
}

type Props = {
  onCredentialCreated?: (id: string) => void;
  /** Optional controlled mode: pass isOpen and onOpenChange to control modal state locally */
  isOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** When provided, the modal opens in edit mode and pre-fills available fields */
  editingCredential?: CredentialApiResponse;
  /** Override the modal type (used in edit mode to set the correct form) */
  overrideType?: CredentialModalType;
};

function CredentialsModal({
  onCredentialCreated,
  isOpen: controlledIsOpen,
  onOpenChange: controlledOnOpenChange,
  editingCredential,
  overrideType,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const {
    isOpen: urlIsOpen,
    type: urlType,
    setIsOpen: setUrlIsOpen,
  } = useCredentialModalState();

  const isEditMode = !!editingCredential;

  // Use controlled props if provided, otherwise fall back to URL-based state
  const isOpen = controlledIsOpen ?? urlIsOpen;
  const setIsOpen = controlledOnOpenChange ?? setUrlIsOpen;
  const type = overrideType ?? urlType;
  const { data: credentials } = useCredentialsQuery({
    page_size: 100,
  });
  const [passwordCredentialValues, setPasswordCredentialValues] = useState(
    PASSWORD_CREDENTIAL_INITIAL_VALUES,
  );
  const [creditCardCredentialValues, setCreditCardCredentialValues] = useState(
    CREDIT_CARD_CREDENTIAL_INITIAL_VALUES,
  );
  const [secretCredentialValues, setSecretCredentialValues] = useState(
    SECRET_CREDENTIAL_INITIAL_VALUES,
  );

  // Test & Save Browser Profile state
  const [testAndSave, setTestAndSave] = useState(false);
  const [testUrl, setTestUrl] = useState("");
  const [testStatus, setTestStatus] = useState<
    "idle" | "testing" | "completed" | "failed" | "profile_failed"
  >("idle");
  const [testFailureReason, setTestFailureReason] = useState<string | null>(
    null,
  );
  // The temporary credential ID created by the test-login endpoint
  const [testCredentialId, setTestCredentialId] = useState<string | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearTimeout(pollIntervalRef.current);
      }
    };
  }, []);

  const nameInitializedRef = useRef(false);

  // Set default name when modal opens, or pre-populate fields in edit mode
  useEffect(() => {
    if (!isOpen) {
      nameInitializedRef.current = false;
      return;
    }

    if (isEditMode) {
      reset();
      const cred = editingCredential.credential;
      if (isPasswordCredential(cred)) {
        setPasswordCredentialValues({
          name: editingCredential.name,
          username: cred.username,
          password: "",
          totp: "",
          totp_type: cred.totp_type,
          totp_identifier: cred.totp_identifier ?? "",
        });
      } else if (isCreditCardCredential(cred)) {
        setCreditCardCredentialValues({
          name: editingCredential.name,
          cardNumber: "",
          cardExpirationDate: "",
          cardCode: "",
          cardBrand: cred.brand,
          cardHolderName: "",
        });
      } else if (isSecretCredential(cred)) {
        setSecretCredentialValues({
          name: editingCredential.name,
          secretLabel: cred.secret_label ?? "",
          secretValue: "",
        });
      }
      return;
    }

    if (credentials && !nameInitializedRef.current) {
      nameInitializedRef.current = true;
      const existingNames = credentials.map((c) => c.name);
      const defaultName = generateDefaultCredentialName(existingNames);

      setPasswordCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
      setCreditCardCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
      setSecretCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
    }
  }, [isOpen, credentials, isEditMode, editingCredential]);

  function reset() {
    setPasswordCredentialValues(PASSWORD_CREDENTIAL_INITIAL_VALUES);
    setCreditCardCredentialValues(CREDIT_CARD_CREDENTIAL_INITIAL_VALUES);
    setSecretCredentialValues(SECRET_CREDENTIAL_INITIAL_VALUES);
    setTestAndSave(false);
    setTestUrl("");
    setTestStatus("idle");
    setTestFailureReason(null);
    setTestCredentialId(null);
    if (pollIntervalRef.current) {
      clearTimeout(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }

  const pollTestStatus = useCallback(
    async (credentialId: string, workflowRunId: string) => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<TestCredentialStatusResponse>(
          `/credentials/${credentialId}/test/${workflowRunId}`,
        );
        const data = response.data;

        if (data.status === "completed") {
          pollIntervalRef.current = null;
          queryClient.invalidateQueries({ queryKey: ["credentials"] });

          // Check if login succeeded but browser profile failed to save
          if (data.browser_profile_failure_reason && !data.browser_profile_id) {
            setTestStatus("profile_failed");
            setTestFailureReason(data.browser_profile_failure_reason);
            toast({
              title: "Browser profile was not saved",
              description: data.browser_profile_failure_reason,
              variant: "destructive",
            });
            return;
          }

          setTestStatus("completed");
          const profileHost = data.tested_url
            ? getHostname(data.tested_url)
            : null;
          toast({
            title: "Credential test passed",
            description: data.browser_profile_id
              ? profileHost
                ? `Login successful! Login-free credentials enabled for ${profileHost}`
                : "Login successful! Login-free credentials enabled."
              : "Login successful!",
            variant: "success",
          });
          return;
        } else if (
          data.status === "failed" ||
          data.status === "terminated" ||
          data.status === "timed_out" ||
          data.status === "canceled"
        ) {
          pollIntervalRef.current = null;
          setTestStatus("failed");
          setTestFailureReason(data.failure_reason ?? "Unknown error");
          const failedHost = (data.tested_url ? getHostname(data.tested_url) : null)
            ?? (testUrl ? getHostname(testUrl) : null)
            ?? testUrl;
          toast({
            title: failedHost
              ? `Unable to establish login-free credentials for ${failedHost}`
              : "Unable to establish login-free credentials",
            description:
              data.failure_reason ?? "The login test did not succeed",
            variant: "destructive",
          });
          return;
        }
        // Still running — schedule next poll (no overlap possible)
        pollIntervalRef.current = setTimeout(() => {
          pollTestStatus(credentialId, workflowRunId);
        }, 3000);
      } catch {
        // Network error — retry after delay
        pollIntervalRef.current = setTimeout(() => {
          pollTestStatus(credentialId, workflowRunId);
        }, 3000);
      }
    },
    [credentialGetter, queryClient],
  );

  const startTest = useCallback(
    async () => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.post<TestLoginResponse>(
          `/credentials/test-login`,
          {
            url: testUrl,
            username: passwordCredentialValues.username.trim(),
            password: passwordCredentialValues.password.trim(),
            totp: passwordCredentialValues.totp.trim() || null,
            totp_type: passwordCredentialValues.totp_type,
            totp_identifier: passwordCredentialValues.totp_identifier.trim() || null,
          },
        );
        const data = response.data;
        setTestCredentialId(data.credential_id);
        setTestStatus("testing");

        // Start first poll after 3 seconds (subsequent polls scheduled by pollTestStatus)
        pollIntervalRef.current = setTimeout(() => {
          pollTestStatus(data.credential_id, data.workflow_run_id);
        }, 3000);
      } catch (error) {
        setTestStatus("failed");
        const detail = (
          (error as AxiosError)?.response?.data as { detail?: string }
        )?.detail;
        setTestFailureReason(detail ?? "Failed to start credential test");
        const failedHost = testUrl ? getHostname(testUrl) ?? testUrl : null;
        toast({
          title: failedHost
            ? `Unable to establish login-free credentials for ${failedHost}`
            : "Unable to establish login-free credentials",
          description: detail ?? "Failed to start credential test",
          variant: "destructive",
        });
      }
    },
    [credentialGetter, testUrl, passwordCredentialValues, pollTestStatus],
  );

  const createCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.post("/credentials", request);
      return response.data;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      onCredentialCreated?.(data.credential_id);
      reset();
      setIsOpen(false);
      toast({
        title: "Credential created",
        description: "Your credential has been created successfully",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  const renameCredentialMutation = useMutation({
    mutationFn: async ({ id, name }: { id: string; name: string }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.patch(`/credentials/${id}`, { name });
      return response.data;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      onCredentialCreated?.(data.credential_id);
      reset();
      setIsOpen(false);
      toast({
        title: "Credential saved",
        description: "Your credential has been saved successfully",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  const updateCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.post(
        `/credentials/${editingCredential?.credential_id}/update`,
        request,
      );
      return response.data;
    },
    onSuccess: () => {
      reset();
      setIsOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      toast({
        title: "Credential updated",
        description: "Your credential has been updated successfully",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  const activeMutation = isEditMode
    ? updateCredentialMutation
    : createCredentialMutation;

  const handleSave = () => {
    const name =
      type === CredentialModalTypes.PASSWORD
        ? passwordCredentialValues.name.trim()
        : type === CredentialModalTypes.CREDIT_CARD
          ? creditCardCredentialValues.name.trim()
          : secretCredentialValues.name.trim();
    if (name === "") {
      toast({
        title: "Error",
        description: "Name is required",
        variant: "destructive",
      });
      return;
    }

    if (type === CredentialModalTypes.PASSWORD) {
      const username = passwordCredentialValues.username.trim();
      const password = passwordCredentialValues.password.trim();
      const totp = passwordCredentialValues.totp.trim();
      const totpIdentifier = passwordCredentialValues.totp_identifier.trim();

      if (username === "" || password === "") {
        toast({
          title: "Error",
          description: "Username and password are required",
          variant: "destructive",
        });
        return;
      }

      // If test passed, rename the temp credential instead of creating a new one
      if (testAndSave && testStatus === "completed" && testCredentialId) {
        renameCredentialMutation.mutate({ id: testCredentialId, name });
        return;
      }

      activeMutation.mutate({
        name,
        credential_type: "password",
        credential: {
          username,
          password,
          totp: totp === "" ? null : totp,
          totp_type: passwordCredentialValues.totp_type,
          totp_identifier: totpIdentifier === "" ? null : totpIdentifier,
        },
      });
    } else if (type === CredentialModalTypes.CREDIT_CARD) {
      const cardNumber = creditCardCredentialValues.cardNumber.trim();
      const cardCode = creditCardCredentialValues.cardCode.trim();
      const cardExpirationDate =
        creditCardCredentialValues.cardExpirationDate.trim();
      const cardBrand = creditCardCredentialValues.cardBrand.trim();
      const cardHolderName = creditCardCredentialValues.cardHolderName.trim();

      if (
        cardNumber === "" ||
        cardCode === "" ||
        cardExpirationDate === "" ||
        cardBrand === "" ||
        cardHolderName === ""
      ) {
        toast({
          title: "Error",
          description: "All credit card fields are required",
          variant: "destructive",
        });
        return;
      }

      const cardExpirationDateParts = cardExpirationDate.split("/");
      if (cardExpirationDateParts.length !== 2) {
        toast({
          title: "Error",
          description: "Invalid card expiration date",
          variant: "destructive",
        });
        return;
      }
      const cardExpirationMonth = cardExpirationDateParts[0];
      const cardExpirationYear = cardExpirationDateParts[1];
      if (!cardExpirationMonth || !cardExpirationYear) {
        toast({
          title: "Error",
          description: "Invalid card expiration date",
          variant: "destructive",
        });
        return;
      }
      // remove all spaces from the card number
      const number = creditCardCredentialValues.cardNumber.replace(/\s/g, "");
      activeMutation.mutate({
        name,
        credential_type: "credit_card",
        credential: {
          card_number: number,
          card_cvv: cardCode,
          card_exp_month: cardExpirationMonth,
          card_exp_year: cardExpirationYear,
          card_brand: cardBrand,
          card_holder_name: cardHolderName,
        },
      });
    } else if (type === CredentialModalTypes.SECRET) {
      const secretValue = secretCredentialValues.secretValue.trim();
      const secretLabel = secretCredentialValues.secretLabel.trim();

      if (secretValue === "") {
        toast({
          title: "Error",
          description: "Secret value is required",
          variant: "destructive",
        });
        return;
      }

      activeMutation.mutate({
        name,
        credential_type: "secret",
        credential: {
          secret_value: secretValue,
          secret_label: secretLabel === "" ? null : secretLabel,
        },
      });
    }
  };

  const credentialContent = (() => {
    if (type === CredentialModalTypes.PASSWORD) {
      return (
        <PasswordCredentialContent
          values={passwordCredentialValues}
          onChange={setPasswordCredentialValues}
        />
      );
    }
    if (type === CredentialModalTypes.CREDIT_CARD) {
      return (
        <CreditCardCredentialContent
          values={creditCardCredentialValues}
          onChange={setCreditCardCredentialValues}
        />
      );
    }
    return (
      <SecretCredentialContent
        values={secretCredentialValues}
        onChange={setSecretCredentialValues}
      />
    );
  })();

  const isTestInProgress = testStatus === "testing";
  const isTestComplete =
    testStatus === "completed" ||
    testStatus === "failed" ||
    testStatus === "profile_failed";

  const validateTestUrl = (): boolean => {
    if (testUrl.trim() === "") {
      toast({
        title: "Error",
        description: "Login URL is required to test credentials",
        variant: "destructive",
      });
      return false;
    }
    if (
      !testUrl.trim().startsWith("http://") &&
      !testUrl.trim().startsWith("https://")
    ) {
      toast({
        title: "Error",
        description: "Login URL must start with http:// or https://",
        variant: "destructive",
      });
      return false;
    }
    return true;
  };

  const handleTest = () => {
    if (!validateTestUrl()) return;

    const username = passwordCredentialValues.username.trim();
    const password = passwordCredentialValues.password.trim();
    if (username === "" || password === "") {
      toast({
        title: "Error",
        description: "Username and password are required to test",
        variant: "destructive",
      });
      return;
    }

    // Reset any previous test status
    setTestStatus("idle");
    setTestFailureReason(null);
    setTestCredentialId(null);
    startTest();
  };

  // Whether the Test button should be shown
  const showTestButton =
    testAndSave &&
    type === CredentialModalTypes.PASSWORD;

  // Whether the Test button should be enabled
  const canTest =
    showTestButton &&
    testUrl.trim() !== "" &&
    passwordCredentialValues.username.trim() !== "" &&
    passwordCredentialValues.password.trim() !== "" &&
    !isTestInProgress;

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) {
          reset();
        }
        setIsOpen(open);
      }}
    >
      <DialogContent className="w-[700px] max-w-[700px]">
        <DialogHeader>
          <DialogTitle className="font-bold">
            {isEditMode ? "Edit Credential" : "Add Credential"}
          </DialogTitle>
        </DialogHeader>
        {isEditMode && (
          <Alert>
            <InfoCircledIcon className="size-4" />
            <AlertDescription>
              For security, saved passwords and secrets are never retrieved.
              Please re-enter all fields to update this credential.
            </AlertDescription>
          </Alert>
        )}
        {credentialContent}

        {/* Test & Save Browser Profile section — only for password credentials */}
        {type === CredentialModalTypes.PASSWORD && (
          <>
            <Separator />
            <div className="space-y-3">
              <div className="flex items-center gap-3">
                <Checkbox
                  id="test-and-save"
                  checked={testAndSave}
                  onCheckedChange={(checked) =>
                    setTestAndSave(checked === true)
                  }
                  disabled={isTestInProgress}
                />
                <Label
                  htmlFor="test-and-save"
                  className="cursor-pointer text-sm font-medium"
                >
                  Enable login-free workflows when using this credential
                </Label>
              </div>
              {testAndSave && (
                <div className="space-y-2 pl-7">
                  <p className="text-xs text-muted-foreground">
                    Skyvern will log in using your credentials, verify success,
                    and save the browser profile. You can then attach this
                    profile to workflow login blocks to skip future logins.
                  </p>
                  <div className="flex items-center gap-4">
                    <div className="w-32 shrink-0">
                      <Label className="text-xs">Login Page URL</Label>
                    </div>
                    <Input
                      value={testUrl}
                      onChange={(e) => setTestUrl(e.target.value)}
                      placeholder="https://example.com/login"
                      className="text-xs"
                      disabled={isTestInProgress}
                    />
                  </div>
                </div>
              )}

              {/* Test status display */}
              {isTestInProgress && (
                <div className="flex items-center gap-2 pl-7 text-sm text-muted-foreground">
                  <ReloadIcon className="size-4 animate-spin" />
                  <span>Testing credential login...</span>
                </div>
              )}
              {testStatus === "completed" && (
                <div className="flex items-center gap-2 pl-7 text-sm text-green-400">
                  <CheckCircledIcon className="size-4" />
                  <span>
                    {`Login test passed — login-free credentials available for workflows using ${getHostname(testUrl) ?? testUrl}`}
                  </span>
                </div>
              )}
              {testStatus === "profile_failed" && (
                <div className="space-y-1 pl-7">
                  <div className="flex items-center gap-2 text-sm text-destructive">
                    <CrossCircledIcon className="size-4" />
                    <span>Browser profile was not saved</span>
                  </div>
                  {testFailureReason && (
                    <p className="text-xs text-destructive/70">
                      {testFailureReason}
                    </p>
                  )}
                </div>
              )}
              {testStatus === "failed" && (
                <div className="space-y-1 pl-7">
                  <div className="flex items-center gap-2 text-sm text-destructive">
                    <CrossCircledIcon className="size-4" />
                    <span>
                      {testUrl
                        ? `Unable to establish login-free credentials for ${getHostname(testUrl) ?? testUrl}`
                        : "Unable to establish login-free credentials"}
                    </span>
                  </div>
                  {testFailureReason && (
                    <p className="text-xs text-destructive/70">
                      {testFailureReason}
                    </p>
                  )}
                </div>
              )}
            </div>
          </>
        )}

        <DialogFooter>
          <div className="flex w-full items-center justify-end gap-2">
            {showTestButton && (
              <Button
                variant="secondary"
                onClick={handleTest}
                disabled={!canTest}
              >
                {isTestInProgress ? (
                  <ReloadIcon className="mr-2 size-4 animate-spin" />
                ) : null}
                {isTestInProgress
                  ? "Testing..."
                  : isTestComplete
                    ? "Retest"
                    : "Test"}
              </Button>
            )}
            <Button
              onClick={handleSave}
              disabled={
                activeMutation.isPending || renameCredentialMutation.isPending || isTestInProgress
              }
            >
              {activeMutation.isPending || renameCredentialMutation.isPending ? (
                <ReloadIcon className="mr-2 size-4 animate-spin" />
              ) : null}
              {isEditMode ? "Update" : "Save"}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CredentialsModal };
