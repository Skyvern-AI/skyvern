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
import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
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

// Maximum polling duration: 5 minutes
const MAX_POLL_DURATION_MS = 5 * 60 * 1000;

// Progressive status messages during test — each advances once at a real interval
const TEST_STATUS_MESSAGES = [
  "Testing credential login...",
  "Entering credentials...",
  "Verifying login...",
  "This may take a moment...",
  "Still working...",
];
// Delays (ms) before advancing to the next message (last message stays forever)
const TEST_MESSAGE_DELAYS = [15_000, 30_000, 75_000, 60_000];

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
  /** Called after a credential is saved with "Save browser session" checked to trigger an async test */
  onStartBackgroundTest?: (credentialId: string, url: string) => void;
};

function CredentialsModal({
  onCredentialCreated,
  isOpen: controlledIsOpen,
  onOpenChange: controlledOnOpenChange,
  editingCredential,
  overrideType,
  onStartBackgroundTest,
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
  // The temporary credential ID and workflow run ID created by the test-login endpoint
  const [testCredentialId, setTestCredentialId] = useState<string | null>(null);
  // testWorkflowRunId is stored only as a ref (not state) because it's never
  // rendered — it's only needed by cancelTest/close to call the cancel API.
  // Refs mirror state so cancelTest always has the latest IDs regardless of
  // React's async render cycle (e.g. cancel during the startTest HTTP call).
  const testCredentialIdRef = useRef<string | null>(null);
  const testWorkflowRunIdRef = useRef<string | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollStartTimeRef = useRef<number | null>(null);
  const pollErrorCountRef = useRef(0);
  // Guards against in-flight poll responses updating state after cancel/close
  const pollCancelledRef = useRef(false);

  // Captures save intent before mutation fires — testAndSave/testUrl may be
  // reset by the time onSuccess runs, so we snapshot them here.
  const saveIntentRef = useRef<{
    shouldTestAfterSave: boolean;
    testUrl: string;
  }>({ shouldTestAfterSave: false, testUrl: "" });

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearTimeout(pollIntervalRef.current);
      }
    };
  }, []);

  // Invalidate a completed test when credential fields change — the saved
  // temp credential no longer matches the form, so the user must re-test.
  // Also clean up the orphaned temp credential on the backend.
  useEffect(() => {
    if (testStatus === "completed" || testStatus === "profile_failed") {
      const staleCredId = testCredentialIdRef.current;
      if (staleCredId) {
        getClient(credentialGetter)
          .then((client) => client.delete(`/credentials/${staleCredId}`))
          .catch(() => {
            // Best-effort cleanup
          });
      }
      setTestStatus("idle");
      setTestCredentialId(null);
      testCredentialIdRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to login-affecting field changes, not testStatus or name
  }, [
    passwordCredentialValues.username,
    passwordCredentialValues.password,
    passwordCredentialValues.totp,
    passwordCredentialValues.totp_type,
    passwordCredentialValues.totp_identifier,
    testUrl,
  ]);

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
    testCredentialIdRef.current = null;
    testWorkflowRunIdRef.current = null;
    pollStartTimeRef.current = null;
    pollErrorCountRef.current = 0;
    pollCancelledRef.current = false;
    saveIntentRef.current = { shouldTestAfterSave: false, testUrl: "" };
    if (pollIntervalRef.current) {
      clearTimeout(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }

  const pollTestStatus = useCallback(
    async (credentialId: string, workflowRunId: string) => {
      // Bail out if test was canceled/closed while this request was in-flight
      if (pollCancelledRef.current) return;

      // Check if we've exceeded the maximum polling duration
      if (
        pollStartTimeRef.current &&
        Date.now() - pollStartTimeRef.current > MAX_POLL_DURATION_MS
      ) {
        pollIntervalRef.current = null;
        setTestStatus("failed");
        setTestFailureReason(
          "The test timed out after 5 minutes. The login may be taking too long or requires manual interaction.",
        );
        toast({
          title: "Credential test timed out",
          description:
            "The test did not complete within 5 minutes. Please try again.",
          variant: "destructive",
        });
        // Cancel the backend workflow run so it stops consuming resources
        getClient(credentialGetter, "sans-api-v1")
          .then((client) =>
            client.post(
              `/credentials/${credentialId}/test/${workflowRunId}/cancel`,
            ),
          )
          .catch(() => {
            // Best-effort — backend timeout will eventually clean up
          });
        return;
      }

      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<TestCredentialStatusResponse>(
          `/credentials/${credentialId}/test/${workflowRunId}`,
        );
        const data = response.data;
        // Check again after await — cancel may have happened while request was in-flight
        if (pollCancelledRef.current) return;
        pollErrorCountRef.current = 0; // Reset on successful poll

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
                ? `Login successful! Saved browser session enabled for ${profileHost}`
                : "Login successful! Saved browser session enabled."
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
          const failedHost =
            (data.tested_url ? getHostname(data.tested_url) : null) ??
            (testUrl ? getHostname(testUrl) : null) ??
            testUrl;
          toast({
            title: failedHost
              ? `Unable to save browser session for ${failedHost}`
              : "Unable to save browser session",
            description:
              data.failure_reason ?? "The login test did not succeed",
            variant: "destructive",
          });
          return;
        }
        // Still running — schedule next poll
        pollIntervalRef.current = setTimeout(() => {
          pollTestStatus(credentialId, workflowRunId);
        }, 3000);
      } catch {
        pollErrorCountRef.current++;
        if (pollErrorCountRef.current >= 10) {
          pollIntervalRef.current = null;
          setTestStatus("failed");
          setTestFailureReason(
            "Network error — please check your connection and try again.",
          );
          toast({
            title: "Connection lost",
            description:
              "Unable to reach the server after multiple attempts. Please check your connection.",
            variant: "destructive",
          });
          return;
        }
        // Network error — retry after delay
        pollIntervalRef.current = setTimeout(() => {
          pollTestStatus(credentialId, workflowRunId);
        }, 3000);
      }
    },
    [credentialGetter, queryClient, testUrl],
  );

  const startTest = useCallback(async () => {
    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.post<TestLoginResponse>(
        `/credentials/test-login`,
        {
          url: testUrl.trim(),
          username: passwordCredentialValues.username.trim(),
          password: passwordCredentialValues.password.trim(),
          totp: passwordCredentialValues.totp.trim() || null,
          totp_type: passwordCredentialValues.totp_type,
          totp_identifier:
            passwordCredentialValues.totp_identifier.trim() || null,
        },
      );
      const data = response.data;
      testCredentialIdRef.current = data.credential_id;
      testWorkflowRunIdRef.current = data.workflow_run_id;

      // If the user canceled while the POST was in-flight, clean up immediately
      if (pollCancelledRef.current) {
        getClient(credentialGetter, "sans-api-v1")
          .then((c) =>
            c.post(
              `/credentials/${data.credential_id}/test/${data.workflow_run_id}/cancel`,
            ),
          )
          .catch(() => {});
        testCredentialIdRef.current = null;
        testWorkflowRunIdRef.current = null;
        return;
      }

      setTestCredentialId(data.credential_id);
      setTestStatus("testing");
      pollStartTimeRef.current = Date.now();

      // Start first poll after 3 seconds
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
        title: "Failed to start credential test",
        description: detail ?? "An unexpected error occurred",
        variant: "destructive",
      });
    }
  }, [credentialGetter, testUrl, passwordCredentialValues, pollTestStatus]);

  const createCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post("/credentials", request);
      return response.data;
    },
    onSuccess: async (data) => {
      const { shouldTestAfterSave, testUrl: capturedTestUrl } =
        saveIntentRef.current;

      // If the user entered a URL, save it on the credential as metadata
      if (capturedTestUrl) {
        try {
          const client = await getClient(credentialGetter, "sans-api-v1");
          await client.patch(`/credentials/${data.credential_id}`, {
            name: data.name,
            tested_url: capturedTestUrl,
          });
        } catch {
          // Best-effort — credential was created, URL is just metadata
        }
      }
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      onCredentialCreated?.(data.credential_id);
      reset();
      setIsOpen(false);

      if (shouldTestAfterSave && onStartBackgroundTest) {
        onStartBackgroundTest(data.credential_id, capturedTestUrl);
        toast({
          title: "Credential saved",
          description:
            "Testing browser profile in the background. You'll be notified when it's ready.",
          variant: "success",
        });
      } else if (shouldTestAfterSave) {
        // Background test hook not available in this context (e.g. workflow editor)
        toast({
          title: "Credential saved",
          description:
            "To set up a browser profile, test this credential from the Credentials page.",
          variant: "success",
        });
      } else {
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

  const renameCredentialMutation = useMutation({
    mutationFn: async ({
      id,
      name,
      tested_url,
    }: {
      id: string;
      name: string;
      tested_url?: string;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const body: Record<string, string> = { name };
      if (tested_url) {
        body.tested_url = tested_url;
      }
      const response = await client.patch<CredentialApiResponse>(
        `/credentials/${id}`,
        body,
      );
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
        const url = testUrl.trim();
        renameCredentialMutation.mutate({
          id: testCredentialId,
          name,
          tested_url: url || undefined,
        });
        return;
      }

      // Capture intent before mutation — state will be reset in onSuccess
      saveIntentRef.current = {
        shouldTestAfterSave:
          testAndSave && testStatus !== "completed" && testUrl.trim() !== "",
        testUrl: testUrl.trim(),
      };

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

  const isTestInProgress = testStatus === "testing";
  const isTestComplete =
    testStatus === "completed" ||
    testStatus === "failed" ||
    testStatus === "profile_failed";

  const [testMessageIndex, setTestMessageIndex] = useState(0);
  useEffect(() => {
    if (!isTestInProgress) {
      setTestMessageIndex(0);
      return;
    }
    // If we're at the last message, stay there
    if (testMessageIndex >= TEST_STATUS_MESSAGES.length - 1) {
      return;
    }
    const timeout = setTimeout(() => {
      setTestMessageIndex((i) => i + 1);
    }, TEST_MESSAGE_DELAYS[testMessageIndex] ?? 60_000);
    return () => clearTimeout(timeout);
  }, [isTestInProgress, testMessageIndex]);

  const credentialContent = (() => {
    if (type === CredentialModalTypes.PASSWORD) {
      return (
        <PasswordCredentialContent
          values={passwordCredentialValues}
          onChange={setPasswordCredentialValues}
          url={testUrl}
          onUrlChange={!isEditMode ? setTestUrl : undefined}
          urlRequired={testAndSave}
          urlDisabled={isTestInProgress}
          afterUrl={
            !isEditMode ? (
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
                    Save browser session for future logins
                  </Label>
                  <HelpTooltip content="Skyvern will log in using your credentials, verify success, and save the browser session. Future workflow runs will skip the login form entirely because the saved session is already authenticated." />
                </div>

                {isTestInProgress && (
                  <div className="flex items-center gap-2 pl-7 text-sm text-muted-foreground">
                    <ReloadIcon className="size-4 animate-spin" />
                    <span>{TEST_STATUS_MESSAGES[testMessageIndex]}</span>
                  </div>
                )}
                {testStatus === "completed" && (
                  <div className="flex items-center gap-2 pl-7 text-sm text-green-400">
                    <CheckCircledIcon className="size-4" />
                    <span>
                      {`Login test passed — saved browser session available for workflows using ${getHostname(testUrl) ?? testUrl}`}
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
                          ? `Unable to save browser session for ${getHostname(testUrl) ?? testUrl}`
                          : "Unable to save browser session"}
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
            ) : undefined
          }
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

  const handleTest = () => {
    if (testUrl.trim() === "") {
      toast({
        title: "Error",
        description: "Login URL is required to test credentials",
        variant: "destructive",
      });
      return;
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
      return;
    }

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

    // Set testing state immediately to avoid button flash
    pollCancelledRef.current = false;
    setTestStatus("testing");
    setTestFailureReason(null);
    setTestCredentialId(null);
    startTest();
  };

  const cancelTest = useCallback(async () => {
    // Stop polling and prevent in-flight responses from updating state
    pollCancelledRef.current = true;
    if (pollIntervalRef.current) {
      clearTimeout(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    setTestStatus("idle");
    setTestFailureReason(null);
    pollStartTimeRef.current = null;

    // Use refs for IDs — state may be stale if cancel fires during startTest HTTP call
    const credId = testCredentialIdRef.current;
    const wrId = testWorkflowRunIdRef.current;
    if (credId && wrId) {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        await client.post(`/credentials/${credId}/test/${wrId}/cancel`);
      } catch {
        // Best-effort — backend background task will clean up regardless
      }
    }

    testCredentialIdRef.current = null;
    testWorkflowRunIdRef.current = null;
    setTestCredentialId(null);
    toast({
      title: "Test canceled",
      description: "The credential test has been canceled.",
    });
  }, [credentialGetter]);

  // Whether the Test button should be shown
  const showTestButton = testAndSave && type === CredentialModalTypes.PASSWORD;

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
          // Prevent in-flight poll responses from updating state after close
          pollCancelledRef.current = true;
          // Cancel any in-progress test before closing — use refs for latest IDs
          const credId = testCredentialIdRef.current;
          const wrId = testWorkflowRunIdRef.current;
          if (isTestInProgress && credId && wrId) {
            getClient(credentialGetter, "sans-api-v1")
              .then((client) =>
                client.post(`/credentials/${credId}/test/${wrId}/cancel`),
              )
              .catch(() => {
                // Best-effort cleanup
              });
          } else if (credId && !isTestInProgress) {
            // Test completed but user closed without saving — delete orphaned temp credential
            getClient(credentialGetter)
              .then((client) => client.delete(`/credentials/${credId}`))
              .catch(() => {});
          }
          testCredentialIdRef.current = null;
          testWorkflowRunIdRef.current = null;
          if (pollIntervalRef.current) {
            clearTimeout(pollIntervalRef.current);
            pollIntervalRef.current = null;
          }
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

        <DialogFooter>
          <div className="flex w-full items-center justify-end gap-2">
            {showTestButton &&
              (isTestInProgress ? (
                <Button variant="destructive" onClick={cancelTest}>
                  Cancel Test
                </Button>
              ) : (
                <Button
                  variant="secondary"
                  onClick={handleTest}
                  disabled={!canTest}
                >
                  {isTestComplete ? "Retest" : "Test"}
                </Button>
              ))}
            <Button
              onClick={handleSave}
              disabled={
                activeMutation.isPending ||
                renameCredentialMutation.isPending ||
                isTestInProgress
              }
            >
              {activeMutation.isPending ||
              renameCredentialMutation.isPending ? (
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
