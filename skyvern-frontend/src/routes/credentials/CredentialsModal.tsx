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
  type CredentialModalType,
} from "./useCredentialModalState";
import { PasswordCredentialContent } from "./PasswordCredentialContent";
import { SecretCredentialContent } from "./SecretCredentialContent";
import { useState, useEffect, useCallback, useRef } from "react";
import { Button } from "@/components/ui/button";
import {
  CreditCardCredentialContent,
  type CreditCardCredentialValues,
} from "./CreditCardCredentialContent";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  type CreateCredentialRequest,
  type CreditCardBillingAddress,
  type CreditCardCredential,
  type CredentialApiResponse,
  PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
  isPasswordCredential,
  isCreditCardCredential,
  isSecretCredential,
  type TestCredentialStatusResponse,
  type TestLoginResponse,
  type ProxyLocation,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { AxiosError } from "axios";
import {
  CheckCircledIcon,
  CrossCircledIcon,
  ExclamationTriangleIcon,
  ExternalLinkIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { Checkbox } from "@/components/ui/checkbox";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { getHostname } from "@/util/getHostname";
import { useCustomCredentialServiceConfig } from "@/hooks/useCustomCredentialServiceConfig";
import { getAuthenticatorKeyError } from "./credentialTotpValidation";
import {
  getAuthenticatorSaveError,
  getCredentialErrorMessage,
  type AuthenticatorSaveError,
} from "./authenticatorSaveError";

const PASSWORD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  username: "",
  password: "",
  totp: "",
  totp_type: "none" as "none" | "authenticator" | "email" | "text",
  totp_identifier: "",
};

function createCreditCardCredentialInitialValues(): CreditCardCredentialValues {
  return {
    name: "",
    cardNumber: "",
    cardExpirationDate: "",
    cardCode: "",
    cardBrand: "",
    cardHolderName: "",
    billingAddressLine1: "",
    billingAddressLine2: "",
    billingCity: "",
    billingState: "",
    billingStateCode: "",
    billingPostalCode: "",
    billingCountry: "",
    billingCountryCode: "",
    billingEmail: "",
    billingPhone: "",
    metadata: [{ key: "", value: "" }],
  };
}

const SECRET_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  secretLabel: "",
  secretValue: "",
};

// Maximum polling duration: 10 minutes (matches the backend profile-creation budget)
const MAX_POLL_DURATION_MS = 10 * 60 * 1000;

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

