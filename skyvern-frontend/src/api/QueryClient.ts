import { QueryClient } from "@tanstack/react-query";

import { isTransientNetworkError } from "./transientNetworkError";

const MAX_TRANSIENT_NETWORK_RETRIES = 2;

function retryTransientNetworkFailures(
  failureCount: number,
  error: unknown,
): boolean {
  return (
    isTransientNetworkError(error) &&
    failureCount < MAX_TRANSIENT_NETWORK_RETRIES
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000, // 5 minutes
      retry: retryTransientNetworkFailures,
    },
  },
});

export { queryClient, retryTransientNetworkFailures };
