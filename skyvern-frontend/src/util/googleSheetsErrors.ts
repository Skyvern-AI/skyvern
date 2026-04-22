export function isReconnectRequired(error: unknown): boolean {
  const typed = error as {
    response?: {
      status?: number;
      data?: { code?: string; detail?: { code?: string } | string };
    };
  };
  if (typed?.response?.status !== 409) return false;
  const data = typed.response.data;
  if (!data) return false;
  if (data.code === "reconnect_required") return true;
  if (
    typeof data.detail === "object" &&
    data.detail !== null &&
    data.detail.code === "reconnect_required"
  ) {
    return true;
  }
  return false;
}
