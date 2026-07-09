import { AxiosError, isAxiosError, isCancel } from "axios";

function isTransientNetworkError(error: unknown): boolean {
  if (!isAxiosError(error)) {
    return false;
  }
  if (isCancel(error) || error.code === AxiosError.ERR_CANCELED) {
    return false;
  }
  return error.response === undefined;
}

export { isTransientNetworkError };
