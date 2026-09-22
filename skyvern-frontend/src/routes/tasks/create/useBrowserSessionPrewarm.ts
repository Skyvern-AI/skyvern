import { useCallback, useRef } from "react";
import { useAuth } from "@clerk/clerk-react";
import type { ProxyLocation } from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCurrentOrgId } from "@/hooks/useCurrentOrgId";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { BROWSER_SESSION_PREWARM_FLAG } from "@/util/featureFlags";

const PREWARM_DISPATCH_WAIT_TIMEOUT_MS = 10_000;

type PrewarmRequest = {
  request: Promise<void>;
  expiresAt: number;
  unlockTimeout: number;
};

// Shared across Home/onboarding mounts and scoped to the authenticated identity.
// The server binding is the final dedupe guard; the client lock only suppresses
// redundant requests during normal SPA navigation.
const prewarmRequests = new Map<string, PrewarmRequest>();
let latestPrewarmRequest: PrewarmRequest | undefined;

function clearPrewarmRequest(identityKey: string, entry: PrewarmRequest) {
  if (prewarmRequests.get(identityKey) === entry) {
    prewarmRequests.delete(identityKey);
  }
  if (latestPrewarmRequest === entry) latestPrewarmRequest = undefined;
  window.clearTimeout(entry.unlockTimeout);
}

function waitForBrowserSessionPrewarm(): Promise<void> {
  const entry = latestPrewarmRequest;
  if (!entry) return Promise.resolve();

  const remainingWait = Math.max(0, entry.expiresAt - Date.now());
  if (remainingWait === 0) return Promise.resolve();

  // The endpoint returns after dispatch, not browser startup. Keep the POST
  // alive so a slow response can still establish its binding, but never let a
  // dead connection block the editor indefinitely.
  return new Promise((resolve) => {
    const timeout = window.setTimeout(resolve, remainingWait);
    void entry.request.then(() => {
      window.clearTimeout(timeout);
      resolve();
    });
  });
}

function useBrowserSessionPrewarm(proxyLocation: ProxyLocation) {
  const credentialGetter = useCredentialGetter();
  const { userId } = useAuth();
  const organizationId = useCurrentOrgId();
  const enabled = useFeatureFlag(BROWSER_SESSION_PREWARM_FLAG) === true;
  const attemptedIdentity = useRef<string>();

  return useCallback(
    (value: string) => {
      if (!enabled || !userId || !organizationId || !value.trim()) return;
      const identityKey = JSON.stringify([organizationId, userId]);
      if (attemptedIdentity.current === identityKey) return;
      attemptedIdentity.current = identityKey;

      const existing = prewarmRequests.get(identityKey);
      if (existing && existing.expiresAt > Date.now()) return;

      const entry = {
        request: Promise.resolve(),
        expiresAt: Date.now() + PREWARM_DISPATCH_WAIT_TIMEOUT_MS,
        unlockTimeout: 0,
      } satisfies PrewarmRequest;
      entry.request = getClient(credentialGetter, "sans-api-v1")
        .then((client) =>
          client.post("/debug-session/prewarm", {
            proxy_location: proxyLocation,
          }),
        )
        .then(() => undefined)
        .catch(() => undefined)
        .finally(() => {
          clearPrewarmRequest(identityKey, entry);
        });
      entry.unlockTimeout = window.setTimeout(
        () => clearPrewarmRequest(identityKey, entry),
        PREWARM_DISPATCH_WAIT_TIMEOUT_MS,
      );
      prewarmRequests.set(identityKey, entry);
      latestPrewarmRequest = entry;
    },
    [credentialGetter, enabled, organizationId, proxyLocation, userId],
  );
}

export {
  PREWARM_DISPATCH_WAIT_TIMEOUT_MS,
  useBrowserSessionPrewarm,
  waitForBrowserSessionPrewarm,
};
