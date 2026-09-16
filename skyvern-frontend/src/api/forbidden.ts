import { isAxiosError } from "axios";

const UNAUTHORIZED_STATUS = 401;
const FORBIDDEN_STATUS = 403;

// A 401/403 while polling a resource means the caller is not (or no longer)
// authorized for it — an expired browser/debug session, or a session that
// belongs to another org. Re-issuing the same request can only produce another
// 401/403, so every poller must stop instead of retrying at the same interval.
function isForbiddenError(error: unknown): boolean {
  if (!isAxiosError(error)) {
    return false;
  }
  const status = error.response?.status;
  return status === UNAUTHORIZED_STATUS || status === FORBIDDEN_STATUS;
}

export { isForbiddenError };