function trimmedOrNull(value: string) {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function buildBillingAddress(
  values: CreditCardCredentialValues,
): CreditCardBillingAddress | null {
  const billingAddress: CreditCardBillingAddress = {};

  const line1 = trimmedOrNull(values.billingAddressLine1);
  const line2 = trimmedOrNull(values.billingAddressLine2);
  const city = trimmedOrNull(values.billingCity);
  const state = trimmedOrNull(values.billingState);
  const stateCode = trimmedOrNull(values.billingStateCode);
  const postalCode = trimmedOrNull(values.billingPostalCode);
  const country = trimmedOrNull(values.billingCountry);
  const countryCode = trimmedOrNull(values.billingCountryCode);

  if (line1) billingAddress.line1 = line1;
  if (line2) billingAddress.line2 = line2;
  if (city) billingAddress.city = city;
  if (state) billingAddress.state = state;
  if (stateCode) billingAddress.state_code = stateCode;
  if (postalCode) billingAddress.postal_code = postalCode;
  if (country) billingAddress.country = country;
  if (countryCode) billingAddress.country_code = countryCode;

  return Object.keys(billingAddress).length > 0 ? billingAddress : null;
}

function buildMetadata(values: CreditCardCredentialValues) {
  const metadata: Record<string, string> = {};
  let hasIncompleteEntry = false;

  for (const entry of values.metadata) {
    const key = entry.key.trim();
    const value = entry.value.trim();
    if (!key && !value) {
      continue;
    }
    if (!key || !value) {
      hasIncompleteEntry = true;
      continue;
    }
    metadata[key] = value;
  }

  return {
    metadata: Object.keys(metadata).length > 0 ? metadata : null,
    hasIncompleteEntry,
  };
}

type Props = {
  onCredentialCreated?: (id: string, name?: string) => void;
  /** Optional controlled mode: pass isOpen and onOpenChange to control modal state locally */
  isOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** When provided, the modal opens in edit mode and pre-fills available fields */
  editingCredential?: CredentialApiResponse;
  /** Override the modal type (used in edit mode to set the correct form) */
  overrideType?: CredentialModalType;
  // Copilot-only: seed the login URL so a quick-added credential carries a
  // tested_url and matches later hostname-keyed asks. Create mode only; leaving
  // it undefined (every non-copilot caller) keeps the field empty as before.
  defaultTestUrl?: string;
  /** Called after a credential is saved with "Save browser session" checked to trigger an async test */
  onStartBackgroundTest?: (
    credentialId: string,
    url: string,
    userContext?: string,
  ) => void;
};

type ProxyPinPayload = {
  proxy_location: ProxyLocation | null;
  proxy_session_id?: string | null;
  rotate_proxy_session_id?: boolean;
};

function formatProxyIdentity(value?: string | null) {
  if (!value) {
    return null;
  }
  return `${value.slice(0, 3)}...${value.slice(-2)}`;
}

function CredentialsModal({
  onCredentialCreated,
  isOpen: controlledIsOpen,
  onOpenChange: controlledOnOpenChange,
  editingCredential,
  overrideType,
  defaultTestUrl,
  onStartBackgroundTest,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const {
    isOpen: urlIsOpen,
    type: urlType,
    setIsOpen: setUrlIsOpen,
  } = useCredentialModalState();
  const { parsedConfig: customCredentialServiceConfig } =
    useCustomCredentialServiceConfig();
  const hasCustomCredentialService = !!customCredentialServiceConfig;
  const [vaultType, setVaultType] = useState<"default" | "custom">("default");

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
    createCreditCardCredentialInitialValues,
  );
  const [secretCredentialValues, setSecretCredentialValues] = useState(
    SECRET_CREDENTIAL_INITIAL_VALUES,
  );
  const [editingGroups, setEditingGroups] = useState({
    name: false,
    values: false,
  });
  // Inline authenticator setup error returned by the backend when it rejects a
  // decoded QR value or pasted key. Cleared whenever the user edits the key.
  const [authenticatorSaveError, setAuthenticatorSaveError] =
    useState<AuthenticatorSaveError | null>(null);

  const handlePasswordCredentialChange = useCallback(
    (next: typeof PASSWORD_CREDENTIAL_INITIAL_VALUES) => {
      setPasswordCredentialValues((prev) => {
        if (next.totp !== prev.totp || next.totp_type !== prev.totp_type) {
          setAuthenticatorSaveError(null);
        }
        return next;
      });
    },
    [],
  );

  const reportCredentialSaveError = useCallback(
    (error: unknown, title = "Error"): string => {
      const authError = getAuthenticatorSaveError(error);
      if (authError && passwordCredentialValues.totp_type === "authenticator") {
        setAuthenticatorSaveError(authError);
      }
      const description =
        authError?.message ??
        getCredentialErrorMessage(error) ??
        (error instanceof Error
          ? error.message
          : "An unexpected error occurred");
      const isEnterpriseUpgrade =
        authError?.code === "enterprise_required" &&
        passwordCredentialValues.totp_type === "authenticator";
      if (!isEnterpriseUpgrade) {
        toast({ title, description, variant: "destructive" });
      }
      return description;
    },
    [passwordCredentialValues.totp_type],
  );

  const handleEnableEditName = useCallback(() => {
    setEditingGroups((prev) => ({ ...prev, name: true }));
  }, []);

  const handleEnableEditValues = useCallback(() => {
    setEditingGroups((prev) => ({ ...prev, values: true }));
  }, []);

  // Test & Save Browser Profile state
  const [testAndSave, setTestAndSave] = useState(false);
  const [testUrl, setTestUrl] = useState("");
  const [userContext, setUserContext] = useState("");
  const [pinResidentialIspProxy, setPinResidentialIspProxy] = useState(false);
  const [rotateProxyPin, setRotateProxyPin] = useState(false);
  const existingProxyIdentity = formatProxyIdentity(
    editingCredential?.proxy_session_id,
  );
  const [testStatus, setTestStatus] = useState<
    "idle" | "testing" | "completed" | "failed" | "profile_failed"
  >("idle");
  const [testFailureReason, setTestFailureReason] = useState<string | null>(
    null,
  );
  // The temporary credential ID and workflow run ID created by the test-login endpoint
  const [testCredentialId, setTestCredentialId] = useState<string | null>(null);
  // Workflow run ID used to render the "watch live" link — must be state so the
  // link appears immediately after the POST returns (refs don't trigger re-renders).
  const [testWorkflowRunId, setTestWorkflowRunId] = useState<string | null>(
    null,
  );
  // Refs mirror state so cancelTest/close always have the latest IDs regardless
  // of React's async render cycle (e.g. cancel during the startTest HTTP call).
  const testCredentialIdRef = useRef<string | null>(null);
  const testWorkflowRunIdRef = useRef<string | null>(null);
  const pollIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollStartTimeRef = useRef<number | null>(null);
  const pollErrorCountRef = useRef(0);
  // Guards against in-flight poll responses updating state after cancel/close
  const pollCancelledRef = useRef(false);

  const getProxyPinPayload = useCallback((): ProxyPinPayload => {
    if (!pinResidentialIspProxy) {
      return {
        proxy_location: null,
        proxy_session_id: null,
      };
    }
    return {
      proxy_location: PINNED_RESIDENTIAL_ISP_PROXY_LOCATION,
      ...(rotateProxyPin ? { rotate_proxy_session_id: true } : {}),
    };
  }, [pinResidentialIspProxy, rotateProxyPin]);

  const hasProxyPinChanges = useCallback(() => {
    if (!editingCredential) {
      return pinResidentialIspProxy;
    }
    const existingPinEnabled = Boolean(editingCredential.proxy_session_id);
    return (
      pinResidentialIspProxy !== existingPinEnabled ||
      (pinResidentialIspProxy && rotateProxyPin)
    );
  }, [editingCredential, pinResidentialIspProxy, rotateProxyPin]);

  // Captures save intent before mutation fires — testAndSave/testUrl may be
  // reset by the time onSuccess runs, so we snapshot them here.
  const saveIntentRef = useRef<{
    shouldTestAfterSave: boolean;
    saveBrowserSessionIntent: boolean;
    testUrl: string;
    userContext: string;
    name: string;
    proxyLocation: ProxyLocation | null;
    proxySessionId?: string | null;
    proxyPinChanged: boolean;
  }>({
    shouldTestAfterSave: false,
    saveBrowserSessionIntent: false,
    testUrl: "",
    userContext: "",
    name: "",
    proxyLocation: null,
    proxySessionId: null,
    proxyPinChanged: false,
  });

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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to login-affecting field changes, not testStatus, name, or userContext (context is cosmetic, not credential identity)
  }, [
    passwordCredentialValues.username,
    passwordCredentialValues.password,
    passwordCredentialValues.totp,
    passwordCredentialValues.totp_type,
    passwordCredentialValues.totp_identifier,
    testUrl,
    pinResidentialIspProxy,
    rotateProxyPin,
  ]);

  const formInitializedRef = useRef(false);

  // Initialize the form once per modal open — guarded by a ref so a background
  // credentials refetch (e.g. the test-completion poll) can't re-run this and
  // wipe typed values or in-progress test state.
  useEffect(() => {
    if (!isOpen) {
      formInitializedRef.current = false;
      return;
    }

    if (formInitializedRef.current) {
      return;
    }

    if (isEditMode) {
      formInitializedRef.current = true;
      reset();
      const cred = editingCredential.credential;
      if (editingCredential.tested_url) {
        setTestUrl(editingCredential.tested_url);
      }
      if (
        editingCredential.save_browser_session_intent ||
        !!editingCredential.browser_profile_id
      ) {
        setTestAndSave(true);
      }
      if (editingCredential.user_context) {
        setUserContext(editingCredential.user_context);
      }
      setPinResidentialIspProxy(Boolean(editingCredential.proxy_session_id));
      setRotateProxyPin(false);
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
          ...createCreditCardCredentialInitialValues(),
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

    if (credentials) {
      formInitializedRef.current = true;
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
      if (defaultTestUrl) {
        setTestUrl(defaultTestUrl);
      }
    }
  }, [isOpen, credentials, isEditMode, editingCredential, defaultTestUrl]);

  function reset() {
    setVaultType("default");
    setPasswordCredentialValues(PASSWORD_CREDENTIAL_INITIAL_VALUES);
    setCreditCardCredentialValues(createCreditCardCredentialInitialValues());
    setSecretCredentialValues(SECRET_CREDENTIAL_INITIAL_VALUES);
    setEditingGroups({ name: false, values: false });
    setAuthenticatorSaveError(null);
    setTestAndSave(false);
    setTestUrl("");
    setTestStatus("idle");
    setTestFailureReason(null);
    setTestCredentialId(null);
    setTestWorkflowRunId(null);
    testCredentialIdRef.current = null;
    testWorkflowRunIdRef.current = null;
    pollStartTimeRef.current = null;
    pollErrorCountRef.current = 0;
    pollCancelledRef.current = false;
    setUserContext("");
    setPinResidentialIspProxy(false);
    setRotateProxyPin(false);
    saveIntentRef.current = {
      shouldTestAfterSave: false,
      saveBrowserSessionIntent: false,
      testUrl: "",
      userContext: "",
      name: "",
      proxyLocation: null,
      proxySessionId: null,
      proxyPinChanged: false,
    };
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
          "The test timed out after 10 minutes. The login may be taking too long or requires manual interaction.",
        );
        toast({
          title: "Credential test timed out",
          description:
            "The test did not complete within 10 minutes. Please try again.",
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
    setAuthenticatorSaveError(null);
    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const proxyPinPayload = getProxyPinPayload();
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
          user_context: userContext.trim() || null,
          ...proxyPinPayload,
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
      setTestWorkflowRunId(data.workflow_run_id);
      setTestStatus("testing");
      pollStartTimeRef.current = Date.now();

      // Start first poll after 3 seconds
      pollIntervalRef.current = setTimeout(() => {
        pollTestStatus(data.credential_id, data.workflow_run_id);
      }, 3000);
    } catch (error) {
      setTestStatus("failed");
      const description = reportCredentialSaveError(
        error,
        "Failed to start credential test",
      );
      setTestFailureReason(description);
    }
  }, [
    credentialGetter,
    testUrl,
    passwordCredentialValues,
    userContext,
    pollTestStatus,
    getProxyPinPayload,
    reportCredentialSaveError,
  ]);

  const createCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post("/credentials", request);
      return response.data;
    },
    onSuccess: async (data) => {
      const {
        shouldTestAfterSave,
        saveBrowserSessionIntent,
        testUrl: capturedTestUrl,
        userContext: capturedUserContext,
      } = saveIntentRef.current;

      // Save metadata (tested_url, user_context, save_browser_session_intent) on the credential via PATCH
      if (capturedTestUrl || capturedUserContext || saveBrowserSessionIntent) {
        try {
          const client = await getClient(credentialGetter, "sans-api-v1");
          await client.patch(`/credentials/${data.credential_id}`, {
            name: data.name,
            ...(capturedTestUrl && { tested_url: capturedTestUrl }),
            user_context: capturedUserContext?.trim() || null,
            save_browser_session_intent: saveBrowserSessionIntent,
          });
        } catch {
          // Best-effort — credential was created, URL is just metadata
        }
      }
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      onCredentialCreated?.(data.credential_id, data.name);
      reset();
      setIsOpen(false);

      if (shouldTestAfterSave && onStartBackgroundTest) {
        onStartBackgroundTest(
          data.credential_id,
          capturedTestUrl,
          capturedUserContext || undefined,
        );
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
      reportCredentialSaveError(error);
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
    onSuccess: async () => {
      const {
        shouldTestAfterSave,
        saveBrowserSessionIntent,
        testUrl: capturedTestUrl,
        userContext: capturedUserContext,
        name: capturedName,
      } = saveIntentRef.current;

      // Persist metadata (tested_url, user_context, save_browser_session_intent) via PATCH
      if (editingCredential?.credential_id) {
        try {
          const client = await getClient(credentialGetter, "sans-api-v1");
          await client.patch(
            `/credentials/${editingCredential.credential_id}`,
            {
              name: capturedName || editingCredential.name,
              ...(capturedTestUrl && { tested_url: capturedTestUrl }),
              user_context: capturedUserContext?.trim() || null,
              save_browser_session_intent: saveBrowserSessionIntent,
            },
          );
        } catch {
          toast({
            title: "Partial save",
            description:
              "Credential updated, but login instructions could not be saved. Please try editing again.",
            variant: "destructive",
          });
        }
      }

      reset();
      setIsOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });

      if (
        shouldTestAfterSave &&
        capturedTestUrl &&
        editingCredential?.credential_id &&
        onStartBackgroundTest
      ) {
        onStartBackgroundTest(
          editingCredential.credential_id,
          capturedTestUrl,
          capturedUserContext || undefined,
        );
        toast({
          title: "Credential updated",
          description:
            "Testing login and saving browser session in the background…",
          variant: "success",
        });
      } else {
        toast({
          title: "Credential updated",
          description: "Your credential has been updated successfully",
          variant: "success",
        });
      }
    },
    onError: (error: AxiosError) => {
      reportCredentialSaveError(error);
    },
  });

  const renameCredentialMutation = useMutation({
    mutationFn: async ({
      id,
      name,
      tested_url,
      user_context,
      save_browser_session_intent,
      proxy_location,
      proxy_session_id,
      rotate_proxy_session_id,
    }: {
      id: string;
      name: string;
      tested_url?: string;
      user_context?: string | null;
      save_browser_session_intent?: boolean;
      proxy_location?: ProxyLocation | null;
      proxy_session_id?: string | null;
      rotate_proxy_session_id?: boolean;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const body: Record<string, string | boolean | ProxyLocation | null> = {
        name,
      };
      if (tested_url) {
        body.tested_url = tested_url;
      }
      if (user_context !== undefined) {
        body.user_context = user_context;
      }
      if (save_browser_session_intent !== undefined) {
        body.save_browser_session_intent = save_browser_session_intent;
      }
      if (proxy_location !== undefined) {
        body.proxy_location = proxy_location;
      }
      if (proxy_session_id !== undefined) {
        body.proxy_session_id = proxy_session_id;
      }
      if (rotate_proxy_session_id !== undefined) {
        body.rotate_proxy_session_id = rotate_proxy_session_id;
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
      onCredentialCreated?.(data.credential_id, data.name);
      reset();
      setIsOpen(false);
      toast({
        title: "Credential saved",
        description: "Your credential has been saved successfully",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      reportCredentialSaveError(error);
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

    const proxyPinPayload = getProxyPinPayload();
    const proxyPinChanged = hasProxyPinChanges();
    const credentialSaveProxyPinPayload =
      !isEditMode || proxyPinChanged ? proxyPinPayload : {};

    // In edit mode, use editingGroups to determine what changed (type-agnostic)
    if (isEditMode && editingCredential) {
      if (!editingGroups.name && !editingGroups.values && !proxyPinChanged) {
        // Nothing was edited — close silently
        reset();
        setIsOpen(false);
        return;
      }
      if (!editingGroups.values) {
        renameCredentialMutation.mutate({
          id: editingCredential.credential_id,
          name,
          ...(proxyPinChanged ? proxyPinPayload : {}),
        });
        return;
      }
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
      if (authenticatorKeyError) {
        return;
      }
      setAuthenticatorSaveError(null);

      const hasCompletedInlineTest =
        testAndSave && testStatus === "completed" && !!testCredentialId;

      // Create mode: the temp credential created by the inline test already holds
      // the entered secret and the saved browser profile, so rename it in place
      // instead of creating a duplicate.
      if (!isEditMode && hasCompletedInlineTest && testCredentialId) {
        const url = testUrl.trim();
        const ctx = userContext.trim();
        renameCredentialMutation.mutate({
          id: testCredentialId,
          name,
          tested_url: url || undefined,
          user_context: ctx || null,
          save_browser_session_intent: true,
          ...proxyPinPayload,
        });
        return;
      }

      // Any remaining temp credential is an orphan we're not reusing — either
      // we're in edit mode (the real credential gets updated below instead),
      // or "Save browser session" got unchecked after a completed test. Key
      // this on testCredentialId alone, not hasCompletedInlineTest: the
      // checkbox is togglable post-test with no side effect on testStatus, so
      // gating on testAndSave here silently leaked the temp credential (and
      // the secret it holds) whenever the box was unchecked before saving.
      if (testCredentialId) {
        const tempCredentialId = testCredentialId;
        getClient(credentialGetter)
          .then((client) => client.delete(`/credentials/${tempCredentialId}`))
          .catch(() => {
            // Best-effort cleanup
          });
      }

      // Capture intent before mutation — state will be reset in onSuccess
      // In edit mode, only trigger a background test if the user actually changed
      // credentials, user_context, or URL — not just because the checkbox was pre-checked.
      const hasEditModeChanges =
        !isEditMode ||
        editingGroups.values ||
        userContext.trim() !== (editingCredential?.user_context ?? "") ||
        testUrl.trim() !== (editingCredential?.tested_url ?? "");
      saveIntentRef.current = {
        shouldTestAfterSave:
          testAndSave &&
          (testStatus !== "completed" ||
            (isEditMode && hasCompletedInlineTest)) &&
          testUrl.trim() !== "" &&
          hasEditModeChanges,
        saveBrowserSessionIntent: testAndSave,
        testUrl: testUrl.trim(),
        userContext: userContext.trim(),
        name,
        proxyLocation: proxyPinPayload.proxy_location,
        proxySessionId: proxyPinPayload.proxy_session_id,
        proxyPinChanged,
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
        ...(vaultType === "custom" ? { vault_type: "custom" } : {}),
        ...credentialSaveProxyPinPayload,
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
      const billingAddress = buildBillingAddress(creditCardCredentialValues);
      const billingEmail = trimmedOrNull(
        creditCardCredentialValues.billingEmail,
      );
      const billingPhone = trimmedOrNull(
        creditCardCredentialValues.billingPhone,
      );
      const { metadata, hasIncompleteEntry } = buildMetadata(
        creditCardCredentialValues,
      );
      if (hasIncompleteEntry) {
        toast({
          title: "Error",
          description: "Metadata rows need both a key and a value",
          variant: "destructive",
        });
        return;
      }
      const credentialPayload: CreditCardCredential = {
        card_number: number,
        card_cvv: cardCode,
        card_exp_month: cardExpirationMonth,
        card_exp_year: cardExpirationYear,
        card_brand: cardBrand,
        card_holder_name: cardHolderName,
        ...(billingAddress ? { billing_address: billingAddress } : {}),
        ...(billingEmail ? { billing_email: billingEmail } : {}),
        ...(billingPhone ? { billing_phone: billingPhone } : {}),
        ...(metadata ? { metadata } : {}),
      };
      saveIntentRef.current = {
        shouldTestAfterSave: false,
        saveBrowserSessionIntent: false,
        testUrl: "",
        userContext: "",
        name,
        proxyLocation: proxyPinPayload.proxy_location,
        proxySessionId: proxyPinPayload.proxy_session_id,
        proxyPinChanged,
      };
      activeMutation.mutate({
        name,
        credential_type: "credit_card",
        credential: credentialPayload,
        ...(vaultType === "custom" ? { vault_type: "custom" } : {}),
        ...credentialSaveProxyPinPayload,
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

      saveIntentRef.current = {
        shouldTestAfterSave: false,
        saveBrowserSessionIntent: false,
        testUrl: "",
        userContext: "",
        name,
        proxyLocation: proxyPinPayload.proxy_location,
        proxySessionId: proxyPinPayload.proxy_session_id,
        proxyPinChanged,
      };
      activeMutation.mutate({
        name,
        credential_type: "secret",
        credential: {
          secret_value: secretValue,
          secret_label: secretLabel === "" ? null : secretLabel,
        },
        ...(vaultType === "custom" ? { vault_type: "custom" } : {}),
        ...credentialSaveProxyPinPayload,
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

  const customVaultCheckbox =
    hasCustomCredentialService && !isEditMode ? (
      <div className="flex items-center gap-3">
        <Checkbox
          id="use-custom-vault"
          checked={vaultType === "custom"}
          onCheckedChange={(checked) =>
            setVaultType(checked === true ? "custom" : "default")
          }
          disabled={isTestInProgress}
        />
        <Label
          htmlFor="use-custom-vault"
          className="cursor-pointer text-sm font-medium"
        >
          Store in Custom Credential Service
        </Label>
        <HelpTooltip content="Store this credential in your external credential service instead of the default Skyvern vault." />
      </div>
    ) : undefined;

  const shouldValidateAuthenticatorKey =
    type === CredentialModalTypes.PASSWORD &&
    (!isEditMode || editingGroups.values);
  const authenticatorKeyError = shouldValidateAuthenticatorKey
    ? getAuthenticatorKeyError(passwordCredentialValues)
    : null;

  const proxyPinContent = (
    <div className="space-y-3 border-t border-slate-700 pt-4">
      <div className="flex items-start gap-3">
        <Checkbox
          id="pin-residential-isp-proxy"
          checked={pinResidentialIspProxy}
          onCheckedChange={(checked) =>
            setPinResidentialIspProxy(checked === true)
          }
          disabled={isTestInProgress}
          className="mt-0.5"
        />
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Label
              htmlFor="pin-residential-isp-proxy"
              className="cursor-pointer text-sm font-medium"
            >
              Use a consistent IP address
            </Label>
            <HelpTooltip content="Routes this credential through the same residential IP each time to reduce account security prompts caused by changing IPs." />
          </div>
          <p className="text-xs leading-5 text-muted-foreground">
            Helps avoid extra 2FA, captchas, or temporary locks caused by
            signing in from a new location.
          </p>
          {pinResidentialIspProxy && existingProxyIdentity && (
            <div className="space-y-2">
              <p className="text-xs text-slate-300">
                Consistent IP active: identity {existingProxyIdentity}
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => setRotateProxyPin(true)}
                  disabled={isTestInProgress || rotateProxyPin}
                >
                  Rotate IP identity
                </Button>
                {rotateProxyPin && (
                  <span className="text-xs text-slate-300">
                    A new IP identity will be created when you save.
                  </span>
                )}
              </div>
            </div>
          )}
          {pinResidentialIspProxy && !existingProxyIdentity && (
            <p className="text-xs text-slate-300">
              Skyvern will create an IP identity for this credential when you
              save.
            </p>
          )}
        </div>
      </div>
    </div>
  );

  const credentialContent = (() => {
    if (type === CredentialModalTypes.PASSWORD) {
      return (
        <PasswordCredentialContent
          values={passwordCredentialValues}
          onChange={handlePasswordCredentialChange}
          url={testUrl}
          onUrlChange={setTestUrl}
          urlRequired={testAndSave}
          urlDisabled={isTestInProgress}
          editMode={isEditMode}
          editingGroups={editingGroups}
          onEnableEditName={handleEnableEditName}
          onEnableEditValues={handleEnableEditValues}
          totpError={authenticatorKeyError}
          authenticatorSaveError={authenticatorSaveError}
          beforeCredentialFields={customVaultCheckbox}
          afterUrl={
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
                <HelpTooltip content="Skyvern will log in using your credentials, verify success, and save the browser session. Future agent runs will skip the login form entirely because the saved session is already authenticated." />
              </div>

              {testAndSave && (
                <div className="space-y-1 pl-7">
                  <Label
                    htmlFor="user-context"
                    className="text-xs text-muted-foreground"
                  >
                    Login instructions (optional)
                  </Label>
                  {/* maxLength is intentionally lower than the backend's 1000-char limit (defense-in-depth) */}
                  <Textarea
                    id="user-context"
                    value={userContext}
                    onChange={(e) => setUserContext(e.target.value)}
                    placeholder='Describe the login flow, e.g. "Click the SSO button first, then enter Google credentials"'
                    disabled={isTestInProgress}
                    className="min-h-[60px] resize-y"
                    rows={2}
                    maxLength={500}
                  />
                </div>
              )}

              {isTestInProgress && (
                <div className="space-y-1 pl-7">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <ReloadIcon className="size-4 animate-spin" />
                    <span>{TEST_STATUS_MESSAGES[testMessageIndex]}</span>
                  </div>
                  {testWorkflowRunId && (
                    <a
                      href={`/runs/${testWorkflowRunId}/overview`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
                    >
                      <ExternalLinkIcon className="size-3" />
                      Watch Skyvern test login live
                    </a>
                  )}
                </div>
              )}
              {testStatus === "completed" && (
                <div className="flex items-center gap-2 pl-7 text-sm text-green-400">
                  <CheckCircledIcon className="size-4" />
                  <span>
                    {`Login test passed — saved browser session available for agents using ${getHostname(testUrl) ?? testUrl}`}
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
          }
        />
      );
    }
    if (type === CredentialModalTypes.CREDIT_CARD) {
      return (
        <CreditCardCredentialContent
          values={creditCardCredentialValues}
          onChange={setCreditCardCredentialValues}
          beforeCredentialFields={customVaultCheckbox}
          editMode={isEditMode}
          editingGroups={editingGroups}
          onEnableEditName={handleEnableEditName}
          onEnableEditValues={handleEnableEditValues}
        />
      );
    }
    return (
      <SecretCredentialContent
        values={secretCredentialValues}
        onChange={setSecretCredentialValues}
        beforeCredentialFields={customVaultCheckbox}
        editMode={isEditMode}
        editingGroups={editingGroups}
        onEnableEditName={handleEnableEditName}
        onEnableEditValues={handleEnableEditValues}
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
    if (authenticatorKeyError) {
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
    setTestWorkflowRunId(null);
    toast({
      title: "Test canceled",
      description: "The credential test has been canceled.",
    });
  }, [credentialGetter]);

  // Whether the Test button should be shown
  const showTestButton =
    testAndSave &&
    type === CredentialModalTypes.PASSWORD &&
    (!isEditMode || editingGroups.values);

  // Whether the Test button should be enabled
  const canTest =
    showTestButton &&
    testUrl.trim() !== "" &&
    passwordCredentialValues.username.trim() !== "" &&
    passwordCredentialValues.password.trim() !== "" &&
    !authenticatorKeyError &&
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
      <DialogContent className="max-h-[90vh] w-[700px] max-w-[700px] overflow-y-auto [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:border-2 [&::-webkit-scrollbar-thumb]:border-slate-100 [&::-webkit-scrollbar-thumb]:bg-slate-300 dark:[&::-webkit-scrollbar-thumb]:border-slate-800 dark:[&::-webkit-scrollbar-thumb]:bg-slate-600 [&::-webkit-scrollbar-track]:bg-slate-100 dark:[&::-webkit-scrollbar-track]:bg-slate-800 [&::-webkit-scrollbar]:w-2">
        <DialogHeader>
          <DialogTitle className="font-bold">
            {isEditMode ? "Edit Credential" : "Add Credential"}
          </DialogTitle>
        </DialogHeader>
        {isEditMode && editingGroups.values && (
          <Alert variant="warning">
            <ExclamationTriangleIcon className="size-4" />
            <AlertDescription>
              For security, saved values are never retrieved. Changing any field
              other than the credential name requires re-entering all fields,
              including passwords and 2FA settings.
            </AlertDescription>
          </Alert>
        )}
        {credentialContent}
        {proxyPinContent}

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
