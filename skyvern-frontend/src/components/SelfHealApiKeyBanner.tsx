import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AuthStatusValue,
  useAuthDiagnostics,
} from "@/hooks/useAuthDiagnostics";
import { useAuthIssueStore } from "@/store/AuthIssueStore";

type BannerStatus =
  | Exclude<AuthStatusValue, "ok">
  | "request_auth_error"
  | "error";

function getCopy(status: BannerStatus): { title: string; description: string } {
  switch (status) {
    case "missing_api_key":
      return {
        title: "Frontend API key missing",
        description:
          "The UI is not sending an x-api-key header. Authenticated requests will fail until local auth is repaired.",
      };
    case "invalid_format":
      return {
        title: "Skyvern API key is invalid",
        description: "The configured key cannot be decoded.",
      };
    case "invalid":
      return {
        title: "Skyvern API key not recognized",
        description: "The backend rejected the configured key.",
      };
    case "expired":
      return {
        title: "Skyvern API key expired",
        description: "The current key is no longer valid.",
      };
    case "not_found":
      return {
        title: "Local organization missing",
        description: "The backend could not find the local organization.",
      };
    case "request_auth_error":
      return {
        title: "Skyvern API requests are unauthorized",
        description:
          "The backend rejected a UI request. This usually means the frontend API key, backend API key, and local Docker database are out of sync.",
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

function SelfHealApiKeyBanner() {
  const diagnosticsQuery = useAuthDiagnostics();
  const authIssue = useAuthIssueStore((state) => state.issue);

  const { data, error } = diagnosticsQuery;

  const rawStatus = data?.status;
  const bannerStatus: BannerStatus | null = error
    ? "error"
    : rawStatus && rawStatus !== "ok"
      ? rawStatus
      : authIssue
        ? "request_auth_error"
        : null;

  if (!bannerStatus) {
    return null;
  }

  const copy = getCopy(bannerStatus);
  const queryErrorMessage = error?.message ?? null;

  return (
    <div className="px-4 pt-4">
      <Alert className="flex flex-col items-center gap-2 border-slate-700 bg-slate-900 text-slate-50">
        <AlertTitle className="text-center text-base font-semibold tracking-wide">
          {copy.title}
        </AlertTitle>
        <AlertDescription className="space-y-3 text-center text-sm leading-6">
          {bannerStatus !== "error" ? (
            <>
              <p>
                {copy.description} Run <code>skyvern doctor --fix</code> in the
                project directory, then restart local services if prompted.
              </p>
              {data?.detail ? (
                <p className="text-xs text-slate-300">{data.detail}</p>
              ) : null}
              {authIssue ? (
                <p className="text-xs text-slate-300">
                  Last failed request: HTTP {authIssue.statusCode}
                  {authIssue.path ? ` at ${authIssue.path}` : ""}
                  {authIssue.detail ? ` (${authIssue.detail})` : ""}
                </p>
              ) : null}
            </>
          ) : (
            <p>{copy.description}</p>
          )}
          {queryErrorMessage ? (
            <p className="text-xs text-rose-200">{queryErrorMessage}</p>
          ) : null}
        </AlertDescription>
      </Alert>
    </div>
  );
}

export { SelfHealApiKeyBanner };
