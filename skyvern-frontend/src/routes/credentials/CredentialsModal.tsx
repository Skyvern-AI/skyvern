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
import { PasswordCredentialContent } from "./PasswordCredentialContent";
import { SecretCredentialContent } from "./SecretCredentialContent";
import { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@/components/ui/button";
import { CreditCardCredentialContent } from "./CreditCardCredentialContent";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CreateCredentialRequest,
  TestCredentialResponse,
  TestCredentialStatusResponse,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { AxiosError } from "axios";
import {
  CheckCircledIcon,
  CrossCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
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
};

function CredentialsModal({
  onCredentialCreated,
  isOpen: controlledIsOpen,
  onOpenChange: controlledOnOpenChange,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const {
    isOpen: urlIsOpen,
    type,
    setIsOpen: setUrlIsOpen,
  } = useCredentialModalState();

  // Use controlled props if provided, otherwise fall back to URL-based state
  const isOpen = controlledIsOpen ?? urlIsOpen;
  const setIsOpen = controlledOnOpenChange ?? setUrlIsOpen;
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
  const pollIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearTimeout(pollIntervalRef.current);
      }
    };
  }, []);

  // Set default name when modal opens
  useEffect(() => {
    if (isOpen && credentials) {
      const existingNames = credentials.map((cred) => cred.name);
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
  }, [isOpen, credentials]);

  function reset() {
    setPasswordCredentialValues(PASSWORD_CREDENTIAL_INITIAL_VALUES);
    setCreditCardCredentialValues(CREDIT_CARD_CREDENTIAL_INITIAL_VALUES);
    setSecretCredentialValues(SECRET_CREDENTIAL_INITIAL_VALUES);
    setTestAndSave(false);
    setTestUrl("");
    setTestStatus("idle");
    setTestFailureReason(null);
    if (pollIntervalRef.current) {
      clearTimeout(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }

  const pollTestStatus = useCallback(
    async (credentialId: string, workflowRunId: string) => {
      try {
        const client = await getClient(credentialGetter);
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
          toast({
            title: "Credential test failed",
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
    async (credentialId: string) => {
      try {
        const client = await getClient(credentialGetter);
        const response = await client.post<TestCredentialResponse>(
          `/credentials/${credentialId}/test`,
          {
            url: testUrl,
            save_browser_profile: true,
          },
        );
        const data = response.data;
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
        toast({
          title: "Error",
          description: detail ?? "Failed to start credential test",
          variant: "destructive",
        });
      }
    },
    [credentialGetter, testUrl, pollTestStatus],
  );

  const createCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post("/credentials", request);
      return response.data;
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      onCredentialCreated?.(data.credential_id);

      if (
        testAndSave &&
        type === CredentialModalTypes.PASSWORD &&
        testUrl.trim() !== ""
      ) {
        // Don't close the modal — start the test
        toast({
          title: "Credential created",
          description: "Starting login test...",
          variant: "success",
        });
        startTest(data.credential_id);
      } else {
        reset();
        setIsOpen(false);
        toast({
          title: "Credential created",
          description: "Your credential has been created successfully",
          variant: "success",
        });
      }
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

      if (testAndSave && testUrl.trim() === "") {
        toast({
          title: "Error",
          description: "Login URL is required when testing credentials",
          variant: "destructive",
        });
        return;
      }

      if (
        testAndSave &&
        !testUrl.trim().startsWith("http://") &&
        !testUrl.trim().startsWith("https://")
      ) {
        toast({
          title: "Error",
          description: "Login URL must start with http:// or https://",
          variant: "destructive",
        });
        return;
      }

      createCredentialMutation.mutate({
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
      createCredentialMutation.mutate({
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

      createCredentialMutation.mutate({
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
          <DialogTitle className="font-bold">Add Credential</DialogTitle>
        </DialogHeader>
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
                  <span>Login test passed — browser profile saved!</span>
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
                    <span>Login test failed</span>
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
          {isTestComplete ? (
            <Button
              onClick={() => {
                reset();
                setIsOpen(false);
              }}
            >
              Done
            </Button>
          ) : (
            <Button
              onClick={handleSave}
              disabled={
                createCredentialMutation.isPending || isTestInProgress
              }
            >
              {createCredentialMutation.isPending || isTestInProgress ? (
                <ReloadIcon className="mr-2 size-4 animate-spin" />
              ) : null}
              {testAndSave && type === CredentialModalTypes.PASSWORD
                ? "Save & Test"
                : "Save"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CredentialsModal };
