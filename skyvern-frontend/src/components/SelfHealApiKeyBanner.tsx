import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/use-toast";
import { getClient, setApiKeyHeader } from "@/api/AxiosClient";
import {
  AuthStatusValue,
  useAuthDiagnostics,
} from "@/hooks/useAuthDiagnostics";

type BannerStatus = Exclude<AuthStatusValue, "ok"> | "error";

/** Return the user-facing title and description for the given banner status. */
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
          "The backend could not find the Skyvern-local organization. Ensure the API key is entered without quotation marks, then regenerate the key to recreate it.",
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

/** Banner that diagnoses API-key issues and offers one-click regeneration. */
function SelfHealApiKeyBanner() {
  const diagnosticsQuery = useAuthDiagnostics();
  const { toast } = useToast();
  const [isRepairing, setIsRepairing] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const isProductionBuild = !import.meta.env.DEV;

  const { data, error, refetch } = diagnosticsQuery;

  const rawStatus = data?.status;
  const bannerStatus: BannerStatus | null = error
    ? "error"
    : rawStatus && rawStatus !== "ok"
      ? rawStatus
      : null;

  if (!bannerStatus && !errorMessage && !isRepairing) return null;

  const copy = getCopy(bannerStatus ?? "missing_env");
  const queryErrorMessage = error?.message ?? null;

  const handleRepair = async () => {
    setIsRepairing(true);
    setErrorMessage(null);

    try {
      const client = await getClient(null);

      const response = await client.post<{
        fingerprint?: string;
        api_key?: string;
        backend_env_path?: string;
        frontend_env_path?: string;
      }>("/internal/auth/repair");

      const {
        fingerprint,
        api_key: apiKey,
        backend_env_path: backendEnvPath,
        frontend_env_path: frontendEnvPath,
      } = response.data;

      if (!apiKey) {
        throw new Error("Repair succeeded but no API key was returned.");
      }

      setApiKeyHeader(apiKey);

      const fingerprintSuffix = fingerprint
        ? ` (fingerprint ${fingerprint})`
        : "";

      const pathsElements = [];
      if (backendEnvPath) {
        pathsElements.push(<div key="backend">Backend: {backendEnvPath}</div>);
      }
      if (frontendEnvPath) {
        pathsElements.push(
          <div key="frontend">Frontend: {frontendEnvPath}</div>,
        );
      }

      toast({
        title: "API key regenerated",
        description: (
          <div className="space-y-2">
            <div>
              Your API key was regenerated and is now used automatically
              {fingerprintSuffix}. The key is persisted in sessionStorage under{" "}
              <code>skyvern.apiKey</code> and written to:
            </div>

            {pathsElements.length > 0 && (
              <div className="ml-3 space-y-1">{pathsElements}</div>
            )}

            <div>
              To view or update the key, open DevTools, go to Application
              (Chrome/Edge) or Storage (Firefox), select Session Storage, and
              inspect <code>skyvern.apiKey</code>. Ensure it matches your{" "}
              <code>.env</code> file exactly and contains no quotes or non-ASCII
              characters.
            </div>

            {isProductionBuild && (
              <div>
                Restart the UI server for more robust API key persistence.
              </div>
            )}
          </div>
        ),
      });

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
            <code>skyvern-frontend/.env</code> by running{" "}
            <code>skyvern init</code> or click the button below to regenerate it
            automatically.
          </p>

          {isProductionBuild && (
            <p className="text-yellow-300">
              In production builds, regenerated keys are stored in
              sessionStorage. Closing the tab will lose the key.
            </p>
          )}

          <div className="flex justify-center">
            <Button
              onClick={handleRepair}
              disabled={isRepairing}
              variant="secondary"
            >
              {isRepairing ? "Regenerating…" : "Regenerate API key"}
            </Button>
          </div>

          {errorMessage && (
            <p className="text-xs text-rose-200">{errorMessage}</p>
          )}

          {queryErrorMessage && !errorMessage && (
            <p className="text-xs text-rose-200">{queryErrorMessage}</p>
          )}
        </AlertDescription>
      </Alert>
    </div>
  );
}

export { SelfHealApiKeyBanner };
