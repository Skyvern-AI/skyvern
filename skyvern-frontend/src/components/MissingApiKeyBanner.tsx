import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { getClient } from "@/api/AxiosClient";
import {
  AuthStatusValue,
  useAuthDiagnostics,
} from "@/hooks/useAuthDiagnostics";

type BannerStatus = Exclude<AuthStatusValue, "ok"> | "error";

function getCopy(status: BannerStatus): { title: string; description: string } {
  switch (status) {
    case "missing_env":
      return {
        title: "Skyvern API key missing",
        description:
          "All requests from the UI to the local backend will fail until a valid key is configured.",
      };
    case "invalid_format":
      return {
        title: "Skyvern API key is invalid",
        description:
          "The configured key cannot be decoded. Regenerate a new key to continue using the UI.",
      };
    case "invalid":
      return {
        title: "Skyvern API key not recognized",
        description:
          "The backend rejected the configured key. Regenerate it to refresh local auth.",
      };
    case "expired":
      return {
        title: "Skyvern API key expired",
        description:
          "The current key is no longer valid. Generate a fresh key to restore connectivity.",
      };
    case "not_found":
      return {
        title: "Local organization missing",
        description:
          "The backend could not find the Skyvern-local organization. Regenerate the key to recreate it.",
      };
    case "error":
    default:
      return {
        title: "Unable to verify Skyvern API key",
        description:
          "The UI could not reach the diagnostics endpoint. Ensure the backend is running locally.",
      };
  }
}

function MissingApiKeyBanner() {
  const diagnosticsQuery = useAuthDiagnostics();
  const [isRepairing, setIsRepairing] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const isDevMode = import.meta.env.MODE === "development";
  if (!isDevMode) {
    return null;
  }

  const { data, error, isLoading, refetch } = diagnosticsQuery;

  const rawStatus = data?.status;
  const bannerStatus: BannerStatus | null = error
    ? "error"
    : rawStatus && rawStatus !== "ok"
      ? rawStatus
      : null;

  if (!bannerStatus && !statusMessage && !errorMessage) {
    if (isLoading) {
      return null;
    }
    return null;
  }

  const copy = getCopy(bannerStatus ?? "missing_env");
  const queryErrorMessage = error?.message ?? null;

  const handleRepair = async () => {
    setIsRepairing(true);
    setStatusMessage(null);
    setErrorMessage(null);
    try {
      const client = await getClient(null);
      const response = await client.post<{ fingerprint?: string }>(
        "/internal/auth/repair",
      );
      const fingerprint = response.data.fingerprint
        ? ` (fingerprint ${response.data.fingerprint})`
        : "";
      setStatusMessage(
        `API key regenerated${fingerprint}. The UI should reload automatically due to an .env update...`,
      );
      await refetch({ throwOnError: false });
    } catch (fetchError) {
      const message =
        fetchError instanceof Error
          ? fetchError.message
          : "Unable to repair API key";
      setErrorMessage(message);
    } finally {
      setIsRepairing(false);
    }
  };

  return (
    <div className="px-4 pt-4">
      <Alert className="flex flex-col items-center gap-2 border-slate-700 bg-slate-900 text-slate-50">
        <AlertTitle className="text-center text-base font-semibold tracking-wide">
          {copy.title}
        </AlertTitle>
        <AlertDescription className="space-y-3 text-center text-sm leading-6">
          <p>
            {copy.description} Update <code>VITE_SKYVERN_API_KEY</code> in{" "}
            <code className="mx-1">skyvern-frontend/.env</code>
            by running <code>skyvern init</code> or click the button below to
            regenerate it automatically.
          </p>
          <div className="flex justify-center">
            <Button
              onClick={handleRepair}
              disabled={isRepairing}
              variant="secondary"
            >
              {isRepairing ? "Regenerating…" : "Regenerate API key"}
            </Button>
          </div>
          {statusMessage ? (
            <p className="text-xs text-slate-200">{statusMessage}</p>
          ) : null}
          {errorMessage ? (
            <p className="text-xs text-rose-200">{errorMessage}</p>
          ) : null}
          {queryErrorMessage && !errorMessage ? (
            <p className="text-xs text-rose-200">{queryErrorMessage}</p>
          ) : null}
        </AlertDescription>
      </Alert>
    </div>
  );
}

export { MissingApiKeyBanner };
