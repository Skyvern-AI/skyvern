import {
  isCreditCardCredential,
  isPasswordCredential,
  isSecretCredential,
  type CredentialApiResponse,
  type CredentialTotpCodeResponse,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { SelectionCheckbox } from "@/components/SelectionCheckbox";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  CopyIcon,
  EyeNoneIcon,
  EyeOpenIcon,
  ExclamationTriangleIcon,
  ExternalLinkIcon,
  Pencil1Icon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { DeleteCredentialButton } from "./DeleteCredentialButton";
import { CredentialFolderSelector } from "./CredentialFolderSelector";
import { getHostname } from "@/util/getHostname";
import { CredentialsModal } from "./CredentialsModal";
import { credentialTypeToModalType } from "./useCredentialModalState";
import { SaveIcon } from "@/components/icons/SaveIcon";
import { useCredentialTestStore } from "@/store/useCredentialTestStore";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { copyText } from "@/util/copyText";
import { cn } from "@/util/utils";
import { getCredentialErrorMessage } from "./authenticatorSaveError";

type Props = {
  credential: CredentialApiResponse;
  onStartBackgroundTest?: (
    credentialId: string,
    url: string,
    userContext?: string,
  ) => void;
  index?: number;
  selected?: boolean;
  hasSelection?: boolean;
  onSelect?: (index: number, shiftKey: boolean) => void;
};

function formatTotpCode(code: string): string {
  const splitAt = Math.ceil(code.length / 2);
  return `${code.slice(0, splitAt)} ${code.slice(splitAt)}`;
}

function CredentialTotpCodePreview({ credentialId }: { credentialId: string }) {
  const credentialGetter = useCredentialGetter();
  const isMountedRef = useRef(true);
  const [totpCode, setTotpCode] = useState<CredentialTotpCodeResponse | null>(
    null,
  );
  const [secondsRemaining, setSecondsRemaining] = useState<number | null>(null);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [isCodeVisible, setIsCodeVisible] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (expiresAt === null) {
      return;
    }

    const updateRemaining = () => {
      const nextSecondsRemaining = Math.max(
        0,
        Math.ceil((expiresAt - Date.now()) / 1000),
      );
      if (nextSecondsRemaining <= 0) {
        setTotpCode(null);
        setSecondsRemaining(null);
        setExpiresAt(null);
        setIsCodeVisible(false);
        setError("2FA code expired. Refresh to load a new code.");
        return;
      }
      setSecondsRemaining(nextSecondsRemaining);
    };

    updateRemaining();
    const interval = window.setInterval(updateRemaining, 1000);
    return () => window.clearInterval(interval);
  }, [expiresAt]);

  const fetchTotpCode = async ({
    reveal = true,
  }: { reveal?: boolean } = {}) => {
    setIsLoading(true);
    setError(null);
    try {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<CredentialTotpCodeResponse>(
        `/credentials/${credentialId}/totp-code`,
      );
      if (!isMountedRef.current) {
        return;
      }
      setTotpCode(response.data);
      setSecondsRemaining(response.data.seconds_remaining);
      setExpiresAt(Date.now() + response.data.seconds_remaining * 1000);
      setIsCodeVisible(reveal);
    } catch (caught) {
      if (!isMountedRef.current) {
        return;
      }
      setTotpCode(null);
      setSecondsRemaining(null);
      setExpiresAt(null);
      setIsCodeVisible(false);
      setError(getCredentialErrorMessage(caught) ?? "Unable to load code");
    } finally {
      if (isMountedRef.current) {
        setIsLoading(false);
      }
    }
  };

  const hasValidTotpCode =
    totpCode !== null && secondsRemaining !== null && secondsRemaining > 0;
  const visibleTotpCode =
    isCodeVisible && hasValidTotpCode ? totpCode.code : null;
  const canCopyCode = Boolean(visibleTotpCode);

  const handleToggleCodeVisibility = () => {
    if (isCodeVisible) {
      setIsCodeVisible(false);
      return;
    }

    if (hasValidTotpCode) {
      setIsCodeVisible(true);
      return;
    }

    void fetchTotpCode();
  };

  const handleCopyCode = async () => {
    if (!visibleTotpCode) {
      return;
    }
    // Display formatting adds spacing; copy the raw code for paste targets.
    const copied = await copyText(visibleTotpCode);
    toast({
      title: copied ? "Copied code" : "Copy failed",
      description: copied
        ? "The current 2FA code was copied to your clipboard."
        : "Could not copy the current 2FA code.",
      variant: copied ? "success" : "destructive",
    });
  };

  return (
    <div className="space-y-1">
      <div className="flex min-h-6 items-center gap-2">
        <span
          className={cn(
            "inline-block min-w-[5.75rem] font-mono text-sm tabular-nums text-foreground",
            !isCodeVisible && "text-muted-foreground",
          )}
        >
          {isCodeVisible && totpCode
            ? formatTotpCode(totpCode.code)
            : "••••••••"}
        </span>
        <span
          className={cn(
            "inline-block w-7 text-right text-xs text-slate-400",
            (!isCodeVisible || !hasValidTotpCode) && "invisible",
          )}
        >
          {secondsRemaining ?? 0}s
        </span>
        <TooltipProvider delayDuration={200}>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size="icon"
                variant="tertiary"
                className="h-6 w-6"
                onClick={handleToggleCodeVisibility}
                disabled={isLoading}
                aria-label={isCodeVisible ? "Hide 2FA code" : "Show 2FA code"}
              >
                {isCodeVisible ? (
                  <EyeNoneIcon className="size-3.5" />
                ) : (
                  <EyeOpenIcon className="size-3.5" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {isCodeVisible ? "Hide 2FA code" : "Show 2FA code"}
            </TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size="icon"
                variant="tertiary"
                className="h-6 w-6"
                onClick={() => void fetchTotpCode({ reveal: isCodeVisible })}
                disabled={isLoading}
                aria-label="Refresh 2FA code"
              >
                <ReloadIcon
                  className={cn("size-3.5", isLoading && "animate-spin")}
                />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Refresh 2FA code</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size="icon"
                variant="tertiary"
                className="h-6 w-6"
                onClick={handleCopyCode}
                disabled={!canCopyCode}
                aria-label="Copy 2FA code"
              >
                <CopyIcon className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Copy 2FA code</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

function CredentialItem({
  credential,
  onStartBackgroundTest,
  index = -1,
  selected = false,
  hasSelection = false,
  onSelect,
}: Props) {
  const [editModalOpen, setEditModalOpen] = useState(false);
  const activeTest = useCredentialTestStore((s) =>
    s.activeTest?.credentialId === credential.credential_id
      ? s.activeTest
      : null,
  );
  const credentialData = credential.credential;
  const modalType = credentialTypeToModalType(credential.credential_type);
  // Offer re-save for any credential that already has a saved profile, not only ones
  // flagged with the save-session intent.
  const canResaveSession = Boolean(
    credential.save_browser_session_intent || credential.browser_profile_id,
  );
  const handleResaveSession = () => {
    if (credential.tested_url && onStartBackgroundTest) {
      onStartBackgroundTest(
        credential.credential_id,
        credential.tested_url,
        credential.user_context ?? undefined,
      );
    } else {
      // No URL on record to test against — fall back to the editor where it's entered.
      setEditModalOpen(true);
    }
  };
  const getTotpTypeDisplay = (totpType: string) => {
    switch (totpType) {
      case "authenticator":
        return "Authenticator App";
      case "email":
        return "Email";
      case "text":
        return "Text Message";
      case "none":
      default:
        return "";
    }
  };

  let credentialDetails = null;

  if (isPasswordCredential(credentialData)) {
    credentialDetails = (
      <div className="border-l pl-5">
        <div className="flex gap-5">
          <div className="shrink-0 space-y-2">
            <p className="text-sm text-neutral-600 dark:text-slate-400">
              Username/Email
            </p>
            <p className="text-sm text-neutral-600 dark:text-slate-400">
              Password
            </p>
            {credentialData.totp_type !== "none" && (
              <p className="text-sm text-neutral-600 dark:text-slate-400">
                2FA Type
              </p>
            )}
            {credentialData.totp_type === "authenticator" && (
              <p className="text-sm text-neutral-600 dark:text-slate-400">
                2FA Code
              </p>
            )}
          </div>
          <div className="space-y-2">
            <p className="text-sm">{credentialData.username}</p>
            <p className="text-sm">{"••••••••"}</p>
            {credentialData.totp_type !== "none" && (
              <p className="text-sm">
                {getTotpTypeDisplay(credentialData.totp_type)}
              </p>
            )}
            {credentialData.totp_type === "authenticator" && (
              <CredentialTotpCodePreview
                credentialId={credential.credential_id}
              />
            )}
          </div>
        </div>
      </div>
    );
  } else if (isCreditCardCredential(credentialData)) {
    credentialDetails = (
      <div className="flex gap-5 border-l pl-5">
        <div className="flex gap-5">
          <div className="shrink-0 space-y-2">
            <p className="text-sm text-neutral-600 dark:text-slate-400">
              Card Number
            </p>
            <p className="text-sm text-neutral-600 dark:text-slate-400">
              Brand
            </p>
          </div>
        </div>
        <div className="flex gap-5">
          <div className="shrink-0 space-y-2">
            <p className="text-sm">
              {"************" + credentialData.last_four}
            </p>
            <p className="text-sm">{credentialData.brand}</p>
          </div>
        </div>
      </div>
    );
  } else if (isSecretCredential(credentialData)) {
    credentialDetails = (
      <div className="flex gap-5 border-l pl-5">
        <div className="shrink-0 space-y-2">
          <p className="text-sm text-neutral-600 dark:text-slate-400">
            Secret Value
          </p>
          {credentialData.secret_label ? (
            <p className="text-sm text-neutral-600 dark:text-slate-400">Type</p>
          ) : null}
        </div>
        <div className="space-y-2">
          <p className="text-sm">{"************"}</p>
          {credentialData.secret_label ? (
            <p className="text-sm">{credentialData.secret_label}</p>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <div
      className="group/row flex gap-5 rounded-lg bg-slate-elevation2 p-4 data-[state=selected]:ring-1 data-[state=selected]:ring-primary"
      data-state={selected ? "selected" : undefined}
    >
      {onSelect && (
        <div className="self-start pt-1">
          <SelectionCheckbox
            index={index}
            checked={selected}
            hasSelection={hasSelection}
            onSelect={onSelect}
            ariaLabel={`Select ${credential.name}`}
          />
        </div>
      )}
      <div className="w-48 space-y-2">
        <p className="w-full truncate" title={credential.name}>
          {credential.name}
        </p>
        <p className="text-sm text-neutral-600 dark:text-slate-400">
          {credential.credential_id}
        </p>
        {activeTest && (
          <div className="flex items-center gap-1 text-xs">
            <ReloadIcon className="size-3 animate-spin text-blue-400" />
            <a
              href={`/runs/${activeTest.workflowRunId}/overview`}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-blue-400 hover:text-blue-300"
            >
              Testing login
              <ExternalLinkIcon className="size-3" />
            </a>
          </div>
        )}
        {credential.browser_profile_id && (
          <div className="flex items-center gap-1 text-xs">
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="flex items-center text-green-400">
                    <SaveIcon className="size-4" />
                  </span>
                </TooltipTrigger>
                <TooltipContent>Saved browser session</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            {credential.tested_url && (
              <span className="text-muted-foreground">
                {getHostname(credential.tested_url) ?? credential.tested_url}
              </span>
            )}
            <span className="text-muted-foreground">·</span>
            <Link
              to={`/browser-profiles/${credential.browser_profile_id}`}
              className="text-blue-400 hover:text-blue-300"
            >
              Fix the saved login by hand
            </Link>
          </div>
        )}
        {canResaveSession && !credential.browser_profile_id && !activeTest && (
          <div className="flex items-center gap-1 text-xs">
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="flex items-center text-red-400">
                    <ExclamationTriangleIcon className="size-4" />
                  </span>
                </TooltipTrigger>
                <TooltipContent>Browser session was not saved</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <span className="text-red-400">Not saved</span>
          </div>
        )}
      </div>
      {credentialDetails}
      <div className="ml-auto flex gap-1">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <div>
                <CredentialFolderSelector
                  credentialId={credential.credential_id}
                  currentFolderId={credential.folder_id ?? null}
                />
              </div>
            </TooltipTrigger>
            <TooltipContent>Assign to Folder</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        {canResaveSession && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon"
                  variant="tertiary"
                  className="h-8 w-9"
                  disabled={Boolean(activeTest)}
                  onClick={handleResaveSession}
                  aria-label={
                    credential.browser_profile_id
                      ? "Refresh saved session"
                      : "Retry saving session"
                  }
                >
                  <ReloadIcon className="size-5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {credential.browser_profile_id
                  ? "Refresh saved session"
                  : "Retry saving session"}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size="icon"
                variant="tertiary"
                className="h-8 w-9"
                onClick={() => setEditModalOpen(true)}
                aria-label="Edit credential"
              >
                <Pencil1Icon className="size-5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Edit Credential</TooltipContent>
          </Tooltip>
        </TooltipProvider>
        <DeleteCredentialButton credential={credential} />
      </div>
      <CredentialsModal
        isOpen={editModalOpen}
        onOpenChange={setEditModalOpen}
        editingCredential={credential}
        overrideType={modalType}
        onStartBackgroundTest={onStartBackgroundTest}
      />
    </div>
  );
}

export { CredentialItem };
