import { AxiosError } from "axios";

export function getCopilotFailureLogFields(error: unknown) {
  const isSseHttpError =
    error instanceof Error &&
    error.name === "SseHttpError" &&
    "status" in error &&
    typeof error.status === "number";
  const isAxiosError = error instanceof AxiosError;

  return {
    error_type: error instanceof Error ? error.name : typeof error,
    http_status: isSseHttpError
      ? error.status
      : isAxiosError
        ? (error.response?.status ?? null)
        : null,
    ...(!isSseHttpError &&
    !isAxiosError &&
    error instanceof Error &&
    !(error instanceof SyntaxError)
      ? { error_message: error.message }
      : {}),
  };
}
