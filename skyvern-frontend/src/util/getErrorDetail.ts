// API errors carry the actionable text in the FastAPI `detail`, not the generic
// axios "Request failed with status code N" message. Prefer the detail and fall
// back to the message so error UI surfaces the server's guidance.
function getErrorDetail(error: unknown): string | undefined {
  if (error && typeof error === "object") {
    const detail = (error as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string") {
      return detail;
    }
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") {
      return message;
    }
  }
  return undefined;
}

export { getErrorDetail };
