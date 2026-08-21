import { useSyncExternalStore } from "react";

import {
  getRuntimeApiKey,
  getRuntimeApiKeyExpiresAt,
  subscribeToRuntimeCredential,
} from "@/util/env";

type RuntimeCredential = {
  apiKey: string | null;
  expiresAt: number | null;
};

let snapshot: RuntimeCredential = { apiKey: null, expiresAt: null };

function getSnapshot(): RuntimeCredential {
  const apiKey = getRuntimeApiKey();
  const expiresAt = getRuntimeApiKeyExpiresAt();
  // useSyncExternalStore compares snapshots by identity, so only build a new one on a real change.
  if (snapshot.apiKey !== apiKey || snapshot.expiresAt !== expiresAt) {
    snapshot = { apiKey, expiresAt };
  }
  return snapshot;
}

function useRuntimeCredential(): RuntimeCredential {
  return useSyncExternalStore(
    subscribeToRuntimeCredential,
    getSnapshot,
    getSnapshot,
  );
}

export { useRuntimeCredential };
export type { RuntimeCredential };
